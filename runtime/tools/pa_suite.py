"""pa-full-suite's run logic (t-056). Python standard library only (3.7+). Loaded by pa_hooks.py from its own
folder; the pa_hooks module is passed in (`h`) for its helpers, so nothing here copies them.

  pa-full-suite [--again]                          run the full suite once for this worktree's tree, recorded in
                                                   content/suites.jsonl; a passing clean tree is not run again
                                                   unless --again
  pa-full-suite --last [--commit C|--tree T]       the last recorded run (default: this worktree's HEAD tree)
  pa-full-suite --report                           the suite's parts by time, slowest median first
  pa-full-suite --module test_x                    one unit module: tests/run-all.sh --module test_x as the command
                                                   (the same slot, part limit, progress file and record)
  pa-full-suite --status [--commit C]              this worktree's latest run (or C's; outside a worktree, every run
                                                   not done): waiting, running (parts done, the parts running now) or done
  pa-full-suite --wait [--commit C] [--timeout S]  waits for that run (a heartbeat line every minute) and exits with its
                                                   code; 75 when it still runs after S (default 540) s: --wait again

A run (t-076) names its output file first (evidence/<task>/full-suite-<run>.out, the whole output) and keeps a
progress file, HERDR_PA_HOME/suite-runs/<run>.json, which its tests/run-all.sh fills in with its parts; every
run-all.sh run with parts has one (pa_suite.py slot writes it). The slot holder also stops a part that runs past
max(PA_PART_LIMIT or PART_FLOOR, 3x its median in the suite record) and marks it hung.

The machine cap: at most CAP suite runs at once per user (slots under HERDR_PA_HOME, i.e. ~/.config/herdr-pa,
never a space). A slot is an flock on suite-slots/slot-<i>, so the kernel frees it when its holder dies.

Run as a script (t-072), it is how tests/run-all.sh takes the same slot, full or --affected:
  python3 pa_suite.py slot --for <what> [--worker W] [--dir D] -- <command...>
waits for a slot (saying for whom), runs the command under it and exits with its code (124: no slot came free).
A caller that holds a slot already (pa-full-suite) names it in PA_SUITE_SLOT; it is trusted only when that slot
is locked and its recorded holder is an ancestor of this process, so the run under it does not wait for itself. With
no trusted marker, any locked slot of this home whose holder is an ancestor counts too (an older pa-full-suite sets
none). A wait ends after SLOT_WAIT_LIMIT s (PA_SUITE_WAIT_LIMIT) or once this process's caller is gone, never later.
The command gets PA_SUITE_SLOT, PA_SUITE_SLOT_PID (this process) and PA_SUITE_HOLD (the seconds waited, or held
by the caller). Script mode never loads pa.py (run-all.sh's test fixture stubs it): HERDR_PA_HOME and a UTC clock.
"""
import argparse
import calendar
import datetime
import fcntl
import glob
import json
import os
import re
import signal
import statistics
import subprocess
import sys
import threading
import time
import uuid

if __name__ == "__main__":
    pa = None  # the slot entry point: see the top
else:
    try:
        import pa
    except ImportError:  # loaded from a module that dropped it: pa_hooks sets it after loading
        pa = None

CAP = 1  # one at a time: two overlapping took 8-10 min each, two in a row ~4 min together (plan.md, rule r-worker-mix)
POLL = 2
WAIT_LIMIT = 7200  # longer than a run's own 3600 s timeout
SLOT_WAIT_LIMIT = 840  # script mode (run-all.sh): under its shortest caller's timeout, the affected-tests hook's 900 s
PART = re.compile(r"^  (.+): (ok|FAILED|HUNG) \((\d+)s\)$")
PART_FLOOR = 900  # a part's limit is at least this (PA_PART_LIMIT), else 3x its recorded median
STOP_GRACE = 10  # seconds from TERM to KILL for a hung part
HEARTBEAT = 60  # a status line this often while a run or a --wait goes on (PA_SUITE_HEARTBEAT)
WAIT_CALL = 540  # --wait's default: under a 600 s tool-call limit, so a worker's call ends and says so
STILL_RUNNING = 75  # --wait's exit when the run goes on past its timeout
KEEP_DONE = 7 * 86400  # progress files of runs that ended are pruned after this
MODULE = re.compile(r"^test_[A-Za-z0-9_]+$")


class Waited(Exception):
    pass


def cap():
    """CAP, or PA_SUITE_CAP when it is a positive whole number."""
    value = os.environ.get("PA_SUITE_CAP", "")
    return int(value) if value.isdigit() and int(value) > 0 else CAP


def home():
    if pa is not None and getattr(pa, "ROOT", None):
        return pa.ROOT
    return os.environ.get("HERDR_PA_HOME") or os.path.join(os.path.expanduser("~"), ".config", "herdr-pa")


