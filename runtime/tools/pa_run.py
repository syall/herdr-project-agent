"""pa-run (t-107): any test command in the tree to run, on a configured machine or this Mac. Python standard library
only (3.7+). Loaded by pa_hooks.py from its own folder (h: the pa_hooks module, for its helpers); it loads
pa_suite.py from the same folder for t-089's remote parts (probe, slots, ship, terminal, watcher, cleanup, sweep).

  pa-run [--on M | --local] [--commit C] [--overlay PATH ...] [--cwd SUBDIR] [--for KIND] -- <argv...>

Run inside a task's worktree. argv runs in the tree to run and its output streams to stdout (pa-run's own lines go
to stderr); pa-run exits with argv's code.
- The tree to run (R4-R6): HEAD plus every change in the worktree (what `git add -A` would stage: tracked changes,
  deletions, untracked files that are not ignored); with --commit C (an ancestor of HEAD, or HEAD), C with only the
  --overlay paths taken from the worktree (a path missing there is deleted). It is built with a temporary index and
  object folder (GIT_INDEX_FILE, GIT_OBJECT_DIRECTORY, the repo's objects as alternates): nothing is written under
  .git. The patch `git diff --binary C T` travels with the bundle of HEAD; there the clone checks out C, applies it
  with `git apply --index`, and its commit and tree must be C and T.
- argv (R2): an argv list, each word quoted into the fixed remote script. argv[0] is python3, node, sh or a relative
  path to an executable file in the tree. Refused (125): hc or any other name, `sh -c`, a command that starts an hc
  core (tests/pa_session_check.sh; tests/run-all.sh unless its first word is --affected, --except-integration
  or --module: use pa-full-suite). sh's script and an argv[0] path must be relative and stay inside the tree; they
  are compared by realpath (a symlink in the tree to either script is that script).
- Never sent (R8): a changed path matching the guard's secret patterns or the profile's secretPaths, or under
  .claude/; ignored files; bundle and patch over 6 MB base64 (125).
- Where (R9 as J1-D1, R10, R11): every run on a machine takes one of its `cap` slots (the same slots as full
  suites). The first configured machine with a free slot that is reachable; all busy: it waits, saying for whom, up
  to PA_SUITE_WAIT_LIMIT (600) s, then 124. None reachable or its setup failed: this Mac when config.json's
  suiteFallback is "mac" (the default), else 125. --on M: only M (125 when it cannot). --local, or no suiteMachines:
  this Mac (with --commit, in a scratch clone).
- There (R16, R17): t-089's run folder, own HOME/TMPDIR/XDG/HERDR_PA_HOME, refusing hc shim, PATH {folder}/bin first,
  plus PA_SUITE_REMOTE=1; output copied back every second for the first minute, then every PA_SUITE_REMOTE_POLL s.
  Only the output and code come back. TERM, INT, HUP: the terminal ended, the folder removed, 143, 130, 129.
- Records (R18-R20): evidence/<task>/run-<id>.out (the whole output), one build.log line, one line per run in
  HERDR_PA_HOME/test-runs.jsonl (local runs too; lines older than 30 days dropped), a progress file of kind pa-run.
- Kinds (T1, J2): --for, else inferred: run-all.sh --affected / --module, --commit with --overlay (failing-first),
  python3 -m unittest (single), else other. A local run's line has the command's own time (ran) and its setup apart.
- The slot step (J2, R12-R14): pa_suite.py slot runs run-all.sh's --affected and --module on a machine through
  slot_run (this code, no pa.py), honouring PA_SUITE_DEADLINE; a local pa-run sets PA_SUITE_LOCAL_RUN so a
  run-all.sh under it stays here and is not recorded twice.
  pa-run --report [--days N]   per kind: runs and medians here and on each machine, and which is faster (T2)
Exit: argv's code; 125 not run; 124 no slot came free or the machine was lost.
"""
import base64
import fcntl
import json
import os
import posixpath
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

pa = None  # set by pa_hooks after loading
S = None   # pa_suite, set by load()

NOT_RUN, NO_SLOT = 125, 124
KINDS = ("single", "failing-first", "mutation", "module", "affected", "measure", "other")
PROGRAMS = ("python3", "node", "sh")
WAIT_LIMIT = 600  # PA_SUITE_WAIT_LIMIT
KEEP = 30 * 86400  # test-runs.jsonl lines older than this are dropped on write
ARGV_KEEP = 200
SESSION_CHECK = "tests/pa_session_check.sh"
RUN_ALL = "tests/run-all.sh"
# run-all.sh's own case: the flags that are not a whole run ("" is its no-flag run, --only-integration its
# integration part, anything else its usage); run-all.sh runs only with one of these first (J1-D13)
RUN_ALL_FLAGS = ("--affected", "--except-integration", "--module")
GIT_SAFE = ["-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", "-c", "core.splitIndex=false"]


class NotRun(Exception):
    pass


def load(h):
    """pa_suite from this folder (once), sharing pa."""
    global S
    if S is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("pa_suite", os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                                                                "pa_suite.py"))
        S = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(S)
    S.pa = pa
    return S


PREFIX = "pa-run"  # the slot step (slot_run) says its lines as run-all.sh, on stdout (R12)
TO_STDOUT = False


def say(line):
    print("{}: {}".format(PREFIX, line), file=sys.stdout if TO_STDOUT else sys.stderr, flush=True)


def infer_kind(argv, commit, overlay):
    """T1: the kind of a run given no --for: run-all.sh --affected / --module, a failing-first shape (--commit with
    --overlay), a unittest run (single), else other."""
    words_ = list(argv)
    if words_[:1] == ["sh"]:
        i, _ = sh_script(words_)
        words_ = words_[i:] if i is not None else words_[1:]
    if words_ and os.path.normpath(words_[0]) == RUN_ALL:
        if words_[1:2] == ["--affected"]:
            return "affected"
        if words_[1:2] == ["--module"]:
            return "module"
    if commit and overlay:
        return "failing-first"
    if argv[:3] == ["python3", "-m", "unittest"]:
        return "single"
    return "other"


