"""Unit tests for pa-worker close / keep / close --leftovers, the worktree check and its card (t-008).

Run: python3 -m unittest -v tests/test_pa_worker.py
No hc calls and no real homes: HERDR_PA_HOME, CLAUDE_CONFIG_DIR and HERDR_APP_HOME point at a temp
folder while pa.py is loaded, hc is a stub (a dict of terminals), and git runs only on repositories
made under that temp folder, with no global or system git config.
"""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(HERE, "..", "runtime", "tools")
TMP = tempfile.mkdtemp(prefix="t008-unit-")

# Load pa.py and pa_hooks.py with the temp homes, then give the environment back, so the other test
# files in the same run see what they saw before.
_HOMES = {"HERDR_PA_HOME": os.path.join(TMP, "pa"), "CLAUDE_CONFIG_DIR": os.path.join(TMP, "claude"),
          "HERDR_APP_HOME": os.path.join(TMP, "app")}
_saved_env = {k: os.environ.get(k) for k in _HOMES}
_saved_mod = sys.modules.get("pa")
os.environ.update(_HOMES)
_spec = importlib.util.spec_from_file_location("pa", os.path.join(TOOLS, "pa.py"))
pa = importlib.util.module_from_spec(_spec)
sys.modules["pa"] = pa
_spec.loader.exec_module(pa)
_spec = importlib.util.spec_from_file_location("pa_hooks", os.path.join(TOOLS, "pa_hooks.py"))
pa_hooks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pa_hooks)
if _saved_mod is None:
    del sys.modules["pa"]
else:
    sys.modules["pa"] = _saved_mod
for _k, _v in _saved_env.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v
assert pa.ROOT.startswith(TMP) and pa.CLAUDE_HOME.startswith(TMP) and pa.APP.startswith(TMP), "pa.py must use the temp homes"
assert pa_hooks.pa is pa

# While these tests run: no hc reachable, no agent markers, and git with no config but ours.
TEST_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid", "HERDR_PA_HOME": _HOMES["HERDR_PA_HOME"]}
DROP_ENV = ("HC_TERMINAL_ID", "HC_SOCKET", "HC_TERMINAL_SOCKET", "HC_SCOPE", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
            "CODEX_SANDBOX", "CODEX_HOME_SESSION")
_module_env = {}


def setUpModule():
    for k in list(TEST_ENV) + list(DROP_ENV):
        _module_env[k] = os.environ.get(k)
    for k in DROP_ENV:
        os.environ.pop(k, None)
    os.environ.update(TEST_ENV)


def tearDownModule():
    for k, v in _module_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    shutil.rmtree(TMP, ignore_errors=True)


def load(path):
    with open(path) as f:
        return json.load(f)