def stamp():
    if pa is not None and getattr(pa, "now", None):
        return pa.now()
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slot_dir():
    return os.path.join(home(), "suite-slots")


def parse_parts(text):
    """run-all.sh's summary list: `  <name>: ok (<n>s)` or `  <name>: FAILED (<n>s)`, in order."""
    parts = []
    for m in (PART.match(l) for l in text.splitlines()):
        if m:
            part = {"name": m.group(1), "ok": m.group(2) == "ok", "seconds": int(m.group(3))}
            if m.group(2) == "HUNG":
                part["hung"] = True
            parts.append(part)
    return parts


def holder_of(path):
    try:
        with open(path) as f:
            return json.loads(f.read() or "{}")
    except (OSError, ValueError):
        return {}


def describe(holders):
    return "; ".join("task {} (worker {}, pid {}, since {}{})".format(
        h.get("task"), h.get("worker"), h.get("pid"), h.get("start"),
        ", {}{}".format(h["what"], " in " + h["dir"] if h.get("dir") else "") if h.get("what") else "")
        for h in holders) or "unknown"


def acquire(limit_slots, info, poll=POLL, limit=WAIT_LIMIT, say=print, who="pa-full-suite", gone=None, beat=None):
    """Takes a free slot (an open file holding an flock) out of limit_slots, waiting for one. Says once that it
    waits and for whom, and again when the holders change. Raises Waited after `limit` seconds, or as soon as
    gone() (when given) says the caller it waits for has gone. beat(holders text), when given, on every poll."""
    folder = slot_dir()
    os.makedirs(folder, exist_ok=True)
    began, shown = time.time(), None
    while True:
        holders = []
        for i in range(limit_slots):
            path = os.path.join(folder, "slot-{}".format(i))
            f = open(path, "a+")
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                f.close()
                holders.append(holder_of(path))
                continue
            f.seek(0)
            f.truncate()
            f.write(json.dumps(dict(info, pid=os.getpid(), start=stamp())))
            f.flush()
            return f
        text = describe(holders)
        if text != shown:
            say("{}: waiting for the suite slot (cap {}): running: {}".format(who, limit_slots, text))
            shown = text
        if time.time() - began >= limit:
            raise Waited("{}: no slot came free in {:.0f} s (running: {})".format(who, time.time() - began, text))
        if beat:
            beat(text)
        reason = gone() if gone else None
        if reason:
            raise Waited("{}: stopped waiting for the suite slot after {:.0f} s: {}".format(who, time.time() - began, reason))
        time.sleep(poll)


def release(f):
    try:
        f.seek(0)
        f.truncate()
        fcntl.flock(f, fcntl.LOCK_UN)
    finally:
        f.close()


