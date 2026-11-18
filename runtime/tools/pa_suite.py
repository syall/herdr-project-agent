"""pa-full-suite's run logic (t-056). Python standard library only (3.7+). Loaded by pa_hooks.py from its own
folder; the pa_hooks module is passed in (`h`) for its helpers, so nothing here copies them.

  pa-full-suite [--again]                          run the full suite once for this worktree's tree, recorded in
                                                   content/suites.jsonl; a passing clean tree is not run again
                                                   unless --again
  pa-full-suite --last [--commit C|--tree T]       the last recorded run (default: this worktree's HEAD tree)
  pa-full-suite --report                           the suite's parts by time, slowest median first

The machine cap: at most CAP suite runs at once per user (slots under HERDR_PA_HOME, i.e. ~/.config/herdr-pa,
never a space). A slot is an flock on suite-slots/slot-<i>, so the kernel frees it when its holder dies.

Run as a script (t-072), it is how tests/run-all.sh takes the same slot, full or --affected:
  python3 pa_suite.py slot --for <what> [--worker W] [--dir D] -- <command...>
waits for a slot (saying for whom), runs the command under it and exits with its code (124: no slot came free).
A caller that holds a slot already (pa-full-suite) names it in PA_SUITE_SLOT; it is trusted only when that slot
is locked and its recorded holder is an ancestor of this process, so the run under it does not wait for itself.
The command gets PA_SUITE_SLOT, PA_SUITE_SLOT_PID (this process) and PA_SUITE_HOLD (the seconds waited, or held
by the caller). Script mode never loads pa.py (run-all.sh's test fixture stubs it): HERDR_PA_HOME and a UTC clock.
"""
import argparse
import datetime
import fcntl
import json
import os
import re
import signal
import statistics
import subprocess
import sys
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
PART = re.compile(r"^  (.+): (ok|FAILED) \((\d+)s\)$")


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
    return [{"name": m.group(1), "ok": m.group(2) == "ok", "seconds": int(m.group(3))}
            for m in (PART.match(l) for l in text.splitlines()) if m]


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


def acquire(limit_slots, info, poll=POLL, limit=WAIT_LIMIT, say=print, who="pa-full-suite"):
    """Takes a free slot (an open file holding an flock) out of limit_slots, waiting for one. Says once that it
    waits and for whom, and again when the holders change. Raises Waited after `limit` seconds."""
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


def held_by_caller(path):
    """The holder line of the slot at path when a caller of this process holds it (the path is one of this home's
    slots, it is locked, and its recorded pid is an ancestor), else None: a stale or leaked marker is not trusted."""
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
    return holder if isinstance(pid, int) and pid in ancestors() else None


def task_of(start):
    """The task id of the worktree around start (pa-hooks install marks it in .claude/pa-task.json), else None."""
    folder = os.path.realpath(start)
    while True:
        try:
            with open(os.path.join(folder, ".claude", "pa-task.json")) as f:
                record = json.load(f)
            if isinstance(record, dict) and record.get("task"):
                return record["task"]
        except (OSError, ValueError):
            pass
        parent = os.path.dirname(folder)
        if parent == folder:
            return None
        folder = parent


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
    limit = int(value) if value.isdigit() else WAIT_LIMIT
    who = args.what.split()[0] if args.what.split() else "pa_suite.py"
    marker = os.environ.get("PA_SUITE_SLOT", "")
    caller, slot = held_by_caller(marker), None
    if caller:
        path = marker
        hold = "held by the caller (task {}, pid {}, {})".format(caller.get("task"), caller.get("pid"), os.path.basename(path))
    else:
        queued = time.time()
        info = {"task": task_of(args.dir), "worker": args.worker or None, "what": args.what, "dir": args.dir}
        try:
            slot = acquire(cap(), info, limit=limit, say=lambda m: print(m, flush=True), who=who)
        except Waited as e:
            print(e, flush=True)
            return 124
        except KeyboardInterrupt:
            return 130
        path = slot.name
        hold = "waited {:.1f}s for {} (cap {})".format(time.time() - queued, os.path.basename(path), cap())
    env = dict(os.environ, PA_SUITE_SLOT=path, PA_SUITE_SLOT_PID=str(os.getpid()), PA_SUITE_HOLD=hold)
    child = None

    def forward(sig, frame):  # TERM, INT or HUP to this process goes to the run, which stops its parts
        if child is not None and child.poll() is None:
            child.send_signal(sig)
    try:
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, forward)
        child = subprocess.Popen(command, env=env)
        code = child.wait()
    finally:
        if slot is not None:
            release(slot)
    return 128 - code if code < 0 else code


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


def full_suite(h, args):
    if getattr(args, "last", False):
        return sys.exit(cmd_last(h, args))
    if getattr(args, "report", False):
        return sys.exit(cmd_report(h, args))
    record = h.find_task(os.getcwd())
    if not record:
        raise pa.Problem("run pa-full-suite inside a task's worktree (pa-hooks install marks it)")
    directory, agent, profile = h.load_context(record["space"])
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
    queued = time.time()
    try:
        slot = acquire(cap(), {"task": record.get("task"), "worker": os.environ.get("HC_TERMINAL_ID") or None,
                               "tree": found[0].get("tree") if found else None})
    except Waited as e:
        print(e)
        return sys.exit(124)
    marker = os.environ.get("PA_SUITE_SLOT")
    os.environ["PA_SUITE_SLOT"] = slot.name  # tests/run-all.sh under this run sees its caller holds the slot (t-072)
    try:
        waited = round(time.time() - queued, 1)
        found = pins(h, record["root"])  # MF-1: what runs is the tree at this point, not the one before the wait
        if reuse():  # a run of the same tree may have passed while this one waited
            return sys.exit(0)
        start, load, began = pa.now(), load_now(), time.time()
        code, out = h.run_shell(command, record["root"], [], 3600)
        seconds, end = round(time.time() - began, 1), pa.now()
        change = moved(found, pins(h, record["root"]))
    finally:
        if marker is None:
            os.environ.pop("PA_SUITE_SLOT", None)
        else:
            os.environ["PA_SUITE_SLOT"] = marker
        release(slot)
    with open(build_log, "a") as f:
        f.write("=== {} full suite: `{}` in {} -> exit {} after {:.0f}s ===\n{}\n".format(end, command, record["root"], code, seconds, h.tail(out, 200)))
    extra = {"run": uuid.uuid4().hex[:12], "start": start, "end": end, "waited": waited, "load": load, "parts": parse_parts(out)}
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
