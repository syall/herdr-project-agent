"""Unit tests for pa-push-main and pa-push-allow (t-007 part 7b, Job A): the person's push card.

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


# ---- A1: the allowlist ----------------------------------------------------------------------------

class Allowlist(Repo):
    def valid(self, **entry_changes):
        e = dict(self.entry, **entry_changes)
        return {"version": 1, "enabled": True, "repos": [e]}

    def test_a01_missing_file_is_off(self):
        os.remove(self.allow)
        data, problem, sha, mtime = pa_push.load_allow()
        self.assertEqual((data, problem, sha, mtime), (None, None, None, None))
        self.assertEqual(pa_push.allow_path(), os.path.join(pa.ROOT, "push-allow.json"))
        shown = pa_push.allow_report(SPACE)
        self.assertFalse(shown["exists"])
        self.assertIn("off (the default)", shown["state"])

    def test_a02_each_bad_field_is_invalid_with_reason(self):
        self.assertIsNone(pa_push.validate(self.valid()))
        self.assertIsNone(pa_push.validate(self.valid(prePush=["/bin/true", "{path}"], note="n")))
        top_bad = [({"version": 1, "enabled": True, "repos": [], "extra": 1}, "extra"),
                   ({"version": 1, "repos": []}, "enabled"),
                   ({"version": 1, "enabled": "yes", "repos": []}, "enabled"),
                   ({"version": 1, "enabled": True, "repos": {}}, "repos"),
                   ({"version": 1, "enabled": True, "repos": [dict(self.entry, path="/x/{}".format(i)) for i in range(51)]}, "50"),
                   ([], "object")]
        for data, word in top_bad:
            problem = pa_push.validate(data)
            self.assertIsNotNone(problem, data)
            self.assertIn(word, problem)
        entry_bad = [({"path": "relative/path"}, "path"),
                     ({"path": 3}, "path"),
                     ({"url": "-oProxyCommand=x"}, "url"),
                     ({"url": "ftp://host/x"}, "url"),
                     ({"url": "file://relative"}, "url"),
                     ({"url": "https://host/a b"}, "url"),
                     ({"branch": "-main"}, "branch"),
                     ({"branch": "ma*in"}, "branch"),
                     ({"branch": "a..b"}, "branch"),
                     ({"branch": "x.lock"}, "branch"),
                     ({"branch": "a b"}, "branch"),
                     ({"branch": ""}, "branch"),
                     ({"branch": "@{u}"}, "branch"),
                     ({"space": "sp/1"}, "space"),
                     ({"enabled": 1}, "enabled"),
                     ({"note": "x" * 201}, "note"),
                     ({"note": 5}, "note"),
                     ({"prePush": []}, "prePush"),
                     ({"prePush": ["relative.sh"]}, "prePush"),
                     ({"prePush": ["/bin/check", ""]}, "prePush"),
                     ({"prePush": "/bin/check"}, "prePush"),
                     ({"other": 1}, "other")]
        for change, word in entry_bad:
            problem = pa_push.validate(self.valid(**change))
            self.assertIsNotNone(problem, change)
            self.assertIn(word, problem, change)
        missing = dict(self.entry)
        del missing["url"]
        self.assertIn("url", pa_push.validate({"version": 1, "enabled": True, "repos": [missing]}))
        for url in ("ssh://git.example.invalid/x", "git@example.invalid:me/x.git", "https://example.invalid/x.git", "file:///tmp/x"):
            self.assertIsNone(pa_push.validate(self.valid(url=url)), url)
        for branch in ("main", "release/1.2", "trunk"):
            self.assertIsNone(pa_push.validate(self.valid(branch=branch)), branch)

    def test_a02b_invalid_file_allows_nothing_and_show_says_why(self):
        write(self.allow, "{not json")
        data, problem, sha, _ = pa_push.load_allow()
        self.assertIsNotNone(problem)
        self.assertIsNotNone(sha)
        report = pa_push.allow_report(SPACE)
        self.assertFalse(report["valid"])
        self.assertIn("not valid", report["state"])

    def test_a03_duplicate_path_space_invalid(self):
        data = self.valid()
        data["repos"].append(dict(self.entry, path=self.main + "/"))
        self.assertIn("twice", pa_push.validate(data))
        data["repos"][1]["space"] = "sp2"
        self.assertIsNone(pa_push.validate(data))

    def test_a04_version_not_1_invalid(self):
        for v in (2, 0, "1", True, None):
            data = self.valid()
            data["version"] = v
            self.assertIn("version", pa_push.validate(data), v)

    def test_a05_prepush_placeholders_and_absolute(self):
        ok = ["/bin/check", "{path}", "{repo}", "{commit}", "{url}", "{branch}", "--x={path}"]
        self.assertIsNone(pa_push.validate(self.valid(prePush=ok)))
        self.assertIn("placeholder", pa_push.validate(self.valid(prePush=["/bin/check", "{home}"])))
        self.assertIn("prePush", pa_push.validate(self.valid(prePush=["{path}/check.sh"])))
        argv = pa_push.fill(["/bin/check", "{path}", "x{commit}"], path="/T", repo="/R", commit="c" * 40, url="file:///u", branch="main")
        self.assertEqual(argv, ["/bin/check", "/T", "x" + "c" * 40])

    def test_a06_allow_change_refuses_agents(self):
        before = snapshot(pa.ROOT)
        for extra in ({"CLAUDECODE": "1"}, {"CODEX_SANDBOX": "seatbelt"}, {"CLAUDE_CODE_SSE_PORT": "1"}):
            env = dict(os.environ, **extra)
            code, out, err = self.cli("pa-push-allow", "add", "--path", self.main, "--space", "sp2", "--url", self.url,
                                      "--branch", "main", env=env)
            self.assertEqual(code, 1, err)
            self.assertIn("the person's command", err)
        code, out, err = self.cli("pa-push-allow", "off", env=dict(os.environ, CLAUDECODE="1"))
        self.assertEqual(code, 1)
        self.assertEqual(snapshot(pa.ROOT), before)
        # in-process: an agent ancestor refuses too, and a failed ps refuses
        args = pa_push.allow_parser().parse_args(["off"])
        for tree in ([{"pid": 5, "comm": "claude", "args": "claude"}], [{"pid": 5, "comm": "node", "args": "node /x/claude-code/cli.js"}], None):
            code, _o, err = self.quiet(pa_push.allow_change, args, env=PERSON_ENV, ancestors=tree)
            self.assertEqual(code, 1)
        self.assertEqual(snapshot(pa.ROOT), before)
        # show is anyone's
        code, out, err = self.cli("pa-push-allow", "show", "--json", env=dict(os.environ, CLAUDECODE="1"))
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(out)["valid"])

    def test_a07_add_remove_on_off_write_valid_json_and_backup(self):
        os.remove(self.allow)
        parse = pa_push.allow_parser().parse_args
        code, out, _ = self.quiet(pa_push.allow_change, parse(["add", "--path", self.main, "--space", SPACE]), env=PERSON_ENV, ancestors=PERSON_TREE)
        self.assertEqual(code, 0)
        data = load(self.allow)
        self.assertIsNone(pa_push.validate(data))
        self.assertEqual(data["repos"], [{"path": self.main, "url": self.url, "branch": "main", "space": SPACE, "enabled": True}])
        self.assertIn(self.url, out)
        code, _o, err = self.quiet(pa_push.allow_change, parse(["add", "--path", self.main, "--space", SPACE]), env=PERSON_ENV, ancestors=PERSON_TREE)
        self.assertEqual(code, 1)
        self.assertIn("already", err)
        code, _o, _e = self.quiet(pa_push.allow_change, parse(["add", "--path", self.main, "--space", "sp2", "--url", "ssh://h.invalid/x",
                                                                "--branch", "trunk", "--pre-push", "/bin/true", "{path}"]),
                                  env=PERSON_ENV, ancestors=PERSON_TREE)
        self.assertEqual(code, 0)
        data = load(self.allow)
        self.assertEqual(data["repos"][1]["prePush"], ["/bin/true", "{path}"])
        self.assertEqual(data["repos"][1]["branch"], "trunk")
        self.assertTrue(os.path.exists(self.allow + ".bak-pa-push-allow"))
        self.quiet(pa_push.allow_change, parse(["off", "--path", self.main, "--space", "sp2"]), env=PERSON_ENV, ancestors=PERSON_TREE)
        self.assertFalse(load(self.allow)["repos"][1]["enabled"])
        self.quiet(pa_push.allow_change, parse(["off"]), env=PERSON_ENV, ancestors=PERSON_TREE)
        self.assertFalse(load(self.allow)["enabled"])
        self.quiet(pa_push.allow_change, parse(["on"]), env=PERSON_ENV, ancestors=PERSON_TREE)
        self.assertTrue(load(self.allow)["enabled"])
        code, _o, _e = self.quiet(pa_push.allow_change, parse(["remove", "--path", self.main, "--space", "sp2"]), env=PERSON_ENV, ancestors=PERSON_TREE)
        self.assertEqual(code, 0)
        self.assertEqual(len(load(self.allow)["repos"]), 1)
        code, _o, err = self.quiet(pa_push.allow_change, parse(["remove", "--path", self.main, "--space", "sp9"]), env=PERSON_ENV, ancestors=PERSON_TREE)
        self.assertEqual(code, 1)
        self.assertIsNone(pa_push.validate(load(self.allow)))
        backups = [n for n in os.listdir(pa.ROOT) if n.startswith("push-allow.json.bak-pa-push-allow")]
        self.assertGreaterEqual(len(backups), 4)
        # a change that would make the file invalid is refused, and the file is untouched
        before = snapshot(pa.ROOT)
        code, _o, err = self.quiet(pa_push.allow_change, parse(["add", "--path", self.main, "--space", "sp3", "--url=-x", "--branch", "main"]),
                                   env=PERSON_ENV, ancestors=PERSON_TREE)
        self.assertEqual(code, 1)
        self.assertIn("url", err)
        self.assertEqual(snapshot(pa.ROOT), before)

    def test_a08_sync_dry_run_never_lists_it(self):
        os.makedirs(os.path.join(pa.ROOT, "tools"), exist_ok=True)
        write(os.path.join(pa.ROOT, "tools", "pa.py"), "#\n")
        _v, out, _e = self.quiet(pa.cmd_sync, argparse.Namespace(on="m", dry_run=True))
        self.assertIn("tools/pa.py", out)
        self.assertNotIn("push-allow", out)

    def test_a09_install_sh_never_names_it(self):
        with open(os.path.join(REPO, "install.sh")) as f:
            text = f.read()
        self.assertNotIn("push-allow.json", text)


# ---- A2: plan and check; pa-decide ---------------------------------------------------------------

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
        return pa_push.check_card(self.dir, SPACE, card, env if env is not None else PERSON_ENV, PERSON_TREE if tree is None else tree)

    def journal(self):
        path = os.path.join(self.dir, "journal.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(l) for l in f if l.strip()]


class Plan(Planned):
    def test_p01_plan_writes_card_shape(self):
        # an earlier task's commit, merged locally and not pushed yet: it rides along, marked not from this task
        earlier = self.commit_on(self.main, "e.txt", "earlier task")
        sh(self.wt, "git", "rebase", "-q", "main")
        self.assertEqual(self.gate(), 0)
        commit = self.head(self.wt)
        code, out, err = self.plan()
        self.assertEqual(code, 0, err)
        self.assertIn(CARD, out)
        c = self.card()
        for key in ("id", "task", "kind", "question", "context", "options", "evidence", "recommendation", "status", "answer",
                    "note", "created", "answered", "push"):
            self.assertIn(key, c)
        self.assertEqual((c["id"], c["task"], c["kind"], c["status"], c["answer"], c["recommendation"]), (CARD, TASK, "push", "open", None, "Push"))
        self.assertEqual([o["label"] for o in c["options"]], ["Push", "Not now"])
        self.assertIn("No force", c["options"][0]["detail"])
        p = c["push"]
        with open(self.allow, "rb") as f:
            sha = hashlib.sha256(f.read()).hexdigest()
        gate_at = load(os.path.join(self.dir, "evidence", TASK, "GATE.json"))["at"]
        self.assertEqual(p["version"], 1)
        self.assertEqual((p["space"], p["task"], p["path"], p["url"], p["branch"]), (SPACE, TASK, self.main, self.url, "main"))
        self.assertEqual((p["taskBranch"], p["commit"], p["localBefore"], p["remoteBefore"]), ("t-1-x", commit, earlier, self.origin_head()))
        self.assertEqual((p["merge"], p["allowSha"], p["gateAt"], p["done"], p["result"], p["attempts"]), (True, sha, gate_at, False, None, []))
        self.assertTrue(p["allowMtime"].endswith("Z"))
        self.assertEqual([(x["subject"], x["fromTask"]) for x in p["commits"]], [("task work", True), ("earlier task", False)])
        self.assertTrue(all(len(x["sha"]) == 40 and "<" in x["author"] for x in p["commits"]))
        self.assertIn(self.url, c["question"])
        self.assertIn("2 commits", c["question"])
        self.assertIn("not from this task", c["context"])
        line = self.journal()[-1]
        self.assertEqual((line["trigger"], line["task"], line["decision"]), ("push-plan", TASK, CARD))
        self.assertIn(line["by"], ("agent", "person"))
        # plan pushed and merged nothing
        self.assertEqual(self.head(self.main), earlier)
        self.assertNotEqual(self.origin_head(), commit)

    def test_p02_plan_refuses_without_allowlist(self):
        os.remove(self.allow)
        code, out, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("no push allowlist", err)
        self.assertIn("main-branch pushes are off", err)
        self.assertFalse(os.listdir(os.path.join(self.dir, "decisions")))
        dump({"version": 1, "enabled": False, "repos": [self.entry]}, self.allow)
        code, _o, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("turned off", err)

    def test_p03_plan_refuses_other_space_or_url(self):
        self.set_allow(space="sp2")
        code, _o, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("names space sp2, not this space sp1", err)
        self.set_allow(space=SPACE, path=os.path.join(self.tmp, "elsewhere"))
        code, _o, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("no allowlist entry for", err)
        self.assertFalse(os.listdir(os.path.join(self.dir, "decisions")))
        # origin pointing elsewhere is a warning on the card, not a refusal (the URL comes from the allowlist)
        self.set_allow(path=self.main)
        sh(self.main, "git", "remote", "set-url", "origin", "file://" + os.path.join(self.tmp, "other.git"))
        code, _o, err = self.plan()
        self.assertEqual(code, 0, err)
        self.assertIn("origin", self.card()["context"])
        self.assertIn("other.git", self.card()["context"])
        self.assertEqual(self.card()["push"]["url"], self.url)

    def test_p04_plan_refuses_from_a_worker_worktree(self):
        dump({"task": TASK, "space": SPACE, "fix": False}, os.path.join(self.wt, ".claude", "pa-task.json"))
        os.chdir(self.wt)
        code, _o, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("workers do not open push cards", err)
        self.assertFalse(os.listdir(os.path.join(self.dir, "decisions")))

    def test_p05_second_plan_with_open_card_refused(self):
        self.assertEqual(self.plan()[0], 0)
        code, _o, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn(CARD, err)
        self.assertIn("withdraw", err)
        c = self.card()
        c["status"] = "withdrawn"
        dump(c, self.card_path())
        code, out, err = self.plan()
        self.assertEqual(code, 0, err)
        self.assertIn("d-push-t-1-2", out)

    def test_p06_plan_refuses_gate_not_review_or_failed(self):
        gate_file = os.path.join(self.dir, "evidence", TASK, "GATE.json")
        os.remove(gate_file)
        code, _o, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("GATE.json", err)
        self.gate(phase="build")
        code, _o, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("is from phase build", err)
        self.gate()
        g = load(gate_file)
        g["passed"] = False
        dump(g, gate_file)
        code, _o, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("did not pass", err)
        os.remove(os.path.join(self.dir, "evidence", TASK, "REVIEW.md"))
        code, _o, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("does not pass now", err)
        self.assertIn("REVIEW.md", err)
        self.assertFalse(os.listdir(os.path.join(self.dir, "decisions")))

    def test_p06b_plan_refuses_nothing_to_push(self):
        sh(self.main, "git", "merge", "-q", "--ff-only", "t-1-x")
        sh(self.main, "git", "push", "-q", "origin", "main")
        code, _o, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("already", err)


class Check(Planned):
    def setUp(self):
        Planned.setUp(self)
        self.assertEqual(self.plan()[0], 0)

    def test_p07_check_writes_nothing(self):
        os.remove(os.path.join(self.dir, ".push.lock"))  # made by plan; check takes no lock
        before = snapshot(pa.ROOT)
        refs = (self.head(self.main), self.origin_head(), self.head(self.main, "refs/heads/t-1-x"))
        report = self.check()
        self.assertTrue(report["ok"], report["refusal"])
        code, out, err = self.cli("pa-push-main", "check", CARD, "--space", SPACE, "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(snapshot(pa.ROOT), before)
        self.assertEqual((self.head(self.main), self.origin_head(), self.head(self.main, "refs/heads/t-1-x")), refs)
        self.assertFalse(os.path.exists(os.path.join(self.dir, ".push.lock")))

    def test_p08_check_json_contract(self):
        code, out, err = self.cli("pa-push-main", "check", CARD, "--space", SPACE, "--json")
        self.assertEqual(code, 0, err)
        r = json.loads(out)
        self.assertEqual(set(r), {"card", "ok", "refusal", "caller", "status", "done", "result", "facts", "seen"})
        self.assertEqual((r["card"], r["ok"], r["refusal"], r["status"], r["done"]), (CARD, True, None, "open", False))
        self.assertEqual(set(r["facts"]), FACT_KEYS)
        f = r["facts"]
        self.assertEqual((f["path"], f["url"], f["branch"], f["originUrl"], f["commit"], f["taskBranch"], f["branchTip"], f["merge"], f["count"]),
                         (self.main, self.url, "main", self.url, self.task_commit, "t-1-x", self.task_commit, True, 1))
        self.assertEqual((f["remoteNow"], f["localNow"]), (self.origin_head(), self.main_before))
        self.assertEqual(set(f["gate"]), {"at", "phase", "passed", "commit", "uncommitted"})
        self.assertEqual(set(f["allow"]), {"file", "sha", "mtime", "changedSinceCard", "entry"})
        self.assertFalse(f["allow"]["changedSinceCard"])
        self.assertEqual(set(f["commits"][0]), {"sha", "subject", "author", "fromTask"})
        flat = [" ".join(c) for c in f["commands"]]
        self.assertTrue(any(" merge --ff-only " in c for c in flat), flat)
        self.assertTrue(any(" push " in c and c.endswith(self.url + " " + self.task_commit + ":refs/heads/main") for c in flat), flat)
        self.assertEqual(len(r["seen"]), 64)
        self.assertEqual(set(r["caller"]), {"ok", "why", "ancestors"})
        # refused: exit 1, the first failing check named
        self.set_allow(enabled=False)
        code, out, _e = self.cli("pa-push-main", "check", CARD, "--space", SPACE, "--json")
        self.assertEqual(code, 1)
        r = json.loads(out)
        self.assertFalse(r["ok"])
        self.assertEqual(r["refusal"]["check"], "C6h")
        self.assertIsNone(r["seen"])
        code, _o, _e = self.cli("pa-push-main", "check", "d-nope", "--space", SPACE, "--json")
        self.assertEqual(code, 1)
        code, _o, _e = self.cli("pa-push-main", "check")
        self.assertEqual(code, 2)

    def test_p09_check_reports_caller(self):
        r = self.check()
        self.assertEqual(r["caller"], {"ok": True, "why": None, "ancestors": ["zsh", "launchd"]})
        r = self.check(env=dict(PERSON_ENV, CLAUDECODE="1"))
        self.assertFalse(r["caller"]["ok"])
        self.assertIn("CLAUDECODE", r["caller"]["why"])
        self.assertTrue(r["ok"])  # the caller line is shown; the facts are still checked
        r = self.check(env=dict(PERSON_ENV, HC_TERMINAL_ID="t9"))
        self.assertIn("Herdr terminal", r["caller"]["why"])
        r = pa_push.check_card(self.dir, SPACE, CARD, PERSON_ENV, None)
        self.assertIn("ps failed", r["caller"]["why"])

    def test_d01_decide_refuses_push_card(self):
        before = snapshot(self.dir)
        args = argparse.Namespace(space=SPACE, card=CARD, withdraw=False, answer="Push", quote="yes, push it", why=None)
        with self.assertRaises(pa.Problem) as e:
            pa.cmd_decide(args)
        self.assertIn("a push card is answered in the panel, not in conversation (its answer runs the push)", str(e.exception))
        c = self.card()
        del c["push"]  # recognised by kind alone too
        dump(c, self.card_path())
        with self.assertRaises(pa.Problem):
            pa.cmd_decide(args)
        c["push"], c["kind"] = {"version": 1}, "other"  # and by the push object alone
        dump(c, self.card_path())
        with self.assertRaises(pa.Problem):
            pa.cmd_decide(args)
        self.assertEqual(set(snapshot(self.dir)), set(before))

    def test_d02_decide_withdraw_works(self):
        args = argparse.Namespace(space=SPACE, card=CARD, withdraw=True, answer=None, quote=None, why="superseded by a new card")
        self.quiet(pa.cmd_decide, args)
        c = self.card()
        self.assertEqual((c["status"], c["push"]["done"]), ("withdrawn", False))
        self.assertNotEqual(self.origin_head(), self.task_commit)


if __name__ == "__main__":
    unittest.main()
