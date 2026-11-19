"""Shared fixture of the merge queue's tests (tests/test_pa_queue.py, t-052 J2). Not a test module.

No hc calls and no real homes: HOME, HERDR_PA_HOME, CLAUDE_CONFIG_DIR and HERDR_APP_HOME point at a temp folder made
with mkdtemp, and git runs only on repositories made under it. A fake space sp1 holds the ledger, the evidence and
queue.json. pa-queue's git calls run with no GIT_* variable (as pa-push-main's do), so each scratch repository has its
own user.name and user.email, and HOME's .gitconfig is the test's own (empty unless a test writes it).
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import repo_template  # noqa: E402 (t-095)

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.realpath(os.path.join(HERE, ".."))
TOOLS = os.path.join(REPO, "runtime", "tools")
TMP = os.path.realpath(tempfile.mkdtemp(prefix="t052-queue-"))

_HOMES = {"HERDR_PA_HOME": os.path.join(TMP, "pa"), "CLAUDE_CONFIG_DIR": os.path.join(TMP, "claude"),
          "HERDR_APP_HOME": os.path.join(TMP, "app")}
_saved_env = {k: os.environ.get(k) for k in _HOMES}
_saved_mods = {k: sys.modules.get(k) for k in ("pa", "pa_hooks", "pa_push", "pa_queue")}
os.environ.update(_HOMES)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(TOOLS, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pa = _load("pa")
pa_hooks = _load("pa_hooks")
pa_push = _load("pa_push")
try:
    pa_queue = _load("pa_queue")
except FileNotFoundError:  # before the tool exists (the failing-tests commit)
    pa_queue = None
for _k, _v in _saved_mods.items():
    if _v is None:
        sys.modules.pop(_k, None)
    else:
        sys.modules[_k] = _v
for _k, _v in _saved_env.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v
assert pa.ROOT.startswith(TMP), "pa.py must use the temp homes"


def _load_pa_suite():
    """runtime/tools/pa_suite.py as pa-full-suite loads it (pa_hooks.cmd_full_suite): its own module, given pa."""
    spec = importlib.util.spec_from_file_location("pa_suite", os.path.join(TOOLS, "pa_suite.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.pa = pa
    return module


pa_suite_module = _load_pa_suite()

TEST_ENV = {"HOME": os.path.join(TMP, "home"), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid", "HERDR_PA_HOME": _HOMES["HERDR_PA_HOME"],
            "CLAUDE_CONFIG_DIR": _HOMES["CLAUDE_CONFIG_DIR"], "HERDR_APP_HOME": _HOMES["HERDR_APP_HOME"]}
DROP_ENV = ("HC_TERMINAL_ID", "HC_SOCKET", "HC_TERMINAL_SOCKET", "HC_SCOPE", "CLAUDECODE", "GIT_DIR", "GIT_WORK_TREE")
_module_env = {}

SINCE = "2000-01-01T00:00:00Z"  # the space's content stamp
AFTER = "2000-01-02T00:00:00Z"  # verifiers started after it: the content check binds
LINES = "".join("line {}\n".format(n) for n in range(1, 201))
REVIEW_FILES = ["intent.md", "spec.md", "plan.md", "FINDINGS.md", "DECISIONS.md", "REVIEW.md", "build.log"]
SPACE = "sp1"


def no_push_tool_path(path):
    """PATH without the folders that hold an installed push tool (t-140 R1: a queue card pushes only where the tool
    is on PATH): a queue test sees the tool only where it puts a stand-in itself, on every machine alike."""
    keep = [p for p in (path or "").split(os.pathsep) if p and not os.path.exists(os.path.join(p, "pa-push-main"))]
    return os.pathsep.join(keep)


def setUpModule():
    for k in list(TEST_ENV) + list(DROP_ENV) + ["PATH"]:
        _module_env[k] = os.environ.get(k)
    for k in DROP_ENV:
        os.environ.pop(k, None)
    os.environ.update(TEST_ENV)
    os.environ["PATH"] = no_push_tool_path(os.environ.get("PATH"))
    os.makedirs(TEST_ENV["HOME"], exist_ok=True)


def tearDownModule():
    for k, v in _module_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    shutil.rmtree(TMP, ignore_errors=True)


def dump(value, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(value, f, indent=2)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def read(path):
    with open(path) as f:
        return f.read()


def sh(cwd, *argv):
    r = subprocess.run(list(argv), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    if r.returncode:
        raise AssertionError("{} failed in {}: {}".format(" ".join(argv), cwd, r.stderr))
    return r.stdout.strip()


def line_edit(text, n, new):
    lines = text.splitlines(True)
    lines[n - 1] = new + "\n"
    return "".join(lines)


def snapshot(*paths):
    """{path: bytes} of every file under the paths (missing ones too, as None)."""
    out = {}
    for top in paths:
        if os.path.isfile(top) or not os.path.exists(top):
            out[top] = open(top, "rb").read() if os.path.isfile(top) else None
            continue
        for folder, _dirs, files in os.walk(top):
            for name in files:
                p = os.path.join(folder, name)
                with open(p, "rb") as f:
                    out[p] = f.read()
    return out


def build_proj(path):
    """The main checkout every QBase test starts from (copied per test, tests/repo_template.py)."""
    sh(path, "git", "init", "-q", "-b", "main")
    sh(path, "git", "config", "user.name", "t")
    sh(path, "git", "config", "user.email", "t@example.invalid")
    write(os.path.join(path, "a.txt"), LINES)
    write(os.path.join(path, "b.txt"), "b\n")
    write(os.path.join(path, "README"), "readme\n")
    sh(path, "git", "add", "a.txt", "b.txt", "README")
    sh(path, "git", "commit", "-q", "-m", "base")


class QBase(unittest.TestCase):
    """A main checkout `proj` (main: a.txt of 200 lines, b.txt, README) and a fake space sp1."""

    def setUp(self):
        self.assertIsNotNone(pa_queue, "runtime/tools/pa_queue.py does not exist yet")
        shutil.rmtree(pa.ROOT, ignore_errors=True)
        self.tmp = os.path.realpath(tempfile.mkdtemp(dir=TMP))
        self.dir = os.path.join(pa.ROOT, "spaces", SPACE)
        dump({"space": SPACE, "terminal": "t0", "terminals": ["t0"], "profile": "test", "name": "S"}, os.path.join(self.dir, "agent.json"))
        dump({"name": "test", "claude": ["true"], "codex": ["true"]}, os.path.join(pa.ROOT, "profiles", "test", "profile.json"))
        dump({"version": 1, "tasks": []}, os.path.join(self.dir, "ledger.json"))
        dump({"version": 1, "at": SINCE}, os.path.join(self.dir, "content", "since.json"))
        self.saved = {k: getattr(pa, k) for k in ("hc_get", "call")}
        pa.hc_get = lambda address: {}
        pa.call = self.no_call
        self.saved_wait = pa_queue.LOCK_WAIT
        self.cwd = os.getcwd()
        os.chdir(self.tmp)  # no task record above this folder
        self.proj = repo_template.copy(TMP, "proj", os.path.join(self.tmp, "proj"), build_proj)  # t-095: built once
        self.trees = {}

    def tearDown(self):
        os.chdir(self.cwd)
        for k, v in self.saved.items():
            setattr(pa, k, v)
        pa_queue.LOCK_WAIT = self.saved_wait
        gitconfig = os.path.join(TEST_ENV["HOME"], ".gitconfig")
        if os.path.exists(gitconfig):
            os.remove(gitconfig)

    def no_call(self, argv, check=True):
        raise AssertionError("no hc call expected: {}".format(argv))

    # ---- the ledger and evidence ----
    def ledger(self):
        return pa.read_json(os.path.join(self.dir, "ledger.json"))

    def set_ledger(self, led):
        dump(led, os.path.join(self.dir, "ledger.json"))

    def ledger_task(self, tid):
        return next(t for t in self.ledger()["tasks"] if t["id"] == tid)

    def add_worker(self, tid, address, role, started=None, finished=True):
        led = self.ledger()
        task = next(t for t in led["tasks"] if t["id"] == tid)
        w = {"address": address, "role": role, "started": started or pa.now()}
        if finished:
            w["finished"] = True
        task["workers"].append(w)
        self.set_ledger(led)

    def evidence(self, tid):
        return os.path.join(self.dir, "evidence", tid)

    # ---- tasks ----
    def task(self, tid, edits, start="main", gate=True):
        """A ledger task with a linked worktree on <tid>-x from start; edits: [(file, text or (line, new))], one
        commit each. VERIFY.md names the tip; pa-gate runs (it stores the records the queue reads)."""
        wt = os.path.join(self.tmp, "wt-" + tid)
        sh(self.proj, "git", "worktree", "add", "-q", "-b", tid + "-x", wt, start)
        self.trees[tid] = wt
        for f, change in edits:
            self.change(tid, f, change)
        led = self.ledger()
        led["tasks"].append({"id": tid, "title": "T", "state": "in-progress", "tier": "normal", "type": "feature", "worktree": wt,
                             "workers": [{"address": tid + "b", "role": "build", "started": AFTER, "finished": True},
                                         {"address": tid + "r", "role": "reviewer", "started": AFTER, "finished": True},
                                         {"address": tid + "v", "role": "verifier", "started": AFTER, "finished": True}]})
        self.set_ledger(led)
        for name in REVIEW_FILES:
            write(os.path.join(self.evidence(tid), name), "text\n")
        self.checked(tid)
        if gate:
            code, out, _err = self.gate(tid)
            self.assertEqual(code, 0, out)
        return wt

    def change(self, tid, f, change, message=None):
        wt = self.trees[tid]
        path = os.path.join(wt, f)
        if isinstance(change, tuple):
            text = line_edit(read(path), change[0], change[1])
        else:
            text = change
        write(path, text)
        sh(wt, "git", "add", f)
        sh(wt, "git", "commit", "-q", "-m", message or "{} {}".format(tid, f))
        return self.tip(tid)

    def checked(self, tid, sha=None):
        write(os.path.join(self.evidence(tid), "VERIFY.md"),
              "text\nChecked: {}\nVerdict: pass\n".format(sha or self.tip(tid)))

    def tip(self, tid):
        return sh(self.trees[tid], "git", "rev-parse", "HEAD")

    def ref(self, name):
        return sh(self.proj, "git", "rev-parse", "refs/heads/" + name)

    def refs(self):
        return sh(self.proj, "git", "for-each-ref", "--format=%(refname) %(objectname)")

    def on_main(self, f, change):
        path = os.path.join(self.proj, f)
        text = line_edit(read(path), change[0], change[1]) if isinstance(change, tuple) else change
        write(path, text)
        sh(self.proj, "git", "add", f)
        sh(self.proj, "git", "commit", "-q", "-m", "main " + f)
        return self.ref("main")

    # ---- the tools ----
    def run_main(self, module, argv):
        out, err, code = io.StringIO(), io.StringIO(), 0
        saved = sys.argv
        sys.argv = argv
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = module.main() or 0
                except SystemExit as e:
                    code = e.code if isinstance(e.code, int) else (0 if e.code is None else 2)
                except pa.Problem as e:
                    print(str(e), file=sys.stderr)
                    code = 2
        finally:
            sys.argv = saved
        return code, out.getvalue(), err.getvalue()

    def gate(self, tid):
        return self.run_main(pa_hooks, ["pa-gate", tid, "--space", SPACE])

    def queue(self, *args):
        return self.run_main(pa_queue, ["pa-queue"] + list(args) + ["--space", SPACE])

    def add(self, tid, ok=True):
        code, out, err = self.queue("add", tid, "--repo", self.proj)
        if ok:
            self.assertEqual(code, 0, out + err)
        return code, out, err

    def suite(self, tid, code):
        """One suites.jsonl line as t-056's pa-full-suite writes it: pins (with untracked) from pa_suite.pins, start
        and end, written by pa_hooks.suite_record (t-052 J3a A6: a line with no untracked count is not reusable)."""
        found = pa_suite_module.pins(types.SimpleNamespace(**vars(pa_hooks)), self.trees[tid])
        stamp = pa.now()
        pa_hooks.suite_record(self.dir, {"task": tid, "root": self.trees[tid]}, "full", code, 1.0, found,
                              {"start": stamp, "end": stamp})

    # ---- the files ----
    def qfile(self):
        return os.path.join(self.dir, "queue.json")

    def qrepo(self):
        data = pa.read_json(self.qfile()) or {}
        return (data.get("repos") or {}).get(os.path.realpath(self.proj)) or {}

    def entries(self):
        return self.qrepo().get("entries") or []

    def entry(self, tid):
        return next(e for e in self.entries() if e["task"] == tid)

    def set_entries(self, change):
        data = pa.read_json(self.qfile())
        change(data["repos"][os.path.realpath(self.proj)])
        dump(data, self.qfile())

    def journal(self, event=None):
        path = os.path.join(self.dir, "journal.jsonl")
        lines = [json.loads(l) for l in read(path).splitlines()] if os.path.exists(path) else []
        return [l for l in lines if l.get("trigger") == "queue" and (event is None or l.get("event") == event)]

    def base_of(self, tid, onto):
        return sh(self.proj, "git", "merge-base", "refs/heads/{}-x".format(tid), onto)