def ancestors():
    """This process's parent, its parent, and so on (ps), nearest first."""
    found, pid = [], os.getppid()
    while pid > 1 and pid not in found and len(found) < 64:
        found.append(pid)
        try:
            out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, universal_newlines=True, timeout=10).stdout
            pid = int(out.strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            break
    return found


def held_by_caller(path, chain=None):
    """The holder line of the slot at path when a caller of this process holds it (the path is one of this home's
    slots, it is locked, and its recorded pid is an ancestor), else None: a stale or leaked marker is not trusted.
    chain: ancestors(), when the caller has it already."""
    if not path or not os.path.basename(path).startswith("slot-"):
        return None
    if os.path.dirname(os.path.realpath(path)) != os.path.realpath(slot_dir()) or not os.path.isfile(path):
        return None
    try:
        f = open(path, "a+")
    except OSError:
        return None
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            pass  # locked: by whom is in its holder line
        else:
            fcntl.flock(f, fcntl.LOCK_UN)
            return None
    finally:
        f.close()
    holder = holder_of(path)
    pid = holder.get("pid")
    return holder if isinstance(pid, int) and pid in (ancestors() if chain is None else chain) else None


def caller_slot(marker):
    """(path, holder line) of the slot a caller of this process holds, else (None, None). The slot PA_SUITE_SLOT
    names is looked at first; else every slot of this home, since a pa-full-suite from before t-072 holds one (its
    pid in the holder line) and names none (round 2, MF-1). Either way only a locked slot held by an ancestor counts."""
    chain = ancestors()
    holder = held_by_caller(marker, chain)
    if holder:
        return marker, holder
    try:
        names = sorted(n for n in os.listdir(slot_dir()) if n.startswith("slot-"))
    except OSError:
        names = []
    for name in names:
        path = os.path.join(slot_dir(), name)
        holder = held_by_caller(path, chain)
        if holder:
            return path, holder
    return None, None


def task_record(start):
    """The task record of the worktree around start (pa-hooks install marks it in .claude/pa-task.json), else {}."""
    folder = os.path.realpath(start)
    while True:
        try:
            with open(os.path.join(folder, ".claude", "pa-task.json")) as f:
                record = json.load(f)
            if isinstance(record, dict) and record.get("task"):
                return record
        except (OSError, ValueError):
            pass
        parent = os.path.dirname(folder)
        if parent == folder:
            return {}
        folder = parent


def task_of(start):
    """The task id of the worktree around start, else None."""
    return task_record(start).get("task")


# ---- progress files (t-076): HERDR_PA_HOME/suite-runs/<run>.json, one per run, written whole ----

def runs_dir():
    return os.path.join(home(), "suite-runs")


def alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_progress(path):
    try:
        with open(path) as f:
            row = json.load(f)
        return row if isinstance(row, dict) else {}
    except (OSError, ValueError):
        return {}


def write_progress(path, row):
    tmp = "{}.tmp-{}-{}".format(path, os.getpid(), threading.get_ident())
    with open(tmp, "w") as f:
        json.dump(row, f)
    os.replace(tmp, path)


_progress_lock = threading.Lock()


def update_progress(path, **fields):
    """Adds fields to the progress file at path (read, merged, written whole). Never raises: progress is a view."""
    if not path:
        return
    with _progress_lock:
        try:
            row = read_progress(path)
            row.update(fields, updated=stamp())
            write_progress(path, row)
        except OSError:
            pass


def prune():
    """Removes progress files of runs that ended (or whose owner is gone) more than KEEP_DONE ago."""
    for path in glob.glob(os.path.join(runs_dir(), "*.json")):
        try:
            old = time.time() - os.path.getmtime(path) > KEEP_DONE
        except OSError:
            continue
        if old:
            row = read_progress(path)
            if row.get("state") == "done" or not alive(row.get("pid")):
                try:
                    os.remove(path)
                except OSError:
                    pass


def new_progress(row):
    """A progress file for a new run (its id in row["run"]); its path, or None when it cannot be written."""
    try:
        os.makedirs(runs_dir(), exist_ok=True)
        prune()
        path = os.path.join(runs_dir(), row["run"] + ".json")
        write_progress(path, dict(row, t=time.time(), updated=stamp()))
        return path
    except OSError:
        return None


def all_progress():
    rows = []
    for path in glob.glob(os.path.join(runs_dir(), "*.json")):
        row = read_progress(path)
        if row.get("run"):
            rows.append(row)
    return sorted(rows, key=lambda r: (r.get("t") or epoch(r.get("queued")) or 0, r.get("run")))


def epoch(value):
    """Seconds since the epoch of a stamp() string (or a number), else None."""
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return None


def head_commit(folder):
    try:
        r = subprocess.run(["git", "-C", folder, "rev-parse", "--verify", "-q", "HEAD"], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, universal_newlines=True, timeout=10)
        return r.stdout.strip() or None if not r.returncode else None
    except (OSError, subprocess.SubprocessError):
        return None


# ---- the per-part limit: from the suite record's medians ----

def floor_limit():
    value = os.environ.get("PA_PART_LIMIT", "")
    return int(value) if value.isdigit() and int(value) > 0 else PART_FLOOR


def part_limit(median, floor):
    """A part's limit: 3x its median, at least floor (no median: the floor)."""
    return max(floor, 3 * median) if median else floor


def record_rows(start):
    """The suite record (suites.jsonl) of the space of the task around start, else every space's."""
    space = task_record(start).get("space") or ""
    space = space.split("/")[-1]
    paths = []
    if re.match(r"^[A-Za-z0-9_.-]+$", space) and space not in (".", ".."):
        paths = [os.path.join(home(), "spaces", space, "content", "suites.jsonl")]
    rows = [r for p in paths for r in read_lines(p)]
    if not rows:
        rows = [r for p in sorted(glob.glob(os.path.join(home(), "spaces", "*", "content", "suites.jsonl")))
                for r in read_lines(p)]
    return rows


def median_total(rows, command=None, full=False):
    """The median seconds of the recorded runs (each run once): those of command, or (full) every run with parts
    that ran no single module. (median, runs), or (None, 0)."""
    seen, times = set(), []
    for i, r in enumerate(rows):
        key = r.get("run") or i
        if key in seen or not isinstance(r.get("seconds"), (int, float)):
            continue
        if (command is not None and r.get("command") == command) or (full and r.get("parts") and not r.get("module")):
            seen.add(key)
            times.append(r["seconds"])
    return (statistics.median(times), len(times)) if times else (None, 0)


class Watcher(threading.Thread):
    """Follows a tests/run-all.sh run folder (named in the watch file once run-all.sh made it): writes its parts
    into the progress file and stops a part past its limit (TERM to its process group, KILL after STOP_GRACE)."""

    def __init__(self, progress, watch, medians, floor, poll=1.0):
        threading.Thread.__init__(self, daemon=True)
        self.progress, self.watch, self.medians, self.floor, self.poll = progress, watch, medians, floor, poll
        self.folder, self.stopped, self.done = None, {}, threading.Event()

    def run(self):
        while not self.done.wait(self.poll):
            try:
                self.tick()
            except Exception:  # a view: never stop the run for it
                pass

    def stop(self):
        self.done.set()
        self.join(10)
        try:
            self.tick()
        except Exception:
            pass

    def read(self, name):
        try:
            with open(os.path.join(self.folder, name)) as f:
                return f.read().strip()
        except OSError:
            return None

    def tick(self):
        if not self.folder:
            try:
                with open(self.watch) as f:
                    self.folder = f.read().strip() or None
            except OSError:
                return
            if not self.folder:
                return
        parts = []
        for line in (self.read("parts") or "").splitlines():
            bits = line.split("\t")
            if len(bits) == 3:
                parts.append(bits)
        now, running, done, failed, hung = time.time(), [], 0, [], []
        for i, name, log in parts:
            start = self.read(i + ".start")
            if self.read(i + ".end") is not None:
                done += 1
                code = self.read(i + ".code")
                if os.path.exists(os.path.join(self.folder, i + ".hung")):
                    hung.append(name)
                elif code not in (None, "", "0"):
                    failed.append(name)
                continue
            if not start or not start.isdigit():
                continue
            began, median = int(start), self.medians.get(name)
            limit = part_limit(median, self.floor)
            try:
                last = os.path.getmtime(log)
            except OSError:
                last = began
            running.append({"name": name, "since": began, "last_output": last, "limit": limit, "median": median})
            if now - began > limit:
                self.halt(i, now - began, limit, median)
        update_progress(self.progress, folder=self.folder,
                        parts={"total": len(parts), "done": done, "running": running, "failed": failed, "hung": hung})

    def halt(self, i, seconds, limit, median):
        pid = self.read(i + ".pid")
        if not pid or not pid.isdigit():
            return
        pid = int(pid)
        first = i not in self.stopped
        if not first and (self.stopped[i] is None or time.time() - self.stopped[i] < STOP_GRACE):
            return  # TERM sent and waiting out the grace, or KILL sent already
        if first:
            why = "3x its median {:g}s, at least {}s".format(median, self.floor) if median else "no recorded median: {}s".format(self.floor)
            with open(os.path.join(self.folder, i + ".hung"), "w") as f:
                f.write("stopped after {:.0f}s (limit {:g}s: {})\n".format(seconds, limit, why))
        self.stopped[i] = time.time() if first else None
        sig = signal.SIGTERM if first else signal.SIGKILL
        try:
            if os.getpgid(pid) == pid:  # its own group (run-all.sh's set -m): the part and its children
                os.killpg(pid, sig)
            else:
                os.kill(pid, sig)
        except OSError:
            pass


def run_under_slot(argv):
    """The script entry point (see the top): a slot, the command under it, its exit code."""
    parser = argparse.ArgumentParser(prog="pa_suite.py slot")
    parser.add_argument("--for", dest="what", default="a test run")
    parser.add_argument("--worker", default="")
    parser.add_argument("--dir", default=os.getcwd())
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("no command after --")
    value = os.environ.get("PA_SUITE_WAIT_LIMIT", "")
    limit = int(value) if value.isdigit() else SLOT_WAIT_LIMIT
    who = args.what.split()[0] if args.what.split() else "pa_suite.py"
    path, caller = caller_slot(os.environ.get("PA_SUITE_SLOT", ""))
    slot, parent = None, os.getppid()
    # The progress file (t-076): the caller's (a pa-full-suite holding the slot) gets this run's parts; else its own.
    progress = os.environ.get("PA_SUITE_PROGRESS", "") if caller else ""
    if not (progress and os.path.dirname(os.path.realpath(progress)) == os.path.realpath(runs_dir())
            and os.path.isfile(progress)):
        progress = None
    own = progress is None
    run = uuid.uuid4().hex[:12]
    if own:
        rows = record_rows(args.dir)
        total, runs = median_total(rows, full=True) if args.what == "tests/run-all.sh" else (None, 0)
        progress = new_progress({"run": run, "kind": "run-all.sh", "what": args.what, "task": task_of(args.dir),
                                 "worker": args.worker or None, "dir": os.path.realpath(args.dir),
                                 "commit": head_commit(args.dir), "pid": os.getpid(), "state": "waiting",
                                 "queued": stamp(), "median_total": total, "median_runs": runs})
    if caller:
        hold = "held by the caller (task {}, pid {}, {})".format(caller.get("task"), caller.get("pid"), os.path.basename(path))
    else:
        queued = time.time()
        info = {"task": task_of(args.dir), "worker": args.worker or None, "what": args.what, "dir": args.dir}
        shown = {}

        def beat(text):
            if shown.get("text") != text:
                shown["text"] = text
                update_progress(progress, waiting_for=text)
        try:
            slot = acquire(cap(), info, limit=limit, say=lambda m: print(m, flush=True), who=who, beat=beat,
                           gone=lambda: os.getppid() != parent and "its caller is gone (pid {})".format(parent))
        except Waited as e:
            print(e, flush=True)
            update_progress(progress, state="done", exit=124, end=stamp(), note=str(e))
            return 124
        except KeyboardInterrupt:
            update_progress(progress, state="done", exit=130, end=stamp())
            return 130
        path = slot.name
        hold = "waited {:.1f}s for {} (cap {})".format(time.time() - queued, os.path.basename(path), cap())
    if own:
        update_progress(progress, state="running", start=stamp(), hold=hold)
    watch = os.path.join(runs_dir(), "{}.watch-{}".format(run, os.getpid()))
    env = dict(os.environ, PA_SUITE_SLOT=path, PA_SUITE_SLOT_PID=str(os.getpid()), PA_SUITE_HOLD=hold,
               PA_SUITE_WATCH=watch)
    env.pop("PA_SUITE_PROGRESS", None)
    medians = {p["name"]: p["median"] for p in report(record_rows(args.dir))}
    watcher = Watcher(progress, watch, medians, floor_limit()) if progress else None
    if watcher:
        watcher.start()
    child, code = None, 1

    def forward(sig, frame):  # TERM, INT or HUP to this process goes to the run, which stops its parts
        if child is not None and child.poll() is None:
            child.send_signal(sig)
    try:
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, forward)
        child = subprocess.Popen(command, env=env)
        code = child.wait()
    finally:
        if watcher:
            watcher.stop()
        if slot is not None:
            release(slot)
        try:
            os.remove(watch)
        except OSError:
            pass
    code = 128 - code if code < 0 else code
    if own:
        update_progress(progress, state="done", exit=code, end=stamp())
    return code


