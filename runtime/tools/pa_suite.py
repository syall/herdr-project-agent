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
  pa-full-suite --local                            on this Mac, never on a configured machine
  pa-full-suite --on M                             on machine M only: exit 2 when M is not configured, not reachable,
                                                   or its setup fails (no fallback)
  pa-full-suite --places                           each configured machine (reachable or why not, cap, holders) and
                                                   this Mac's slot

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

The remote mode (t-089): HERDR_PA_HOME/config.json (this Mac's, never synced) may list machines,
  "suiteMachines": [{"machine": "<hc machine name>", "cap": <1..32, default 1>}, ...]
in order of preference (a bad entry is named and skipped; a cap over 32 is used as 32). Then a run of the command
`sh tests/run-all.sh` from a clean one-repo worktree (not --module; or a profile full command its tests.fullIsRunAll
declares to be that in a plain git repo, see as_run_all) goes to the first reachable machine (one
`hc exec` probe: uname and the paths and versions of git, python3 >= 3.7, node and sh) with a free slot: a local flock
under suite-slots/<machine>/ (its cap), held by this pa-full-suite for the whole run. All busy: it waits, and after
PA_SUITE_PREFER_WAIT (300) s this Mac's slot counts too; none reachable: this Mac at once. There: mkdir -m 700
/tmp/pa-suite-<run>, a `git bundle` of HEAD (exactly the pinned commit; at most 6 MB base64) by `hc file write`, one
`hc exec` that clones it, checks out the commit and prints its commit and tree (both must equal the pins), then an hc
terminal (`hc run --bg --rm`) running `tests/run-all.sh --except-integration` with its own HERDR_PA_HOME, TMPDIR,
XDG folders, HOME and an `hc` that refuses (no test there reaches an hc core). Here, at the same time, under the
machine's slot (not this Mac's): `tests/run-all.sh --only-integration`. Every PA_SUITE_REMOTE_POLL (5) s the output
and the remote progress are copied back (--status says `on <machine>`); PA_SUITE_LOST_GRACE (300) s without a good
read ends the run with 124. The exit is the remote one if it failed, else the local one; the output, build.log and
the record (machine, os, tools, places, note; each part with its place) say where. Then the terminal is ended and
the folder removed (`rm -rf -- /tmp/pa-suite-<run>`, the run id checked); TERM, INT or HUP does the same and exits
143, 130 or 129, with no record. A setup failure runs on this Mac in the same call (exit 2 with --on). Part medians
and limits use only times of the same place. Every hc call is an argv list built from fixed text, the checked
machine name, run id and pins, and the probe's tool paths.
"""
import argparse
import base64
import calendar
import datetime
import fcntl
import glob
import json
import os
import platform
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
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
# t-089: the remote mode (see the top)
MACHINE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
RUN_ID = re.compile(r"^[0-9a-f]{12}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
TOOL_PATH = re.compile(r"^/[A-Za-z0-9._/+-]+$")
MACHINE_CAP = 32  # a machine's cap at most (a 32-core machine); more is used as this
PREFER_WAIT = 300  # PA_SUITE_PREFER_WAIT: waiting this long for a busy machine, this Mac's slot is taken too
REMOTE_POLL = 5  # PA_SUITE_REMOTE_POLL: how often a remote run's output and progress are copied back
LOST_GRACE = 300  # PA_SUITE_LOST_GRACE: no good read from the machine for this long ends the run (124)
REMOTE_TIMEOUT = 3600  # as a local run's own timeout
BUNDLE_MAX = 6 * 1024 * 1024  # base64 bytes; hc reads at most 8 MB
REMOTE_COMMAND = "sh tests/run-all.sh"  # the only command that goes to a machine (R6; as_run_all, J2-D15)
FILE_NAME = re.compile(r"^[^/\x00]+$")
TOOLS = ("git", "python3", "node", "sh")
FAST_POLL, FAST_SPAN = 1.0, 60  # t-107 R17: a pa-run's watcher polls every second for its first minute
FALLBACKS = ("mac", "none")  # t-107 R11: config.json's suiteFallback
SIGNAL_EXIT = {signal.SIGTERM: 143, signal.SIGINT: 130, signal.SIGHUP: 129}


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
    began, shown = time.time(), None
    while True:
        f, holders = try_slot(folder, limit_slots, info)
        if f:
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


def try_slot(folder, limit_slots, info):
    """One pass over the slots of folder: (an open file holding a free slot's flock, []) or (None, its holders)."""
    os.makedirs(folder, exist_ok=True)
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
        return f, []
    return None, holders


def peek(folder, limit_slots):
    """The holders of folder's slots now, taking none."""
    holders = []
    for i in range(limit_slots):
        path = os.path.join(folder, "slot-{}".format(i))
        if not os.path.exists(path):
            continue
        with open(path, "a+") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                holders.append(holder_of(path))
                continue
            fcntl.flock(f, fcntl.LOCK_UN)
    return holders


def machine_dir(machine):
    """A machine's slots (t-089): local flocks under suite-slots/<machine>/, held by the local pa-full-suite."""
    return os.path.join(slot_dir(), machine)


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
    folder, base = os.path.dirname(os.path.realpath(path)), os.path.realpath(slot_dir())
    if folder != base and not (os.path.dirname(folder) == base and MACHINE.match(os.path.basename(folder))):
        return None  # a machine's slots (t-089) are this home's too: suite-slots/<machine>/slot-<i>
    if not os.path.isfile(path):
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


def median_total(rows, command=None, full=False, place=None):
    """The median seconds of the recorded runs (each run once): those of command, or (full) every run with parts
    that ran no single module. place (t-089): only runs of that place (a record without `machine` is `local`).
    (median, runs), or (None, 0)."""
    seen, times = set(), []
    for i, r in enumerate(rows):
        key = r.get("run") or i
        if key in seen or not isinstance(r.get("seconds"), (int, float)):
            continue
        if place is not None and (r.get("machine") or "local") != place:
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
        total, runs = median_total(rows, full=True, place="local") if args.what == "tests/run-all.sh" else (None, 0)
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
    medians = {p["name"]: p["median"] for p in report(record_rows(args.dir), place="local")}
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


def report(rows, place=None):
    """Each part seen over the recorded runs (a run's several repo lines count once): last and median seconds.
    place (t-089): only parts that ran there (a part's `place`, else its record's `machine`, else `local`)."""
    seen, times = set(), {}
    for i, r in enumerate(rows):
        if not r.get("parts"):
            continue
        key = r.get("run") or i
        if key in seen:
            continue
        seen.add(key)
        for p in r["parts"]:
            if place is not None and (p.get("place") or r.get("machine") or "local") != place:
                continue
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
    place = row.get("place")  # t-089: only rows of a run that had machines to choose from carry it
    who = "[run {}: {}, task {}, {}{}]".format(row.get("run"), row.get("kind") or "run", row.get("task"), row.get("dir"),
                                            "" if not place else ", local" if place == "local" else ", on " + place)
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


# ---- t-089: the remote mode (see the top). Every hc call is an argv list; no local shell. ----

class SetupFailed(Exception):
    pass


class Interrupted(Exception):
    def __init__(self, code):
        Exception.__init__(self, code)
        self.code = code


def env_seconds(name, default):
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def machines(cfg):
    """The configured machines of config.json's suiteMachines, in order: ([{machine, cap}], notes on bad entries)."""
    value = (cfg or {}).get("suiteMachines")
    if value is None or value == []:
        return [], []
    if not isinstance(value, list):
        return [], ["suiteMachines ignored: not a list of {\"machine\": <hc machine name>, \"cap\": <1..32>}"]
    good, bad, seen = [], [], set()
    for i, entry in enumerate(value, 1):
        if not isinstance(entry, dict):
            bad.append("entry {} ignored: {!r} is not an object {{\"machine\": ..., \"cap\": ...}}".format(i, entry))
            continue
        name, cap_value = entry.get("machine"), entry.get("cap", 1)
        if not isinstance(name, str) or not MACHINE.match(name):
            bad.append("entry {} ignored: machine {!r} is not a machine name (A-Z a-z 0-9 . _ -, 1-64)".format(i, name))
            continue
        if isinstance(cap_value, bool) or not isinstance(cap_value, int) or cap_value < 1:
            bad.append("entry {} ({}) ignored: cap {!r} is not a whole number 1..{}".format(i, name, cap_value, MACHINE_CAP))
            continue
        if name in seen:
            bad.append("entry {} ({}) ignored: named twice".format(i, name))
            continue
        if cap_value > MACHINE_CAP:
            bad.append("entry {} ({}): cap {} used as {} (at most {})".format(i, name, cap_value, MACHINE_CAP, MACHINE_CAP))
            cap_value = MACHINE_CAP
        seen.add(name)
        good.append({"machine": name, "cap": cap_value})
    return good, bad


def fallback(cfg):
    """t-107 R11: (config.json's suiteFallback, a note on a bad value or None). "mac" (the default): a run no machine
    takes may run on this Mac; "none": never on its own (only --local)."""
    value = (cfg or {}).get("suiteFallback", "mac")
    if value in FALLBACKS:
        return value, None
    return "mac", "suiteFallback {!r} ignored: one of \"mac\", \"none\"; read as \"mac\"".format(value)


def hc(argv, timeout=120, stdin=None):
    """(exit, stdout, stderr) of one hc call."""
    feed = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
    try:
        r = subprocess.run(["hc"] + argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                           timeout=timeout, **feed)
    except subprocess.TimeoutExpired:
        return 124, "", "hc {} timed out after {} s".format(argv[0] if argv else "", timeout)
    except OSError as e:
        return 127, "", "hc not found ({})".format(e)
    return r.returncode, r.stdout, r.stderr


def first_line(text, default=""):
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    return lines[0][:200] if lines else default


PROBE = r"""uname -s; uname -m
for t in git python3 node sh; do
  p=$(command -v "$t") || { echo "missing $t"; continue; }
  case $t in
    git) v=$("$p" --version 2>&1) ;;
    python3) v=$("$p" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3]); sys.exit(0 if sys.version_info >= (3, 7) else 3)' 2>&1) || { echo "missing python3 (3.7 or later; $p is $v)"; continue; } ;;
    node) v=$("$p" --version 2>&1) ;;
    sh) v=- ;;
  esac
  echo "tool $t $p $v"
done
"""


def probe(machine):
    """R2: is machine reachable, with the four tools? {ok, reason, os, arch, tools, seconds}. Writes nothing there."""
    began = time.time()
    code, out, err = hc(["exec", "--on", machine, "--timeout", "30", "--", "sh", "-c", PROBE], timeout=60)
    found = {"ok": False, "seconds": round(time.time() - began, 1), "os": None, "arch": None, "tools": {}}
    if code:
        found["reason"] = first_line(err, "hc exec exited {}".format(code))
        return found
    lines = out.splitlines()
    missing = []
    for line in lines[2:]:
        bits = line.split(" ", 3)
        if bits[0] == "tool" and len(bits) == 4 and bits[1] in TOOLS and TOOL_PATH.match(bits[2]):
            found["tools"][bits[1]] = {"path": bits[2], "version": bits[3].strip()}
        elif line.startswith("missing "):
            missing.append(line[len("missing "):])
    missing += [t for t in TOOLS if t not in found["tools"] and not any(m.startswith(t) for m in missing)]
    found["os"], found["arch"] = (lines[0].strip() if lines else None), (lines[1].strip() if len(lines) > 1 else None)
    if missing or not found["os"]:
        found["reason"] = "missing " + ", ".join(missing) if missing else "no answer from uname"
        return found
    found["ok"] = True
    return found


def as_run_all(h, command, profile, root):
    """d-222 A (J2-D15): (the command R6 checks, why not run-all.sh or None). A profile may declare in its tests that
    its own full command runs tests/run-all.sh from the top level of a plain git repo:
      "fullIsRunAll": {"command": "<its tests.full, as written>", "unlessFound": ["<file name>", ...]}
    It counts as `sh tests/run-all.sh` only while tests.full is that command and the space has no command of its own,
    the worktree root is its git top level with an executable tests/run-all.sh, and none of the named files is in the
    root or a folder above it (the profile's marks of a workspace whose full command does more)."""
    tests = (profile or {}).get("tests") or {}
    said = tests.get("fullIsRunAll")
    if said is None or command == REMOTE_COMMAND:
        return command, None
    names = said.get("unlessFound") if isinstance(said, dict) else None
    if (not isinstance(said, dict) or not isinstance(said.get("command"), str) or not isinstance(names, list)
            or not all(isinstance(n, str) and FILE_NAME.match(n) and n not in (".", "..") for n in names)):
        return command, ('profile {}: tests.fullIsRunAll must be {{"command": "<tests.full>", "unlessFound": '
                         '[<file names>]}}; ignored').format(profile.get("name"))
    if said["command"] != tests.get("full") or command != h.command_for("full", {}, profile):
        return command, None
    only = "the profile's full command runs tests/run-all.sh only in a plain git repo"
    top = os.path.realpath(root)
    code, out, _ = h.git(top, "rev-parse", "--show-toplevel")
    if code or not out or os.path.realpath(out) != top:
        return command, "{}; {} is not its git top level".format(only, top)
    run_all = os.path.join(top, "tests", "run-all.sh")
    if not os.path.isfile(run_all) or not os.access(run_all, os.X_OK):
        return command, "{}; {} is not executable".format(only, run_all)
    d = top
    while True:
        for n in names:
            if os.path.lexists(os.path.join(d, n)):
                return command, "{}; {} is in {}".format(only, n, d)
        up = os.path.dirname(d)
        if up == d:
            return REMOTE_COMMAND, None
        d = up


def remote_ok(command, found, module, why_not=None):
    """R6: None when this run may go to a machine, else why it stays on this Mac. command is as_run_all's."""
    if module:
        return "--module runs on this Mac"
    if command != REMOTE_COMMAND:
        return "the full command is `{}`, not `{}`{}".format(command, REMOTE_COMMAND,
                                                              " ({})".format(why_not) if why_not else "")
    if len(found) != 1 or not found[0].get("commit") or not found[0].get("tree"):
        return "the worktree is not one git repository with a commit"
    if found[0].get("untracked"):
        return "the worktree has {} untracked file(s); only committed state travels".format(found[0]["untracked"])
    if found[0].get("uncommitted"):
        return "the worktree has {} uncommitted file(s); only committed state travels".format(found[0]["uncommitted"])
    if not clean(found[0]):
        return "the worktree's state could not be read"
    return None


def folder_of(run):
    if not RUN_ID.match(run or ""):
        raise SetupFailed("bad run id {!r}".format(run))
    return "/tmp/pa-suite-" + run


def ship(h, machine, run, pin, tools, root, patch=None, head=None):
    """R7, R8: the run folder there, the bundle of the pinned commit, the clone checked out and its ids checked.
    t-107 (pa-run): head, the commit the bundle holds when the commit to check out is an ancestor of it; patch, the
    `git diff --binary` from the commit to pin["tree"], sent as p.b64 and applied with `git apply --index` there,
    then the clone's index tree (write-tree) must be that tree. Without a patch the script is t-089's."""
    folder = folder_of(run)
    commit, tree = pin.get("commit") or "", pin.get("tree") or ""
    head = head or commit
    if not SHA.match(commit) or not SHA.match(tree) or not SHA.match(head):
        raise SetupFailed("bad pins {!r} {!r}".format(commit, tree))
    extra = base64.b64encode(patch.encode("utf-8", "surrogateescape") if isinstance(patch, str) else patch).decode(
        "ascii") if patch else ""
    for t in TOOLS:
        if not TOOL_PATH.match((tools.get(t) or {}).get("path") or ""):
            raise SetupFailed("bad {} path from the probe".format(t))
    code, _, err = hc(["exec", "--on", machine, "--timeout", "60", "--", "mkdir", "-m", "700", folder])
    if code:
        raise SetupFailed("mkdir {}: {}".format(folder, first_line(err, "exit {}".format(code))))
    scratch = tempfile.mkdtemp(prefix="pa-suite-bundle-")
    try:
        bundle = os.path.join(scratch, "b.bundle")
        code, _, err = h.git(root, "bundle", "create", "-q", bundle, "HEAD")
        if code:
            raise SetupFailed("git bundle create: " + first_line(err, "exit {}".format(code)))
        code, heads, err = h.git(root, "bundle", "list-heads", bundle)
        if code or heads.split() != [head, "HEAD"]:
            raise SetupFailed("the bundle's heads are {!r}, not the pinned commit {} (HEAD moved?)".format(heads, head))
        with open(bundle, "rb") as f:
            data = base64.b64encode(f.read()).decode("ascii")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if len(data) + len(extra) > BUNDLE_MAX:
        raise SetupFailed("bundle too large ({} bytes base64, at most {})".format(len(data) + len(extra), BUNDLE_MAX))
    code, _, err = hc(["file", "write", "--on", machine, folder + "/b.b64"], stdin=data)
    if code:
        raise SetupFailed("hc file write: " + first_line(err, "exit {}".format(code)))
    if extra:
        code, _, err = hc(["file", "write", "--on", machine, folder + "/p.b64"], stdin=extra)
        if code:
            raise SetupFailed("hc file write: " + first_line(err, "exit {}".format(code)))
    py, git, sh_path = tools["python3"]["path"], tools["git"]["path"], tools["sh"]["path"]
    script = "\n".join([
        "set -e",
        "cd {}".format(folder),
        "{} -c 'import base64, sys; open(sys.argv[2], \"wb\").write(base64.b64decode(open(sys.argv[1]).read()))' b.b64 b.bundle".format(py),
        "rm -f b.b64",
        "{} clone -q --no-checkout b.bundle w".format(git),
        "{} -C w checkout -q --detach {}".format(git, commit),
        "mkdir -m 700 bin home tmp xdg npm u",
        "printf '%s\\n' '#!{}' 'echo \"hc is not available in a remote suite run\" >&2' 'exit 2' > bin/hc".format(sh_path),
        "chmod 755 bin/hc",
    ] + ["ln -s {} bin/{}".format(tools[t]["path"], t) for t in TOOLS] + ([
        "{} -c 'import base64, sys; open(sys.argv[2], \"wb\").write(base64.b64decode(open(sys.argv[1]).read()))' p.b64 p.patch".format(py),
        "rm -f p.b64",
        "{} -C w apply --index --binary {}/p.patch".format(git, folder),
        "rm -f p.patch",
        "{} -C w rev-parse HEAD".format(git),
        "{} -C w write-tree".format(git), ""] if extra else [
        "{} -C w rev-parse HEAD 'HEAD^{{tree}}'".format(git), ""]))
    code, out, err = hc(["exec", "--on", machine, "--timeout", "300", "--", "sh", "-c", script], timeout=360)
    if code:
        raise SetupFailed("setup: " + first_line(err or out, "exit {}".format(code)))
    got = out.split()[-2:]
    if got != [commit, tree]:
        raise SetupFailed("the checkout there is commit/tree {}, not {} {}".format(" ".join(got) or "nothing", commit, tree))
    return folder


def run_script(folder, tools):
    """R10, R11: the remote terminal's fixed script. {folder}/bin holds hc's refusing shim and a link to each probed
    tool (made at setup), and only it goes ahead of the machine's PATH: a probed tool's folder is never put first, so
    another tool in it (an older python3 beside git) cannot shadow the probed one, and the record names what ran."""
    return "\n".join([
        "date +%s > ../t0",
        "unset HC_TERMINAL_ID HC_SOCKET HC_TERMINAL_SOCKET HC_SCOPE CLAUDECODE",
        "export HERDR_PA_HOME={0}/home TMPDIR={0}/tmp XDG_CACHE_HOME={0}/xdg/cache XDG_CONFIG_HOME={0}/xdg/config "
        "XDG_DATA_HOME={0}/xdg/data XDG_STATE_HOME={0}/xdg/state PYTHONDONTWRITEBYTECODE=1 npm_config_cache={0}/npm "
        "PA_CHANGED_FILES= NODE={1} HOME={0}/u".format(folder, tools["node"]["path"]),
        "export PATH={}/bin:$PATH".format(folder),
        "sh tests/run-all.sh --except-integration > ../out 2>&1",
        "echo $? > ../code.new && mv ../code.new ../code",
        "date +%s > ../t1", ""])


def start_remote(machine, run, folder, tools, script=None):
    """The run's hc terminal there; script: t-107's pa-run script in place of run_script's."""
    code, out, err = hc(["run", "--bg", "--rm", "--no-notify", "--json", "--on", machine, "--name", "pa-suite-" + run,
                         "--label", "pa.suite=" + run, "--cwd", folder + "/w", "--", "sh", "-c",
                         script or run_script(folder, tools)])
    if code:
        raise SetupFailed("hc run: " + first_line(err, "exit {}".format(code)))
    try:
        answer = json.loads(out)
    except ValueError:
        answer = {}
    address = (answer.get("address") or answer.get("id")) if isinstance(answer, dict) else None
    return address if isinstance(address, str) and address else "{}/pa-suite-{}".format(machine, run)


STATUS = """cd {0} || exit 3
if [ -f code ]; then printf 'code %s\\n' "$(cat code)"; fi
if [ -f out ]; then printf 'lines %s\\n' "$(wc -l < out)"; fi
for f in home/suite-runs/*.json; do if [ -f "$f" ]; then printf 'progress '; cat "$f"; echo; fi; done
"""


def remote_status(machine, folder):
    """One exec: (ok, code or None, remote line count or None, the remote run-all's progress row or {})."""
    code, out, _ = hc(["exec", "--on", machine, "--timeout", "30", "--", "sh", "-c", STATUS.format(folder)], timeout=60)
    if code:
        return False, None, None, {}
    rc = lines = None
    progress = {}
    for line in out.splitlines():
        if line.startswith("code ") and line[5:].strip().lstrip("-").isdigit():
            rc = int(line[5:].strip())
        elif line.startswith("lines ") and line[6:].strip().isdigit():
            lines = int(line[6:].strip())
        elif line.startswith("progress "):
            try:
                row = json.loads(line[len("progress "):])
            except ValueError:
                continue
            if isinstance(row, dict) and isinstance(row.get("parts"), dict):
                progress = row
    return True, rc, lines, progress


def merge_parts(*rows):
    total = {"total": 0, "done": 0, "running": [], "failed": [], "hung": []}
    for parts in rows:
        parts = parts or {}
        for key in ("total", "done"):
            total[key] += parts.get(key) or 0
        for key in ("running", "failed", "hung"):
            total[key] += parts.get(key) or []
    return total


class RemoteWatcher(threading.Thread):
    """R15, R17: copies a remote run's output (whole lines) and progress back while it runs; ends when its code
    is there, after REMOTE_TIMEOUT, or after LOST_GRACE s with no good read."""

    def __init__(self, machine, folder, output, progress, side, sink=None, fast=False):
        threading.Thread.__init__(self, daemon=True)
        self.machine, self.folder, self.output, self.progress, self.side = machine, folder, output, progress, side
        self.poll, self.grace = env_seconds("PA_SUITE_REMOTE_POLL", REMOTE_POLL), env_seconds("PA_SUITE_LOST_GRACE", LOST_GRACE)
        self.sink, self.fast = sink, fast  # t-107: each copied text to sink too; fast: 1 s polls for the first 60 s
        self.lines, self.code, self.remote_lines, self.lost, self.timed_out = 0, None, None, None, False
        self.began = self.last_ok = time.time()
        self.stopping, self.ended = threading.Event(), threading.Event()
        open(output, "w").close()

    def run(self):
        try:
            while not self.stopping.is_set():
                try:
                    self.tick()
                except Exception:  # a view: a bad read counts as no read
                    pass
                now = time.time()
                if self.code is not None:
                    break
                if now - self.last_ok > self.grace:
                    self.lost = now - self.last_ok
                    break
                if now - self.began > REMOTE_TIMEOUT:
                    self.timed_out = True
                    break
                self.stopping.wait(min(self.poll, FAST_POLL) if self.fast and now - self.began < FAST_SPAN else self.poll)
        finally:
            self.ended.set()

    def tick(self):
        code, out, _ = hc(["file", "read", "--on", self.machine, self.folder + "/out", "--offset", str(self.lines + 1)])
        if not code and out:
            whole = out[:out.rfind("\n") + 1]
            if whole:
                with open(self.output, "a") as f:
                    f.write(whole)
                self.lines += whole.count("\n")
                if self.sink:
                    self.sink(whole)
        ok, rc, lines, row = remote_status(self.machine, self.folder)
        if not ok:
            return
        self.last_ok = time.time()
        self.remote_lines = lines
        local = read_progress(self.side).get("parts") if self.side else None
        update_progress(self.progress, parts=merge_parts(row.get("parts"), local))
        if rc is not None:
            self.code = rc

    def stop(self):
        self.stopping.set()
        self.join(30)

    def final(self):
        """The remote output whole, replacing the copy (checked against the remote line count): a note, or None."""
        code, out, err = hc(["file", "read", "--on", self.machine, self.folder + "/out"])
        if code:
            return "the last read of the remote output failed ({}); the output has the {} lines copied before".format(
                first_line(err, "exit {}".format(code)), self.lines)
        with open(self.output, "w") as f:
            f.write(out if not out or out.endswith("\n") else out + "\n")
        got = out.count("\n") + (1 if out and not out.endswith("\n") else 0)
        if self.remote_lines is not None and got < self.remote_lines:
            return "copied {} lines of the remote output, the machine counted {}".format(got, self.remote_lines)
        return None


def terminals_of(run):
    """The hc terminals labelled pa.suite=<run>: ([rows], error or None)."""
    code, out, err = hc(["ls", "--all", "--json", "--label", "pa.suite=" + run])
    if code:
        return [], first_line(err, "exit {}".format(code))
    try:
        rows = json.loads(out).get("terminals") or []
    except (ValueError, AttributeError):
        return [], "hc ls answered no JSON"
    return [r for r in rows if isinstance(r, dict) and (r.get("labels") or {}).get("pa.suite") == run], None


def cleanup(machine, run, terminal=None):
    """R16: the run's terminal ended and removed, its folder there removed (a literal path). Notes on failures."""
    if not RUN_ID.match(run or "") or not MACHINE.match(machine or ""):
        return ["not cleaned: bad machine or run id"]
    notes = []
    rows, error = terminals_of(run)
    if error:
        notes.append("hc ls: " + error)
        if terminal:
            rows = [{"address": terminal, "state": "running"}]
    for row in rows:
        address = row.get("address") or row.get("id") or terminal
        if not isinstance(address, str):
            continue
        if row.get("state") in ("finished", "exited", "done", "lost"):
            code, _, err = hc(["rm", address])
            if code:
                notes.append("hc rm {}: {}".format(address, first_line(err, "exit {}".format(code))))
        else:
            code, _, err = hc(["kill", address])
            if code:
                notes.append("hc kill {}: {}".format(address, first_line(err, "exit {}".format(code))))
    code, _, err = hc(["exec", "--on", machine, "--timeout", "60", "--", "rm", "-rf", "--", "/tmp/pa-suite-" + run])
    if code:
        notes.append("rm -rf /tmp/pa-suite-{} on {}: {}".format(run, machine, first_line(err, "exit {}".format(code))))
    return notes


def sweep(machine, current):
    """R18: the leftovers of this Mac's earlier remote runs on machine (done or gone, not cleaned): cleaned."""
    notes = []
    for row in all_progress():
        run = row.get("run")
        if run == current or row.get("machine") != machine or not isinstance(row.get("remote"), dict) or row.get("cleaned"):
            continue
        if not RUN_ID.match(run or "") or not (row.get("state") == "done" or gone(row)):
            continue
        left = cleanup(machine, run, (row.get("remote") or {}).get("terminal"))
        if not [n for n in left if n.startswith("rm -rf")]:
            update_progress(os.path.join(runs_dir(), run + ".json"), cleaned=True)
        notes += left
    return notes


def stop_group(child):
    """TERM to a child's process group, KILL after STOP_GRACE; its exit code."""
    if child.poll() is None:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            child.wait(STOP_GRACE)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except OSError:
                pass
            child.wait()
    return 128 - child.returncode if child.returncode < 0 else child.returncode


def mac_os():
    return platform.system() or "local"


def cmd_places(h, args):
    """R25: each configured machine (reachable or why not, cap, holders) and this Mac's slot."""
    good, bad = machines(pa.config())
    where = os.path.join(home(), "config.json")
    for note in bad:
        print("pa-full-suite: suiteMachines in {}: {}".format(where, note))
    if not good:
        print("pa-full-suite: no suiteMachines in {}: every run is on this Mac".format(where))
    for m in good:
        found = probe(m["machine"])
        state = ("reachable ({} {}, in {:g}s; {})".format(found["os"], found["arch"], found["seconds"], ", ".join(
            "{} {}".format(t, found["tools"][t]["version"]) for t in ("git", "python3", "node")))
                 if found["ok"] else "not reachable ({})".format(found.get("reason")))
        holders = peek(machine_dir(m["machine"]), m["cap"])
        print("{}: {}, cap {}, holders: {}".format(m["machine"], state, m["cap"], describe(holders) if holders else "none"))
    holders = peek(slot_dir(), cap())
    print("this Mac: cap {}, holders: {}".format(cap(), describe(holders) if holders else "none"))
    return 0


def full_suite(h, args):
    if getattr(args, "last", False):
        return sys.exit(cmd_last(h, args))
    if getattr(args, "report", False):
        return sys.exit(cmd_report(h, args))
    if getattr(args, "status", False):
        return sys.exit(cmd_status(h, args))
    if getattr(args, "wait", False):
        return sys.exit(cmd_wait(h, args))
    if getattr(args, "places", False):
        return sys.exit(cmd_places(h, args))
    on, local = getattr(args, "on", None), getattr(args, "local", False)
    if on and local:
        print("pa-full-suite: --on and --local name two places; give one")
        return sys.exit(2)
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

    def log(line):  # a line to the output and build.log (the place lines, t-089)
        print(line, flush=True)
        with open(build_log, "a") as f:
            f.write("=== {} full suite: {}\n".format(pa.now(), line))

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
    run = uuid.uuid4().hex[:12]
    output = os.path.join(evidence, "full-suite-{}.out".format(run))
    # t-089: the places. No suiteMachines (or --local): this Mac, today's path and lines exactly.
    good, bad = ([], []) if local else machines(pa.config())
    config_path = os.path.join(home(), "config.json")
    if good and not on and fallback(pa.config())[0] == "none":  # t-107 R11: never this Mac on its own
        on = good[0]["machine"]
        print("pa-full-suite: suiteFallback is \"none\" in {}: as --on {} (--local runs on this Mac)".format(
            config_path, on), flush=True)
    remote_mode = bool(on or good)
    note, chosen, probes, reachable = None, None, {}, []
    for b in bad:  # named once, even when no entry is left (then the run is today's)
        log("pa-full-suite: suiteMachines in {}: {}".format(config_path, b))
    eligible, why_not = (command, None) if module else as_run_all(h, command, profile, record["root"])
    if remote_mode:
        why = remote_ok(eligible, found, module, why_not)
        if on and not [m for m in good if m["machine"] == on]:
            print("pa-full-suite: --on {}: not a machine of suiteMachines in {}".format(on, config_path))
            return sys.exit(2)
        if why:
            if on:
                print("pa-full-suite: --on {}: {}".format(on, why))
                return sys.exit(2)
            note = why
        else:
            for m in good:
                if on and m["machine"] != on:
                    continue
                probes[m["machine"]] = probe(m["machine"])
                if probes[m["machine"]]["ok"]:
                    reachable.append(m)
                else:
                    log("pa-full-suite: {} is not reachable: {}".format(m["machine"], probes[m["machine"]].get("reason")))
            if not reachable:
                why = "; ".join("{}: {}".format(k, v.get("reason")) for k, v in probes.items())
                if on:
                    print("pa-full-suite: --on {}: not reachable ({})".format(on, why))
                    return sys.exit(2)
                note = "no machine reachable: " + why
    info = {"task": record.get("task"), "worker": os.environ.get("HC_TERMINAL_ID") or None,
            "tree": found[0].get("tree") if found else None}
    slot = None
    for m in reachable:  # a free slot at once: the first configured machine that has one
        slot, _ = try_slot(machine_dir(m["machine"]), m["cap"], info)
        if slot:
            chosen = m
            break
    tail_text = "output in {} (pa-full-suite --status shows it; pa-full-suite --wait waits for it)".format(output)
    if not remote_mode:
        print("pa-full-suite: run {}: {}".format(run, tail_text), flush=True)
    elif reachable:
        m = chosen or reachable[0]
        p = probes[m["machine"]]
        log("pa-full-suite: run {} on {} ({}; reachable in {:g}s{}): {}".format(
            run, m["machine"], p["os"], p["seconds"], "" if chosen else "; waiting for a slot", tail_text))
    else:
        log("pa-full-suite: run {} on this Mac ({}): {}".format(run, note, tail_text))
    rows = read_lines(where)
    total, runs = median_total(rows, command=command, place="local" if remote_mode else None)
    first = {"run": run, "kind": "pa-full-suite", "what": command, "task": record.get("task"),
             "worker": os.environ.get("HC_TERMINAL_ID") or None, "dir": os.path.realpath(record["root"]),
             "commit": found[0].get("commit") if found else None, "tree": found[0].get("tree") if found else None,
             "pid": os.getpid(), "state": "waiting", "queued": pa.now(), "output": output, "record": where,
             "median_total": total, "median_runs": runs, "module": module}
    if remote_mode:
        first["place"] = "local"
    progress = new_progress(first)
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
    if reachable and not slot:
        try:
            chosen, slot, why = wait_for_machine(reachable, info, not on, pulse)
        except Waited as e:
            print(e)
            update_progress(progress, state="done", exit=124, end=pa.now(), note=str(e))
            return sys.exit(124)
        if why:
            note = why
            log("pa-full-suite: run {} now on this Mac ({})".format(run, why))
    if chosen:
        m = chosen["machine"]
        try:
            code = run_remote(h, args, record, directory, command, evidence, build_log, where, run, output, progress,
                              slot, m, probes[m], queued, pulse, reuse, log, eligible)
        except SetupFailed as e:  # before anything ran there: this Mac in the same call (exit 2 with --on)
            release(slot)
            note = "setup on {} failed: {}".format(m, e)
            if on:
                print("pa-full-suite: --on {}: {}".format(on, note))
                update_progress(progress, state="done", exit=2, end=pa.now(), note=note)
                return sys.exit(2)
            log("pa-full-suite: run {} now on this Mac ({})".format(run, note))
            update_progress(progress, place="local", machine=None, remote=None)
            slot = None
        else:
            return sys.exit(code)
    if slot is None:
        try:
            slot = acquire(cap(), info, say=lambda m: print(m, flush=True), beat=pulse)
        except Waited as e:
            print(e)
            update_progress(progress, state="done", exit=124, end=pa.now(), note=str(e))
            return sys.exit(124)
    return sys.exit(run_on_mac(h, record, directory, command, module, build_log, where, run, output, progress, slot,
                               queued, pulse, reuse, note))


def wait_for_machine(reachable, info, mac_too, pulse):
    """R3: waits for a slot on any reachable machine; after PREFER_WAIT s (when mac_too) this Mac's slot counts too,
    whichever frees first. (machine or None, slot, why this Mac or None)."""
    began, shown = time.time(), None
    prefer = env_seconds("PA_SUITE_PREFER_WAIT", PREFER_WAIT)
    while True:
        texts = []
        for m in reachable:
            f, holders = try_slot(machine_dir(m["machine"]), m["cap"], info)
            if f:
                return m, f, None
            texts.append("{} (cap {}): {}".format(m["machine"], m["cap"], describe(holders)))
        text = "; ".join(texts)
        if mac_too and time.time() - began >= prefer:
            f, _ = try_slot(slot_dir(), cap(), info)
            if f:
                return None, f, "every machine busy for {:.0f} s: {}".format(time.time() - began, text)
        if text != shown:
            print("pa-full-suite: waiting for a slot on {}".format(text), flush=True)
            shown = text
        if time.time() - began >= WAIT_LIMIT:
            raise Waited("pa-full-suite: no slot came free in {:.0f} s (running: {})".format(time.time() - began, text))
        pulse(text)
        time.sleep(POLL)


def run_on_mac(h, record, directory, command, module, build_log, where, run, output, progress, slot, queued, pulse,
               reuse, note=None):
    """The run on this Mac under slot (today's path); its exit code. note: why it is here (t-089), for the record."""
    marker = os.environ.get("PA_SUITE_SLOT")
    os.environ["PA_SUITE_SLOT"] = slot.name  # tests/run-all.sh under this run sees its caller holds the slot (t-072)
    try:
        waited = round(time.time() - queued, 1)
        found = pins(h, record["root"])  # MF-1: what runs is the tree at this point, not the one before the wait
        if reuse():  # a run of the same tree may have passed while this one waited
            update_progress(progress, state="done", exit=0, end=pa.now(), note="not run: this tree passed while it waited")
            return 0
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
    if note:
        extra["note"] = note
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
    return code


def run_remote(h, args, record, directory, command, evidence, build_log, where, run, output, progress, slot, machine,
               found_probe, queued, pulse, reuse, log, eligible=None):
    """R7-R18 on machine under its slot: ship, run there, the integration part here, copy back, record, clean.
    Its exit code; SetupFailed before anything ran there (the slot is then still held, for the caller to release)."""
    root = record["root"]
    found = pins(h, root)  # MF-1
    if reuse():
        release(slot)
        update_progress(progress, state="done", exit=0, end=pa.now(), note="not run: this tree passed while it waited")
        return 0
    why = remote_ok(eligible or command, found, None)
    if why:
        raise SetupFailed(why)
    waited = round(time.time() - queued, 1)
    os_name, tools = found_probe.get("os"), found_probe.get("tools") or {}
    folder = folder_of(run)
    for n in sweep(machine, run):
        log("pa-full-suite: leftovers of an earlier run on {}: {}".format(machine, n))
    total, runs = median_total(read_lines(where), command=command, place=machine)
    update_progress(progress, place=machine, machine=machine, os=os_name, remote={"folder": folder, "terminal": None},
                    median_total=total, median_runs=runs)
    start, load, began = pa.now(), load_now(), time.time()
    terminal, child, watcher, side, notes = None, None, None, None, []
    saved = {}

    def on_signal(sig, frame):
        raise Interrupted(SIGNAL_EXIT.get(sig, 1))
    for sig in SIGNAL_EXIT:
        saved[sig] = signal.signal(sig, on_signal)
    try:
        try:
            ship(h, machine, run, found[0], tools, root)
            terminal = start_remote(machine, run, folder, tools)
        except SetupFailed:
            for n in cleanup(machine, run, terminal):
                log("pa-full-suite: cleanup on {}: {}".format(machine, n))
            update_progress(progress, cleaned=True)
            raise
        update_progress(progress, state="running", start=start, commit=found[0].get("commit"), tree=found[0].get("tree"),
                        remote={"folder": folder, "terminal": terminal})
        side = (progress or os.path.join(runs_dir(), run + ".json")) + ".local"
        write_progress(side, {"run": run + "-local", "kind": "local part"})
        watcher = RemoteWatcher(machine, folder, output, progress, side)
        watcher.start()
        local_out = output[:-len(".out")] + "-local.out"
        env = dict(os.environ, PA_CHANGED_FILES="", PA_SUITE_SLOT=slot.name, PA_SUITE_PROGRESS=side)
        with open(local_out, "w") as f:
            child = subprocess.Popen(["sh", "-c", REMOTE_COMMAND + " --only-integration"], cwd=root, env=env,
                                     stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT, start_new_session=True)
        local_code, local_began, local_seconds, local_stopped = None, time.time(), None, False
        while True:
            pulse()
            if local_code is None and child.poll() is not None:
                local_code = 128 - child.returncode if child.returncode < 0 else child.returncode
                local_seconds = round(time.time() - local_began, 1)
            if watcher.ended.is_set():
                if (watcher.lost is not None or watcher.timed_out) and local_code is None:
                    local_code, local_stopped = stop_group(child), True
                    local_seconds = round(time.time() - local_began, 1)
                if local_code is not None:
                    break
            if local_code is None and time.time() - local_began > REMOTE_TIMEOUT:
                local_code, local_stopped = stop_group(child), True
                local_seconds = round(time.time() - local_began, 1)
            time.sleep(0.2)
        remote_seconds = round(time.time() - began, 1)
        if watcher.lost is not None:
            remote_code = 124
            notes.append("machine {} lost after {:.0f} s".format(machine, watcher.lost))
        elif watcher.timed_out:
            remote_code = 124
            notes.append("the remote part timed out after {} s".format(REMOTE_TIMEOUT))
        else:
            remote_code = watcher.code
            n = watcher.final()
            if n:
                notes.append(n)
        if local_stopped:
            notes.append("the local part was stopped")
        code = remote_code if remote_code else local_code
        seconds, end = round(time.time() - began, 1), pa.now()
        change = moved(found, pins(h, root))
        remote_text = pa.read_text(output) or ""
        local_text = pa.read_text(local_out) or ""
        mac = mac_os()
        with open(output, "w") as f:
            f.write(remote_text if not remote_text or remote_text.endswith("\n") else remote_text + "\n")
            f.write("=== local part on this Mac\n")
            f.write(local_text if not local_text or local_text.endswith("\n") else local_text + "\n")
            for n in notes:
                f.write("=== {}\n".format(n))
            f.write("=== places: {} ({}) exit {}; local exit {} -> exit {}\n".format(machine, os_name, remote_code, local_code, code))
        out = pa.read_text(output) or ""
        try:
            os.remove(local_out)
        except OSError:
            pass
        update_progress(progress, state="done", exit=code, end=end, note="; ".join(notes) or None)
        with open(build_log, "a") as f:
            f.write("=== {} full suite: `{}` in {} on {} ({}) -> exit {} after {:.0f}s ===\n{}\n".format(
                end, command, root, machine, os_name, code, seconds, h.tail(out, 200)))
        parts = [dict(p, place=machine) for p in parse_parts(remote_text)] + \
                [dict(p, place="local") for p in parse_parts(local_text)]
        extra = {"run": run, "start": start, "end": end, "waited": waited, "load": load, "parts": parts, "output": output,
                 "machine": machine, "os": os_name, "tools": tools,
                 "places": [{"place": machine, "os": os_name, "exit": remote_code, "seconds": remote_seconds},
                            {"place": "local", "os": mac, "exit": local_code, "seconds": local_seconds}]}
        if notes:
            extra["note"] = "; ".join(notes)
        if change:
            extra["moved"] = True
        try:
            h.suite_record(directory, record, command, code, seconds, found, extra)
            recorded = "recorded in " + where
        except OSError as e:
            recorded = "could not record the run in {} ({})".format(where, e)
        left = cleanup(machine, run, terminal)
        for n in left:
            log("pa-full-suite: cleanup on {}: {} (the exit stays {})".format(machine, n, code))
        if not [n for n in left if n.startswith("rm -rf")]:
            update_progress(progress, cleaned=True)
        terminal = None  # cleaned
        print(h.tail(out, 40))
        if change:
            print("pa-full-suite: the worktree changed during the run ({}); the run is recorded under the tree it started "
                  "on with moved: true and is never reused; run pa-full-suite again for the new tree".format(change))
        print("full suite exit {} on {} ({}) and this Mac (appended to {}; {})".format(code, machine, os_name, build_log, recorded))
        return code
    except Interrupted as e:
        print("pa-full-suite: interrupted: ending the run on {} and cleaning up".format(machine), flush=True)
        if child is not None:
            stop_group(child)
        if watcher is not None:
            watcher.stop()
        left = cleanup(machine, run, terminal)
        for n in left:
            log("pa-full-suite: cleanup on {}: {}".format(machine, n))
        update_progress(progress, state="done", exit=e.code, end=pa.now(), note="interrupted",
                        cleaned=not [n for n in left if n.startswith("rm -rf")])
        return e.code
    finally:
        for sig, old in saved.items():
            signal.signal(sig, old)
        if child is not None and child.poll() is None:
            stop_group(child)
        if watcher is not None:
            watcher.stop()
        if side:
            try:
                os.remove(side)
            except OSError:
                pass
        if not isinstance(sys.exc_info()[1], SetupFailed):
            release(slot)  # on a setup failure the caller releases it (and takes this Mac's slot)



if __name__ == "__main__":
    if sys.argv[1:2] == ["slot"]:
        sys.exit(run_under_slot(sys.argv[2:]))
    print("usage: python3 pa_suite.py slot --for <what> [--worker W] [--dir D] -- <command...>", file=sys.stderr)
    sys.exit(2)