# ---- argv (R2) ----

def clean_rel(value):
    """A relative path inside the tree, normalised ('' for the top), else None."""
    if not value or os.path.isabs(value):
        return None if value else ""
    n = os.path.normpath(value)
    if n == ".":
        return ""
    if n == ".." or n.startswith("../") or "/../" in "/" + value + "/":
        return None
    return n


def check_cwd(value):
    n = clean_rel(value or "")
    return (n, None) if n is not None else (None, "--cwd {!r} is not a folder inside the tree (relative, no ..)".format(value))


def executable_in(top, rel, commit, overlay, h):
    """Is rel (from the top) an executable file in the tree to run?"""
    if commit and rel not in overlay:
        code, out, _ = h.git(top, "ls-tree", commit, "--", rel)
        return not code and out.split(" ")[0] == "100755"
    path = os.path.join(top, rel)
    return os.path.isfile(path) and os.access(path, os.X_OK)


SH_FLAGS = set("abCefhmnuvx")  # POSIX sh's set options; with o (takes the next word as its name) all sh may get


def sh_script(argv):
    """argv[0] is sh: (index of the script word, None) or (None, why not). Parses sh's options the way sh does:
    `-`/`+` clusters of SH_FLAGS letters, `o` in a cluster takes the next word, `--` or `-` ends them. -c (a shell
    line), -s or no script (a shell line from stdin), a script word starting with - (cannot be told from an
    option), and anything else (-i, a long option, an unknown letter, -o with no name) are refused: fail closed."""
    i = 1
    while i < len(argv):
        word = argv[i]
        if word in ("--", "-"):
            i += 1
            break
        if word[:1] not in ("-", "+"):
            break
        if word == "+" or word.startswith("--"):
            return None, "sh option {!r}: cannot parse it, so it is refused".format(word)
        names = 0
        for letter in word[1:]:
            if letter == "c":
                return None, "sh -c runs a shell line; give the command's own words (sh <script> ...)"
            if letter == "s":
                return None, "sh -s reads a shell line from stdin; give a script (sh <script> ...)"
            if letter == "o":
                names += 1
            elif letter not in SH_FLAGS:
                return None, "sh option {!r}: cannot parse it, so it is refused".format(word)
        if i + names >= len(argv) and names:
            return None, "sh option {!r} needs a name after it: cannot parse it, so it is refused".format(word)
        i += 1 + names
    if i >= len(argv):
        return None, "sh with no script reads a shell line from stdin; give a script (sh <script> ...)"
    if argv[i].startswith("-"):
        return None, "sh script {!r} starts with -: it cannot be told from an option".format(argv[i])
    return i, None


def tree_path(subdir, word):
    """word as a path from the tree's top, normalised the way the file system reads it ('..' and '//' resolved)."""
    return posixpath.normpath(posixpath.join(subdir or "", word)) if word else ""


def script_rel(subdir, word):
    """sh's script, or an argv[0] path: from the tree's top, normalised; None when it is absolute, empty or leaves
    the tree (a path that leaves and comes back, ../w/tests/run-all.sh, is refused too: J1-D13)."""
    if not word or word.startswith("/"):
        return None
    rel = tree_path(subdir, word)
    if rel == ".." or rel.startswith("../") or rel.startswith("/"):
        return None
    return rel


def is_file(top, rel, target):
    """Is rel (from the top) the tree's file target: the same realpath under the top, or the same file (a symlink,
    a hard link or another case spelling of it)?"""
    a = os.path.realpath(os.path.join(top, rel))
    b = os.path.realpath(os.path.join(top, target))
    if a == b:
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def check_argv(argv, top, subdir, commit=None, overlay=(), h=None):
    """R2: None when argv may run, else why not."""
    if not argv:
        return "no command after --"
    first = argv[0]
    script = None
    if first == "sh":
        script, why = sh_script(argv)
        if why:
            return why
    elif first not in PROGRAMS:
        script = 0
    if script is not None:  # what runs as a script: relative, inside the tree, and not one that starts an hc core
        word = argv[script]
        rel = script_rel(subdir, word)
        if rel is None:
            return "{} {!r} is an absolute path or leaves the tree (..): give a relative path inside the tree".format(
                "sh's script" if script else "argv[0]", word)
        if is_file(top, rel, SESSION_CHECK):
            return "{} starts an hc core: use pa-full-suite: the integration part runs on this Mac".format(SESSION_CHECK)
        if is_file(top, rel, RUN_ALL):
            flags = argv[script + 1:]
            if not flags or flags[0] not in RUN_ALL_FLAGS:  # allowlist: everything else is a whole run (fail closed)
                return "{} {} is a whole run or starts an hc core (allowed: {}): use pa-full-suite".format(
                    RUN_ALL, " ".join(repr(f) if not f else f for f in flags) or "(no flag)", ", ".join(RUN_ALL_FLAGS))
    for word in argv[1:]:  # the session check named anywhere as a path in the tree
        if not word.startswith("-") and tree_path(subdir, word) == SESSION_CHECK:
            return "{} starts an hc core: use pa-full-suite: the integration part runs on this Mac".format(SESSION_CHECK)
    if first == "hc":
        return "argv[0] is hc: hc never runs from pa-run (a test reaches no hc core)"
    if first not in PROGRAMS:
        if os.path.isabs(first):
            return "argv[0] {!r} is an absolute path: give python3, node, sh or a relative path in the tree".format(first)
        if "/.." in "/" + first + "/" or first.startswith(".."):
            return "argv[0] {!r} has ..: give a path inside the tree".format(first)
        rel = clean_rel(os.path.join(subdir or "", first))
        if rel is None or not executable_in(top, rel, commit, set(overlay or ()), h or _h):
            return "argv[0] {!r} is not python3, node, sh or an executable file in the tree".format(first)
    return None