def untracked(h, repo):
    code, out, _ = h.git(repo, "--no-optional-locks", "status", "--porcelain", "--untracked-files=all")
    if code:
        return None
    return len([l for l in out.splitlines() if l.startswith("??") and not l[3:].strip('"').startswith(".claude/")])


def pins(h, root):
    """The repos the run is for, each with commit, tree, uncommitted and untracked, taken before the run."""
    found = []
    for pin in h.gate_commits(root) or [{"repo": os.path.realpath(root), "commit": None, "uncommitted": None}]:
        tree = None
        if pin.get("commit"):
            c, out, _ = h.git(pin["repo"], "rev-parse", "--verify", "-q", pin["commit"] + "^{tree}")
            tree = out if not c and out else None
        found.append(dict(pin, tree=tree, untracked=untracked(h, pin["repo"]) if pin.get("commit") else None))
    return found


def read_lines(path):
    rows = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        pass
    return rows


def clean(row):
    return (bool(row.get("commit") and row.get("tree")) and row.get("uncommitted") == 0 and row.get("untracked") == 0
            and not row.get("moved"))


def moved(before, after):
    """The repos whose commit, tree or uncommitted count differ between two pins() lists, as text; '' when none."""
    key = lambda p: (p.get("commit"), p.get("tree"), p.get("uncommitted"))
    was = {p["repo"]: p for p in before}
    now = {p["repo"]: p for p in after}
    changed = []
    for repo in sorted(set(was) | set(now)):
        a, b = was.get(repo, {}), now.get(repo, {})
        if key(a) != key(b):
            changed.append("{}: commit {} tree {} uncommitted {} -> commit {} tree {} uncommitted {}".format(
                repo, a.get("commit"), a.get("tree"), a.get("uncommitted"), b.get("commit"), b.get("tree"), b.get("uncommitted")))
    return "; ".join(changed)


