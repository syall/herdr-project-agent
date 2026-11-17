"""Shared fixture of the push tool's tests (tests/test_pa_push*.py), moved from tests/test_pa_push.py
(t-030) so the tests can run as several parts at once. Not a test module: run-all.sh and discover only
collect tests/test_*.py.


Run: python3 -m unittest -v tests/test_pa_push.py
No hc calls and no real homes: HOME, HERDR_PA_HOME, CLAUDE_CONFIG_DIR and HERDR_APP_HOME point at a temp
folder, and git runs only on repositories made under that temp folder. The only remote anywhere is a bare
repository under that temp folder, reached by a file:// URL: nothing here contacts a host. The person-only
check (C0) has no switch: the tests call run_push() and allow_change() in-process with an injected
environment and process table, and every command-line run of a person-only command expects a refusal.
"""
import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.realpath(os.path.join(HERE, ".."))
TOOLS = os.path.join(REPO, "runtime", "tools")
TMP = os.path.realpath(tempfile.mkdtemp(prefix="t007b-push-"))

_HOMES = {"HERDR_PA_HOME": os.path.join(TMP, "pa"), "CLAUDE_CONFIG_DIR": os.path.join(TMP, "claude"),
          "HERDR_APP_HOME": os.path.join(TMP, "app")}