_h = None


# ---- the tree to run (R4-R8) ----

def git_bytes(top, env, *args, stdin=None):
    try:
        r = subprocess.run(["git"] + GIT_SAFE + ["-C", top] + list(args), env=env, input=stdin,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
    except subprocess.TimeoutExpired:
        return 124, b"", b"git timed out"
    except OSError as e:
        return 127, b"", str(e).encode()
    return r.returncode, r.stdout, r.stderr


def text(b):
    return b.decode("utf-8", "replace").strip()


def secret_patterns(h, profile):
    return list(h.DENY_PATHS) + [(p, "a secret ({} profile)".format((profile or {}).get("name")))
                                 for p in (profile or {}).get("secretPaths", []) if isinstance(p, str)]


def tree_to_run(h, top, commit=None, overlay=None, secrets=None):
    """R4-R6, R8: {commit, head, tree, patch (bytes), paths}. secrets: [(pattern, what)] checked against the changed
    paths (with .claude/), or None to skip (a local run: nothing is sent). Raises NotRun. Writes nothing in .git:
    git add and write-tree see only the temp object folder (with the repo's objects as alternates, git would
    "freshen" an existing object by setting its mtime), so every object they write lands there; read-tree and diff,
    which only read, see the repo's objects too (J1-D3)."""
    load(None)
    base_env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        base_env.pop(k, None)
    code, out, err = git_bytes(top, base_env, "rev-parse", "--verify", "-q", "HEAD^{commit}")
    head = text(out)
    if code or not S.SHA.match(head):
        raise NotRun("the worktree has no commit (git rev-parse HEAD: {})".format(text(err) or code))
    code, out, err = git_bytes(top, base_env, "rev-parse", "--git-common-dir")
    common = text(out)
    if code or not common:
        raise NotRun("git rev-parse --git-common-dir: " + (text(err) or str(code)))
    common = os.path.realpath(os.path.join(top, common))
    code, out, _ = git_bytes(top, base_env, "rev-parse", "--git-path", "index")
    real_index = os.path.join(top, text(out)) if not code and text(out) else None
    base = head
    if commit:
        code, out, err = git_bytes(top, base_env, "rev-parse", "--verify", "-q", commit + "^{commit}")
        base = text(out)
        if code or not S.SHA.match(base):
            raise NotRun("--commit {}: not a commit here".format(commit))
        code, _, _ = git_bytes(top, base_env, "merge-base", "--is-ancestor", base, head)
        if code:
            raise NotRun("--commit {} is not an ancestor of HEAD {} (v1 runs only HEAD's history)".format(commit, head[:12]))
    scratch = tempfile.mkdtemp(prefix="pa-run-tree-")
    try:
        os.makedirs(os.path.join(scratch, "objects", "pack"))
        os.makedirs(os.path.join(scratch, "objects", "info"))
        write_env = dict(base_env, GIT_INDEX_FILE=os.path.join(scratch, "index"),
                         GIT_OBJECT_DIRECTORY=os.path.join(scratch, "objects"))
        env = dict(write_env, GIT_ALTERNATE_OBJECT_DIRECTORIES=os.path.join(common, "objects"))
        if commit:
            steps = [("read-tree", base), ("add", "-A", "--") + tuple(overlay or ())] if overlay else [("read-tree", base)]
        else:
            copied = False
            if real_index and os.path.isfile(real_index):  # read only: its stat data spares rehashing every file
                try:
                    shutil.copyfile(real_index, env["GIT_INDEX_FILE"])
                    copied = True
                except OSError:
                    pass
            steps = ([] if copied else [("read-tree", head)]) + [("add", "-A")]
        for step in steps:
            code, _, err = git_bytes(top, write_env if step[0] == "add" else env, *step)
            if code and step[0] == "add" and not commit:  # a copied index git cannot use: from HEAD instead
                git_bytes(top, env, "read-tree", head)
                code, _, err = git_bytes(top, write_env, *step)
            if code:
                raise NotRun("git {}: {}".format(step[0], S.first_line(text(err), "exit {}".format(code))))
        code, out, err = git_bytes(top, write_env, "write-tree", "--missing-ok")
        tree = text(out)
        if code or not S.SHA.match(tree):
            raise NotRun("git write-tree: " + (text(err) or str(code)))
        diff = ["diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", "--src-prefix=a/",
                "--dst-prefix=b/"]
        code, names, err = git_bytes(top, env, *(diff + ["--name-only", "-z", base, tree]))
        if code:
            raise NotRun("git diff --name-only: " + text(err))
        paths = [p.decode("utf-8", "surrogateescape") for p in names.split(b"\0") if p]
        patch = b""
        if paths:
            code, patch, err = git_bytes(top, env, *(diff + ["--binary", "--full-index", base, tree]))
            if code:
                raise NotRun("git diff --binary: " + text(err))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if secrets is not None:
        for p in paths:
            if p.startswith(".claude/") or "/.claude/" in "/" + p:
                raise NotRun("{} is under .claude/ (the worker's own settings): never sent".format(p))
            for pattern, what in secrets:
                if re.search(pattern, p):
                    raise NotRun("{} is {}: never sent".format(p, what))
    return {"commit": base, "head": head, "tree": tree, "patch": patch, "paths": paths}


# ---- records (R18, R19) ----

def runs_file():
    return os.path.join(S.home(), "test-runs.jsonl")


def record(row):
    """One line in HERDR_PA_HOME/test-runs.jsonl (pa_suite.test_run_record; lines older than 30 days dropped)."""
    S.test_run_record(row, say=say)


def words(argv):
    return " ".join(shlex.quote(w) for w in argv)


# ---- running ----

class Run(object):
    """One run. The slot step's (slot_run) sets slot: its output file is given, no build.log line, its own worker,
    the deadline (R14), and on_mac (its Mac path: the step goes on with today's local run)."""

    def __init__(self, h, args, record_, directory, profile, output=None):
        self.h, self.args, self.task = h, args, record_
        self.directory, self.profile = directory, profile
        self.run = uuid.uuid4().hex[:12]
        if output is None:
            self.evidence = os.path.join(directory, "evidence", record_["task"])
            os.makedirs(self.evidence, exist_ok=True)
            output = os.path.join(self.evidence, "run-{}.out".format(self.run))
            self.build_log = os.path.join(self.evidence, "build.log")
        else:
            self.build_log = None
        self.output = output
        self.kind = args.kind or "other"
        self.began = time.time()
        self.waited = 0.0
        self.overhead = {"probe": None, "ship": None, "start": None, "copyback": None, "cleanup": None}
        self.notes = []
        self.progress = None
        self.printed = 0
        self.slot, self.worker, self.deadline, self.on_mac = False, os.environ.get("HC_TERMINAL_ID") or None, None, None
        self.temp_output = False  # the slot step's output outside a task's evidence folder: removed, not recorded

    def finish(self, code, place, machine=None, os_name=None, ran=None):
        seconds = round(time.time() - self.began, 1)
        at = pa.now()
        argv = words(self.argv)
        if self.build_log:
            with open(self.build_log, "a") as f:
                f.write("=== {} pa-run {}: {} on {} -> exit {} after {:.0f}s (waited {:.0f}s){}\n".format(
                    at, self.kind, argv[:ARGV_KEEP], machine or "this Mac", code, seconds, self.waited,
                    "; " + "; ".join(self.notes) if self.notes else ""))
        record({"at": at, "run": self.run, "task": self.task.get("task"), "worker": self.worker,
                "kind": self.kind, "argv": argv[:ARGV_KEEP], "commit": (getattr(self, "tree", None) or {}).get("commit"),
                "tree": (getattr(self, "tree", None) or {}).get("tree"), "place": place, "machine": machine, "os": os_name,
                "waited": round(self.waited, 1), "seconds": seconds, "ran": ran, "overhead": self.overhead,
                "exit": code, "note": "; ".join(self.notes) or None, "output": None if self.temp_output else self.output})
        S.update_progress(self.progress, state="done", exit=code, end=at, note="; ".join(self.notes) or None)
        return code

    def sink(self, chunk):
        sys.stdout.write(chunk)
        sys.stdout.flush()
        self.printed += chunk.count("\n")


def main(h, args):
    global _h
    _h = h
    load(h)
    record_ = h.find_task(os.getcwd())
    if not record_:
        raise pa.Problem("run pa-run inside a task's worktree (pa-hooks install marks it)")
    argv = list(args.argv or [])
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if args.on and args.local:
        say("not run: --on and --local name two places; give one")
        return NOT_RUN
    code, top, err = h.git(os.getcwd(), "rev-parse", "--show-toplevel")
    if code or not top:
        say("not run: not in a git tree ({})".format(err or code))
        return NOT_RUN
    top = os.path.realpath(top)
    here = os.path.relpath(os.path.realpath(os.getcwd()), top)
    if args.cwd is not None:
        subdir, why = check_cwd(args.cwd)
    else:
        subdir, why = check_cwd("" if here == "." else here)
    if why:
        say("not run: " + why)
        return NOT_RUN
    overlay = []
    for p in args.overlay or []:
        rel = clean_rel(os.path.relpath(os.path.abspath(p), top)) if not os.path.isabs(p) or p.startswith(top) else None
        if rel is None or rel == "":
            say("not run: --overlay {!r} is not a path inside the tree".format(p))
            return NOT_RUN
        overlay.append(rel)
    if overlay and not args.commit:
        say("not run: --overlay goes with --commit (without it every change is in the tree already)")
        return NOT_RUN
    why = check_argv(argv, top, subdir, args.commit, overlay, h)
    if why:
        say("not run: " + why)
        return NOT_RUN
    directory, _agent, profile = h.load_context(record_["space"])
    if not args.kind:
        args.kind = infer_kind(argv, args.commit, overlay)
    r = Run(h, args, record_, directory, profile)
    r.argv, r.top, r.subdir, r.overlay = argv, top, subdir, overlay
    saved = {}

    def on_signal(sig, frame):
        raise S.Interrupted(S.SIGNAL_EXIT.get(sig, 1))
    for sig in S.SIGNAL_EXIT:
        saved[sig] = signal.signal(sig, on_signal)
    try:
        return place_and_run(r)
    finally:
        for sig, old in saved.items():
            signal.signal(sig, old)


def place_and_run(r):
    args = r.args
    cfg = pa.config()
    good, bad = ([], []) if args.local else S.machines(cfg)
    where = os.path.join(S.home(), "config.json")
    for b in bad:
        say("suiteMachines in {}: {}".format(where, b))
    if args.on and not [m for m in good if m["machine"] == args.on]:
        say("not run on {}: not a machine of suiteMachines in {}".format(args.on, where))
        return NOT_RUN
    fb, fb_note = S.fallback(cfg) if good else ("mac", None)
    if fb_note:
        say("{} in {}".format(fb_note, where))
    if not good:
        return run_local(r, "--local" if args.local else "no suiteMachines in " + where)
    candidates = [m for m in good if not args.on or m["machine"] == args.on]
    name = args.on or candidates[0]["machine"]
    try:
        tree = tree_to_run(r.h, r.top, args.commit, r.overlay, secret_patterns(r.h, r.profile))
        size = len(base64.b64encode(tree["patch"])) if tree["patch"] else 0
        if size > S.BUNDLE_MAX:
            raise NotRun("too large: the patch is {} bytes base64, at most {} with the bundle".format(size, S.BUNDLE_MAX))
    except NotRun as e:
        say("not run on {}: {}".format(name, e))
        return NOT_RUN
    r.tree = tree
    info = {"task": r.task.get("task"), "worker": r.worker,
            "what": "{} {}: {}".format("run-all.sh" if r.slot else "pa-run", r.kind, words(r.argv)[:120]), "dir": r.top}
    if r.progress is None:  # the slot step's run has its own (kind run-all.sh)
        r.progress = S.new_progress({"run": r.run, "kind": "pa-run", "what": words(r.argv)[:ARGV_KEEP], "for": r.kind,
                                     "task": r.task.get("task"), "worker": info["worker"], "dir": r.top,
                                     "commit": tree["commit"], "tree": tree["tree"], "pid": os.getpid(),
                                     "state": "waiting", "queued": pa.now(), "output": r.output, "place": "local"})
    else:
        S.update_progress(r.progress, commit=tree["commit"], tree=tree["tree"])
    probes, chosen, slot = {}, None, None
    queued = time.time()
    try:
        for m in candidates:  # a free slot first (a local flock), then the probe
            f, _ = S.try_slot(S.machine_dir(m["machine"]), m["cap"], info)
            if not f:
                continue
            probes[m["machine"]] = p = S.probe(m["machine"])
            if p["ok"]:
                chosen, slot = m, f
                break
            S.release(f)
            say("{} is not reachable: {}".format(m["machine"], p.get("reason")))
        if not chosen:
            for m in candidates:
                if m["machine"] not in probes:
                    probes[m["machine"]] = p = S.probe(m["machine"])
                    if not p["ok"]:
                        say("{} is not reachable: {}".format(m["machine"], p.get("reason")))
            reachable = [m for m in candidates if probes[m["machine"]]["ok"]]
            if reachable:
                chosen, slot = wait_for_slot(r, reachable, info)
                if not chosen:
                    return r.finish(NO_SLOT, None)
            else:
                why = "; ".join("{}: {}".format(k, v.get("reason")) for k, v in probes.items())
                return not_there(r, name, "not reachable ({})".format(why), fb)
        r.waited = time.time() - queued
        p = probes[chosen["machine"]]
        r.overhead["probe"] = p.get("seconds")
        try:
            return run_remote(r, chosen["machine"], p, slot)
        except S.SetupFailed as e:
            return not_there(r, chosen["machine"], "setup failed: {}".format(e), fb)
    except S.Interrupted as e:
        say("interrupted while choosing a place")
        r.notes.append("interrupted")
        return r.finish(e.code, None)
    finally:
        if slot is not None:
            S.release(slot)


def not_there(r, machine, why, fb):
    if r.args.on or fb == "none":
        line = "not run on {}: {}{}".format(machine, why, "" if r.args.on else
                                            " (suiteFallback \"none\": never this Mac on its own; --local runs here)")
        say(line)
        r.notes.append(line)
        return r.finish(NOT_RUN, None)
    if r.on_mac:
        return r.on_mac(r, "{} {}".format(machine, why))
    return run_local(r, "{} {}".format(machine, why))


def wait_for_slot(r, reachable, info):
    """R10: a slot on any reachable machine, waiting up to PA_SUITE_WAIT_LIMIT s (the slot step: SLOT_WAIT_LIMIT,
    and with a deadline (R14) each machine only until the deadline minus the kind's median there, else minus 300 s).
    (machine, slot) or (None, None)."""
    value = os.environ.get("PA_SUITE_WAIT_LIMIT", "")
    limit = float(value) if value.replace(".", "", 1).isdigit() else (S.SLOT_WAIT_LIMIT if r.slot else WAIT_LIMIT)
    began, shown = time.time(), None
    ends = {m["machine"]: S.wait_end(r.deadline, r.kind, m["machine"]) for m in reachable} if r.deadline else {}
    while True:
        texts, now = [], time.time()
        live = [m for m in reachable if now < ends.get(m["machine"], float("inf"))]
        for m in live:
            f, holders = S.try_slot(S.machine_dir(m["machine"]), m["cap"], info)
            if f:
                return m, f
            texts.append("{} (cap {}): {}".format(m["machine"], m["cap"], S.describe(holders)))
        text = "; ".join(texts) or "-"
        if live and text != shown:
            say("waiting for a slot on {}".format(text))
            S.update_progress(r.progress, waiting_for=text)
            shown = text
        if not live or time.time() - began >= limit:
            line = "not run on {}: no slot came free in {:.0f} s ({})".format(
                ", ".join(m["machine"] for m in reachable), time.time() - began,
                "running: " + (shown or text) if live else "the deadline (PA_SUITE_DEADLINE) leaves no time to run")
            say(line)
            r.notes.append(line)
            r.waited = time.time() - began
            return None, None
        time.sleep(S.POLL)


def argv_script(folder, tools, argv, subdir):
    """R16: the fixed remote script: t-089's environment, PA_SUITE_REMOTE=1, the quoted argv in the clone's subdir."""
    first = argv[0]
    if first not in PROGRAMS:
        first = "./" + os.path.normpath(first)
    command = words([first] + list(argv[1:]))
    changed = os.environ.get("PA_CHANGED_FILES", "")
    return "\n".join([
        "date +%s > {}/t0".format(folder),
        "unset HC_TERMINAL_ID HC_SOCKET HC_TERMINAL_SOCKET HC_SCOPE CLAUDECODE",
        "export HERDR_PA_HOME={0}/home TMPDIR={0}/tmp XDG_CACHE_HOME={0}/xdg/cache XDG_CONFIG_HOME={0}/xdg/config "
        "XDG_DATA_HOME={0}/xdg/data XDG_STATE_HOME={0}/xdg/state PYTHONDONTWRITEBYTECODE=1 npm_config_cache={0}/npm "
        "NODE={1} HOME={0}/u PA_SUITE_REMOTE=1 PA_CHANGED_FILES={2}".format(folder, tools["node"]["path"],
                                                                            shlex.quote(changed)),
        "export PATH={}/bin:$PATH".format(folder),
        "( cd {} && {} ) > {}/out 2>&1 < /dev/null".format(shlex.quote(subdir or "."), command, folder),
        "rc=$?",
        "date +%s > {}/t1".format(folder),
        "echo $rc > {0}/code.new && mv {0}/code.new {0}/code".format(folder), ""])


def run_remote(r, machine, found, slot):
    """R7, R16, R17 on machine under its slot. Its exit code; SetupFailed before anything ran there."""
    h, tree = r.h, r.tree
    tools, os_name = found.get("tools") or {}, found.get("os")
    folder = S.folder_of(r.run)
    if r.slot:  # R12: the run's first line
        say("on {} ({})".format(machine, os_name))
    else:
        say("run {} on {} ({}; reachable in {:g}s): output in {}".format(r.run, machine, os_name,
                                                                         found.get("seconds") or 0, r.output))
    for n in S.sweep(machine, r.run):
        say("leftovers of an earlier run on {}: {}".format(machine, n))
    S.update_progress(r.progress, place=machine, machine=machine, os=os_name, remote={"folder": folder, "terminal": None})
    terminal, watcher = None, None
    try:
        try:
            t = time.time()
            S.ship(h, machine, r.run, {"commit": tree["commit"], "tree": tree["tree"]}, tools, r.top,
                   patch=tree["patch"] or None, head=tree["head"])
            r.overhead["ship"] = round(time.time() - t, 1)
            t = time.time()
            terminal = S.start_remote(machine, r.run, folder, tools, argv_script(folder, tools, r.argv, r.subdir))
            r.overhead["start"] = round(time.time() - t, 1)
        except S.SetupFailed:
            S.cleanup(machine, r.run, terminal)
            S.update_progress(r.progress, cleaned=True, place="local", machine=None, remote=None)
            raise
        S.update_progress(r.progress, state="running", start=pa.now(), remote={"folder": folder, "terminal": terminal})
        watcher = S.RemoteWatcher(machine, folder, r.output, r.progress, None, sink=r.sink, fast=True)
        watcher.start()
        late = False
        while not watcher.ended.wait(0.2):
            if r.deadline and time.time() >= r.deadline:  # R14: the hook's deadline came
                late = True
                break
        seen = time.time()
        ran = None
        if late:
            watcher.stop()
            code = NO_SLOT
            r.notes.append("not run on {}: the deadline (PA_SUITE_DEADLINE) came after {:.0f} s; the run there was "
                           "ended".format(machine, time.time() - r.began))
        elif watcher.lost is not None:
            code = NO_SLOT
            r.notes.append("machine {} lost after {:.0f} s".format(machine, watcher.lost))
        elif watcher.timed_out:
            code = NO_SLOT
            r.notes.append("timed out after {} s there".format(S.REMOTE_TIMEOUT))
        else:
            code = watcher.code
            n = watcher.final()
            if n:
                r.notes.append(n)
            rest = (pa.read_text(r.output) or "").splitlines(True)[r.printed:]
            if rest:
                r.sink("".join(rest))
            c, out, _ = S.hc(["exec", "--on", machine, "--timeout", "30", "--", "cat", folder + "/t0", folder + "/t1"])
            stamps = [int(x) for x in out.split() if x.isdigit()] if not c else []
            if len(stamps) == 2:
                ran = stamps[1] - stamps[0]
                r.overhead["copyback"] = round(max(0.0, seen - stamps[1]), 1)
        if not r.slot:
            for n in r.notes:
                say(n)
        t = time.time()
        left = S.cleanup(machine, r.run, terminal)
        r.overhead["cleanup"] = round(time.time() - t, 1)
        terminal = None
        for n in left:
            say("cleanup on {}: {} (the exit stays {})".format(machine, n, code))
        S.update_progress(r.progress, cleaned=not [n for n in left if n.startswith("rm -rf")])
        if not r.slot:
            say("exit {} on {} ({}) after {:.0f}s; output in {}".format(code, machine, os_name, time.time() - r.began,
                                                                       r.output))
        elif code == NO_SLOT and r.notes:  # the slot step's not-run line comes last (R15 reads it)
            line = r.notes[-1]
            say(line if line.startswith("not run on ") else "not run on {}: {}".format(machine, line))
        return r.finish(code, machine, machine, os_name, ran)
    except S.Interrupted as e:
        say("interrupted: ending the run on {} and cleaning up".format(machine))
        if watcher is not None:
            watcher.stop()
        left = S.cleanup(machine, r.run, terminal)
        for n in left:
            say("cleanup on {}: {}".format(machine, n))
        S.update_progress(r.progress, cleaned=not [n for n in left if n.startswith("rm -rf")])
        r.notes.append("interrupted")
        return r.finish(e.code, machine, machine, os_name)
    finally:
        if watcher is not None:
            watcher.stop()


def run_local(r, why):
    """This Mac: the worktree itself, or (--commit) a scratch clone with the tree to run checked out."""
    say("run {} on this Mac ({}): output in {}".format(r.run, why, r.output))
    if why != "--local":
        r.notes.append("on this Mac: " + why)
    if r.progress is None:
        r.progress = S.new_progress({"run": r.run, "kind": "pa-run", "what": words(r.argv)[:ARGV_KEEP], "for": r.kind,
                                     "task": r.task.get("task"), "dir": r.top, "pid": os.getpid(), "state": "waiting",
                                     "queued": pa.now(), "output": r.output, "place": "local"})
    else:
        S.update_progress(r.progress, place="local", machine=None, remote=None)
    scratch = None
    child = None
    r.overhead = {"setup": 0}  # T1: a local run's setup (the scratch clone of --commit) apart from its own time
    try:
        root = r.top
        if r.args.commit:
            t = time.time()
            try:
                tree = getattr(r, "tree", None) or tree_to_run(r.h, r.top, r.args.commit, r.overlay, None)
                scratch = tempfile.mkdtemp(prefix="pa-run-")
                root = os.path.join(scratch, "w")
                local_checkout(r.top, root, tree)
            except NotRun as e:
                say("not run on this Mac: {}".format(e))
                r.notes.append(str(e))
                return r.finish(NOT_RUN, "local")
            r.overhead["setup"] = round(time.time() - t, 1)
        first = r.argv[0] if r.argv[0] in PROGRAMS else "./" + os.path.normpath(r.argv[0])
        S.update_progress(r.progress, state="running", start=pa.now())
        # PA_SUITE_LOCAL_RUN: a run-all.sh under it neither goes to a machine nor records (this run does)
        env = dict(os.environ, PA_SUITE_LOCAL_RUN=r.run)
        started = time.time()
        with open(r.output, "w") as out:
            child = subprocess.Popen([first] + r.argv[1:], cwd=os.path.join(root, r.subdir or ""), stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True, env=env)
            for line in iter(child.stdout.readline, b""):
                chunk = line.decode("utf-8", "replace")
                out.write(chunk)
                out.flush()
                r.sink(chunk)
            code = child.wait()
        ran = round(time.time() - started, 1)
        code = 128 - code if code < 0 else code
        say("exit {} on this Mac after {:.0f}s; output in {}".format(code, time.time() - r.began, r.output))
        return r.finish(code, "local", None, S.mac_os(), ran)
    except S.Interrupted as e:
        if child is not None:
            S.stop_group(child)
        r.notes.append("interrupted")
        return r.finish(e.code, "local", None, S.mac_os())
    finally:
        if child is not None and child.poll() is None:
            S.stop_group(child)
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)