def reusable(rows, found, command):
    """The passing record that covers every repo of this clean tree with this command, else None."""
    if not found or not all(clean(p) for p in found):
        return None
    match = None
    for p in found:
        hits = [r for r in rows if r.get("repo") == p["repo"] and r.get("tree") == p["tree"] and r.get("command") == command
                and r.get("exit") == 0 and clean(r)]
        if not hits:
            return None
        match = match or hits[-1]
    return match


def last(rows, commit=None, tree=None):
    for r in reversed(rows):
        if (commit is None or str(r.get("commit") or "").startswith(commit)) and (tree is None or r.get("tree") == tree):
            return r
    return None


def report(rows):
    """Each part seen over the recorded runs (a run's several repo lines count once): last and median seconds."""
    seen, times = set(), {}
    for i, r in enumerate(rows):
        if not r.get("parts"):
            continue
        key = r.get("run") or i
        if key in seen:
            continue
        seen.add(key)
        for p in r["parts"]:
            times.setdefault(p["name"], []).append(p["seconds"])
    out = [{"name": n, "last": t[-1], "median": statistics.median(t), "runs": len(t)} for n, t in times.items()]
    return sorted(out, key=lambda p: (-p["median"], p["name"]))


def load_now():
    try:
        one, five, fifteen = os.getloadavg()
    except OSError:
        one = five = fifteen = None
    return {"1m": one, "5m": five, "15m": fifteen, "cpus": os.cpu_count()}