_saved_env = {k: os.environ.get(k) for k in _HOMES}
_saved_mods = {k: sys.modules.get(k) for k in ("pa", "pa_hooks")}
os.environ.update(_HOMES)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(TOOLS, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pa = _load("pa")
pa_hooks = _load("pa_hooks")
try:
    pa_push = _load("pa_push")
except FileNotFoundError:  # before the tool exists (the failing-tests commit)
    pa_push = None
sys.modules.pop("pa_push", None)
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

TEST_ENV = {"HOME": os.path.join(TMP, "home"), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid", "HERDR_PA_HOME": _HOMES["HERDR_PA_HOME"],
            "CLAUDE_CONFIG_DIR": _HOMES["CLAUDE_CONFIG_DIR"], "HERDR_APP_HOME": _HOMES["HERDR_APP_HOME"]}
DROP_ENV = ("HC_TERMINAL_ID", "HC_SOCKET", "HC_TERMINAL_SOCKET", "HC_SCOPE", "GIT_DIR", "GIT_WORK_TREE")
_module_env = {}

# What a person's plain terminal looks like to C0: no agent variable, no Herdr terminal, no agent ancestor.
PERSON_ENV = {"HOME": TEST_ENV["HOME"], "PATH": os.environ.get("PATH", "")}
PERSON_TREE = [{"pid": 11, "comm": "/bin/zsh", "args": "-zsh"}, {"pid": 1, "comm": "/sbin/launchd", "args": "/sbin/launchd"}]
# What the panel's check and run look like to C0: started by Herdr's core under the Herdr app (as `ps` shows them).
PANEL_TREE = [{"pid": 31, "comm": "/Users/p/.local/bin/herdr-core", "args": "/Users/p/.local/bin/herdr-core"},
              {"pid": 30, "comm": "/Applications/Herdr.app/Contents/MacOS/Herdr", "args": "/Applications/Herdr.app/Contents/MacOS/Herdr"},
              {"pid": 1, "comm": "/sbin/launchd", "args": "/sbin/launchd"}]
SPACE = "sp1"
TASK = "t-1"


def setUpModule():
    for k in list(TEST_ENV) + list(DROP_ENV):
        _module_env[k] = os.environ.get(k)
    for k in DROP_ENV:
        os.environ.pop(k, None)
    os.environ.update(TEST_ENV)
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


def load(path):
    with open(path) as f:
        return json.load(f)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def sh(cwd, *argv, check=True):
    """A fixture command (never the tool's): runs with the test environment."""
    r = subprocess.run(list(argv), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    if check and r.returncode:
        raise AssertionError("{} failed in {}: {}".format(" ".join(argv), cwd, r.stderr))
    return r.stdout.strip()


def snapshot(folder):
    """Every file under folder with its bytes (to prove a read-only command wrote nothing)."""
    out = {}
    for base, _dirs, names in os.walk(folder):
        for name in names:
            path = os.path.join(base, name)
            with open(path, "rb") as f:
                out[os.path.relpath(path, folder)] = f.read()
    return out


REVIEW_FILES = ["intent.md", "spec.md", "plan.md", "FINDINGS.md", "DECISIONS.md", "VERIFY.md", "REVIEW.md", "build.log"]
WORKERS = [{"address": "t31", "role": "build"}, {"address": "t32", "role": "reviewer"}]


class Repo(unittest.TestCase):
    """origin.git (bare, the only remote), main (its clone: the main checkout), wt (a linked worktree on t-1-x
    with one commit), a space sp1 with task t-1 whose full gate passes, and an allowlist entry for main."""

    def setUp(self):
        if pa_push is None:
            self.fail("runtime/tools/pa_push.py does not exist yet")
        shutil.rmtree(pa.ROOT, ignore_errors=True)
        self.tmp = os.path.realpath(tempfile.mkdtemp(dir=TMP))
        self.assertTrue(pa.ROOT.startswith(TMP) and os.environ["HOME"].startswith(TMP))
        self.origin = os.path.join(self.tmp, "origin.git")
        self.main = os.path.join(self.tmp, "main")
        self.wt = os.path.join(self.tmp, "wt")
        self.url = "file://" + self.origin
        self.assertTrue(self.url.startswith("file://" + TMP))
        sh(self.tmp, "git", "-c", "init.defaultBranch=main", "init", "-q", "--bare", self.origin)
        sh(self.tmp, "git", "clone", "-q", self.url, self.main)
        sh(self.main, "git", "checkout", "-q", "-b", "main")
        write(os.path.join(self.main, "a.txt"), "one\n")
        sh(self.main, "git", "add", "a.txt")
        sh(self.main, "git", "commit", "-q", "-m", "first")
        sh(self.main, "git", "push", "-q", "origin", "main")
        sh(self.main, "git", "worktree", "add", "-q", "-b", "t-1-x", self.wt, "main")
        self.commit_on(self.wt, "b.txt", "task work")
        self.dir = os.path.join(pa.ROOT, "spaces", SPACE)
        dump({"space": SPACE, "terminal": "t0", "terminals": ["t0"], "profile": "test", "name": "S"}, os.path.join(self.dir, "agent.json"))
        dump({"name": "test", "claude": ["true"], "codex": ["true"]}, os.path.join(pa.ROOT, "profiles", "test", "profile.json"))
        dump({"version": 1, "tasks": [{"id": TASK, "title": "T", "state": "in-progress", "tier": "high", "type": "feature",
                                       "worktree": self.wt, "workers": WORKERS}]}, os.path.join(self.dir, "ledger.json"))
        for name in REVIEW_FILES:
            write(os.path.join(self.dir, "evidence", TASK, name), "text\n")
        os.makedirs(os.path.join(self.dir, "decisions"), exist_ok=True)
        self.allow = os.path.join(pa.ROOT, "push-allow.json")
        self.entry = {"path": self.main, "url": self.url, "branch": "main", "space": SPACE, "enabled": True}
        dump({"version": 1, "enabled": True, "repos": [self.entry]}, self.allow)
        self.saved = {k: getattr(pa, k) for k in ("hc_get", "call")}
        pa.hc_get = lambda address: {}
        pa.call = self.no_call
        self.calls = []
        self.real_git_call = pa_push.git_call
        pa_push.git_call = self.record_call
        self.cwd = os.getcwd()
        os.chdir(self.tmp)  # no task record above this folder

    def tearDown(self):
        os.chdir(self.cwd)
        for k, v in getattr(self, "saved", {}).items():
            setattr(pa, k, v)
        if pa_push is not None and hasattr(self, "real_git_call"):
            pa_push.git_call = self.real_git_call

    def no_call(self, argv, check=True):
        raise AssertionError("no hc call expected: {}".format(argv))

    def record_call(self, argv, timeout, cwd=None):
        self.calls.append(list(argv))
        return self.real_git_call(argv, timeout, cwd=cwd)

    # ---- helpers

    def commit_on(self, repo, name="c.txt", subject="more"):
        write(os.path.join(repo, name), subject + "\n")
        sh(repo, "git", "add", name)
        sh(repo, "git", "commit", "-q", "-m", subject)
        return sh(repo, "git", "rev-parse", "HEAD")

    def head(self, repo, ref="HEAD"):
        return sh(repo, "git", "rev-parse", ref)

    def origin_head(self):
        return sh(self.origin, "git", "rev-parse", "refs/heads/main")

    def gate(self, task=TASK, phase=None):
        argv = ["pa-gate", task, "--space", SPACE] + (["--phase", phase] if phase else [])
        saved = sys.argv
        sys.argv = argv
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                try:
                    pa_hooks.main()
                except SystemExit as e:
                    return e.code
        finally:
            sys.argv = saved
        return 0

    def set_allow(self, **changes):
        data = load(self.allow)
        data["repos"][0].update(changes)
        dump(data, self.allow)

    def ledger_task(self, **changes):
        path = os.path.join(self.dir, "ledger.json")
        led = load(path)
        led["tasks"][0].update(changes)
        dump(led, path)

    def cli(self, name, *args, env=None, cwd=None):
        """A command-line run of the tool, as the person's symlink would start it, in a subprocess."""
        link_dir = os.path.join(self.tmp, "bin")
        os.makedirs(link_dir, exist_ok=True)
        link = os.path.join(link_dir, name)
        if not os.path.islink(link):
            os.symlink(os.path.join(TOOLS, "pa_push.py"), link)
        r = subprocess.run([sys.executable, link] + list(args), cwd=cwd or self.tmp, env=env or dict(os.environ),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        return r.returncode, r.stdout, r.stderr

    def quiet(self, fn, *args, **kw):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                value = fn(*args, **kw)
            except SystemExit as e:
                value = e.code
        return value, out.getvalue(), err.getvalue()


CARD = "d-push-t-1-1"
FACT_KEYS = {"path", "url", "branch", "originUrl", "remoteNow", "localNow", "commit", "taskBranch", "branchTip", "merge",
             "gate", "commits", "count", "allow", "commands", "checkedAt"}


class Planned(Repo):
    """The fixture with a passed review gate."""

    def setUp(self):
        Repo.setUp(self)
        self.assertEqual(self.gate(), 0)
        self.task_commit = self.head(self.wt)
        self.main_before = self.head(self.main)

    def plan(self, *extra, repo=None):
        args = pa_push.main_parser().parse_args(["plan", TASK, "--repo", repo or self.main, "--space", SPACE] + list(extra))
        return self.quiet(pa_push.cmd_plan, args)

    def card_path(self, card=CARD):
        return os.path.join(self.dir, "decisions", card + ".json")

    def card(self, card=CARD):
        return load(self.card_path(card))

    def check(self, card=CARD, env=None, tree=None):
        return pa_push.check_card(self.dir, SPACE, card, env if env is not None else PERSON_ENV, PANEL_TREE if tree is None else tree)

    def journal(self):
        path = os.path.join(self.dir, "journal.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(l) for l in f if l.strip()]


FRESH = object()  # take the seen token from a check made just now


class Runs(Planned):
    """A push card made by plan, and the seen token of a check made right after it (what the person saw)."""

    def setUp(self):
        Planned.setUp(self)
        self.assertEqual(self.plan()[0], 0)
        self.seen = self.check()["seen"]
        self.assertTrue(self.seen)
        self.origin_before = self.origin_head()
        self.main_before = self.head(self.main)
        self.calls[:] = []

    def replan(self, **entry_changes):
        """The allowlist entry changed by the person, and a new card made with it."""
        self.set_allow(**entry_changes)
        os.remove(self.card_path())
        code, _o, err = self.plan()
        self.assertEqual(code, 0, err)
        self.seen = self.check()["seen"]

    def run_push(self, answer="Push", seen=None, env=None, tree=None, space=SPACE, card=CARD, note=None, deadline=None):
        if seen is FRESH:
            seen = self.check(card)["seen"]
        elif seen is None:
            seen = self.seen
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            return pa_push.run_push(space, card, answer, note, seen, PERSON_ENV if env is None else env,
                                    PANEL_TREE if tree is None else tree, deadline)

    def card_edit(self, **push_changes):
        c = self.card()
        c["push"].update(push_changes)
        dump(c, self.card_path())

    def gate_edit(self, **pin_changes):
        path = os.path.join(self.dir, "evidence", TASK, "GATE.json")
        g = load(path)
        if pin_changes.pop("no_pin", False):
            g["commits"] = []
        g["commits"] and g["commits"][0].update(pin_changes)
        dump(g, path)

    def assertRefused(self, result, check, text, attempts=1, origin=True):
        code, r = result
        self.assertEqual(code, 1, r)
        self.assertFalse(r["ok"])
        self.assertFalse(r["done"])
        self.assertEqual(r["refusal"]["check"], check, r["refusal"])
        self.assertIn(text, r["refusal"]["message"])
        if origin:
            self.assertEqual(self.origin_head(), self.origin_before)
        self.assertEqual(self.head(self.main, "refs/heads/main"), self.main_before)
        if os.path.exists(self.card_path()):
            c = self.card()
            if c.get("push", {}).get("space") == SPACE and c["status"] in ("open",):
                self.assertEqual(len(c["push"]["attempts"]), attempts, c["push"]["attempts"])
                if attempts:
                    self.assertEqual(c["push"]["attempts"][-1]["check"], check)
                    self.assertIn("push refused: ", self.journal()[-1]["why"])
        self.assertFalse(any(a[:1] == ["git"] and "push" in a for a in self.calls), self.calls)
        return r


FORBIDDEN = {"-f", "--mirror", "--all", "--prune", "--delete", "--no-verify", "--tags"}


def git_calls(calls, word):
    return [a for a in calls if a[:1] == ["git"] and word in a]
