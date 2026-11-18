"""pa-full-suite's run logic (t-056). Python standard library only (3.7+). Loaded by pa_hooks.py from its own
folder; the pa_hooks module is passed in (`h`) for its helpers, so nothing here copies them.

  pa-full-suite [--again]                          run the full suite once for this worktree's tree, recorded in
                                                   content/suites.jsonl; a passing clean tree is not run again
                                                   unless --again
  pa-full-suite --last [--commit C|--tree T]       the last recorded run (default: this worktree's HEAD tree)
  pa-full-suite --report                           the suite's parts by time, slowest median first

The machine cap: at most CAP full suites at once per user (slots under HERDR_PA_HOME, i.e. ~/.config/herdr-pa,
never a space). A slot is an flock on suite-slots/slot-<i>, so the kernel frees it when its holder dies.
"""
import fcntl
import json
import os
import re
import statistics
import subprocess
import sys
import time
import uuid

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


def slot_dir():
    return os.path.join(pa.ROOT, "suite-slots")


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
    return "; ".join("task {} (worker {}, pid {}, since {})".format(h.get("task"), h.get("worker"), h.get("pid"), h.get("start"))
                     for h in holders) or "unknown"


def acquire(limit_slots, info, poll=POLL, limit=WAIT_LIMIT, say=print):
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
            f.write(json.dumps(dict(info, pid=os.getpid(), start=pa.now())))
            f.flush()
            return f
        text = describe(holders)
        if text != shown:
            say("pa-full-suite: waiting for a free slot (cap {}): running: {}".format(limit_slots, text))
            shown = text
        if time.time() - began >= limit:
            raise Waited("pa-full-suite: no slot came free in {:.0f} s (running: {})".format(time.time() - began, text))
        time.sleep(poll)


def release(f):
    try:
        f.seek(0)
        f.truncate()
        fcntl.flock(f, fcntl.LOCK_UN)
    finally:
        f.close()


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