def suites_path(h, directory):
    return os.path.join(h.content_dir(directory), "suites.jsonl")


def space_of(h, args):
    if getattr(args, "space", None):
        return pa.space_dir(args.space), None
    record = h.find_task(os.getcwd())
    if not record:
        raise pa.Problem("run pa-full-suite inside a task's worktree (pa-hooks install marks it), or pass --space")
    return pa.space_dir(record["space"]), record


def cmd_last(h, args):
    directory, record = space_of(h, args)
    rows = read_lines(suites_path(h, directory))
    commit, tree = args.commit, args.tree
    if not commit and not tree:
        if not record:
            raise pa.Problem("pa-full-suite --last outside a worktree needs --commit or --tree")
        found = pins(h, record["root"])
        tree = found[0].get("tree") if found else None
        if not tree:
            print("pa-full-suite: this worktree has no commit, so no tree to look up")
            return 1
    row = last(rows, commit, tree)
    if not row:
        print("pa-full-suite: no run recorded for {} in {}".format(("commit " + commit) if commit else ("tree " + tree),
                                                                  suites_path(h, directory)))
        return 1
    print(json.dumps(row))
    return 0


def cmd_report(h, args):
    directory, _ = space_of(h, args)
    rows = report(read_lines(suites_path(h, directory)))
    if not rows:
        print("pa-full-suite: no part times recorded in " + suites_path(h, directory))
        return 0
    print("median  last  runs  part (from {})".format(suites_path(h, directory)))
    for p in rows:
        print("{:>5}s {:>4}s {:>5}  {}".format("{:g}".format(p["median"]), p["last"], p["runs"], p["name"]))
    return 0


def heartbeat():
    try:
        value = float(os.environ.get("PA_SUITE_HEARTBEAT", ""))
    except ValueError:
        value = 0
    return value if value > 0 else HEARTBEAT


def gone(row):
    return row.get("state") in ("waiting", "running") and not alive(row.get("pid"))


def status_line(row, rows=None, now=None):
    """One line on a run, from its progress file: waiting, running or done (or gone without a result)."""
    now = time.time() if now is None else now
    who = "[run {}: {}, task {}, {}]".format(row.get("run"), row.get("kind") or "run", row.get("task"), row.get("dir"))
    state = row.get("state")
    if gone(row):
        return "pa-full-suite: ended without a result (pid {} gone; it was {} since {}) {}".format(
            row.get("pid"), state, row.get("start") or row.get("queued"), who)
    if state == "waiting":
        queued = epoch(row.get("queued"))
        line = [r for r in (rows if rows is not None else all_progress()) if r.get("state") == "waiting" and not gone(r)]
        place = [r.get("run") for r in line].index(row.get("run")) + 1 if row.get("run") in [r.get("run") for r in line] else len(line) + 1
        return "pa-full-suite: waiting for the suite slot since {} ({}s), place {} of {} waiting; running: {} {}".format(
            row.get("queued"), int(now - queued) if queued else "?", place, max(len(line), place),
            row.get("waiting_for") or "unknown", who)
    if state == "running":
        start = epoch(row.get("start"))
        text = "pa-full-suite: running since {} ({}s".format(row.get("start"), int(now - start) if start else "?")
        if row.get("median_total") is not None:
            text += "; median total {:g}s over {} runs".format(row["median_total"], row.get("median_runs"))
        text += ")"
        parts = row.get("parts") or {}
        if parts.get("total"):
            text += ": parts {} of {} done".format(parts.get("done", 0), parts["total"])
            now_parts = ["{} ({}s, limit {:g}s; last output {}s ago)".format(
                p.get("name"), int(now - (epoch(p.get("since")) or now)), p.get("limit") or 0,
                int(now - (epoch(p.get("last_output")) or now))) for p in parts.get("running") or []]
            if now_parts:
                text += "; now: " + ", ".join(now_parts)
            for key in ("failed", "hung"):
                if parts.get(key):
                    text += "; {}: {}".format(key, ", ".join(parts[key]))
        return text + " " + who
    start, end = epoch(row.get("start")), epoch(row.get("end"))
    text = "pa-full-suite: done: exit {} at {} (started {}, {}s)".format(
        row.get("exit"), row.get("end"), row.get("start") or row.get("queued"),
        int(end - start) if start and end else "?")
    parts = row.get("parts") or {}
    for key in ("failed", "hung"):
        if parts.get(key):
            text += "; {}: {}".format(key, ", ".join(parts[key]))
    if row.get("note"):
        text += "; " + row["note"]
    if row.get("output") or row.get("folder"):
        text += "; output {}".format(row.get("output") or row.get("folder"))
    if row.get("record"):
        text += "; record {}".format(row["record"])
    return text + " " + who


