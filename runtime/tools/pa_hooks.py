#!/usr/bin/env python3
"""Project Agent gates and worker hooks. Python standard library only (3.7+).

Selected by the name it is invoked as (symlinks in ~/.local/bin):

  pa-gate <task-id> [--phase P] [--space ID]    deterministic evidence check (exit 0 pass, 1 fail)
  pa-hooks install <worktree> --task ID [--fix] install the hooks below into a worktree's local settings
  pa-hooks remove <worktree>                    take them out again
  pa-full-suite [--again]                       run the full test suite once, output into build.log, one line per
                                                repo in content/suites.jsonl; a passing clean tree is not run again;
                                                at most pa_suite.CAP at once per machine (see pa_suite.py)
  pa-full-suite --last [--commit C|--tree T]    the last recorded run; --report: its parts by time
  pa-full-suite --local | --on M | --places     on this Mac only, on machine M only, or where runs can go: the
                                                machines of config.json's suiteMachines (t-089, see pa_suite.py);
                                                --places --json: the same as JSON (t-119, the panel reads it)
  pa-run [--on M|--local] [--commit C --overlay P] -- <argv...>
                                                a test command in the tree to run (HEAD plus the worktree's changes)
                                                on a configured machine or this Mac (t-107, see pa_run.py)
  pa-run --report [--days N]                    which kinds of test run are faster here or on a machine (t-107)
  pa-worktree check [--open-cards]              worktrees whose tasks are all done or parked: list them, open decision cards
  pa-worktree remove <task-id>                  delete a task's worktree, only with an answered "Delete" card
  pa-worktree remove --card <d-wt-batch-n>      delete each worktree an answered batch card covers, each re-checked;
                                                one that changed is skipped and listed (t-168)
  pa-worktree remove --standing <rule>          the Project Agent's: delete the clean worktrees (merged into main and
                                                pushed, nothing uncommitted or untracked, no terminal, no keep) on the
                                                person's rule bound in worktree-allow.json (t-168)
  pa-worktree-allow show|set|unbind|on|off      the person's binding of that rule (show: anyone)
  pa-worker info <address>                      what deleting a worker would remove, as JSON
  pa-worker delete <address> [--keep-worktree] [--discard] [--with-task]
                                                the person's command: end a worker and remove what belongs to it
  pa-worker close <address> --evidence F         end a worker whose report is read (agents may run it; guarded)
  pa-worker close --leftovers [--dry-run]        list the workers from before one-worker-per-job on one decision card
  pa-worker close [<address>] --card ID          close the workers the person approved on that card (same guards)
  pa-worker keep <address> --quote TEXT [--off]  record that the person wants a worker kept open
  pa-trust <address> [--space ID]               a blocked worker's Claude Code folder trust prompt: accept it only for
                                                the project folder or a task's evidence folder (see cmd_trust); one
                                                JSON line, exit 0 accepted, 1 not, 2 usage or hc error
  pa-hook <name>                                one hook, called by the agent harness (reads its JSON on stdin):
      lint-after-edit   after each edit: lint the changed file
      affected-tests    when a task is marked complete, and (--stop) before the session ends; nothing when the
                        worktree is as it was when this worker started (t-133)
      session-start     when a session starts: record the worktree's state once per worker (temp folder)
      test-file-lock    before an edit: no test-file edits during a fix task
      guard             before a command or an edit: nothing that cannot be taken back,
                        and the short denylist in pa_guard.py

A failing hook exits with code 2, which sends the agent back to fix it.
"""
import argparse
import contextlib
import datetime
import hashlib
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True  # keep tools/ free of caches
import pa  # noqa: E402


def tool_spec(name):
    """The import spec of <name>.py in this file's own folder. t-147: when pa_launch.py started this process, its
    Finder (on sys.meta_path) gives one whose code comes from the compiled-code cache; else the usual source loader."""
    path = os.path.join(os.path.dirname(os.path.realpath(__file__)), name + ".py")
    for finder in sys.meta_path:
        make = getattr(finder, "pa_tool_spec", None)
        if make is not None:
            return make(name, path)
    return importlib.util.spec_from_file_location(name, path)


def _load_guard():
    """pa_guard.py from this file's own folder, found the same way when run through a symlink or loaded by a test.

    Loaded by hook_guard each time it is needed, not at import, so that a missing or broken pa_guard.py stops only
    the guard (which then refuses the call) and the other hooks keep working.
    """
    spec = tool_spec("pa_guard")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Card d-038 ("Block"): when the guard cannot load or its check fails, the call is refused, not let through.
GUARD_UNAVAILABLE = ("Refused by the Project Agent's guard: guard unavailable ({}). Commands and edits cannot be checked, "
                     "so they do not run: hc send is refused too. Stop and report this to the Project Agent by saying so as your "
                     "last message; it repairs the guard. There is no override.")
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")

TASK_FILE = os.path.join(".claude", "pa-task.json")
SETTINGS = os.path.join(".claude", "settings.local.json")
MARK = "pa-hook "  # every hook command we install contains this
PHASES = ["intent", "design", "plan", "build", "test", "review", "release", "maintain"]
# What each phase must leave behind before a task may leave it.
ARTIFACTS = {
    "intent": ["intent.md"],
    "design": ["spec.md"],
    "plan": ["plan.md"],
    "build": ["FINDINGS.md", "DECISIONS.md"],
    "test": ["VERIFY.md"],
    "review": ["REVIEW.md"],
}
TEST_FILE = re.compile(r"(^|/)(tests?|__tests__|spec|specs)/|(^|/)test_[^/]+$|[._-](test|tests|spec)\.[^/]+$|Tests?\.[A-Za-z]+$")
# Never, whatever the handoff says. Each is (pattern, what it is).
DENY_COMMANDS = [
    (r"\bgit\s+push\b[^;&|]*(\s--force\b|\s-f\b|\s--force-with-lease\b|\s\+\S)", "a force push"),
    (r"\bgit\s+reset\s+--hard\b", "discarding uncommitted work (git reset --hard)"),
    (r"\bgit\s+clean\b[^;&|]*\s-[a-zA-Z]*f", "deleting untracked work (git clean -f)"),
    (r"\bgit\s+(checkout|restore)\s+(--\s+)?\.(\s|$)", "discarding uncommitted work"),
    (r"\bgit\s+stash\s+(drop|clear)\b", "deleting stashed work"),
    (r"\bgit\s+worktree\s+remove\b[^;&|]*(\s--force\b|\s-f\b)", "force-removing a worktree (uncommitted work is lost)"),
    (r"\bgit\s+branch\s+[^;&|]*-D\b", "force-deleting a branch"),
    (r"\bgh\s+pr\s+merge\b", "merging a change request"),
    (r"\bgh\s+release\s+create\b", "publishing a release"),
    (r"\b(npm|pnpm|yarn)\s+publish\b|\bcargo\s+publish\b|\btwine\s+upload\b", "publishing a package"),
    (r"\bterraform\s+(apply|destroy)\b|\bkubectl\s+(apply|delete|rollout)\b|\bcdk\s+(deploy|destroy)\b|\bhelm\s+(install|upgrade|uninstall)\b", "a deploy or infrastructure change"),
    (r"(^|[\s'\"=/])\.env(\.[\w.-]+)?(\s|$|['\"])|~/\.ssh\b|/\.ssh/|\bid_(rsa|ed25519|ecdsa)\b", "reading secrets"),
    # t-009 R15: billed amounts come only from the person (quotes and backslashes between the words do not hide it)
    (r"\bpa-costs['\"\s\\]+billed['\"\s\\]+(fetch|enable|import)\b", "the person's billing command (pa-costs billed fetch, enable or import)"),
    # t-032 R-S5: the price fetch downloads a price list and writes the price file: the person's (--dry-run too: it downloads)
    (r"\bpa-costs\b[^;&|\n]*?['\"\s\\]prices['\"\s\\]+([^;&|\n]*?['\"\s\\])?fetch\b", "the person's price fetch (pa-costs prices fetch)"),
]
DENY_PATHS = [(r"(^|/)\.env(\.[^/]*)?$", "an environment file with secrets"), (r"(^|/)\.ssh(/|$)", "SSH keys"),
              (r"(^|/)(credentials|\.netrc|\.npmrc|\.pypirc)$", "a credentials file")]
ROUTE = "This needs the person's approval. Stop, and report it to the Project Agent; it opens a decision card."


def find_task(start):
    """The worktree's task record ({task, space, fix, root}), walking up from start."""
    folder = os.path.realpath(start)
    while True:
        record = pa.read_json(os.path.join(folder, TASK_FILE))
        if record:
            return dict(record, root=folder)
        parent = os.path.dirname(folder)
        if parent == folder:
            return None
        folder = parent


def space_from_terminal():
    seen, address = set(), os.environ.get("HC_TERMINAL_ID")
    while address and address not in seen and len(seen) < 6:
        seen.add(address)
        info = pa.hc_get(address)
        if (info.get("labels") or {}).get("herdr.pa"):
            return info["labels"]["herdr.pa"]
        address = info.get("parent")
    return None


def load_context(space):
    directory = pa.space_dir(space)
    agent = pa.read_json(os.path.join(directory, "agent.json"))
    if not agent:
        raise pa.Problem("no Project Agent state for space " + space)
    return directory, agent, pa.load_profile(agent.get("profile"))


def command_for(kind, agent, profile):
    """The space's own command (agent.json "commands"), else the profile's. kind: lint | affected | full."""
    own = (agent.get("commands") or {}).get(kind)
    if own:
        return own
    value = profile.get("lint") if kind == "lint" else (profile.get("tests") or {}).get(kind)
    if value and value.startswith("hooks/"):
        value = os.path.join(profile["_folder"], value)
    return value or None


def git_status(root, every=False):
    """The worktree's uncommitted files, or None when git cannot answer in 30 s; every=True lists each file under an
    untracked folder, not the folder. -z: paths as they are, never quoted (t-136, t-133 RL6)."""
    try:
        out = subprocess.run(["git", "status", "--porcelain", "-z"] + (["--untracked-files=all"] if every else []), cwd=root,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    files, fields = [], iter(out.decode("utf-8", "surrogateescape").split("\0"))
    for entry in fields:
        if "R" in entry[:2] or "C" in entry[:2]:  # a rename or copy: its source path is the next field
            next(fields, None)
        if entry[3:] and not entry[3:].startswith(".claude/"):
            files.append(entry[3:])
    return files


def changed_files(root, every=False):
    """git_status's list; [] when git cannot answer."""
    return git_status(root, every) or []


TIMED_OUT = "(timed out after {} s)"  # run_shell's last line when the command outlived its limit
KILL_GRACE = 5  # t-147: seconds a command's process group has to end on SIGTERM before SIGKILL


def end_group(proc, grace):
    """t-147: ends what is left of proc's process group (its own, start_new_session): SIGTERM, up to grace s for it
    to go (proc itself reaped meanwhile, so it is not counted as a zombie), then SIGKILL. Never raises."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except OSError:  # ESRCH: nothing left (EPERM on macOS: only zombies left)
            return
        end = time.monotonic() + (grace if sig == signal.SIGTERM else 0)
        while time.monotonic() < end:
            proc.poll()
            try:
                os.killpg(proc.pid, 0)
            except OSError:
                return
            time.sleep(0.05)


def run_shell(command, cwd, files, timeout, extra_env=None, mark=None, first=None):
    """Runs a configured command. {file}/{files} are filled in; PA_CHANGED_FILES lists them, one per line.

    t-147: it runs in a process group of its own, stdin /dev/null, its output in a temp file (no pipe that a
    grandchild could hold open). Its limit is timeout s; with mark and first, first s unless the file mark exists by
    then (the command entered the slot step), and then timeout s from the start. At the limit the whole group is
    ended (end_group) and the last line is TIMED_OUT; what a clean exit leaves in the group is ended too."""
    quoted = " ".join(shlex.quote(f) for f in files)
    line = command.replace("{files}", quoted).replace("{file}", quoted)
    env = dict(os.environ, PA_CHANGED_FILES="\n".join(files), **(extra_env or {}))
    limit = first if mark and first is not None and first < timeout else timeout
    code = None
    with tempfile.TemporaryFile() as out:
        proc = subprocess.Popen(["sh", "-c", line], cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT, start_new_session=True)
        began = time.monotonic()
        try:
            while code is None:
                try:
                    code = proc.wait(timeout=max(0, began + limit - time.monotonic()))
                except subprocess.TimeoutExpired:
                    if limit == timeout or not os.path.exists(mark):
                        break
                    limit = timeout  # in the slot step: the whole limit, counted from the start
        finally:
            end_group(proc, KILL_GRACE)
            if code is None:
                try:
                    proc.wait(timeout=KILL_GRACE)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        out.seek(0)
        text = out.read().decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
    if code is None:
        return 124, text + "\n" + TIMED_OUT.format(limit)
    return code, text


def tail(text, lines=40):
    rows = text.rstrip().splitlines()
    return "\n".join((["... ({} more lines above)".format(len(rows) - lines)] if len(rows) > lines else []) + rows[-lines:])


def block(message):
    print(message, file=sys.stderr)
    sys.exit(2)


# ---- hooks ------------------------------------------------------------------------------------

def hook_input():
    try:
        return json.load(sys.stdin) if not sys.stdin.isatty() else {}
    except ValueError:
        return {}


def hook_context(data):
    record = find_task(data.get("cwd") or os.getcwd())
    if not record:
        return None, None, None
    try:
        _dir, agent, profile = load_context(record["space"])
    except pa.Problem:
        return record, {}, {}
    return record, agent, profile


def hook_lint(data, _args):
    record, agent, profile = hook_context(data)
    path = (data.get("tool_input") or {}).get("file_path")
    command = record and command_for("lint", agent, profile)
    if not command or not path or not os.path.isfile(path):
        return 0
    code, out = run_shell(command, record["root"], [os.path.relpath(path, record["root"])], 120)
    if code:
        block("Lint failed for {} (task {}). Fix it before going on.\n{}".format(path, record["task"], tail(out)))
    return 0


# t-133: a worker that changed nothing in its worktree since it started (a reviewer, verifier, check or design worker)
# runs no affected tests at its end. The session-start hook records the worktree's state once per worker; first write
# wins, so a resume or a compaction keeps the first state. t-136 (t-133 RL2): the record is in the space's own folder
# (<space>/.hook-starts/, the user's, fenced for workers' editor writes; nothing is written into the worktree), 0600,
# and one that is not the user's or that others can write is not read. (RL5) A snapshot that cannot be taken within
# SNAPSHOT_LIMIT s (git status's own timeout included) is None: no start record, or at the end the tests run.
START_DIR = None  # None: <space>/.hook-starts (tests set a folder of their own)
SNAPSHOT_LIMIT = 20  # seconds a snapshot may take, git status and hashing together
START_KEPT_DAYS = 30  # older records are removed when a new one is written


class OverBudget(Exception):
    pass


def start_path(record, data):
    """The worker's start record, or None when the task's space has no folder here."""
    key = os.environ.get("HC_TERMINAL_ID") or "session:" + str(data.get("session_id") or "none")
    digest = hashlib.sha1("{}\0{}".format(record["root"], key).encode("utf-8")).hexdigest()[:24]
    folder = START_DIR
    if not folder:
        try:
            directory = pa.space_dir(str(record.get("space") or ""))
        except pa.Problem:
            return None
        folder = os.path.join(directory, ".hook-starts") if os.path.isdir(directory) else None
    return folder and os.path.join(folder, "pa-hook-start-{}.json".format(digest))


def hashed(path, digest, deadline):
    """Adds the file's bytes to digest, 1 MB at a time; OverBudget past the deadline."""
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
            if time.monotonic() > deadline:
                raise OverBudget()
    return digest


def entry_state(path, deadline):
    """A link's target (what git records, t-133 N2), a folder git lists whole (a nested repository: every path and
    file below it), else the sha1 of the file's bytes."""
    if os.path.islink(path):
        return "link:" + os.readlink(path)
    if not os.path.isdir(path):
        return hashed(path, hashlib.sha1(), deadline).hexdigest()
    digest = hashlib.sha1()
    for top, dirs, names in os.walk(path):
        dirs.sort()
        for name in sorted(names) + [d for d in dirs if os.path.islink(os.path.join(top, d))]:
            full = os.path.join(top, name)
            digest.update(os.path.relpath(full, path).encode("utf-8", "surrogateescape") + b"\0")
            try:
                digest.update(("link:" + os.readlink(full)).encode("utf-8", "surrogateescape") if os.path.islink(full)
                              else hashed(full, hashlib.sha1(), deadline).digest())
            except OSError:
                digest.update(b"gone")
    return "dir:" + digest.hexdigest()


def tree_state(root):
    """{head, files: {path: entry_state, or "gone"}} of the worktree's changed files, each file under an untracked
    folder listed on its own (git_status every=True), or None (git did not answer, or over SNAPSHOT_LIMIT)."""
    deadline = time.monotonic() + SNAPSHOT_LIMIT
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              universal_newlines=True, timeout=30).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    listed, files = git_status(root, every=True), {}
    if listed is None or time.monotonic() > deadline:
        return None
    for rel in listed:
        try:
            files[rel] = entry_state(os.path.join(root, rel), deadline)
        except OverBudget:
            return None
        except OSError:
            files[rel] = "gone"
    return {"head": head, "files": files}


def write_start(path, state):
    """Writes the record once: a private temp file linked into place, so the first write wins even in a race."""
    folder = os.path.dirname(path)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    old = time.time() - START_KEPT_DAYS * 86400
    for name in os.listdir(folder):
        try:
            if name.startswith("pa-hook-start-") and os.lstat(os.path.join(folder, name)).st_mtime < old:
                os.unlink(os.path.join(folder, name))
        except OSError:
            pass
    fd, temp = tempfile.mkstemp(dir=folder, prefix=".pa-hook-start-")  # 0600
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f)
        os.link(temp, path)
    except FileExistsError:
        pass
    finally:
        os.unlink(temp)


def hook_session_start(data, _args):
    record = find_task(data.get("cwd") or os.getcwd())
    path = record and start_path(record, data)
    if not path or os.path.lexists(path):
        return 0
    state = tree_state(record["root"])
    if state is None:
        return 0
    try:
        write_start(path, dict(state, root=record["root"], task=record.get("task"), at=pa.now()))
    except OSError:
        pass
    return 0


def unchanged_since_start(record, data):
    path = start_path(record, data)
    try:
        info = os.lstat(path) if path else None
    except OSError:
        info = None
    if not info or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        return False  # none, or not the user's own: the tests run
    start = pa.read_json(path)
    if not isinstance(start, dict) or start.get("root") != record["root"]:
        return False
    now = tree_state(record["root"])
    return now is not None and now["head"] == start.get("head") and now["files"] == start.get("files")


# t-079 I2 (t-072 RL-5): the affected-tests hook's run_shell limit is a whole slot wait plus a whole run, so a run
# that waited long for the suite slot still has its own time once it has it
AFFECTED_WAIT = 840  # the slot step's wait at most (pa_suite.SLOT_WAIT_LIMIT)
AFFECTED_RUN = 900  # the run's own time once it has the slot (PA_SUITE_RUN_LIMIT is this less DEADLINE_SPARE);
# t-147: also the whole limit of a command that never entered the slot step (no PA_HOOK_SLOT_MARK made)
AFFECTED_LIMIT = AFFECTED_WAIT + AFFECTED_RUN
DEADLINE_SPARE = 30  # R14: PA_SUITE_DEADLINE is this long before that limit


# t-117 R7: the slot step's own could-not-run last lines, as pa_suite prints them (NOT_RUN_PREFIX, NO_SLOT_TEXT,
# STOPPED_TEXT there): `run-all.sh: not run on <M>: ...` / `run-all.sh: not run: ...`, `<who>: no slot came free in
# ...`, `<who>: stopped waiting for the suite slot after ...`. Any other 124 or 125 is the command's own.
COULD_NOT_RUN = re.compile(r"^(?:run-all\.sh: not run(?: on |: )|\S+: no slot came free in |"
                           r"\S+: stopped waiting for the suite slot after )")


def could_not_run(out):
    """R7: the last non-empty line, when it is one of the slot step's could-not-run lines, else None."""
    lines = [l.strip() for l in (out or "").splitlines() if l.strip()]
    return lines[-1].rstrip(".") if lines and COULD_NOT_RUN.match(lines[-1]) else None


def not_run_line(out):
    """R15: the line that says why the tests did not run: the last `... not run ...` / `no slot came free` line,
    else the last line."""
    lines = [l.strip() for l in (out or "").splitlines() if l.strip()]
    for line in reversed(lines):
        if re.search(r"\bnot run\b|no slot came free", line):
            return line.rstrip(".")
    return lines[-1].rstrip(".") if lines else "(no output)"


def did_not_finish(out):
    """MF-J2-1: the line that says the tests started and did not end, else None: run_shell's own timeout (its last
    line) or the slot step's `run-all.sh: did not finish on <M>: ...` (the deadline or the machine's limit during the
    run). A test the change makes hang ends this way; it is a failure to fix, never "could not run"."""
    lines = [l.strip() for l in (out or "").splitlines() if l.strip()]
    if lines and (re.fullmatch(re.escape(TIMED_OUT).replace(r"\{\}", r"\d+"), lines[-1])
                  or lines[-1].startswith("run-all.sh: did not finish on ")):
        return lines[-1].rstrip(".")
    return None


def hook_affected(data, args):
    record, agent, profile = hook_context(data)
    if not record:
        return 0
    files = changed_files(record["root"])
    command = command_for("affected", agent, profile)
    if not files or not command:
        return 0
    if unchanged_since_start(record, data):
        return 0
    # t-107 R14: the slot step's wait and a run on a machine end before this hook's own limit (less 30 s); t-079 I2:
    # a run on this Mac is ended by the slot step once it has run PA_SUITE_RUN_LIMIT s after taking the slot
    # t-147 (RL-b): a command that has not entered the slot step (run-all.sh makes the mark just before it) by
    # AFFECTED_RUN s is ended then: it had no slot wait, so it gets the run's own time only
    with tempfile.TemporaryDirectory(prefix="pa-hook-") as marks:
        mark = os.path.join(marks, "slot")
        code, out = run_shell(command, record["root"], files, AFFECTED_LIMIT,
                              {"PA_SUITE_DEADLINE": str(int(time.time()) + AFFECTED_LIMIT - DEADLINE_SPARE),
                               "PA_SUITE_RUN_LIMIT": str(AFFECTED_RUN - DEADLINE_SPARE), "PA_HOOK_SLOT_MARK": mark},
                              mark=mark, first=AFFECTED_RUN)
    if not code:
        return 0
    unfinished = did_not_finish(out) if code == 124 else None
    if code in (124, 125) and not unfinished and could_not_run(out):  # R15, t-117 R7: could not run (no slot, not
        # reachable, refused, the wait's deadline: the slot step's own last line), not a test failure
        message = "The affected tests could not run (task {}): {}. Do not change code for this; report " \
                  "it.\n{}".format(record["task"], not_run_line(out), tail(out, 20))
        if "--stop" in args:  # let the session stop, saying so
            print(message, file=sys.stderr)
            return 1
        block(message)
    if unfinished:  # MF-J2-1: started and did not end: a test may hang; fix it like a failure
        message = "The affected tests did not finish (task {}): {}. A test may hang: fix the code and run them " \
                  "again.\n{}".format(record["task"], unfinished, tail(out))
    else:
        message = "The tests affected by this session's changes fail (task {}). Fix the code and run them again.\n{}".format(record["task"], tail(out))
    if "--stop" in args:
        # Hold the session open a few times, then let it end and say so, rather than loop forever.
        counter = os.path.join(tempfile.gettempdir(), "pa-hook-stop-{}".format(re.sub(r"[^A-Za-z0-9_-]", "", str(data.get("session_id", "none")))))
        count = int(pa.read_text(counter).strip() or 0) + 1
        with open(counter, "w") as f:
            f.write(str(count))
        if count > 3:
            print(message + "\n(Held this session open 3 times already; letting it stop. Report the failure.)", file=sys.stderr)
            return 1
    block(message)


def hook_test_lock(data, _args):
    record = find_task(data.get("cwd") or os.getcwd())
    path = (data.get("tool_input") or {}).get("file_path")
    if not record or not record.get("fix") or not path:
        return 0
    rel = os.path.relpath(os.path.realpath(path), record["root"])
    if TEST_FILE.search(rel):
        block("Test files are locked during a fix task ({}): {} was not changed. Fix the code so the existing "
              "failing test passes; if the test itself is wrong, stop and report that.".format(record["task"], rel))
    return 0


def hook_guard(data, args):
    """The guard fails closed: anything that goes wrong in it refuses the call (block() exits through SystemExit)."""
    try:
        if not isinstance(data, dict) or not data.get("tool_name"):
            # Malformed or empty hook input (hook_input gives {}): nothing can be checked, so nothing runs.
            raise ValueError("no tool call in the hook input")
        return _hook_guard(data, args)
    except Exception as e:  # noqa: BLE001 - card d-038: every failure refuses
        block(GUARD_UNAVAILABLE.format("{}: {}".format(type(e).__name__, e)[:200]))


def guard_folders(data):
    """The folders a tool call may run in, and the task record (REVIEW-3 M5: the hook does not assume that its cwd is
    the Bash tool's folder). Tried in order: the reported cwd, the project folder, the hook's own folder; the first
    task record found wins, and its worktree root is one more folder."""
    candidates = [data.get("cwd"), os.environ.get("CLAUDE_PROJECT_DIR"), os.getcwd()]
    candidates = [c for c in dict.fromkeys(candidates) if c]
    for folder in candidates:
        record, agent, profile = hook_context(dict(data, cwd=folder))
        if record:
            return candidates + [record["root"]], (record, agent, profile)
    return candidates, hook_context(data)


def _hook_guard(data, _args):
    starts, (record, agent, profile) = guard_folders(data)
    tool, tool_input = data.get("tool_name"), data.get("tool_input") or {}
    extra_commands = [(p, "a release tool ({} profile)".format(profile.get("name"))) for p in (profile or {}).get("releaseTools", [])]
    extra_paths = [(p, "a secret ({} profile)".format(profile.get("name"))) for p in (profile or {}).get("secretPaths", [])]
    if tool == "Bash":
        command = tool_input.get("command") or ""
        for pattern, what in DENY_COMMANDS + extra_commands:
            if re.search(pattern, command):
                block("Blocked by the Project Agent safety hook: {}. {}".format(what, ROUTE))
        mains = ["main", "master", "trunk"] + list((profile or {}).get("mainBranches", []))
        if re.search(r"\bgit\s+push\b[^;&|]*[\s:/](" + "|".join(re.escape(b) for b in mains) + r")(\s|$)", command):
            block("Blocked by the Project Agent safety hook: a push to the main branch. " + ROUTE)
        if re.search(r"\bgit\s+push\b", command) and record:
            branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=record["root"], stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, universal_newlines=True).stdout.strip()
            if branch in mains:
                block("Blocked by the Project Agent safety hook: a push from the main branch ({}). {}".format(branch, ROUTE))
        # After the refusals above (unchanged): the denylist.
        refusal = _load_guard().check_command(command)
        if refusal:
            block(refusal)
    else:
        path = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("notebook_path") or ""
        for pattern, what in DENY_PATHS + extra_paths:
            if path and re.search(pattern, path):
                block("Blocked by the Project Agent safety hook: {} ({}). {}".format(what, path, ROUTE))
        if tool in EDIT_TOOLS:
            refusal = _load_guard().check_edit(path, os.path.join(pa.ROOT, "spaces"), cwd=starts, home=pa.ROOT)
            if refusal:
                block(refusal)
    return 0


HOOKS = {"lint-after-edit": hook_lint, "affected-tests": hook_affected, "test-file-lock": hook_test_lock, "guard": hook_guard,
         "session-start": hook_session_start}


# ---- pa-hooks install / remove ------------------------------------------------------------------

def hook_settings():
    run = lambda name: {"type": "command", "command": os.path.join("$HOME", ".local", "bin", "pa-hook") + " " + name}
    return {
        "PreToolUse": [{"matcher": "Edit|Write|MultiEdit", "hooks": [dict(run("test-file-lock"), timeout=10)]},
                       {"matcher": "Bash|Read|Edit|Write|MultiEdit|NotebookEdit", "hooks": [dict(run("guard"), timeout=10)]}],
        "PostToolUse": [{"matcher": "Edit|Write|MultiEdit", "hooks": [dict(run("lint-after-edit"), timeout=150)]}],
        "TaskCompleted": [{"hooks": [dict(run("affected-tests"), timeout=AFFECTED_LIMIT + 30)]}],
        "Stop": [{"hooks": [dict(run("affected-tests --stop"), timeout=AFFECTED_LIMIT + 30)]}],
        "SessionStart": [{"hooks": [dict(run("session-start"), timeout=60)]}],
    }


DENY_RULES = ["Read(.env)", "Read(.env.*)", "Read(**/.env)", "Read(**/.env.*)", "Read(~/.ssh/**)",
              "Bash(git push --force*)", "Bash(git push -f*)", "Bash(git reset --hard*)", "Bash(git clean -f*)"]


def ours(entry):
    return any(MARK in (h.get("command") or "") for h in entry.get("hooks", []))


def cmd_hooks(args):
    root = os.path.realpath(os.path.expanduser(args.worktree))
    if not os.path.isdir(root):
        raise pa.Problem("not a folder: " + root)
    path = os.path.join(root, SETTINGS)
    settings = pa.read_json(path, {}) or {}
    hooks = settings.setdefault("hooks", {})
    for event in list(hooks):
        hooks[event] = [e for e in hooks[event] if not ours(e)]
        if not hooks[event]:
            del hooks[event]
    task_path = os.path.join(root, TASK_FILE)
    if args.action == "remove":
        if not hooks:
            settings.pop("hooks", None)
        pa.backup(path, "pa-hooks")
        pa.write_json(path, settings)
        if os.path.exists(task_path):
            os.unlink(task_path)
        print("Project Agent hooks removed from " + root)
        return
    if not args.task:
        raise pa.Problem("pa-hooks install needs --task <id>")
    space = pa.local_id(args.space or space_from_terminal() or "")
    if not space:
        raise pa.Problem("pass --space <id>, or run this from the Project Agent's terminal")
    load_context(space)
    for event, entries in hook_settings().items():
        hooks.setdefault(event, []).extend(entries)
    deny = settings.setdefault("permissions", {}).setdefault("deny", [])
    deny.extend(r for r in DENY_RULES if r not in deny)
    pa.backup(path, "pa-hooks")
    pa.write_json(path, settings)
    pa.write_json(task_path, {"task": args.task, "space": space, "fix": bool(args.fix), "installed": pa.now()})
    # Keep both files out of commits without touching the repo's tracked ignore file.
    try:
        exclude = subprocess.run(["git", "rev-parse", "--git-path", "info/exclude"], cwd=root, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, universal_newlines=True).stdout.strip()
        if exclude:
            exclude = os.path.join(root, exclude)
            have = pa.read_text(exclude)
            # .playwright-mcp/: page snapshots a browser tool leaves in the folder it runs in.
            missing = [p for p in (SETTINGS, TASK_FILE, ".playwright-mcp/") if p not in have]
            if missing:
                os.makedirs(os.path.dirname(exclude), exist_ok=True)
                with open(exclude, "a") as f:
                    f.write(("" if have.endswith("\n") or not have else "\n") + "\n".join(missing) + "\n")
    except OSError:
        pass
    print("Project Agent hooks installed in {} for task {}{}.".format(root, args.task, " (fix task: test files locked)" if args.fix else ""))


# ---- pa-gate ----------------------------------------------------------------------------------

def applies(rule, task, phase):
    target = str(rule.get("appliesTo") or "all").lower()
    return target in ("all", phase, str(task.get("type", "")).lower(), str(task.get("tier", "")).lower() + " tier")


def gate_commits(worktree, runner=None):
    """The pin: the commit(s) a gate run saw in a task's worktree. One entry when the folder is a git top level,
    one for each src/<name> child that is (a folder of packages), else []. Never raises; records, never fails.
    runner: how to run git, with git()'s signature and result (default: git(), the module's own)."""
    git = globals()["git"] if runner is None else runner
    if not worktree or not os.path.isdir(worktree):
        return []
    root = os.path.realpath(worktree)

    def where(path):
        """(top, common, branch) as the pre-t-094 calls read them, or None: the fallback of entry's first call."""
        code, top, _ = git(path, "rev-parse", "--show-toplevel")
        if code or not top or os.path.realpath(top) != path:
            return None
        code, common, _ = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if code or not common:  # git older than 2.31: no --path-format
            code, common, _ = git(path, "rev-parse", "--git-common-dir")
            common = os.path.join(path, common) if not code and common else None
        _c, branch, _ = git(path, "symbolic-ref", "--short", "-q", "HEAD")
        return top, common, branch or None

    def entry(path):
        # Two calls (t-094 R1, J1-D1). 1: the top level, the absolute common dir and HEAD's short name;
        # --abbrev-ref=loose shortens as symbolic-ref --short does (heads/x when a tag x exists), and HEAD means
        # detached. It fails on an unborn HEAD, a HEAD naming a missing ref, or outside a repository: then the
        # calls as before t-094 (where()).
        code, out, _ = git(path, "rev-parse", "--show-toplevel", "--path-format=absolute", "--git-common-dir",
                           "--abbrev-ref=loose", "HEAD")
        lines = out.splitlines() if not code else []
        if len(lines) == 3 and os.path.isabs(lines[1]):
            top, common, short = lines
            if not top or os.path.realpath(top) != path:
                return None
            branch = None if short == "HEAD" else short or None
        else:
            found = where(path)
            if not found:
                return None
            top, common, branch = found
        # 2: the commit and the tracked changes. --no-optional-locks stays the first argument. --no-ahead-behind
        # (t-108 review N-1): no walk to count against the upstream, which the pin never reads (branch.ab says +? -?).
        argv = ["--no-optional-locks", "status", "--porcelain=v2", "--branch", "--untracked-files=no", "--no-ahead-behind"]
        code, status, err = git(path, *argv)
        if code and "no-ahead-behind" in (err or ""):  # git 2.11-2.16 has no such option (t-139, t-108 RL-1)
            code, status, _ = git(path, *argv[:-1])
        commit, changed, oid = None, 0, None
        for line in status.splitlines() if not code else []:
            if line.startswith("# branch.oid "):
                oid = line[len("# branch.oid "):].strip()
            elif line[:2] in ("1 ", "2 ", "u "):
                # The path the porcelain v1 line starts with (for a rename or copy, the original), tested as v1's
                # "XY path" was: quotes stripped, a leading blank kept (v1 quotes a path with a blank, so the old
                # filter saw '" .claude/x"' and kept the blank; v2 leaves it unquoted; MF-1).
                # v2's fields before the path (git-status(1)): 8 on an ordinary "1" line (XY sub mH mI mW hH hI),
                # 9 on a rename or copy "2" line (... X<score>), 10 on an unmerged "u" line (... m1 m2 m3 mW h1 h2
                # h3); splitting that many times leaves the path, blanks and all, as the last field (t-108 N-2).
                fields = line.split(" ", {"1": 8, "2": 9, "u": 10}[line[0]])
                tested = fields[-1].split("\t", 1)[-1] if line[0] == "2" else fields[-1]
                if not tested.strip('"').startswith(".claude/"):
                    changed += 1
        if oid is None:  # status failed: the commit as read before t-094, no tracked change counted
            changed = 0
            c, commit, _ = git(path, "rev-parse", "--verify", "-q", "HEAD")
            commit = commit if not c and commit else None
        elif oid != "(initial)":
            commit = oid or None
        return {"repo": path, "common": os.path.realpath(common) if common else None, "branch": branch or None,
                "commit": commit, "uncommitted": changed}

    one = entry(root)
    if one:
        return [one]
    src = os.path.join(root, "src")
    if not os.path.isdir(src):
        return []
    found = []
    for name in sorted(os.listdir(src)):
        path = os.path.join(src, name)
        if os.path.isdir(path) and not os.path.islink(path):
            e = entry(path)
            if e:
                found.append(e)
    return found


# ---- t-192 R2: a task with several jobs gates each job on its own check files -------------------------------------
JOB_WORD = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")  # what pa.job_slug makes


def check_files(job=None):
    """(verify, review, gate) file names of a job: VERIFY.md, REVIEW.md, GATE.json for the main job (None or 'main');
    else VERIFY-<job>.md, REVIEW-<job>.md, GATE-<job>.json. No fallback from one to the other (Q2)."""
    if job is None or job == "main":
        return "VERIFY.md", "REVIEW.md", "GATE.json"
    return "VERIFY-{}.md".format(job), "REVIEW-{}.md".format(job), "GATE-{}.json".format(job)


def gate_job(task, job):
    """The recorded worktree path of job `job` of the ledger task (pa-gate --job, pa-queue add --job), None for the
    main job (None or exactly 'main': today's gate). Raises pa.Problem, before any file is read, for a word that is not
    exactly the `job` of one non-removed `worktrees` entry of the task (H2.1-H2.4). A job recorded on the task's own
    worktree (its `worktree` field) is the main job: None."""
    if job is None or job == "main":
        return None
    tid = clean(task.get("id"))
    if not isinstance(job, str) or not JOB_WORD.fullmatch(job):
        raise pa.Problem("{!r} is not a recorded job of {}".format(clean(job), tid))
    entries = [w for w in task.get("worktrees") or [] if isinstance(w, dict) and w.get("job") == job] \
        if isinstance(task.get("worktrees"), list) else []
    usable = [w for w in entries if isinstance(w.get("path"), str) and w["path"].strip()]
    live = [w for w in usable if not w.get("removed")]
    if not usable:
        raise pa.Problem("{!r} is not a recorded job of {}".format(job, tid))
    if len(live) > 1:
        raise pa.Problem("job {} of {} is recorded {} times ({}); name one worktree per job in the ledger".format(
            job, tid, len(live), ", ".join(clean(w["path"]) for w in live)))
    if not live:
        raise pa.Problem("job {} of {} was removed at {}".format(job, tid, clean(usable[-1].get("removed"))))
    main = task.get("worktree")
    if isinstance(main, str) and main.strip() and \
            os.path.realpath(os.path.expanduser(live[0]["path"])) == os.path.realpath(os.path.expanduser(main)):
        return None  # the job of the task's own worktree is its main job: today's files (DECISIONS D-R2-main)
    return live[0]["path"]


def files_job(task, job):
    """The job whose check files a gate, a queue entry or a handoff of `job` reads: None for the main job (check_files
    gives VERIFY.md, REVIEW.md, GATE.json), else job. Raises pa.Problem as gate_job does."""
    return job if gate_job(task, job) else None


def evaluate_gate(directory, task, phase, rules=None, runner=None, records=None, cache=None, job=None):
    """The gate's verdict for one ledger task, as GATE.json holds it. Reads only: writes and prints nothing.
    rules: the space's rules list (default: read from rules.json in directory). runner: how gate_commits and the
    content check run git (default: git()). records: a list the content check appends the content records it
    computed to (cmd_gate stores them; t-052). cache: the run's ContentCache (t-094 R3: pa-queue's command,
    pa-push-main's Session); given none, this call is its own run and makes its own, shared with no other call.
    job (t-192 R2): a recorded job other than main: its worktree is pinned and its VERIFY-<job>.md and REVIEW-<job>.md
    are read in place of VERIFY.md and REVIEW.md (raises pa.Problem for a job that is not recorded, gate_job)."""
    tree = gate_job(task, job)
    job = job if tree else None
    verify_name, review_name, _gate_name = check_files(job)
    tier = str(task.get("tier") or "normal").lower()
    code = bool(task.get("worktree")) or str(task.get("type", "")).lower() in ("feature", "bug", "chore", "code")
    # Leaving `review` (the default) needs every earlier phase's artifact too.
    needed = []
    for p in PHASES[:PHASES.index(phase) + 1] if phase == "review" else [phase]:
        for name in ARTIFACTS.get(p, []):
            if name == "spec.md" and tier == "low":
                continue  # low-tier tasks skip design
            needed.append({"VERIFY.md": verify_name, "REVIEW.md": review_name}.get(name, name))
    if code and phase in ("test", "review"):
        needed.append("build.log")
    if rules is None:
        rules = (pa.read_json(os.path.join(directory, "rules.json"), {}) or {}).get("rules", [])
    for rule in rules:
        if rule.get("status") == "active" and applies(rule, task, phase):
            needed.extend(str(x) for x in rule.get("evidence", []) if isinstance(x, str))
    evidence = os.path.join(directory, "evidence", task.get("id"))
    checks, ok = [], True
    for name in dict.fromkeys(needed):
        path = os.path.normpath(os.path.join(evidence, name))
        if not path.startswith(evidence + os.sep):
            checks.append({"check": name, "ok": False, "why": "outside the evidence folder"})
        elif job and name in (verify_name, review_name) and os.path.lexists(path) and \
                not os.path.realpath(path).startswith(os.path.realpath(evidence) + os.sep):  # H2.6: a link out
            checks.append({"check": name, "ok": False, "why": "outside the evidence folder"})
        elif not os.path.isfile(path):
            checks.append({"check": name, "ok": False, "why": "missing"})
        elif not pa.read_text(path).strip():
            checks.append({"check": name, "ok": False, "why": "empty"})
        else:
            checks.append({"check": name, "ok": True})
    if phase == "review":
        # Only a worker with an address is a reviewing worker or an author; roles are compared in lower case (card d-077).
        workers = [w for w in task.get("workers") or [] if isinstance(w, dict) and isinstance(w.get("address"), str) and w["address"].strip()]
        role = lambda w: str(w.get("role") or "").lower()
        authors = {w["address"] for w in workers if role(w) in ("build", "design")}
        # A `check` worker (verifier and reviewer in one) counts as the reviewer for low tier only (t-017 R4,
        # AC4.3); at any tier, a reviewer or check worker that is also an author fails.
        counted = ("reviewer", "check") if tier == "low" else ("reviewer",)
        reviewers = {w["address"] for w in workers if role(w) in counted}
        both = [w for w in workers if role(w) in ("reviewer", "check") and w["address"] in authors]
        separate = bool(reviewers) and not both
        if both:
            why = "{} is recorded as {} and as a build or design worker".format(both[0].get("address"), both[0].get("role"))
        elif tier == "low":
            why = "the ledger lists no reviewer or check worker separate from the build workers"
        else:
            why = "the ledger lists no reviewer worker separate from the build workers (a check worker counts for low tier only)"
        checks.append({"check": "reviewer is not the author", "ok": separate, **({} if separate else {"why": why})})
    pins = gate_commits(tree if job else task.get("worktree"), runner)
    content = []
    if phase == "review" and pins:
        check, content = content_check(directory, task, pins, runner, records, ContentCache() if cache is None else cache, job=job)
        checks.append(check)
    ok = all(c["ok"] for c in checks)
    result = {"task": task.get("id"), "phase": phase, "tier": tier, "passed": ok, "at": pa.now(), "checks": checks,
              "commits": pins, "content": content}
    if job:
        result["job"] = job
    return result


# ---- t-052: what a check saw, as content ------------------------------------------------------------

CONTENT_CHECK = "the checks saw this content"
CHECKED_LINE = re.compile(r"^Checked: ([0-9a-f]{40}|[0-9a-f]{64})[ \t]*$", re.M)
CONTENT_FILES_SHOWN = 10  # files named in a failing content check, per repository
CHECKER_ROLES = ("verifier", "check")
# Every content-path git call: refs/replace/* never changes what a commit holds (round 2), and a commit-graph never
# says what its parents are (round 3): parents come from the commit objects themselves.
CONTENT_GIT = ("--no-replace-objects", "-c", "core.commitGraph=false")
# Files git reads parentage from that no option turns off for both runners (gate_git drops GIT_*, so GIT_GRAFT_FILE
# cannot reach git there): the content path refuses while one exists (t-052 J1 round 3, verify MF-R2-1).
STEERING_FILES = (("info/grafts", "a grafts file"), ("shallow", "a shallow file"))
RAW_DIFF = ("diff", "--raw", "-z", "--no-renames", "--no-abbrev", "--no-ext-diff", "--no-textconv", "--ignore-submodules=none")
# t-104 R1: every diff of one record in one call. Each stdin line `<commit> <parent>` gives `<commit>\0` and then the
# same `:meta\0path\0` pairs as RAW_DIFF of parent..commit; --always prints that header for an empty diff too, so
# every line can be framed. The parent is always named (from the commit object, R6), never found by git.
DIFF_TREE = ("diff-tree", "--stdin", "--always", "-r", "-z", "--raw", "--no-renames", "--no-abbrev", "--no-ext-diff",
             "--no-textconv", "--ignore-submodules=none")
PARENTS_CHANGED = ("the commit objects between {} and {} are not the chain git listed; something changed what git says "
                   "a commit's parents are during a call (a grafts or shallow file); the content cannot be compared")


class ContentError(Exception):
    pass


def content_dir(directory):
    return os.path.join(directory, "content")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def checked_commits(text):
    """The ids on `Checked: <full id>` lines, in file order: exact lines only (no short ids, no Markdown)."""
    return CHECKED_LINE.findall(text or "")


def as_text(data, errors=None):
    """bytes as both runners read a whole answer today (t-104 R2): decoded as a text-mode pipe decodes (subprocess
    universal_newlines: the pipe's encoding and errors, then \\r\\n and \\r to \\n), then stripped. Each piece of a
    batch answer goes through it on its own, so its text, and any decode error, is that of the one call it replaces."""
    pipe = io.TextIOWrapper(io.BytesIO(), errors=errors)
    return data.decode(pipe.encoding, pipe.errors).replace("\r\n", "\n").replace("\r", "\n").strip()


def raw_files(run, path, a, b, label=None):
    """[{path, status, baseMode, tipMode, baseBlob, tipBlob}] of a..b, sorted by path. NUL-separated, so any path is exact.
    label: the name a failure gives a (default a)."""
    code, out, err = run(path, *(RAW_DIFF + (a, b)))
    if code:
        raise ContentError("git diff {}..{} failed ({})".format((label or a)[:7], b[:7], clean(err)[-200:]))
    return parse_raw(out)


def parse_raw(out):
    """raw_files of one `diff --raw -z` answer (text)."""
    parts, files, i = out.split("\0"), [], 0
    while i < len(parts):
        meta = parts[i].strip("\n")
        if not meta:
            i += 1
            continue
        fields = meta.lstrip(":").split()
        if not meta.startswith(":") or len(fields) != 5 or i + 1 >= len(parts):
            raise ContentError("git diff gave a line it cannot read")
        files.append({"path": parts[i + 1], "status": fields[4], "baseMode": fields[0], "tipMode": fields[1],
                      "baseBlob": fields[2], "tipBlob": fields[3]})
        i += 2
    return sorted(files, key=lambda f: f["path"])


def commit_message(run, path, sha):
    """The commit's message as stored (git cat-file: no program runs, no signature is checked)."""
    return commit_object(run, path, sha)[1]


def commit_object(run, path, sha):
    """([parent ids], message) of the commit as stored, from one cat-file commit."""
    code, out, err = run(path, "cat-file", "commit", sha)
    if code:
        raise ContentError("cannot read commit {} ({})".format(sha[:7], clean(err)[-200:]))
    head, sep, message = out.partition("\n\n")
    return [line[7:] for line in head.split("\n") if line.startswith("parent ")], message if sep else ""


def batch_objects(run, path, ids):
    """[commit object bytes] of ids, in order, from one cat-file --batch (t-104 R1), or None when the call fails or
    its answer is not exactly one `<id> commit <size>` object per id (a missing object, another type, a short read,
    trailing bytes): the caller then asks today's per-call way (R3)."""
    try:
        data = "".join(i + "\n" for i in ids).encode("ascii")
    except UnicodeEncodeError:
        return None
    # One GIT_TIMEOUT per id (t-139, t-104 review RL-1): the per-call path gives each of its calls one.
    code, out, _err = run(path, "cat-file", "--batch", input=data, timeout=GIT_TIMEOUT * max(1, len(ids)))
    if code or not isinstance(out, bytes):
        return None
    objects, at = [], 0
    for sha in ids:
        end = out.find(b"\n", at)
        head = out[at:end].split(b" ") if end >= 0 else []
        if len(head) != 3 or head[0] != sha.encode("ascii") or head[1] != b"commit" or not head[2].isdigit():
            return None
        start = end + 1
        stop = start + int(head[2])
        if out[stop:stop + 1] != b"\n":
            return None
        objects.append(out[start:stop])
        at = stop + 1
    return objects if at == len(out) else None


def batch_diffs(run, path, pairs):
    """[raw diff bytes, as RAW_DIFF prints them] of each (commit, parent) pair, in order, from one DIFF_TREE call
    (t-104 R1), or None when the call fails or its answer cannot be framed exactly (R3). The answer is read in
    tokens: a header must be the next pair's commit id, and after it come `:meta` `path` pairs while the next token
    starts with `:`; a path is always a pair's second token, so no path can move the frame."""
    try:
        data = "".join(a + " " + b + "\n" for a, b in pairs).encode("ascii")
    except UnicodeEncodeError:
        return None
    code, out, _err = run(path, *DIFF_TREE, input=data, timeout=GIT_TIMEOUT * max(1, len(pairs)))  # as batch_objects
    if code or not isinstance(out, bytes):
        return None
    tokens = out.split(b"\0")
    if tokens[-1] != b"":
        return None
    tokens.pop()
    pieces, i = [], 0
    for sha, _parent in pairs:
        if i >= len(tokens) or tokens[i] != sha.encode("ascii"):
            return None
        i += 1
        start = i
        while i < len(tokens) and tokens[i].startswith(b":"):
            if i + 1 >= len(tokens):
                return None
            i += 2
        pieces.append(b"".join(t + b"\0" for t in tokens[start:i]))
    return pieces if i == len(tokens) else None


def object_header(obj):
    """(tree id or None, [parent ids]) of a commit object's bytes: its header lines, ASCII."""
    lines = obj.split(b"\n\n", 1)[0].split(b"\n")
    tree = lines[0][5:].decode("ascii", "replace") if lines[0].startswith(b"tree ") else None
    return tree, [line[7:].decode("ascii", "replace") for line in lines if line.startswith(b"parent ")]


def is_commit(run, path, sha):
    return run(path, "cat-file", "-e", sha + "^{commit}")[0] == 0


OBJECT_HEX = {"sha1": 40, "sha256": 64}  # the length of a full object id, per object format


def repo_facts(run, path):
    """(common dir, [(absolute path, what)], id length or None): the repository's common directory, the files that
    would change what git says a commit's parents are, as git itself resolves them (`--git-path`: the common
    directory's info/grafts or GIT_GRAFT_FILE, and its shallow file), and the length of its full object ids (t-094
    J2-D1: the cache takes a value as an id only at that length), in one call (t-094 R2). Raises ContentError when
    git cannot say where the files are."""
    names = ()
    for name, _what in STEERING_FILES:
        names += ("--git-path", name)
    code, out, err = run(path, *(CONTENT_GIT + ("rev-parse", "--show-object-format", "--path-format=absolute",
                                                "--git-common-dir") + names))
    lines = out.split("\n") if not code and out else []
    hexlen = OBJECT_HEX.get(lines[0]) if lines else None
    lines = lines[1:]
    for i, (_name, what) in enumerate(STEERING_FILES):
        if len(lines) != 1 + len(STEERING_FILES) or not os.path.isabs(lines[0]) or not os.path.isabs(lines[1 + i]):
            raise ContentError("cannot tell where {} would be ({})".format(what, clean(err)[-200:] or "no answer"))
    return lines[0], [(lines[1 + i], what) for i, (_name, what) in enumerate(STEERING_FILES)], hexlen


def repo_paths(run, path):
    """(common dir, [(absolute path, what)]) of repo_facts."""
    return repo_facts(run, path)[:2]


def steering_files(run, path):
    """[(absolute path, what)] of the files that would change what git says a commit's parents are (repo_paths)."""
    return repo_paths(run, path)[1]


def steering_refusal(path, name, what):
    why = "the repository has {} ({}), which changes what git says a commit's parents are; the content cannot be " \
          "compared".format(what, clean(name))
    if what == "a shallow file":  # t-094 R9 (t-057 item 1): the fix; the grafts file has none to name
        why += " (fix: git fetch --unshallow in {})".format(clean(path))
    return why


class ContentCache:
    """One run's answers to object-addressed content calls (t-094 R3): {(common dir, argv): (code, out, err)} as the
    runner gave them. A run is one pa-gate, one pa-queue command, one pa-push-main plan, check or run (its Session),
    or one evaluate_gate call given none. It belongs to the run, is never module-global and never written to disk.
    Objects never change, so an answer stays true across a rebase or a fast-forward of the same run; nothing that
    reads a ref, HEAD, the index or the worktree is ever stored (cacheable). A hit replays the stored answer exactly,
    its stderr included (t-104 D4). close() ends the run: a closed cache is never looked up or filled again, so a
    runner built on it and kept past its run (Ctx.run) asks git each time (t-104 R11)."""

    def __init__(self):
        self.answers = {}
        self.hits = 0
        self.closed = False

    def clear(self):
        self.answers.clear()
        self.hits = 0

    def close(self):
        self.clear()
        self.closed = True


class StdinArgv(tuple):
    """A cache key's argv for a call given stdin (t-104 R4): the argv, then the input (ASCII text) as its last word,
    so the key holds both and cacheable reads the input back from it. Only content_git makes one. It equals only a
    StdinArgv with the same words (t-139, t-104 review N-1): a key with input never matches a plain argv key."""

    def __eq__(self, other):
        return type(other) is StdinArgv and tuple.__eq__(self, other)

    def __ne__(self, other):
        return not self == other

    def __hash__(self):
        return hash((StdinArgv, tuple(self)))


def cacheable(args, hexlen, input=None):
    """The exit codes under which the answer of content call args (CONTENT_GIT not included) may be stored, or None
    for a call that is never stored (t-094 R3). Only these exact shapes, every value a full object id of the
    repository's own length hexlen (J2-D1: in a SHA-1 repository 64 hex is a ref name to git, and in a SHA-256 one
    40 hex is an abbreviation, which git tries as a ref first; an unknown format caches nothing):
    cat-file commit <id>; cat-file -e <id>^{commit}; RAW_DIFF <id>[^] <id>; rev-list [--reverse] [--parents]
    [--count] <id>..<id>; rev-parse --verify -q <id>^{tree}; merge-base <id> <id>; merge-base --is-ancestor <id>
    <id> (exit 0 or 1). Everything else, a ref, HEAD, a short id, status, config, a write, is never stored.
    input (t-104 R4): the bytes given on stdin. Only two shapes take it, and they require it: cat-file --batch with
    lines `<id>`, and DIFF_TREE with lines `<id> <id>`; one space between ids, `\\n` after every line, nothing else,
    at least one line. Every other shape is never stored with input."""
    if isinstance(args, StdinArgv):
        args, input = tuple(args[:-1]), args[-1].encode("ascii")
    if hexlen not in OBJECT_HEX.values():
        return None
    args = tuple(args)

    def oid(v):
        return isinstance(v, str) and len(v) == hexlen and all(c in "0123456789abcdef" for c in v)
    if input is not None:
        if not isinstance(input, bytes) or not input.endswith(b"\n"):
            return None
        try:
            lines = input.decode("ascii")[:-1].split("\n")
        except UnicodeDecodeError:
            return None
        if args == ("cat-file", "--batch"):
            return {0} if all(oid(line) for line in lines) else None
        if args == DIFF_TREE:
            return {0} if all(len(ids) == 2 and oid(ids[0]) and oid(ids[1]) for ids in (line.split(" ") for line in lines)) else None
        return None
    n = len(RAW_DIFF)
    if len(args) == 3 and args[:2] == ("cat-file", "commit") and oid(args[2]):
        return {0}
    if len(args) == 3 and args[:2] == ("cat-file", "-e") and args[2].endswith("^{commit}") and oid(args[2][:-9]):
        return {0}
    if len(args) == n + 2 and args[:n] == RAW_DIFF and (oid(args[n]) or (args[n].endswith("^") and oid(args[n][:-1]))) \
            and oid(args[n + 1]):
        return {0}
    if len(args) >= 2 and args[0] == "rev-list":
        opts, value = args[1:-1], args[-1]
        a, sep, b = value.partition("..")
        if set(opts) <= {"--reverse", "--parents", "--count"} and len(set(opts)) == len(opts) and sep and oid(a) and oid(b):
            return {0}
        return None
    if len(args) == 4 and args[:3] == ("rev-parse", "--verify", "-q") and args[3].endswith("^{tree}") and oid(args[3][:-7]):
        return {0}
    if len(args) == 3 and args[0] == "merge-base" and oid(args[1]) and oid(args[2]):
        return {0}
    if len(args) == 4 and args[:2] == ("merge-base", "--is-ancestor") and oid(args[2]) and oid(args[3]):
        return {0, 1}
    return None


def content_git(run, cache=None):
    """run for the content path (t-052 J1 rounds 2 and 3, verify MF-1 and MF-R2-1): CONTENT_GIT before every
    subcommand, and ContentError instead of any call while a grafts or shallow file exists, checked before and
    after each call (t-094 R10: a file made during the call discards its answer). gate_git drops GIT_*, so options
    and this check, not variables, are what hold in pa-push-main too. call.common(path): the common directory, from
    the same one paths call per folder (t-094 R2). cache: the run's ContentCache (t-094 R3), looked up and filled
    here, in this order: before-check, lookup (a hit returns), git, after-check, store; so the steering check runs
    on every call, hits included, and only an answer that passed both checks is stored."""
    known = {}

    def check(path, files):
        for name, what in files:
            if os.path.lexists(name):
                raise ContentError(steering_refusal(path, name, what))

    def paths(path):
        if path not in known:
            common, files, hexlen = repo_facts(run, path)
            check(path, files)  # after the paths call itself (R10)
            known[path] = (common, files, hexlen)
        return known[path]

    def call(path, *args, input=None, **kw):
        common, files, hexlen = paths(path)
        check(path, files)
        codes = cacheable(args, hexlen, input) if cache is not None and not getattr(cache, "closed", False) else None
        key = None
        if codes:
            argv = tuple(args) if input is None else StdinArgv(tuple(args) + (input.decode("ascii"),))
            key = (os.path.realpath(common), argv)
        if key is not None and key in cache.answers:
            cache.hits += 1
            return cache.answers[key]
        if input is not None:
            kw["input"] = input
        answer = run(path, *(CONTENT_GIT + args), **kw)
        check(path, files)
        if key is not None and answer[0] in codes:
            cache.answers[key] = answer
        return answer
    def keep(path, args, answer):
        """Store answer as the answer of args, as call would have stored it (t-104 D7: the per-call answers a batch
        already holds, so a later per-call record of the same run asks git nothing). Only an R3 shape, exit 0, and
        only after the steering check passes now."""
        common, files, hexlen = paths(path)
        check(path, files)
        codes = cacheable(args, hexlen) if cache is not None and not getattr(cache, "closed", False) else None
        if codes and answer[0] in codes:
            cache.answers.setdefault((os.path.realpath(common), tuple(args)), answer)
    call.common = lambda path: paths(path)[0]
    call.keep = keep
    call.takes_input = bool(getattr(run, "takes_input", False))  # t-104: a batch only where the runner takes stdin
    return call


def common_dir(run, path):
    """(code, common dir, err) of the content path's runner: content_git's one paths answer, else a call."""
    if callable(getattr(run, "common", None)):
        return 0, run.common(path), ""
    return run(path, "rev-parse", "--path-format=absolute", "--git-common-dir")


def content_main(run, path, profile):
    """The branch a repository's change sits on until the queue says otherwise (J2): main, master, trunk, then the
    profile's mainBranches; the first that exists. None if none does. origin/HEAD is not read: the spec names main
    (3.3), and a symbolic ref the repository sets must not pick the base (t-052 J1 round 2, review L2)."""
    names = ["main", "master", "trunk"] + [b for b in (profile or {}).get("mainBranches") or [] if isinstance(b, str)]
    for name in dict.fromkeys(names):
        if name and run(path, "rev-parse", "--verify", "-q", "refs/heads/" + name + "^{commit}")[0] == 0:
            return name
    return None


def content_record(run, path, tip, onto, base=None):
    """The content record of tip against its base on onto (spec 3.2), or {"error": why}. Never raises.
    base: the base the record was made on (a stored record's), checked to be an ancestor of tip; default the merge
    base of tip and onto now (t-052 J1 round 2)."""
    try:
        if not onto:
            raise ContentError("no base branch (none of main, master, trunk or the profile's main branches)")
        if base:
            if run(path, "merge-base", "--is-ancestor", base, tip)[0] != 0:
                raise ContentError("the stored base {} is not an ancestor of {}".format(clean(base)[:7], tip[:7]))
        else:
            code, base, err = run(path, "merge-base", tip, "refs/heads/" + onto)
            if code or not base:
                raise ContentError("no merge base of {} and {}".format(tip[:7], onto))
        # One rev-list (t-094 R4): each line is a commit and its parents, oldest first; more than one parent is a
        # merge, and the newest merge (the last such line) is named. Without merges the first ids are the commits.
        code, lines, err = run(path, "rev-list", "--reverse", "--parents", base + ".." + tip)
        if code:
            raise ContentError("git rev-list failed ({})".format(clean(err)[-200:]))
        rows = [line.split() for line in lines.splitlines() if line.split()]
        merges = [row[0] for row in rows if len(row) > 2]
        if merges:
            raise ContentError("the branch has a merge commit ({}); the content check takes linear branches".format(merges[-1][:7]))
        listed = [row[0] for row in rows]
        # t-104 R6: the chain is checked against the commit objects themselves (their own parent lines), which no
        # grafts or shallow file changes: c1's parent is base, each next one's the one before, the last is tip.
        changed = ContentError(PARENTS_CHANGED.format(clean(base)[:7], tip[:7]))
        if (listed[-1] if listed else base) != tip:
            raise changed
        batched = content_batched(run, path, tip, base, listed, changed) if getattr(run, "takes_input", False) else None
        if batched is not None:
            tree, files, commits = batched
        else:  # today's calls, one per answer (t-104 R3), the parents checked the same way
            code, tree, err = run(path, "rev-parse", "--verify", "-q", tip + "^{tree}")
            files = raw_files(run, path, base, tip)
            commits, parent = [], base
            for sha in listed:
                parents, message = commit_object(run, path, sha)
                if parents != [parent]:
                    raise changed
                commits.append({"sha": sha, "raw": digest(raw_files(run, path, parent, sha, label=sha)),
                                "message": hashlib.sha256(message.encode("utf-8")).hexdigest()})
                parent = sha
        _c, common, _e = common_dir(run, path)
        return {"repo": os.path.realpath(path), "common": os.path.realpath(common) if common else None, "commit": tip,
                "tree": tree or None, "onto": onto, "base": base, "files": files, "commits": commits,
                "change": digest(files), "sequence": digest([[c["raw"], c["message"]] for c in commits]),
                "at": pa.now(), "by": "pa-gate"}
    except ContentError as e:
        return {"error": str(e)}
    except Exception as e:  # a decode error, an unexpected answer: a record error, never a crash of the gate
        return {"error": "could not compute the content of {} ({}: {})".format(tip[:7], type(e).__name__, clean(e)[:200])}


def content_batched(run, path, tip, base, listed, changed):
    """(tree, files, commits) of content_record from two batch calls (t-104 R1): one cat-file --batch for the commit
    objects (tip's alone when none is listed), then one DIFF_TREE for the whole change and each commit against the
    parent its object names. None when either call fails or cannot be framed (R3: the caller asks per call). Raises
    changed when the objects are not rev-list's chain (R6). Each piece is decoded on its own, in today's order (the
    whole change, then each commit's message and diff), so an error is today's (R2)."""
    objects = batch_objects(run, path, listed or [tip])
    if objects is None:
        return None
    heads = [object_header(obj) for obj in objects]
    tree = heads[-1][0]
    if tree is None:
        return None
    parent = base
    for sha, (_tree, parents) in zip(listed, heads):
        if parents != [parent]:
            raise changed
        parent = sha
    pieces = batch_diffs(run, path, [(tip, base)] + [(sha, heads[i][1][0]) for i, sha in enumerate(listed)])
    if pieces is None:
        return None
    whole = as_text(pieces[0])
    files = parse_raw(whole)
    commits, answers = [], [(("rev-parse", "--verify", "-q", tip + "^{tree}"), tree), (RAW_DIFF + (base, tip), whole)]
    for i, (sha, obj, piece) in enumerate(zip(listed, objects, pieces[1:])):
        text = as_text(obj)
        _head, sep, message = text.partition("\n\n")
        message = message if sep else ""
        raw = as_text(piece)
        commits.append({"sha": sha, "raw": digest(parse_raw(raw)),
                        "message": hashlib.sha256(message.encode("utf-8")).hexdigest()})
        answers += [(("cat-file", "commit", sha), text), (RAW_DIFF + (heads[i][1][0], sha), raw)]
    if callable(getattr(run, "keep", None)):
        for args, out in answers:
            run.keep(path, args, (0, out, ""))
    return tree, files, commits


def content_required(directory, task):
    """True when the task's newest verifier or check worker in the ledger started at or after content/since.json:
    it was started under the worker.md that asks for a Checked: line. False for a task in flight (t-052 AC5.2)."""
    since = pa.read_json_safe(os.path.join(content_dir(directory), "since.json"))
    stamp = pa.parse_time(str(since.get("at"))) if isinstance(since, dict) and since.get("at") else None
    if stamp is None:
        return False
    starts = [pa.parse_time(str(w.get("started"))) for w in task.get("workers") or []
              if isinstance(w, dict) and str(w.get("role") or "").lower() in CHECKER_ROLES and w.get("started")]
    starts = [t for t in starts if t is not None]
    return bool(starts) and max(starts) >= stamp


def first_checked(records):
    """Of one commit's stored records with a change, oldest first: the first stored while it was VERIFY.md's checked
    commit (source verify), else the first; None for none. The base the check saw (t-052 J2 round 2, MF-J2-1)."""
    for r in records:
        if r.get("source") == "verify":
            return r
    return records[0] if records else None


def stored_records(directory, task_id):
    data = pa.read_json_safe(os.path.join(content_dir(directory), "{}.json".format(task_id)))
    records = data.get("records") if isinstance(data, dict) else None
    return [r for r in records if isinstance(r, dict)] if isinstance(records, list) else []


def compare_records(checked, now):
    """'' when both digests are equal, else why: the files whose entry differs and what happened to the commits."""
    if checked.get("change") == now.get("change") and checked.get("sequence") == now.get("sequence"):
        return ""
    parts = []
    if checked.get("change") != now.get("change"):
        a = {f.get("path"): f for f in checked.get("files") or [] if isinstance(f, dict)}
        b = {f.get("path"): f for f in now.get("files") or [] if isinstance(f, dict)}
        paths = sorted(p for p in set(a) | set(b) if a.get(p) != b.get(p) and isinstance(p, str))
        shown = ", ".join(clean(p) for p in paths[:CONTENT_FILES_SHOWN])
        more = " and {} more".format(len(paths) - CONTENT_FILES_SHOWN) if len(paths) > CONTENT_FILES_SHOWN else ""
        parts.append("{}{} {}".format(shown, more, "differs" if len(paths) == 1 else "differ") if paths else "the change differs")
    n, m = len(checked.get("commits") or []), len(now.get("commits") or [])
    if n != m:
        parts.append("{} commit{} checked, {} now".format(n, "" if n == 1 else "s", m))
    elif checked.get("sequence") != now.get("sequence"):
        parts.append("commits changed: reworded, reordered or split differently after the check")
    return "; ".join(parts)


def content_check(directory, task, pins, runner=None, records=None, cache=None, job=None):
    """(check, GATE.json content) for the review gate: the commit on VERIFY.md's last Checked: line makes the same
    change as HEAD, per pin (t-052 R5). records: computed records are appended here (with source verify or head).
    job (t-192 R2): a recorded job's VERIFY-<job>.md and REVIEW-<job>.md instead (check_files)."""
    raw = globals()["git"] if runner is None else runner
    run = content_git(raw, cache)
    evidence = os.path.join(directory, "evidence", str(task.get("id")))
    vname, rname, _g = check_files(job)
    verify = checked_commits(pa.read_text(os.path.join(evidence, vname)))
    review = checked_commits(pa.read_text(os.path.join(evidence, rname)))
    required = content_required(directory, task)
    stored = stored_records(directory, task.get("id"))
    try:
        agent = pa.read_json_safe(os.path.join(directory, "agent.json")) or {}
        profile = pa.load_profile(agent.get("profile")) if agent.get("profile") else None
    except (pa.Problem, OSError, ValueError):
        profile = None
    keep = records if records is not None else []
    fails, notes, summary = [], [], []
    for pin in pins:
        path, head, common = pin["repo"], pin.get("commit"), pin.get("common")
        label = (os.path.basename(path) + ": ") if len(pins) > 1 else ""
        entry = {"repo": path, "common": common, "onto": None, "base": None, "change": None, "sequence": None,
                 "checked": None, "review": None, "same": None}
        summary.append(entry)
        try:
            mine = [r for r in stored if r.get("common") == common and not r.get("error")]
            known = {r.get("commit") for r in mine}

            def made(sha):
                """The stored records of sha with a change (a base below it), oldest first."""
                return [r for r in mine if r.get("commit") == sha and isinstance(r.get("base"), str) and r["base"] and r["base"] != sha]

            def made_on(sha):
                """The first stored record of sha with a change: the base it was made on, or None."""
                found = made(sha)
                return found[0] if found else None

            def checked_on(sha):
                """The record of the checked sha: the first made while VERIFY.md named it (source verify), else the
                first (MF-J2-1: a gate run before an R8 check may store the tip against main first)."""
                return first_checked(made(sha))

            def last(ids):
                found = [x for x in ids if x in known or is_commit(run, path, x)]
                return found[-1] if found else None

            x, rv = last(verify), last(review)
            onto = content_onto(directory, task.get("id"), common, pin.get("branch"), run, path, x, job) or content_main(run, path, profile)
            entry.update(onto=onto, checked=x, review=rv)
            now = content_record(run, path, head, onto) if head else {"error": "no commit in the worktree"}
            if not now.get("error") and now["base"] == head and x != head:
                # HEAD is already on onto (a hand merge, a push card's ff): its change is where it was made, not today's
                # empty one (review M1, verify MF-2). Unknown: it cannot be compared.
                saved_head = made_on(head)
                now = content_record(run, path, head, onto, saved_head["base"]) if saved_head else {"error": (
                    "HEAD {} is already on {} and no stored record says where its change began; the content cannot be "
                    "compared".format(head[:7], onto))}
            if now.get("error"):
                entry["error"] = now["error"]
            else:
                entry.update(base=now["base"], change=now["change"], sequence=now["sequence"])
                if now["base"] != head:  # a record with no change says nothing of where it began: never stored
                    keep.append(dict(now, source="head"))
            if x is None:
                if verify and len(pins) == 1:
                    fails.append("{}{}'s Checked commit {} is not a commit in this repository".format(label, vname, verify[-1][:7]))
                elif required:
                    fails.append("{}{} has no Checked: line for this repository".format(label, vname))
                else:
                    notes.append("{}content not recorded: {} has no Checked: line, and its verifier started before the content check".format(label, vname))
                continue
            if now.get("error"):
                fails.append(label + now["error"])
                continue
            if x == head:
                entry["same"] = True
                if now["base"] != head:
                    keep.append(dict(now, source="verify"))
                notes.append("{}the checked commit is HEAD".format(label))
                continue
            saved = checked_on(x)
            if not is_commit(run, path, x):
                computed = {"error": "not a commit"}
            elif saved:  # computed now against the stored base (spec-amendment 6): never the file alone, never today's onto
                computed = content_record(run, path, x, onto, saved["base"])
                if computed.get("error"):
                    fails.append("{}the checked {}: {}".format(label, x[:7], computed["error"]))
                    continue
            else:
                computed = content_record(run, path, x, onto)
                if not computed.get("error") and computed["base"] == x:
                    fails.append("{}the checked {} is already on {} and no stored record says where its change began; "
                                 "the content cannot be compared".format(label, x[:7], onto))
                    continue
            if not computed.get("error") and saved:
                # Every stored record of X is recomputed against its own base, never trusted alone (D-J1-4).
                again = {saved["base"]: computed}
                differs = None
                for r in made(x):
                    if r["base"] not in again:
                        again[r["base"]] = content_record(run, path, x, onto, r["base"])
                    c = again[r["base"]]
                    if c.get("error") or (c.get("change"), c.get("sequence")) != (r.get("change"), r.get("sequence")):
                        differs = r
                        break
                if differs is not None:
                    entry["same"] = False
                    fails.append("{}the record of the checked {} computed now differs from the record stored at {}".format(
                        label, x[:7], clean(differs.get("at"))))
                    continue
            checked = computed if not computed.get("error") else saved
            if checked is None:
                fails.append("{}the checked {}: {}".format(label, x[:7], computed["error"]))
                continue
            if checked is computed:
                keep.append(dict(computed, source="verify"))
            why = compare_records(checked, now)
            entry["same"] = not why
            if why:
                moved, _mwhy, shared = moved_only(run, path, checked, now)
                if moved:  # the shared-file rule (t-052 J2): change lines only moved, counted only with a short review
                    names = ", ".join(clean(f["path"]) for f in shared[:CONTENT_FILES_SHOWN])
                    entry["moved"] = [f["path"] for f in shared]
                    passed = short_review_passed(run, directory, task, common, path, head, now.get("tree"))
                    if passed:
                        entry["shortReview"] = passed.get("file")
                        notes.append("{}change lines only moved in {}; short review {} passed".format(label, names, clean(passed.get("file"))))
                    else:
                        fails.append("{}change lines only moved in {}: a short review is needed (pa-queue verify names it); "
                                     "HEAD {} against the checked {}{}".format(label, names, head[:7], x[:7],
                                                                             review_hint(directory, task.get("id"))))
                    continue
                union, _uwhy, unions, ushared = union_only(run, raw, path, checked, now)
                if union:  # d-166 (t-052 J3c): a conflict kept on both sides counts only with a doc-only short review
                    names = ", ".join(clean(u["path"]) for u in unions[:CONTENT_FILES_SHOWN])
                    entry["union"] = [u["path"] for u in unions]
                    if ushared:
                        entry["moved"] = [f["path"] for f in ushared]
                    passed = short_review_passed(run, directory, task, common, path, head, now.get("tree"), about="doc-only")
                    if passed:
                        entry["shortReview"] = passed.get("file")
                        notes.append("{}a conflict kept on both sides in {}; doc-only short review {} passed".format(
                            label, names, clean(passed.get("file"))))
                    else:
                        fails.append("{}a conflict kept on both sides in {}: a short review is needed (doc-only; pa-queue "
                                     "verify names it); HEAD {} against the checked {}{}".format(
                                         label, names, head[:7], x[:7], review_hint(directory, task.get("id"))))
                    continue
                fails.append("{}{}; HEAD {} against the checked {}".format(label, why, head[:7], x[:7]))
            else:
                notes.append("{}same content as the checked {}".format(label, x[:7]))
        except ContentError as e:  # content_git refused a call (a grafts or shallow file): never a crash, never ok
            entry["error"] = str(e)
            if not verify and not required:
                notes.append("{}content not recorded: {} has no Checked: line, and its verifier started before the content check".format(label, vname))
            else:
                fails.append(label + str(e))
    check = {"check": CONTENT_CHECK, "ok": not fails}
    if fails:
        check["why"] = "; ".join(fails)
    elif notes:
        check["note"] = "; ".join(notes)
    return check, summary


def review_hint(directory, task_id):
    """t-176 R10: '; <file> is written: pa-queue verify <task> records it' (refresh, for a queue entry) when a short
    review file of the task is in its evidence (the newest), else ''. Text only: the gate never counts a file itself;
    only a reviews.jsonl line pa-queue wrote passes the content check."""
    folder = os.path.join(directory, "evidence", str(task_id))
    pattern = re.compile(r"^SHORT-REVIEW-{}(?:-(?!stacked-)[^/]+)?-\d+\.md$".format(re.escape(str(task_id))))
    try:
        found = [n for n in os.listdir(folder) if pattern.match(n) and os.path.isfile(os.path.join(folder, n))]
    except OSError:
        return ""
    if not found:
        return ""
    name = max(found, key=lambda n: (os.path.getmtime(os.path.join(folder, n)), n))
    data = pa.read_json_safe(os.path.join(directory, "queue.json"))
    repos = data.get("repos") if isinstance(data, dict) and isinstance(data.get("repos"), dict) else {}
    queued = any(isinstance(e, dict) and e.get("task") == task_id for r in repos.values() if isinstance(r, dict)
                 for e in r.get("entries") or [])
    return "; {} is written: {} records it".format(clean(name), "pa-queue refresh" if queued else "pa-queue verify " + clean(task_id))


def content_onto(directory, task_id, common, branch, run=None, path=None, commit=None, job=None):
    """The branch a pin's change sits on as the merge queue says (t-052 J2): the queue entry of this repository whose
    branch is the pin's, its onto; else, for a task planned on another task's branch (pa-queue plan, dependsOn), that
    entry's branch; else, given git (run, path) and the checked commit (VERIFY.md's), the branch of the last entry (of
    another task) whose tip the checked commit sits on and main does not hold: a task refused for a conflict or a
    changed rebase whose R8 check saw it there, joining again with add (spec-amendment R3 step 3, R8; review MF-J2-1).
    The checked commit, not HEAD: a branch taken out of the queue whose check saw it on main stays against main (C14,
    Q21). Else None (the caller falls back to content_main)."""
    data = pa.read_json_safe(os.path.join(directory, "queue.json"))
    repos = data.get("repos") if isinstance(data, dict) else None
    if not isinstance(repos, dict) or not common:
        return None
    for rec in repos.values():
        if not isinstance(rec, dict) or rec.get("common") != common:
            continue
        entries = [e for e in rec.get("entries") or [] if isinstance(e, dict)]
        for i, e in enumerate(entries):
            if branch and e.get("branch") == branch:
                # RL-J2-1 (t-052 J3a): the onto counts only when it is main (first entry) or the entry directly ahead;
                # an onto naming a branch that left the queue (before restack) is not trusted: None, so main.
                ahead = entries[i - 1].get("branch") if i else rec.get("main")
                onto = e.get("onto")
                return onto if isinstance(onto, str) and onto and onto == ahead else None
        for p in rec.get("planned") or []:
            dep = p.get("dependsOn") if isinstance(p, dict) and p.get("task") == task_id else None
            if isinstance(dep, dict):
                ahead = next((e for e in entries if e.get("task") == dep.get("task") and e.get("job") == dep.get("job")), None)
                if ahead and isinstance(ahead.get("branch"), str) and ahead["branch"]:
                    return ahead["branch"]
        if run is not None and path and commit:
            below = entry_below(run, path, rec, entries, task_id, commit, job)
            if below:
                return below
    return None


def entry_below(run, path, rec, entries, task_id, commit, job=None):
    """The branch of the last entry, in queue order, of another task (or another job of this task: Nit-J2-R2-1, t-052
    J3a) whose tip is a proper ancestor of commit and not in main; None on any doubt (main unreadable, a git error):
    the caller then compares against main, as in J1."""
    def tip(name):
        if not isinstance(name, str) or not name:
            return None
        code, out, _e = run(path, "rev-parse", "--verify", "-q", "refs/heads/" + name + "^{commit}")
        return out.strip() if code == 0 and out.strip() else None

    def below(a, b):
        return run(path, "merge-base", "--is-ancestor", a, b)[0] == 0

    try:
        main_tip = tip(rec.get("main"))
        if not main_tip:
            return None
        for e in reversed(entries):
            mine = e.get("task") == task_id and (e.get("job") or None) == (job or None)
            at = tip(e.get("branch")) if not mine else None
            if at and at != commit and below(at, commit) and not below(at, main_tip):
                return e["branch"]
    except Exception:  # a refused call (grafts, shallow) or an unexpected answer: doubt, so main (J1)
        return None
    return None


HUNK = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def diff_hunks(run, path, a, b):
    """The -U0 hunks of blob a -> blob b: [{"old": [start, count], "new": [start, count], "lines": [bytes, ...]}], the
    change lines exact (git writes the patch to a file: the runners strip their output, which would lose trailing
    whitespace on the last line). Raises ContentError for a binary diff or a git failure."""
    folder = tempfile.mkdtemp(prefix="pa-hunks-")
    out = os.path.join(folder, "diff")
    try:
        code, _o, err = run(path, "diff", "-U0", "--no-color", "--no-ext-diff", "--no-textconv", "--diff-algorithm=myers",
                            "--output=" + out, a, b)
        if code:
            raise ContentError("git diff {}..{} failed ({})".format(a[:7], b[:7], clean(err)[-200:]))
        with open(out, "rb") as f:
            data = f.read()
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    hunks = []
    for line in data.split(b"\n"):
        if line.startswith(b"Binary files ") or line.startswith(b"GIT binary patch"):
            raise ContentError("a binary file")
        m = HUNK.match(line)
        if m:
            hunks.append({"old": [int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)],
                          "new": [int(m.group(3)), int(m.group(4) if m.group(4) is not None else 1)], "lines": []})
        elif hunks and line[:1] in (b"-", b"+", b"\\"):
            hunks[-1]["lines"].append(line)
    return hunks


def same_lines(run, path, a, b):
    """True when the change of one file entry a and of b is the same change lines, only moved: both modified (M) with
    the same modes, different bases (the base changed the file: a shared file), and equal -U0 hunks once positions are
    dropped. Any doubt is False."""
    if a.get("status") != "M" or b.get("status") != "M":
        return False
    if (a.get("baseMode"), a.get("tipMode")) != (b.get("baseMode"), b.get("tipMode")) or a.get("baseBlob") == b.get("baseBlob"):
        return False
    one = [h["lines"] for h in diff_hunks(run, path, a["baseBlob"], a["tipBlob"])]
    two = [h["lines"] for h in diff_hunks(run, path, b["baseBlob"], b["tipBlob"])]
    return bool(one) and one == two


def moved_only(run, path, checked, now):
    """(True, "", shared) when now makes checked's change with identical change lines that only moved in files its base
    also changed (the person's shared-file rule, t-052): the same paths; every differing file a shared file whose
    hunks are equal; the same commits (count, messages, paths), each the same or moved in the same sense. shared:
    [{path, oldBase, newBase, tip}]. Else (False, why, []). Never raises; it is never clean alone (a short review)."""
    try:
        a = {f.get("path"): f for f in checked.get("files") or [] if isinstance(f, dict)}
        b = {f.get("path"): f for f in now.get("files") or [] if isinstance(f, dict)}
        if set(a) != set(b):
            return False, "the files touched differ", []
        shared = []
        for p in sorted(x for x in a if isinstance(x, str)):
            if a[p] == b[p]:
                continue
            if not same_lines(run, path, a[p], b[p]):
                return False, "{} changed".format(clean(p)), []
            shared.append({"path": p, "oldBase": a[p]["baseBlob"], "newBase": b[p]["baseBlob"], "tip": b[p]["tipBlob"]})
        if not shared:
            return False, "the commits changed", []
        ca, cb = checked.get("commits") or [], now.get("commits") or []
        if len(ca) != len(cb) or [c.get("message") for c in ca] != [c.get("message") for c in cb]:
            return False, "the commits changed", []
        for x, y in zip(ca, cb):
            if x.get("raw") == y.get("raw"):
                continue
            for sha in (x.get("sha"), y.get("sha")):
                if not isinstance(sha, str) or not is_commit(run, path, sha):
                    return False, "cannot compare the commits (one is not in the repository)", []
            fx = {f["path"]: f for f in raw_files(run, path, x["sha"] + "^", x["sha"])}
            fy = {f["path"]: f for f in raw_files(run, path, y["sha"] + "^", y["sha"])}
            if set(fx) != set(fy):
                return False, "the commits changed", []
            for p in fx:
                if fx[p] != fy[p] and not same_lines(run, path, fx[p], fy[p]):
                    return False, "the commits changed ({})".format(clean(p)), []
        return True, "", shared
    except ContentError as e:
        return False, str(e), []
    except Exception as e:  # an unexpected answer: never moved
        return False, "{}: {}".format(type(e).__name__, clean(e)[:200]), []


REGULAR_MODES = ("100644", "100755")
# git merge-file's diff: its own default (myers), then the one a rebase's ort merge uses (histogram), so a resolution
# made from either conflict shape is found.
UNION_ALGORITHMS = ((), ("--diff-algorithm=histogram",))


def marker_size(*blobs):
    """A conflict marker size no content line can match (t-086, RL-J3c-3): one more than the longest run of <, = or >
    that starts a line in any of the blobs, at least git's 7."""
    longest = 0
    for data in blobs:
        for text in data.split(b"\n"):
            for c in b"<=>":
                run = len(text) - len(text.lstrip(bytes([c])))
                longest = max(longest, run)
    return max(7, longest + 1)


def conflict_regions(data, size=7):
    """[[first, last]] lines (1-based) of each conflict of a merge-file output with markers of `size` characters
    (merge-file --marker-size: marker_size of its blobs, so no content line starts like one), as they lie in the
    --union result of the same merge (both sides' lines, no markers)."""
    regions, line, start, inside = [], 0, None, 0
    opening, separator, closing = b"<" * size, b"=" * size, b">" * size
    for text in data.split(b"\n"):
        if inside == 0 and text.startswith(opening):
            inside, start = 1, line + 1
        elif inside == 1 and text.rstrip(b"\r") == separator:
            inside = 2
        elif inside == 2 and text.startswith(closing):
            inside = 0
            if line >= start:
                regions.append([start, line])
        else:
            line += 1
    return regions


def raw_blobs(raw, common, folder, shas):
    """({key: file name}, {key: bytes}) of each (key, blob sha) written raw (git unpack-file: no filter, no attribute)
    into folder. ContentError when one cannot be read."""
    names, blobs = {}, {}
    for key, sha in shas:
        code, out, err = raw(folder, *(CONTENT_GIT + ("--git-dir=" + common, "unpack-file", sha)))
        name = (out or "").strip()
        if code or not re.match(r"^\.merge_file_[A-Za-z0-9_]+$", name):
            raise ContentError("cannot read blob {} ({})".format(str(sha)[:7], clean(err)[-200:]))
        names[key] = name
        with open(os.path.join(folder, name), "rb") as f:
            blobs[key] = f.read()
    return names, blobs


def union_pair(x, y):
    """True when file entries x (before the rebase) and y (after) may be compared by content: status M on both, the
    same regular modes, another base blob (t-052 J3c, t-086, t-116: one guard for union_of, commit_union, replay_of)."""
    return x.get("status") == "M" and y.get("status") == "M" and x.get("baseBlob") != y.get("baseBlob") and \
        (x.get("baseMode"), x.get("tipMode")) == (y.get("baseMode"), y.get("tipMode")) and \
        x.get("tipMode") in REGULAR_MODES and x.get("baseMode") in REGULAR_MODES


def union_of(raw, common, mine, old, new, tip):
    """(order, regions) when blob tip is exactly `git merge-file --union` of the checked tip `mine` and the new base
    `new` over the old base `old`, in either order ("checked first": mine's lines first; "base first"), and the plain
    merge of the same has at least one conflict; else (None, why). The four blobs are written raw (git unpack-file:
    no filter, no attribute) into a mktemp -d folder and merge-file runs on those files only: no work tree, no
    attributes, no merge driver; nothing is written to the repository (t-052 J3c, V-U2)."""
    folder = tempfile.mkdtemp(prefix="pa-union-")
    try:
        names, blobs = raw_blobs(raw, common, folder, (("mine", mine), ("old", old), ("new", new), ("tip", tip)))
        want = blobs["tip"]
        size = marker_size(*blobs.values())
        for order, (first, second) in (("checked first", ("mine", "new")), ("base first", ("new", "mine"))):
            start = blobs[first]
            for algorithm in UNION_ALGORITHMS:
                merged = {}
                for out_name, union in (("union", ("--union",)), ("markers", ())):
                    with open(os.path.join(folder, out_name), "wb") as f:
                        f.write(start)
                    code, _o, _e = raw(folder, *(("-c", "merge.conflictStyle=merge", "merge-file", "-q",
                                                  "--marker-size={}".format(size)) + algorithm + union +
                                                (out_name, names["old"], names[second])))
                    with open(os.path.join(folder, out_name), "rb") as f:
                        merged[out_name] = (code, f.read())
                (ucode, udata), (mcode, mdata) = merged["union"], merged["markers"]
                # merge-file exits with the number of conflicts (at most 127), 255 on an error
                if ucode == 0 and udata == want and 0 < mcode < 128:
                    regions = conflict_regions(mdata, size)
                    if regions:
                        return order, regions
        return None, "not both sides kept unchanged"
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def commit_union(raw, common, x, y):
    """(order, regions) when file entry y (a commit's) makes x's change (the same commit before the rebase) by keeping
    both sides of a conflict unchanged: union_pair, and union_of finds y's blob (t-086, RL-J3c-1); the regions are lines
    of y's blob (t-116: listed for the short reviewer). Else None."""
    if not union_pair(x, y):
        return None
    order, regions = union_of(raw, common, x["tipBlob"], x["baseBlob"], y["baseBlob"], y["tipBlob"])
    return (order, regions) if order else None


def replay_of(raw, common, x, y):
    """True when file entry y (a commit's) is a clean replay of x (the same commit before the rebase) whose -U0 hunks
    differ (a diff realignment, t-086 finding 2): union_pair, and a plain `git merge-file` of x's blob onto y's parent
    over x's parent exits 0 with an output equal to y's blob byte for byte, with git's own diff or the rebase's
    (UNION_ALGORITHMS). The blobs are written raw into a mktemp -d folder, as union_of does (t-116)."""
    if not union_pair(x, y):
        return False
    folder = tempfile.mkdtemp(prefix="pa-replay-")
    try:
        names, blobs = raw_blobs(raw, common, folder, (("mine", x["tipBlob"]), ("old", x["baseBlob"]),
                                                       ("new", y["baseBlob"]), ("tip", y["tipBlob"])))
        for algorithm in UNION_ALGORITHMS:
            with open(os.path.join(folder, "replay"), "wb") as f:
                f.write(blobs["mine"])
            code, _o, _e = raw(folder, *(("merge-file", "-q") + algorithm + ("replay", names["old"], names["new"])))
            with open(os.path.join(folder, "replay"), "rb") as f:
                merged = f.read()
            if code == 0 and merged == blobs["tip"]:
                return True
        return False
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def union_only(run, raw, path, checked, now):
    """union_check's first four values: (ok, why, unions, shared)."""
    return union_check(run, raw, path, checked, now)[:4]


def union_check(run, raw, path, checked, now):
    """(True, "", unions, shared, commit_unions) when now makes checked's change with a rebase conflict resolved by keeping both
    sides unchanged (d-166, t-052 J3c): the same paths; each differing file either moved only (same_lines: in shared)
    or a union: status M on both sides, the same regular modes, another base, and its tip blob exactly the
    merge-file --union of the checked tip and the new base (union_of); at least one union; the same commits (count,
    messages, paths), each the same, moved, or touching union files differently only as a union of its own
    (commit_union, t-086: every commit is checked, not just the tip) or as a clean replay of its own change (replay_of,
    t-116: a diff realignment). unions: [{path, oldBase, newBase, checkedTip, tip, order, regions}]. commit_unions
    (t-116, t-086 finding 1): [{commit, path, blob, regions, order}] of each commit's union, except one that is the tip's
    own (the same blob and regions as a tip union): a one-commit union gives []. Else (False, why, [], [], []). Never
    raises; never clean alone: a short reviewer judges that every region is only docstrings or comments."""
    try:
        a = {f.get("path"): f for f in checked.get("files") or [] if isinstance(f, dict)}
        b = {f.get("path"): f for f in now.get("files") or [] if isinstance(f, dict)}
        if set(a) != set(b):
            return False, "the files touched differ", [], [], []
        code, common, err = common_dir(run, path)
        if code or not common:
            return False, "cannot find the repository's objects ({})".format(clean(err)[-200:]), [], [], []
        unions, shared = [], []
        for p in sorted(x for x in a if isinstance(x, str)):
            x, y = a[p], b[p]
            if x == y:
                continue
            if same_lines(run, path, x, y):
                shared.append({"path": p, "oldBase": x["baseBlob"], "newBase": y["baseBlob"], "tip": y["tipBlob"]})
                continue
            if not union_pair(x, y):
                return False, "{} changed".format(clean(p)), [], [], []
            order, regions = union_of(raw, common, x["tipBlob"], x["baseBlob"], y["baseBlob"], y["tipBlob"])
            if not order:
                return False, "{} changed ({})".format(clean(p), regions), [], [], []
            unions.append({"path": p, "oldBase": x["baseBlob"], "newBase": y["baseBlob"], "checkedTip": x["tipBlob"],
                           "tip": y["tipBlob"], "order": order, "regions": regions})
        if not unions:
            return False, "no file is a conflict kept on both sides", [], [], []
        union_paths = {u["path"] for u in unions}
        tip_own = {(u["path"], u["tip"], json.dumps(u["regions"])) for u in unions}
        commit_unions = []
        ca, cb = checked.get("commits") or [], now.get("commits") or []
        if len(ca) != len(cb) or [c.get("message") for c in ca] != [c.get("message") for c in cb]:
            return False, "the commits changed", [], [], []
        for x, y in zip(ca, cb):
            if x.get("raw") == y.get("raw"):
                continue
            for sha in (x.get("sha"), y.get("sha")):
                if not isinstance(sha, str) or not is_commit(run, path, sha):
                    return False, "cannot compare the commits (one is not in the repository)", [], [], []
            fx = {f["path"]: f for f in raw_files(run, path, x["sha"] + "^", x["sha"])}
            fy = {f["path"]: f for f in raw_files(run, path, y["sha"] + "^", y["sha"])}
            if set(fx) != set(fy):
                return False, "the commits changed", [], [], []
            for p in sorted(fx):
                if fx[p] == fy[p] or same_lines(run, path, fx[p], fy[p]):
                    continue
                # t-086 (RL-J3c-1): a union file is checked in every commit that changes it otherwise, the same way
                # as at the tip: that commit's blob must be the union of its old change on its new parent, or (t-116,
                # t-086 finding 2) exactly the clean three-way replay of its old change there
                if p not in union_paths:
                    return False, "the commits changed ({})".format(clean(p)), [], [], []
                found = commit_union(raw, common, fx[p], fy[p])
                if found:
                    order, regions = found
                    if (p, fy[p]["tipBlob"], json.dumps(regions)) not in tip_own:
                        commit_unions.append({"commit": y["sha"], "path": p, "blob": fy[p]["tipBlob"],
                                              "regions": regions, "order": order})
                elif not replay_of(raw, common, fx[p], fy[p]):
                    return False, "the commits changed ({})".format(clean(p)), [], [], []
        return True, "", unions, shared, commit_unions
    except ContentError as e:
        return False, str(e), [], [], []
    except Exception as e:  # an unexpected answer: never a union
        return False, "{}: {}".format(type(e).__name__, clean(e)[:200]), [], [], []


def iso_at(ts):
    return datetime.datetime.fromtimestamp(round(ts), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_lines(path):
    out = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    out.append(value)
    except OSError:
        pass
    return out


def suite_runs(directory):
    """Every full suite run of content/suites.jsonl, in file order, in one shape (t-052 J2): {kind, task, worker, repo,
    commit, tree, uncommitted, untracked, moved, reusable, exit, started, ended, seconds, parts}. J1's lines have `at`
    (written at the end) and `seconds`; t-056's (pa_suite.full_suite) add `start`, `end`, `parts` ({name, ok, seconds}),
    `untracked`, and `moved: true` when the worktree changed during the run. reusable is False for a moved run or one
    with untracked files (t-056 never reuses those: pa_suite.clean): such a run is no pass or failure for its tree.
    A J1 line has no untracked count (None) and is not reusable either, as pa_suite.clean decides (t-052 J3a A6,
    D-J2-15 dropped): its tree needs a run of its own. This is the one reader of both shapes."""
    runs = []
    for l in read_lines(os.path.join(content_dir(directory), "suites.jsonl")):
        seconds = l.get("seconds") if isinstance(l.get("seconds"), (int, float)) else None
        ended = l.get("end") if isinstance(l.get("end"), str) else l.get("at")
        started = l.get("start") if isinstance(l.get("start"), str) else None
        end_ts = pa.parse_time(ended) if isinstance(ended, str) else None
        if started is None and end_ts is not None and seconds is not None:
            started = iso_at(end_ts - seconds)
        untracked = l.get("untracked") if isinstance(l.get("untracked"), int) else None
        moved = bool(l.get("moved"))
        runs.append({"kind": l.get("kind") or "suite", "task": l.get("task"), "worker": l.get("worker"), "repo": l.get("repo"),
                     "commit": l.get("commit"), "tree": l.get("tree"), "uncommitted": l.get("uncommitted"),
                     "untracked": untracked, "moved": moved,
                     "reusable": not moved and untracked == 0, "exit": l.get("exit"),
                     "started": started, "ended": ended, "seconds": seconds,
                     "parts": l.get("parts") if isinstance(l.get("parts"), list) else None,
                     # t-089: where it ran (no machine: this Mac), and why a run that could go remote did not
                     "machine": l.get("machine") if isinstance(l.get("machine"), str) else None,
                     "os": l.get("os") if isinstance(l.get("os"), str) else None,
                     "places": l.get("places") if isinstance(l.get("places"), list) else None,
                     "note": l.get("note") if isinstance(l.get("note"), str) else None})
    return runs


def review_runs(directory):
    """Every short review run of content/reviews.jsonl (written by pa-queue: refresh, add, and verify for a branch not
    in the queue, t-176), in file order."""
    return [l for l in read_lines(os.path.join(content_dir(directory), "reviews.jsonl")) if l.get("kind") == "short-review"]


def committer_time(run, path, sha):
    code, out, _err = run(path, "cat-file", "commit", sha)
    if code:
        return None
    for line in out.split("\n\n", 1)[0].splitlines():
        if line.startswith("committer "):
            parts = line.rsplit(" ", 2)
            if len(parts) == 3 and parts[1].isdigit():
                return int(parts[1])
    return None


def short_review_passed(run, directory, task, common, path, head, tree, about=None):
    """The passing short review record for HEAD, or None: one of reviews.jsonl for this task, repository, commit and
    tree, by a ledger reviewer of the task who is not an author, started at or after HEAD was committed. about: the
    question it must have answered ("doc-only" for a conflict kept on both sides, t-052 J3c); None: any."""
    workers = [w for w in task.get("workers") or [] if isinstance(w, dict) and isinstance(w.get("address"), str)]
    authors = {w["address"] for w in workers if str(w.get("role") or "").lower() in ("build", "design")}
    reviewers = {w["address"] for w in workers if str(w.get("role") or "").lower() == "reviewer"} - authors
    committed = committer_time(run, path, head)
    for r in reversed(review_runs(directory)):
        if (r.get("task"), r.get("common"), r.get("tip")) != (task.get("id"), common, head) or not tree or r.get("tree") != tree:
            continue
        if r.get("verdict") != "pass" or r.get("worker") not in reviewers or (about and r.get("about") != about):
            continue
        started = pa.parse_time(str(r.get("started"))) if r.get("started") else None
        if committed is None or started is None or started < committed:
            continue
        return r
    return None


@contextlib.contextmanager
def content_lock(directory):
    import fcntl
    os.makedirs(content_dir(directory), exist_ok=True)
    with open(os.path.join(content_dir(directory), ".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def store_records(directory, task_id, records):
    """Append the gate's content records to content/<task-id>.json, one per (common, commit, base, source)."""
    if not records:
        return
    path = os.path.join(content_dir(directory), "{}.json".format(task_id))
    with content_lock(directory):
        data = pa.read_json_safe(path)
        if not isinstance(data, dict) or not isinstance(data.get("records"), list):
            data = {"version": 1, "records": []}
        seen = {(r.get("common"), r.get("commit"), r.get("base"), r.get("source")) for r in data["records"] if isinstance(r, dict)}
        for r in records:
            key = (r.get("common"), r.get("commit"), r.get("base"), r.get("source"))
            if key not in seen:
                seen.add(key)
                data["records"].append(r)
        pa.write_json(path, data)


def stamp_content(directory):
    """content/since.json: when this space's gate first ran with the content check (t-052 AC5.2). Written once."""
    path = os.path.join(content_dir(directory), "since.json")
    with content_lock(directory):
        if not os.path.exists(path):
            pa.write_json(path, {"version": 1, "at": pa.now()})


def cmd_gate(args):
    record = find_task(os.getcwd())
    space = pa.local_id(args.space or (record or {}).get("space") or space_from_terminal() or "")
    if not space:
        base = os.path.join(pa.ROOT, "spaces")
        owners = [n for n in (os.listdir(base) if os.path.isdir(base) else [])
                  if any(t.get("id") == args.task for t in (pa.read_json(os.path.join(base, n, "ledger.json"), {}) or {}).get("tasks", []))]
        if len(owners) != 1:
            raise pa.Problem("pass --space <id> ({} spaces have a task {})".format(len(owners), args.task))
        space = owners[0]
    directory, _agent, _profile = load_context(space)
    ledger = pa.read_json(os.path.join(directory, "ledger.json"), {}) or {}
    task = next((t for t in ledger.get("tasks", []) if t.get("id") == args.task), None)
    if not task:
        raise pa.Problem("no task {} in {}".format(args.task, os.path.join(directory, "ledger.json")))
    phase = (args.phase or "review").lower()
    if phase not in PHASES:
        raise pa.Problem("unknown phase {!r}; one of {}".format(phase, ", ".join(PHASES)))
    job = getattr(args, "job", None)
    job = job if gate_job(task, job) else None  # t-192 R2: refused here, before any file is read, unless recorded
    stamp_content(directory)
    records = []
    result = evaluate_gate(directory, task, phase, records=records, job=job)
    evidence, checks, ok = os.path.join(directory, "evidence", args.task), result["checks"], result["passed"]
    store_records(directory, args.task, records)
    if os.path.isdir(evidence):
        pa.write_json(os.path.join(evidence, check_files(job)[2]), result)
    for c in checks:
        print("{}  {}{}".format("ok  " if c["ok"] else "FAIL", c["check"],
                                "  ({})".format(c["why"]) if not c["ok"] else "  ({})".format(c["note"]) if c.get("note") else ""))
    print("pa-gate {}{} ({} phase, {} tier): {}".format(args.task, " job " + job if job else "", phase, result["tier"],
                                                       "PASSED" if ok else "FAILED. The task does not move."))
    sys.exit(0 if ok else 1)


# ---- pa-worktree ------------------------------------------------------------------------------

GIT_TIMEOUT = 20  # seconds, for each git call made while checking or describing a worktree


def git(path, *args, timeout=None, input=None):
    """(exit code, stdout, stderr), stripped. A call that runs past the timeout gives code 124.
    input (bytes, t-104): given on stdin; stdout is then the answer's bytes as git wrote them (not decoded, not
    stripped: a batch answer is framed by byte sizes), stderr text as without it."""
    limit = timeout or GIT_TIMEOUT
    if input is not None:
        try:
            r = subprocess.run(["git", "-C", path] + list(args), input=input, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=limit)
        except subprocess.TimeoutExpired:
            return 124, b"", "git {} timed out after {} s".format(args[0] if args else "", limit)
        except OSError as e:
            return 127, b"", str(e)
        return r.returncode, r.stdout, as_text(r.stderr, "replace")
    try:
        r = subprocess.run(["git", "-C", path] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=limit)
    except subprocess.TimeoutExpired:
        return 124, "", "git {} timed out after {} s".format(args[0] if args else "", limit)
    except OSError as e:
        return 127, "", str(e)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


git.takes_input = True


def worktree_facts(path, profile=None):
    """What deleting this folder would cost. None if it is not a worktree we know how to remove:
    a linked git worktree, or the profile's own kind (a marker file plus status and remove scripts).
    {"unreadable": <why>} when it is one but its state cannot be read (git status or the profile's
    status script failed): never to be taken for gone or clean."""
    if not os.path.isdir(path):
        return None
    custom = (profile or {}).get("worktree") or {}
    if custom.get("marker") and custom.get("status") and custom.get("remove") and os.path.exists(os.path.join(path, custom["marker"])):
        script = lambda name: custom[name] if os.path.isabs(custom[name]) else os.path.join(profile["_folder"], custom[name])
        r = subprocess.run([script("status"), path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        try:
            facts = json.loads(r.stdout) if r.returncode == 0 else None
        except ValueError:
            facts = None
        if not facts:
            return {"unreadable": "the profile's worktree status script failed"}
        # "repos" (optional): the git repositories inside it, so its card can describe each one.
        repos = [os.path.realpath(r) for r in facts.get("repos") or [] if isinstance(r, str) and os.path.isabs(r)
                 and os.path.isdir(r) and inside(r, path)]
        return {"main": False, "branch": facts.get("branch", ""), "uncommitted": int(facts.get("uncommitted", 0)),
                "unpushed": int(facts.get("unpushed", 0)), "removesBranches": bool(facts.get("removesBranches")),
                "kind": facts.get("kind", "worktree"), "remove": script("remove"), "repos": repos}
    code, top, _ = git(path, "rev-parse", "--path-format=absolute", "--show-toplevel")
    code2, common, _ = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if code or code2 or os.path.realpath(top) != os.path.realpath(path):
        return None
    if os.path.realpath(common) == os.path.realpath(os.path.join(top, ".git")):
        return {"main": True}
    files = status_files(path)
    _c, branch, _ = git(path, "rev-parse", "--abbrev-ref", "HEAD")
    _c, ahead, _ = git(path, "rev-list", "--count", "HEAD", "--not", "--remotes")
    code3, head, _ = git(path, "rev-parse", "--verify", "-q", "HEAD")
    code4, ref, _ = git(path, "symbolic-ref", "-q", "HEAD")  # the full name: --abbrev-ref says heads/x when a tag x exists
    if files is None:
        return {"unreadable": "git status failed"}
    return {"main": False, "repo": os.path.dirname(os.path.realpath(common)), "branch": branch,
            "uncommitted": len(files), "unpushed": int(ahead) if ahead.isdigit() else 0,
            "head": head if not code3 and head else None, "ref": ref if not code4 and ref else None}


def inside(path, folder):
    path, folder = os.path.realpath(path or "/nonexistent"), os.path.realpath(folder)
    return path == folder or path.startswith(folder + os.sep)


def worker_tree(task, entry=None):
    """The realpath of the worktree a task's worker works in, always one its own task records (pa.task_worktrees):
    the one its ledger entry's `worktree` (the folder it was started in) equals or lies inside, the deepest if
    several do. A field that names no worktree of the task (another task's, stale, mistyped, relative) counts as
    absent: the task's main worktree, else None."""
    trees = pa.task_worktrees(task)
    given = (entry or {}).get("worktree") if isinstance(entry, dict) else None
    if isinstance(given, str) and given.strip():
        given = os.path.realpath(os.path.expanduser(given))
        holding = [t["path"] for t in trees if inside(given, t["path"])]
        if holding:
            return max(holding, key=len)
    main = next((t for t in trees if t["main"]), None)
    return main["path"] if main else None


def worktree_state(directory, agent, profile=None):
    """Per worktree in the ledger (every one a task records, pa.task_worktrees): its tasks (with their states,
    titles and the job it is for each), its workers' fates, and anyone else working inside it. A task's worker
    belongs to the row of its own worktree (worker_tree). A worker's fate: live, closed (its ledger entry is
    finished), killed, removed or exited."""
    ledger = pa.read_json(os.path.join(directory, "ledger.json"), {}) or {}
    live = pa.live_terminals() or {}
    rows = {}
    for task in ledger.get("tasks", []):
        if not task.get("id"):
            continue
        for tree in pa.task_worktrees(task):
            path = tree["path"]
            row = rows.setdefault(path, {"path": path, "given": tree["given"], "tasks": [], "states": {}, "titles": {}, "jobs": {}, "mains": {},
                                         "jobRecorded": {}, "keep": False, "workers": []})
            row["tasks"].append(task["id"])
            row["states"][task["id"]] = task.get("state")
            row["titles"][task["id"]] = task.get("title") or ""
            row["jobs"][task["id"]], row["mains"][task["id"]] = tree["job"], tree["main"]
            row["jobRecorded"][task["id"]] = tree["jobRecorded"]
            row["keep"] = row["keep"] or tree["keep"]
            for w in task.get("workers") or []:
                if worker_tree(task, w) != path:
                    continue
                info = pa.hc_get(w.get("address"))
                fate = "live" if info.get("state") == "live" else ("closed" if w.get("finished") else
                                                                  "killed" if info.get("reason") == "killed" else
                                                                  "removed" if not info else "exited")
                row["workers"].append({"address": w.get("address"), "name": w.get("name") or info.get("name"), "fate": fate})
    # A task counts on a row when the path it records is the worktree, inside it (a workspace package src/<Pkg>) or the
    # container that holds it (REVIEW M2, round 3): `counted` (distinct, exact ones first) is what rule (a), rule (b) and
    # a card's task set use; `related` names the paths those other tasks record, which a removal marks once they are gone.
    for row in rows.values():
        row["counted"], row["related"] = list(dict.fromkeys(row["tasks"])), {}
    for row in rows.values():
        for other in rows.values():
            if other is row or not (inside(other["path"], row["path"]) or inside(row["path"], other["path"])):
                continue
            for t in other["tasks"]:
                if t in row["tasks"]:
                    continue
                row["related"].setdefault(t, []).append(other["path"])
                if t not in row["counted"]:
                    row["counted"].append(t)
                    row["states"][t], row["titles"][t] = other["states"][t], other["titles"][t]
    for row in rows.values():
        mine = {w["address"] for w in row["workers"]}
        row["others"] = sorted(a for a, info in live.items() if a not in mine and a != agent.get("terminal") and (
            inside(info.get("cwd"), row["path"]) or inside((info.get("foreground") or {}).get("cwd"), row["path"])))
        row["facts"] = worktree_facts(row["path"], profile)
    return rows


# ---- what a worktree holds, for its Delete/Keep card (and WORKTREE.md) ----------------------------
# Read-only git calls, each with GIT_TIMEOUT. Any failure goes into "error"; the card opens anyway.

CARD_REPOS = 6  # repositories shown on the card
CARD_COMMITS = 8  # commits shown per repository
CARD_FILES = 10  # changed files shown per repository
CARD_UNCOMMITTED = 10  # uncommitted files shown per repository
SUBJECT = 72  # characters of a commit subject
PATH = 80  # characters of a file path (cut from the left)
CONTEXT_MAX = 2000  # characters of the card's context, a hard cap
MD_COMMITS, MD_FILES = 200, 500  # what WORKTREE.md lists per repository
DESCRIBE_REPOS = 20  # repositories described at all (each costs about 8 git calls)
NO_REPOS = "this kind of worktree lists no repositories; counts only"
SQUASH = "(a squash merge shows as not merged)"
DESCRIBE_DEADLINE = 60  # seconds for describing a whole worktree; repositories past it are named, not described
CONTROL = re.compile("[\x00-\x1f\x7f\x85\u2028\u2029]")


def clean(text):
    """Repository and ledger text as one visible line: control characters (a newline in a file name, a
    terminal escape) become escapes, so they cannot add lines or styling to a card or WORKTREE.md."""
    named = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
    return CONTROL.sub(lambda m: named.get(m.group(), "\\x{:02x}".format(ord(m.group())) if ord(m.group()) < 256
                                           else "\\u{:04x}".format(ord(m.group()))), str(text if text is not None else ""))


def md(text, limit=300):
    """clean() for WORKTREE.md, also safe inside a code span or a table cell."""
    return cut(clean(text), limit).replace("`", "'").replace("|", "\\|")


def cut(text, limit):
    text = str(text or "")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def cut_path(path, limit=PATH):
    path = str(path or "")
    return path if len(path) <= limit else "…" + path[-(limit - 1):]


def describe_repo(path, profile=None, name=None, deadline=None):
    """One git repository: its branch, the main branch it came from, the commits on the branch (merged
    into the main branch? pushed?), the files they change, and what is uncommitted (spec 4.3)."""
    repo = {"name": name or os.path.basename(path.rstrip(os.sep)), "path": path, "branch": None, "base": None, "fork": None,
            "commits": [], "commitsTotal": 0, "merged": 0, "pushed": 0, "files": [], "filesTotal": 0, "added": 0, "removed": 0,
            "uncommitted": [], "uncommittedTotal": 0, "error": None, "head": None, "fetched": None}

    def g(*args):  # every call within GIT_TIMEOUT, and within the whole description's deadline
        left = GIT_TIMEOUT if deadline is None else min(GIT_TIMEOUT, max(0.2, deadline - time.time()))
        return git(path, *args, timeout=left)

    def fail(err):
        repo["error"] = repo["error"] or (clean(str(err or "git failed").strip())[:200] or "git failed")

    code, branch, err = g("rev-parse", "--abbrev-ref", "HEAD")
    if code:
        fail(err)
        return repo
    repo["branch"] = clean(branch)
    repo["head"] = g("rev-parse", "HEAD")[1] or None
    # Merged and pushed come from local refs: say when they were last fetched.
    code, common, _ = g("rev-parse", "--path-format=absolute", "--git-common-dir")
    fetch_head = os.path.join(common, "FETCH_HEAD") if not code and common else None
    if fetch_head and os.path.isfile(fetch_head):
        repo["fetched"] = iso(os.path.getmtime(fetch_head))
    # The main branch: origin/HEAD's target, then main, master, trunk, then the profile's main branches.
    names = []
    code, target, _ = g("symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if not code and target:
        names.append(target)
    names += ["main", "master", "trunk"]
    for b in (profile or {}).get("mainBranches") or []:
        names += [b, "origin/" + b]
    bases = [n for n in dict.fromkeys(names) if n != branch and g("rev-parse", "--verify", "--quiet", n + "^{commit}")[0] == 0]
    repo["base"] = bases[0] if bases else None
    # Where the branch began: its creation in the reflog (still right after a fast-forward merge), else the merge-base.
    fork = None
    if branch != "HEAD":
        code, out, _ = g("reflog", "show", "--format=%H", "refs/heads/" + branch)
        oldest = out.splitlines()[-1].strip() if not code and out else None
        if oldest and g("merge-base", "--is-ancestor", oldest, "HEAD")[0] == 0:
            fork = oldest
    if not fork and bases:
        code, out, err = g("merge-base", "HEAD", bases[0])
        fork = out if not code and out else None
        if code:
            fail(err)
    if fork:
        repo["fork"] = g("rev-parse", "--short", fork)[1] or fork[:7]
        code, count, _ = g("rev-list", "--count", fork + "..HEAD")
        code, out, err = g("log", "-n", str(MD_COMMITS + 1), "--format=%H%x1f%h%x1f%s", fork + "..HEAD")
        if code:
            fail(err)
        rows = [line.split("\x1f", 2) for line in out.splitlines() if line.count("\x1f") >= 2] if not code else []
        repo["commitsTotal"] = max(int(count) if count.isdigit() else 0, len(rows))
        unmerged = []  # per main-branch candidate: the branch commits it does not contain
        for b in bases:
            code, out, _ = g("rev-list", "HEAD", "--not", fork, b)
            if not code:
                unmerged.append(set(out.split()))
        picked = set()  # commits whose patch is already in the main branch (rebased or picked)
        if repo["base"] and repo["commitsTotal"] <= MD_COMMITS:
            code, out, _ = g("cherry", repo["base"], "HEAD", fork)
            picked = {line[2:].strip() for line in out.splitlines() if line.startswith("- ")} if not code else set()
        code, out, err = g("rev-list", "HEAD", "--not", "--remotes")
        if code:
            fail(err)
        unpushed = set(out.split())
        for full, short, subject in rows:
            merged = (any(full not in u for u in unmerged) or full in picked) if unmerged else None
            repo["commits"].append({"sha": short, "subject": clean(subject), "merged": merged, "pushed": not code and full not in unpushed})
        repo["merged"] = sum(1 for c in repo["commits"] if c["merged"])
        repo["pushed"] = sum(1 for c in repo["commits"] if c["pushed"])
        code, out, err = g("diff", "--numstat", "-z", "-M", fork, "HEAD")
        if code:
            fail(err)
        parts, i = (out.split("\x00") if not code else []), 0
        while i < len(parts):
            bits = parts[i].split("\t", 2)
            i += 1
            if len(bits) < 3:
                continue
            added, removed, name_ = bits
            if not name_:  # a rename: the old and the new path follow
                name_ = parts[i + 1] if i + 1 < len(parts) else ""
                i += 2
            added = int(added) if added.isdigit() else None
            removed = int(removed) if removed.isdigit() else None
            repo["filesTotal"] += 1
            repo["added"] += added or 0
            repo["removed"] += removed or 0
            if len(repo["files"]) < MD_FILES:
                repo["files"].append({"path": clean(name_), "added": added, "removed": removed})
    elif not repo["error"]:
        fail("no main branch found")
    files = status_files(path)
    if files is None:
        fail("git status failed")
    else:
        repo["uncommittedTotal"] = len(files)
        repo["uncommitted"] = [{"path": clean(f), "status": st} for st, f in files[:MD_FILES]]
    return repo


def describe_worktree(row, facts, profile):
    """The tasks using the worktree and each repository in it (spec 4.2, 4.4)."""
    desc = {"tasks": [{"id": clean(t), "title": clean(row.get("titles", {}).get(t, "")), "state": clean(row.get("states", {}).get(t))}
                      for t in row["tasks"]],
            "repos": [], "notDescribed": [], "notDescribedWhy": {}, "note": None, "error": None, "kind": clean(facts.get("kind") or "git worktree")}
    deadline = time.time() + DESCRIBE_DEADLINE
    if facts.get("remove"):  # the profile's own kind: its status script names the repositories, if it can
        paths = facts.get("repos") or []
        if not paths:
            desc["note"] = NO_REPOS
        names = [None] * len(paths)
    else:
        paths, names = [row["path"]], [os.path.basename(facts.get("repo") or row["path"])]
    for i, path in enumerate(paths):
        why = ("more than {} repositories".format(DESCRIBE_REPOS) if i >= DESCRIBE_REPOS else
               "time limit of {:g} s".format(DESCRIBE_DEADLINE) if time.time() >= deadline else None)
        if why:
            desc["notDescribed"].append(clean(os.path.basename(path)))
            desc["notDescribedWhy"].setdefault(why, []).append(clean(os.path.basename(path)))
            continue
        try:
            desc["repos"].append(describe_repo(path, profile, names[i], deadline))
        except Exception as e:  # noqa: BLE001 - a description never stops the card
            desc["repos"].append(dict(name=names[i] or os.path.basename(path), path=path, branch=None, base=None, fork=None,
                                      commits=[], commitsTotal=0, merged=0, pushed=0, files=[], filesTotal=0, added=0, removed=0,
                                      uncommitted=[], uncommittedTotal=0, error=clean(e)[:200], head=None, fetched=None))
    return desc


def counts(added, removed):
    text = " ".join(x for x in ("+{}".format(added) if added else "", "−{}".format(removed) if removed else "") if x)
    return text


def unchanged(repo):
    return not repo["commitsTotal"] and not repo["filesTotal"] and not repo["uncommittedTotal"] and not repo["error"]


def merged_word(c):
    return "merged: unknown" if c["merged"] is None else "merged" if c["merged"] else "not merged"


def repo_lines(r):
    head = "{}: branch {}".format(r["name"], r["branch"] or "?")
    if r["fork"]:
        n = r["commitsTotal"]
        head += ", from {} at {}: {} commit{}, {}, {} pushed".format(
            r["base"] or "its start", r["fork"], n, "" if n == 1 else "s",
            "{} merged into {}".format(r["merged"], r["base"]) if r["base"] else "merged: unknown", r["pushed"])
    lines = [head]
    if r["commitsTotal"]:
        lines.append("  merged and pushed are from local refs; last fetch: {}".format(r.get("fetched") or "never"))
    shown = r["commits"][:CARD_COMMITS]
    for c in shown:
        lines.append("  {} {}  ({}, {})".format(c["sha"], cut(c["subject"], SUBJECT), merged_word(c), "pushed" if c["pushed"] else "not pushed"))
    if r["commitsTotal"] > len(shown):
        lines.append("  … {} shown, {} more".format(len(shown), r["commitsTotal"] - len(shown)))
    if r["filesTotal"]:
        listed = ["{} (binary)".format(cut_path(f["path"])) if f["added"] is None else
                  " ".join(x for x in (cut_path(f["path"]), counts(f["added"], f["removed"])) if x) for f in r["files"][:CARD_FILES]]
        more = r["filesTotal"] - len(listed)
        lines.append("  Files changed: {} ({}): {}{}".format(r["filesTotal"], counts(r["added"], r["removed"]) or "+0",
                                                          ", ".join(listed), ", … {} more".format(more) if more > 0 else ""))
    if r["uncommittedTotal"]:
        listed = ["{} {}".format(u["status"], cut_path(u["path"])) for u in r["uncommitted"][:CARD_UNCOMMITTED]]
        more = r["uncommittedTotal"] - len(listed)
        lines.append("  Uncommitted: {}: {}{}".format(r["uncommittedTotal"], ", ".join(listed), ", … {} more".format(more) if more > 0 else ""))
    if r["error"]:
        lines.append("  could not read the changes: {}".format(r["error"]))
    return lines


def branch_loss(desc, facts):
    """When deleting also deletes the branches (removesBranches) and the repositories were described: what the
    branches' commits would come to (AC3.3). A commit merged into a main branch (the local one counts: it keeps
    the commit) or on a remote is kept. {"lost": n or None (unknown), "lostIn": [names], "lines": {index: line}};
    None when deleting keeps the branches or only counts are known."""
    if not facts.get("removesBranches") or not desc.get("repos"):
        return None
    out = {"lost": 0, "lostIn": [], "lines": {}}
    unknown = bool(desc.get("error") or desc.get("notDescribed"))
    for i, r in enumerate(desc["repos"]):
        n = r["commitsTotal"]
        lost = sum(1 for c in r["commits"] if c["merged"] is not True and not c["pushed"])
        if r["error"]:
            why = r["error"]
        elif len(r["commits"]) < n or n > MD_COMMITS:
            why = "more commits than described"
        elif any(c["merged"] is None and not c["pushed"] for c in r["commits"]):
            why = "merged unknown"
        else:
            why = None
        if why:
            unknown = True
            out["lines"][i] = "  unknown if deleted: {}; Keep is recommended".format(why)
        elif lost:
            out["lost"] += lost
            out["lostIn"].append(r["name"])
            out["lines"][i] = "  LOST if deleted: {} of {} commits neither merged nor on a remote".format(lost, n)
        elif n:
            out["lines"][i] = "  kept if deleted: all {} commits merged or pushed".format(n)
    if unknown:
        out["lost"] = None
    return out


def deleting_text(facts, loss=None):
    loses_commits = facts.get("removesBranches") and facts.get("unpushed", 0) > 0
    n = facts.get("uncommitted", 0)
    lost = "; the {} uncommitted files are DISCARDED".format(n) if n else ""
    if facts.get("removesBranches") and loss is not None:
        said = ("; whether every commit is merged or pushed is unknown" if loss["lost"] is None else
                "; {} commits ({}) are neither merged nor on a remote and would be LOST".format(loss["lost"], ", ".join(loss["lostIn"]))
                if loss["lost"] else "; all their commits are merged or pushed")
        return "Deleting also deletes its branches ({}){}{}.".format(facts.get("branch"), said, lost)
    if facts.get("removesBranches"):
        return "Deleting also deletes its branches ({}){}{}.".format(
            facts.get("branch"), "; the commits that are on no remote would be LOST" if loses_commits else "", lost)
    holder = facts.get("holderNote") or ""
    return "Deleting: the branch {} is deleted when every commit is merged and pushed, else kept with its commits{}.{}".format(
        facts.get("branch"), lost or "; nothing uncommitted is lost", holder)


def card_context(desc, row, facts, gone, extra=(), md_file="WORKTREE.md"):
    """The card's context: one fact per line, at most CONTEXT_MAX characters (spec 4.5). extra: lines after the
    Tasks line (the job, rule (b)); md_file: the evidence file with the full list."""
    loss = branch_loss(desc, facts)
    lines = ["{} ({})".format(row["path"], desc.get("kind") or facts.get("kind") or "git worktree"),
             "Tasks: " + ", ".join('{} "{}" ({})'.format(t["id"], cut(t["title"], 60), t["state"]) for t in desc["tasks"])] + list(extra)
    if desc.get("error"):
        lines.append("could not read the changes: {}".format(desc["error"]))
    if desc.get("note") or desc.get("error"):
        lines += [desc["note"]] if desc.get("note") else []
        lines.append("Uncommitted files: {}. Commits not on any remote: {}.".format(facts.get("uncommitted", 0), facts.get("unpushed", 0)))
    changed = [(i, r) for i, r in enumerate(desc["repos"]) if not unchanged(r)]
    for i, r in changed[:CARD_REPOS]:
        lines += repo_lines(r)
        if loss and i in loss["lines"]:
            lines.append(loss["lines"][i])
    more = max(0, len(changed) - CARD_REPOS) + len(desc.get("notDescribed") or [])
    if more:
        lines.append("… and {} more repositories ({})".format(more, md_file))
    still = [r["name"] for r in desc["repos"] if unchanged(r)]
    if still:
        lines.append("Unchanged: " + ", ".join(still))
    lines += ["Workers: " + gone, "No other terminal is working in it.", deleting_text(facts, loss), "Full list: " + md_file]
    text = "\n".join(lines)
    if len(text) > CONTEXT_MAX:
        tail_ = "… (full list in {})".format(md_file)
        keep = text[:CONTEXT_MAX - len(tail_) - 1]
        text = (keep[:keep.rfind("\n")] if "\n" in keep else keep) + "\n" + tail_
    return text


def card_changes(desc):
    """The structured description on the card, with the card's caps; truncated when anything was cut."""
    truncated = len(desc["repos"]) > CARD_REPOS or bool(desc.get("notDescribed"))
    repos = []
    for r in desc["repos"][:CARD_REPOS]:
        truncated = truncated or r["commitsTotal"] > CARD_COMMITS or r["filesTotal"] > CARD_FILES or r["uncommittedTotal"] > CARD_UNCOMMITTED \
            or any(len(c["subject"]) > SUBJECT for c in r["commits"][:CARD_COMMITS])
        repos.append(dict(r, commits=[dict(c, subject=cut(c["subject"], SUBJECT)) for c in r["commits"][:CARD_COMMITS]],
                          files=r["files"][:CARD_FILES], uncommitted=r["uncommitted"][:CARD_UNCOMMITTED]))
    return {"tasks": desc["tasks"], "repos": repos, "note": desc.get("note"), "error": desc.get("error"), "truncated": truncated, "at": pa.now()}


def worktree_md(desc, row, facts, gone, card_id, extra=()):
    """WORKTREE.md (WORKTREE-<job>.md for a job's card): the same sections as the card, with the larger caps
    (spec 4.6). Every piece of repository or ledger text goes through md(), so it stays inside its own line,
    code span or cell."""
    loss = branch_loss(desc, facts)
    out = ["# Worktree {}".format(md(row["path"], 1000)), "", "{}. Written {} with decision card {}.".format(
        md(desc.get("kind") or "git worktree"), pa.now(), card_id), "", "## Tasks", ""]
    out += ['- {} "{}" ({})'.format(md(t["id"]), md(t["title"]), md(t["state"])) for t in desc["tasks"]]
    out += ([""] + [md(line, 1000) for line in extra]) if extra else []
    if desc.get("note") or desc.get("error"):
        out += [""] + ([desc["note"]] if desc.get("note") else []) + (["Could not read the changes: " + md(desc["error"])] if desc.get("error") else [])
        out.append("Uncommitted files: {}. Commits not on any remote: {}.".format(facts.get("uncommitted", 0), facts.get("unpushed", 0)))
    for i, r in enumerate(desc["repos"]):
        out += ["", "## {}".format(md(r["name"])), "", "`{}`: branch `{}`{}.".format(md(r["path"], 1000), md(r["branch"] or "?"),
                ", from `{}` at `{}`".format(md(r["base"] or "its start"), md(r["fork"])) if r["fork"] else "")]
        if loss and i in loss["lines"]:
            out += ["", md(loss["lines"][i].strip(), 1000) + "."]
        if r["commitsTotal"]:
            out += ["", "Merged and pushed are from local refs; last fetch: {}.".format(r.get("fetched") or "never")]
        if r["error"]:
            out += ["", "Could not read the changes: {}".format(md(r["error"]))]
        out += ["", "### Commits ({}, {} merged, {} pushed)".format(r["commitsTotal"], r["merged"], r["pushed"]), ""]
        out += ["- `{}` {} ({}, {})".format(md(c["sha"]), md(c["subject"]), merged_word(c), "pushed" if c["pushed"] else "not pushed")
                for c in r["commits"][:MD_COMMITS]] or ["- none"]
        if r["commitsTotal"] > MD_COMMITS:
            out.append("- … {} more".format(r["commitsTotal"] - MD_COMMITS))
        out += ["", "### Files changed ({}, {})".format(r["filesTotal"], counts(r["added"], r["removed"]) or "+0"), ""]
        if r["files"]:
            out += ["| File | Added | Removed |", "|---|---|---|"] + ["| `{}` | {} | {} |".format(
                md(f["path"]), "binary" if f["added"] is None else f["added"], "" if f["removed"] is None else f["removed"]) for f in r["files"]]
            if r["filesTotal"] > len(r["files"]):
                out += ["", "… {} more".format(r["filesTotal"] - len(r["files"]))]
        else:
            out.append("None.")
        out += ["", "### Uncommitted ({})".format(r["uncommittedTotal"]), ""]
        out += ["- `{}` {}".format(md(u["status"]), md(u["path"])) for u in r["uncommitted"]] or ["None."]
    for why, names in (desc.get("notDescribedWhy") or {}).items():
        out += ["", "Not described ({}): {}".format(why, ", ".join(md(n) for n in names))]
    out += ["", "## Workers", "", md(gone, 2000), "", "## Deleting", "", md(deleting_text(facts, loss), 2000), ""]
    return "\n".join(out)


def card_id(task, job, main=True):
    """The Delete/Keep card of a worktree: d-wt-<task> for the task's main one (cards from before t-020 stay valid),
    d-wt-<task>-<job> for any other."""
    return "d-wt-{}".format(task) if main else "d-wt-{}-{}".format(task, job)


def md_name(job, main=True):
    """The evidence file with a worktree card's full list: WORKTREE.md, or WORKTREE-<job>.md for a job's card."""
    return "WORKTREE.md" if main else "WORKTREE-{}.md".format(job)


def card_path(directory, task, job=None, main=True):
    """Where the card is (t-054: in decisions-archive/ when only there, so an archived answer still counts). Write it
    with pa.write_card, which puts it in decisions/."""
    return pa.card_file(directory, card_id(task, job, main))


def card_owner(row):
    """(task, job, main) whose card a worktree row gets: the first task whose main worktree it is, else the row's
    first task with the job it records it for. check and remove both use this, so they agree on the card."""
    task = next((t for t in row["tasks"] if row["mains"].get(t)), None)
    if task:
        return task, row["jobs"][task], True
    return row["tasks"][0], row["jobs"][row["tasks"][0]], False


# ---- which card is a worktree's (t-073) ----------------------------------------------------------------------------
# A card answers only for the worktree path it names, on the branch it names (when it records one), and only until that
# worktree is removed. The id alone never decides: an answered card for another path, an earlier worktree at the same
# folder or another branch is not this worktree's, and the worktree gets its own card under an id no other card holds.


def card_listing(directory):
    """The ids of every card file in decisions/ and decisions-archive/: pa-worktree check lists both once (t-131) and
    hands the set to task_cards."""
    ids = set()
    for sub in (pa.ARCHIVE, "decisions"):
        folder = os.path.join(directory, sub)
        for name in os.listdir(folder) if os.path.isdir(folder) else []:
            if name.endswith(".json"):
                ids.add(name[:-len(".json")])
    return ids


def task_cards(directory, task, listing=None):
    """{card id: card} of the task's worktree cards, in decisions/ and decisions-archive/ (decisions/ wins, t-054): files
    d-wt-<task>.json and d-wt-<task>-*.json whose `task` is the task and which name a `worktree`. listing: card_listing's
    set, read once by the caller; None: listed now."""
    prefix = "d-wt-" + str(task)
    ids = {cid for cid in (card_listing(directory) if listing is None else listing) if cid == prefix or cid.startswith(prefix + "-")}
    cards = {}
    for cid in sorted(ids):
        card = pa.read_json_safe(pa.card_file(directory, cid))
        if (isinstance(card, dict) and card.get("task") == task and isinstance(card.get("worktree"), str)
                and card["worktree"].strip()):
            cards[cid] = card
    return cards


def card_tree(card):
    return os.path.realpath(os.path.expanduser(card.get("worktree") or "/nonexistent"))


STAMP = re.compile(r"^(\d{4})-(\d\d)-(\d\d)(?:[T ](\d\d):(\d\d)(?::(\d\d)(?:\.\d+)?)?\s*(Z|[+-]\d\d:?\d\d)?)?$")


def stamp_time(value):
    """(UTC datetime, date only) of a written time: ISO with Z, an offset or none (UTC), or a date only; None if it is
    not one (t-131: stamps are compared as times, never as strings)."""
    m = STAMP.match(str(value).strip()) if isinstance(value, str) else None
    if not m:
        return None
    y, mo, d, h, mi, sec, zone = m.groups()
    try:
        when = datetime.datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(sec or 0), tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    if zone and zone != "Z":
        sign = -1 if zone[0] == "-" else 1
        digits = zone[1:].replace(":", "")
        when -= sign * datetime.timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
    return when, h is None


def removed_after(removed, stamp):
    """Whether a ledger `removed` is at or after a card's time `stamp`: by day when either is a date only; true when the
    card has no time or `removed` is not a time (true, a word): the card then never counts, the safe way."""
    then, gone = stamp_time(stamp), stamp_time(removed)
    if then is None or gone is None:
        return True
    if then[1] or gone[1]:
        return gone[0].date() >= then[0].date()
    return gone[0] >= then[0]


def card_spent(card, entry):
    """Why a card no longer counts for any worktree, or None: its worktree was removed (`worktreeRemoved`, written by
    pa-worktree remove), or the ledger task records its path removed at or after the card was answered (or asked, for an
    open one; any removal when the card has no time): a card from before t-073 for a folder made again later."""
    if card.get("worktreeRemoved"):
        return "its worktree was removed at {}".format(clean(card["worktreeRemoved"]))
    stamp = str(card.get("answered") or card.get("created") or "")
    path = card_tree(card)
    for e in (entry or {}).get("worktrees") or [] if isinstance((entry or {}).get("worktrees"), list) else []:
        if (isinstance(e, dict) and isinstance(e.get("path"), str) and e["path"].strip() and e.get("removed")
                and os.path.realpath(os.path.expanduser(e["path"])) == path and removed_after(e["removed"], stamp)):
            return "its worktree was removed at {}, after the card".format(clean(e["removed"]))
    return None


def branch_differs(card, facts):
    """Why a card names another branch than the worktree is on now, or None (only when both are known)."""
    then, now = card.get("branch"), (facts or {}).get("branch")
    if isinstance(then, str) and then and isinstance(now, str) and now and then != now:
        return "it was asked for branch {}; the worktree is on {} now".format(clean(then), clean(now))
    return None


def card_for(directory, row, task, entry, listing=None):
    """(id, path, card) of the worktree's own card among the task's, or (None, None, None): a card whose worktree is the
    row's path, not spent and not for another branch. An open card first, then the newest answered, then the newest
    withdrawn (asked again under its id). Its items on batch cards count too, as item_view gives them (t-168 R8)."""
    mine = [(cid, c) for cid, c in list(task_cards(directory, task, listing).items()) + batch_views(directory, task, listing)
            if card_tree(c) == row["path"] and not card_spent(c, entry) and not branch_differs(c, row.get("facts"))]
    rank = {"open": 2, "answered": 1}
    mine.sort(key=lambda x: (rank.get(x[1].get("status"), 0), str(x[1].get("answered") or x[1].get("created") or "")), reverse=True)
    if not mine:
        return None, None, None
    cid, card = mine[0]
    return cid, pa.card_file(directory, cid), card


def new_card_id(directory, task, job, main):
    """The first free id of a worktree's new card: d-wt-<task> (the main worktree only), d-wt-<task>-<job>, then
    d-wt-<task>-<job>-2, -3, ... An id held by any card (decisions/ or the archive) is never reused for another worktree."""
    first = ["d-wt-{}".format(task)] if main else []
    for cid in first + ["d-wt-{}-{}".format(task, job)]:
        if not os.path.lexists(pa.card_file(directory, cid)):
            return cid
    n = 2
    while os.path.lexists(pa.card_file(directory, "d-wt-{}-{}-{}".format(task, job, n))):
        n += 1
    return "d-wt-{}-{}-{}".format(task, job, n)


def md_for(task, cid):
    """The evidence file of card `cid`: WORKTREE.md for d-wt-<task>, WORKTREE-<rest>.md for d-wt-<task>-<rest>."""
    prefix = "d-wt-{}".format(task)
    return "WORKTREE.md" if cid == prefix else "WORKTREE-{}.md".format(cid[len(prefix) + 1:])


def stale_open_card(card, rows, entry, task=None):
    """Why an open card of a task stands for no worktree any more, or None: no task records its path, its worktree was
    removed after it was asked, the worktree is on another branch now, or (t-131) its card is another task's now (a
    shared worktree whose owner changed: check and remove read only the owner's card)."""
    path = card_tree(card)
    if path not in rows:
        return "its worktree {} is no longer recorded".format(clean(card.get("worktree")))
    owner = card_owner(rows[path])[0]
    if task is not None and owner != task:
        return "its worktree's card is now {}'s".format(owner)
    return card_spent(card, entry) or branch_differs(card, rows[path].get("facts"))


def ledger_tasks(directory):
    """{task id: ledger entry} (read only)."""
    tasks = read_ledger(directory).get("tasks")
    return {t["id"]: t for t in tasks if isinstance(t, dict) and isinstance(t.get("id"), str)} if isinstance(tasks, list) else {}


def withdraw_stale(directory, rows, entries, open_cards, listing=None):
    """pa-worktree check: an open worktree card that stands for no worktree any more (stale_open_card) is said, and
    withdrawn with --open-cards, so it is never answered Delete for nothing. Answered and withdrawn cards stay as they are."""
    for task in sorted(entries):
        for cid, card in task_cards(directory, task, listing).items():
            why = card.get("status") == "open" and stale_open_card(card, rows, entries.get(task), task)
            if not why:
                continue
            if open_cards:
                withdraw_card(directory, pa.card_file(directory, cid), card, task, why)
            else:
                print("{}: card {} is open, but {}; --open-cards withdraws it".format(task, cid, why))


def refuse_others(directory, row, task, entry, listing=None):
    """pa-worktree remove with no answered card of the worktree's own: when an answered Delete of the task names another
    worktree (another path, an earlier worktree at this folder, another branch), refuse and name it (t-073 R3)."""
    found = []
    for cid, card in task_cards(directory, task, listing).items():
        if card.get("status") != "answered" or not str(card.get("answer", "")).lower().startswith("delete"):
            continue
        if card_tree(card) != row["path"]:
            found.append((str(card.get("answered") or ""), "the card is for {}, not {} (card {} answered {!r})".format(
                clean(card.get("worktree")), row["path"], cid, card.get("answer"))))
        else:
            why = card_spent(card, entry) or branch_differs(card, row.get("facts"))
            if why:
                found.append((str(card.get("answered") or ""), "card {} answered {!r} was for an earlier worktree at {}: {}".format(
                    cid, card.get("answer"), row["path"], why)))
    if found:
        raise pa.Problem("{}; nothing was removed. {} gets its own card from `pa-worktree check --open-cards`".format(
            max(found)[1], row["path"]))


def in_worktrees_folder(path, profile=None):
    """True when the worktree sits in a folder named `worktrees`: <project>/worktrees/<container>/<repo> or
    <workspace>/worktrees/<name>, or (t-192 R3) it is a package of a holder in one (holder_of). A job's worktree
    elsewhere is neither offered nor removed (D4)."""
    parent = os.path.dirname(path)
    return os.path.basename(parent) == "worktrees" or os.path.basename(os.path.dirname(parent)) == "worktrees" or \
        holder_of(path, profile) is not None


WARNED = set()  # the profile glob warnings already printed by this process


def holder_glob(profile):
    """The profile's `worktree.repos` glob as its parts (each one path component, `*` matching within it), or [] when
    the profile names no marker or no glob (H3.6), or the glob is not a plain relative one (H3.7: warned once)."""
    custom = (profile or {}).get("worktree") or {}
    glob = custom.get("repos") if isinstance(custom, dict) else None
    if not isinstance(custom, dict) or not isinstance(custom.get("marker"), str) or not custom["marker"] or \
            not isinstance(glob, str) or not glob:
        return []
    parts = glob.split("/")
    if os.path.isabs(glob) or any(p in ("", ".", "..") for p in parts) or "\\" in glob:
        if glob not in WARNED:
            WARNED.add(glob)
            print("pa-worktree: the profile's worktree.repos {!r} is not a relative glob of plain parts; no holder is "
                  "looked for".format(clean(glob)), file=sys.stderr)
        return []
    return parts


def holder_of(path, profile):
    """t-192 R3: (H, P) when the recorded path's realpath P is a package of a holder H: H is as many folders above P as
    the profile's `worktree.repos` glob has parts, P's path below H matches it part by part, H holds the profile's
    `worktree.marker`, and H's parent folder is named `worktrees`; else None. Realpaths, so `..` and links resolve
    first and a package linked out of H is not in it."""
    import fnmatch
    parts = holder_glob(profile)
    if not parts or not isinstance(path, str) or not path.strip():
        return None
    p = os.path.realpath(os.path.expanduser(path))
    h = p
    for _ in parts:
        h = os.path.dirname(h)
    rel = os.path.relpath(p, h).split(os.sep)
    if len(rel) != len(parts) or not all(fnmatch.fnmatchcase(r, g) for r, g in zip(rel, parts)):
        return None
    if os.path.basename(os.path.dirname(h)) != "worktrees" or not os.path.isfile(os.path.join(h, profile["worktree"]["marker"])):
        return None
    return h, p


def not_over(desc):
    """Why a described worktree's job is not over under rule (b), or None when it is: every repository has at least
    one commit since its fork, all of them merged into a main branch (pushed is not enough while the task is in
    progress), nothing uncommitted, and the whole of it described (spec R2, AC2.1, AC2.2)."""
    if desc.get("error"):
        return "cannot tell ({})".format(desc["error"])
    if desc.get("note"):
        return "cannot tell ({})".format(desc["note"])
    if desc.get("notDescribed"):
        return "cannot tell (not every repository was described)"
    if not desc.get("repos"):
        return "cannot tell (no repository in it)"
    for r in desc["repos"]:
        n = r["commitsTotal"]
        if r["error"]:
            return "cannot tell ({})".format(r["error"])
        if not n:
            return "no commits since its fork"
        if len(r["commits"]) < n or n > MD_COMMITS:
            return "cannot tell (more commits than described)"
        if r["uncommittedTotal"]:
            return "{} uncommitted file(s)".format(r["uncommittedTotal"])
        if any(c["merged"] is None for c in r["commits"]):
            return "merged unknown"
        k = sum(1 for c in r["commits"] if c["merged"] is not True)
        if k:
            return "{} of {} commits not merged into {}".format(k, n, r["base"] or "the main branch")
    return None


def job_over(row, facts, profile):
    """Rule (b): (True, None, desc) when the worktree's job is over although a task using it is not done or parked;
    else (False, why, desc or None). The cheap checks come first; describing (git calls) only when they pass."""
    live = [w["address"] for w in row["workers"] if w["fate"] == "live"]
    if live:
        return False, "in use by " + ", ".join(live), None
    if row["others"]:
        return False, "another terminal is working in it: " + ", ".join(row["others"]), None
    if facts.get("uncommitted"):
        return False, "{} uncommitted file(s)".format(facts["uncommitted"]), None
    try:
        desc = describe_worktree(row, facts, profile)
    except Exception as e:  # noqa: BLE001 - unknown is never over
        return False, "cannot tell ({})".format(clean(e)[:200]), None
    why = not_over(desc)
    return why is None, why, desc


def under_rule_b(row, card=None):
    """True when rule (b) decides this worktree: a task using it is not done or parked, exactly one task records it, and
    (when a card is given) the card was opened under rule (b). A worktree two or more tasks record keeps rule (a): offered
    and removed only once every task using it is done or parked (spec AC7.1, VP2). check and remove both ask this.
    A task that records a path inside the worktree or its container counts too (row["counted"], round 3)."""
    return bool(tasks_still(row)) and len(row_tasks(row)) == 1 and (card is None or card.get("rule") == "b")


def row_tasks(row):
    """The distinct tasks that count on a worktree row, sorted: what a card records as the set it was asked about."""
    return sorted(set(row.get("counted") or row["tasks"]))


def card_mismatch(row, card, answered):
    """Why a card no longer stands for the worktree as the ledger has it now, or None (VERIFY M2, round 3). A card is for
    the task set it was asked about: a different set now (a card from before round 3 records none: it differs once two or
    more tasks count on the row, and stands for one). An open rule-(b) card also stops standing once every task using the
    worktree is done or parked (rule (a) asks then); an answered one keeps standing there (RL-V4, not changed here)."""
    now = row_tasks(row)
    asked = card.get("tasks")
    if not isinstance(asked, list):
        if len(now) > 1:
            return "the card does not record which tasks it was asked for, and {} record the worktree now".format(
                ", ".join(clean(t) for t in now))
    elif sorted(set(str(t) for t in asked)) != now:
        return "the card was asked for {}; the worktree is recorded by {} now".format(
            ", ".join(clean(t) for t in sorted(set(str(t) for t in asked))) or "no task", ", ".join(clean(t) for t in now))
    if not answered and card.get("rule") == "b" and not tasks_still(row):
        return ("the card was asked under rule (b), and every task using the worktree is done or parked now; it is asked again "
                "under rule (a)")
    return None


def withdraw_card(directory, path, card, task, why):
    card.update(status="withdrawn", answer=None, note="Withdrawn by pa-worktree check: " + why, answered=pa.now(),
                answeredBy="pa-worktree check: withdrawn")
    pa.write_card(directory, path, card)
    append_journal(directory, {"by": "agent" if by_an_agent() else "person", "trigger": "worktree", "task": task,
                               "why": "withdrew card {}: {}".format(card["id"], why)})
    print("{}: withdrew card {}: {}".format(task, card["id"], why))


def card_extra(row, owner, rule):
    """(named, extra) of a worktree's card: whether the question names the job, and the lines after its Tasks line."""
    task, job, main = owner
    named = rule == "b" or not main or job != "main"
    extra = []
    if named:
        extra.append("Job: {}{}".format(clean(job), "" if row["jobRecorded"].get(task, True) else " (named from its folder; not recorded)"))
    if rule == "b":  # one task records the worktree (under_rule_b)
        extra.append("Task still {}; this job's commits are all merged".format(state_word(row["states"].get(task))))
    return named, extra


def describe_safely(row, facts, profile):
    """describe_worktree, or a description holding only the error: describing never stops a card (AC7.5)."""
    try:
        return describe_worktree(row, facts, profile)
    except Exception as e:  # noqa: BLE001
        return {"tasks": [{"id": clean(t), "title": clean(row["titles"].get(t, "")), "state": clean(row["states"].get(t))} for t in row["tasks"]],
                "repos": [], "notDescribed": [], "notDescribedWhy": {}, "note": None, "error": clean(e)[:200],
                "kind": clean(facts.get("kind") or "git worktree")}


def card_loss(desc, facts):
    """(branch_loss, dirty): dirty when deleting loses something: uncommitted files, or (removesBranches) commits that
    are neither merged nor on a remote, or not known to be."""
    loss = branch_loss(desc, facts)
    loses_commits = facts.get("removesBranches") and (loss["lost"] != 0 if loss else facts["unpushed"] > 0)
    return loss, bool(facts["uncommitted"] > 0 or loses_commits)


def card_snapshot(facts, desc):
    """What pa-worktree remove compares at removal: the counts, each repository's HEAD and its uncommitted files."""
    return {"uncommitted": facts["uncommitted"], "unpushed": facts["unpushed"],
            "commits": sum(r["commitsTotal"] for r in desc["repos"]),
            "heads": {r["path"]: r["head"] for r in desc["repos"] if r.get("head")},
            "uncommittedFiles": {r["path"]: [u["path"] for u in r["uncommitted"]] for r in desc["repos"]
                                 if r.get("head") and r["uncommittedTotal"] == len(r["uncommitted"])}}


def open_worktree_card(directory, row, facts, profile, owner, rule, gone, desc=None, cid=None):
    """Writes the Delete/Keep card of one worktree and its evidence file; returns the recommendation. `cid`: the id of
    the worktree's own withdrawn card, asked again under it; else a new id no other card holds (new_card_id, t-073)."""
    task, job, main = owner
    named, extra = card_extra(row, owner, rule)
    facts = dict(facts, holderNote=holder_note(row, profile))  # t-192 R6.4: its Deleting line names the holder
    if desc is None:
        desc = describe_safely(row, facts, profile)
    loss, dirty = card_loss(desc, facts)
    cid = cid or new_card_id(directory, task, job, main)
    md_file = md_for(task, cid)
    evidence = []
    try:
        pa.write_text(os.path.join(directory, "evidence", task, md_file), worktree_md(desc, row, facts, gone, cid, extra))
        evidence = [md_file]
    except OSError:
        pass
    squash = (" " + SQUASH if any(c["merged"] is False for r in desc["repos"] for c in r["commits"]) else "")
    titles = ", ".join('{} "{}"'.format(clean(t), cut(clean(row["titles"].get(t, "")), 60)) for t in row.get("counted") or row["tasks"])
    path = pa.card_file(directory, cid)
    carry = held_carry(worktree_holds(directory), [row]) or carried(pa.read_json(path))
    pa.write_card(directory, path, dict(carry, **{
        "id": cid, "task": task, "worktree": row["path"], "branch": facts.get("branch") or None, "job": job, "main": main,
        "rule": rule, "tasks": row_tasks(row),
        "question": "Delete the worktree for {}{}?".format(titles, ", job {}".format(clean(job)) if named else ""),
        "context": card_context(desc, row, facts, gone, extra, md_file),
        "evidence": evidence,
        "changes": card_changes(desc),
        "options": [{"label": "Delete", "detail": "Remove the {}{}.{}".format(facts.get("kind", "worktree folder"),
                     " with everything listed above that is not saved elsewhere" if dirty else "", squash)},
                    {"label": "Keep", "detail": "Leave the worktree. It will not be asked about again."}],
        "recommendation": "Keep" if dirty else "Delete", "status": "open", "answer": None, "note": None,
        "snapshot": card_snapshot(facts, desc),
        "created": pa.now(), "answered": None}))
    print("{}: opened card {} (recommends {})".format(task, cid, "Keep" if dirty else "Delete"))


CARRIED = ("previousAnswer", "previousNote", "previousAnswered")


def carried(old):
    """What a card asked again keeps of the withdrawn card it replaces: the answer, the person's words and when they
    were given, so the person and the Project Agent see what was said (t-046 J1b). Nothing from any other card."""
    if not isinstance(old, dict) or old.get("status") != "withdrawn" or old.get("previousAnswer") is None:
        return {}
    return {k: old[k] for k in CARRIED if old.get(k) is not None}


def remove_command(task, job, main):
    return "pa-worktree remove {}{}".format(task, "" if main else " --job " + job)


# ---- one Delete card for many worktrees (t-168 Part A) ----------------------------------------------------------
# Two or more candidates go on one card d-wt-batch-<n>, one item per worktree. An item answers for its worktree the way
# a single card does: card_for sees it through item_view, with a single card's keys, so check and remove read it the
# same way. pa-worktree remove --card <id> removes each covered item through the single card's checks (remove_one).

BATCH = "d-wt-batch-"
BATCH_MAX = 60  # worktrees on one card, at most; the rest wait for the next card
BATCH_CONTEXT_MAX = 6000  # characters of a batch card's context (t-168 Q1: 48 lines fit; a single card keeps CONTEXT_MAX)
BATCH_LINE = 200  # characters of one worktree's line on it
HELD_LINE = 400  # characters of one of the person's earlier answers shown under it (the item's `held` keeps them all)
WORKTREE_LOCK = ".worktree.lock"
WORKTREE_LOCK_WAIT = 30  # seconds a second pa-worktree remove --card or --standing waits for the first


def is_batch_id(cid):
    return isinstance(cid, str) and cid.startswith(BATCH) and cid[len(BATCH):].isdigit()


def task_key(task):
    """t-9 before t-10: task ids in natural order."""
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", str(task))]


def worktrees_cards(directory, listing=None):
    """{id: card} of the batch cards in decisions/ and decisions-archive/ (decisions/ wins)."""
    cards = {}
    for cid in sorted(c for c in (card_listing(directory) if listing is None else listing) if is_batch_id(c)):
        card = pa.read_json_safe(pa.card_file(directory, cid))
        if isinstance(card, dict) and card.get("kind") == "worktrees" and isinstance(card.get("items"), list):
            cards[cid] = card
    return cards


def next_batch_id(directory, listing=None):
    """The first d-wt-batch-<n> above every one held in decisions/ or the archive: ids are never reused. listing: the
    run's card_listing (a batch card is only ever added by this run after it)."""
    used = [int(c[len(BATCH):]) for c in (card_listing(directory) if listing is None else listing) if is_batch_id(c)]
    return BATCH + str(1 + max(used or [0]))


def answer_scope(card):
    """The scope of the option the batch card was answered with: lossless, all or keep; None when it matches none."""
    answer = str(card.get("answer") or "").strip().lower()
    return next((o.get("scope") for o in card.get("options") or [] if str(o.get("label") or "").strip().lower() == answer), None)


def item_view(cid, card, i):
    """Item i of batch card cid in the shape of a single card (id, task, worktree, branch, rule, tasks, snapshot, status,
    answer, note, answered, answeredBy, worktreeRemoved), plus `batch` and `item`. Its status: withdrawn when the item
    or the card is; open while the card is; answered when the card's answer covers the item (item_covered); else
    withdrawn: the next check asks about it again (Q3). The person's holds are read from the card itself (worktree_holds)."""
    item = card["items"][i] if isinstance(card["items"][i], dict) else {}
    status, answer = card.get("status"), card.get("answer")
    if item.get("withdrawn") or status not in ("open", "answered"):
        status = "withdrawn"
    elif status == "answered" and not item_covered(card, item):
        status, answer = "withdrawn", None
    view = {"id": cid, "batch": cid, "item": i, "task": item.get("task"), "worktree": item.get("path"),
            "branch": item.get("branch"), "job": item.get("job"), "main": item.get("main"), "rule": item.get("rule"),
            "tasks": item.get("tasks"), "snapshot": item.get("snapshot") or {}, "status": status,
            "answer": answer if status == "answered" else None, "note": card.get("note"), "answered": card.get("answered"),
            "answeredBy": card.get("answeredBy"), "created": card.get("created"), "worktreeRemoved": item.get("removed")}
    if card.get("other") is True and status == "answered":
        view["other"] = True
    return view


def item_covered(card, item):
    """Whether answered batch card `card`'s answer covers `item`: Other, Keep all, Delete all, an answer that is none of
    its options (fail closed: it then holds), or Delete the lossless ones for a lossless item."""
    return pa.is_other(card) or answer_scope(card) != "lossless" or bool(item.get("lossless"))


def batch_views(directory, task, listing=None):
    """[(id, view)] of the task's items on batch cards."""
    out = []
    for cid, card in worktrees_cards(directory, listing).items():
        for i, item in enumerate(card["items"]):
            if isinstance(item, dict) and item.get("task") == task and isinstance(item.get("path"), str) and item["path"].strip():
                out.append((cid, item_view(cid, card, i)))
    return out


def update_item(directory, view, **fields):
    """Writes fields into the item a view stands for, on its batch card as the file has it now."""
    path = pa.card_file(directory, view["batch"])
    card = pa.read_json(path)
    card["items"][view["item"]].update(fields)
    pa.write_card(directory, path, card)
    return card


def remove_for(card, owner):
    """The command that removes the worktree of this card: remove --card for a batch item."""
    return "pa-worktree remove --card {}".format(card["batch"]) if card.get("batch") else remove_command(*owner)


def own_withdrawn_single(directory, row, task, entry, listing=None):
    """The id of the worktree's newest withdrawn single card (asked again under it), or None."""
    mine = [(str(c.get("answered") or c.get("created") or ""), cid) for cid, c in task_cards(directory, task, listing).items()
            if card_tree(c) == row["path"] and c.get("status") == "withdrawn" and not card_spent(c, entry)
            and not branch_differs(c, row.get("facts"))]
    return max(mine)[1] if mine else None


def candidate_why(row, profile, owner, card=None):
    """(why not, rule, desc): why the worktree is not a candidate for a Delete card now (None when it is), the rule it is
    one under (a or b) and rule (b)'s description. card: the item it is on, whose task set and rule must still stand.
    The same tests as pa-worktree check's branches."""
    facts = row["facts"]
    task, job, main = owner
    if not facts:
        return "gone or not a git worktree", None, None
    if facts.get("unreadable"):
        return "could not read it ({})".format(facts["unreadable"]), None, None
    if facts.get("main"):
        return "the project's main checkout", None, None
    if row["keep"]:
        return "keep is set", None, None
    if not main and not in_worktrees_folder(row["path"], profile):
        return "not under a worktrees folder", None, None
    mismatch = card and card_mismatch(row, card, False)
    if mismatch:
        return mismatch, None, None
    still = tasks_still(row)
    if still:
        if not under_rule_b(row, card):
            return "tasks still " + still, None, None
        over, why, desc = job_over(row, facts, profile)
        return (None if over else "job no longer over: " + why), "b", desc
    live = [w["address"] for w in row["workers"] if w["fate"] == "live"]
    if live:
        return "in use by " + ", ".join(live), None, None
    if row["others"]:
        return "another terminal is working in it: " + ", ".join(row["others"]), None, None
    return None, "a", None


def worktree_item(directory, cand, profile, on_card):
    """(item, WORKTREE*.md text) of one candidate on batch card on_card: what open_worktree_card would record for it.
    The md file is the one its single card would use (md_for of that card's id), written once the card is fitted."""
    row, owner, rule, gone = cand["row"], cand["owner"], cand["rule"], cand["gone"]
    task, job, main = owner
    facts = dict(row["facts"], holderNote=holder_note(row, profile))  # t-192 R6.4: its Deleting line names the holder
    _named, extra = card_extra(row, owner, rule)
    desc = cand.get("desc") or describe_safely(row, facts, profile)
    loss, dirty = card_loss(desc, facts)
    single = cand.get("single")
    cid = single[0] if single else cand.get("again") or new_card_id(directory, task, job, main)
    cost = []
    if facts["uncommitted"]:
        cost.append("uncommitted files discarded")
    lost = (loss["lost"] if loss else facts["unpushed"]) if facts.get("removesBranches") else 0
    if lost != 0:
        cost.append("{}: branches deleted, {}".format(clean(facts.get("kind") or "worktree"), "lost commits unknown" if lost is None else
                                                       "{} commit{} LOST".format(lost, "" if lost == 1 else "s")))
    item = {"path": row["path"], "branch": facts.get("branch") or None, "task": task, "job": job, "main": main, "rule": rule,
            "tasks": row_tasks(row), "lossless": not dirty, "snapshot": card_snapshot(facts, desc), "md": md_for(task, cid),
            "removed": None, "skipped": None, "withdrawn": None, "cost": "; ".join(cost) or None,
            "counts": {"commits": sum(r["commitsTotal"] for r in desc["repos"]), "merged": sum(r["merged"] for r in desc["repos"]),
                       "pushed": sum(r["pushed"] for r in desc["repos"]), "uncommitted": facts["uncommitted"]}}
    return item, worktree_md(desc, row, facts, gone, on_card, extra)


def worktree_line(item):
    """One worktree on a batch card: task, job, branch, commits (merged, pushed), uncommitted, and what deleting costs."""
    c = item.get("counts") or {}
    n = c.get("commits", 0)
    parts = ["{} {}".format(clean(item.get("task")), cut(clean(item.get("job")), 40)), cut(clean(item.get("branch") or "?"), 60),
             "{} commit{}, {} merged, {} pushed".format(n, "" if n == 1 else "s", c.get("merged", 0), c.get("pushed", 0)),
             "{} uncommitted".format(c["uncommitted"]) if c.get("uncommitted") else "clean"]
    if item.get("cost"):
        parts.append(clean(item["cost"]))
    lines = ["  " + cut(" · ".join(parts), BATCH_LINE)]
    for h in item.get("held") or []:  # the person's earlier words on this worktree, each hold on its own line
        said = ["{!r}".format(clean(h["answer"]))] if h.get("answer") is not None else []
        if h.get("note"):
            said.append('note "{}"'.format(clean(h["note"])))
        lines.append("    " + cut("you said before, on card {}: {}".format(clean(h.get("card")), ", ".join(said)), HELD_LINE))
    return "\n".join(lines)


def worktrees_card_text(items, total):
    """(shown, context) of a batch card: the first items that fit BATCH_MAX and BATCH_CONTEXT_MAX, grouped by whether
    deleting loses something; `total` candidates in all, the rest named as waiting."""
    def text(shown):
        n, more = len(shown), total - len(shown)
        lines = ["{} worktrees whose jobs are over. Deleting removes each folder; a branch is deleted when every commit is merged "
                 "and pushed, else kept, unless a line says otherwise. "
                 "Files git ignores are not counted.".format(n)]
        kept, lossy = [i for i in shown if i["lossless"]], [i for i in shown if not i["lossless"]]
        if kept:
            lines += ["Nothing is lost ({}):".format(len(kept))] + [worktree_line(i) for i in kept]
        if lossy:
            lines += ["Deleting loses something ({}):".format(len(lossy))] + [worktree_line(i) for i in lossy]
        if more:
            lines.append("… and {} more wait for the next card, once this one is answered".format(more))
        lines.append("Each worktree's full list: WORKTREE*.md in its task's evidence folder.")
        return "\n".join(lines)
    shown = list(items[:BATCH_MAX])
    while len(shown) > 2 and len(text(shown)) > BATCH_CONTEXT_MAX:
        shown.pop()
    return shown, text(shown)


def batch_options(shown):
    n = len(shown)
    k = sum(1 for i in shown if i["lossless"])
    m = n - k
    options = []
    if k and m:
        options.append({"label": "Delete the {} where nothing is lost".format(k), "scope": "lossless",
                        "detail": "Remove the {} listed under Nothing is lost. The {} under Deleting loses something stay, and the "
                                  "next card asks about them again.".format(k, m)})
    options.append({"label": "Delete all {}".format(n), "scope": "all",
                    "detail": "Remove every worktree listed above{}.".format(
                        ", including the {} listed under Deleting loses something: their uncommitted files and unpushed commits "
                        "are lost".format(m) if m else "")})
    options.append({"label": "Keep all {}".format(n), "scope": "keep",
                    "detail": "Leave them all. None of them is asked about again."})
    # Option 1 when shown, else Delete all; Keep all when every one loses something (as a single card recommends Keep).
    return options, options[0]["label"] if k else options[-1]["label"]


def open_worktrees_card(directory, candidates, profile, old=None, listing=None):
    """Writes one batch card for two or more candidates (R1-R3, R10): their items in task order, then job; the open
    single cards of the ones on it are withdrawn (moved to it, R8). old: the batch card it replaces (carried)."""
    ordered = sorted(candidates, key=lambda c: (task_key(c["owner"][0]), str(c["owner"][1])))
    cid = next_batch_id(directory, listing)
    if listing is not None:
        listing.add(cid)
    built = [worktree_item(directory, c, profile, cid) for c in ordered[:BATCH_MAX]]
    holds = worktree_holds(directory, listing)
    for (item, _text), c in zip(built, ordered):
        item["held"] = item_held(holds, c["row"])  # every hold's words, under the worktree they hold (round 6)
    shown, context = worktrees_card_text([b[0] for b in built], len(ordered))
    for (item, text_), c in zip(built[:len(shown)], ordered):
        try:
            pa.write_text(os.path.join(directory, "evidence", item["task"], item["md"]), text_)
        except OSError:
            pass
    options, recommendation = batch_options(shown)
    n = len(shown)
    path = pa.card_file(directory, cid)
    # The person's standing words on any worktree on it, from the store, whichever card they were given on (round 5).
    carry = held_carry(holds, [c["row"] for c in ordered[:n]]) or carried(old)
    pa.write_card(directory, path, dict(carry, **{
        "id": cid, "kind": "worktrees", "task": None, "question": "Delete {} worktrees whose jobs are over?".format(n),
        "context": context, "options": options, "recommendation": recommendation, "status": "open", "answer": None,
        "note": None, "items": shown, "more": len(ordered) - n, "created": pa.now(), "answered": None}))
    for c in ordered[:n]:
        if c.get("single"):
            scid, spath, scard = c["single"]
            withdraw_card(directory, spath, scard, c["owner"][0], "moved to card " + cid)
    more = len(ordered) - n
    print("opened card {} ({} worktrees{})".format(cid, n, "; {} more wait for the next card".format(more) if more else ""))
    return cid


def withdraw_batch(directory, cid, card, why, by="pa-worktree check"):
    card.update(status="withdrawn", answer=None, note="Withdrawn by {}: {}".format(by, why), answered=pa.now(),
                answeredBy=by + ": withdrawn")
    pa.write_card(directory, pa.card_file(directory, cid), card)
    append_journal(directory, {"by": "agent" if by_an_agent() else "person", "trigger": "worktree", "task": None,
                               "why": "withdrew card {}: {}".format(cid, why)})
    print("withdrew card {}: {}".format(cid, why))


def who(owner):
    task, job, main = owner
    return clean(task) if main and job == "main" else "{} {}".format(clean(task), clean(job))


def item_who(item):
    return who((item.get("task"), item.get("job"), bool(item.get("main"))))


def finish_candidates(directory, candidates, leaving, profile, open_cards, listing=None):
    """pa-worktree check, after its loop: the candidates on one card (R1, R8, R9, R10). An open batch card stays while
    its worktrees are the candidates (new ones wait when it is full); else it is withdrawn and one card is opened for all
    of them, a single card when only one is left."""
    old = None
    for cid, card in sorted(worktrees_cards(directory, listing).items(), key=lambda x: task_key(x[0])):
        if card.get("status") != "open":
            continue
        keys = {(c["row"]["path"], c["row"]["facts"].get("branch") or None) for c in candidates}
        left = [(i, leaving.get(os.path.realpath(i.get("path") or "")) or "no longer a candidate") for i in card["items"]
                if (os.path.realpath(i.get("path") or ""), i.get("branch")) not in keys]
        joined = [c for c in candidates if not (c.get("view") and c["view"]["batch"] == cid)]
        if not left and (not joined or card.get("more")):
            print("card {} is open ({} worktrees)".format(cid, len(card["items"])))
            for c in joined:
                print("{}: candidate; waits for the next card, once card {} is answered".format(c["owner"][0], cid))
            return
        why = "the worktrees changed: " + ", ".join(["+ " + who(c["owner"]) for c in joined] +
                                                   ["- {} ({})".format(item_who(i), w) for i, w in left])
        if not open_cards:
            print("card {} is open, but {}; --open-cards withdraws it and opens it again".format(cid, why))
            return
        withdraw_batch(directory, cid, card, why)
        old = card
    if not candidates:
        return
    if len(candidates) == 1:
        c = candidates[0]
        task = c["owner"][0]
        if c.get("single"):
            print("{}: card {} is open".format(task, c["single"][0]))
        elif not open_cards:
            print(c["line"] + "; --open-cards asks the person")
        else:
            open_worktree_card(directory, c["row"], c["row"]["facts"], profile, c["owner"], c["rule"], c["gone"], c.get("desc"),
                               cid=c.get("again"))
        return
    if not open_cards:
        for c in candidates:
            print("{}; --open-cards puts it on one card with the other {}".format(
                c["line"] if not c.get("single") else "{}: card {} is open".format(c["owner"][0], c["single"][0]), len(candidates) - 1))
        return
    open_worktrees_card(directory, candidates, profile, old, listing)


# ---- worktrees no task records (t-020 J4, spec R4, R5) ---------------------------------------------
# Found only under the scanned `worktrees` folders, listed once on a card d-wt-unrecorded-<n>, never deleted.

UNRECORDED_CAP = 20  # worktrees described and listed on one card; the rest go on the next one
UNRECORDED = "d-wt-unrecorded-"


def scan_folders(agent, rows):
    """The folders pa-worktree check looks in for worktrees no task records: <project>/worktrees (project from
    agent.json), and for each recorded worktree its nearest ancestor named `worktrees`, at any depth (how a workspace's
    own worktrees folder comes in, also from a package path inside one, t-131). Realpaths of existing folders, the
    project's first, no repeats."""
    found = []
    project = agent.get("project") if isinstance(agent.get("project"), str) and agent.get("project").strip() else None
    if project:
        found.append(os.path.join(os.path.expanduser(project), "worktrees"))
    for row in sorted(rows.values(), key=lambda r: r["path"]):
        for path in (row.get("given") or row["path"], row["path"]):
            folder = os.path.dirname(os.path.normpath(os.path.expanduser(str(path))))
            while folder and folder != os.path.dirname(folder):
                if os.path.basename(folder) == "worktrees":
                    found.append(folder)
                    break
                folder = os.path.dirname(folder)
    out = []
    for folder in found:
        real = os.path.realpath(folder)
        if os.path.isdir(real) and real not in out:
            out.append(real)
    return out


def recorded_paths(directory, agent):
    """(paths, unreadable): every worktree path any task records (removed entries too) in this space and in every space
    under pa.ROOT/spaces whose agent.json names the same project folder; unreadable: [(space, why)] for a same-project
    space whose ledger cannot be read, or whose agent.json is there but cannot be read (t-131: it may be on this project),
    as [(space, why, "ledger" | "agent.json")] (D5: then nothing is listed as unrecorded)."""
    folders, unreadable = project_folders(directory, agent)
    paths = set()
    for folder in folders:
        try:
            ledger = pa.read_json(os.path.join(folder, "ledger.json"), {}) or {}
            tasks = ledger.get("tasks") or [] if isinstance(ledger, dict) else None
            if not isinstance(tasks, list):
                raise ValueError("no task list")
        except (OSError, ValueError) as e:
            unreadable.append((os.path.basename(folder), clean(e)[:200], "ledger"))
            continue
        for task in tasks:
            if not isinstance(task, dict):
                continue
            paths.update(t["path"] for t in pa.task_worktrees(task))
            entries = task.get("worktrees") if isinstance(task.get("worktrees"), list) else []
            paths.update(os.path.realpath(os.path.expanduser(e["path"])) for e in entries
                         if isinstance(e, dict) and isinstance(e.get("path"), str) and e["path"].strip())
    return sorted(paths), unreadable


def project_folders(directory, agent):
    """(folders, unreadable): this space's folder and every space under pa.ROOT/spaces whose agent.json names the same
    project folder; unreadable as recorded_paths gives it, for an agent.json that cannot be read."""
    project = agent.get("project") if isinstance(agent.get("project"), str) and agent.get("project").strip() else None
    project = os.path.realpath(os.path.expanduser(project)) if project else None
    folders, unreadable = [directory], []
    root = os.path.join(pa.ROOT, "spaces")
    if project:
        for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
            folder = os.path.join(root, name)
            if os.path.realpath(folder) == os.path.realpath(directory):
                continue
            if not os.path.lexists(os.path.join(folder, "agent.json")):
                continue  # not a space
            try:
                other = pa.read_json(os.path.join(folder, "agent.json"))
                if not isinstance(other, dict):
                    raise ValueError("not a JSON object")
            except (OSError, ValueError) as e:
                unreadable.append((name, clean(e)[:200], "agent.json"))
                continue
            theirs = other.get("project")
            if isinstance(theirs, str) and theirs.strip() and os.path.realpath(os.path.expanduser(theirs)) == project:
                folders.append(folder)
    return folders, unreadable


def guess_task(name, ids):
    """The task a worktree's folder name seems to belong to: its leading t-<digits><letter> if that is a task id of
    this ledger, else its leading t-<digits> if that is one, else None (then it is taken as the person's, R5)."""
    for pattern in (r"t-\d+[a-z]?", r"t-\d+"):
        m = re.match(pattern, name)
        if m and m.group() in ids:
            return m.group()
    return None


def unrecorded_cards(directory):
    """{card id: card} of every d-wt-unrecorded-<n> card, in decisions/ and in decisions-archive/ (t-054)."""
    cards = {}
    for sub in (pa.ARCHIVE, "decisions"):  # decisions/ last: it wins over an archived copy
        folder = os.path.join(directory, sub)
        for name in os.listdir(folder) if os.path.isdir(folder) else []:
            if re.fullmatch(re.escape(UNRECORDED) + r"[0-9]+\.json", name):
                card = pa.read_json_safe(os.path.join(folder, name))
                if isinstance(card, dict):
                    cards[name[:-len(".json")]] = card
    return cards


def looks_tree(path, profile):
    """Whether a folder looks like a worktree without asking git (t-131: the person's own are never probed): a `.git`
    file (a linked git worktree) or the profile's worktree marker."""
    marker = ((profile or {}).get("worktree") or {}).get("marker")
    return os.path.isfile(os.path.join(path, ".git")) or bool(marker and os.path.exists(os.path.join(path, marker)))


def scan_unrecorded(directory, agent, profile, ledger, rows, cards=None):
    """What is under the scanned folders that no task records. {"items": [...], "more": [...], "empty": {folder: n},
    "persons": {folder: n}, "unreadable": [(space, why, what)], "folders": [...]}. A folder's child counts when it is a
    worktree (worktree_facts: a linked git worktree or the profile's kind; never a main checkout), or for a container,
    each direct child that is one; an empty child folder is counted in "empty"; anything else (and every symlink) is
    passed over. A child whose name guesses no task of this ledger is the person's: counted by looks_tree only, never
    probed with git or the profile's script (t-131). Recorded: a recorded path equals it, lies inside it, or holds it
    from inside the scanned folder (a task that records the project folder hides nothing, t-131). Paths on an answered
    d-wt-unrecorded card are left out. Items carry path, guess and job only; describe_unrecorded adds the rest for a card
    (t-131: only when one is written). The first UNRECORDED_CAP are "items", the rest "more". cards: unrecorded_cards'."""
    folders = scan_folders(agent, rows)
    recorded, unreadable = recorded_paths(directory, agent)
    ids = {t.get("id") for t in ledger.get("tasks") or [] if isinstance(t, dict) and isinstance(t.get("id"), str)}
    answered = set()
    for card in (unrecorded_cards(directory) if cards is None else cards).values():
        if card.get("status") == "answered":
            answered.update(os.path.realpath(p) for p in card.get("paths") or [] if isinstance(p, str))
    out = {"items": [], "more": [], "empty": {}, "persons": {}, "unreadable": unreadable, "folders": folders}
    found = []
    for folder in folders:
        def known(real):
            return real in answered or any(inside(r, real) or (inside(real, r) and r != folder and inside(r, folder)) for r in recorded)
        for name in sorted(os.listdir(folder)):
            child = os.path.join(folder, name)
            if os.path.islink(child) or not os.path.isdir(child):
                continue
            guess = guess_task(name, ids)
            if guess is None:  # the person's: counted, never probed
                try:
                    inner = sorted(os.listdir(child))
                except OSError:
                    continue
                if not inner:
                    out["empty"][folder] = out["empty"].get(folder, 0) + 1
                    continue
                trees = [child] if looks_tree(child, profile) else [
                    os.path.join(child, sub) for sub in inner if not os.path.islink(os.path.join(child, sub))
                    and os.path.isdir(os.path.join(child, sub)) and looks_tree(os.path.join(child, sub), profile)]
                n = sum(1 for t in trees if not known(os.path.realpath(t)))
                if n:
                    out["persons"][folder] = out["persons"].get(folder, 0) + n
                continue
            facts = worktree_facts(child, profile)
            if facts is not None:
                trees = [] if facts.get("main") else [(child, facts)]
            else:
                trees = []
                try:
                    inner = sorted(os.listdir(child))
                except OSError:
                    continue
                if not inner:
                    out["empty"][folder] = out["empty"].get(folder, 0) + 1
                    continue
                for sub in inner:
                    path = os.path.join(child, sub)
                    if os.path.islink(path) or not os.path.isdir(path):
                        continue
                    sub_facts = worktree_facts(path, profile)
                    if sub_facts is not None and not sub_facts.get("main"):
                        trees.append((path, sub_facts))
            for path, facts_ in trees:
                real = os.path.realpath(path)
                if known(real):
                    continue
                found.append({"folder": folder, "name": name, "path": real, "facts": facts_, "guess": guess,
                              "job": pa.legacy_job(guess, path)})
    if unreadable:
        return out  # D5: another space's work is never listed as unrecorded
    out["items"], out["more"] = found[:UNRECORDED_CAP], found[UNRECORDED_CAP:]
    return out


def describe_unrecorded(scan, agent, profile):
    """The card's lines for scan["items"]: kind, branch, commits, merged, pushed, uncommitted and the terminals in each
    (git calls: only for a card about to be written, t-131)."""
    live = pa.live_terminals() or {}
    items = []
    for f in scan["items"]:
        facts_ = f["facts"]
        try:
            desc = describe_worktree({"path": f["path"], "tasks": [], "titles": {}, "states": {}}, facts_, profile)
        except Exception as e:  # noqa: BLE001 - describing never stops the scan
            desc = {"repos": [], "kind": facts_.get("kind") or "git worktree", "error": clean(e)[:200]}
        repos = desc.get("repos") or []
        items.append({
            "folder": f["folder"], "name": f["name"], "path": f["path"], "kind": clean(desc.get("kind") or facts_.get("kind") or "git worktree"),
            "branch": clean(facts_.get("branch") or (repos[0]["branch"] if repos else None) or "?"),
            "commits": sum(r["commitsTotal"] for r in repos), "merged": sum(r["merged"] for r in repos),
            "pushed": sum(r["pushed"] for r in repos), "uncommitted": facts_.get("uncommitted"),
            "terminals": sorted(a for a, info in live.items() if a != agent.get("terminal") and (
                inside(info.get("cwd"), f["path"]) or inside((info.get("foreground") or {}).get("cwd"), f["path"]))),
            "guess": f["guess"], "job": f["job"]})
    return dict(scan, items=items)


def unrecorded_card(card_id, scan, old=None):
    """The card that lists the worktrees no task records: Record as guessed or Leave them alone; no Delete option and no
    `worktree` field, so neither pa-worktree remove nor the panel's worktree path can take it (AC4.2)."""
    items = scan["items"]
    lines = []
    for folder in dict.fromkeys(i["folder"] for i in items):
        lines.append("Worktrees under {} that no task records. Nothing on this card deletes anything.".format(clean(folder)))
        for i in (i for i in items if i["folder"] == folder):
            lines.append("{} ({}): branch {}, {} commits ({} merged, {} pushed), {} uncommitted; terminals in it: {}; guess: {}, job {}".format(
                clean(i["path"]), i["kind"], i["branch"], i["commits"], i["merged"], i["pushed"],
                "unknown" if i["uncommitted"] is None else i["uncommitted"], ", ".join(clean(t) for t in i["terminals"]) or "none",
                clean(i["guess"]), clean(i["job"])))
    if scan["more"]:
        lines.append("… and {} more (more than {}; listed on the next card)".format(len(scan["more"]), UNRECORDED_CAP))
    first = clean(items[0]["name"])
    lines.append('In the note you can say per folder: "{0}: {1}" or "{0}: leave".'.format(first, clean(items[0]["guess"])))
    card = {"id": card_id, "task": None, "kind": "unrecorded-worktrees", "paths": [i["path"] for i in items],
            "items": [{k: i[k] for k in ("path", "kind", "branch", "commits", "merged", "pushed", "uncommitted", "terminals", "guess", "job")}
                      for i in items],
            "question": "Record these worktrees no task records under the tasks they seem to belong to?",
            "context": "\n".join(lines),
            "options": [{"label": "Record as guessed", "detail": "Add each to its guessed task's worktrees with that job; it then gets its own "
                                                                 "Delete/Keep card when its job is over."},
                        {"label": "Leave them alone", "detail": "Record nothing; these are not listed again."}],
            "recommendation": "Record as guessed", "status": "open", "answer": None, "note": None,
            "created": (old or {}).get("created") or pa.now(), "answered": None}
    if old:
        card["updated"] = pa.now()
    return card


def check_unrecorded(directory, agent, profile, rows, open_cards):
    """pa-worktree check's part for worktrees no task records (R4, R5): lines always; the card only with --open-cards.
    At most one open card: updated in place when the set changes, withdrawn when nothing is left."""
    ledger = pa.read_json(os.path.join(directory, "ledger.json"), {}) or {}
    cards = unrecorded_cards(directory)
    scan = scan_unrecorded(directory, agent, profile, ledger, rows, cards)
    for space, why, what in scan["unreadable"]:
        print("could not read the {} of space {} ({}); no worktree is listed as unrecorded until it can be read".format(what, clean(space), why))
    for folder in scan["folders"]:
        if scan["empty"].get(folder):
            print("{} empty folders under {} (left by earlier removals)".format(scan["empty"][folder], clean(folder)))
        if scan["persons"].get(folder):
            print("{} worktrees under {} not named after a task of this ledger: left alone, not listed".format(scan["persons"][folder], clean(folder)))
    if scan["unreadable"]:
        return
    number = lambda cid: int(cid[len(UNRECORDED):])
    is_open = sorted((cid for cid, c in cards.items() if c.get("status") == "open"), key=number)
    cid = is_open[0] if is_open else None
    old = cards.get(cid) if cid else None
    listed = scan["items"] + scan["more"]
    if not open_cards:
        on_card = set((old or {}).get("paths") or [])
        for i in listed:
            print("unrecorded: {} (guess {}, job {}); {}".format(clean(i["path"]), clean(i["guess"]), clean(i["job"]),
                  "on card " + cid if i["path"] in on_card else "--open-cards lists it on a card"))
        if old and sorted(old.get("paths") or []) != sorted(i["path"] for i in scan["items"]):
            print("card {} is open, but the worktrees no task records changed; --open-cards {}".format(
                cid, "updates it" if scan["items"] else "withdraws it"))
        return
    if not scan["items"]:
        if old:
            path = pa.card_file(directory, cid)
            why = "no unrecorded worktree is left"
            old.update(status="withdrawn", answer=None, note="Withdrawn by pa-worktree check: " + why, answered=pa.now(),
                       answeredBy="pa-worktree check: withdrawn")
            pa.write_card(directory, path, old)
            append_journal(directory, {"by": "agent" if by_an_agent() else "person", "trigger": "worktree", "task": None,
                                       "why": "withdrew card {}: {}".format(cid, why)})
            print("withdrew card {}: {}".format(cid, why))
        return
    n = len(scan["items"])
    count = "{} worktree{} no task records".format(n, "" if n == 1 else "s")
    if old and sorted(old.get("paths") or []) == sorted(i["path"] for i in scan["items"]):
        print("card {} is open ({})".format(cid, count))
        return
    if not old:
        cid = UNRECORDED + str(1 + max([number(c) for c in cards] or [0]))
    pa.write_card(directory, pa.card_file(directory, cid), unrecorded_card(cid, describe_unrecorded(scan, agent, profile), old))
    print("{} card {} ({})".format("updated" if old else "opened", cid, count))


def cmd_worktree(args):
    record = find_task(os.getcwd())
    space = pa.local_id(args.space or (record or {}).get("space") or space_from_terminal() or "")
    if not space:
        raise pa.Problem("pass --space <id>, or run this from the Project Agent's terminal")
    directory, agent, profile = load_context(space)
    rows = worktree_state(directory, agent, profile)
    entries = ledger_tasks(directory)
    listing = card_listing(directory)  # both card folders, listed once per run (t-131)
    if args.action == "check":
        candidates, leaving = [], {}  # t-168: every candidate goes on one card after the loop; why an open item left it
        for row in rows.values():
            owner = card_owner(row)
            task, job, main = owner
            facts = row["facts"]
            # The worktree's own card only (t-073): one for another path, an earlier worktree here or another branch never counts.
            cid, path, card = card_for(directory, row, task, entries.get(task), listing)
            again = None
            if card and card.get("status") == "withdrawn":
                # asked again under its id once its job is over or every task is done or parked; a batch item under the
                # worktree's own withdrawn single card, if it has one
                again = own_withdrawn_single(directory, row, task, entries.get(task), listing) if card.get("batch") else cid
                card = None
            fates = [w["fate"] for w in row["workers"]]
            still = tasks_still(row)
            mismatch = card and card_mismatch(row, card, card.get("status") == "answered")
            gone = ", ".join("{} ({})".format(clean(w["name"] or w["address"]), w["fate"]) for w in row["workers"]) or "no workers recorded"
            cand = {"row": row, "owner": owner, "gone": gone, "again": again, "single": None, "view": None, "desc": None}
            if card and card.get("batch") and card.get("status") == "open":
                why, rule, desc = candidate_why(row, profile, owner, card)
                if why:
                    leaving[row["path"]] = why
                    print("{}: on card {}, but {}".format(task, card["id"], why))
                else:
                    candidates.append(dict(cand, rule=rule, desc=desc, view=card))
                continue
            if not facts:
                print("{}: gone or not a git worktree ({})".format(task, row["path"]))
            elif facts.get("unreadable"):
                print("{}: could not read {} ({}); asked once it can be read".format(task, row["path"], facts["unreadable"]))
            elif facts.get("main"):
                print("{}: {} is the project's main checkout; never removed".format(task, row["path"]))
            elif row["keep"]:
                print("{}: kept (keepWorktree is set)".format(task))
            elif not main and not in_worktrees_folder(row["path"], profile):
                print("{}: {} is not under a worktrees folder; not offered".format(task, row["path"]))
            elif card and card.get("status") == "answered" and pa.is_other(card):
                # The person's own words: never read as Delete or Keep, and nothing is opened.
                print("{}: card {} answered Other (\"{}\"): nothing is removed; not asked again unless the card is withdrawn".format(
                      task, card["id"], cut(clean(card.get("note")), 200)))
            elif card and card.get("status") == "answered":
                delete = str(card.get("answer", "")).lower().startswith("delete")
                if card.get("batch") and str(card.get("note") or "").strip():  # Delete or Keep all with a note (R7)
                    print("{}: card {} answered {!r} with a note: run `{}`; it removes nothing, withdraws the card, and you ask "
                          "the person to say \"keep tNN\" for any to keep".format(task, card["id"], card.get("answer"), remove_for(card, owner)))
                elif delete and under_rule_b(row, card) and not mismatch:
                    print("{}: card {} answered {!r}: run `{}`; it re-checks".format(task, card["id"], card.get("answer"), remove_for(card, owner)))
                elif delete and still:
                    print("{}: card {} answered {!r}, but tasks still {}; pa-worktree remove refuses until every task using it is "
                          "done or parked".format(task, card["id"], card.get("answer"), still))
                elif delete and mismatch:
                    print("{}: card {} answered {!r}, but {}; pa-worktree remove refuses it and withdraws it, and the next "
                          "--open-cards asks again".format(task, card["id"], card.get("answer"), mismatch))
                else:
                    print("{}: card {} answered {!r}{}".format(task, card["id"], card.get("answer"),
                          ": run `{}`".format(remove_for(card, owner)) if delete else ": kept, not asked again"))
            elif card and under_rule_b(row, card) and not mismatch:
                over, why, desc = job_over(row, facts, profile)
                if over:
                    candidates.append(dict(cand, rule="b", desc=desc, single=(cid, path, card)))
                elif not args.open_cards:
                    print("{}: card {} is open, but the job is no longer over ({}); --open-cards withdraws it".format(task, card["id"], why))
                else:
                    withdraw_card(directory, path, card, task, "the job is no longer over: " + why)
            elif card and still and not args.open_cards:
                print("{}: card {} is open, but tasks still {}; --open-cards withdraws it".format(task, card["id"], still))
            elif card and still:
                # A task was reopened after the card opened: the question no longer stands.
                withdraw_card(directory, path, card, task,
                              "tasks still {}; it is asked again once every task using the worktree is done or parked".format(still))
            elif card and mismatch and not args.open_cards:
                print("{}: card {} is open, but {}; --open-cards withdraws it".format(task, card["id"], mismatch))
            elif card and mismatch:
                withdraw_card(directory, path, card, task, mismatch)
            elif card:
                candidates.append(dict(cand, rule=card.get("rule") or ("b" if still else "a"), single=(cid, path, card)))
            elif still and not under_rule_b(row):
                print("{}: tasks still {}; asked once every task using it is done or parked; rule (b) is only for a worktree "
                      "one task records".format(task, still))
            elif still:
                over, why, desc = job_over(row, facts, profile)
                if not over:
                    print("{}: tasks still {}; asked once every task using it is done or parked; job {} not over yet ({})".format(
                        task, still, clean(job), why))
                else:
                    candidates.append(dict(cand, rule="b", desc=desc,
                                           line="{}: candidate, job {} is over (every commit merged)".format(task, clean(job))))
            elif "live" in fates:
                print("{}: in use by {}".format(task, ", ".join(w["address"] for w in row["workers"] if w["fate"] == "live")))
            elif row["others"]:
                print("{}: another terminal is working in it ({})".format(task, ", ".join(row["others"])))
            else:
                # Every task is done or parked and nothing runs inside: a candidate, however its workers ended.
                candidates.append(dict(cand, rule="a", line="{}: candidate, every task done or parked ({})".format(task, gone)))
        finish_candidates(directory, candidates, leaving, profile, args.open_cards, listing)
        withdraw_stale(directory, rows, entries, args.open_cards, listing)
        if not rows:
            print("No task in the ledger has a worktree.")
        check_unrecorded(directory, agent, profile, rows, args.open_cards)
        return
    # remove: only what an answered card allows, re-checked now.
    if getattr(args, "card", None):
        return remove_card(directory, agent, profile, args.card)
    if getattr(args, "standing", None):
        return remove_standing(directory, space, agent, profile, args.standing)
    row = pick_row(rows, args.task, args.job)
    owner = card_owner(row)
    task, job, main = owner
    _cid, path, card = card_for(directory, row, task, entries.get(task), listing)
    card = card or {}
    if card.get("batch"):
        raise pa.Problem("{} is on card {} ({}); run `pa-worktree remove --card {}`: it removes each worktree on it through "
                         "these checks. Nothing was removed".format(row["path"], card["id"], card.get("status"), card["id"]))
    if card.get("status") != "answered":
        refuse_others(directory, row, task, entries.get(task), listing)
    if card.get("status") == "answered" and pa.is_other(card):
        raise pa.Problem('card {id} was answered Other, in the person\'s own words: "{words}". pa-worktree removes nothing on it. '
                         'Act on their words; if they ask for the worktree to go, withdraw the card (pa-decide {id} --withdraw '
                         '--why "<reason>") and the next pa-worktree check --open-cards asks again.'.format(
                             id=card.get("id"), words=cut(clean(card.get("note")), 200)))
    if card.get("status") != "answered" or not str(card.get("answer", "")).lower().startswith("delete"):
        raise pa.Problem("no answered \"Delete\" decision card for this worktree (run `pa-worktree check --open-cards`; the person answers it)")
    if os.path.realpath(card.get("worktree") or "") != row["path"]:
        raise pa.Problem("the card is for {}, not {}".format(card.get("worktree"), row["path"]))
    remove_one(directory, row, card, path, owner, profile)
    return 0


class RemoveFailed(pa.Problem):
    """The removal itself failed (git or the profile's script refused): not a check that stopped it."""


def remove_one(directory, row, card, path, owner, profile):
    """Removes one worktree on its answered Delete card (a single card, or a batch item's view), after every check made
    again now; raises pa.Problem (RemoveFailed when the removal itself failed) and removes nothing on a failed check.
    True when removed, None when it was already gone. path: the card's file."""
    task, job, main = owner
    facts = row["facts"]
    if not facts:
        print("Already gone: " + row["path"])
        return None
    if facts.get("unreadable"):
        raise pa.Problem("could not read {} ({}); nothing was removed".format(row["path"], facts["unreadable"]))
    if facts.get("main"):
        raise pa.Problem("{} is the project's main checkout; it is never removed".format(row["path"]))
    if row["keep"]:
        raise pa.Problem("keepWorktree is set on the task")
    if not main and not in_worktrees_folder(row["path"], profile):
        raise pa.Problem("{} is not under a worktrees folder; nothing was removed".format(row["path"]))
    still = tasks_still(row)
    if still and not under_rule_b(row, card):
        raise pa.Problem("tasks still {}; a worktree is removed only once every task using it is done or parked. Nothing was removed".format(still))
    if still:
        over, why, _desc = job_over(row, facts, profile)
        if not over:
            raise pa.Problem("tasks still {} and job {} is not over ({}); nothing was removed".format(still, clean(job), why))
    busy = [w["address"] for w in row["workers"] if w["fate"] == "live"] + row["others"]
    if busy:
        raise pa.Problem("still in use by {}; nothing was removed".format(", ".join(busy)))
    mismatch = card_mismatch(row, card, True)
    if mismatch:
        # The person answered for other tasks than the ones the worktree belongs to now: ask again with a fresh card.
        withdraw_answered(directory, path, card, task, "the card no longer stands: " + mismatch)
        raise pa.Problem(mismatch + "; the card is withdrawn and the next `pa-worktree check --open-cards` asks again. Nothing was removed")
    snapshot = card.get("snapshot") or {}
    changed = None
    if facts["uncommitted"] > snapshot.get("uncommitted", 0):
        changed = "there are more uncommitted files now ({}) than when the person answered ({})".format(
            facts["uncommitted"], snapshot.get("uncommitted", 0))
    elif facts.get("removesBranches") and facts["unpushed"] > snapshot.get("unpushed", 0):
        changed = "there are more unpushed commits now ({}) than when the person answered ({})".format(
            facts["unpushed"], snapshot.get("unpushed", 0))
    else:
        changed = changed_since_card(snapshot)
    if changed:
        # The person answered about a worktree that is no longer the same: ask again with a fresh card.
        withdraw_answered(directory, path, card, task, "the worktree changed since the person answered {!r}: {}".format(card.get("answer"), changed))
        raise pa.Problem(changed + "; the card is withdrawn and the next `pa-worktree check --open-cards` asks again. Nothing was removed")
    try:
        done, more, holder = remove_with_holder(directory, row, profile, facts)
    except pa.Problem as e:
        if not os.path.lexists(row["path"]):  # it failed, but the folder is gone: the ledger must not name it
            spend_card(directory, path, card)
            mark_removed_everywhere(directory, row, "the worktree {} is gone after a failed removal (card {})".format(row["path"], card["id"]))
        raise RemoveFailed(str(e))
    spend_card(directory, path, card)
    print(done + " (card {})".format(card["id"]))
    for line in more:
        print(line)
    tidy = remove_container(row["given"])
    if tidy:
        print(tidy)
    mark_removed_everywhere(directory, row, "removed the worktree {} (card {}){}".format(
        row["path"], card["id"], "; " + done if holder else ""))
    return True


@contextlib.contextmanager
def worktree_lock(directory):
    """One pa-worktree remove --card or --standing at a time per space (R11): <state>/.worktree.lock, waited for up to
    WORKTREE_LOCK_WAIT seconds; then refused, nothing removed."""
    import fcntl
    with open(os.path.join(directory, WORKTREE_LOCK), "a") as lock:
        deadline = time.monotonic() + WORKTREE_LOCK_WAIT
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise pa.Problem("another pa-worktree remove is running (it holds {}); nothing was removed. Run it again once "
                                     "that one has ended".format(os.path.join(directory, WORKTREE_LOCK)))
                time.sleep(0.1)
        yield


def refresh_busy(row, agent, profile):
    """Reads a row's facts, its workers' state and the other terminals in it again, right before it is removed (R5)."""
    row["facts"] = worktree_facts(row["path"], profile)
    for w in row["workers"]:
        if pa.hc_get(w.get("address")).get("state") == "live":
            w["fate"] = "live"
    mine = {w["address"] for w in row["workers"]}
    live = pa.live_terminals() or {}
    row["others"] = sorted(a for a, info in live.items() if a not in mine and a != agent.get("terminal") and (
        inside(info.get("cwd"), row["path"]) or inside((info.get("foreground") or {}).get("cwd"), row["path"])))


def answered_batch(directory, cid):
    """(path, card) of an answered batch card that pa-worktree remove --card may act on, or a refusal (R4, R7)."""
    path = pa.card_file(directory, cid) if is_batch_id(cid) else None
    card = pa.read_json_safe(path) if path else None
    if not isinstance(card, dict) or card.get("kind") != "worktrees" or not isinstance(card.get("items"), list):
        raise pa.Problem("no batch card {}; nothing was removed".format(cid))
    if card.get("status") == "open":
        raise pa.Problem("card {} is not answered yet; the person answers it in the panel. Nothing was removed".format(cid))
    if card.get("status") != "answered":
        raise pa.Problem("card {} is {}; nothing was removed".format(cid, card.get("status")))
    if "in conversation" in str(card.get("answeredBy") or ""):
        raise pa.Problem("card {} is answered in the panel only; nothing was removed".format(cid))
    if pa.is_other(card):
        raise pa.Problem('card {id} was answered Other, in the person\'s own words: "{words}". pa-worktree removes nothing on it. '
                         'Act on their words; if they ask for the worktrees to go, withdraw the card (pa-decide {id} --withdraw '
                         '--why "<reason>") and the next pa-worktree check --open-cards asks again.'.format(
                             id=cid, words=cut(clean(card.get("note")), 200)))
    if str(card.get("note") or "").strip():
        # A note is never read: it could name a worktree to keep in any words. Remove nothing and ask again (as d-leftovers).
        words = cut(clean(card["note"]), 200)
        pa.keep_previous(card, card.get("answer"))
        card.update(status="withdrawn", answer=None, answered=pa.now(), answeredBy="pa-worktree remove: withdrawn",
                    note="Withdrawn by pa-worktree remove: it was answered with a note")
        pa.write_card(directory, path, card)
        append_journal(directory, {"by": "agent" if by_an_agent() else "person", "trigger": "worktree", "task": None,
                                   "why": "withdrew card {}: it was answered with a note ({}); nothing was removed".format(cid, words)})
        raise pa.Problem('card {} has a note, so nothing was removed: "{}". The note is not read. Ask the person to say "keep tNN" '
                         'for each worktree to keep (then set keep on it); the next pa-worktree check --open-cards asks again on '
                         'a new card'.format(cid, words))
    if answer_scope(card) is None:
        raise pa.Problem("card {} was answered {!r}, which is none of its options; nothing was removed".format(cid, card.get("answer")))
    return path, card


def remove_card(directory, agent, profile, cid):
    """pa-worktree remove --card <batch id> (R5, R6): each worktree the answer covers, through every check that
    pa-worktree remove <task> makes, read now and again right before each one. One that fails a check is skipped and
    listed (and asked again when it changed); the others are removed. 0 when nothing was skipped or failed this run."""
    with worktree_lock(directory):
        path, card = answered_batch(directory, cid)
        scope = answer_scope(card)
        if scope == "keep":
            print("card {} answered {!r}: nothing is removed; none of them is asked about again".format(cid, card.get("answer")))
            return 0
        rows = worktree_state(directory, agent, profile)
        entries = ledger_tasks(directory)
        covered = removed = 0
        names, skipped, already, earlier = [], [], [], []
        for i in range(len(card["items"])):
            card = pa.read_json(path)
            item = card["items"][i]
            label = item_who(item)
            if scope == "lossless" and not item.get("lossless"):
                continue
            covered += 1
            if item.get("removed"):
                print("{}: already removed ({})".format(label, clean(item["removed"])))
                already.append(label)
                continue
            if item.get("withdrawn"):
                print("{}: skipped earlier ({}); the next card asks about it again".format(label, cut(clean(item["withdrawn"]), 200)))
                earlier.append(label)
                continue
            view = item_view(cid, card, i)
            real = os.path.realpath(os.path.expanduser(str(item.get("path") or "/nonexistent")))

            def skip(why, withdraw=False):
                why = cut(clean(why), 300)
                update_item(directory, view, skipped=why, **({"withdrawn": why} if withdraw else {}))
                skipped.append("{} ({})".format(label, why))
                print("{}: skipped: {}".format(label, why))
            spent = card_spent(view, entries.get(item.get("task")))
            if spent:
                skip("the ledger records it removed after the card ({}); a worktree there now gets its own card".format(spent), True)
                continue
            row = rows.get(real)
            if row is None:
                if not os.path.lexists(real):
                    update_item(directory, view, removed=pa.now(), skipped=None)
                    print("{}: already gone: {}".format(label, real))
                    removed += 1
                    names.append(label)
                else:
                    skip("no task records {} now".format(real), True)
                continue
            refresh_busy(row, agent, profile)
            if not row["facts"]:
                update_item(directory, view, removed=pa.now(), skipped=None)
                print("{}: already gone: {}".format(label, real))
                mark_removed_everywhere(directory, row, "the worktree {} is already gone (card {})".format(real, cid))
                removed += 1
                names.append(label)
                continue
            differs = branch_differs(view, row["facts"])
            if differs:
                skip(differs, True)
                continue
            owner = card_owner(row)
            if owner[0] != item.get("task"):
                skip("its card is {}'s now".format(clean(owner[0])), True)
                continue
            try:
                done = remove_one(directory, row, view, path, owner, profile)
            except RemoveFailed as e:
                skip("failed: {}".format(e))
                continue
            except pa.Problem as e:
                skip(str(e))  # remove_one withdrew the item when the person must be asked again
                continue
            if done:
                removed += 1
                names.append(label)
        line = "Removed {} of {} on card {}: {}".format(removed, covered, cid, ", ".join(names) or "none")
        if already:
            line += "; already removed {}: {}".format(len(already), ", ".join(already))
        if earlier:
            line += "; skipped earlier {}: {}".format(len(earlier), ", ".join(earlier))
        if skipped:
            line += "; skipped {}: {}; the next pa-worktree check --open-cards asks again about the ones that changed".format(
                len(skipped), ", ".join(skipped))
        print(line)
        return 1 if skipped else 0


# ---- deleting clean worktrees on the person's standing rule (t-168 Part B) ---------------------------------------
# The person's rule, bound by them in worktree-allow.json (their command pa-worktree-allow only), is their standing
# approval to delete, with no card, the worktrees whose every commit is merged into main and pushed, with nothing
# uncommitted or untracked, no terminal in them and no keep. Only this space's Project Agent runs it. pa_push's
# standing-rule checks (t-140) are reused; anything unknown refuses.

WORKTREE_ALLOW = "worktree-allow.json"
ALLOW_TOP = ("version", "enabled", "spaces")
ALLOW_ENTRY = ("space", "enabled", "standing", "note")
_PUSH = [None]


class _Self:
    """pa_hooks as pa_push sees it: every name read from this module's globals when used (so a test's patch counts),
    also when this module was loaded under another name or as __main__."""

    def __getattr__(self, name):
        return globals()[name]


def push_tool():
    """pa_push.py from this file's folder, loaded once with this pa and this module as its pa_hooks (it imports
    pa_hooks at its top; loading it here lazily keeps pa_hooks free of an import cycle)."""
    if _PUSH[0] is None:
        saved = {k: sys.modules.get(k) for k in ("pa", "pa_hooks")}
        sys.modules["pa"], sys.modules["pa_hooks"] = pa, _Self()
        try:
            spec = tool_spec("pa_push")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v
        _PUSH[0] = module
    return _PUSH[0]


def worktree_allow_path():
    return os.path.join(pa.ROOT, WORKTREE_ALLOW)


def worktree_allow_validate(data):
    """None when data is a valid worktree-allow.json; else why not. All or nothing."""
    if not isinstance(data, dict):
        return "the file must hold a JSON object"
    extra = sorted(set(data) - set(ALLOW_TOP))
    if extra:
        return "unknown key {}".format(", ".join(extra))
    if type(data.get("version")) is not int or data.get("version") != 1:
        return "version must be 1"
    if not isinstance(data.get("enabled"), bool):
        return "enabled must be true or false"
    if not isinstance(data.get("spaces"), list):
        return "spaces must be a list"
    seen = set()
    for i, e in enumerate(data["spaces"]):
        where = "spaces[{}]: ".format(i)
        if not isinstance(e, dict):
            return where + "must be an object"
        extra = sorted(set(e) - set(ALLOW_ENTRY))
        if extra:
            return where + "unknown key {}".format(", ".join(extra))
        if not isinstance(e.get("space"), str) or not pa.ID.match(e["space"]):
            return where + "space must be a space id (letters, digits, _ and -)"
        if not isinstance(e.get("enabled"), bool):
            return where + "enabled must be true or false"
        b = e.get("standing")
        if "standing" in e and (not isinstance(b, dict) or sorted(b) != ["rule", "textSha"] or not isinstance(b.get("rule"), str)
                                or not pa.ID.match(b["rule"]) or not isinstance(b.get("textSha"), str)
                                or not re.match(r"^[0-9a-f]{64}$", b["textSha"])):
            return where + "standing must be {\"rule\": <rule id>, \"textSha\": <SHA-256 of its text>} (pa-worktree-allow set)"
        if "note" in e and (not isinstance(e["note"], str) or len(e["note"]) > 200):
            return where + "note must be a string of at most 200 characters"
        if e["space"] in seen:
            return where + "space {} is listed twice".format(e["space"])
        seen.add(e["space"])
    return None


def worktree_allow_load():
    """(data, problem): (None, None) when there is no file; a problem when it cannot be read or is not valid."""
    try:
        with open(worktree_allow_path(), "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return None, None
    except OSError as e:
        return None, "cannot read it ({})".format(e.strerror or e)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        return None, "not JSON ({})".format(e)
    return data, worktree_allow_validate(data)


def cmd_worktree_allow(args, env=None, ancestors=None):
    """pa-worktree-allow: show (anyone); set, unbind, on, off: the person's own changes, refused in an agent's shell
    (pa_push.caller_refusal, not strict, as pa-push-allow). The old file is kept as .bak-pa-worktree-allow[-n]."""
    push = push_tool()
    path = worktree_allow_path()
    data, problem = worktree_allow_load()
    space = pa.local_id(args.space or "") or None
    if args.action == "show":
        exists = data is not None or problem is not None
        state = ("off (the default): no worktree-allow.json" if not exists else "not valid, so off: " + problem if problem
                 else "on" if data["enabled"] else "off (enabled: false)")
        spaces = [dict(e, thisSpace=e["space"] == space) for e in (data or {}).get("spaces") or []] if exists and not problem else []
        if args.json:
            print(json.dumps({"file": path, "exists": exists, "valid": problem is None, "problem": problem, "state": state,
                              "enabled": bool(exists and not problem and data["enabled"]), "spaces": spaces}, indent=2))
            return 0
        print("Worktree deletion rule bindings {}: {}".format(path, state))
        for e in spaces:
            b = e.get("standing") or {}
            print("  {} {} space {}: {}{}".format("*" if e["thisSpace"] else "-", "on " if e["enabled"] else "off", e["space"],
                                                  "rule {} (text sha256 {})".format(b["rule"], b["textSha"]) if b else "no rule bound",
                                                  "  note: " + e["note"] if e.get("note") else ""))
        return 0
    env = os.environ if env is None else env
    why = push.caller_refusal(env, push.ancestors() if ancestors is None else ancestors, strict=False)
    if why:
        print("pa-worktree-allow: refused: pa-worktree-allow {} is the person's command; run it in your own terminal. ({})".format(
            args.action, why), file=sys.stderr)
        return 1
    if problem:
        print("pa-worktree-allow: {} is not valid: {}; fix it by hand".format(path, problem), file=sys.stderr)
        return 1
    data = data or {"version": 1, "enabled": True, "spaces": []}
    found = [e for e in data["spaces"] if e["space"] == space]
    if args.action in ("on", "off") and not space:
        data["enabled"] = args.action == "on"
        what = "worktree-allow.json is {}".format(args.action)
    elif not space:
        print("pa-worktree-allow: {} needs --space <id>".format(args.action), file=sys.stderr)
        return 1
    elif args.action == "set":
        binding, why = push.binding_for(space, args.standing)
        if why:
            print("pa-worktree-allow: not changed: " + why, file=sys.stderr)
            return 1
        if found:
            found[0]["standing"] = binding
        else:
            data["spaces"].append({"space": space, "enabled": True, "standing": binding})
        if args.note is not None:
            (found or data["spaces"][-1:])[0]["note"] = args.note
        what = "space {} is bound to rule {} (text sha256 {})".format(space, binding["rule"], binding["textSha"])
    elif not found:
        print("pa-worktree-allow: space {} is not in {}".format(space, path), file=sys.stderr)
        return 1
    elif args.action == "unbind":
        found[0].pop("standing", None)
        what = "space {} has no rule bound".format(space)
    else:
        found[0]["enabled"] = args.action == "on"
        what = "space {} is {}".format(space, args.action)
    problem = worktree_allow_validate(data)
    if problem:
        print("pa-worktree-allow: not changed: the result would not be valid: {}".format(problem), file=sys.stderr)
        return 1
    pa.backup(path, "pa-worktree-allow")
    pa.write_json(path, data)
    print("{} ({}).".format(what, path))
    return 0


def standing_delete_rule(directory, space, rule_id):
    """(rule, binding) when the person's rule rule_id is in force for deleting worktrees in this space (R13), read now;
    else pa.Problem with why not. The binding is in worktree-allow.json only (a push binding does not count)."""
    push = push_tool()

    def no(why):
        raise pa.Problem("the rule is not in force: {}; nothing was removed".format(why))
    if not isinstance(rule_id, str) or not pa.ID.match(rule_id):
        no("--standing needs the rule's id")
    data, problem = worktree_allow_load()
    if data is None and problem is None:
        no("no worktree-allow.json ({}): the rule is not bound; the person binds it with pa-worktree-allow set --space {} "
           "--standing {}".format(worktree_allow_path(), space, rule_id))
    if problem:
        no("worktree-allow.json is not valid: {}".format(problem))
    if not data["enabled"]:
        no("worktree-allow.json is turned off (enabled: false)")
    entry = next((e for e in data["spaces"] if e["space"] == space), None)
    if entry is None:
        no("space {} is not in worktree-allow.json".format(space))
    if not entry["enabled"]:
        no("worktree-allow.json is turned off for space {}".format(space))
    bound = (entry.get("standing") or {}).get("rule")
    if bound != rule_id:
        no("space {} is bound to rule {}, not {}".format(space, bound, rule_id) if bound else "space {} has no rule bound".format(space))
    rules = push.space_rules(directory)
    if rules is None:
        no("this space has no valid rules.json")
    found = [r for r in rules if r.get("id") == rule_id]
    if not found:
        no("no rule {} in this space's rules.json".format(rule_id))
    if len(found) > 1:
        no("{} rules have the id {}".format(len(found), rule_id))
    rule = found[0]
    if rule.get("status") != "active":
        no("rule {} is {}, not active".format(rule_id, rule.get("status")))
    if rule.get("appliesTo") != "all":
        no("rule {} applies to {}, not all".format(rule_id, rule.get("appliesTo")))
    why = push.not_persons(rule)
    if why:
        no("rule {} is not the person's: {}".format(rule_id, why))
    if not isinstance(rule.get("text"), str) or push.text_sha(rule["text"]) != entry["standing"]["textSha"]:
        no("the text of rule {} changed since the person bound it; they bind it again".format(rule_id))
    return rule, entry["standing"]


def merged_and_pushed(path, profile, rev="HEAD"):
    """True when HEAD (or rev) is reachable from a remote-tracking ref of a main branch (local refs, no fetch, Q8): every
    commit is merged into main and on the remote. A squash merge or a picked commit is not."""
    names = {"main", "master", "trunk"}
    for b in (profile or {}).get("mainBranches") or []:
        names.add(str(b).split("/", 1)[-1] if str(b).startswith("origin/") else str(b))
    code, target, _ = git(path, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if not code and "/" in target:
        names.add(target.split("/", 1)[1])
    code, out, _ = git(path, "for-each-ref", "--format=%(refname)", "refs/remotes")
    if code:
        return False
    for ref in out.splitlines():
        parts = ref[len("refs/remotes/"):].split("/", 1)
        if len(parts) == 2 and parts[1] in names and git(path, "merge-base", "--is-ancestor", rev, ref)[0] == 0:
            return True
    return False


def standing_clean(directory, row, owner, profile, entries, listing=None, holds=None):
    """Why a candidate worktree is not clean enough to delete on the rule (R15), or None. Fails closed. holds: the run's
    worktree_holds (None: read now, as standing_recheck does)."""
    facts = row["facts"]
    hold = worktree_hold(worktree_holds(directory, listing, entries) if holds is None else holds, row)
    if hold:
        return hold_line(hold)
    if not in_worktrees_folder(row["path"], profile):
        return "not under a worktrees folder"
    if facts.get("uncommitted"):
        return "{} uncommitted or untracked file(s)".format(facts["uncommitted"])
    if facts.get("unpushed"):
        return "not every commit is merged into main and pushed ({} on no remote)".format(facts["unpushed"])
    repos = facts.get("repos") if facts.get("remove") else [row["path"]]
    if not repos:
        return "its repositories are not listed, so they cannot be checked"
    for repo in repos:
        files = status_files(repo)
        name = os.path.basename(repo.rstrip(os.sep))
        if files is None:
            return "cannot read {} (git status failed)".format(clean(name))
        if files:
            return "{} uncommitted or untracked file(s) in {}".format(len(files), clean(name))
        if not merged_and_pushed(repo, profile):
            return "not every commit is merged into main and pushed in {}".format(clean(name))
    return None


# ---- the person's holds, one store per worktree (t-168 round 5) ---------------------------------------------------
# Every worktree card (single, and each item of a batch card), answered, withdrawn or asked again, gives events: its
# answer, when it covers the worktree, and the answer it carries from before (previous*). An event holds the worktree
# when it is Keep, Keep all, Other, an answer that is none of the options, or any answer with a note (never read). A hold
# stands until a later answer of the person that covers the worktree and holds nothing (a Delete with no note) replaces
# it: answered after it, or on a card asked after it, or the answer on the card that carries it. Keyed by the worktree's
# path; an event for another branch than the worktree is on now does not count, nor a spent card. Read by the card
# builds (open_worktree_card, open_worktrees_card carry the words) and by remove --standing (standing_clean).

def hold_time(value):
    when = stamp_time(value)
    return when[0] if when else None


def answer_hold(answer, note, other=False):
    """What of an answer given now holds its worktree: its answer when not a Delete, else its note, else None."""
    if other or answer is None or not str(answer).strip().lower().startswith("delete"):
        return "answer {!r}".format("Other" if other else str(answer))
    return "note" if str(note or "").strip() else None


def carried_hold(card):
    """What of the answer a card carries from before (previous*) holds its worktree: its note, else a not-Delete answer."""
    if str(card.get("previousNote") or "").strip():
        return "note"
    answer = card.get("previousAnswer")
    if answer is not None and not str(answer).strip().lower().startswith("delete"):
        return "answer {!r}".format(str(answer))
    return None


def card_events(cid, card, covered, unknown=False):
    """The events of one card for one worktree: (answer when `covered`, previous*). unknown: a batch card answered
    with none of its options, which holds (fail closed)."""
    base = {"cid": cid, "status": card.get("status"), "branch": card.get("branch"), "created": hold_time(card.get("created"))}
    out = []
    if card.get("status") == "answered" and covered:
        out.append(dict(base, source="answer", time=hold_time(card.get("answered")),
                        words=("answer {!r}".format(str(card.get("answer"))) if unknown and not pa.is_other(card) else
                               answer_hold(card.get("answer"), card.get("note"), pa.is_other(card))),
                        label="Other" if pa.is_other(card) else card.get("answer"),
                        carry={"previousAnswer": card.get("answer"), "previousNote": card.get("note") if str(card.get("note") or "").strip() else None,
                               "previousAnswered": card.get("answered")}))
    if card.get("previousAnswer") is not None or str(card.get("previousNote") or "").strip():
        out.append(dict(base, source="previous", time=hold_time(card.get("previousAnswered") or card.get("answered")),
                        words=carried_hold(card), label=card.get("previousAnswer"),
                        carry={k: card.get(k) for k in CARRIED}))
    return out


def worktree_holds(directory, listing=None, entries=None):
    """{worktree realpath: [event]} of every worktree card in decisions/ and the archive (see above)."""
    listing = card_listing(directory) if listing is None else listing
    entries = ledger_tasks(directory) if entries is None else entries
    holds = {}
    for cid in sorted(listing):
        if not cid.startswith("d-wt-") or is_batch_id(cid) or cid.startswith(UNRECORDED):
            continue
        card = pa.read_json_safe(pa.card_file(directory, cid))
        if not isinstance(card, dict) or not isinstance(card.get("worktree"), str) or not card["worktree"].strip():
            continue
        if card_spent(card, entries.get(card.get("task"))):
            continue
        holds.setdefault(card_tree(card), []).extend(card_events(cid, card, True))
    for cid, card in worktrees_cards(directory, listing).items():
        for i, item in enumerate(card["items"]):
            if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item["path"].strip():
                continue
            view = item_view(cid, card, i)
            if card_spent(view, entries.get(item.get("task"))):
                continue
            events = card_events(cid, dict(card, branch=item.get("branch")), item_covered(card, item), answer_scope(card) is None)
            holds.setdefault(card_tree(view), []).extend(events)
    return holds


def replaces(answer, hold):
    """Whether a Delete with no note (`answer`) is later than `hold`, so it replaces it."""
    if answer["cid"] == hold["cid"] and hold["source"] == "previous":
        return True  # the card asked again with these words carried, answered since
    if hold["time"] is None:
        return False
    return bool((answer["time"] and answer["time"] > hold["time"]) or (answer["created"] and answer["created"] >= hold["time"]))


def standing_holds(holds, row):
    """Every event that holds the row's worktree now: the holds on its branch no later answer replaced."""
    events = [e for e in holds.get(row["path"], []) if not branch_differs(e, row.get("facts"))]
    clears = [e for e in events if e["source"] == "answer" and not e["words"]]
    return [e for e in events if e["words"] and not any(replaces(a, e) for a in clears)]


def worktree_hold(holds, row):
    """The event that holds the row's worktree now, or None: the newest hold no later answer replaced (an open card
    first among equals, as card_for ranks)."""
    standing = standing_holds(holds, row)
    if not standing:
        return None
    rank = {"open": 2, "answered": 1}
    floor = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    return max(standing, key=lambda e: (e["time"] or floor, rank.get(e["status"], 0)))


def hold_line(hold):
    if hold["source"] == "answer" and hold["words"].startswith("answer"):
        return "answered {!r} on card {}".format(hold["label"], hold["cid"])
    return "its card {} carries the person's {}; left for its card".format(hold["cid"], hold["words"])


def item_held(holds, row):
    """[{card, answer, note, answered}] of every hold on the row's worktree, oldest first, once per answer and note:
    what a batch card shows under that worktree's line (review r5 RL1), so the person answers with all their words on it."""
    floor = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    out, seen = [], set()
    for e in sorted(standing_holds(holds, row), key=lambda e: e["time"] or floor):
        c = e["carry"]
        held = {"card": e["cid"], "answer": c.get("previousAnswer"),
                "note": c.get("previousNote") if str(c.get("previousNote") or "").strip() else None,
                "answered": c.get("previousAnswered")}
        if (held["answer"], held["note"]) not in seen:
            seen.add((held["answer"], held["note"]))
            out.append(held)
    return out


def held_carry(holds, rows):
    """previous* of the newest hold on any of `rows`, for a card asked now: the person sees their words again. {} if none."""
    found = [h for h in (worktree_hold(holds, r) for r in rows) if h]
    if not found:
        return {}
    floor = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    best = max(found, key=lambda e: e["time"] or floor)
    return {k: v for k, v in best["carry"].items() if v is not None}


def standing_recheck(directory, row, owner, agent, profile, entries, listing):
    """Right before a standing removal: the facts, workers and terminals read again, and every loss fact the first read
    checked checked again on them (uncommitted, untracked, unpushed, unmerged). Why it must stay, or None. Fails closed."""
    refresh_busy(row, agent, profile)
    busy = [w["address"] for w in row["workers"] if w["fate"] == "live"] + row["others"]
    if busy:
        return "in use by " + ", ".join(busy)
    facts = row["facts"]
    if not facts:
        return "changed while it ran: gone or not a worktree"
    if facts.get("unreadable"):
        return "changed while it ran: could not read it ({})".format(facts["unreadable"])
    if facts.get("main"):
        return "changed while it ran: it is the project's main checkout"
    why = standing_clean(directory, row, owner, profile, entries, listing)
    return "changed while it ran: " + why if why else None


def standing_who(owner):
    task, job, main = owner
    return "{} (main)".format(clean(task)) if main and job == "main" else "{} {}".format(clean(task), clean(job))


def remove_standing(directory, space, agent, profile, rule_id, env=None, ancs=None, cwd=None):
    """pa-worktree remove --standing <rule> (R13-R16): the Project Agent deletes the clean candidate worktrees on the
    person's rule, all in one run, and reports one line. A failed removal writes the rule's stop line and ends the run."""
    push = push_tool()
    env = os.environ if env is None else env
    why = push.standing_caller_refusal(env, push.ancestors() if ancs is None else ancs, cwd or os.getcwd(), directory)
    if why:
        raise pa.Problem("pa-worktree remove --standing is the Project Agent's, under the person's standing rule in its own "
                         "space; without it, the person answers a card in the panel ({}). Nothing was removed".format(why))
    with worktree_lock(directory):
        rule, binding = standing_delete_rule(directory, space, rule_id)
        stop = push.standing_stops(directory, rule)
        if stop:
            if stop[1]:
                push.write_stop(directory, rule_id, stop[0])
            raise pa.Problem("the rule stops here: {}; nothing was removed".format(stop[0]))
        rows = worktree_state(directory, agent, profile)
        entries = ledger_tasks(directory)
        listing = card_listing(directory)
        holds = worktree_holds(directory, listing, entries)
        removed, paths, left, failed, notes = [], [], [], None, []
        for row in rows.values():
            owner = card_owner(row)
            why, _rule, _desc = candidate_why(row, profile, owner)
            if why:
                continue  # not a candidate: not touched, not listed
            why = standing_clean(directory, row, owner, profile, entries, listing, holds)
            if not why:
                why = standing_recheck(directory, row, owner, agent, profile, entries, listing)
            if why:
                left.append("{} ({})".format(standing_who(owner), cut(clean(why), 120)))
                continue
            cid, cpath, card = card_for(directory, row, owner[0], entries.get(owner[0]), listing)
            try:
                # Clean, just checked again: its counts are 0, and they go as 0 so the removal is never forced (R15).
                done, more, holder = remove_with_holder(directory, row, profile, dict(row["facts"], uncommitted=0, unpushed=0))
                notes += ([done] if holder else []) + more
                if os.path.lexists(row["path"]):
                    raise pa.Problem("the folder is still there after its removal")
            except pa.Problem as e:
                failed = "removing {} failed: {}".format(row["path"], clean(e))
                if not os.path.lexists(row["path"]):
                    mark_removed_everywhere(directory, row, "the worktree {} is gone after a failed removal on rule {}".format(row["path"], rule_id))
                push.write_stop(directory, rule_id, failed)
                break
            if card and card.get("status") == "open" and not card.get("batch"):
                with contextlib.redirect_stdout(io.StringIO()):  # journaled; the report line is the run's only output
                    withdraw_card(directory, cpath, card, owner[0], "its worktree was removed on the person's rule " + rule_id)
            if card and card.get("status") in ("open", "answered", "withdrawn"):
                spend_card(directory, cpath, card)
            remove_container(row["given"])  # one report line for the person (R16): the container's outcome is not printed
            mark_removed_everywhere(directory, row, "removed the worktree {} on rule {} (standing): {}".format(row["path"], rule_id, done))
            removed.append(standing_who(owner))
            paths.append(row["path"])
        line = "Removed {} worktrees on rule {}: {}".format(len(removed), rule_id, ", ".join(removed) or "nothing is clean")
        if left:
            line += "; {} not clean, left for cards: {}".format(len(left), ", ".join(left))
        if notes:  # t-192 R6, R7: what became of each removed worktree's holder and branch
            line += "; " + "; ".join(n.rstrip(".") for n in notes)
        old = [] if failed else old_branches(directory, agent, profile, rows)  # t-192 Q5
        if old:
            line += "; deleted {} merged and pushed branch{} of worktrees removed before: {}{}".format(
                len(old), "" if len(old) == 1 else "es", ", ".join(clean(b) for b in old[:10]),
                " and {} more".format(len(old) - 10) if len(old) > 10 else "")
        if failed:
            line += "; then {}: the rule is stopped, and resuming it is the person's".format(failed)
        if removed:
            append_journal(directory, {"by": "agent", "trigger": "standing", "rule": rule_id, "textSha": binding["textSha"],
                                       "why": cut(line, 1000), "removed": paths})
        print(line)
        if failed:
            raise pa.Problem(failed + "; the rule is stopped")
        return 0


def spend_card(directory, path, card):
    """After its worktree is removed (or gone after a failed removal), the card never counts again (t-073 R3b)."""
    card["worktreeRemoved"] = pa.now()
    if card.get("batch"):
        update_item(directory, card, removed=card["worktreeRemoved"], skipped=None)
        return
    pa.write_card(directory, path, card)


def withdraw_answered(directory, path, card, task, why):
    """pa-worktree remove withdraws an answered card that no longer stands; the next check asks again. A batch item's
    view: that item only (its `withdrawn`), the card and its other items stay as they are."""
    if card.get("batch"):
        update_item(directory, card, withdrawn=why)
        append_journal(directory, {"by": "agent" if by_an_agent() else "person", "trigger": "worktree", "task": task,
                                   "why": "withdrew {} on card {}: {}".format(card.get("worktree"), card["id"], why)})
        return
    pa.keep_previous(card, card.get("answer"))
    card.update(status="withdrawn", answer=None, answered=pa.now(),
                answeredBy="pa-worktree remove: withdrawn", note="Withdrawn by pa-worktree remove: " + why)
    pa.write_card(directory, path, card)
    append_journal(directory, {"by": "agent" if by_an_agent() else "person", "trigger": "worktree", "task": task,
                               "why": "withdrew card {}: {}".format(card["id"], why)})


def pick_row(rows, task, job=None):
    """The row `pa-worktree remove <task> [--job <job>]` means: the job's worktree, else the task's main one, else its
    only one. Refuses (nothing removed) when there is none, or several and no main one."""
    mine = [r for r in rows.values() if task in r["tasks"]]
    if job:
        hit = [r for r in mine if r["jobs"][task] in (job, pa.job_slug(job))]
        if not hit:
            raise pa.Problem("task {} has no worktree for job {}; nothing was removed".format(task, clean(job)))
        return hit[0]
    row = next((r for r in mine if r["mains"].get(task)), None)
    if row:
        return row
    if not mine:
        raise pa.Problem("task {} has no worktree in the ledger".format(task))
    if len(mine) > 1:
        raise pa.Problem("task {} has no main worktree; name one: {}; nothing was removed".format(
            task, ", ".join("--job " + clean(r["jobs"][task]) for r in mine)))
    return mine[0]


def state_word(state):
    """A task's state as said on a line: `no state` when the ledger gives none (it counts as unfinished)."""
    return clean(state) if state not in (None, "") else "no state"


def tasks_still(row):
    """'t-003 in-progress, t-004 review' for the tasks using a worktree that are not done or parked."""
    return ", ".join("{} {}".format(clean(i), state_word(s)) for i, s in row["states"].items() if s not in ("done", "parked"))


def mark_removed_everywhere(directory, row, why):
    """After a worktree was removed (or is gone after a failed removal): every task that records it gets it marked
    removed (mark_removed), under the ledger lock, and the journal says so. No entry is left naming the folder. A task that
    counts on the row through a path inside the worktree or its container (row["related"]) gets that path marked once
    it is gone (a package inside is gone with the worktree; a container only once remove_container took it)."""
    with ledger_lock(directory):
        ledger = read_ledger(directory)
        for t in ledger.get("tasks", []):
            if t.get("id") in row["tasks"]:
                mark_removed(t, row["path"], pa.now())
                t["updated"] = pa.now()
            gone = [p for p in (row.get("related") or {}).get(t.get("id"), []) if not os.path.lexists(p)]
            for p in gone:
                mark_removed(t, p, pa.now())
            if gone:
                t["updated"] = pa.now()
        if ledger:
            pa.write_json(os.path.join(directory, "ledger.json"), ledger)
    append_journal(directory, {"by": "agent" if by_an_agent() else "person", "trigger": "worktree", "task": row["tasks"][0], "why": why})


def changed_since_card(snapshot):
    """What changed in the worktree since its card's snapshot, in a way that matters: a repository's HEAD
    moved (new commits), or an uncommitted file that the card did not list. None when nothing did."""
    for path, head in (snapshot.get("heads") or {}).items():
        code, now_head, err = git(path, "rev-parse", "HEAD")
        if code:
            return "cannot read {} ({})".format(path, clean(err) or "git failed")
        if now_head != head:
            return "{} has new commits since the card (HEAD was {}, now {})".format(path, head[:7], now_head[:7])
    for path, listed in (snapshot.get("uncommittedFiles") or {}).items():
        files = status_files(path)
        if files is None:
            return "cannot read {} (git status failed)".format(path)
        new = sorted({clean(f) for _s, f in files} - set(listed))
        if new:
            return "{} has uncommitted files that were not on the card: {}".format(path, ", ".join(new[:5]) + (", +{} more".format(len(new) - 5) if len(new) > 5 else ""))
    return None


# ---- t-192 R6: removing a package of a holder removes the holder too, on the profile's rule ------------------------
HOLDER_STATUS_LIMIT = 60  # seconds for the profile's status script on a holder
HOLDER_REMOVE_LIMIT = 900  # seconds for the profile's remove script on a holder


def rule_active(directory, rule_id):
    """True when rules.json, read now, has a rule whose id is exactly rule_id and whose status is exactly active."""
    try:
        data = pa.read_json(os.path.join(directory, "rules.json"), None)
    except Exception:  # noqa: BLE001 - unreadable is never active
        return False
    rules = data.get("rules") if isinstance(data, dict) else None
    return isinstance(rules, list) and any(isinstance(r, dict) and r.get("id") == rule_id and r.get("status") == "active"
                                           for r in rules)


def holder_status(h, profile):
    """The profile's status script's JSON for the holder h, read now (HOLDER_STATUS_LIMIT), or None when it fails."""
    custom = profile["worktree"]
    script = custom["status"] if os.path.isabs(custom["status"]) else os.path.join(profile.get("_folder") or "", custom["status"])
    try:
        r = subprocess.run([script, h], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                           timeout=HOLDER_STATUS_LIMIT)
        facts = json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return facts if isinstance(facts, dict) else None


def holder_why(directory, profile, h, p, status):
    """Why the holder h must stay when its package p is removed (R6.2 b-e and a, all read now), or None when it may go
    with it. status: holder_status(h) (None: the script failed). Fails closed."""
    import fnmatch
    custom, parts = profile["worktree"], holder_glob(profile)
    if os.path.basename(h).startswith("-"):
        return "its name starts with '-' (it would read as an option)"
    # b: no terminal in it, the Project Agent's own included
    live = pa.live_terminals()
    if live is None:
        return "the terminal list cannot be read"
    here = sorted(a for a, info in live.items() if inside(info.get("cwd"), h) or inside((info.get("foreground") or {}).get("cwd"), h))
    if here:
        return "a terminal is working in it: " + ", ".join(clean(a) for a in here)
    # c: no other task records a path in it
    agent = pa.read_json_safe(os.path.join(directory, "agent.json")) or {}
    folders, unreadable = project_folders(directory, agent if isinstance(agent, dict) else {})
    if unreadable:
        return "cannot read {} of space {}".format(unreadable[0][2], clean(unreadable[0][0]))
    for folder in folders:
        try:
            ledger = pa.read_json(os.path.join(folder, "ledger.json"), {}) or {}
            tasks = ledger.get("tasks") if isinstance(ledger, dict) else None
            if not isinstance(tasks, list):
                raise ValueError("no task list")
        except (OSError, ValueError):
            return "cannot read the ledger of space {}".format(clean(os.path.basename(folder)))
        for task in tasks:
            for t in pa.task_worktrees(task) if isinstance(task, dict) else []:
                if inside(t["path"], h) and os.path.realpath(t["path"]) != p:
                    return "task {} records {} in it".format(clean(task.get("id")), clean(t["path"]))
    # e: the remove script would run without --force
    if status is None:
        return "the profile's status script failed"
    try:
        dirty, ahead = int(status.get("uncommitted", 0)), int(status.get("unpushed", 0))
    except (TypeError, ValueError):
        return "the profile's status script gave counts it cannot read"
    if dirty or ahead:
        return "its status script counts {} uncommitted and {} unpushed".format(dirty, ahead)
    listed = {os.path.realpath(r) for r in status.get("repos") or [] if isinstance(r, str) and os.path.isabs(r)}
    # d and a: its top level holds only the marker, the packages' folders and the profile's own names; every entry on
    # the way to the packages is a package folder; every package is a git repository the status script lists, inside
    # it, clean, merged and pushed
    own = [n for n in custom.get("own") or [] if isinstance(n, str)]
    repos = []

    def walk(folder, depth):
        for entry in sorted(os.listdir(folder)):
            path = os.path.join(folder, entry)
            if depth == 0 and (entry == custom["marker"] or entry in own):
                continue
            if not fnmatch.fnmatchcase(entry, parts[depth]):
                return "{} is in it{}".format(clean(os.path.relpath(path, h)), " (not the profile's own)" if depth == 0 else "")
            if os.path.islink(path) or not inside(path, h):
                return "{} links out of it".format(clean(os.path.relpath(path, h)))
            if depth == len(parts) - 1:
                repos.append(os.path.realpath(path))
            elif os.path.isdir(path):
                why = walk(path, depth + 1)
                if why:
                    return why
            else:
                return "{} is in it".format(clean(os.path.relpath(path, h)))
        return None
    try:
        why = walk(h, 0)
    except OSError as e:
        return "cannot list it ({})".format(clean(e)[:200])
    if why:
        return why
    if p not in repos:
        return "{} is not one of its packages".format(clean(p))
    for extra in sorted(listed - set(repos)):
        return "its status script lists {}, which is not one of its packages".format(clean(extra))
    branch = status.get("branch") if isinstance(status.get("branch"), str) else None
    for repo in repos:
        name = os.path.relpath(repo, h)
        if repo not in listed:
            return "{} is not listed by the profile's status script".format(clean(name))
        code, top, _err = git(repo, "rev-parse", "--path-format=absolute", "--show-toplevel")
        if code or os.path.realpath(top) != repo:
            return "{} is not a git repository".format(clean(name))
        files = status_files(repo)
        if files is None:
            return "cannot read {} (git status failed)".format(clean(name))
        if files:
            return "{} uncommitted or untracked file(s) in {}".format(len(files), clean(name))
        if not merged_and_pushed(repo, profile):
            return "not every commit is merged into main and pushed in {}".format(clean(name))
        if branch and git(repo, "rev-parse", "--verify", "-q", "refs/heads/" + branch)[0] == 0 and \
                not merged_and_pushed(repo, profile, "refs/heads/" + branch):
            return "branch {} is not merged into main and pushed in {}".format(clean(branch), clean(name))
    return None


def holder_note(row, profile):
    """t-192 R6.4: for a package of a holder, what a card and WORKTREE.md say of the holder (a leading blank), else ''."""
    hold = None if (row.get("facts") or {}).get("remove") else holder_of(row["path"], profile)
    if not hold:
        return ""
    rid = profile["worktree"].get("holderRule")
    status = holder_status(hold[0], profile)
    kind = clean(status.get("kind")) if status and isinstance(status.get("kind"), str) and status["kind"].strip() else \
        "worktree that holds it"
    if not isinstance(rid, str) or not rid:
        return " The {} {} stays (the profile names no rule for removing it).".format(kind, hold[0])
    return (" The {} {} is deleted too when rule {} is active and everything in it is merged, pushed and clean; else it "
            "stays.".format(kind, hold[0], clean(rid)))


def remove_with_holder(directory, row, profile, facts):
    """Removes the worktree of a row (remove_worktree) and, for a package of a holder (holder_of) on the profile's
    rule (worktree.holderRule, active in rules.json now), the holder with it when holder_why finds nothing to keep:
    the profile's remove script on the holder, never --force (R6). (done, lines, holder or None): lines say what became
    of the holder. Raises pa.Problem as remove_worktree does."""
    hold = None if facts.get("remove") else holder_of(row["path"], profile)
    if not hold:
        return git_route(directory, row, profile, facts, [])
    h, p = hold
    custom = profile["worktree"]
    rid = custom.get("holderRule") if isinstance(custom.get("holderRule"), str) and custom.get("holderRule") else None
    status = holder_status(h, profile)
    kind = clean(status.get("kind")) if status and isinstance(status.get("kind"), str) and status["kind"].strip() else \
        "worktree that holds it"
    if not rid:
        why = "the profile names no rule for it"
    elif not rule_active(directory, rid):
        why = "rule {} is not active".format(clean(rid))
    else:
        why = holder_why(directory, profile, h, p, status)
    lines = []
    if why is None:
        script = custom["remove"] if os.path.isabs(custom["remove"]) else os.path.join(profile.get("_folder") or "", custom["remove"])
        try:
            r = subprocess.run([script, h], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                               timeout=HOLDER_REMOVE_LIMIT)
            out, code = r.stdout, r.returncode
        except (OSError, subprocess.TimeoutExpired) as e:
            out, code = str(e), 1
        if not code and not os.path.lexists(h):
            return "Removed the {} {} with {} (rule {}).".format(kind, h, p, rid), [], h
        lines.append("the {} {} stays: its remove script did not remove it:\n{}".format(kind, h, tail(out, 15)))
        if not os.path.lexists(row["path"]):
            return "The worktree {} is gone with the failed removal of the {} {}.".format(row["path"], kind, h), lines, None
    else:
        lines.append("the {} {} stays: {}".format(kind, h, why))
    return git_route(directory, row, profile, facts, lines)


def git_route(directory, row, profile, facts, lines):
    """remove_worktree, then (t-192 R7) for a git worktree its branch: deleted when merged and pushed (branch_after).
    (done, lines, None) as remove_with_holder gives them."""
    done = remove_worktree(row["path"], facts)
    if facts.get("remove"):
        return done, lines, None
    kept = " Branch {} is kept.".format(facts.get("branch"))
    if done.endswith(kept):
        done = done[:-len(kept)]
    return done, lines + [branch_after(directory, facts, profile)], None


def old_branches(directory, agent, profile, rows):
    """t-192 Q5 (remove --standing): the branches of worktrees removed before R7, deleted now on R7's checks
    (branch_after): for each spent worktree card (single or a batch item) whose worktree the ledger records as removed
    and is gone, its branch at the HEAD its snapshot recorded, in each main checkout known here (the rows' repositories,
    the project folder or its src/* repositories) where that branch still points at that commit. Returns the names
    deleted."""
    removed = set()
    for task in ledger_tasks(directory).values():
        for e in task.get("worktrees") or [] if isinstance(task.get("worktrees"), list) else []:
            if isinstance(e, dict) and e.get("removed") and isinstance(e.get("path"), str) and e["path"].strip():
                removed.add(os.path.realpath(os.path.expanduser(e["path"])))
    if not removed:
        return []
    listing = card_listing(directory)
    views = []
    for cid in sorted(listing):
        if cid.startswith("d-wt-") and not is_batch_id(cid) and not cid.startswith(UNRECORDED):
            card = pa.read_json_safe(pa.card_file(directory, cid))
            if isinstance(card, dict):
                views.append(card)
    for cid, card in worktrees_cards(directory, listing).items():
        views += [item_view(cid, card, i) for i in range(len(card["items"]))]
    repos = {r["facts"]["repo"] for r in rows.values() if isinstance(r.get("facts"), dict) and isinstance(r["facts"].get("repo"), str)}
    project = agent.get("project") if isinstance(agent.get("project"), str) and agent.get("project").strip() else None
    if project:
        project = os.path.realpath(os.path.expanduser(project))
        src = os.path.join(project, "src")
        for place in [project] + ([os.path.join(src, n) for n in sorted(os.listdir(src))] if os.path.isdir(src) else []):
            code, top, _ = git(place, "rev-parse", "--path-format=absolute", "--show-toplevel")
            if not code and os.path.realpath(top) == os.path.realpath(place):
                repos.add(os.path.realpath(place))
    deleted, seen = [], set()
    for v in views:
        path, b = card_tree(v), v.get("branch")
        if not v.get("worktreeRemoved") or path not in removed or os.path.lexists(path) or not isinstance(b, str) or not b:
            continue
        heads = (v.get("snapshot") or {}).get("heads") if isinstance(v.get("snapshot"), dict) else None
        heads = heads if isinstance(heads, dict) else {}
        sha = heads.get(v.get("worktree")) or heads.get(path) or (next(iter(heads.values())) if len(heads) == 1 else None)
        if not isinstance(sha, str) or not sha:
            continue
        for repo in sorted(repos):
            if (repo, b) in seen:
                continue
            code, now, _ = git(repo, "rev-parse", "--verify", "-q", "refs/heads/" + b)
            if code or now != sha:
                continue
            seen.add((repo, b))
            if branch_after(directory, {"repo": repo, "branch": b, "ref": "refs/heads/" + b, "head": sha}, profile).startswith("Deleted"):
                deleted.append(b)
    return deleted


def main_names(repo, profile):
    """The names a main branch goes by here: main, master, trunk, the profile's mainBranches, origin/HEAD's target."""
    names = {"main", "master", "trunk"}
    for b in (profile or {}).get("mainBranches") or []:
        names.add(str(b).split("/", 1)[-1] if str(b).startswith("origin/") else str(b))
    code, target, _ = git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if not code and "/" in target:
        names.add(target.split("/", 1)[1])
    return names


def branch_after(directory, facts, profile):
    """t-192 R7: after the git route removed a worktree, its branch B (facts["branch"], at facts["head"] when the facts
    were read) is deleted from the repository (facts["repo"]) when, read now: B is a branch name git accepts, not a main
    branch name; refs/heads/B still points at that commit; it is merged into a main branch and pushed; no other worktree
    has it checked out; no queue.json entry names it. The ref goes by update-ref with that commit as its old value
    (refused if B moved), then its config section. Never a remote ref, a tag or a push. Returns the line to print;
    never raises."""
    repo, b, sha = facts.get("repo"), facts.get("branch"), facts.get("head")
    if "ref" in facts:  # read as refs/heads/<B> (worktree_facts): no short name a tag can make ambiguous
        ref = facts["ref"]
        b = ref[len("refs/heads/"):] if isinstance(ref, str) and ref.startswith("refs/heads/") else None
    if not isinstance(b, str) or b in ("", "HEAD"):
        return "The worktree had no branch to delete (detached HEAD)."
    shown = clean(b)
    try:
        if b.startswith(("-", "refs/")) or any(c.isspace() for c in b) or git(repo, "check-ref-format", "--branch", b)[0] != 0:
            return "Branch {} is kept: not a branch name git accepts".format(shown)
        if b in main_names(repo, profile):
            return "Branch {} is kept: a main branch name".format(shown)
        code, now, _ = git(repo, "rev-parse", "--verify", "-q", "refs/heads/" + b)
        if code or not now:
            return "Branch {} is kept: it is not there any more".format(shown)
        if not isinstance(sha, str) or now != sha:
            return "Branch {} is kept: it moved since it was read ({} now)".format(shown, now[:7])
        if not merged_and_pushed(repo, profile, sha):
            return "Branch {} is kept: not every commit is merged into main and pushed".format(shown)
        code, out, _ = git(repo, "worktree", "list", "--porcelain")
        if code:
            return "Branch {} is kept: cannot list the repository's worktrees".format(shown)
        if "branch refs/heads/" + b in out.splitlines():
            return "Branch {} is kept: another worktree has it checked out".format(shown)
        queue_file = os.path.join(directory, "queue.json")
        queue = pa.read_json_safe(queue_file)
        recs = queue.get("repos") if isinstance(queue, dict) else None
        if os.path.lexists(queue_file) and not isinstance(recs, dict):
            return "Branch {} is kept: queue.json cannot be read".format(shown)
        for rec in (recs or {}).values():
            entries = rec.get("entries") if isinstance(rec, dict) and isinstance(rec.get("entries"), list) else []
            if any(isinstance(e, dict) and e.get("branch") == b for e in entries):
                return "Branch {} is kept: a queue entry names it".format(shown)
        code, _out, err = git(repo, "update-ref", "-d", "refs/heads/" + b, sha)
        if code:
            return "Branch {} is kept: it moved (git update-ref refused: {})".format(shown, clean(err)[-200:])
        git(repo, "config", "--remove-section", "branch." + b)
        return "Deleted branch {} (was {}), merged and pushed".format(shown, sha[:7])
    except Exception as e:  # noqa: BLE001 - a failed deletion never fails the removal (R7.2)
        return "Branch {} is kept: {}".format(shown, clean(e)[:200])


def remove_worktree(path, facts):
    """Removes a worktree whose removal the person has approved. Forced only as far as there is
    something to lose (the caller has shown the person those counts). Returns what it did."""
    if facts.get("remove"):  # the profile's kind of worktree: its own remove script
        force = facts["uncommitted"] > 0 or (facts.get("removesBranches") and facts["unpushed"] > 0)
        r = subprocess.run([facts["remove"], path] + (["--force"] if force else []), stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, universal_newlines=True)
        if r.returncode or os.path.isdir(path):
            raise pa.Problem("the profile's remove script did not remove it:\n{}".format(tail(r.stdout, 15)))
        return "Removed the {} {}.".format(facts.get("kind", "worktree"), path)
    for extra in (TASK_FILE, SETTINGS, SETTINGS + ".bak-pa-hooks"):  # our own files must not count as changes
        target = os.path.join(path, extra)
        if os.path.isfile(target):
            os.unlink(target)
    try:
        os.rmdir(os.path.join(path, ".claude"))
    except OSError:
        pass
    code, out, err = git(facts["repo"], "worktree", "remove", *(["--force"] if facts["uncommitted"] else []), path)
    if code:
        raise pa.Problem("git would not remove it: {}".format(err or out))
    return "Removed the worktree {}. Branch {} is kept.".format(path, facts["branch"])


def remove_container(given):
    """After a worktree was removed: removes the folder that held it when that is a container,
    <...>/worktrees/<container>/<repo>, now empty. given: the worktree's path as recorded (so a symlinked container is
    seen). Never the `worktrees` folder, a project or workspace, a symlink, a non-empty folder or anything else;
    a workspace's worktree (<ws>/worktrees/<name>) has no container. One os.rmdir, no recursion; never raises.
    Returns what it did, or None when there is no container to look at."""
    try:
        parent = os.path.dirname(os.path.abspath(os.path.expanduser(given)))
        holder = os.path.dirname(parent)
        if (os.path.basename(holder) != "worktrees" or os.path.basename(parent) == "worktrees" or os.path.islink(parent)
                or not os.path.isdir(parent) or os.path.basename(os.path.realpath(holder)) != "worktrees"
                or os.path.dirname(os.path.realpath(parent)) != os.path.realpath(holder)):
            return None
        names = sorted(os.listdir(parent))
        if names:
            return "kept {}: not empty ({}{})".format(parent, ", ".join(clean(n) for n in names[:3]), ", …" if len(names) > 3 else "")
        os.rmdir(parent)
        return "Removed the empty folder {}.".format(parent)
    except OSError as e:
        return "could not remove the empty folder {}: {}".format(os.path.dirname(os.path.abspath(str(given))), clean(e))


def by_an_agent():
    """True inside an agent's own shell. The person's destructive commands refuse to run there:
    an agent asks with a decision card instead."""
    return any(os.environ.get(k) for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_SANDBOX", "CODEX_HOME_SESSION"))


def worker_facts(directory, agent, profile, address):
    """What belongs to one worker: its task, and the worktree it would take with it."""
    address = pa.local_id(address)
    ledger = pa.read_json(os.path.join(directory, "ledger.json"), {}) or {}
    task = next((t for t in ledger.get("tasks", []) if any(w.get("address") == address for w in t.get("workers") or [])), None)
    info = pa.hc_get(address)
    out = {"address": address, "name": info.get("name"), "state": info.get("state") or "gone", "task": None, "worktree": None}
    if not task:
        return out, None
    crew = task.get("workers") or []
    entry = next(w for w in crew if w.get("address") == address)
    out.update(task=task.get("id"), title=task.get("title"), taskState=task.get("state"), role=entry.get("role"),
               onlyWorker=len(crew) == 1, keep=bool(entry.get("keep")), finished=bool(entry.get("finished")), closed=entry.get("closed"))
    row = worktree_state(directory, agent, profile).get(worker_tree(task, entry))  # its own worktree only, never a sibling job's
    if row and task["id"] not in row["tasks"]:  # never another task's worktree (worker_tree gives only its own; a second guard)
        row = None
    if row and row["facts"] and not row["facts"].get("main") and not row["facts"].get("unreadable"):
        # Another task that counts on this worktree and is not done or parked keeps it (rule (a), every ledger shape: card d-094).
        # It counts the way pa-worktree does (row_tasks): it records the worktree, a folder inside it or its container
        # (t-048, t-020 RL-V5). row["counted"] is that set with the exact ones first, in ledger order.
        held = [t for t in row.get("counted") or row["tasks"] if t != task["id"] and row["states"].get(t) not in ("done", "parked")]
        others = [w["address"] for w in row["workers"] if w["fate"] == "live" and w["address"] != address] + row["others"]
        others += ["task {} ({})".format(clean(t), state_word(row["states"].get(t))) for t in held]
        out["worktree"] = {"path": row["path"], "kind": row["facts"].get("kind", "worktree"), "uncommitted": row["facts"]["uncommitted"],
                           "unpushed": row["facts"]["unpushed"], "removesBranches": bool(row["facts"].get("removesBranches")),
                           "branch": row["facts"]["branch"], "sharedWith": others, "keep": row["keep"], "heldBy": held,
                           "tasks": list(row["tasks"]),  # whose ledger entries a removal marks
                           "related": {t: list(p) for t, p in (row.get("related") or {}).items()}}  # marked once each path is gone
    return out, row


# ---- pa-worker close ------------------------------------------------------------------------
# The agent ends a worker once it has read its report. Every check below only reads; a refusal
# changes nothing. Only a terminal recorded in this space's ledger is ever ended.

CLOSABLE = ("idle", "done")  # hc statuses a worker may be closed in (also: exited, or already gone)
ORCHESTRATOR_FILES = ("intent.md",)  # the orchestrator's own artefact (sdlc.md); spec.md and plan.md are workers'
TOOL_FILES = ("GATE.json", "WORKTREE.md")  # written by pa-gate and pa-worktree, never a worker's report


def tool_file(name):
    """True for a file the tools write into a task's evidence folder: TOOL_FILES, and a job's WORKTREE-<job>.md and
    GATE-<job>.json (t-192 R2)."""
    return name in TOOL_FILES or re.fullmatch(r"WORKTREE-[a-z0-9-]+\.md|GATE-[a-z0-9-]+\.json", name) is not None


@contextlib.contextmanager
def ledger_lock(directory):
    """The lock pa-task and pa-worker take around a read-modify-write of ledger.json."""
    import fcntl
    with open(os.path.join(directory, ".ledger.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def read_ledger(directory):
    return pa.read_json(os.path.join(directory, "ledger.json"), {}) or {}


def append_journal(directory, line):
    with open(os.path.join(directory, "journal.jsonl"), "a") as journal:
        journal.write(json.dumps(dict({"at": pa.now()}, **line)) + "\n")


def recorded(ledger, address, info=None):
    """(task, entry) of the worker the ledger records at this address, or (None, None). When the address
    is recorded more than once (a core restart can hand an old address out again), an entry not yet
    finished comes first, and among those the one whose name is the live terminal's name."""
    found = [(t, w) for t in ledger.get("tasks", []) for w in t.get("workers") or [] if w.get("address") == address]
    live_name = (info or {}).get("name")
    found.sort(key=lambda tw: (bool(tw[1].get("finished")), not (live_name and tw[1].get("name") == live_name)))
    return found[0] if found else (None, None)


def read_terminal(address):
    """(hc get record, None); ({}, None) when hc says there is no such terminal; (None, why) when hc
    cannot tell. `pa.hc_get` gives {} for any hc error, so an empty answer is checked with `hc ls --all`."""
    info = pa.hc_get(address)
    if info:
        return info, None
    try:
        listed = json.loads(pa.call(["hc", "ls", "--all", "--json"])).get("terminals", [])
    except (pa.Problem, ValueError, AttributeError):
        return None, "hc did not answer"
    if any(t.get("address") == address for t in listed):
        return None, "hc get failed, but hc ls lists it"
    return {}, None


def worker_label(address, entry, info):
    return "{} ({})".format(address, entry.get("name") or (info or {}).get("name") or "unnamed")


def worker_start(entry, info):
    """When the worker started (epoch seconds): the later of the ledger's `started` (the agent writes it) and
    hc's created_ms (hc records it), so a wrong early `started` cannot make an older file count; None if neither."""
    known = []
    if entry.get("started"):
        stamp = pa.parse_time(str(entry["started"]))
        if stamp:
            known.append(stamp)
    created = (info or {}).get("created_ms")
    if isinstance(created, (int, float)) and created > 0:
        known.append(created / 1000.0)
    return max(known) if known else None


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def identity_refusal(address, task, entry, info):
    """The terminal at this address is no longer the recorded worker, or the person said to keep it."""
    if info:
        got, want = info.get("name") or "", entry.get("name")
        if (got != want) if want else not got.startswith(str(task.get("id")) + "-"):
            return "name", "{} is now a different terminal ({}), not {}; nothing was closed".format(
                address, got or "unnamed", want or "a worker of " + str(task.get("id")))
    if entry.get("keep"):
        return "keep", '{} is marked keep ({}: "{}"); only the person closes it, with the × in the panel'.format(
            worker_label(address, entry, info), entry.get("keptAt"), entry.get("keepQuote"))
    return None


status_word = pa.status_word  # one reading of hc status, shared with pa-pass


def status_refusal(address, entry, info):
    """Only an idle, done, exited or gone worker is closed."""
    if not info:
        return None
    status = status_word(info)
    if status == "working":
        return "working", "{} is working; close it after its report".format(worker_label(address, entry, info))
    if status == "blocked":
        return "blocked", "{} is blocked on a question or a permission; bring that to the person, do not close it".format(
            worker_label(address, entry, info))
    if status in CLOSABLE or (not status and info.get("state") == "finished"):
        return None
    return "status", "{} has status {}; nothing was closed".format(address, status or "none")


def own_file(name):
    """The files pa-hooks install writes into a worktree: never counted as work."""
    return name in (SETTINGS, TASK_FILE) or name.startswith(SETTINGS + ".bak-pa-hooks")


def status_files(path):
    """[(XY status, path)] of the uncommitted files in a git checkout, every untracked file listed
    whatever the repository's own config, without pa-hooks' own files; None if git cannot tell.
    Ignored files are not seen."""
    try:
        # --no-optional-locks (t-131): never the index lock, never an index refresh, in a worktree someone works in
        r = subprocess.run(["git", "--no-optional-locks", "-C", path, "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode:
        return None
    files = []
    for line in r.stdout.splitlines():
        name = line[3:].split(" -> ")[-1].strip().strip('"')
        if name and not own_file(name):
            files.append((line[:2].strip(), name))
    return files


def uncommitted_refusal(path, names_known=True, count=None):
    """A refusal for a checkout with uncommitted files, or None."""
    files = status_files(path)
    if files is None:
        return "unreadable", "cannot tell whether {} holds uncommitted work (git status failed); nothing was closed".format(path)
    names = [f for _s, f in files]
    count = max(count or 0, len(names))
    if count:
        listed = names[:5] + (["+{} more".format(len(names) - 5)] if len(names) > 5 else [])
        return "uncommitted", "{} has {} uncommitted file(s){}; have them committed or reported first. Nothing was closed".format(
            path, count, " ({})".format(", ".join(listed)) if listed else "")
    return None


def worktree_refusal(task, profile, info=None, entry=None):
    """(refusal or None, what was checked, what could not be seen). It looks at the worker's own worktree
    (worker_tree: its entry's `worktree`, else the task's main one; never a sibling job's), each repository a
    profile kind lists, too, and at the git checkout the worker's terminal runs in.
    A worktree that is gone (or none) is fine: there is nothing in it to lose."""
    checked, unseen = [], []
    path = worker_tree(task, entry)
    if path and os.path.exists(path):
        facts = worktree_facts(path, profile)
        custom = (profile or {}).get("worktree") or {}
        if not facts:
            why = ("the profile's worktree status script failed" if custom.get("marker") and os.path.exists(os.path.join(path, custom["marker"]))
                   else "not a git worktree")
            return ("unreadable", "cannot tell whether {} holds uncommitted work ({}); nothing was closed".format(path, why)), checked, unseen
        if facts.get("unreadable"):
            return ("unreadable", "cannot tell whether {} holds uncommitted work ({}); nothing was closed".format(
                path, facts["unreadable"])), checked, unseen
        if facts.get("remove"):  # the profile's own kind: its script's count, and each repository it lists
            for repo in facts.get("repos") or []:
                refusal = uncommitted_refusal(repo)
                if refusal:
                    return refusal, checked, unseen
                checked.append(repo)
            if facts["uncommitted"]:
                return ("uncommitted", "{} has {} uncommitted file(s); have them committed or reported first. Nothing was closed".format(
                    path, facts["uncommitted"])), checked, unseen
            if not facts.get("repos"):
                unseen.append("{} is checked by its profile's script only (it lists no repositories)".format(path))
            checked.append(path)
        else:
            refusal = uncommitted_refusal(path)
            if refusal:
                return refusal, checked, unseen
            checked.append(path)
    cwd = (info or {}).get("cwd") or ((info or {}).get("foreground") or {}).get("cwd")
    if cwd and not any(inside(cwd, c) for c in checked):
        cwd = os.path.realpath(cwd)
        code, top, _ = git(cwd, "rev-parse", "--show-toplevel") if os.path.isdir(cwd) else (1, "", "")
        if code or not top:
            unseen.append("{} is not in a git checkout".format(cwd))
        elif not any(inside(top, c) for c in checked):
            refusal = uncommitted_refusal(os.path.realpath(top))
            if refusal:
                return refusal, checked, unseen
            checked.append(os.path.realpath(top))
    return None, checked, unseen


def checked_note(checked, unseen):
    return "Checked for uncommitted work: {}; ignored files are not seen{}.".format(
        ", ".join(checked) or "none (no worktree, and no git checkout where it ran)",
        "; not seen: " + "; ".join(unseen) if unseen else "")


def report_problem(folder, rel, given, address, start):
    """Why a file in the evidence folder cannot stand for this worker's report, or None."""
    if rel in ORCHESTRATOR_FILES:
        return "{} is the orchestrator's own file, not a worker's report; name the file that holds this worker's report".format(given)
    if tool_file(os.path.basename(rel)):
        return "{} is written by the Project Agent's tools, not a worker's report; name the file that holds this worker's report".format(given)
    if start is not None:
        written = os.path.getmtime(os.path.join(folder, rel))
        if written < start:
            return "evidence {} was last written before {} started ({} < {}); name the file that holds this worker's report".format(
                given, address, iso(written), iso(start))
    return None


def report_path(directory, task_id, address, given, start=None):
    """The report file the agent read, relative to evidence/<task>/. It must be inside that folder, not
    empty, not the orchestrator's or the tools' own file, and written after the worker started."""
    if not given:
        raise pa.Problem("pa-worker close needs --evidence <file>: the report you read, in evidence/{}/".format(task_id))
    folder = os.path.realpath(os.path.join(directory, "evidence", task_id))
    path = os.path.realpath(given if os.path.isabs(given) else os.path.join(folder, given))
    if not path.startswith(folder + os.sep):
        raise pa.Problem("{} is not in {}'s task evidence folder evidence/{}/".format(given, address, task_id))
    if not os.path.isfile(path):
        raise pa.Problem("evidence {} is missing".format(given))
    with open(path, "rb") as f:
        if not f.read().strip():
            raise pa.Problem("evidence {} is empty; read the report before closing".format(given))
    rel = os.path.relpath(path, folder)
    problem = report_problem(folder, rel, given, address, start)
    if problem:
        raise pa.Problem(problem)
    return rel


def end_worker(directory, ledger, task, entry, address, info, report, by, trigger, actor=None, why=None):
    """Ends one worker whose checks passed: kill if live, rm, then mark its ledger entry and journal
    it. The caller holds the ledger lock, re-read the ledger and re-checked the status (`info`, the
    `hc get` it checked; empty when the terminal is already gone). A kill that leaves the terminal
    live raises, and nothing is recorded. Returns the line to print. Never touches the worktree, the
    branch, the task or the evidence. `by` goes into the entry's closedBy; `actor` (default: `by`) is
    who the journal line says ran it."""
    note, live = "", False
    try:
        if info and info.get("state") == "live":
            pa.call(["hc", "kill", address], check=False)
            live = pa.hc_get(address).get("state") == "live"
        if info and not live:
            pa.call(["hc", "rm", address], check=False)
            note = " (ended {} but hc rm did not remove its record)".format(address) if pa.hc_get(address) else ""
    except pa.Problem as e:  # t-136 (t-133 RL4): hc did not answer (the waiter's CALL_TIMEOUT); the kill may have landed
        raise Refused("unsure", "{}: {} while it was being ended; it may have been closed".format(address, e))
    if live:
        raise pa.Problem("hc kill did not end {} (it is still live); nothing was recorded".format(address))
    stamp = pa.now()
    entry.update(finished=True, closed=stamp, closedBy=by, report=report)
    task["updated"] = stamp
    pa.write_json(os.path.join(directory, "ledger.json"), ledger)
    name = entry.get("name") or "unnamed"
    append_journal(directory, {"by": actor or by, "trigger": trigger, "task": task.get("id"),
                               "why": why or "closed worker {} ({}) after reading its report {}".format(address, name, report)})
    costs = pa.costs_module()  # t-009: one closed event for the space's costs; never changes what close does
    if costs:
        try:
            costs.record_closed(directory, address, "pa-worker close" + (" --card" if trigger == "leftovers" else ""))
        except Exception:  # noqa: BLE001
            pass
    return "Closed worker {} ({}, task {}); its worktree and branch are untouched.{}".format(address, name, task.get("id"), note)


def unrecorded(address):
    return pa.Problem("{} is not a recorded worker of this space; nothing was closed (if it is yours, close it in Herdr)".format(address))


class Refused(pa.Problem):
    """A close refused on the terminal's identity or status; `kind` is the refusal's first word (t-042)."""

    def __init__(self, kind, text):
        super().__init__(text)
        self.kind = kind


def close_worker(directory, agent, profile, address, evidence, card_id=None, trigger="close", stop=None):
    """pa-worker close <address> --evidence F (spec 3.2 and 3.3), or --card <id>: the person's answer on a
    leftovers card stands in for the evidence file (card d-031). Returns the lines to print. stop(info): the
    --when-idle waiter's own refusal ((kind, text) or None), checked with each status read, the last one under the
    ledger lock right before the kill (t-133)."""
    if "/" in address:
        raise pa.Problem("{} is on another machine; pa-worker close runs on the machine the space runs on".format(address))
    if address == agent.get("terminal"):
        raise pa.Problem("that is the Project Agent itself; it ends when its space is closed")
    info, why = read_terminal(address)
    task, entry = recorded(read_ledger(directory), address, info)
    if not entry:
        raise unrecorded(address)
    if info is None:
        raise pa.Problem("cannot read {}'s state from hc ({}); nothing was closed".format(address, why))
    refusal = identity_refusal(address, task, entry, info) or (stop and stop(info))
    if refusal:
        raise Refused(*refusal)
    if entry.get("finished") and not info:
        return ["{} is already closed.".format(address)]
    card = leftovers_card(directory, card_id) if card_id else None
    if card:
        problem = card_refusal(card, address, task, entry, info)
        if problem:
            raise pa.Problem(problem)
        report = "card " + card_id
    else:
        report = report_path(directory, task["id"], address, evidence, worker_start(entry, info))
    refusal = status_refusal(address, entry, info)
    if refusal:
        raise Refused(*refusal)
    refusal, checked, unseen = worktree_refusal(task, profile, info, entry)
    if refusal:
        raise pa.Problem(refusal[1])
    by = "agent" if by_an_agent() else "person"
    with ledger_lock(directory):
        ledger = read_ledger(directory)
        info, why = read_terminal(address)
        task, entry = recorded(ledger, address, info)
        if not entry:
            raise unrecorded(address)
        if info is None:
            raise pa.Problem("cannot read {}'s state from hc ({}); nothing was closed".format(address, why))
        refusal = identity_refusal(address, task, entry, info) or (stop and stop(info)) or status_refusal(address, entry, info)
        if refusal:
            raise Refused(*refusal)
        if card:
            problem = card_refusal(card, address, task, entry, info)
            if problem:
                raise pa.Problem(problem)
        if entry.get("finished") and not info:  # closed meanwhile (another pa-worker close)
            return ["{} is already closed.".format(address)]
        if card:
            line = end_worker(directory, ledger, task, entry, address, info, report, by, "leftovers",
                              why="closed worker {} ({}) on the person's answer to card {}".format(address, entry.get("name") or "unnamed", card_id))
        else:
            line = end_worker(directory, ledger, task, entry, address, info, report, by, trigger)
    return [line, checked_note(checked, unseen)]


# ---- pa-worker close --when-idle (t-042) ------------------------------------------------------
# A worker that reported is often still finishing its turn when the agent closes it. With --when-idle
# that refusal (and only that one) leaves one detached waiter: the same command with --idle-waiter,
# which runs close_worker again, every guard included, until the worker is idle or the limit is reached.
# It journals what happened and sends no hc message, so it starts no Project Agent pass. A worker that
# becomes blocked is never closed: it is left for the person.

WHEN_IDLE_LIMIT = 600  # seconds the waiter waits, at most (awake time and wall-clock time both, t-133)
WHEN_IDLE_POLL = 5  # seconds between its checks
WHEN_IDLE_HC_TIMEOUT = 30  # seconds one hc call of the waiter may take (t-133: a hung hc cannot hold it, or the lock)


WHEN_IDLE_START_GRACE = 60  # seconds a .starting marker counts as a waiter that has not yet taken its lock (t-136)


def idle_file(directory, address, ext):
    return os.path.join(directory, ".close-when-idle-{}.{}".format(re.sub(r"[^A-Za-z0-9_-]", "_", address), ext))


def spawn_idle_closer(directory, space, address, report, turn_ms=None):
    """Starts the waiter in its own session, detached from the caller, with no terminal input or output. turn_ms: hc's
    status_ms of the working turn it waits for (t-133)."""
    argv = [sys.executable, os.path.abspath(sys.argv[0]), "close", address, "--evidence", report, "--space", space, "--idle-waiter"]
    if turn_ms is not None:
        argv += ["--turn-ms", str(turn_ms)]
    subprocess.Popen(argv, cwd=directory, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)


def close_when_idle(directory, agent, profile, space, address, evidence):
    """pa-worker close <address> --evidence F --when-idle: close now, or leave a waiter if it is only working."""
    try:
        return close_worker(directory, agent, profile, address, evidence)
    except Refused as e:
        if e.kind != "working":
            raise
    task, entry = recorded(read_ledger(directory), address)
    try:
        os.unlink(idle_file(directory, address, "cancel"))  # a new --when-idle: an old cancel no longer applies
    except FileNotFoundError:
        pass
    turn = pa.hc_get(address).get("status_ms")
    report = report_path(directory, task["id"], address, evidence)
    starting = idle_file(directory, address, "starting")  # t-136 (t-133 RL3): a cancel before the child holds its lock
    with open(starting, "w") as f:
        f.write(pa.now() + "\n")
    try:
        spawn_idle_closer(directory, space, address, report,
                          turn_ms=turn if isinstance(turn, (int, float)) and not isinstance(turn, bool) else None)
    except BaseException:
        os.unlink(starting)
        raise
    return ["{} is working; it will be closed once it is idle (checked every {} s, for up to {} min), under the same checks, "
            "and the result journaled (trigger close-when-idle). Nothing more to do for it. To send it a follow-up "
            "first, run pa-worker close {} --cancel-when-idle.".format(
                worker_label(address, entry, None), WHEN_IDLE_POLL, int(WHEN_IDLE_LIMIT // 60), address)]


def idle_stop(directory, address, turn_ms):
    """The waiter's own refusals (t-133): a cancel by the Project Agent, or a new turn (another status_ms while working)."""
    def stop(info):
        if os.path.exists(idle_file(directory, address, "cancel")):
            return "cancelled", "{}: the close was cancelled (pa-worker close --cancel-when-idle)".format(address)
        if turn_ms is not None and info and status_word(info) == "working" and info.get("status_ms") not in (None, turn_ms):
            return "turn", "{}: a new turn began after --when-idle (a follow-up)".format(address)
        return None
    return stop


def wait_idle(directory, agent, profile, address, evidence, turn_ms=None):
    """The waiter: one per worker (a lock in the space folder; a second one leaves at once). Every refusal
    but `working` ends it, and so does the limit; the close journals itself, anything else is journaled here."""
    import fcntl
    with open(idle_file(directory, address, "lock"), "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return
        try:
            os.unlink(idle_file(directory, address, "starting"))  # its lock now says it is here
        except FileNotFoundError:
            pass
        saved, pa.CALL_TIMEOUT = pa.CALL_TIMEOUT, WHEN_IDLE_HC_TIMEOUT
        try:
            kind, text = waiting(directory, agent, profile, address, evidence, turn_ms)
        finally:
            pa.CALL_TIMEOUT = saved
            try:
                os.unlink(idle_file(directory, address, "cancel"))
            except FileNotFoundError:
                pass
    if kind is None:
        return
    if kind == "working":
        text = "{} still working after {} s".format(address, WHEN_IDLE_LIMIT)
    what = {"blocked": "left for the person; not closed",
            "unsure": "run pa-worker close {} again to record it".format(address)}.get(kind, "not closed")
    task, _entry = recorded(read_ledger(directory), address)
    append_journal(directory, {"by": "agent" if by_an_agent() else "person", "trigger": "close-when-idle",
                               "task": (task or {}).get("id"), "why": "{}: {} (pa-worker close --when-idle)".format(text, what)})


def waiting(directory, agent, profile, address, evidence, turn_ms):
    """The waiter's loop: (None, None) once closed, else (refusal kind, text)."""
    stop = idle_stop(directory, address, turn_ms)
    deadline, wall = time.monotonic() + WHEN_IDLE_LIMIT, time.time() + WHEN_IDLE_LIMIT
    while True:
        try:
            close_worker(directory, agent, profile, address, evidence, trigger="close-when-idle", stop=stop)
            return None, None
        except Refused as e:
            if e.kind == "working" and time.monotonic() < deadline and time.time() < wall:
                time.sleep(WHEN_IDLE_POLL)
                continue
            return e.kind, str(e)
        except Exception as e:  # noqa: BLE001 (detached: nobody sees a traceback, so it is journaled)
            return "error", str(e) or type(e).__name__


def cancel_when_idle(directory, address):
    """pa-worker close <address> --cancel-when-idle (t-133): the waiter left by --when-idle closes nothing. Written under
    the ledger lock, which the waiter's last check before the kill holds too: the cancel lands first, or it was closed."""
    import fcntl
    with ledger_lock(directory):
        # t-136 (t-133 RL3): spawned, and not yet holding its lock. Read before the lock probe: the waiter locks first and
        # then unlinks the marker, so a marker gone here means the lock is already held when the probe below runs.
        try:
            running = time.time() - os.stat(idle_file(directory, address, "starting")).st_mtime < WHEN_IDLE_START_GRACE
        except OSError:
            running = False
        with open(idle_file(directory, address, "lock"), "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock, fcntl.LOCK_UN)
            except OSError:
                running = True
        task, entry = recorded(read_ledger(directory), address)
        done = entry and entry.get("finished")
        if running and done:  # the waiter closed it and has not yet left its lock: nothing is left to cancel
            return ["{}: already closed by close --when-idle; nothing to cancel.".format(address)]
        if not running:
            return ["{}: no pa-worker close --when-idle is waiting for {}; nothing to cancel{}.".format(
                address, address, " (it is already closed)" if done else "")]
        with open(idle_file(directory, address, "cancel"), "w") as f:
            f.write(pa.now() + "\n")
        append_journal(directory, {"by": "agent" if by_an_agent() else "person", "trigger": "close-when-idle",
                                   "task": (task or {}).get("id"),
                                   "why": "{}: close --when-idle cancelled; the worker stays open".format(address)})
    return ["{}: close --when-idle cancelled; the worker stays open (close it later with pa-worker close).".format(address)]


def keep_worker(directory, agent, address, quote, off):
    """pa-worker keep <address> --quote "<the person's words>" [--off] (spec 3.5). Both ways need the
    person's own words, stored and journaled as given. It only marks the ledger entry; it reopens nothing."""
    if "/" in address:
        raise pa.Problem("{} is on another machine; pa-worker keep runs on the machine the space runs on".format(address))
    if address == agent.get("terminal"):
        raise pa.Problem("that is the Project Agent itself; it ends when its space is closed")
    if not (quote or "").strip():
        raise pa.Problem('pa-worker keep{} needs --quote "<the person\'s own words>"'.format(" --off" if off else ""))
    quote = quote.strip()
    with ledger_lock(directory):
        ledger = read_ledger(directory)
        task, entry = recorded(ledger, address)
        if not entry:
            raise pa.Problem("{} is not a recorded worker of this space; nothing was marked".format(address))
        name = entry.get("name") or "unnamed"
        if off and not entry.get("keep"):
            print("{} is not marked keep.".format(address))
            return
        stamp = pa.now()
        if off:
            entry.update(keep=False, unkeptAt=stamp, unkeepQuote=quote)
            why = 'the person said {} may be closed again: "{}"'.format(address, quote)
        else:
            entry.update(keep=True, keptAt=stamp, keepQuote=quote)
            why = 'the person asked to keep {} open: "{}"'.format(address, quote)
        task["updated"] = stamp
        pa.write_json(os.path.join(directory, "ledger.json"), ledger)
        append_journal(directory, {"by": "agent" if by_an_agent() else "person", "trigger": "keep", "task": task.get("id"), "why": why})
    print("{} ({}, task {}) {}.".format(address, name, task.get("id"), "is no longer kept" if off else "is marked keep: it is not closed by the agent"))


LEFTOVERS_CARD = "d-leftovers"
LEFTOVERS_PER_CARD = 10  # workers on one leftovers card, at most; fewer when their lines pass CONTEXT_MAX
CLOSE_ALL, CLOSE_NONE = "Close all listed", "Close none"


def worker_words(address, name):
    """Patterns for the worker's address and name, as whole words."""
    words = [r"(?<![A-Za-z0-9]){}(?![A-Za-z0-9])".format(re.escape(address))]
    if name:
        words.append(r"(?<![A-Za-z0-9-]){}(?![A-Za-z0-9-])".format(re.escape(name)))
    return words


def line_text(line):
    return " ".join(str(v) for k, v in sorted(line.items()) if k != "at" and isinstance(v, (str, int, float)))


def read_journal(directory):
    lines = []
    for raw_line in pa.read_text(os.path.join(directory, "journal.jsonl")).splitlines():
        try:
            line = json.loads(raw_line)
        except ValueError:
            continue
        if isinstance(line, dict):
            lines.append(line)
    return lines


def idle_for(info, now=None):
    """'3 h' since hc last saw the status change (status_ms), or 'unknown'."""
    changed = (info or {}).get("status_ms")
    if not isinstance(changed, (int, float)) or changed <= 0:
        return "unknown"
    seconds = max(0, int((now or time.time()) - changed / 1000.0))
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if seconds >= size:
            return "{} {}".format(seconds // size, unit)
    return "{} s".format(seconds)


def uncommitted_in(task, profile, entry=None):
    """(worktree path or None, uncommitted count or None when it cannot be read, file names), for the worker's
    own worktree (worker_tree)."""
    path = worker_tree(task, entry)
    if not path:
        return None, 0, []
    if not os.path.exists(path):
        return path, 0, []
    facts = worktree_facts(path, profile)
    if not facts or facts.get("unreadable"):
        return path, None, []
    if facts.get("remove"):
        return path, facts["uncommitted"], []
    files = status_files(path)
    return (path, None, []) if files is None else (path, len(files), [clean(f) for _s, f in files])


def leftover_list(directory, agent, profile, ledger):
    """The old-style workers to put on the card, and the ones held back with why. It reads only: hc get,
    git status, the journal. The last journal line naming a worker is quoted for the person, never read."""
    journal = read_journal(directory)
    listed, held = [], []
    for task in ledger.get("tasks", []):
        for entry in task.get("workers") or []:
            address = entry.get("address") or ""
            if entry.get("finished") or not address or address == agent.get("terminal"):
                continue
            base = {"address": address, "name": entry.get("name"), "task": task.get("id")}
            if "/" in address:
                held.append(dict(base, why="on another machine"))
                continue
            info = pa.hc_get(address)
            if entry.get("keep"):
                held.append(dict(base, why="marked keep"))
                continue
            refusal = identity_refusal(address, task, entry, info) or status_refusal(address, entry, info)
            if refusal:
                why = {"name": "the address now names another terminal ({})".format(clean(info.get("name") or "unnamed")),
                       "working": "working", "blocked": "blocked"}.get(refusal[0], "status {}".format(clean(status_word(info) or "none")))
                held.append(dict(base, why=why))
                continue
            path, count, files = uncommitted_in(task, profile, entry)
            last = None
            for line in journal:
                if any(re.search(w, line_text(line)) for w in worker_words(address, entry.get("name"))):
                    last = line
            listed.append(dict(base, name=clean(entry.get("name")), title=clean(task.get("title")), taskState=clean(task.get("state")),
                               role=clean(entry.get("role") or "worker"), state="gone" if not info else clean(status_word(info) or info.get("state")),
                               idle="gone" if not info else idle_for(info), worktree=path, uncommitted=count, uncommittedFiles=files[:50],
                               lastLine={k: cut(clean(last.get(k)), 300) for k in ("at", "trigger", "why") if last.get(k) is not None} if last else None))
    return listed, held


def leftover_line(w):
    tree = ("; uncommitted: cannot read {}".format(w["worktree"]) if w["uncommitted"] is None else
            "; uncommitted: {} in {}{}".format(w["uncommitted"], w["worktree"], " ({})".format(", ".join(cut_path(f) for f in w["uncommittedFiles"][:5])
                                                                                       + (", …" if w["uncommitted"] > 5 else "")) if w["uncommittedFiles"] else "")
            if w["uncommitted"] else "; nothing uncommitted" if w["worktree"] else "; no worktree")
    head = '{} {} ({}, {} "{}" {}): {}, idle {}{}'.format(w["address"], w["name"] or "unnamed", w["role"], w["task"], cut(w["title"], 60),
                                                         w["taskState"], w["state"], w["idle"], tree)
    last = w.get("lastLine")
    quoted = ('  last journal line naming it (quoted, not read): "{}" ({}, {})'.format(cut(last.get("why", ""), 160), last.get("at", "?"),
                                                                                       last.get("trigger", "?")) if last else "  no journal line names it")
    return [head, quoted]


def leftovers_card_text(listed, held):
    """(the workers on the card, its context). A card holds only the workers whose lines it shows in full:
    at most LEFTOVERS_PER_CARD, and no more than fit in CONTEXT_MAX with the header and the footer. The
    panel shows the whole context, so what "Close all listed" can close is exactly what the person saw."""
    header = "Workers recorded before one-worker-per-job, listed by `pa-worker close --leftovers`. Nothing has been closed."
    held_line = ("Not listed (the agent closes them later, after their reports): " + ", ".join(
        "{} ({})".format(h["address"], h["why"]) for h in held)) if held else ""
    held_line = cut(held_line, 400)
    shown, body = [], []
    for w in listed[:LEFTOVERS_PER_CARD]:
        lines = leftover_line(w)
        more = len(listed) - len(shown) - 1
        footer = ([("{} more old-style workers did not fit on this card; they come on the next card, once this one is "
                    "answered.").format(more)] if more else []) + ([held_line] if held_line else [])
        if len("\n".join([header] + body + lines + footer)) > CONTEXT_MAX and shown:
            break
        shown.append(w)
        body += lines
    more = len(listed) - len(shown)
    footer = ([("{} more old-style workers did not fit on this card; they come on the next card, once this one is "
                "answered.").format(more)] if more else []) + ([held_line] if held_line else [])
    return shown, "\n".join([header] + body + footer)[:CONTEXT_MAX]


def shown_before(directory, policy):
    """Workers on earlier leftovers cards that the person answered: they are not offered again."""
    seen = set()
    for card_id in (policy or {}).get("cards") or []:
        card = pa.read_json(pa.card_file(directory, card_id)) or {}
        if card.get("status") == "answered":
            seen |= {(w.get("address"), w.get("task"), w.get("name")) for w in card.get("workers") or []}
    return seen


def close_leftovers(directory, agent, profile, dry_run):
    """pa-worker close --leftovers [--dry-run], as the person decided on card d-031: put the recorded workers
    from before one-worker-per-job that are idle and not finished on a decision card, one card at a time, and
    close nothing; `pa-worker close --card` does that after the person answers it in the panel. The first run
    sets workerPolicy. A later run opens the next card only when the last one is answered and some workers did
    not fit on it, or when it was withdrawn (a note); else it says it already ran."""
    ledger = read_ledger(directory)
    policy = ledger.get("workerPolicy")
    if policy:
        last_id = policy.get("card")
        last = pa.read_json(pa.card_file(directory, last_id)) if last_id else None
        again = last and (last.get("status") == "withdrawn" or (last.get("status") == "answered" and last.get("more")))
        if not again:
            print("--leftovers already ran (workerPolicy since {}; card {}{}); nothing was done.".format(
                policy.get("since"), last_id or "none",
                " is open: the person answers it first" if last and last.get("status") == "open" else ""))
            return
    seen = shown_before(directory, policy)
    listed, held = leftover_list(directory, agent, profile, ledger)
    listed = [w for w in listed if (w["address"], w["task"], w["name"]) not in seen]
    on_card, context = leftovers_card_text(listed, held)
    cards = list((policy or {}).get("cards") or [])
    card_id = LEFTOVERS_CARD if not cards else "{}-{}".format(LEFTOVERS_CARD, len(cards) + 1)
    if dry_run:
        for w in on_card:
            print("would list {} ({}, {}): {}".format(w["address"], w["name"] or "unnamed", w["task"], leftover_line(w)[0].split(": ", 1)[1]))
        if len(listed) > len(on_card):
            print("{} more would wait for the next card".format(len(listed) - len(on_card)))
        for h in held:
            print("not listed {} ({}, {}): {}".format(h["address"], h.get("name") or "unnamed", h["task"], h["why"]))
        if not listed:
            print("No old-style workers to list.")
        return
    by = "agent" if by_an_agent() else "person"
    path = pa.card_file(directory, card_id)  # an archived card of that name counts as there (t-054)
    with ledger_lock(directory):
        ledger = read_ledger(directory)
        opened = bool(on_card) and not os.path.exists(path)
        if opened:
            before = carried(pa.read_json(pa.card_file(directory, last_id)) if policy and last_id else None)
            pa.write_card(directory, path, dict(dict(before, previousCard=last_id) if before else {}, **{
                "id": card_id, "kind": "leftovers", "task": None,
                "question": "Close the {} worker(s) left from before one-worker-per-job that are listed here?".format(len(on_card)),
                "context": context,
                "options": [{"label": CLOSE_ALL, "detail": "The agent closes each worker listed here with `pa-worker close --card {}`. It skips "
                             "one marked keep (tell the agent \"keep tNN\" first), and one that is working, blocked, has uncommitted files, or "
                             "became active or changed since this card. Leave the note empty: a note means nothing on this card is closed. "
                             "Worktrees, branches, tasks and evidence stay.".format(card_id)},
                            {"label": CLOSE_NONE, "detail": "Leave them. The agent closes each later, once it has read its report."}],
                "evidence": [], "recommendation": None, "status": "open", "answer": None, "note": None,
                "workers": on_card, "more": len(listed) - len(on_card), "notListed": held, "created": pa.now(), "answered": None}))
        ledger["workerPolicy"] = dict(policy or {"style": "per-job", "since": pa.now()},
                                      card=card_id if opened else (policy or {}).get("card"), cards=cards + ([card_id] if opened else []))
        pa.write_json(os.path.join(directory, "ledger.json"), ledger)
        append_journal(directory, {"by": by, "trigger": "leftovers", "why": "listed {} old-style worker(s) on card {} for the person: {}; {} wait for "
                                   "the next card; not listed: {}; nothing was closed".format(
                                       len(on_card), card_id if opened else "(none)", ", ".join(w["address"] for w in on_card) or "none",
                                       len(listed) - len(on_card), ", ".join("{} ({})".format(h["address"], h["why"]) for h in held) or "none")})
    for w in on_card:
        print("listed {} ({}, {}): {}".format(w["address"], w["name"] or "unnamed", w["task"], leftover_line(w)[0].split(": ", 1)[1]))
    if len(listed) > len(on_card):
        print("{} more wait for the next card: once {} is answered, run pa-worker close --leftovers again".format(len(listed) - len(on_card), card_id))
    for h in held:
        print("not listed {} ({}, {}): {}".format(h["address"], h.get("name") or "unnamed", h["task"], h["why"]))
    if not on_card:
        print("No old-style workers to list; no card opened. workerPolicy is set (per-job).")
    elif opened:
        print("opened card {} for the person; nothing was closed. workerPolicy is set (per-job).".format(card_id))
    else:
        print("card {} exists already; nothing was closed.".format(card_id))


def leftovers_card(directory, card_id):
    """The answered leftovers card that lets `pa-worker close --card` close workers, or a refusal."""
    path = pa.card_file(directory, card_id) if pa.ID.match(card_id or "") else None
    card = pa.read_json(path) if path else None
    if not card or card.get("kind") != "leftovers":
        raise pa.Problem("no leftovers card {}; nothing was closed".format(card_id))
    if card.get("status") == "open":
        raise pa.Problem("card {} is not answered yet; the person answers it in the panel. Nothing was closed".format(card_id))
    if card.get("status") != "answered":
        raise pa.Problem("card {} is {}; nothing was closed".format(card_id, card.get("status")))
    if "in conversation" in str(card.get("answeredBy") or ""):
        raise pa.Problem("card {} is answered in the panel only; nothing was closed".format(card_id))
    if pa.is_other(card):
        # The person's own words are never parsed, as a note: close nothing, withdraw, and the next sweep asks again.
        words = cut(clean(card.get("note")), 200)
        pa.keep_previous(card, pa.OTHER)
        card.update(status="withdrawn", answer=None, answeredBy="pa-worker close: withdrawn", withdrawnAt=pa.now())
        card.pop("other", None)
        pa.write_card(directory, path, card)
        append_journal(directory, {"by": "agent" if by_an_agent() else "person", "trigger": "leftovers",
                                   "why": "withdrew card {}: it was answered Other ({}); nothing was closed".format(card_id, words)})
        raise pa.Problem('card {} was answered Other, in the person\'s own words: "{}". Nothing was closed; the words are not read by '
                         'this tool. Act on them (ask the person to say "keep tNN" for each worker to keep), then run pa-worker close '
                         '--leftovers to ask again on a new card.'.format(card_id, words))
    if card.get("answer") != CLOSE_ALL:
        raise pa.Problem("card {} was answered {!r}; nothing was closed".format(card_id, card.get("answer")))
    if str(card.get("note") or "").strip():
        # A note is never parsed: it could name a worker to keep in any words. Close nothing and ask again.
        pa.keep_previous(card, card.get("answer"))
        card.update(status="withdrawn", answer=None, answeredBy="pa-worker close: withdrawn", withdrawnAt=pa.now())
        pa.write_card(directory, path, card)
        append_journal(directory, {"by": "agent" if by_an_agent() else "person", "trigger": "leftovers",
                                   "why": "withdrew card {}: it was answered with a note ({}); nothing was closed".format(card_id, cut(clean(card["note"]), 200))})
        raise pa.Problem('card {} has a note, so nothing was closed: "{}". The note is not read. Ask the person to say "keep tNN" for each '
                         'worker to keep (pa-worker keep), then run pa-worker close --leftovers to ask again on a new card'.format(
                             card_id, cut(clean(card["note"]), 200)))
    return card


def card_refusal(card, address, task, entry, info):
    """Why this worker is not closed on the card's answer: not on it, named in its note, or changed since."""
    listed = next((w for w in card.get("workers") or [] if w.get("address") == address and w.get("task") == task.get("id")
                   and w.get("name") == clean(entry.get("name"))), None)
    if not listed:
        return "{} is not on card {}; nothing was closed".format(address, card["id"])
    created = pa.parse_time(str(card.get("created") or ""))
    changed = (info or {}).get("status_ms")
    if info and not isinstance(changed, (int, float)):
        return "activity since card {} opened is unknown (hc gives no status time); not closed".format(card["id"])
    if (listed.get("state") == "gone" and info) or (created and isinstance(changed, (int, float)) and changed / 1000.0 > created):
        return "{} has been active since card {} opened; not closed".format(address, card["id"])
    return None


def close_by_card(directory, agent, profile, card_id):
    """pa-worker close --card <id>: every worker on the answered leftovers card, each through the normal
    guards. A worker that fails one is skipped and listed; the others are closed."""
    card = leftovers_card(directory, card_id)
    for w in card.get("workers") or []:
        try:
            for line in close_worker(directory, agent, profile, w.get("address") or "", None, card_id):
                print(line)
        except pa.Problem as e:
            print("skipped {} ({}, {}): {}".format(w.get("address"), w.get("name") or "unnamed", w.get("task"), e))


def mark_removed(task, path, when):
    """Records in a ledger task that the worktree at path (a realpath) was removed: its `worktrees` entry gets
    `removed`, or one is added (path as recorded, its job) when it came from `worktree` or `extraWorktrees`;
    `worktree` becomes null only if it was that one. History is kept; nothing else changes."""
    tree = next((w for w in pa.task_worktrees(task) if w["path"] == path), None)
    real = lambda p: os.path.realpath(os.path.expanduser(p))
    entries = task.get("worktrees")
    marked = False
    if isinstance(entries, list):
        for e in entries:
            if isinstance(e, dict) and isinstance(e.get("path"), str) and e["path"].strip() and real(e["path"]) == path:
                e["removed"] = e.get("removed") or when
                marked = True
    if not marked and tree and (entries is None or isinstance(entries, list)):
        task["worktrees"] = (entries or []) + [{"path": tree["given"], "job": tree["job"], "removed": when}]
    if isinstance(task.get("worktree"), str) and task["worktree"].strip() and real(task["worktree"]) == path:
        task["worktree"] = None


def cmd_worker(args):
    given = args.space or space_from_terminal() or ""
    space = pa.local_id(given)
    if not space:
        raise pa.Problem("pass --space <id>")
    if args.action in ("close", "keep") and "/" in given and not os.path.isdir(pa.space_dir(space)):
        raise pa.Problem("space {} lives on {}; run pa-worker there (its Project Agent does)".format(given, given.split("/")[0]))
    directory, agent, profile = load_context(space)
    if args.action == "close":
        if args.leftovers:
            return close_leftovers(directory, agent, profile, args.dry_run)
        if args.card and not args.address:
            return close_by_card(directory, agent, profile, args.card)
        if args.cancel_when_idle:
            for line in cancel_when_idle(directory, args.address):
                print(line)
            return
        if args.idle_waiter:
            return wait_idle(directory, agent, profile, args.address, args.evidence, args.turn_ms)
        lines = (close_when_idle(directory, agent, profile, space, args.address, args.evidence) if args.when_idle
                 else close_worker(directory, agent, profile, args.address, args.evidence, args.card))
        for line in lines:
            print(line)
        return
    if args.action == "keep":
        return keep_worker(directory, agent, args.address, args.quote, args.off)
    facts, row = worker_facts(directory, agent, profile, args.address)
    if args.action == "info":
        print(json.dumps(facts, indent=2))
        return
    if by_an_agent():  # delete only: close is the agent's
        raise pa.Problem("pa-worker delete is the person's command. To remove a worker or a worktree, ask the person.")
    if facts["address"] == agent.get("terminal"):
        raise pa.Problem("that is the Project Agent itself; it ends when its space is closed")
    did, failed = [], None
    if facts["state"] == "live":
        pa.call(["hc", "kill", facts["address"]], check=False)
    if facts["state"] != "gone":
        pa.call(["hc", "rm", facts["address"]], check=False)
        did.append("Ended and removed worker {} ({}).".format(facts["address"], facts.get("name") or "unnamed"))
    tree = facts.get("worktree")
    if tree and not args.keep_worktree:
        if tree["keep"]:
            did.append("Kept the worktree (keepWorktree is set on the task).")
        elif tree["sharedWith"]:
            did.append("Kept the worktree: still used by {}.".format(", ".join(tree["sharedWith"])))
        elif (tree["uncommitted"] or (tree["removesBranches"] and tree["unpushed"])) and not args.discard:
            did.append("Kept the worktree: it has {} uncommitted file(s) and {} unpushed commit(s); delete again with --discard to lose them.".format(
                tree["uncommitted"], tree["unpushed"]))
        else:
            try:
                did.append(remove_worktree(tree["path"], row["facts"]))
                tree["removed"] = True
                tidy = remove_container(row["given"])
                if tidy:
                    did.append(tidy)
            except pa.Problem as e:  # the worker is ended already: the ledger is still written, the failure said
                failed = "Could not remove the worktree {}: {}".format(tree["path"], e)
                did.append(failed)
                tree["removed"] = not os.path.lexists(tree["path"])  # gone anyway: marked, so no entry names it
    if facts.get("task"):
        with open(os.path.join(directory, ".ledger.lock"), "a") as lock:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = os.path.join(directory, "ledger.json")
            ledger = pa.read_json(path)
            for t in (ledger or {}).get("tasks", []):
                if t.get("id") == facts["task"]:
                    t["workers"] = [w for w in t.get("workers") or [] if w.get("address") != facts["address"]]
                    t["updated"] = pa.now()
                if tree and tree.get("removed") and t.get("id") in tree["tasks"]:  # every task that recorded it
                    mark_removed(t, tree["path"], pa.now())
                    t["updated"] = pa.now()
                # a task that counts through a folder inside the worktree or its container: that path, once it is gone
                # (inside: with the worktree; the container: only once remove_container took it), as mark_removed_everywhere
                gone = [p for p in (tree or {}).get("related", {}).get(t.get("id"), []) if not os.path.lexists(p)] if tree and tree.get("removed") else []
                for p in gone:
                    mark_removed(t, p, pa.now())
                if gone:
                    t["updated"] = pa.now()
            if ledger:
                pa.write_json(path, ledger)
            with open(os.path.join(directory, "journal.jsonl"), "a") as journal:
                journal.write(json.dumps({"at": pa.now(), "by": "person", "trigger": "panel", "task": facts["task"],
                                          "why": "the person deleted worker {}: {}".format(facts["address"], " ".join(did))}) + "\n")
    for line in did:
        print(line)
    if args.with_task and facts.get("task"):
        subprocess.call([os.path.join(os.path.dirname(os.path.realpath(__file__)), "pa.py"), "task", "delete", facts["task"], "--space", space])
    elif facts.get("task"):
        term = pa.hc_get(agent.get("terminal"))
        if term.get("state") == "live" and pa.launcher_alive(directory, agent.get("terminal")):
            pa.call(["hc", "send", agent["terminal"], "The person deleted worker {} of task {}. {} Re-read ledger.json; do not start a replacement "
                     "unless they ask.".format(facts["address"], facts["task"], " ".join(did[1:]))], check=False)
        else:
            print("(The Project Agent is not running; it reads the ledger when it starts.)")
    if failed:
        raise pa.Problem("the worktree was not removed ({}); the worker is ended and the ledger updated".format(tree["path"]))


# ---- pa-trust: Claude Code's folder trust prompt (t-035) ---------------------------------------------------
#
# The marker table is built from a real screen (evidence/t-035/trust-screen.txt, kept as
# tests/trust_screens/claude-real.txt) and, as a second set, the older wording the spec remembers. Anything else
# is not recognised and goes to the person (fail closed). Nothing here writes a file or opens Claude Code's or
# Codex's own state.

BOX = "│╭╮╰╯─┃┏┓┗┛━"
CURSOR = "❯"
TRUST_WORDINGS = [
    {"name": "saved 2026-11-18", "markers": ["Accessing workspace:", "Is this a project you created or one you trust?"],
     "yes": "Yes, I trust this folder", "no": "No, exit",
     "text": "Accessing workspace: Quick safety check: Is this a project you created or one you trust? (Like your own "
             "code, a well-known open source project, or work from your team). If not, take a moment to review what's "
             "in this folder first. Claude Code'll be able to read, edit, and execute files here. Security guide",
     "footer": ["Enter to confirm · Esc to cancel"]},
    {"name": "older, remembered", "markers": ["Do you trust the files in this folder?"],
     "yes": "1. Yes, proceed", "no": "2. No, exit",
     "text": "Do you trust the files in this folder? Claude Code may read files in this folder. Reading untrusted files "
             "may lead Claude Code to behave in unexpected ways. With your permission Claude Code may execute files in "
             "this folder. Executing untrusted code is unsafe. Learn more",
     "footer": ["Enter to confirm · Esc to exit", "Enter to confirm · Esc to cancel"]},
]
# Another dialog anywhere on the screen: a tool permission prompt, an MCP server approval, the bypass-permissions
# confirmation, an auto-mode notice, Codex.
OTHER_DIALOG_MARKERS = ["Do you want to", "don't ask again", "MCP server", ".mcp.json", "Bypass Permissions",
                        "bypass permissions", "Auto mode", "auto mode", "Codex", "Yes, I accept", "esc to interrupt"]
GRANT_WORDS = ("allow", "permission", "director", "mcp", "server", "env", "helper", "apikey")
PERMISSION_RULE = re.compile(r"\b(Bash|Read|Edit|Write|MultiEdit|NotebookEdit|WebFetch|WebSearch|Glob|Grep|Task|Agent|Skill)\(|\bmcp__")
PA_HOOK_COMMAND = re.compile(r"^(\$HOME/\.local/bin/|~/\.local/bin/|/[^\s;&|`$<>()]*/)?pa-hook [a-z][a-z-]*( --[a-z][a-z-]*)*$")
PA_HOOK_LISTED = re.compile(r"(^|\s)(\S*/)?pa-hook [a-z][a-z-]*( --[a-z][a-z-]*)*$")
TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
CONFIRM_TRIES, CONFIRM_DELAY = 10, 0.5


def screen_line(line):
    return re.sub(r"\s+", " ", line.strip().strip(BOX).strip())


def trust_marks(text):
    """The wording whose markers are all on the screen, or None (none, or more than one)."""
    flat = re.sub(r"\s+", " ", " ".join(screen_line(l) for l in text.split("\n")))
    found = [w for w in TRUST_WORDINGS if all(m in flat for m in w["markers"])]
    return found[0] if len(found) == 1 else None


def pa_hook_line(text):
    """A listed hook that is a pa-hook command and nothing more."""
    if any(ch in text.replace("$HOME", "") for ch in ";&|`$<>()"):
        return False
    return bool(PA_HOOK_LISTED.search(text))


def trust_read(text, width=None):
    """Read a screen: (result or None, why, facts). facts: wording, shown (the folder as shown), grants (what the
    dialog lists that would be granted), cursor ("yes", "no" or None) and move (the key from No to Yes, or None)."""
    lines = [l.rstrip("\r") for l in (text or "").split("\n")]
    for m in OTHER_DIALOG_MARKERS:
        if m in (text or ""):
            return "not-this-prompt", "the screen shows another dialog ({!r})".format(m), {}
    wording = trust_marks(text or "")
    if not wording:
        return "not-this-prompt", "no folder trust prompt this tool knows is on the screen", {}
    known = re.sub(r"\s+", " ", wording["text"])
    options = {}
    for i, line in enumerate(lines):
        c = screen_line(line)
        mark = c.startswith(CURSOR)
        bare = c[len(CURSOR):].strip() if mark else c
        for key in ("yes", "no"):
            if bare == wording[key]:
                options.setdefault(key, []).append((i, mark))
    if len(options.get("yes", [])) != 1 or len(options.get("no", [])) != 1:
        return "not-this-prompt", "the yes and no choices are not each on the screen once", {}
    (yes_i, on_yes), (no_i, on_no) = options["yes"][0], options["no"][0]
    first = next(i for i, l in enumerate(lines) if wording["markers"][0] in screen_line(l))
    if first > min(yes_i, no_i):
        return "not-this-prompt", "the choices come before the dialog's text", {}
    end = max(yes_i, no_i)
    for line in lines[end + 1:]:
        c = screen_line(line)
        if c and c not in wording["footer"]:
            return "not-this-prompt", "text after the dialog it does not know: {!r}".format(c[:80]), {}
    paths, grants, unknown, section = [], [], [], None
    for i in range(first, end + 1):
        if i in (yes_i, no_i):
            continue
        c = screen_line(lines[i])
        if not c:
            section = None
            continue
        if c in wording["footer"] or (len(c) >= 3 and c in known):
            continue
        if section == "grant":
            grants.append(c)
            continue
        if section == "hooks":
            if not pa_hook_line(c):
                grants.append("a hook that is not the Project Agent's: " + c)
            continue
        low = c.lower()
        if c.endswith(":"):
            if "hook" in low:
                section = "hooks"
            elif any(w in low for w in GRANT_WORDS):
                section = "grant"
                grants.append(c)
            else:
                unknown.append(c)
            continue
        if c.startswith(("/", "~", "…", "...")):
            paths.append(i)
        elif PERMISSION_RULE.search(c) or any(w in low for w in GRANT_WORDS):
            grants.append(c)
        elif not pa_hook_line(c):
            unknown.append(c)
    if len(paths) != 1:
        return "uncertain", "{} folder lines on the dialog, not exactly one".format(len(paths)), {}
    i = paths[0]
    shown = screen_line(lines[i])
    raw = lines[i].rstrip()
    width = width or max(len(l.rstrip()) for l in lines)
    if "…" in shown or "..." in shown:
        return "uncertain", "the folder is shown cut: " + shown, {}
    if i + 1 >= len(lines) or screen_line(lines[i + 1]):
        return "uncertain", "the line under the folder is not blank: the path may go on", {}
    if raw and raw[-1] in BOX:
        if not raw[:-1].endswith("  "):
            return "uncertain", "the folder reaches the dialog's right edge: it may be wrapped", {}
    elif len(raw) >= width:
        return "uncertain", "the folder reaches the screen's right edge: it may be wrapped", {}
    if shown.startswith("~") and not shown.startswith("~/"):
        return "uncertain", "the folder starts with ~ but not ~/: " + shown, {}
    if unknown:
        return "not-this-prompt", "text on the dialog it does not know: {!r}".format(unknown[0][:80]), {}
    cursor = "yes" if on_yes and not on_no else ("no" if on_no and not on_yes else None)
    # The saved screen has Yes on the line right under No: one `down`. Any other layout is not one seen; no key.
    move = "down" if cursor == "no" and yes_i == no_i + 1 else None
    return None, "", {"wording": wording["name"], "shown": shown, "grants": grants, "cursor": cursor, "move": move}


def trust_project(project, home, pa_home=None):
    """(real project folder, None), or (None, why) when it cannot be the base of a trust: also when the home folder
    is inside it (every folder in it, the Claude home among them, would be inside), or when it overlaps the Project
    Agent's home or its spaces folder either way, on real paths (holds it, is it, or lies inside it: its state
    folders would be inside it), or holds the real path of any space folder (<pa home>/spaces/<id>, a symlink)."""
    if not isinstance(project, str) or not project.strip():
        return None, "agent.json names no project folder"
    if not os.path.isabs(project):
        return None, "the project folder is not an absolute path: " + project
    real = os.path.realpath(project)
    if not os.path.isdir(real):
        return None, "the project folder is not a folder: " + project
    if real == os.path.realpath(os.sep) or trust_inside(home, real):
        return None, "the project folder is /, the home folder or a folder holding it: every folder in the home " \
                     "folder would be inside it"
    if pa_home and any(trust_inside(x, real) or trust_inside(real, x) for x in (pa_home, os.path.join(pa_home, "spaces"))):
        return None, "the project folder overlaps the Project Agent's home (holds it or its spaces folder, or lies " \
                     "inside it): its state would be inside it"
    if pa_home:
        spaces = os.path.join(pa_home, "spaces")
        try:
            names = os.listdir(spaces)
        except FileNotFoundError:
            names = []
        except OSError as e:
            return None, TrustUncertain("the Project Agent's spaces folder could not be read ({}): whether a space "
                                        "folder lands in the project folder is not known".format(e))
        if any(trust_inside(os.path.join(spaces, n), real) for n in names):
            return None, "the project folder holds a space folder of the Project Agent's home (through a symlink): " \
                         "its state would be inside it"
        unread = None
        for n in names:
            link, why = trust_link_into(os.path.join(spaces, n), real)
            if link:
                return None, "a symlink in a space folder of the Project Agent's home ({}) lands in the project " \
                             "folder: its state would be inside it".format(link)
            unread = unread or why
        if unread:
            return None, TrustUncertain("a part of a space folder of the Project Agent's home could not be read "
                                        "({}): whether it lands in the project folder is not known".format(unread))
    return real, None


class TrustUncertain(str):
    """A why from trust_project that means uncertain (not known), not refused; no key either way."""


TRUST_WALK_DEPTH = 64  # folders deep, links followed
TRUST_WALK_ENTRIES = 200000


def trust_link_into(folder, base):
    """(the first entry under folder whose real path is base or inside it, None), else (None, what could not be
    read, or None). Links are followed to their final real path, through folders anywhere (a chain: evidence ->
    /X, /X/t-1 -> P/...); each folder is entered once by its real path (the cycle guard), up to TRUST_WALK_DEPTH
    folders deep and TRUST_WALK_ENTRIES entries. A dangling link counts by where it points. An entry that cannot be
    read (an unreadable folder, a link loop) or a bound reached is returned as the second value; the walk goes on,
    since a link into base found later still refuses (t-047 W1 and round 2: Verify L1, Review L-R1, N-R1)."""
    if trust_inside(folder, base):
        return folder, None
    seen, unread, count = set(), None, 0
    stack = [(folder, 0)]
    while stack:
        top, depth = stack.pop()
        real = os.path.realpath(top)
        if real in seen:
            continue
        seen.add(real)
        if depth >= TRUST_WALK_DEPTH:
            unread = unread or "{}: more than {} folders deep".format(top, TRUST_WALK_DEPTH)
            continue
        try:
            names = sorted(os.listdir(real))
        except OSError as e:
            unread = unread or "{}: {}".format(top, e.strerror or e)
            continue
        for name in names:
            count += 1
            if count > TRUST_WALK_ENTRIES:
                return None, unread or "{}: more than {} entries".format(folder, TRUST_WALK_ENTRIES)
            path = os.path.join(top, name)
            if trust_inside(path, base):
                return path, None
            try:
                os.stat(path)
            except FileNotFoundError:
                if not os.path.islink(path):
                    unread = unread or "{}: gone while read".format(path)
                continue  # a dangling link: where it points was checked above
            except OSError as e:
                unread = unread or "{}: {}".format(path, e.strerror or e)
                continue
            if os.path.isdir(path):
                stack.append((path, depth + 1))
    return None, unread


def trust_inside(folder, base):
    """folder is base or inside it, on real paths (symlinks and '..' resolved, separator checked)."""
    f, b = os.path.realpath(folder), os.path.realpath(base)
    return f == b or f.startswith(b.rstrip(os.sep) + os.sep)


def trust_evidence(folder, state_dir, task_ids):
    """folder is <state_dir>/evidence/<id>/ or inside it, for one of task_ids (plain names only), on real paths;
    never the state folder, evidence/ itself or the rest of the state folder."""
    base = os.path.realpath(os.path.join(state_dir, "evidence"))
    for task_id in task_ids:
        if not isinstance(task_id, str) or not TASK_ID.match(task_id) or task_id in (".", ".."):
            continue
        own = os.path.realpath(os.path.join(base, task_id))
        if os.path.dirname(own) != base or not os.path.isdir(own):
            continue  # a task folder that is a symlink elsewhere counts for nothing
        if trust_inside(folder, own):
            return True
    return False


TRUST_GIT_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_CEILING_DIRECTORIES",
                  "GIT_DISCOVERY_ACROSS_FILESYSTEM")


def trust_git_env(environ):
    """pa-trust's git environment: without the variables that move git's repository discovery or its index, without
    any GIT_CONFIG* variable (config injected by GIT_CONFIG_PARAMETERS, GIT_CONFIG_COUNT/KEY_n/VALUE_n or another
    config file: GIT_CONFIG_GLOBAL, GIT_CONFIG_SYSTEM, GIT_CONFIG_NOSYSTEM, GIT_CONFIG), so git reads its default
    config files as a plain git would, and LC_ALL=C so its messages are matched in one language."""
    env = {k: v for k, v in environ.items() if k not in TRUST_GIT_DROP and not k.startswith("GIT_CONFIG")}
    env["LC_ALL"] = "C"
    return env


def trust_git(path, *args):
    """git() for pa-trust: the same (exit code, stdout, stderr), run with trust_git_env."""
    try:
        r = subprocess.run(["git", "-C", path] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=GIT_TIMEOUT, env=trust_git_env(os.environ))
    except subprocess.TimeoutExpired:
        return 124, "", "git {} timed out after {} s".format(args[0] if args else "", GIT_TIMEOUT)
    except OSError as e:
        return 127, "", str(e)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def git_top(folder):
    code, out, _ = trust_git(folder, "rev-parse", "--show-toplevel")
    return os.path.realpath(out) if code == 0 and out else None


def trust_key_why(folder):
    """(the folder Claude Code records the trust for (spec D2), as a real path, None), or (None, why). In a git
    repository its root, and in a worktree its main checkout's root; outside git the folder itself. Outside git
    only when git finds no repository at all (exit 128, 'not a git repository (or any ...'): a broken .git file
    ('not a git repository: <gitdir>'), a timeout, 'dubious ownership' or any other failure, and a repository whose
    main checkout git cannot tell (a bare repository's worktree, a submodule), give a why."""
    real = os.path.realpath(folder)
    code, out, err = trust_git(real, "rev-parse", "--show-toplevel")
    if code == 128 and "not a git repository (or any" in err:
        dot = trust_dot_git(real)
        if dot:
            return None, "git finds no repository, yet {} holds a .git entry that a lookup for .git would take " \
                         "for one".format(dot)
        return real, None
    if code != 0 or not out:
        return None, "git could not say whether the folder is in a repository (exit {}: {})".format(
            code, (err or "no output").splitlines()[0][:200])
    code, common, _ = trust_git(real, "rev-parse", "--git-common-dir")
    if code == 0 and common:
        common = os.path.realpath(os.path.join(real, common))
        if os.path.basename(common) == ".git":
            key = os.path.dirname(common)
            top = git_top(key)
            if top != key:
                return None, "the repository at {} has its working tree at {} (core.worktree), not there".format(
                    key, top or "a place git does not name")
            return key, None
    return None, "git names a repository for the folder but not its main checkout"


def trust_dot_git(real):
    """The first of real and its ancestors that holds a .git entry (file, folder or link), else None. Used only
    when git finds no repository: such an entry is an empty or broken .git (t-047 W2a)."""
    folder = real
    while True:
        if os.path.lexists(os.path.join(folder, ".git")):
            return folder
        parent = os.path.dirname(folder)
        if parent == folder:
            return None
        folder = parent


def trust_key(folder):
    return trust_key_why(folder)[0]


def trust_area(folder, project, state_dir, task_ids):
    """Where folder lies, or None: in the state folder only a ledger task's evidence folder counts (whatever holds
    the state folder), else the project folder or inside it."""
    if trust_inside(folder, state_dir):
        return "a task's evidence folder in the state folder" if trust_evidence(folder, state_dir, task_ids) else None
    return "inside the project folder" if trust_inside(folder, project) else None


def worker_dirs(folder):
    """The worker's folder, its git top level and its main checkout root, as real paths."""
    if not isinstance(folder, str) or not folder or not os.path.isdir(folder):
        return []
    real = os.path.realpath(folder)
    out = [real]
    top = git_top(real)
    if top:
        out.append(top)
        code, common, _ = trust_git(real, "rev-parse", "--git-common-dir")
        if code == 0 and common:
            common = os.path.realpath(os.path.join(real, common))
            if os.path.basename(common) == ".git":
                out.append(os.path.dirname(common))
    return list(dict.fromkeys(out))


def trust_own(folder, dirs):
    return os.path.realpath(folder) in dirs


def local_settings_grants(path):
    """What an untracked .claude/settings.local.json grants beyond the Project Agent's own (pa-hooks)."""
    try:
        data = json.loads(pa.read_text(path))
    except ValueError:
        return ["{} is not valid JSON".format(path)]
    if not isinstance(data, dict):
        return ["{} is not a settings object".format(path)]
    found = ["{} sets {}".format(path, k) for k in sorted(set(data) - {"hooks", "permissions"})]
    perms = data.get("permissions", {})
    if not isinstance(perms, dict) or set(perms) - {"deny"} or not isinstance(perms.get("deny", []), list):
        found.append("{} has permissions other than deny rules".format(path))
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        return found + ["{} has hooks it cannot read".format(path)]
    for event, entries in hooks.items():
        for entry in entries if isinstance(entries, list) else [None]:
            inner = entry.get("hooks") if isinstance(entry, dict) and not set(entry) - {"matcher", "hooks"} else None
            for h in inner if isinstance(inner, list) else [None]:
                ok = (isinstance(h, dict) and not set(h) - {"type", "command", "timeout"} and h.get("type") == "command"
                      and isinstance(h.get("command"), str) and PA_HOOK_COMMAND.match(h["command"]))
                if not ok:
                    found.append("{} has a {} hook that is not a pa-hook command".format(path, event))
    return found


def folder_grants(dirs):
    """What the folders' own files would grant once trusted (spec R5). Repository files only."""
    found = []
    for d in dict.fromkeys(os.path.realpath(x) for x in dirs if x):
        claude = os.path.join(d, ".claude")
        if os.path.lexists(os.path.join(d, ".mcp.json")):
            found.append("{} has a .mcp.json".format(d))
        if os.path.lexists(os.path.join(claude, "settings.json")):
            found.append("{} has a .claude/settings.json".format(d))
        local = os.path.join(claude, "settings.local.json")
        if not os.path.lexists(local):
            continue
        if os.path.islink(claude) or os.path.islink(local):
            found.append("{} reaches its settings.local.json through a symlink".format(d))
            continue
        code, _, _ = trust_git(d, "ls-files", "--error-unmatch", "--", os.path.join(".claude", "settings.local.json"))
        if code == 0:
            found.append("{} tracks its .claude/settings.local.json in git".format(d))
            continue
        found += local_settings_grants(local)
    return found


def cmd_trust(args):
    if not args.address:
        raise pa.Problem("pa-trust needs a terminal address")
    address = args.address
    space = pa.local_id(args.space or space_from_terminal() or "")
    if not space:
        raise pa.Problem("pass --space <id>")
    directory, agent, _profile = load_context(space)
    home = os.path.expanduser("~")
    out = {"worker": address, "folder": None, "real": None, "project": None, "result": None, "why": ""}

    def done(result, why):
        out.update(result=result, why=why)
        print(json.dumps(out))
        sys.exit(0 if result == "accepted" else 1)

    project, why = trust_project(agent.get("project"), home, pa.ROOT)
    if why:
        done("uncertain" if isinstance(why, TrustUncertain) else "refused", why)
    out["project"] = project
    if address == agent.get("terminal") or address in (agent.get("terminals") or []):
        done("refused", "that is the Project Agent's own terminal: its trust prompt is the person's")
    ledger = read_ledger(directory)
    _task, entry = recorded(ledger, address)
    if not entry:
        done("refused", "not a worker this space's ledger records: its prompts are the person's")

    def get():
        info = pa.hc_get(address)
        if not info:
            raise pa.Problem("hc gave no record for " + address)
        return info

    def read():
        return pa.call(["hc", "read", address, "--lines", "0"])

    info = get()
    status = info.get("status") if isinstance(info.get("status"), dict) else {}
    if "codex" in str(status.get("app") or "").lower():
        done("not-this-prompt", "a Codex worker: its trust prompt goes to the person")
    if pa.status_word(info) != "blocked":
        done("not-this-prompt", "its status is {!r}, not blocked".format(pa.status_word(info)))
    size = info.get("size") if isinstance(info.get("size"), dict) else {}
    width = size.get("cols") if isinstance(size.get("cols"), int) and size.get("cols") > 0 else None
    screen = read()
    result, why, facts = trust_read(screen, width)
    if result:
        done(result, why)
    shown = facts["shown"]
    out["folder"] = shown
    path = os.path.join(home, shown[2:]) if shown.startswith("~/") else shown
    if not os.path.isdir(path):
        done("uncertain", "the folder on the screen is not a folder here (it may be cut): " + shown)
    real = os.path.realpath(path)
    out["real"] = real
    ids = [t.get("id") for t in ledger.get("tasks", []) if isinstance(t, dict)]
    area = trust_area(real, project, directory, ids)
    if not area:
        done("outside", "on real paths the folder is neither the project folder or inside it (outside the state "
             "folder), nor a task's evidence folder")
    key, why = trust_key_why(real)
    if not key:
        done("uncertain", why + ", so where the trust would be recorded is not known")
    if key != real and not trust_area(key, project, directory, ids):
        done("outside", "Claude Code would record the trust for {}, the folder's repository (or its main checkout), "
             "which is neither the project folder or inside it nor a task's evidence folder".format(key))
    cwd = (info.get("foreground") or {}).get("cwd") or info.get("cwd")
    dirs = worker_dirs(cwd)
    if not dirs:
        done("uncertain", "hc does not say which folder the worker runs in")
    if not trust_own(real, dirs):
        done("uncertain", "the folder is not the worker's own folder or its repository's")
    if facts["grants"]:
        done("grants", "the dialog lists what the folder would grant: " + "; ".join(facts["grants"])[:300])
    found = folder_grants([real, dirs[0], git_top(real), git_top(dirs[0])])
    if found:
        done("grants", "; ".join(found)[:300])
    if facts["cursor"] is None:
        done("uncertain", "the cursor is not on one choice")
    if facts["cursor"] == "no" and not facts["move"]:
        done("uncertain", "the cursor is on the no choice and the yes choice is not the line right under it")
    if read() != screen:
        done("uncertain", "the screen changed before any key was pressed")
    if pa.status_word(get()) != "blocked":
        done("uncertain", "the worker is no longer blocked")
    if facts["cursor"] == "no":
        pa.call(["hc", "keys", address, facts["move"]])
        moved = read()
        r2, _w2, f2 = trust_read(moved, width)
        if r2 or f2.get("cursor") != "yes" or moved.replace(CURSOR, " ") != screen.replace(CURSOR, " ") \
                or pa.status_word(get()) != "blocked":
            done("uncertain", "pressed {} once; the screen then did not show the cursor on the yes choice with nothing "
                 "else changed, so Enter was not pressed (the dialog is left as it is now)".format(facts["move"]))
    pa.call(["hc", "keys", address, "enter"])
    for n in range(CONFIRM_TRIES):
        if n:
            time.sleep(CONFIRM_DELAY)
        try:
            after, text = pa.hc_get(address), read()
        except pa.Problem:
            continue
        if after and pa.status_word(after) != "blocked" and not trust_marks(text):
            done("accepted", "the folder is {}, the worker's own, with no grants of its own; the prompt moved on".format(area))
    done("unconfirmed", "pressed Enter once; the screen still shows the trust prompt or the worker is still blocked")


def cmd_full_suite(args):
    """The run, its machine slot, reuse of a passing tree, --last and --report: pa_suite.py (t-056)."""
    spec = tool_spec("pa_suite")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.pa = pa
    import types
    return module.full_suite(types.SimpleNamespace(**globals()), args)  # this module's helpers (also when a test loaded it)


def load_tool(name):
    """A tool module of this folder (pa_suite, pa_run), sharing pa."""
    spec = tool_spec(name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.pa = pa
    return module


def types_namespace(names):
    import types
    return types.SimpleNamespace(**names)


def cmd_run(args):
    """pa-run (t-107): any test command in the tree to run, on a configured machine or this Mac: pa_run.py."""
    module = load_tool("pa_run")
    return sys.exit(module.main(types_namespace(globals()), args))


def suite_record(directory, record, command, code, seconds, pins=None, extra=None):
    """One line per repository of the worktree in content/suites.jsonl: what the full suite ran on (t-052 AC7.1).
    pins: the repos as taken before the run (each with its tree; t-056), else read now. extra: more keys per line."""
    lines = []
    for pin in pins or gate_commits(record["root"]) or [{"repo": os.path.realpath(record["root"]), "commit": None, "uncommitted": None}]:
        tree = pin.get("tree")
        if pin.get("commit") and "tree" not in pin:
            c, out, _ = git(pin["repo"], "rev-parse", "--verify", "-q", pin["commit"] + "^{tree}")
            tree = out if not c and out else None
        line = {"at": pa.now(), "task": record.get("task"), "worker": os.environ.get("HC_TERMINAL_ID") or None,
                "repo": pin["repo"], "commit": pin.get("commit"), "tree": tree, "uncommitted": pin.get("uncommitted"),
                "exit": code, "seconds": seconds, "command": command}
        if "untracked" in pin:
            line["untracked"] = pin["untracked"]
        lines.append(dict(line, **(extra or {})))
    os.makedirs(content_dir(directory), exist_ok=True)
    with open(os.path.join(content_dir(directory), "suites.jsonl"), "a") as f:
        f.write("".join(json.dumps(l) + "\n" for l in lines))


def main():
    kind = os.path.basename(sys.argv[0])
    parser = argparse.ArgumentParser(prog=kind)
    if kind == "pa-hook":
        name, rest = (sys.argv[1] if len(sys.argv) > 1 else ""), sys.argv[2:]
        if name not in HOOKS:
            raise pa.Problem("unknown hook {!r}; one of {}".format(name, ", ".join(sorted(HOOKS))))
        try:
            sys.exit(HOOKS[name](hook_input(), rest) or 0)
        except pa.Problem as e:  # a broken hook must not wedge the agent: report, don't block
            print("pa-hook {}: {}".format(name, e), file=sys.stderr)
            sys.exit(1)
    if kind == "pa-gate":
        parser.add_argument("task")
        parser.add_argument("--phase", help="the phase the task wants to leave (default: review)")
        parser.add_argument("--job", help="a recorded job of the task (t-192): its worktree, VERIFY-<job>.md, REVIEW-<job>.md, "
                                          "GATE-<job>.json; default or main: the task's (VERIFY.md, REVIEW.md, GATE.json)")
        parser.add_argument("--space")
        return cmd_gate(parser.parse_args())
    if kind == "pa-hooks":
        parser.add_argument("action", choices=("install", "remove"))
        parser.add_argument("worktree")
        parser.add_argument("--task")
        parser.add_argument("--space")
        parser.add_argument("--fix", action="store_true", help="a bug-fix task: lock test files")
        return cmd_hooks(parser.parse_args())
    if kind == "pa-worktree":
        parser.add_argument("action", choices=("check", "remove"))
        parser.add_argument("task", nargs="?")
        parser.add_argument("--open-cards", action="store_true", help="open a decision card for each candidate")
        parser.add_argument("--job", help="remove: the job whose worktree to remove (default: the task's main worktree)")
        parser.add_argument("--card", help="remove: every worktree on this answered batch card d-wt-batch-<n> (t-168)")
        parser.add_argument("--standing", metavar="RULE", help="remove: the clean worktrees, on the person's bound rule (the "
                                                               "Project Agent's; t-168)")
        parser.add_argument("--space")
        args = parser.parse_args()
        if args.action == "remove" and sum(map(bool, (args.task, args.card, args.standing))) != 1:
            raise pa.Problem("pa-worktree remove needs a task id, --card <id> or --standing <rule>")
        if (args.card or args.standing) and (args.action != "remove" or args.job):
            raise pa.Problem("--card and --standing are for pa-worktree remove, with nothing else")
        code = cmd_worktree(args)
        if code:
            sys.exit(code)
        return code
    if kind == "pa-worktree-allow":
        parser.description = "The person's binding of their rule to delete clean worktrees ({}).".format(worktree_allow_path())
        sub = parser.add_subparsers(dest="action")
        sub.required = True
        show = sub.add_parser("show", help="print the bindings (anyone)")
        show.add_argument("--json", action="store_true")
        show.add_argument("--space")
        bind = sub.add_parser("set", help="bind the rule, as its text stands now (the person's command)")
        bind.add_argument("--space", required=True)
        bind.add_argument("--standing", required=True, help="the rule's id in the space's rules.json")
        bind.add_argument("--note")
        unbind = sub.add_parser("unbind", help="take the space's binding away (the person's command)")
        unbind.add_argument("--space", required=True)
        for name in ("on", "off"):
            p = sub.add_parser(name, help="turn the file, or one space, {} (the person's command)".format(name))
            p.add_argument("--space")
        code = cmd_worktree_allow(parser.parse_args())
        if code:
            sys.exit(code)
        return 0
    if kind == "pa-worker":
        parser.add_argument("action", choices=("info", "delete", "close", "keep"))
        parser.add_argument("address", nargs="?")
        parser.add_argument("--keep-worktree", action="store_true", help="delete: leave its worktree in place")
        parser.add_argument("--discard", action="store_true", help="delete: delete the worktree even with uncommitted files or unpushed commits")
        parser.add_argument("--with-task", action="store_true", help="delete: also delete its task, with the task's evidence and cards (to the trash)")
        parser.add_argument("--evidence", help="close: the report you read, a file in evidence/<task>/")
        parser.add_argument("--leftovers", action="store_true", help="close: list the workers from before one-worker-per-job on one decision card (closes nothing)")
        parser.add_argument("--card", help="close: an answered leftovers card, in place of --evidence (without an address: every worker on it)")
        parser.add_argument("--dry-run", action="store_true", help="close --leftovers: only list what it would do")
        parser.add_argument("--when-idle", action="store_true", help="close: if it is still working, close it once idle (in the background, up to 10 min)")
        parser.add_argument("--idle-waiter", action="store_true", help=argparse.SUPPRESS)  # the --when-idle waiter itself
        parser.add_argument("--turn-ms", type=int, help=argparse.SUPPRESS)  # the waiter: hc's status_ms of the turn it waits for
        parser.add_argument("--cancel-when-idle", action="store_true", help="close: the waiter a --when-idle left closes nothing")
        parser.add_argument("--quote", help="keep: the person's own words")
        parser.add_argument("--off", action="store_true", help="keep: the person says it may be closed again")
        parser.add_argument("--space")
        args = parser.parse_args()
        if args.action == "close" and args.evidence and args.card:
            raise pa.Problem("pa-worker close takes --evidence or --card, not both")
        if args.action == "close" and (sum(map(bool, (args.address, args.leftovers))) != 1 if not args.card else bool(args.leftovers)):
            raise pa.Problem("pa-worker close needs an address and --evidence, or --leftovers, or --card <id>")
        if args.cancel_when_idle and (args.action != "close" or not args.address or args.when_idle or args.idle_waiter
                                      or args.evidence or args.card or args.leftovers):
            raise pa.Problem("pa-worker close <address> --cancel-when-idle takes nothing else")
        if (args.when_idle or args.idle_waiter) and (args.action != "close" or not args.address or not args.evidence or args.card):
            raise pa.Problem("pa-worker close --when-idle needs an address and --evidence")
        if args.action != "close" and not args.address:
            raise pa.Problem("pa-worker {} needs an address".format(args.action))
        return cmd_worker(args)
    if kind == "pa-trust":
        parser.add_argument("address")
        parser.add_argument("--space")
        return cmd_trust(parser.parse_args())
    if kind == "pa-full-suite":
        parser.add_argument("--again", action="store_true", help="run even when this clean tree already passed")
        parser.add_argument("--last", action="store_true", help="print the last recorded run (default: this worktree's tree)")
        parser.add_argument("--commit", help="--last, --status, --wait: the run for this commit (a prefix is enough)")
        parser.add_argument("--tree", help="--last: the run for this tree")
        parser.add_argument("--report", action="store_true", help="the suite's parts by time over the recorded runs")
        parser.add_argument("--status", action="store_true", help="this worktree's latest run: waiting, running or done (t-076)")
        parser.add_argument("--wait", action="store_true", help="wait for that run; a heartbeat every minute; its exit")
        parser.add_argument("--timeout", type=float, help="--wait: give up after this many seconds (default 540; exit 75)")
        parser.add_argument("--module", help="run one unit module (tests/run-all.sh --module test_x) the same way")
        parser.add_argument("--local", action="store_true", help="run on this Mac, never on a configured machine (t-089)")
        parser.add_argument("--on", metavar="M", help="run on machine M of config.json's suiteMachines only; exit 2 when "
                                                      "it is not reachable or its setup fails (no fallback)")
        parser.add_argument("--places", action="store_true", help="each configured machine (reachable or why not, "
                                                                  "cap, holders) and this Mac's slot")
        parser.add_argument("--json", action="store_true", help="--places: the same as one JSON object (t-119)")
        parser.add_argument("--space")
        return cmd_full_suite(parser.parse_args())
    if kind == "pa-run":
        parser.description = "Run a test command in the tree to run (HEAD plus the worktree's changes) on a machine of " \
                             "config.json's suiteMachines, or this Mac (t-107; see pa_run.py)."
        parser.add_argument("--on", metavar="M", help="on machine M only; 125 when it cannot run there (no fallback)")
        parser.add_argument("--local", action="store_true", help="on this Mac, never on a configured machine")
        parser.add_argument("--commit", metavar="C", help="the tree to run is commit C (an ancestor of HEAD), with only "
                                                          "the --overlay paths from the worktree")
        parser.add_argument("--overlay", metavar="PATH", action="append", help="with --commit: a path taken from the "
                                                                              "worktree (missing there: deleted); repeat")
        parser.add_argument("--cwd", metavar="SUBDIR", help="run in this folder of the tree (default: the current one)")
        parser.add_argument("--for", dest="kind", default=None, choices=("single", "failing-first", "mutation",
                            "module", "affected", "measure", "other"), help="the kind of run, for the record (default: "
                                                                          "inferred from the command)")
        parser.add_argument("--report", action="store_true", help="which kinds of test run are faster on this Mac or "
                                                                  "on a machine, from HERDR_PA_HOME/test-runs.jsonl")
        parser.add_argument("--days", type=float, help="--report: only runs of the last N days")
        parser.add_argument("argv", nargs=argparse.REMAINDER, help="-- then the command's words")
        args = parser.parse_args()
        if args.report:
            return sys.exit(load_tool("pa_run").report_main(types_namespace(globals()), args))
        if not [w for w in args.argv if w != "--"][:1]:
            parser.error("give the command after --")
        return cmd_run(args)
    raise pa.Problem("invoke as pa-gate, pa-hooks, pa-hook, pa-worktree, pa-worktree-allow, pa-worker, pa-trust, pa-full-suite or pa-run")


if __name__ == "__main__":
    try:
        main()
    except pa.Problem as e:
        print("{}: {}".format(os.path.basename(sys.argv[0]), e), file=sys.stderr)
        sys.exit(2)