def local_checkout(top, root, tree):
    """A scratch clone (objects shared, nothing written in the source) with C checked out and the patch applied to
    its index; its tree must be T."""
    env = dict(os.environ)
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        env.pop(k, None)
    for argv, stdin in ((["clone", "-q", "--shared", "--no-checkout", top, root], None),
                        (["-C", root, "checkout", "-q", "--detach", tree["commit"]], None)) + (
            ((["-C", root, "apply", "--index", "--binary", "-"], tree["patch"]),) if tree["patch"] else ()):
        try:
            p = subprocess.run(["git"] + argv, env=env, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=300)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise NotRun("git {}: {}".format(argv[0], e))
        if p.returncode:
            raise NotRun("git {}: {}".format(" ".join(argv[:3]), text(p.stderr)))
    p = subprocess.run(["git", "-C", root, "write-tree"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    if text(p.stdout) != tree["tree"]:
        raise NotRun("the scratch tree is {}, not {}".format(text(p.stdout) or "nothing", tree["tree"]))


# ---- the slot step's run on a machine (t-107 J2: R12-R14) ----

LOCAL = None  # slot_run's answer for "run it on this Mac" (the slot step goes on with today's path)
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def script_profile(record_, cfg):
    """The profile of the task's space (for its secretPaths), read under HERDR_PA_HOME without pa.py: {} when no
    profile is named anywhere (the guard's list alone, as the guard with no profile). Raises NotRun when one is
    named but cannot be read (fail closed)."""
    name, home = None, S.home()
    space = (record_.get("space") or "").split("/")[-1]
    if space:
        if not S.SEGMENT.fullmatch(space) or space in (".", ".."):
            raise NotRun("the task's space {!r} is not a space id".format(space))
        try:
            with open(os.path.join(home, "spaces", space, "agent.json")) as f:
                name = (json.load(f) or {}).get("profile")
        except (OSError, ValueError, AttributeError):
            name = None
    name = name or os.environ.get("HERDR_PA_PROFILE") or (cfg or {}).get("defaultProfile")
    if not name:
        return {}
    if not isinstance(name, str) or not S.SEGMENT.fullmatch(name) or name in (".", ".."):
        raise NotRun("the profile {!r} is not a profile name".format(name))
    try:
        with open(os.path.join(home, "profiles", name, "profile.json")) as f:
            profile = json.load(f)
    except (OSError, ValueError) as e:
        raise NotRun("the profile {!r} cannot be read for its secretPaths ({})".format(name, e))
    if not isinstance(profile, dict) or profile.get("name") != name:
        raise NotRun("the profile {!r} cannot be read for its secretPaths".format(name))
    return profile


def check_changed(value, secrets):
    """R12: PA_CHANGED_FILES's paths, each relative, inside the tree, no control character, not a secret pattern or
    under .claude/ (they are sent): None, or why not."""
    for p in [w for w in re.split(r"[ \t\n]+", value or "") if w]:
        if CONTROL.search(p) or os.path.isabs(p) or clean_rel(p) in (None, ""):
            return "PA_CHANGED_FILES names {!r}: not a relative path inside the tree".format(p)
        if p.startswith(".claude/") or "/.claude/" in "/" + p:
            return "PA_CHANGED_FILES names {!r}: under .claude/, never sent".format(p)
        for pattern, what in secrets:
            if re.search(pattern, p):
                return "PA_CHANGED_FILES names {!r}: {}, never sent".format(p, what)
    return None


def slot_run(h, command, flags, kind, step_args, progress, at):
    """R12: the slot step's `sh tests/run-all.sh <flags>` on a machine, through pa-run's code (tree to run, place,
    ship, terminal, watcher, cleanup, records). Lines say `run-all.sh: ...` on stdout, the first `on <M> (<os>)`,
    then the run's output as it comes; 124/125 end with `run-all.sh: not run on <M>: <reason>`. Its exit code, or
    LOCAL (not reachable or setup failed with suiteFallback "mac": the step runs it here and has said why)."""
    import types
    global PREFIX, TO_STDOUT, _h
    PREFIX, TO_STDOUT, _h = "run-all.sh", True, h
    cfg = pa.config()
    record_ = S.task_record(step_args.dir) or {}
    top = os.path.realpath(step_args.dir)
    argv = ["sh", RUN_ALL] + list(flags)
    args = types.SimpleNamespace(on=None, local=False, commit=None, overlay=None, cwd=None, kind=kind)
    name = (S.machines(cfg)[0] or [{}])[0].get("machine") or "a machine"
    output_dir = None
    space = (record_.get("space") or "").split("/")[-1]
    evidence = os.path.join(S.home(), "spaces", space, "evidence", record_.get("task") or "") if space and \
        S.SEGMENT.fullmatch(space) and S.SEGMENT.fullmatch(record_.get("task") or "") else None
    if not (evidence and os.path.isdir(os.path.dirname(evidence))):
        output_dir = tempfile.mkdtemp(prefix="pa-run-slot-")
        evidence = output_dir
    else:
        os.makedirs(evidence, exist_ok=True)

    def done(code, line=None):
        if line:
            say("not run on {}: {}".format(name, line))
        S.update_progress(progress, state="done", exit=code, end=pa.now())
        return code
    try:
        try:
            profile = script_profile(record_, cfg)
        except NotRun as e:
            return done(NOT_RUN, str(e))
        why = check_changed(os.environ.get("PA_CHANGED_FILES", ""), secret_patterns(h, profile))
        if why:
            return done(NOT_RUN, why)
        r = Run(h, args, dict(record_, task=record_.get("task")), None, profile, output=os.devnull)
        r.output = os.path.join(evidence, "run-{}.out".format(r.run))
        r.temp_output = output_dir is not None
        r.slot, r.worker, r.deadline, r.progress = True, step_args.worker or None, at, progress
        r.argv, r.top, r.subdir, r.overlay = argv, top, "", []
        here = {}

        def on_mac(r_, why_):
            say("on this Mac: " + why_)
            here["why"] = why_
            return LOCAL
        r.on_mac = on_mac
        saved = {}

        def on_signal(sig, frame):
            raise S.Interrupted(S.SIGNAL_EXIT.get(sig, 1))
        for sig in S.SIGNAL_EXIT:
            saved[sig] = signal.signal(sig, on_signal)
        try:
            code = place_and_run(r)
        finally:
            for sig, old in saved.items():
                signal.signal(sig, old)
        if code is LOCAL:
            S.update_progress(progress, state="waiting", place="local", machine=None, remote=None,
                              note="on this Mac: " + here.get("why", ""))
            return LOCAL
        S.update_progress(progress, state="done", exit=code, end=pa.now())
        return code
    finally:
        if output_dir:
            shutil.rmtree(output_dir, ignore_errors=True)


# ---- the report (T2): which kinds of run are faster where ----

MIN_RUNS = 3
SAME = 0.10  # medians within 10 % of the faster one: about the same


def fmt(value):
    return "-" if value is None else "{:g}s".format(round(value, 1))


def report(days=None):
    """T2: per kind and place, the run count and medians of the test's own time (ran), setup+transfer (overhead),
    the wall time without the wait (seconds - waited) and the wait; per kind a verdict from the wall medians."""
    import statistics
    rows = S.read_lines(S.test_runs_path())
    if days:
        rows = [r for r in rows if (S.epoch(r.get("at")) or 0) >= time.time() - days * 86400]
    groups = {}
    for r in rows:
        if r.get("exit") in (NO_SLOT, NOT_RUN, None) or not isinstance(r.get("seconds"), (int, float)):
            continue
        groups.setdefault(r.get("kind") or "other", {}).setdefault(r.get("place") or "local", []).append(r)

    def med(values):
        values = [v for v in values if isinstance(v, (int, float))]
        return statistics.median(values) if values else None

    def stats(runs):
        return {"n": len(runs),
                "wall": med([r["seconds"] - (r.get("waited") or 0) for r in runs]),
                "ran": med([r.get("ran") for r in runs]),
                "over": med([sum(v for v in (r.get("overhead") or {}).values() if isinstance(v, (int, float)))
                             for r in runs]),
                "waited": med([r.get("waited") or 0 for r in runs])}
    lines = ["test runs by kind and place ({}, {} lines{}); verdicts compare the median wall time without the slot "
             "wait, {} runs each needed".format(S.test_runs_path(), len(rows), ", last {} days".format(days) if days
                                                 else "", MIN_RUNS)]
    order = ["affected", "single", "module", "failing-first", "mutation", "full", "measure", "other"]
    for kind in sorted(groups, key=lambda k: (order.index(k) if k in order else len(order), k)):
        places = {p: stats(v) for p, v in groups[kind].items()}
        local = places.get("local")
        machines_ = sorted(p for p in places if p != "local")
        if not machines_ or not local or local["n"] < MIN_RUNS:
            there = sum(places[m]["n"] for m in machines_)
            lines.append("{}: not enough runs ({} local, {} on {}; {} each needed)".format(
                kind, local["n"] if local else 0, there, machines_[0] if len(machines_) == 1 else "a machine", MIN_RUNS))
        for m in machines_:
            other = places[m]
            if not local or local["n"] < MIN_RUNS:
                continue
            if other["n"] < MIN_RUNS:
                verdict = "not enough runs ({} local, {} on {}; {} each needed)".format(local["n"], other["n"], m, MIN_RUNS)
            elif abs(local["wall"] - other["wall"]) <= SAME * min(local["wall"], other["wall"]):
                verdict = "about the same"
            else:
                verdict = "faster locally" if local["wall"] < other["wall"] else "faster on " + m
            lines.append("{}: {} (local {} runs, median {}; {} {} runs, median {})".format(
                kind, verdict, local["n"], fmt(local["wall"]), m, other["n"], fmt(other["wall"])))
        for p in ["local"] + machines_:
            if p in places:
                st = places[p]
                lines.append("  {:<24} runs {:>3}  test {:>8}  setup+transfer {:>7}  wall {:>8}  waited {:>7}".format(
                    p, st["n"], fmt(st["ran"]), fmt(st["over"]), fmt(st["wall"]), fmt(st["waited"])))
    if not groups:
        lines.append("no runs recorded yet")
    print("\n".join(lines))
    return 0


def report_main(h, args):
    """pa-run --report (T2): no worktree needed."""
    global _h
    _h = h
    load(h)
    return report(args.days)