def under(path, root):
    path, root = os.path.realpath(path or "/nonexistent"), os.path.realpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def pick_runs(h, args):
    """The runs --status shows and --wait follows: in a worktree its latest (one not done first), with --commit the
    latest for that commit; outside a worktree without --commit every run not done. (rows, where it looked)."""
    rows = all_progress()
    record = h.find_task(os.getcwd())
    found = rows
    if args.commit:
        found = [r for r in found if str(r.get("commit") or "").startswith(args.commit)]
    if record and not args.commit:
        found = [r for r in found if under(r.get("dir"), record["root"])]
    if record or args.commit:
        active = [r for r in found if r.get("state") != "done" and not gone(r)]
        found = (active or found)[-1:]
    else:
        found = [r for r in found if r.get("state") != "done" and not gone(r)]
    where = ("commit " + args.commit) if args.commit else ("this worktree" if record else "any worktree (none not done)")
    return found, rows, where


def cmd_status(h, args):
    found, rows, where = pick_runs(h, args)
    if not found:
        print("pa-full-suite: no run for {} in {}".format(where, runs_dir()))
        return 1
    for row in found:
        print(status_line(row, rows))
    return 0


def cmd_wait(h, args):
    found, rows, where = pick_runs(h, args)
    if not found:
        print("pa-full-suite: no run for {} in {}".format(where, runs_dir()))
        return 1
    run = found[-1]["run"]
    path = os.path.join(runs_dir(), run + ".json")
    limit = args.timeout if getattr(args, "timeout", None) is not None else WAIT_CALL
    began = beat = time.time()
    while True:
        row = read_progress(path) or found[-1]
        if row.get("state") == "done":
            print(status_line(row), flush=True)
            code = row.get("exit")
            return code if isinstance(code, int) else 1
        if gone(row):
            print(status_line(row), flush=True)
            return 1
        if time.time() - began >= limit:
            print(status_line(row), flush=True)
            print("pa-full-suite: still running after {:.0f}s of waiting; run pa-full-suite --wait again".format(
                time.time() - began), flush=True)
            return STILL_RUNNING
        if time.time() - beat >= heartbeat():
            print(status_line(row), flush=True)
            beat = time.time()
        time.sleep(min(1.0, heartbeat(), max(0.05, limit - (time.time() - began))))