def dump(value, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(value, f, indent=2)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def raw(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def sh(cwd, *argv):
    r = subprocess.run(list(argv), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    if r.returncode:
        raise AssertionError("{} failed in {}: {}".format(" ".join(argv), cwd, r.stderr))
    return r.stdout.strip()


class Base(unittest.TestCase):
    """A space sp1 (Project Agent t0, profile test), its ledger and journal, a stub hc and git helpers."""

    def setUp(self):
        shutil.rmtree(pa.ROOT, ignore_errors=True)
        self.tmp = tempfile.mkdtemp(dir=TMP)
        self.dir = os.path.join(pa.ROOT, "spaces", "sp1")
        dump({"space": "sp1", "terminal": "t0", "terminals": ["t0"], "profile": "test", "name": "S"}, os.path.join(self.dir, "agent.json"))
        dump({"name": "test", "claude": ["true"], "codex": ["true"]}, os.path.join(pa.ROOT, "profiles", "test", "profile.json"))
        self.ledger({"version": 1, "tasks": []})
        write(os.path.join(self.dir, "journal.jsonl"), "")
        self.terms = {}  # address -> what `hc get <address> --json` returns
        self.calls = []  # every argv given to pa.call
        self.gets = []  # every address given to pa.hc_get
        self.saved = {k: getattr(pa, k) for k in ("hc_get", "call", "live_terminals")}
        self.saved_hooks = {k: getattr(pa_hooks, k) for k in ("by_an_agent",)}
        pa.hc_get = self.fake_get
        pa.call = self.fake_call
        pa.live_terminals = lambda: {a: dict(i) for a, i in self.terms.items() if i.get("state") == "live"}
        pa_hooks.by_an_agent = lambda: False

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(pa, k, v)
        for k, v in self.saved_hooks.items():
            setattr(pa_hooks, k, v)

    # ---- hc stub ----
    def fake_get(self, address):
        self.gets.append(address)
        return dict(self.terms.get(address) or {})

    def fake_call(self, argv, check=True):
        self.calls.append(list(argv))
        if argv[:2] == ["hc", "kill"] and argv[2] in self.terms:
            self.terms[argv[2]].update(state="finished", reason="killed", status=None)
        elif argv[:2] == ["hc", "rm"] and argv[2] in self.terms:
            if self.terms[argv[2]].get("state") == "live":
                if check:
                    raise pa.Problem("hc rm failed: {} is still running".format(argv[2]))
                return ""
            del self.terms[argv[2]]
        elif argv[:2] == ["hc", "ls"]:
            return json.dumps({"terminals": [{"address": a, "state": i.get("state")} for a, i in self.terms.items()]})
        return ""

    def term(self, address, name, status="idle", state="live", **extra):
        self.terms[address] = dict({"address": address, "name": name, "state": state, "status": status, "parent": "t0"}, **extra)

    def ended(self):
        """The hc calls that end or remove a terminal."""
        return [c for c in self.calls if c[:2] in (["hc", "kill"], ["hc", "rm"])]

    # ---- state files ----
    def ledger(self, value=None):
        path = os.path.join(self.dir, "ledger.json")
        if value is not None:
            dump(value, path)
        return load(path)

    def journal(self):
        with open(os.path.join(self.dir, "journal.jsonl")) as f:
            return [json.loads(l) for l in f if l.strip()]

    def add_task(self, tid, state="in-progress", worktree=None, workers=(), **extra):
        led = self.ledger()
        led["tasks"].append(dict({"id": tid, "title": "Title of " + tid, "state": state, "worktree": worktree,
                                  "workers": [dict(w) for w in workers], "evidence": "evidence/" + tid}, **extra))
        self.ledger(led)

    def evidence(self, tid, name="FINDINGS.md", text="report\n"):
        path = os.path.join(self.dir, "evidence", tid, name)
        write(path, text)
        return path

    def entry(self, tid, address):
        task = next(t for t in self.ledger()["tasks"] if t["id"] == tid)
        return next((w for w in task.get("workers") or [] if w.get("address") == address), None)

    def snapshot(self):
        return raw(os.path.join(self.dir, "ledger.json")), raw(os.path.join(self.dir, "journal.jsonl"))

    # ---- git ----
    def repo(self, name="proj"):
        path = os.path.join(self.tmp, name)
        os.makedirs(path)
        sh(path, "git", "init", "-q", "-b", "main")
        write(os.path.join(path, "README"), "base\n")
        sh(path, "git", "add", "README")
        sh(path, "git", "commit", "-q", "-m", "base")
        return path

    def worktree(self, repo, name):
        path = os.path.join(self.tmp, "wt-" + name)
        sh(repo, "git", "worktree", "add", "-q", "-b", name, path, "main")
        return path

    def commit(self, path, file, text, message):
        write(os.path.join(path, file), text)
        sh(path, "git", "add", file)
        sh(path, "git", "commit", "-q", "-m", message)
        return sh(path, "git", "rev-parse", "--short", "HEAD")

    # ---- running a command as its symlink name ----
    def run_cmd(self, kind, *argv):
        """(exit code, stdout, stderr) of main() invoked as `kind`, as __main__ does."""
        out, err, code = io.StringIO(), io.StringIO(), 0
        saved = sys.argv
        sys.argv = [kind] + list(argv)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    pa_hooks.main()
                except pa.Problem as e:
                    print("{}: {}".format(kind, e), file=sys.stderr)
                    code = 2
                except SystemExit as e:
                    code = e.code if isinstance(e.code, int) else (0 if e.code is None else 2)
        finally:
            sys.argv = saved
        return code, out.getvalue(), err.getvalue()

    def worker(self, *argv):
        return self.run_cmd("pa-worker", *(list(argv) + ["--space", "sp1"]))

    def assertRefused(self, result, *fragments):
        code, out, err = result
        self.assertEqual(code, 2, "expected a refusal (exit 2); stdout {!r} stderr {!r}".format(out, err))
        for f in fragments:
            self.assertIn(f, err)
        self.assertEqual(self.ended(), [], "a refusal never kills or removes a terminal")


class DeleteUnchanged(Base):
    def test_w16a_delete_refuses_inside_an_agent(self):
        self.add_task("t-003", workers=[{"address": "t31", "role": "build", "name": "t-003-tests"}])
        self.term("t31", "t-003-tests")
        pa_hooks.by_an_agent = lambda: True
        before = self.snapshot()
        self.assertRefused(self.worker("delete", "t31"), "pa-worker delete is the person's command")
        self.assertEqual(self.snapshot(), before)


class WorktreeRule(Base):
    """R4: a Delete/Keep card only once every task using the worktree is done or parked."""

    def setUp(self):
        super().setUp()
        self.proj = self.repo()
        self.wt = self.worktree(self.proj, "t-003-x")

    def check(self, *extra):
        return self.run_cmd("pa-worktree", "check", "--open-cards", "--space", "sp1", *extra)

    def card(self, task="t-003"):
        return pa.read_json(os.path.join(self.dir, "decisions", "d-wt-{}.json".format(task)))

    def test_c1_in_progress_task_with_its_worker_closed_opens_no_card(self):
        self.add_task("t-003", "in-progress", self.wt, [{"address": "t31", "role": "build", "name": "t-003-tests", "finished": True}])
        code, out, _ = self.check()
        self.assertEqual(code, 0)
        self.assertIn("t-003: tasks still t-003 in-progress; asked once every task using it is done or parked", out)
        self.assertIsNone(self.card())
        self.assertFalse(os.path.exists(os.path.join(self.dir, "decisions")) and os.listdir(os.path.join(self.dir, "decisions")))

    def test_c2_shared_worktree_waits_for_every_task(self):
        self.add_task("t-003", "done", self.wt, [{"address": "t31", "role": "build", "name": "t-003-tests"}])
        self.add_task("t-004", "review", self.wt, [{"address": "t41", "role": "build", "name": "t-004-fix"}])
        code, out, _ = self.check()
        self.assertIn("tasks still t-004 review", out)
        self.assertIsNone(self.card())
        led = self.ledger()
        led["tasks"][1]["state"] = "done"
        self.ledger(led)
        code, out, _ = self.check()
        self.assertIn("opened card d-wt-t-003", out)
        self.assertIsNotNone(self.card())

    def test_c2b_parked_counts_as_over(self):
        self.add_task("t-003", "done", self.wt, [])
        self.add_task("t-004", "parked", self.wt, [])
        self.check()
        self.assertIsNotNone(self.card())

    def test_c3_workers_that_exited_on_their_own_still_lead_to_a_card(self):
        self.add_task("t-003", "done", self.wt, [{"address": "t31", "role": "build", "name": "t-003-tests"}])
        self.term("t31", "t-003-tests", status=None, state="finished", reason="exited")
        code, out, _ = self.check()
        self.assertIn("opened card d-wt-t-003", out)
        card = self.card()
        self.assertIsNotNone(card)
        self.assertIn("t-003-tests (exited)", card["context"])

    def test_c3b_closed_worker_reads_closed_and_no_workers_recorded(self):
        self.add_task("t-003", "done", self.wt, [{"address": "t31", "role": "build", "name": "t-003-tests", "finished": True}])
        self.check()
        self.assertIn("t-003-tests (closed)", self.card()["context"])
        os.unlink(os.path.join(self.dir, "decisions", "d-wt-t-003.json"))
        led = self.ledger()
        led["tasks"][0]["workers"] = []
        self.ledger(led)
        self.check()
        self.assertIn("no workers recorded", self.card()["context"])

    def test_c3c_without_open_cards_it_lists_the_candidate(self):
        self.add_task("t-003", "done", self.wt, [{"address": "t31", "role": "build", "name": "t-003-tests", "finished": True}])
        code, out, _ = self.run_cmd("pa-worktree", "check", "--space", "sp1")
        self.assertIn("t-003: candidate", out)
        self.assertIsNone(self.card())

    def test_c3d_a_live_worker_or_other_terminal_still_blocks(self):
        self.add_task("t-003", "done", self.wt, [{"address": "t31", "role": "build", "name": "t-003-tests"}])
        self.term("t31", "t-003-tests")
        code, out, _ = self.check()
        self.assertIn("t-003: in use by t31", out)
        del self.terms["t31"]
        self.term("t77", "someone", cwd=self.wt)
        code, out, _ = self.check()
        self.assertIn("another terminal is working in it (t77)", out)
        self.assertIsNone(self.card())

    def test_c4_unchanged_branches(self):
        # keepWorktree
        self.add_task("t-003", "done", self.wt, [], keepWorktree=True)
        self.assertIn("t-003: kept (keepWorktree is set)", self.check()[1])
        self.assertIsNone(self.card())
        # the project's main checkout
        self.ledger({"version": 1, "tasks": []})
        self.add_task("t-005", "done", self.proj, [])
        self.assertIn("is the project's main checkout; never removed", self.check()[1])
        self.assertIsNone(self.card("t-005"))
        # an answered Keep is not asked again
        self.ledger({"version": 1, "tasks": []})
        self.add_task("t-003", "done", self.wt, [])
        dump({"id": "d-wt-t-003", "task": "t-003", "worktree": self.wt, "status": "answered", "answer": "Keep"},
             os.path.join(self.dir, "decisions", "d-wt-t-003.json"))
        before = raw(os.path.join(self.dir, "decisions", "d-wt-t-003.json"))
        self.assertIn("kept, not asked again", self.check()[1])
        self.assertEqual(raw(os.path.join(self.dir, "decisions", "d-wt-t-003.json")), before)
        # an open card on a reopened task is left as it is
        led = self.ledger()
        led["tasks"][0]["state"] = "queued"
        self.ledger(led)
        dump({"id": "d-wt-t-003", "task": "t-003", "worktree": self.wt, "status": "open", "answer": None, "context": "old one-line text"},
             os.path.join(self.dir, "decisions", "d-wt-t-003.json"))
        before = raw(os.path.join(self.dir, "decisions", "d-wt-t-003.json"))
        self.assertIn("card d-wt-t-003 is open", self.check()[1])
        self.assertEqual(raw(os.path.join(self.dir, "decisions", "d-wt-t-003.json")), before)

    def test_c4b_remove_still_rechecks_uncommitted_files(self):
        self.add_task("t-003", "done", self.wt, [])
        dump({"id": "d-wt-t-003", "task": "t-003", "worktree": os.path.realpath(self.wt), "status": "answered", "answer": "Delete",
              "snapshot": {"uncommitted": 0, "unpushed": 0}}, os.path.join(self.dir, "decisions", "d-wt-t-003.json"))
        write(os.path.join(self.wt, "new.txt"), "unsaved\n")
        code, _, err = self.run_cmd("pa-worktree", "remove", "t-003", "--space", "sp1")
        self.assertEqual(code, 2)
        self.assertIn("more uncommitted files now (1) than when the person answered (0)", err)
        self.assertTrue(os.path.isdir(self.wt))


if __name__ == "__main__":
    unittest.main()