def run_to_file(command, cwd, output, timeout, env, beat):
    """Runs command (sh -c) with its whole output into the file output; beat() every second. (exit, output text)."""
    with open(output, "w") as out:
        child = subprocess.Popen(["sh", "-c", command.replace("{files}", "").replace("{file}", "")], cwd=cwd, env=env,
                                 stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
        began = time.time()
        try:
            while True:
                try:
                    code = child.wait(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    if time.time() - began > timeout:
                        child.kill()
                        child.wait()
                        out.write("\n(timed out after {} s)\n".format(timeout))
                        code = 124
                        break
                    beat()
        except BaseException:
            if child.poll() is None:
                child.kill()
                child.wait()
            raise
    try:
        with open(output) as f:
            return code, f.read()
    except OSError:
        return code, ""


def full_suite(h, args):
    if getattr(args, "last", False):
        return sys.exit(cmd_last(h, args))
    if getattr(args, "report", False):
        return sys.exit(cmd_report(h, args))
    if getattr(args, "status", False):
        return sys.exit(cmd_status(h, args))
    if getattr(args, "wait", False):
        return sys.exit(cmd_wait(h, args))
    record = h.find_task(os.getcwd())
    if not record:
        raise pa.Problem("run pa-full-suite inside a task's worktree (pa-hooks install marks it)")
    directory, agent, profile = h.load_context(record["space"])
    module = getattr(args, "module", None)
    if module:
        if not MODULE.match(module):
            raise pa.Problem("--module takes a unit test module's name, test_<name> (tests/test_<name>.py)")
        if not os.path.isfile(os.path.join(record["root"], "tests", "run-all.sh")):
            raise pa.Problem("--module runs tests/run-all.sh --module; this worktree has no tests/run-all.sh")
        command = "sh tests/run-all.sh --module " + module
    else:
        command = h.command_for("full", agent, profile)
    if not command:
        raise pa.Problem("no full-suite command: set tests.full in the profile, or commands.full in the space's agent.json")
    evidence = os.path.join(directory, "evidence", record["task"])
    os.makedirs(evidence, exist_ok=True)
    build_log = os.path.join(evidence, "build.log")
    where = suites_path(h, directory)
    found = pins(h, record["root"])  # for the check before queueing only: pinned again once the slot is taken

    def reuse():
        if getattr(args, "again", False):
            return False
        prior = reusable(read_lines(where), found, command)
        if not prior:
            return False
        line = ("pa-full-suite: tree {} already passed: run {} on commit {} at {} (task {}, worker {}); not run again "
                "(--again runs it anyway; the record: {})").format(prior.get("tree"), prior.get("run"), prior.get("commit"),
                                                                    prior.get("at"), prior.get("task"), prior.get("worker"), where)
        with open(build_log, "a") as f:
            f.write("=== {} full suite: `{}` in {} -> {}\n".format(pa.now(), command, record["root"], line))
        print(line)
        return True

    if reuse():
        return sys.exit(0)
    # t-076: the output file, named first, and the progress file that --status and --wait read.
    run = uuid.uuid4().hex[:12]
    output = os.path.join(evidence, "full-suite-{}.out".format(run))
    print("pa-full-suite: run {}: output in {} (pa-full-suite --status shows it; pa-full-suite --wait waits for it)".format(
        run, output), flush=True)
    rows = read_lines(where)
    total, runs = median_total(rows, command=command)
    progress = new_progress({"run": run, "kind": "pa-full-suite", "what": command, "task": record.get("task"),
                             "worker": os.environ.get("HC_TERMINAL_ID") or None, "dir": os.path.realpath(record["root"]),
                             "commit": found[0].get("commit") if found else None, "tree": found[0].get("tree") if found else None,
                             "pid": os.getpid(), "state": "waiting", "queued": pa.now(), "output": output, "record": where,
                             "median_total": total, "median_runs": runs, "module": module})
    beaten = {"at": time.time()}

    def pulse(text=None):  # the heartbeat: the status line every HEARTBEAT seconds, the holders when they change
        if text is not None and beaten.get("text") != text:
            beaten["text"] = text
            update_progress(progress, waiting_for=text)
        if time.time() - beaten["at"] >= heartbeat():
            beaten["at"] = time.time()
            row = read_progress(progress) if progress else {}
            if row:
                print(status_line(row), flush=True)
    queued = time.time()
    try:
        slot = acquire(cap(), {"task": record.get("task"), "worker": os.environ.get("HC_TERMINAL_ID") or None,
                               "tree": found[0].get("tree") if found else None},
                       say=lambda m: print(m, flush=True), beat=pulse)
    except Waited as e:
        print(e)
        update_progress(progress, state="done", exit=124, end=pa.now(), note=str(e))
        return sys.exit(124)
    marker = os.environ.get("PA_SUITE_SLOT")
    os.environ["PA_SUITE_SLOT"] = slot.name  # tests/run-all.sh under this run sees its caller holds the slot (t-072)
    try:
        waited = round(time.time() - queued, 1)
        found = pins(h, record["root"])  # MF-1: what runs is the tree at this point, not the one before the wait
        if reuse():  # a run of the same tree may have passed while this one waited
            update_progress(progress, state="done", exit=0, end=pa.now(), note="not run: this tree passed while it waited")
            return sys.exit(0)
        start, load, began = pa.now(), load_now(), time.time()
        update_progress(progress, state="running", start=start, commit=found[0].get("commit") if found else None,
                        tree=found[0].get("tree") if found else None)
        env = dict(os.environ, PA_CHANGED_FILES="", PA_SUITE_PROGRESS=progress or "")
        code, out = run_to_file(command, record["root"], output, 3600, env, pulse)
        seconds, end = round(time.time() - began, 1), pa.now()
        change = moved(found, pins(h, record["root"]))
    finally:
        if marker is None:
            os.environ.pop("PA_SUITE_SLOT", None)
        else:
            os.environ["PA_SUITE_SLOT"] = marker
        release(slot)
    update_progress(progress, state="done", exit=code, end=end)
    with open(build_log, "a") as f:
        f.write("=== {} full suite: `{}` in {} -> exit {} after {:.0f}s ===\n{}\n".format(end, command, record["root"], code, seconds, h.tail(out, 200)))
    extra = {"run": run, "start": start, "end": end, "waited": waited, "load": load, "parts": parse_parts(out),
             "output": output}
    if module:
        extra["module"] = module
    if change:  # recorded under the tree the run started on, marked so it is never reused
        extra["moved"] = True
    try:
        h.suite_record(directory, record, command, code, seconds, found, extra)
        recorded = "recorded in " + where
    except OSError as e:
        recorded = "could not record the run in {} ({})".format(where, e)
    print(h.tail(out, 40))
    if change:
        print("pa-full-suite: the worktree changed during the run ({}); the run is recorded under the tree it started "
              "on with moved: true and is never reused; run pa-full-suite again for the new tree".format(change))
    print("full suite exit {} (appended to {}; {})".format(code, build_log, recorded))
    return sys.exit(code)


if __name__ == "__main__":
    if sys.argv[1:2] == ["slot"]:
        sys.exit(run_under_slot(sys.argv[2:]))
    print("usage: python3 pa_suite.py slot --for <what> [--worker W] [--dir D] -- <command...>", file=sys.stderr)
    sys.exit(2)
