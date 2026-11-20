"""Unit tests for pa-worker close / keep / close --leftovers, the worktree check and its card (t-008).

Run: python3 -m unittest -v tests/test_pa_worker.py (all of it; tests/run-all.sh runs it in shards, tests/unit_shards.py)
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
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(HERE, "..", "runtime", "tools")
sys.path.insert(0, HERE)
from unit_shards import load_tests  # noqa: E402,F401 (tests/run-all.sh runs this module in shards, t-078)
import repo_template  # noqa: E402 (fixture repos built once and copied, t-095)
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


def build_repo(path):
    """The repo Base.repo() copies (tests/repo_template.py): main, README "base", one commit "base"."""
    sh(path, "git", "init", "-q", "-b", "main")
    write(os.path.join(path, "README"), "base\n")
    sh(path, "git", "add", "README")
    sh(path, "git", "commit", "-q", "-m", "base")


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
        """A repo of its own at <tmp>/<name>: main, README "base", one commit (a copy of build_repo's, t-095)."""
        return repo_template.copy(TMP, "repo", os.path.join(self.tmp, name), build_repo)

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
        # an open card while every task is done or parked is left as it is
        dump({"id": "d-wt-t-003", "task": "t-003", "worktree": self.wt, "status": "open", "answer": None, "context": "old one-line text"},
             os.path.join(self.dir, "decisions", "d-wt-t-003.json"))
        before = raw(os.path.join(self.dir, "decisions", "d-wt-t-003.json"))
        self.assertIn("card d-wt-t-003 is open", self.check()[1])
        self.assertEqual(raw(os.path.join(self.dir, "decisions", "d-wt-t-003.json")), before)
        # review round 1, I1: an open card on a reopened task is withdrawn (it used to be left open)
        led = self.ledger()
        led["tasks"][0]["state"] = "queued"
        self.ledger(led)
        code, out, _ = self.run_cmd("pa-worktree", "check", "--space", "sp1")
        self.assertIn("card d-wt-t-003 is open, but tasks still t-003 queued", out)
        self.assertEqual(raw(os.path.join(self.dir, "decisions", "d-wt-t-003.json")), before, "without --open-cards nothing is written")
        self.assertIn("withdrew card d-wt-t-003: tasks still t-003 queued", self.check()[1])
        card = self.card()
        self.assertEqual((card["status"], card["answer"]), ("withdrawn", None))
        self.assertIn("tasks still t-003 queued", card["note"])
        self.assertEqual(self.journal()[-1]["trigger"], "worktree")
        # once the task is done again, a fresh card opens
        led = self.ledger()
        led["tasks"][0]["state"] = "done"
        self.ledger(led)
        self.assertIn("opened card d-wt-t-003", self.check()[1])
        self.assertEqual(self.card()["status"], "open")


    def answered_delete(self):
        """A card opened by check, then answered Delete by the person."""
        self.check()
        path = os.path.join(self.dir, "decisions", "d-wt-t-003.json")
        card = load(path)
        card.update(status="answered", answer="Delete")
        dump(card, path)
        return card

    def remove(self):
        return self.run_cmd("pa-worktree", "remove", "t-003", "--space", "sp1")

    def test_c5_remove_refuses_a_reopened_task(self):
        self.add_task("t-003", "done", self.wt, [])
        self.answered_delete()
        led = self.ledger()
        led["tasks"][0]["state"] = "in-progress"
        self.ledger(led)
        self.assertIn("card d-wt-t-003 answered 'Delete', but tasks still t-003 in-progress", self.check()[1])
        code, _, err = self.remove()
        self.assertEqual(code, 2)
        self.assertIn("tasks still t-003 in-progress; a worktree is removed only once every task using it is done or parked. Nothing was removed", err)
        self.assertTrue(os.path.isdir(self.wt))

    def test_c6_remove_refuses_new_commits_since_the_card(self):
        self.add_task("t-003", "done", self.wt, [])
        self.answered_delete()
        self.commit(self.wt, "late.txt", "late\n", "a commit after the card")
        code, _, err = self.remove()
        self.assertEqual(code, 2)
        self.assertIn("has new commits since the card", err)
        self.assertTrue(os.path.isdir(self.wt))

    def test_c6b_remove_refuses_new_uncommitted_files_since_the_card(self):
        self.add_task("t-003", "done", self.wt, [])
        write(os.path.join(self.wt, "a.txt"), "seen on the card\n")
        self.answered_delete()
        os.unlink(os.path.join(self.wt, "a.txt"))
        write(os.path.join(self.wt, "b.txt"), "new since the card\n")  # same count, a different file
        code, _, err = self.remove()
        self.assertEqual(code, 2)
        self.assertIn("uncommitted files that were not on the card: b.txt", err)
        self.assertTrue(os.path.isdir(self.wt))

    def test_c6c_remove_still_works_when_nothing_changed(self):
        self.add_task("t-003", "done", self.wt, [])
        self.answered_delete()
        code, out, err = self.remove()
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.isdir(self.wt))

    # ---- review round 2 ----
    def test_c6d_a_changed_worktree_is_asked_again(self):  # P11
        self.add_task("t-003", "done", self.wt, [])
        self.answered_delete()
        self.commit(self.wt, "late.txt", "late\n", "a commit after the card")
        code, _, err = self.remove()
        self.assertEqual(code, 2)
        self.assertIn("the card is withdrawn and the next `pa-worktree check --open-cards` asks again. Nothing was removed", err)
        card = self.card()
        self.assertEqual((card["status"], card["answer"], card["previousAnswer"]), ("withdrawn", None, "Delete"))
        self.assertIn("has new commits since the card", card["note"])
        self.assertIn("withdrew card d-wt-t-003", self.journal()[-1]["why"])
        self.assertTrue(os.path.isdir(self.wt))
        self.assertIn("opened card d-wt-t-003", self.check()[1])
        self.assertEqual(self.card()["status"], "open")
        self.assertIn("a commit after the card", self.card()["context"])

    def test_c6e_more_uncommitted_files_also_ask_again(self):  # P11, the older count check
        self.add_task("t-003", "done", self.wt, [])
        card = self.answered_delete()
        card["snapshot"] = {"uncommitted": 0, "unpushed": 0}  # a card from before round 1: counts only
        dump(card, os.path.join(self.dir, "decisions", "d-wt-t-003.json"))
        write(os.path.join(self.wt, "new.txt"), "x\n")
        code, _, err = self.remove()
        self.assertEqual(code, 2)
        self.assertEqual(self.card()["status"], "withdrawn")

    def failing_status(self):
        real = shutil.which("git")
        bin_dir = os.path.join(self.tmp, "bin")
        write(os.path.join(bin_dir, "git"), '#!/bin/sh\nfor a in "$@"; do [ "$a" = status ] && {{ echo broken >&2; exit 1; }}; done\nexec "{}" "$@"\n'.format(real))
        os.chmod(os.path.join(bin_dir, "git"), 0o755)
        return bin_dir

    def test_c7_git_status_failing_is_reported_as_a_failure(self):  # P16
        self.add_task("t-003", "done", self.wt, [])
        dump({"id": "d-wt-t-003", "task": "t-003", "worktree": os.path.realpath(self.wt), "status": "answered", "answer": "Delete",
              "snapshot": {"uncommitted": 0, "unpushed": 0}}, os.path.join(self.dir, "decisions", "d-wt-t-003.json"))
        saved = os.environ["PATH"]
        os.environ["PATH"] = self.failing_status() + os.pathsep + saved
        try:
            out = self.run_cmd("pa-worktree", "check", "--space", "sp1")[1]
            code, rout, err = self.remove()
        finally:
            os.environ["PATH"] = saved
        self.assertIn("could not read {} (git status failed)".format(os.path.realpath(self.wt)), out)
        self.assertNotIn("gone or not a git worktree", out)
        self.assertEqual(code, 2)
        self.assertIn("could not read {} (git status failed); nothing was removed".format(os.path.realpath(self.wt)), err)
        self.assertNotIn("Already gone", rout)
        self.assertTrue(os.path.isdir(self.wt))

    def test_c4b_remove_still_rechecks_uncommitted_files(self):
        self.add_task("t-003", "done", self.wt, [])
        dump({"id": "d-wt-t-003", "task": "t-003", "worktree": os.path.realpath(self.wt), "status": "answered", "answer": "Delete",
              "snapshot": {"uncommitted": 0, "unpushed": 0}}, os.path.join(self.dir, "decisions", "d-wt-t-003.json"))
        write(os.path.join(self.wt, "new.txt"), "unsaved\n")
        code, _, err = self.run_cmd("pa-worktree", "remove", "t-003", "--space", "sp1")
        self.assertEqual(code, 2)
        self.assertIn("more uncommitted files now (1) than when the person answered (0)", err)
        self.assertTrue(os.path.isdir(self.wt))


class Close(Base):
    """R2: pa-worker close <address> --evidence <file>, with its guards (spec 3.2, 3.3)."""

    def setUp(self):
        super().setUp()
        self.proj = self.repo()
        self.wt = self.worktree(self.proj, "t-003-x")
        self.add_task("t-003", "in-progress", self.wt, [{"address": "t31", "role": "build", "name": "t-003-tests"}])
        self.term("t31", "t-003-tests")
        self.evidence("t-003")
        pa_hooks.by_an_agent = lambda: True  # the agent runs it

    def close(self, address="t31", *extra):
        return self.worker("close", address, *extra)

    def refused(self, result, *fragments):
        self.assertRefused(result, *fragments)

    def test_w1_success(self):
        code, out, err = self.close("t31", "--evidence", "FINDINGS.md")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ended(), [["hc", "kill", "t31"], ["hc", "rm", "t31"]])
        self.assertIn("Closed worker t31 (t-003-tests, task t-003); its worktree and branch are untouched.", out)
        w = self.entry("t-003", "t31")
        self.assertIsNotNone(w, "the entry stays in the ledger (AC2.6)")
        self.assertEqual((w["finished"], w["closedBy"], w["report"], w["role"], w["name"]), (True, "agent", "FINDINGS.md", "build", "t-003-tests"))
        self.assertRegex(w["closed"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        task = self.ledger()["tasks"][0]
        self.assertEqual(task["state"], "in-progress")
        self.assertTrue(task.get("updated"))
        lines = self.journal()
        self.assertEqual(len(lines), 1)
        self.assertEqual((lines[0]["trigger"], lines[0]["by"], lines[0]["task"]), ("close", "agent", "t-003"))
        self.assertIn("closed worker t31 (t-003-tests) after reading its report FINDINGS.md", lines[0]["why"])
        self.assertTrue(os.path.isdir(self.wt))
        self.assertTrue(sh(self.proj, "git", "rev-parse", "--verify", "refs/heads/t-003-x"))
        self.assertNotIn("t31", self.terms)

    def test_w1b_the_gate_still_sees_closed_workers(self):
        """AC1.4: a closed build worker and a closed reviewer still satisfy reviewer-is-not-the-author."""
        led = self.ledger()
        led["tasks"][0].update(type="docs", worktree=None, workers=[
            {"address": "t31", "role": "build", "name": "t-003-tests"}, {"address": "t32", "role": "reviewer", "name": "t-003-review"}])
        self.ledger(led)
        self.term("t32", "t-003-review")
        self.evidence("t-003", "REVIEW.md")
        self.assertEqual(self.close("t31", "--evidence", "FINDINGS.md")[0], 0)
        self.assertEqual(self.close("t32", "--evidence", "REVIEW.md")[0], 0)
        for name in ("intent.md", "spec.md", "plan.md", "DECISIONS.md", "VERIFY.md"):
            self.evidence("t-003", name)
        code, out, _ = self.run_cmd("pa-gate", "t-003", "--space", "sp1")
        self.assertEqual(code, 0, out)
        self.assertIn("ok    reviewer is not the author", out)

    def test_w1c_absolute_evidence_path_inside_the_folder(self):
        path = self.evidence("t-003", "sub/REPORT.md")
        self.assertEqual(self.close("t31", "--evidence", path)[0], 0)
        self.assertEqual(self.entry("t-003", "t31")["report"], os.path.join("sub", "REPORT.md"))

    def test_w1d_closed_by_the_person(self):
        pa_hooks.by_an_agent = lambda: False
        self.assertEqual(self.close("t31", "--evidence", "FINDINGS.md")[0], 0)
        self.assertEqual(self.entry("t-003", "t31")["closedBy"], "person")
        self.assertEqual(self.journal()[0]["by"], "person")

    def test_w2_working(self):
        self.terms["t31"]["status"] = "working"
        before = self.snapshot()
        self.refused(self.close("t31", "--evidence", "FINDINGS.md"), "t31 (t-003-tests) is working; close it after its report")
        self.assertEqual(self.snapshot(), before)

    def test_w3_blocked(self):
        self.terms["t31"]["status"] = "blocked"
        before = self.snapshot()
        self.refused(self.close("t31", "--evidence", "FINDINGS.md"), "t31 (t-003-tests) is blocked on a question or a permission; bring that to the person")
        self.assertEqual(self.snapshot(), before)

    def test_w3b_unknown_status(self):
        self.terms["t31"]["status"] = None  # live, but hc reports no agent status
        before = self.snapshot()
        self.refused(self.close("t31", "--evidence", "FINDINGS.md"), "t31 has status none; nothing was closed")
        self.assertEqual(self.snapshot(), before)

    def test_w4_the_project_agent(self):
        self.term("t0", "PA: S")
        before = self.snapshot()
        self.refused(self.close("t0", "--evidence", "FINDINGS.md"), "that is the Project Agent itself; it ends when its space is closed")
        self.assertEqual(self.snapshot(), before)

    def test_w5_unrecorded(self):
        self.term("t99", "someone-else")
        before = self.snapshot()
        self.refused(self.close("t99", "--evidence", "FINDINGS.md"), "t99 is not a recorded worker of this space; nothing was closed (if it is yours, close it in Herdr)")
        self.assertEqual(self.snapshot(), before)
        self.assertIn("t99", self.terms)

    def test_w6_marked_keep(self):
        led = self.ledger()
        led["tasks"][0]["workers"][0].update(keep=True, keptAt="2026-11-16T10:00:00Z", keepQuote="leave the tests one up")
        self.ledger(led)
        before = self.snapshot()
        self.refused(self.close("t31", "--evidence", "FINDINGS.md"),
                     't31 (t-003-tests) is marked keep (2026-11-16T10:00:00Z: "leave the tests one up"); only the person closes it, with the × in the panel')
        self.assertEqual(self.snapshot(), before)

    def test_w7_evidence(self):
        before = self.snapshot()
        self.refused(self.close("t31"), "pa-worker close needs --evidence <file>: the report you read, in evidence/t-003/")
        self.refused(self.close("t31", "--evidence", "NOPE.md"), "evidence NOPE.md is missing")
        self.evidence("t-003", "EMPTY.md", "  \n\t\n")
        self.refused(self.close("t31", "--evidence", "EMPTY.md"), "evidence EMPTY.md is empty; read the report before closing")
        self.evidence("t-002", "x.md")
        self.refused(self.close("t31", "--evidence", "../t-002/x.md"), "../t-002/x.md is not in t31's task evidence folder evidence/t-003/")
        elsewhere = os.path.join(self.tmp, "elsewhere.md")
        write(elsewhere, "text\n")
        self.refused(self.close("t31", "--evidence", elsewhere), "is not in t31's task evidence folder evidence/t-003/")
        os.symlink(elsewhere, os.path.join(self.dir, "evidence", "t-003", "link.md"))
        self.refused(self.close("t31", "--evidence", "link.md"), "link.md is not in t31's task evidence folder")
        self.assertEqual(self.snapshot(), before)

    def test_w8_uncommitted(self):
        write(os.path.join(self.wt, "README"), "changed\n")
        for i in range(6):
            write(os.path.join(self.wt, "new{}.txt".format(i)), "x\n")
        before = self.snapshot()
        self.refused(self.close("t31", "--evidence", "FINDINGS.md"), "has 7 uncommitted file(s) (", "README", ", +2 more)",
                     "have them committed or reported first. Nothing was closed")
        self.assertEqual(self.snapshot(), before)

    def test_w8b_our_own_settings_file_does_not_count(self):
        write(os.path.join(self.wt, ".claude", "settings.local.json"), "{}\n")
        self.assertEqual(self.close("t31", "--evidence", "FINDINGS.md")[0], 0)

    def test_w8c_main_checkout_is_checked_too(self):
        led = self.ledger()
        led["tasks"][0]["worktree"] = self.proj
        self.ledger(led)
        write(os.path.join(self.proj, "mine.txt"), "the person's\n")
        before = self.snapshot()
        self.refused(self.close("t31", "--evidence", "FINDINGS.md"), "has 1 uncommitted file(s) (mine.txt)")
        self.assertEqual(self.snapshot(), before)

    def profile_kind(self, status_code=0, output='{"uncommitted": 0, "unpushed": 0, "branch": "b", "kind": "custom worktree"}'):
        folder = os.path.join(pa.ROOT, "profiles", "test2")
        status = os.path.join(self.tmp, "status.sh")
        write(status, "#!/bin/sh\nprintf '%s\\n' '{}'\nexit {}\n".format(output, status_code))
        os.chmod(status, 0o755)
        dump({"name": "test2", "claude": ["true"], "codex": ["true"],
              "worktree": {"marker": "marker.txt", "status": status, "remove": status}}, os.path.join(folder, "profile.json"))
        agent = load(os.path.join(self.dir, "agent.json"))
        agent["profile"] = "test2"
        dump(agent, os.path.join(self.dir, "agent.json"))
        kind = os.path.join(self.tmp, "kind-wt")
        write(os.path.join(kind, "marker.txt"), "")
        led = self.ledger()
        led["tasks"][0]["worktree"] = kind
        self.ledger(led)
        return kind

    def test_w9_unreadable_worktree(self):
        self.profile_kind(status_code=1)
        before = self.snapshot()
        self.refused(self.close("t31", "--evidence", "FINDINGS.md"), "cannot tell whether", "holds uncommitted work (", "); nothing was closed")
        self.assertEqual(self.snapshot(), before)

    def test_w9b_profile_kind_with_uncommitted_files(self):
        self.profile_kind(output='{"uncommitted": 3, "unpushed": 0, "branch": "b"}')
        self.refused(self.close("t31", "--evidence", "FINDINGS.md"), "has 3 uncommitted file(s)", "Nothing was closed")

    def test_w9c_profile_kind_clean(self):
        self.profile_kind()
        self.assertEqual(self.close("t31", "--evidence", "FINDINGS.md")[0], 0)

    def test_w9d_a_folder_that_is_not_a_worktree(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        led = self.ledger()
        led["tasks"][0]["worktree"] = plain
        self.ledger(led)
        self.refused(self.close("t31", "--evidence", "FINDINGS.md"), "cannot tell whether {} holds uncommitted work".format(os.path.realpath(plain)))

    def test_w9e_a_worktree_that_is_gone_is_fine(self):
        led = self.ledger()
        led["tasks"][0]["worktree"] = os.path.join(self.tmp, "gone")
        self.ledger(led)
        self.assertEqual(self.close("t31", "--evidence", "FINDINGS.md")[0], 0)

    def test_w10_unpushed_commits_do_not_block(self):
        self.commit(self.wt, "a.txt", "a\n", "a commit on no remote")
        code, _, err = self.close("t31", "--evidence", "FINDINGS.md")
        self.assertEqual(code, 0, err)
        self.assertTrue(self.entry("t-003", "t31")["finished"])

    def test_w11_status_reread_under_the_lock(self):
        real = self.fake_get
        seen = []

        def flipping(address):
            if address == "t31":
                seen.append(address)
                if len(seen) > 1:
                    self.terms["t31"]["status"] = "working"
            return real(address)
        pa.hc_get = flipping
        before = self.snapshot()
        self.refused(self.close("t31", "--evidence", "FINDINGS.md"), "t31 (t-003-tests) is working")
        self.assertGreaterEqual(len(seen), 2, "the status is read again before the kill")
        self.assertEqual(self.snapshot(), before)

    def test_w12_already_closed(self):
        led = self.ledger()
        led["tasks"][0]["workers"][0].update(finished=True, closed="2026-11-16T10:00:00Z")
        self.ledger(led)
        del self.terms["t31"]
        before = self.snapshot()
        code, out, _ = self.close("t31")
        self.assertEqual(code, 0)
        self.assertIn("t31 is already closed.", out)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.ended(), [])

    def test_w13_gone_but_not_finished(self):
        del self.terms["t31"]
        code, out, err = self.close("t31", "--evidence", "FINDINGS.md")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ended(), [], "no hc call for a terminal that is already gone")
        self.assertTrue(self.entry("t-003", "t31")["finished"])
        self.assertEqual(self.journal()[0]["trigger"], "close")

    def test_w13b_exited_on_its_own(self):
        self.terms["t31"].update(state="finished", status=None, reason="exited")
        self.assertEqual(self.close("t31", "--evidence", "FINDINGS.md")[0], 0)
        self.assertEqual(self.ended(), [["hc", "rm", "t31"]])

    def test_w13c_rm_that_leaves_the_record(self):
        real = self.fake_call

        def stubborn(argv, check=True):
            if argv[:2] == ["hc", "rm"]:
                self.calls.append(list(argv))
                return ""
            return real(argv, check)
        pa.call = stubborn
        code, out, _ = self.close("t31", "--evidence", "FINDINGS.md")
        self.assertEqual(code, 0)
        self.assertIn("ended t31 but hc rm did not remove its record", out)
        self.assertTrue(self.entry("t-003", "t31")["finished"])

    def test_w14_waits_for_the_ledger_lock(self):
        import fcntl
        held, released = threading.Event(), []

        def hold():
            with open(os.path.join(self.dir, ".ledger.lock"), "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                held.set()
                time.sleep(1.0)
                released.append(time.monotonic())  # the lock goes when the file closes, just after this
        t = threading.Thread(target=hold)
        t.start()
        self.assertTrue(held.wait(5))
        code, _, err = self.close("t31", "--evidence", "FINDINGS.md")
        ended = time.monotonic()
        t.join()
        self.assertEqual(code, 0, err)
        # Order of events, not a time window: a close that waits for the lock can only end after it was
        # released, however slow the machine is.
        self.assertTrue(released and ended >= released[0], (released, ended))
        self.assertTrue(self.entry("t-003", "t31")["finished"])

    def test_w15_other_machines(self):
        self.term("m1/t5", "x")
        self.refused(self.close("m1/t5", "--evidence", "FINDINGS.md"), "m1/t5 is on another machine; pa-worker close runs on the machine the space runs on")
        result = self.run_cmd("pa-worker", "close", "t31", "--evidence", "FINDINGS.md", "--space", "m1/sp9")
        self.refused(result, "space m1/sp9 lives on m1; run pa-worker there (its Project Agent does)")

    def test_w16b_close_is_allowed_inside_an_agent(self):
        pa_hooks.by_an_agent = lambda: True
        self.assertEqual(self.close("t31", "--evidence", "FINDINGS.md")[0], 0)

    def test_w17_address_now_names_another_terminal(self):
        self.terms["t31"]["name"] = "other"
        before = self.snapshot()
        self.refused(self.close("t31", "--evidence", "FINDINGS.md"), "t31 is now a different terminal (other), not t-003-tests; nothing was closed")
        self.assertEqual(self.snapshot(), before)

    def test_w18_no_recorded_name(self):
        led = self.ledger()
        del led["tasks"][0]["workers"][0]["name"]
        self.ledger(led)
        self.terms["t31"]["name"] = "zzz"
        self.refused(self.close("t31", "--evidence", "FINDINGS.md"), "t31 is now a different terminal (zzz)", "nothing was closed")
        self.terms["t31"]["name"] = "t-003-fix"  # a name for the task: it is the worker
        self.assertEqual(self.close("t31", "--evidence", "FINDINGS.md")[0], 0)

    def test_w19_usage(self):
        code, _, err = self.worker("close")
        self.assertEqual(code, 2)
        self.assertIn("pa-worker close needs an address and --evidence, or --leftovers", err)
        code, _, err = self.worker("close", "t31", "--leftovers")
        self.assertIn("pa-worker close needs an address and --evidence, or --leftovers", err)
        for action in ("info", "delete", "keep"):
            code, _, err = self.worker(action)
            self.assertEqual(code, 2, action)
        self.assertEqual(self.ended(), [])

    # ---- review round 1 ----
    def test_w20_evidence_must_be_this_workers(self):  # I4
        before = self.snapshot()
        self.evidence("t-003", "intent.md", "the orchestrator's intent\n")
        self.refused(self.close("t31", "--evidence", "intent.md"), "intent.md is the orchestrator's own file, not a worker's report")
        for name in ("GATE.json", "WORKTREE.md"):
            self.evidence("t-003", name, "{}\n")
            self.refused(self.close("t31", "--evidence", name), "{} is written by the Project Agent's tools, not a worker's report".format(name))
        old = self.evidence("t-003", "OLD.md", "an earlier worker's report\n")
        os.utime(old, (time.time() - 7200, time.time() - 7200))
        self.terms["t31"]["created_ms"] = int((time.time() - 3600) * 1000)
        self.refused(self.close("t31", "--evidence", "OLD.md"), "OLD.md was last written before t31 started", "name the file that holds this worker's report")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.close("t31", "--evidence", "FINDINGS.md")[0], 0)  # written after the start

    def test_w20b_recorded_start_in_the_ledger(self):  # I4
        led = self.ledger()
        led["tasks"][0]["workers"][0]["started"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
        self.ledger(led)
        self.refused(self.close("t31", "--evidence", "FINDINGS.md"), "FINDINGS.md was last written before t31 started")

    def test_w21_hc_failure_is_not_gone(self):  # M1
        real = self.fake_get
        pa.hc_get = lambda a: {} if a == "t31" else real(a)  # hc get fails for a terminal that runs
        self.terms["t31"]["status"] = "working"
        before = self.snapshot()
        self.refused(self.close("t31", "--evidence", "FINDINGS.md"), "cannot read t31's state from hc", "nothing was closed")
        real_call = self.fake_call

        def no_hc(argv, check=True):
            if argv[:2] == ["hc", "ls"]:
                raise pa.Problem("hc ls failed: no core")
            return real_call(argv, check)
        pa.call = no_hc
        self.refused(self.close("t31", "--evidence", "FINDINGS.md"), "cannot read t31's state from hc")
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.entry("t-003", "t31").get("finished"))

    def test_w13d_a_kill_that_fails_records_nothing(self):  # M2
        real = self.fake_call

        def deaf(argv, check=True):
            if argv[:2] == ["hc", "kill"]:
                self.calls.append(list(argv))
                return ""
            return real(argv, check)
        pa.call = deaf
        before = self.snapshot()
        code, out, err = self.close("t31", "--evidence", "FINDINGS.md")
        self.assertEqual(code, 2)
        self.assertIn("hc kill did not end t31 (it is still live); nothing was recorded", err)
        self.assertEqual(self.snapshot(), before)

    def test_w8d_untracked_files_whatever_the_repo_config(self):  # M3
        sh(self.wt, "git", "config", "status.showUntrackedFiles", "no")
        write(os.path.join(self.wt, "unsaved.py"), "x = 1\n")
        self.refused(self.close("t31", "--evidence", "FINDINGS.md"), "has 1 uncommitted file(s) (unsaved.py)")
        os.unlink(os.path.join(self.wt, "unsaved.py"))
        write(os.path.join(self.wt, ".claude", "skills", "mine.md"), "real work\n")
        self.refused(self.close("t31", "--evidence", "FINDINGS.md"), ".claude/skills/")

    def test_w8e_no_worktree_checks_where_the_worker_ran(self):  # M3
        led = self.ledger()
        led["tasks"][0]["worktree"] = None
        self.ledger(led)
        write(os.path.join(self.proj, "left.txt"), "unsaved\n")
        self.terms["t31"]["cwd"] = os.path.join(self.proj)
        self.refused(self.close("t31", "--evidence", "FINDINGS.md"), "has 1 uncommitted file(s) (left.txt)")
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        self.terms["t31"]["cwd"] = plain
        code, out, err = self.close("t31", "--evidence", "FINDINGS.md")
        self.assertEqual(code, 0, err)
        self.assertIn("not seen: {} is not in a git checkout".format(os.path.realpath(plain)), out)

    def test_w8f_success_says_what_it_checked(self):  # M3
        code, out, _ = self.close("t31", "--evidence", "FINDINGS.md")
        self.assertEqual(code, 0)
        self.assertIn("Checked for uncommitted work: {}".format(os.path.realpath(self.wt)), out)
        self.assertIn("ignored files are not seen", out)

    def test_w9f_profile_kind_repos_are_checked_one_by_one(self):  # M3
        kind = self.profile_kind()  # its script says 0 uncommitted
        repo = os.path.join(kind, "src", "A")
        os.makedirs(repo)
        sh(repo, "git", "init", "-q", "-b", "main")
        self.commit(repo, "README", "x\n", "base")
        write(os.path.join(repo, "dirty.txt"), "unsaved\n")
        self.profile_kind(output=json.dumps({"uncommitted": 0, "unpushed": 0, "branch": "b", "repos": [repo]}))
        self.refused(self.close("t31", "--evidence", "FINDINGS.md"), "dirty.txt")

    def test_w22_two_unfinished_entries_for_one_address(self):  # M10
        led = self.ledger()
        led["tasks"].insert(0, {"id": "t-002", "title": "old", "state": "done", "worktree": None,
                                "workers": [{"address": "t31", "role": "build", "name": "t-002-build"}], "evidence": "evidence/t-002"})
        self.ledger(led)
        code, out, err = self.close("t31", "--evidence", "FINDINGS.md")
        self.assertEqual(code, 0, err)
        self.assertTrue(self.entry("t-003", "t31")["finished"])
        self.assertFalse(self.entry("t-002", "t31").get("finished"))

    def test_w11b_closed_meanwhile(self):  # M11: the "already closed" branch under the lock
        import fcntl
        held = threading.Event()

        def other_close():  # holds the lock, and while this close waits, finishes the worker
            with open(os.path.join(self.dir, ".ledger.lock"), "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                held.set()
                time.sleep(0.4)
                led = self.ledger()
                led["tasks"][0]["workers"][0].update(finished=True, closed="2026-11-16T10:00:00Z")
                self.ledger(led)
                del self.terms["t31"]
        t = threading.Thread(target=other_close)
        t.start()
        held.wait(5)
        code, out, _ = self.close("t31", "--evidence", "FINDINGS.md")
        t.join()
        self.assertEqual(code, 0)
        self.assertIn("t31 is already closed.", out)
        self.assertEqual(self.ended(), [])
        self.assertEqual(self.journal(), [])

    # ---- the verifier's surviving mutants (VERIFY.md section 4) ----
    def while_waiting(self, change):
        """Run a close while another holder of the ledger lock makes `change` (then lets go)."""
        import fcntl
        held = threading.Event()

        def other():
            with open(os.path.join(self.dir, ".ledger.lock"), "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                held.set()
                time.sleep(0.4)
                change()
        t = threading.Thread(target=other)
        t.start()
        held.wait(5)
        result = self.close("t31", "--evidence", "FINDINGS.md")
        t.join()
        return result

    def test_w23_ledger_reread_under_the_lock(self):  # reread-ledger-lock: no lost update
        def other_writer():
            led = self.ledger()
            led["tasks"][0]["state"] = "review"
            led["tasks"].append({"id": "t-009", "title": "added meanwhile", "state": "queued", "workers": []})
            self.ledger(led)
        code, _, err = self.while_waiting(other_writer)
        self.assertEqual(code, 0, err)
        led = self.ledger()
        self.assertEqual(led["tasks"][0]["state"], "review", "the other writer's change survives the close")
        self.assertEqual(led["tasks"][1]["id"], "t-009")
        self.assertTrue(self.entry("t-003", "t31")["finished"])

    def test_w24_keep_set_while_waiting(self):  # identity-under-lock (keep)
        def keep():
            led = self.ledger()
            led["tasks"][0]["workers"][0].update(keep=True, keptAt="2026-11-16T10:00:00Z", keepQuote="keep it")
            self.ledger(led)
        before_journal = raw(os.path.join(self.dir, "journal.jsonl"))
        self.refused(self.while_waiting(keep), "is marked keep")
        self.assertFalse(self.entry("t-003", "t31").get("finished"))
        self.assertEqual(raw(os.path.join(self.dir, "journal.jsonl")), before_journal)

    def test_w24b_address_taken_by_another_terminal_while_waiting(self):  # identity-under-lock (name)
        def reused():
            self.terms["t31"]["name"] = "someone-else"
        self.refused(self.while_waiting(reused), "t31 is now a different terminal (someone-else)")
        self.assertFalse(self.entry("t-003", "t31").get("finished"))

    def test_w25_entry_removed_while_waiting(self):  # notrec-lock
        def removed():
            led = self.ledger()
            led["tasks"][0]["workers"] = []
            self.ledger(led)
        self.refused(self.while_waiting(removed), "t31 is not a recorded worker of this space")
        self.assertEqual(self.ledger()["tasks"][0]["workers"], [])

    def test_w26_git_status_failing_is_not_clean(self):  # unreadable-gitfail
        real = shutil.which("git")
        bin_dir = os.path.join(self.tmp, "bin")
        write(os.path.join(bin_dir, "git"), '#!/bin/sh\nfor a in "$@"; do [ "$a" = status ] && {{ echo broken >&2; exit 1; }}; done\nexec "{}" "$@"\n'.format(real))
        os.chmod(os.path.join(bin_dir, "git"), 0o755)
        saved = os.environ["PATH"]
        os.environ["PATH"] = bin_dir + os.pathsep + saved
        try:
            result = self.close("t31", "--evidence", "FINDINGS.md")
        finally:
            os.environ["PATH"] = saved
        self.refused(result, "cannot tell whether {} holds uncommitted work".format(os.path.realpath(self.wt)))

    def test_w20c_the_later_start_wins(self):  # P14: an early `started` in the ledger cannot make an old file count
        old = self.evidence("t-003", "OLD.md", "an earlier worker's report\n")
        os.utime(old, (time.time() - 7200, time.time() - 7200))
        led = self.ledger()
        led["tasks"][0]["workers"][0]["started"] = "2020-01-01T00:00:00Z"
        self.ledger(led)
        self.terms["t31"]["created_ms"] = int((time.time() - 3600) * 1000)
        self.refused(self.close("t31", "--evidence", "OLD.md"), "OLD.md was last written before t31 started")


class Keep(Base):
    """R3: pa-worker keep <address> --quote "<words>" [--off]; close and pa-cleanup honour it."""

    def setUp(self):
        super().setUp()
        self.add_task("t-003", "in-progress", None, [{"address": "t31", "role": "build", "name": "t-003-tests"}])
        self.term("t31", "t-003-tests")
        self.evidence("t-003")
        pa_hooks.by_an_agent = lambda: True

    def test_k1_keep_and_off(self):
        code, out, err = self.worker("keep", "t31", "--quote", "keep t31, I want to ask it something")
        self.assertEqual(code, 0, err)
        w = self.entry("t-003", "t31")
        self.assertEqual((w["keep"], w["keepQuote"]), (True, "keep t31, I want to ask it something"))
        self.assertRegex(w["keptAt"], r"^\d{4}-")
        line = self.journal()[-1]
        self.assertEqual((line["trigger"], line["task"]), ("keep", "t-003"))
        self.assertIn('the person asked to keep t31 open: "keep t31, I want to ask it something"', line["why"])
        self.assertEqual(self.ended(), [])
        self.assertRefused(self.worker("close", "t31", "--evidence", "FINDINGS.md"), "is marked keep")
        # review round 1, I3: --off needs the person's words too, stored and journaled as given
        self.assertRefused(self.worker("keep", "t31", "--off"), 'pa-worker keep --off needs --quote "<the person\'s own words>"')
        self.assertTrue(self.entry("t-003", "t31")["keep"])
        code, out, err = self.worker("keep", "t31", "--off", "--quote", "ok, t31 can go")
        self.assertEqual(code, 0, err)
        w = self.entry("t-003", "t31")
        self.assertEqual((w["keep"], w["keepQuote"], w["unkeepQuote"]), (False, "keep t31, I want to ask it something", "ok, t31 can go"))
        self.assertRegex(w["unkeptAt"], r"^\d{4}-")
        self.assertIn('the person said t31 may be closed again: "ok, t31 can go"', self.journal()[-1]["why"])
        self.assertFalse(any("at the person's word" in l["why"] for l in self.journal()))
        self.assertEqual(self.worker("close", "t31", "--evidence", "FINDINGS.md")[0], 0)

    def test_k1b_refusals(self):
        before = self.snapshot()
        for quote in ([], ["--quote", "  "]):
            self.assertRefused(self.worker("keep", "t31", *quote), 'pa-worker keep needs --quote "<the person\'s own words>"')
        self.assertRefused(self.worker("keep", "t99", "--quote", "x"), "t99 is not a recorded worker of this space")
        self.assertRefused(self.worker("keep", "t0", "--quote", "x"), "that is the Project Agent itself")
        self.assertRefused(self.worker("keep", "m1/t31", "--quote", "x"), "m1/t31 is on another machine")
        self.assertEqual(self.snapshot(), before)
        code, out, _ = self.worker("keep", "t31", "--off", "--quote", "it can go")
        self.assertEqual(code, 0)
        self.assertIn("t31 is not marked keep", out)
        self.assertEqual(self.snapshot(), before)

    def test_k1c_keep_on_a_closed_worker(self):
        self.assertEqual(self.worker("close", "t31", "--evidence", "FINDINGS.md")[0], 0)
        calls = len(self.calls)
        code, _, err = self.worker("keep", "t31", "--quote", "keep that one")
        self.assertEqual(code, 0, err)
        w = self.entry("t-003", "t31")
        self.assertTrue(w["keep"] and w["finished"])
        self.assertEqual([c for c in self.calls[calls:] if c[:2] != ["hc", "get"]], [], "keep reopens nothing")

    def test_k1d_info_shows_keep_finished_closed(self):
        self.worker("keep", "t31", "--quote", "keep it")
        code, out, _ = self.worker("info", "t31")
        facts = json.loads(out)
        self.assertEqual((facts["keep"], facts["finished"], facts["closed"]), (True, False, None))

    def test_k2_cleanup_skips_kept_workers(self):
        led = self.ledger()
        led["tasks"][0]["state"] = "done"
        led["tasks"][0]["workers"].append({"address": "t32", "role": "verifier", "name": "t-003-verify"})
        led["tasks"][0]["workers"][0].update(keep=True, keptAt="2026-11-16T10:00:00Z", keepQuote="keep t31")
        self.ledger(led)
        self.term("t32", "t-003-verify")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            pa.cmd_cleanup(argparse.Namespace(space="sp1", kill=True, dry_run=False))
        self.assertIn("kept t31 t-003-tests (t-003): marked keep", out.getvalue())
        self.assertNotIn(["hc", "kill", "t31"], self.calls)
        self.assertNotIn(["hc", "rm", "t31"], self.calls)
        self.assertIn(["hc", "kill", "t32"], self.calls, "a worker that is not kept is still cleaned up")


class Leftovers(Base):
    """R5 as changed by the person on card d-031: the one-time sweep only lists the old-style workers on
    one decision card; it closes nothing. Once the person answers it in the panel, `pa-worker close --card`
    closes the approved ones through the normal guards. No journal text decides anything."""

    def setUp(self):
        super().setUp()
        pa_hooks.by_an_agent = lambda: True
        self.t0 = time.time()

    def add(self, tid, address, name, status="idle", worktree=None, idle_s=3 * 3600, role="build", **entry):
        led = self.ledger()
        task = next((t for t in led["tasks"] if t["id"] == tid), None)
        if task is None:
            self.add_task(tid, "in-progress", worktree, [])
            led = self.ledger()
            task = next(t for t in led["tasks"] if t["id"] == tid)
        task["workers"].append(dict({"address": address, "role": role, "name": name}, **entry))
        self.ledger(led)
        self.term(address, name, status=status, status_ms=int((self.t0 - idle_s) * 1000))

    def note(self, **line):
        with open(os.path.join(self.dir, "journal.jsonl"), "a") as f:
            f.write(json.dumps(dict({"at": "2026-11-15T10:00:00Z"}, **line)) + "\n")

    def sweep(self, *extra):
        return self.worker("close", "--leftovers", *extra)

    def card(self):
        return pa.read_json(os.path.join(self.dir, "decisions", "d-leftovers.json"))

    def answer(self, label, note=None, by=None):
        path = os.path.join(self.dir, "decisions", "d-leftovers.json")
        card = load(path)
        card.update(status="answered", answer=label, note=note, answered=pa.now())
        if by:
            card["answeredBy"] = by
        dump(card, path)

    def mix(self):
        proj = self.repo()
        dirty = self.worktree(proj, "t-004-x")
        write(os.path.join(dirty, "README"), "changed\n")
        self.add("t-001", "t31", "t-001-build")
        self.note(trigger="report", task="t-001", why="t31 has not reported yet")  # quoted, never read as a report
        self.add("t-002", "t34", "t-002-build", status="working")
        self.add("t-003", "t36", "t-003-build", status="blocked")
        self.add("t-004", "t37", "t-004-build", worktree=dirty)
        self.add("t-005", "t40", "t-005-build", keep=True, keptAt="2026-11-16T09:00:00Z", keepQuote="keep t40")
        self.add("t-006", "t41", "t-006-build", finished=True)
        self.add("t-007", "t42", "t-007-review", role="reviewer")
        self.terms["t42"]["name"] = "someone-else"  # the address now names another terminal
        self.add("t-008", "t43", "t-008-old")
        del self.terms["t43"]  # gone
        self.add("t-009", "t0", "PA: S")
        self.term("t77", "t-001-build")  # not in the ledger
        return os.path.realpath(dirty)

    def test_n1_the_sweep_lists_on_one_card_and_closes_nothing(self):
        dirty = self.mix()
        before_entries = json.dumps(self.ledger()["tasks"])
        code, out, err = self.sweep()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ended(), [], "the sweep closes nothing")
        self.assertEqual(json.dumps(self.ledger()["tasks"]), before_entries, "no worker entry changes")
        card = self.card()
        self.assertEqual((card["id"], card["kind"], card["status"], card["answer"], card["recommendation"]),
                         ("d-leftovers", "leftovers", "open", None, None))
        self.assertEqual([o["label"] for o in card["options"]], ["Close all listed", "Close none"])
        listed = {w["address"]: w for w in card["workers"]}
        self.assertEqual(sorted(listed), ["t31", "t37", "t43"])
        w = listed["t31"]
        self.assertEqual((w["name"], w["task"], w["role"], w["state"], w["uncommitted"]), ("t-001-build", "t-001", "build", "idle", 0))
        self.assertEqual(w["idle"], "3 h")
        self.assertEqual(w["lastLine"]["why"], "t31 has not reported yet")
        self.assertEqual((listed["t37"]["uncommitted"], listed["t37"]["uncommittedFiles"], listed["t37"]["worktree"]), (1, ["README"], dirty))
        self.assertEqual(listed["t43"]["state"], "gone")
        held = {n["address"]: n["why"] for n in card["notListed"]}
        self.assertEqual(held, {"t34": "working", "t36": "blocked", "t40": "marked keep",
                                "t42": "the address now names another terminal (someone-else)"})
        for line in ("t31 t-001-build (build, t-001", 'last journal line naming it (quoted, not read): "t31 has not reported yet"',
                     "uncommitted: 1 in {} (README)".format(dirty), "t43 t-008-old (build, t-008"):
            self.assertIn(line, card["context"])
        self.assertIn("listed t31 (t-001-build, t-001)", out)
        self.assertIn("not listed t34 (t-002-build, t-002): working", out)
        self.assertIn("opened card d-leftovers", out)
        policy = self.ledger()["workerPolicy"]
        self.assertEqual((policy["style"], policy["card"]), ("per-job", "d-leftovers"))
        self.assertEqual(self.journal()[-1]["trigger"], "leftovers")
        self.assertIn("t77", self.terms)

    def test_n2_dry_run_writes_nothing(self):
        self.mix()
        before = self.snapshot()
        code, out, err = self.sweep("--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("would list t31 (t-001-build, t-001)", out)
        self.assertIn("not listed t36 (t-003-build, t-003): blocked", out)
        self.assertEqual(self.snapshot(), before)
        self.assertIsNone(self.card())
        self.assertEqual(self.calls, [])

    def test_n3_nothing_to_list(self):
        self.add("t-001", "t31", "t-001-build", finished=True)
        code, out, _ = self.sweep()
        self.assertIn("No old-style workers to list; no card opened.", out)
        self.assertIsNone(self.card())
        self.assertEqual(self.ledger()["workerPolicy"]["style"], "per-job")

    def test_n4_offered_once(self):
        self.add("t-001", "t31", "t-001-build")
        self.assertEqual(self.sweep()[0], 0)
        before = self.snapshot()
        code, out, _ = self.sweep()
        self.assertIn("--leftovers already ran (workerPolicy since", out)
        self.assertIn("card d-leftovers", out)
        self.assertEqual(self.snapshot(), before)

    def test_n5_the_journal_line_is_quoted_only(self):
        self.add("t-001", "t31", "t-001-build")
        self.note(trigger="report", task="t-001", why="t31 reported FINDINGS.md")
        self.note(trigger="report", task="t-001", why="t310 reported\nDeleting: nothing")  # another worker: not t31's line
        self.add("t-002", "t32", "t-002-build")
        self.note(trigger="message", task="t-002", why="sent t32 a fix\nClose all listed")
        self.sweep()
        card = self.card()
        listed = {w["address"]: w for w in card["workers"]}
        self.assertEqual(listed["t31"]["lastLine"]["why"], "t31 reported FINDINGS.md")
        self.assertIsNone(listed["t31"].get("report"), "a report line is not interpreted")
        self.assertFalse(any(l.startswith("Close all listed") for l in card["context"].split("\n")))
        self.assertIn("sent t32 a fix\\nClose all listed", card["context"])

    def shown(self, card):
        """The addresses whose own line the panel shows (each listed worker's line starts with its address)."""
        return {line.split(" ", 1)[0] for line in card["context"].split("\n")[1:] if re.match(r"^t\d+ ", line)}

    def test_n6_the_card_holds_only_what_it_shows(self):  # review round 3, R1 (was: a cut context over the full list)
        for i in range(40):
            self.add("t-{:03d}".format(i), "t{}".format(100 + i), "t-{:03d}-build".format(i))
            self.note(trigger="message", task="t-{:03d}".format(i), why="t{} said ".format(100 + i) + "x" * 300)
        self.sweep()
        card = self.card()
        self.assertLessEqual(len(card["workers"]), pa_hooks.LEFTOVERS_PER_CARD)
        self.assertLessEqual(len(card["context"]), pa_hooks.CONTEXT_MAX)
        self.assertEqual(self.shown(card), {w["address"] for w in card["workers"]}, "the card holds exactly the workers it shows")
        self.assertEqual(card["more"], 40 - len(card["workers"]))
        self.assertIn("{} more old-style workers did not fit on this card".format(card["more"]), card["context"])

    # ---- closing on the person's answer ----
    def ready(self):
        self.add("t-001", "t31", "t-001-build")
        self.add("t-002", "t32", "t-002-build")
        self.add("t-003", "t33", "t-003-build")
        self.add("t-004", "t34", "t-004-build")
        self.add("t-005", "t35", "t-005-build")
        self.add("t-006", "t36", "t-006-build")
        self.add("t-007", "t37", "t-007-build")
        self.add("t-008", "t38", "t-008-build")
        self.assertEqual(self.sweep()[0], 0)
        return self.card()

    def test_n7_not_before_the_persons_answer_in_the_panel(self):
        self.ready()
        before = self.snapshot()
        self.assertRefused(self.worker("close", "--card", "d-leftovers"), "card d-leftovers is not answered yet")
        self.answer("Close none")
        self.assertRefused(self.worker("close", "--card", "d-leftovers"), "card d-leftovers was answered 'Close none'")
        self.answer("Close all listed", by="person, in conversation")
        self.assertRefused(self.worker("close", "t31", "--card", "d-leftovers"), "is answered in the panel only")
        self.assertRefused(self.worker("close", "--card", "d-nope"), "no leftovers card d-nope")
        self.assertEqual(self.snapshot(), before)

    def test_n8_close_all_listed_with_guards(self):
        card = self.ready()
        proj = self.repo()
        dirty = self.worktree(proj, "t-005-x")
        led = self.ledger()
        next(t for t in led["tasks"] if t["id"] == "t-005")["worktree"] = dirty
        next(t for t in led["tasks"] if t["id"] == "t-007")["workers"][0].update(keep=True, keptAt="x", keepQuote="keep t37")
        self.ledger(led)
        self.answer("Close all listed")
        self.terms["t33"]["status"] = "working"  # working now
        self.terms["t34"]["status_ms"] = int((time.time() + 5) * 1000)  # active since the card opened
        write(os.path.join(dirty, "new.txt"), "unsaved\n")  # t35's worktree has uncommitted work now
        del self.terms["t36"]  # gone since: closing only marks it
        self.terms["t38"]["name"] = "someone-else"  # the address now names another terminal
        lines = len(self.journal())
        code, out, err = self.worker("close", "--card", "d-leftovers")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ended(), [["hc", "kill", "t31"], ["hc", "rm", "t31"], ["hc", "kill", "t32"], ["hc", "rm", "t32"]])
        w = self.entry("t-001", "t31")
        self.assertEqual((w["finished"], w["closedBy"], w["report"]), (True, "agent", "card d-leftovers"))
        self.assertTrue(self.entry("t-002", "t32")["finished"])
        self.assertTrue(self.entry("t-006", "t36")["finished"])
        for tid, address, why in (("t-003", "t33", "is working"), ("t-004", "t34", "has been active since card d-leftovers opened"),
                                  ("t-005", "t35", "uncommitted file"), ("t-007", "t37", "is marked keep"),
                                  ("t-008", "t38", "is now a different terminal (someone-else)")):
            self.assertFalse(self.entry(tid, address).get("finished"), address)
            self.assertRegex(out, r"skipped {} \(.*{}".format(address, re.escape(why)))
        new = self.journal()[lines:]
        self.assertEqual([l["trigger"] for l in new], ["leftovers", "leftovers", "leftovers"])
        self.assertIn("on the person's answer to card d-leftovers", new[0]["why"])

    def test_n8b_any_note_closes_nothing(self):  # review round 3, R2: the note is never parsed
        self.ready()
        before_tasks = json.dumps(self.ledger()["tasks"])
        for note in ("keep t32 please", "except T40", "not the job0 worker"):
            path = os.path.join(self.dir, "decisions", "d-leftovers.json")
            card = load(path)
            card.update(status="answered", answer="Close all listed", note=note)
            dump(card, path)
            code, out, err = self.worker("close", "--card", "d-leftovers")
            self.assertEqual(code, 2)
            self.assertIn("card d-leftovers has a note, so nothing was closed", err)
            self.assertIn("say \"keep tNN\"", err)
            self.assertEqual(self.ended(), [])
            self.assertEqual(json.dumps(self.ledger()["tasks"]), before_tasks)
            card = load(path)
            self.assertEqual((card["status"], card["previousAnswer"]), ("withdrawn", "Close all listed"))
        self.assertRefused(self.worker("close", "t31", "--card", "d-leftovers"), "card d-leftovers is withdrawn")
        # the person says "keep t32"; the sweep re-issues the question without it
        self.assertEqual(self.worker("keep", "t32", "--quote", "keep t32 please")[0], 0)
        code, out, err = self.sweep()
        self.assertEqual(code, 0, err)
        self.assertIn("opened card d-leftovers-2", out)
        again = load(os.path.join(self.dir, "decisions", "d-leftovers-2.json"))
        self.assertNotIn("t32", {w["address"] for w in again["workers"]})
        self.assertIn("t31", {w["address"] for w in again["workers"]})

    def test_n12_shown_equals_closed_and_the_rest_comes_next(self):  # review round 3, R1
        for i in range(12):
            self.add("t-{:03d}".format(i), "t{}".format(100 + i), "t-{:03d}-job{}".format(i, i))
            self.note(trigger="message", task="t-{:03d}".format(i), why="t{} handed a long job: ".format(100 + i) + "y" * 150)
        self.sweep()
        first = self.card()
        shown = self.shown(first)
        self.assertEqual(shown, {w["address"] for w in first["workers"]})
        self.assertGreater(first["more"], 0)
        self.answer("Close all listed")
        self.assertEqual(self.worker("close", "--card", "d-leftovers")[0], 0)
        closed = {w["address"] for t in self.ledger()["tasks"] for w in t["workers"] if w.get("finished")}
        self.assertEqual(closed, shown, "exactly the workers the person saw are closed")
        code, out, err = self.sweep()
        self.assertEqual(code, 0, err)
        second = load(os.path.join(self.dir, "decisions", "d-leftovers-2.json"))
        self.assertFalse(shown & {w["address"] for w in second["workers"]})
        self.assertEqual(self.shown(second), {w["address"] for w in second["workers"]})
        path = os.path.join(self.dir, "decisions", "d-leftovers-2.json")
        second.update(status="answered", answer="Close all listed", note=None)
        dump(second, path)
        self.assertEqual(self.worker("close", "--card", "d-leftovers-2")[0], 0)
        closed = {w["address"] for t in self.ledger()["tasks"] for w in t["workers"] if w.get("finished")}
        self.assertEqual(closed, shown | self.shown(second))
        if second["more"] == 0:
            self.assertIn("--leftovers already ran", self.sweep()[1])
        self.assertEqual(self.ledger()["workerPolicy"]["cards"][:2], ["d-leftovers", "d-leftovers-2"])

    def test_n13_unknown_activity_is_skipped(self):  # review round 3, recommended
        self.ready()
        self.answer("Close all listed")
        del self.terms["t31"]["status_ms"]
        code, out, _ = self.worker("close", "--card", "d-leftovers")
        self.assertIn("skipped t31 (t-001-build, t-001): activity since card d-leftovers opened is unknown", out)
        self.assertFalse(self.entry("t-001", "t31").get("finished"))

    def test_n9_one_worker_by_card(self):
        self.ready()
        self.answer("Close all listed")
        self.add("t-009", "t39", "t-009-build")  # recorded after the card: not on it
        self.assertRefused(self.worker("close", "t39", "--card", "d-leftovers"), "t39 is not on card d-leftovers")
        code, out, err = self.worker("close", "t31", "--card", "d-leftovers")
        self.assertEqual(code, 0, err)
        self.assertTrue(self.entry("t-001", "t31")["finished"])

    def test_n10_pa_decide_cannot_answer_it(self):
        self.ready()
        with self.assertRaises(pa.Problem) as e:
            pa.cmd_decide(argparse.Namespace(space="sp1", card="d-leftovers", answer="Close all listed", quote="close them all",
                                             withdraw=False, why=None))
        self.assertIn("a card that closes workers is answered in the panel, not in conversation", str(e.exception))
        self.assertEqual(self.card()["status"], "open")

    def test_n11_usage(self):
        code, _, err = self.worker("close", "t31", "--evidence", "F.md", "--card", "d-leftovers")
        self.assertEqual(code, 2)
        self.assertIn("pa-worker close takes --evidence or --card, not both", err)
        code, _, err = self.worker("close", "--leftovers", "--card", "d-leftovers")
        self.assertIn("pa-worker close needs an address and --evidence, or --leftovers, or --card <id>", err)


class Describe(Base):
    """R7: the worktree card says what the worktree holds; the full list goes to WORKTREE.md (spec 4.2-4.6)."""

    def setUp(self):
        super().setUp()
        self.proj = self.repo()
        self.wt = self.worktree(self.proj, "t-003-x")
        self.fork = sh(self.proj, "git", "rev-parse", "--short", "main")

    def open_card(self, tasks=(("t-003", "done"),), worktree=None):
        for tid, state in tasks:
            self.add_task(tid, state, worktree or self.wt, [{"address": "t31", "role": "build", "name": tid + "-tests", "finished": True}])
        code, out, err = self.run_cmd("pa-worktree", "check", "--open-cards", "--space", "sp1")
        self.assertEqual(code, 0, err)
        self.assertIn("opened card d-wt-" + tasks[0][0], out)
        return load(os.path.join(self.dir, "decisions", "d-wt-{}.json".format(tasks[0][0])))

    def md(self, task="t-003"):
        with open(os.path.join(self.dir, "evidence", task, "WORKTREE.md")) as f:
            return f.read()

    def profile_kind(self, repos=None, extra=""):
        kind = os.path.join(self.tmp, "kind-wt")
        write(os.path.join(kind, "marker.txt"), "")
        out = {"uncommitted": 2, "unpushed": 1, "branch": "A:feat, B:feat", "removesBranches": True, "kind": "custom worktree"}
        if repos is not None:
            out["repos"] = repos
        status = os.path.join(self.tmp, "status.sh")
        write(status, "#!/bin/sh\ncat <<'JSON'\n{}\nJSON\n".format(json.dumps(out)))
        os.chmod(status, 0o755)
        dump({"name": "test2", "claude": ["true"], "codex": ["true"],
              "worktree": {"marker": "marker.txt", "status": status, "remove": status}}, os.path.join(pa.ROOT, "profiles", "test2", "profile.json"))
        agent = load(os.path.join(self.dir, "agent.json"))
        agent["profile"] = "test2"
        dump(agent, os.path.join(self.dir, "agent.json"))
        return kind

    def feature_repo(self, folder, commits=1):
        os.makedirs(folder)
        sh(folder, "git", "init", "-q", "-b", "main")
        self.commit(folder, "README", "base\n", "base")
        sh(folder, "git", "checkout", "-q", "-b", "feat")
        for i in range(commits):
            self.commit(folder, "f{}.txt".format(i), "x\n", "feature commit {}".format(i))
        return folder

    def test_d1_git_worktree_content(self):
        bare = os.path.join(self.tmp, "remote.git")
        sh(self.tmp, "git", "init", "-q", "--bare", bare)
        sh(self.proj, "git", "remote", "add", "origin", bare)
        c1 = self.commit(self.wt, "a.txt", "one\ntwo\n", "c1 adds a")
        sh(self.proj, "git", "merge", "-q", "--ff-only", c1)  # c1 is merged into main
        c2 = self.commit(self.wt, "b.txt", "b\n", "c2 adds b")
        sh(self.wt, "git", "push", "-q", "origin", "HEAD:refs/heads/t-003-x")  # c1 and c2 are pushed
        c3 = self.commit(self.wt, "a.txt", "one\n", "c3 trims a")
        write(os.path.join(self.wt, "README"), "edited\n")
        card = self.open_card()
        repo = card["changes"]["repos"][0]
        self.assertEqual((repo["name"], repo["branch"], repo["base"], repo["fork"]), ("proj", "t-003-x", "main", self.fork))
        self.assertEqual([(c["sha"], c["merged"], c["pushed"]) for c in repo["commits"]], [(c3, False, False), (c2, False, True), (c1, True, True)])
        self.assertEqual([c["subject"] for c in repo["commits"]], ["c3 trims a", "c2 adds b", "c1 adds a"])
        self.assertEqual((repo["commitsTotal"], repo["merged"], repo["pushed"]), (3, 1, 2))
        self.assertEqual(repo["files"], [{"path": "a.txt", "added": 1, "removed": 0}, {"path": "b.txt", "added": 1, "removed": 0}])
        self.assertEqual((repo["filesTotal"], repo["added"], repo["removed"]), (2, 2, 0))
        self.assertEqual(repo["uncommitted"], [{"path": "README", "status": "M"}])
        self.assertEqual((repo["uncommittedTotal"], repo["error"]), (1, None))
        self.assertEqual(card["changes"]["tasks"], [{"id": "t-003", "title": "Title of t-003", "state": "done"}])
        self.assertFalse(card["changes"]["truncated"])
        self.assertEqual(card["question"], 'Delete the worktree for t-003 "Title of t-003"?')
        lines = card["context"].split("\n")
        self.assertEqual(lines[0], "{} (git worktree)".format(os.path.realpath(self.wt)))
        self.assertEqual(lines[1], 'Tasks: t-003 "Title of t-003" (done)')
        self.assertEqual(lines[2], "proj: branch t-003-x, from main at {}: 3 commits, 1 merged into main, 2 pushed".format(self.fork))
        self.assertEqual(lines[3], "  merged and pushed are from local refs; last fetch: never")  # review round 1, M5
        self.assertEqual(lines[4], "  {} c3 trims a  (not merged, not pushed)".format(c3))
        self.assertEqual(lines[6], "  {} c1 adds a  (merged, pushed)".format(c1))
        self.assertIn("  Files changed: 2 (+2): a.txt +1, b.txt +1", lines)
        self.assertIn("  Uncommitted: 1: M README", lines)
        self.assertIn("Workers: t-003-tests (closed)", lines)
        self.assertIn("No other terminal is working in it.", lines)
        self.assertIn("Deleting: the branch t-003-x and its commits are kept; the 1 uncommitted files are DISCARDED.", lines)
        self.assertEqual(lines[-1], "Full list: WORKTREE.md")
        self.assertEqual(card["evidence"], ["WORKTREE.md"])
        snap = card["snapshot"]
        self.assertEqual((snap["uncommitted"], snap["unpushed"], snap["commits"]), (1, 1, 3))
        # review round 1, I1: what pa-worktree remove compares later
        self.assertEqual(snap["heads"], {os.path.realpath(self.wt): sh(self.wt, "git", "rev-parse", "HEAD")})
        self.assertEqual(snap["uncommittedFiles"], {os.path.realpath(self.wt): ["README"]})
        self.assertEqual(card["recommendation"], "Keep")
        md = self.md()
        for text in ("c1 adds a", "c2 adds b", "c3 trims a", "a.txt", "README", "t-003-x"):
            self.assertIn(text, md)

    def test_d2_fast_forward_merged_branch_is_still_described(self):
        c1 = self.commit(self.wt, "a.txt", "a\n", "first")
        c2 = self.commit(self.wt, "b.txt", "b\n", "second")
        sh(self.proj, "git", "merge", "-q", "--ff-only", "t-003-x")
        repo = self.open_card()["changes"]["repos"][0]
        self.assertEqual([(c["sha"], c["merged"]) for c in repo["commits"]], [(c2, True), (c1, True)])
        self.assertEqual(repo["merged"], 2)

    def test_d3_cherry_picked_commit_counts_as_merged(self):
        c1 = self.commit(self.wt, "a.txt", "a\n", "picked one")
        c2 = self.commit(self.wt, "b.txt", "b\n", "not picked")
        self.commit(self.proj, "m.txt", "m\n", "main moves on")
        sh(self.proj, "git", "cherry-pick", c1)
        repo = self.open_card()["changes"]["repos"][0]
        self.assertEqual([(c["sha"], c["merged"]) for c in repo["commits"]], [(c2, False), (c1, True)])
        card = load(os.path.join(self.dir, "decisions", "d-wt-t-003.json"))
        self.assertIn("(a squash merge shows as not merged)", card["options"][0]["detail"])

    def test_d4_limits(self):
        for i in range(29):
            self.commit(self.wt, "f{:02d}.txt".format(i), "x\n", "commit {}".format(i))
        for i in range(11):
            write(os.path.join(self.wt, "g{:02d}.txt".format(i)), "y\n")
        sh(self.wt, "git", "add", ".")
        sh(self.wt, "git", "commit", "-q", "-m", "S" * 200)
        card = self.open_card()
        repo = card["changes"]["repos"][0]
        self.assertEqual((repo["commitsTotal"], len(repo["commits"]), repo["filesTotal"], len(repo["files"])), (30, 8, 40, 10))
        self.assertTrue(card["changes"]["truncated"])
        self.assertLessEqual(len(repo["commits"][0]["subject"]), 72)
        self.assertLessEqual(len(card["context"]), 2000)
        self.assertIn("  … 8 shown, 22 more", card["context"].split("\n"))
        self.assertIn(", … 30 more", card["context"])
        md = self.md()
        self.assertIn("commit 0", md)
        self.assertIn("g10.txt", md)

    def test_d5_several_repos_from_the_profile_script(self):
        kind = self.profile_kind()
        a = self.feature_repo(os.path.join(kind, "src", "A"))
        b = self.feature_repo(os.path.join(kind, "src", "B"), commits=2)
        outside = self.feature_repo(os.path.join(self.tmp, "Outside"))
        self.profile_kind(repos=[a, b, outside, os.path.join(kind, "src", "missing")])
        card = self.open_card(worktree=kind)
        self.assertEqual([r["name"] for r in card["changes"]["repos"]], ["A", "B"])
        self.assertEqual(card["changes"]["repos"][1]["commitsTotal"], 2)
        context = card["context"]
        self.assertIn("{} (custom worktree)".format(os.path.realpath(kind)), context)
        self.assertIn("A: branch feat, from main at", context)
        self.assertIn("B: branch feat, from main at", context)
        self.assertNotIn("Outside", context)
        self.assertIn("Deleting also deletes its branches (A:feat, B:feat)", context)

    def test_d6_profile_script_without_repos(self):
        kind = self.profile_kind()
        card = self.open_card(worktree=kind)
        self.assertEqual(card["changes"]["repos"], [])
        self.assertEqual(card["changes"]["note"], "this kind of worktree lists no repositories; counts only")
        self.assertIn("this kind of worktree lists no repositories; counts only", card["context"].split("\n"))
        self.assertIn("Uncommitted files: 2. Commits not on any remote: 1.", card["context"])

    def test_d7_a_failure_does_not_stop_the_card(self):
        kind = self.profile_kind()
        plain = os.path.join(kind, "src", "NotARepo")
        os.makedirs(plain)
        self.profile_kind(repos=[plain])
        card = self.open_card(worktree=kind)
        repo = card["changes"]["repos"][0]
        self.assertTrue(repo["error"])
        self.assertIn("could not read the changes: ", card["context"])
        self.assertEqual(card["snapshot"]["uncommitted"], 2)

    def test_d8_rename_and_binary(self):
        sh(self.wt, "git", "mv", "README", "READ-ME.txt")
        sh(self.wt, "git", "commit", "-q", "-m", "rename")
        with open(os.path.join(self.wt, "blob.bin"), "wb") as f:
            f.write(bytes(range(256)) * 4)
        sh(self.wt, "git", "add", "blob.bin")
        sh(self.wt, "git", "commit", "-q", "-m", "binary")
        card = self.open_card()
        files = {f["path"]: f for f in card["changes"]["repos"][0]["files"]}
        self.assertIn("READ-ME.txt", files)
        self.assertNotIn("README", files)
        self.assertEqual((files["blob.bin"]["added"], files["blob.bin"]["removed"]), (None, None))
        self.assertIn("blob.bin (binary)", card["context"])

    def test_d9_detached_head(self):
        c1 = self.commit(self.wt, "a.txt", "a\n", "on the branch")
        sh(self.wt, "git", "checkout", "-q", "--detach")
        repo = self.open_card()["changes"]["repos"][0]
        self.assertEqual((repo["branch"], repo["fork"]), ("HEAD", self.fork))
        self.assertEqual([c["sha"] for c in repo["commits"]], [c1])

    def test_d10_many_repos(self):
        kind = self.profile_kind()
        repos = [self.feature_repo(os.path.join(kind, "src", "r{:02d}".format(i)), commits=0) for i in range(25)]
        self.profile_kind(repos=repos)
        # This tests the count cap alone. Describing 20 repositories takes ~41 s at load 18 (16 git calls
        # each), so the 60 s whole-description deadline would cut it short on a busier machine; d15 tests
        # that deadline.
        saved, pa_hooks.DESCRIBE_DEADLINE = pa_hooks.DESCRIBE_DEADLINE, 3600
        try:
            card = self.open_card(worktree=kind)
        finally:
            pa_hooks.DESCRIBE_DEADLINE = saved
        self.assertLessEqual(len(card["changes"]["repos"]), 6)
        self.assertTrue(card["changes"]["truncated"])
        md = self.md()
        self.assertNotIn("time limit", md)
        self.assertEqual(md.count("\n## r"), 20)
        self.assertIn("Not described (more than 20 repositories): r20, r21, r22, r23, r24", md)
        self.assertIn("Unchanged: ", card["context"])
        self.assertLessEqual(len(card["context"]), 2000)

    def test_d11_git_time_cap(self):
        # The slow stand-in never ends by itself (it waits until it is killed). Only the slow call gets
        # the short cap; the quick calls keep the real GIT_TIMEOUT, so machine load cannot time them out.
        real = shutil.which("git")
        bin_dir = os.path.join(self.tmp, "bin")
        write(os.path.join(bin_dir, "git"), '#!/bin/sh\nfor a in "$@"; do [ "$a" = log ] && exec sleep 30; done\nexec "{}" "$@"\n'.format(real))
        os.chmod(os.path.join(bin_dir, "git"), 0o755)
        self.commit(self.wt, "a.txt", "a\n", "slow")
        real_git, slow = pa_hooks.git, []

        def git(path, *args, timeout=None):
            if "log" not in args:
                return real_git(path, *args, timeout=timeout)
            slow.append(timeout)  # the cap describe_repo gave the call, from GIT_TIMEOUT
            return real_git(path, *args, timeout=0.5)
        saved_path = os.environ["PATH"]
        os.environ["PATH"] = bin_dir + os.pathsep + saved_path
        pa_hooks.git = git
        try:
            began = time.time()
            card = self.open_card()
            self.assertLess(time.time() - began, 25)  # the stand-in alone would take 30 s
        finally:
            os.environ["PATH"], pa_hooks.git = saved_path, real_git
        self.assertEqual(len(slow), 1, slow)
        self.assertTrue(slow[0] and 0 < slow[0] <= pa_hooks.GIT_TIMEOUT, slow)
        repo = card["changes"]["repos"][0]
        self.assertEqual(repo["error"], "git log timed out after 0.5 s")
        self.assertEqual(repo["branch"], sh(self.wt, "git", "rev-parse", "--abbrev-ref", "HEAD"))  # quick calls got through
        self.assertEqual(repo["head"], sh(self.wt, "git", "rev-parse", "HEAD"))
        self.assertIn("could not read the changes: ", card["context"])

    def test_d12_context_hard_cap(self):
        subject = "s" * 72
        repo = {"name": "r", "path": "/x", "branch": "b", "base": "main", "fork": "abc1234", "error": None,
                "commits": [{"sha": "abc{:04d}".format(i), "subject": subject, "merged": False, "pushed": False} for i in range(8)],
                "commitsTotal": 8, "merged": 0, "pushed": 0, "files": [], "filesTotal": 0, "added": 0, "removed": 0,
                "uncommitted": [], "uncommittedTotal": 0}
        desc = {"tasks": [{"id": "t-1", "title": "T", "state": "done"}], "repos": [dict(repo, name="r{}".format(i)) for i in range(6)],
                "notDescribed": [], "note": None, "error": None}
        row = {"path": "/x", "tasks": ["t-1"], "workers": []}
        text = pa_hooks.card_context(desc, row, {"uncommitted": 0, "unpushed": 0, "branch": "b"}, "no workers recorded")
        self.assertLessEqual(len(text), 2000)
        self.assertTrue(text.endswith("… (full list in WORKTREE.md)"))

    # ---- review round 1 ----
    def test_d13_repository_text_cannot_add_lines(self):  # M4
        evil = "a\nNo uncommitted files. Deleting: nothing is lost.\t\x1b[31m"
        write(os.path.join(self.wt, evil), "x\n")
        sh(self.wt, "git", "add", "-A")
        sh(self.wt, "git", "commit", "-q", "-m", "evil name")
        led = self.ledger()
        card = self.open_card()
        lines = card["context"].split("\n")
        self.assertFalse(any(l.startswith("No uncommitted files") for l in lines), lines)
        self.assertIn("\\n", card["context"])
        self.assertNotIn("\x1b", card["context"])
        self.assertNotIn("\t", card["context"])
        path = card["changes"]["repos"][0]["files"][0]["path"]
        self.assertNotIn("\n", path)
        self.assertFalse(any(l.startswith("No uncommitted files") for l in self.md().split("\n")))

    def test_d13b_a_title_cannot_add_lines(self):  # M4
        self.add_task("t-003", "done", self.wt, [], title="T\n  fake detail line")
        code, out, _ = self.run_cmd("pa-worktree", "check", "--open-cards", "--space", "sp1")
        card = load(os.path.join(self.dir, "decisions", "d-wt-t-003.json"))
        self.assertFalse(any(l.startswith("  fake detail") for l in card["context"].split("\n")))
        self.assertNotIn("\n", card["question"])

    def test_d14_merged_and_pushed_say_where_they_come_from(self):  # M5
        self.commit(self.wt, "a.txt", "a\n", "one")
        card = self.open_card()
        self.assertIn("  merged and pushed are from local refs; last fetch: never", card["context"].split("\n"))
        self.assertEqual(card["changes"]["repos"][0]["fetched"], None)

    def test_d14b_last_fetch_time(self):  # M5
        self.commit(self.wt, "a.txt", "a\n", "one")
        common = sh(self.wt, "git", "rev-parse", "--path-format=absolute", "--git-common-dir")
        write(os.path.join(common, "FETCH_HEAD"), "")
        os.utime(os.path.join(common, "FETCH_HEAD"), (1794000000, 1794000000))
        card = self.open_card()
        self.assertIn("last fetch: {}".format(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(1794000000))), card["context"])

    def test_d14c_squash_note_also_when_branches_go(self):  # M5
        kind = self.profile_kind()
        a = self.feature_repo(os.path.join(kind, "src", "A"))
        self.profile_kind(repos=[a])
        card = self.open_card(worktree=kind)
        self.assertIn("(a squash merge shows as not merged)", card["options"][0]["detail"])

    def test_d15_a_total_time_bound(self):  # M6
        kind = self.profile_kind()
        repos = [self.feature_repo(os.path.join(kind, "src", "r{}".format(i))) for i in range(3)]
        self.profile_kind(repos=repos)
        real = shutil.which("git")
        bin_dir = os.path.join(self.tmp, "bin")
        # As in d11: the slow stand-in waits until it is killed, and only the slow call gets the short cap
        # (here the one describe_repo gives it from the 1.5 s deadline); the quick calls keep GIT_TIMEOUT.
        write(os.path.join(bin_dir, "git"), '#!/bin/sh\nfor a in "$@"; do [ "$a" = log ] && exec sleep 30; done\nexec "{}" "$@"\n'.format(real))
        os.chmod(os.path.join(bin_dir, "git"), 0o755)
        real_git, slow = pa_hooks.git, []

        def git(path, *args, timeout=None):
            if "log" not in args:
                return real_git(path, *args, timeout=pa_hooks.GIT_TIMEOUT)
            slow.append(timeout)  # the cap describe_repo gave the call, from the deadline
            return real_git(path, *args, timeout=timeout)
        saved = os.environ["PATH"], pa_hooks.DESCRIBE_DEADLINE
        os.environ["PATH"] = bin_dir + os.pathsep + saved[0]
        pa_hooks.git, pa_hooks.DESCRIBE_DEADLINE = git, 1.5
        try:
            began = time.time()
            card = self.open_card(worktree=kind)
            took = time.time() - began
        finally:
            (os.environ["PATH"], pa_hooks.DESCRIBE_DEADLINE), pa_hooks.git = saved, real_git
        self.assertLess(took, 25)  # the stand-in alone would take 30 s
        # r0's log call runs until the deadline has passed (its cap is what is left of 1.5 s, at least 0.2 s), so r1, r2 wait
        self.assertEqual(len(slow), 1, slow)
        self.assertTrue(slow[0] and 0 < slow[0] <= 1.5 < pa_hooks.GIT_TIMEOUT, slow)
        self.assertEqual([r["name"] for r in card["changes"]["repos"]], ["r0"])
        self.assertEqual(card["changes"]["repos"][0]["error"], "git log timed out after {} s".format(slow[0]))
        self.assertEqual(card["changes"]["repos"][0]["branch"], "feat")  # quick calls got through
        self.assertEqual(card["changes"]["repos"][0]["head"], sh(repos[0], "git", "rev-parse", "HEAD"))
        self.assertIn("Not described (time limit of 1.5 s): r1, r2", self.md())
        self.assertIn("more repositories (WORKTREE.md)", card["context"])

    def test_d16_merged_into_any_main_branch(self):  # mg-all
        bare = os.path.join(self.tmp, "remote.git")
        sh(self.tmp, "git", "init", "-q", "--bare", bare)
        sh(self.proj, "git", "remote", "add", "origin", bare)
        sh(self.proj, "git", "push", "-q", "origin", "main")
        sh(self.proj, "git", "remote", "set-head", "origin", "main")
        c1 = self.commit(self.wt, "a.txt", "a\n", "merged locally only")
        sh(self.proj, "git", "merge", "-q", "--ff-only", c1)  # local main has it, origin/main does not
        repo = self.open_card()["changes"]["repos"][0]
        self.assertEqual(repo["base"], "origin/main")
        self.assertEqual([(c["sha"], c["merged"]) for c in repo["commits"]], [(c1, True)])


def hc_status(state, msg=None):
    """A status as `hc get --json` gives it on this machine (t-014): an object, not a word."""
    return dict({"app": "claude-code", "state": state}, **({"msg": msg} if msg else {}))


class StatusShapes(Base):
    """t-014: hc get gives a worker's status as an object {app, state, msg}; hc ls gives a word. Every
    reader of the status takes both: done and idle close, working and blocked refuse, the rest refuse."""

    def test_s1_the_refusal_reads_both_shapes(self):
        entry = {"name": "t-003-tests"}
        for status in ("done", "idle", hc_status("done"), hc_status("idle", "Finished")):
            self.assertIsNone(pa_hooks.status_refusal("t31", entry, {"name": "t-003-tests", "status": status}), status)
        for status, kind in (("working", "working"), (hc_status("working", "Running tests"), "working"),
                             ("blocked", "blocked"), (hc_status("blocked"), "blocked"),
                             ("thinking", "status"), (hc_status("thinking"), "status"), ({"app": "claude-code"}, "status"),
                             ({"state": ["done"]}, "status"), (["done"], "status"), (7, "status")):
            refusal = pa_hooks.status_refusal("t31", entry, {"name": "t-003-tests", "status": status})
            self.assertIsNotNone(refusal, status)
            self.assertEqual(refusal[0], kind, status)
        refusal = pa_hooks.status_refusal("t31", entry, {"name": "t-003-tests", "status": hc_status("thinking", "hm")})
        self.assertEqual(refusal[1], "t31 has status thinking; nothing was closed")
        refusal = pa_hooks.status_refusal("t31", entry, {"name": "t-003-tests", "status": {"app": "claude-code"}})
        self.assertEqual(refusal[1], "t31 has status none; nothing was closed")
        # an ended terminal with no status is still closable, an empty object too
        self.assertIsNone(pa_hooks.status_refusal("t31", entry, {"state": "finished", "status": None}))
        self.assertIsNone(pa_hooks.status_refusal("t31", entry, {"state": "finished", "status": {}}))

    def test_s2_close_a_done_worker_whose_status_is_an_object(self):
        proj = self.repo()
        wt = self.worktree(proj, "t-003-x")
        self.add_task("t-003", "in-progress", wt, [{"address": "t31", "role": "build", "name": "t-003-tests"}])
        self.term("t31", "t-003-tests", status=hc_status("done"))
        self.evidence("t-003")
        pa_hooks.by_an_agent = lambda: True
        code, out, err = self.worker("close", "t31", "--evidence", "FINDINGS.md")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ended(), [["hc", "kill", "t31"], ["hc", "rm", "t31"]])
        self.assertTrue(self.entry("t-003", "t31")["finished"])

    def test_s3_working_and_blocked_objects_are_refused(self):
        self.add_task("t-003", "in-progress", None, [{"address": "t31", "role": "build", "name": "t-003-tests"}])
        self.evidence("t-003")
        pa_hooks.by_an_agent = lambda: True
        for status, why in ((hc_status("working", "Running"), "t31 (t-003-tests) is working; close it after its report"),
                            (hc_status("blocked"), "t31 (t-003-tests) is blocked on a question or a permission"),
                            (hc_status("thinking"), "t31 has status thinking; nothing was closed")):
            self.term("t31", "t-003-tests", status=status)
            before = self.snapshot()
            self.assertRefused(self.worker("close", "t31", "--evidence", "FINDINGS.md"), why)
            self.assertEqual(self.snapshot(), before)

    def test_s4_leftovers_and_card_read_object_statuses(self):
        pa_hooks.by_an_agent = lambda: True
        now = time.time()
        for tid, address, status in (("t-001", "t31", hc_status("done")), ("t-002", "t32", hc_status("idle")),
                                     ("t-003", "t33", hc_status("working", "Running")), ("t-004", "t34", hc_status("blocked")),
                                     ("t-005", "t35", hc_status("thinking"))):
            self.add_task(tid, "in-progress", None, [{"address": address, "role": "build", "name": tid + "-build"}])
            self.term(address, tid + "-build", status=status, status_ms=int((now - 3600) * 1000))
        code, out, err = self.worker("close", "--leftovers")
        self.assertEqual(code, 0, err)
        card = pa.read_json(os.path.join(self.dir, "decisions", "d-leftovers.json"))
        self.assertIsNotNone(card, "the sweep lists the done and idle workers on a card: " + out)
        listed = {w["address"]: w["state"] for w in card["workers"]}
        self.assertEqual(listed, {"t31": "done", "t32": "idle"})
        held = {n["address"]: n["why"] for n in card["notListed"]}
        self.assertEqual(held, {"t33": "working", "t34": "blocked", "t35": "status thinking"})
        self.assertNotIn("'app'", card["context"])
        path = os.path.join(self.dir, "decisions", "d-leftovers.json")
        card.update(status="answered", answer="Close all listed", note=None, answered=pa.now())
        dump(card, path)
        code, out, err = self.worker("close", "--card", "d-leftovers")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ended(), [["hc", "kill", "t31"], ["hc", "rm", "t31"], ["hc", "kill", "t32"], ["hc", "rm", "t32"]])
        self.assertTrue(self.entry("t-001", "t31")["finished"])
        self.assertTrue(self.entry("t-002", "t32")["finished"])


class TaskWorktrees(Base):
    """t-020 J1 (R1, R7, AC3.5): a task's worktrees read as a list, each with its job; the single field keeps working."""

    def setUp(self):
        super().setUp()
        self.proj = self.repo()
        self.wt = self.worktree(self.proj, "t-003-x")
        self.wt2 = self.worktree(self.proj, "t-003-billing")
        self.evidence("t-003")

    def task(self, **fields):
        return dict({"id": "t-003", "title": "T", "state": "in-progress", "worktree": self.wt, "workers": []}, **fields)

    def trees(self, task):
        return [(w["path"], w["job"], w["main"]) for w in pa.task_worktrees(task)]

    def test_tw1_single_field_reads_as_main(self):
        self.assertEqual(pa.task_worktrees(self.task()), [{"path": os.path.realpath(self.wt), "given": self.wt, "job": "main", "keep": False,
                                                           "main": True, "jobRecorded": True, "created": None}])
        self.assertEqual(pa.task_worktrees(self.task(worktree=None)), [])
        self.assertEqual(pa.task_worktrees(self.task(worktree="")), [])
        self.assertEqual(pa.task_worktrees({"id": "t-1"}), [])

    def test_tw2_main_listed_in_worktrees_counted_once(self):
        task = self.task(worktrees=[{"path": self.wt + "/", "job": "tool", "created": "2026-11-17T00:00:00Z"},
                                    {"path": self.wt2, "job": "billing-script", "created": "2026-11-17T01:00:00Z"}])
        got = pa.task_worktrees(task)
        self.assertEqual([(w["path"], w["job"], w["main"], w["jobRecorded"]) for w in got],
                         [(os.path.realpath(self.wt), "tool", True, True), (os.path.realpath(self.wt2), "billing-script", False, True)])
        self.assertEqual(got[0]["created"], "2026-11-17T00:00:00Z")
        self.assertEqual(got[1]["given"], self.wt2)
        # no `worktree`: no main, the list alone
        self.assertEqual(self.trees(self.task(worktree=None, worktrees=[{"path": self.wt2, "job": "b"}])), [(os.path.realpath(self.wt2), "b", False)])

    def test_tw3_legacy_extra_worktrees_job_from_folder(self):
        tool = os.path.join(self.tmp, "Herdr", "worktrees", "t-007b-tool", "herdr-project-agent")
        guard = os.path.join(self.tmp, "Herdr", "worktrees", "t-007-guard", "herdr-project-agent")
        ws_tree = os.path.join(self.tmp, "Ws", "worktrees", "t-007-billing-script")
        plain = os.path.join(self.tmp, "elsewhere", "t-007-notes")
        task = self.task(id="t-007", worktree=None, extraWorktrees=[tool, guard, ws_tree, plain])
        got = pa.task_worktrees(task)
        self.assertEqual([(w["path"], w["job"], w["main"], w["jobRecorded"]) for w in got],
                         [(os.path.realpath(tool), "t-007b-tool", False, False), (os.path.realpath(guard), "guard", False, False),
                          (os.path.realpath(ws_tree), "billing-script", False, False), (os.path.realpath(plain), "notes", False, False)])
        self.assertEqual(pa.legacy_job("t-009", "/w/HerdrPaProfile/worktrees/t-009-billing-script"), "billing-script")
        self.assertEqual(pa.legacy_job("t-007", "/w/Herdr/worktrees/t-007b-tool/herdr-project-agent"), "t-007b-tool")
        self.assertEqual(pa.legacy_job("t-017", "/w/Herdr/worktrees/t-017-timing-test/herdr-project-agent/"), "timing-test")
        # a legacy path that is also the main one is counted once, as the main one
        self.assertEqual(self.trees(self.task(extraWorktrees=[self.wt, self.wt2])),
                         [(os.path.realpath(self.wt), "main", True), (os.path.realpath(self.wt2), pa.legacy_job("t-003", self.wt2), False)])

    def test_tw4_removed_entries_skipped_main_field_wins(self):
        task = self.task(worktrees=[{"path": self.wt, "job": "tool", "removed": "2026-11-17T02:00:00Z"},
                                    {"path": self.wt2, "job": "billing-script", "removed": "2026-11-17T02:00:00Z"}],
                         extraWorktrees=[self.wt2])
        self.assertEqual(self.trees(task), [(os.path.realpath(self.wt), "tool", True)])
        self.assertEqual(self.trees(dict(task, worktree=None)), [])

    def test_tw5_entry_keep_and_task_keep(self):
        task = self.task(worktrees=[{"path": self.wt2, "job": "b", "keep": True}])
        self.assertEqual([w["keep"] for w in pa.task_worktrees(task)], [False, True])
        task = self.task(worktrees=[{"path": self.wt, "job": "a", "keep": True}, {"path": self.wt2, "job": "b", "keep": "yes"}])
        self.assertEqual([w["keep"] for w in pa.task_worktrees(task)], [True, False])
        task["keepWorktree"] = True
        self.assertEqual([w["keep"] for w in pa.task_worktrees(task)], [True, True])

    def test_tw6_bad_entries_and_duplicate_jobs(self):
        c, d, e, f = (os.path.join(self.tmp, n) for n in ("t-003-c", "wt-d", "wt-e", "wt-f"))
        task = self.task(worktrees=["a string", {"job": "x"}, {"path": 5}, {"path": ""}, None, {"path": c},
                                    {"path": d, "job": "Billing Script!"}, {"path": e, "job": "billing-script"}, {"path": f, "job": 7}],
                         extraWorktrees=[7, None, "", self.wt])
        self.assertEqual(self.trees(task), [(os.path.realpath(self.wt), "main", True), (os.path.realpath(c), "c", False),
                                            (os.path.realpath(d), "billing-script", False), (os.path.realpath(e), "billing-script-2", False),
                                            (os.path.realpath(f), "wt-f", False)])
        self.assertEqual([w["jobRecorded"] for w in pa.task_worktrees(task)], [True, False, True, True, False])
        self.assertEqual(self.trees(self.task(worktrees="garbage", extraWorktrees={"a": 1})), [(os.path.realpath(self.wt), "main", True)])
        self.assertEqual(pa.job_slug("Billing Script!"), "billing-script")
        self.assertEqual(pa.job_slug("--x__y--"), "x-y")
        self.assertEqual(pa.job_slug(""), "job")
        self.assertEqual(pa.job_slug("%%%"), "job")
        self.assertEqual(pa.job_slug("a" * 50), "a" * 40)
        # a recorded job named `main` beside the implicit main one gets the next free name
        self.assertEqual(self.trees(self.task(worktrees=[{"path": self.wt2, "job": "main"}])),
                         [(os.path.realpath(self.wt), "main", True), (os.path.realpath(self.wt2), "main-2", False)])

    def test_tw7_state_one_row_per_worktree_with_jobs(self):
        self.add_task("t-003", "done", self.wt, [{"address": "t31", "role": "build", "name": "t-003-main"},
                                                 {"address": "t32", "role": "build", "name": "t-003-billing", "worktree": self.wt2}],
                      worktrees=[{"path": self.wt2, "job": "billing", "keep": True}])
        self.add_task("t-004", "review", self.wt, [{"address": "t41", "role": "build", "name": "t-004-fix"}])
        self.term("t32", "t-003-billing")
        rows = pa_hooks.worktree_state(self.dir, {"terminal": "t0"})
        main, other = rows[os.path.realpath(self.wt)], rows[os.path.realpath(self.wt2)]
        self.assertEqual(sorted(rows), sorted([os.path.realpath(self.wt), os.path.realpath(self.wt2)]))
        self.assertEqual(main["tasks"], ["t-003", "t-004"])
        self.assertEqual(main["jobs"], {"t-003": "main", "t-004": "main"})
        self.assertEqual(main["mains"], {"t-003": True, "t-004": True})
        self.assertEqual(main["jobRecorded"], {"t-003": True, "t-004": True})
        self.assertFalse(main["keep"])
        self.assertEqual([w["address"] for w in main["workers"]], ["t31", "t41"])
        self.assertEqual(other["tasks"], ["t-003"])
        self.assertEqual(other["jobs"], {"t-003": "billing"})
        self.assertEqual(other["mains"], {"t-003": False})
        self.assertTrue(other["keep"])
        self.assertEqual([(w["address"], w["fate"]) for w in other["workers"]], [("t32", "live")])
        self.assertEqual(main["others"], [])
        # rule (a) across the two tasks sharing the main row; the other job's row opens nothing in J1
        code, out, err = self.run_cmd("pa-worktree", "check", "--open-cards", "--space", "sp1")
        self.assertEqual(code, 0, err)
        self.assertIn("t-003: tasks still t-004 review; asked once every task using it is done or parked", out)
        led = self.ledger()
        led["tasks"][1]["state"] = "done"
        led["tasks"][0]["worktrees"][0]["keep"] = False
        self.ledger(led)
        del self.terms["t32"]
        code, out, err = self.run_cmd("pa-worktree", "check", "--open-cards", "--space", "sp1")
        self.assertEqual(code, 0, err)
        self.assertIn("opened card d-wt-t-003", out)
        # J2: the other job's worktree is checked on its own and refused a card outside a worktrees folder (was "not offered yet" in J1)
        self.assertIn("t-003: {} is not under a worktrees folder; not offered".format(os.path.realpath(self.wt2)), out)
        card = pa.read_json(os.path.join(self.dir, "decisions", "d-wt-t-003.json"))
        self.assertEqual(card["worktree"], os.path.realpath(self.wt))
        self.assertEqual(sorted(os.listdir(os.path.join(self.dir, "decisions"))), ["d-wt-t-003.json"])

    def close_setup(self, dirty):
        self.add_task("t-003", "in-progress", self.wt, [{"address": "t31", "role": "build", "name": "t-003-main"},
                                                        {"address": "t32", "role": "build", "name": "t-003-billing", "worktree": self.wt2}],
                      worktrees=[{"path": self.wt2, "job": "billing"}])
        self.term("t31", "t-003-main")
        self.term("t32", "t-003-billing")
        write(os.path.join(dirty, "unsaved.txt"), "x\n")
        pa_hooks.by_an_agent = lambda: True

    def test_tw8_close_ignores_a_dirty_sibling_job_worktree(self):
        self.close_setup(self.wt2)
        code, out, err = self.worker("close", "t31", "--evidence", "FINDINGS.md")
        self.assertEqual(code, 0, err)
        self.assertIn("Checked for uncommitted work: {}".format(os.path.realpath(self.wt)), out)
        self.assertNotIn(os.path.realpath(self.wt2), out)
        task = self.ledger()["tasks"][0]
        self.assertEqual(pa_hooks.uncommitted_in(task, None, task["workers"][0]), (os.path.realpath(self.wt), 0, []))

    def test_tw8b_close_refuses_its_own_dirty_worktree(self):
        self.close_setup(self.wt2)
        before = self.snapshot()
        self.assertRefused(self.worker("close", "t32", "--evidence", "FINDINGS.md"),
                           "{} has 1 uncommitted file(s) (unsaved.txt)".format(os.path.realpath(self.wt2)))
        self.assertEqual(self.snapshot(), before)
        task = self.ledger()["tasks"][0]
        self.assertEqual(pa_hooks.uncommitted_in(task, None, task["workers"][1]), (os.path.realpath(self.wt2), 1, ["unsaved.txt"]))
        # the main worktree being dirty does not stop the billing worker
        os.unlink(os.path.join(self.wt2, "unsaved.txt"))
        write(os.path.join(self.wt, "unsaved.txt"), "x\n")
        code, out, err = self.worker("close", "t32", "--evidence", "FINDINGS.md")
        self.assertEqual(code, 0, err)
        self.assertIn("Checked for uncommitted work: {}".format(os.path.realpath(self.wt2)), out)

    def test_tw9_close_without_worker_worktree_checks_main(self):
        self.close_setup(self.wt)
        self.assertRefused(self.worker("close", "t31", "--evidence", "FINDINGS.md"),
                           "{} has 1 uncommitted file(s) (unsaved.txt)".format(os.path.realpath(self.wt)))
        task = self.ledger()["tasks"][0]
        self.assertEqual(pa_hooks.uncommitted_in(task, None), (os.path.realpath(self.wt), 1, ["unsaved.txt"]))
        self.assertEqual(pa_hooks.uncommitted_in(task, None, task["workers"][0]), (os.path.realpath(self.wt), 1, ["unsaved.txt"]))
        refusal, _checked, _unseen = pa_hooks.worktree_refusal(task, None, {}, task["workers"][1])
        self.assertIsNone(refusal)

    def test_tw10_worker_delete_takes_only_its_own_worktree_and_marks_removed(self):
        self.add_task("t-003", "done", self.wt, [{"address": "t31", "role": "build", "name": "t-003-main"},
                                                 {"address": "t32", "role": "build", "name": "t-003-billing", "worktree": self.wt2}],
                      worktrees=[{"path": self.wt2, "job": "billing", "created": "2026-11-17T00:00:00Z"}])
        self.term("t31", "t-003-main")  # live, but in the main worktree: the billing one is not its
        facts, _row = pa_hooks.worker_facts(self.dir, {"terminal": "t0"}, None, "t32")
        self.assertEqual(facts["worktree"]["path"], os.path.realpath(self.wt2))
        self.assertEqual(facts["worktree"]["sharedWith"], [])
        code, out, err = self.worker("delete", "t32")
        self.assertEqual(code, 0, err)
        self.assertIn("Removed the worktree {}".format(os.path.realpath(self.wt2)), out)
        self.assertFalse(os.path.exists(self.wt2))
        self.assertTrue(os.path.isdir(self.wt))
        task = self.ledger()["tasks"][0]
        self.assertEqual(task["worktree"], self.wt)
        self.assertEqual([w["address"] for w in task["workers"]], ["t31"])
        self.assertEqual(task["worktrees"][0]["job"], "billing")
        self.assertRegex(task["worktrees"][0]["removed"], r"^\d{4}-\d\d-\d\dT")
        self.assertEqual(self.trees(task), [(os.path.realpath(self.wt), "main", True)])
        # the main worker: its worktree is the main one; it goes, `worktree` becomes null, an entry records it
        del self.terms["t31"]
        facts, _row = pa_hooks.worker_facts(self.dir, {"terminal": "t0"}, None, "t31")
        self.assertEqual(facts["worktree"]["path"], os.path.realpath(self.wt))
        code, out, err = self.worker("delete", "t31")
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(self.wt))
        task = self.ledger()["tasks"][0]
        self.assertIsNone(task["worktree"])
        self.assertEqual([(e["path"], e["job"]) for e in task["worktrees"]], [(self.wt2, "billing"), (self.wt, "main")])
        self.assertTrue(task["worktrees"][1]["removed"])
        self.assertEqual(pa.task_worktrees(task), [])

    def test_tw10b_worker_delete_shared_worktree_kept_legacy_marked(self):
        legacy = self.worktree(self.proj, "t-003-legacy")
        self.add_task("t-003", "done", self.wt, [{"address": "t31", "role": "build", "name": "t-003-main"},
                                                 {"address": "t32", "role": "build", "name": "t-003-legacy", "worktree": legacy},
                                                 {"address": "t33", "role": "build", "name": "t-003-legacy-2", "worktree": legacy}],
                      extraWorktrees=[legacy])
        self.term("t33", "t-003-legacy-2")
        code, out, err = self.worker("delete", "t32")
        self.assertEqual(code, 0, err)
        self.assertIn("Kept the worktree: still used by t33.", out)
        self.assertTrue(os.path.isdir(legacy))
        del self.terms["t33"]
        code, out, err = self.worker("delete", "t33")
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(legacy))
        task = self.ledger()["tasks"][0]
        self.assertEqual(task["worktree"], self.wt)
        self.assertEqual(task["extraWorktrees"], [legacy])
        self.assertEqual([(e["path"], e["job"]) for e in task["worktrees"]], [(legacy, pa.legacy_job("t-003", legacy))])
        self.assertTrue(task["worktrees"][0]["removed"])
        self.assertEqual(self.trees(task), [(os.path.realpath(self.wt), "main", True)])

    def test_tw11_worktree_job_md_is_never_a_report(self):
        for name, tool in (("WORKTREE.md", True), ("GATE.json", True), ("WORKTREE-billing-script.md", True), ("WORKTREE-t-007b-tool.md", True),
                           ("FINDINGS.md", False), ("WORKTREE-.md", False), ("WORKTREE-x.txt", False), ("MY-WORKTREE-x.md", False)):
            with self.subTest(name=name):
                self.assertIs(pa_hooks.tool_file(name), tool)
        self.add_task("t-003", "in-progress", None, [{"address": "t31", "role": "build", "name": "t-003-main"}])
        self.term("t31", "t-003-main")
        self.evidence("t-003", "WORKTREE-billing-script.md", "card text\n")
        pa_hooks.by_an_agent = lambda: True
        self.assertRefused(self.worker("close", "t31", "--evidence", "WORKTREE-billing-script.md"),
                           "WORKTREE-billing-script.md is written by the Project Agent's tools, not a worker's report")


    # ---- J1 round 2 (REVIEW M1, M2; VERIFY F1): a worker's `worktree` field counts only inside its own task's worktrees ----
    def test_tw12_worker_field_naming_another_tasks_worktree_takes_nothing_of_it(self):
        other = self.worktree(self.proj, "t-009-other")
        self.add_task("t-009", "in-progress", other, [{"address": "t91", "role": "build", "name": "t-009-other", "finished": True}])
        self.add_task("t-003", "done", self.wt, [{"address": "t31", "role": "build", "name": "t-003-main", "worktree": other}])
        t009 = json.dumps(self.ledger()["tasks"][0], sort_keys=True)
        task = self.ledger()["tasks"][1]
        self.assertEqual(pa_hooks.worker_tree(task, task["workers"][0]), os.path.realpath(self.wt))
        facts, row = pa_hooks.worker_facts(self.dir, {"terminal": "t0"}, None, "t31")
        self.assertEqual(facts["worktree"]["path"], os.path.realpath(self.wt))
        self.assertIn("t-003", row["tasks"])
        self.assertNotIn(os.path.realpath(other), [w["path"] for w in pa.task_worktrees(task)])
        rows = pa_hooks.worktree_state(self.dir, {"terminal": "t0"})
        self.assertEqual([w["address"] for w in rows[os.path.realpath(other)]["workers"]], ["t91"])
        code, out, err = self.worker("delete", "t31")
        self.assertEqual(code, 0, err)
        self.assertNotIn(os.path.realpath(other), out)
        self.assertTrue(os.path.isdir(other), "another task's worktree is never removed")
        led = self.ledger()
        self.assertEqual(json.dumps(led["tasks"][0], sort_keys=True), t009, "another task's ledger entry is untouched")
        self.assertEqual([w["address"] for w in led["tasks"][1]["workers"]], [])

    def stale_fields(self):
        gone = os.path.join(self.tmp, "wt-t-003-gone")
        os.makedirs(gone)
        shutil.rmtree(gone)
        return (("mistyped", self.wt + "-typo"), ("stale", gone), ("relative", "wt-t-003-x"))

    def test_tw13_stale_or_mistyped_field_still_checks_main(self):
        write(os.path.join(self.wt, "unsaved.txt"), "x\n")
        main = os.path.realpath(self.wt)
        for label, field in self.stale_fields():
            with self.subTest(field=label):
                self.ledger({"version": 1, "tasks": []})
                self.terms.clear()
                self.add_task("t-003", "done", self.wt, [{"address": "t31", "role": "build", "name": "t-003-main", "worktree": field}])
                task = self.ledger()["tasks"][0]
                entry = task["workers"][0]
                self.assertEqual(pa_hooks.worker_tree(task, entry), main)
                self.assertEqual(pa_hooks.uncommitted_in(task, None, entry), (main, 1, ["unsaved.txt"]))
                refusal, _checked, _unseen = pa_hooks.worktree_refusal(task, None, {}, entry)
                self.assertIsNotNone(refusal)
                self.assertIn("{} has 1 uncommitted file(s) (unsaved.txt)".format(main), refusal[1])
                # close: refused, the terminal live or already gone
                self.term("t31", "t-003-main")
                pa_hooks.by_an_agent = lambda: True
                self.assertRefused(self.worker("close", "t31", "--evidence", "FINDINGS.md"),
                                   "{} has 1 uncommitted file(s) (unsaved.txt)".format(main))
                del self.terms["t31"]
                self.assertRefused(self.worker("close", "t31", "--evidence", "FINDINGS.md"),
                                   "{} has 1 uncommitted file(s) (unsaved.txt)".format(main))
                # delete: the main worktree is kept for its uncommitted work
                pa_hooks.by_an_agent = lambda: False
                facts, _row = pa_hooks.worker_facts(self.dir, {"terminal": "t0"}, None, "t31")
                self.assertEqual(facts["worktree"]["path"], main)
                self.assertEqual(facts["worktree"]["uncommitted"], 1)
                code, out, err = self.worker("delete", "t31")
                self.assertEqual(code, 0, err)
                self.assertIn("Kept the worktree: it has 1 uncommitted file(s)", out)
                self.assertTrue(os.path.isfile(os.path.join(self.wt, "unsaved.txt")))

    def test_tw14_field_inside_the_worktree_resolves_to_it(self):
        pkg = os.path.join(self.wt2, "src", "Pkg")
        os.makedirs(pkg)
        self.add_task("t-003", "in-progress", self.wt, [{"address": "t31", "role": "build", "name": "t-003-main"},
                                                        {"address": "t32", "role": "build", "name": "t-003-billing", "worktree": pkg}],
                      worktrees=[{"path": self.wt2, "job": "billing"}])
        task = self.ledger()["tasks"][0]
        entry = task["workers"][1]
        second = os.path.realpath(self.wt2)
        self.assertEqual(pa_hooks.worker_tree(task, entry), second)
        self.assertEqual(pa_hooks.worker_tree(task, dict(entry, worktree=os.path.join(self.wt2, "not", "yet"))), second)
        self.assertEqual(pa_hooks.uncommitted_in(task, None, entry), (second, 0, []))
        rows = pa_hooks.worktree_state(self.dir, {"terminal": "t0"})
        self.assertEqual([w["address"] for w in rows[second]["workers"]], ["t32"])
        self.assertEqual([w["address"] for w in rows[os.path.realpath(self.wt)]["workers"]], ["t31"])
        facts, _row = pa_hooks.worker_facts(self.dir, {"terminal": "t0"}, None, "t32")
        self.assertEqual(facts["worktree"]["path"], second)
        write(os.path.join(self.wt2, "unsaved.txt"), "x\n")
        self.term("t32", "t-003-billing")
        pa_hooks.by_an_agent = lambda: True
        self.assertRefused(self.worker("close", "t32", "--evidence", "FINDINGS.md"),
                           "{} has 1 uncommitted file(s) (unsaved.txt)".format(second))

    # ---- J1 round 3 (VERIFY round 2 F2, REVIEW round 2 L5): a worktree another unfinished task records is never removed with a worker ----
    def shared_with_t009(self, state, name, field=True):
        """t-009 (state) has its own main worktree; t-003 (done) lists it as job "shared"; t-003's worker t31 (live) carries it as its field."""
        self.ledger({"version": 1, "tasks": []})
        self.terms.clear()
        self.calls = []
        other = self.worktree(self.proj, name)
        self.add_task("t-009", state, other, [{"address": "t91", "role": "build", "name": "t-009-other", "finished": True}])
        self.add_task("t-003", "done", self.wt, [dict({"address": "t31", "role": "build", "name": "a"}, **({"worktree": other} if field else {}))],
                      worktrees=[{"path": other, "job": "shared"}])
        self.term("t31", "a", cwd=self.tmp)
        return other

    def test_tw15_delete_keeps_a_worktree_another_unfinished_task_records(self):
        for state in ("in-progress", "review"):
            with self.subTest(state=state):
                other = self.shared_with_t009(state, "t-009-" + state)
                before = self.ledger()
                facts, _row = pa_hooks.worker_facts(self.dir, {"terminal": "t0"}, None, "t31")
                self.assertEqual(facts["worktree"]["path"], os.path.realpath(other))
                code, out, err = self.worker("delete", "t31")
                self.assertEqual(code, 0, err)
                self.assertIn("Ended and removed worker t31 (a).", out)
                self.assertTrue(os.path.isdir(other), "another unfinished task's worktree is never removed")
                self.assertIn("Kept the worktree: still used by task t-009 ({}).".format(state), out)
                self.assertNotIn("Removed the worktree", out)
                self.assertEqual(facts["worktree"]["heldBy"], ["t-009"])
                self.assertIn(["hc", "rm", "t31"], self.calls, "the worker itself is still ended")
                led = self.ledger()
                self.assertEqual(json.dumps(led["tasks"][0], sort_keys=True), json.dumps(before["tasks"][0], sort_keys=True))
                t003 = led["tasks"][1]
                self.assertEqual(t003["workers"], [])
                self.assertEqual((t003["worktree"], t003["worktrees"]), (self.wt, [{"path": other, "job": "shared"}]))

    def test_tw15b_delete_removes_it_once_the_other_task_is_done_or_parked(self):
        for state in ("done", "parked"):
            with self.subTest(state=state):
                other = self.shared_with_t009(state, "t-009-" + state)
                code, out, err = self.worker("delete", "t31")
                self.assertEqual(code, 0, err)
                self.assertIn("Removed the worktree {}".format(os.path.realpath(other)), out)
                self.assertFalse(os.path.exists(other))
                led = self.ledger()
                for task in led["tasks"]:
                    self.assertNotIn(os.path.realpath(other), [w["path"] for w in pa.task_worktrees(task)],
                                     "no ledger entry names the removed folder ({})".format(task["id"]))
                self.assertIsNone(led["tasks"][0]["worktree"])
                self.assertTrue(led["tasks"][0]["worktrees"][0]["removed"])
                self.assertEqual(led["tasks"][1]["worktree"], self.wt)
                self.assertTrue(led["tasks"][1]["worktrees"][0]["removed"])

    def test_tw15c_no_field_main_listed_by_another_task_is_not_the_old_shape(self):
        other = self.worktree(self.proj, "t-009-other")
        self.add_task("t-009", "in-progress", other, [], worktrees=[{"path": self.wt, "job": "shared"}])
        self.add_task("t-003", "done", self.wt, [{"address": "t31", "role": "build", "name": "a"}])
        before = json.dumps(self.ledger()["tasks"][0], sort_keys=True)
        code, out, err = self.worker("delete", "t31")
        self.assertEqual(code, 0, err)
        self.assertIn("Kept the worktree: still used by task t-009 (in-progress).", out)
        self.assertTrue(os.path.isdir(self.wt))
        led = self.ledger()
        self.assertEqual(json.dumps(led["tasks"][0], sort_keys=True), before)
        self.assertEqual(led["tasks"][1]["worktree"], self.wt)

    def test_tw16_old_shape_shared_main_no_field_follows_rule_a(self):
        # card d-094, limit 1: the old shape (both tasks have the path as `worktree`, the worker no field) no longer removes a
        # worktree another unfinished task shares; once it is done, both tasks are marked (was: removed always, only its own marked)
        for state in ("in-progress", "done"):
            with self.subTest(state=state):
                self.ledger({"version": 1, "tasks": []})
                self.terms.clear()
                shared = self.worktree(self.proj, "t-003-g-" + state)
                self.add_task("t-009", state, shared, [{"address": "t91", "role": "build", "name": "o", "finished": True}])
                self.add_task("t-003", "done", shared, [{"address": "t31", "role": "build", "name": "a"}])
                self.term("t31", "a", cwd=self.tmp)
                t009 = json.dumps(self.ledger()["tasks"][0], sort_keys=True)
                code, out, err = self.worker("delete", "t31")
                self.assertEqual(code, 0, err)
                led = self.ledger()
                if state == "in-progress":
                    self.assertEqual(out, "Ended and removed worker t31 (a).\nKept the worktree: still used by task t-009 (in-progress).\n"
                                     "(The Project Agent is not running; it reads the ledger when it starts.)\n")
                    self.assertTrue(os.path.isdir(shared))
                    self.assertEqual(json.dumps(led["tasks"][0], sort_keys=True), t009)
                    self.assertEqual(led["tasks"][1]["worktree"], shared)
                else:
                    self.assertEqual(out, "Ended and removed worker t31 (a).\nRemoved the worktree {}. Branch t-003-g-{} is kept.\n"
                                     "(The Project Agent is not running; it reads the ledger when it starts.)\n".format(os.path.realpath(shared), state))
                    self.assertFalse(os.path.exists(shared))
                    self.assertEqual([t["worktree"] for t in led["tasks"]], [None, None])
                    for task in led["tasks"]:
                        self.assertEqual(pa.task_worktrees(task), [])

    def test_tw17_close_never_removes_a_shared_worktree(self):
        other = self.shared_with_t009("in-progress", "t-009-other")
        before = json.dumps(self.ledger()["tasks"][0], sort_keys=True)
        pa_hooks.by_an_agent = lambda: True
        code, out, err = self.worker("close", "t31", "--evidence", "FINDINGS.md")
        self.assertEqual(code, 0, err)
        self.assertIn("its worktree and branch are untouched", out)
        self.assertTrue(os.path.isdir(other))
        self.assertTrue(os.path.isdir(self.wt))
        self.assertEqual(json.dumps(self.ledger()["tasks"][0], sort_keys=True), before)

    def test_tw18_worktree_remove_refuses_while_a_task_listing_it_is_unfinished(self):
        other = self.worktree(self.proj, "t-009-other")
        self.add_task("t-009", "in-progress", other, [], worktrees=[{"path": self.wt, "job": "shared"}])
        self.add_task("t-003", "done", self.wt, [])
        dump({"id": "d-wt-t-003", "task": "t-003", "worktree": self.wt, "status": "answered", "answer": "Delete", "snapshot": {}},
             os.path.join(self.dir, "decisions", "d-wt-t-003.json"))
        pa_hooks.by_an_agent = lambda: False
        code, _out, err = self.run_cmd("pa-worktree", "remove", "t-003", "--space", "sp1")
        self.assertEqual(code, 2)
        self.assertIn("tasks still t-009 in-progress; a worktree is removed only once every task using it is done or parked. Nothing was removed", err)
        self.assertTrue(os.path.isdir(self.wt))


class JobCards(Base):
    """t-020 J2 (R2, R3, AC6.1): each recorded worktree gets its own Delete/Keep card; rule (b) offers one of a task still in
    progress once every commit in it is merged, nothing is uncommitted and no terminal works in it."""

    def setUp(self):
        super().setUp()
        self.proj = self.repo()
        self.wt = self.worktree(self.proj, "t-003-x")
        self.bill = self.job_tree("t-003-billing")

    def job_tree(self, name, branch=None):
        """A worktree in the layout <project>/worktrees/<container>/<repo>."""
        path = os.path.join(self.tmp, "worktrees", name, "proj")
        sh(self.proj, "git", "worktree", "add", "-q", "-b", branch or name, path, "main")
        return path

    def merged_commit(self, path, file=None, message="billing work"):
        """A commit in the worktree, merged into the main branch (a file of its own each time, so it is never empty)."""
        self.merges = getattr(self, "merges", 0) + 1
        file = file or "m{}.txt".format(self.merges)
        sha = self.commit(path, file, "{} {}\n".format(file, self.merges), message)
        sh(self.proj, "git", "merge", "-q", "--no-edit", sh(path, "git", "rev-parse", "HEAD"))
        return sha

    def billing_task(self, state="in-progress", workers=(), **extra):
        self.add_task("t-003", state, self.wt, workers, worktrees=[{"path": self.bill, "job": "billing"}], **extra)

    def check(self, *extra):
        return self.run_cmd("pa-worktree", "check", "--space", "sp1", *extra)

    def card(self, name="d-wt-t-003-billing"):
        return pa.read_json(os.path.join(self.dir, "decisions", name + ".json"))

    def answer(self, name="d-wt-t-003-billing", label="Delete"):
        path = os.path.join(self.dir, "decisions", name + ".json")
        card = load(path)
        card.update(status="answered", answer=label)
        dump(card, path)
        return card

    def remove(self, task="t-003", *extra):
        return self.run_cmd("pa-worktree", "remove", task, "--space", "sp1", *extra)

    def test_b1_merged_job_of_in_progress_task_opens_a_job_card(self):
        self.billing_task()
        self.merged_commit(self.bill)
        code, out, err = self.check()
        self.assertEqual(code, 0, err)
        self.assertIn("t-003: candidate, job billing is over (every commit merged); --open-cards asks the person", out)
        self.assertIsNone(self.card())
        code, out, err = self.check("--open-cards")
        self.assertEqual(code, 0, err)
        self.assertIn("t-003: opened card d-wt-t-003-billing (recommends Delete)", out)
        self.assertIn("t-003: tasks still t-003 in-progress; asked once every task using it is done or parked; job main not over yet "
                      "(no commits since its fork)", out)
        card = self.card()
        self.assertEqual((card["id"], card["task"], card["job"], card["main"], card["rule"], card["worktree"]),
                         ("d-wt-t-003-billing", "t-003", "billing", False, "b", os.path.realpath(self.bill)))
        self.assertEqual(card["question"], 'Delete the worktree for t-003 "Title of t-003", job billing?')
        lines = card["context"].split("\n")
        self.assertEqual(lines[1], 'Tasks: t-003 "Title of t-003" (in-progress)')
        self.assertEqual(lines[2], "Job: billing")
        self.assertEqual(lines[3], "Task still in-progress; this job's commits are all merged")
        self.assertEqual(lines[-1], "Full list: WORKTREE-billing.md")
        self.assertEqual((card["recommendation"], card["status"], card["evidence"]), ("Delete", "open", ["WORKTREE-billing.md"]))
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "evidence", "t-003", "WORKTREE-billing.md")))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "evidence", "t-003", "WORKTREE.md")))
        self.assertIsNone(self.card("d-wt-t-003"), "the main worktree has no commits: not offered")
        self.assertIn("card d-wt-t-003-billing is open", self.check("--open-cards")[1])

    def test_b2_no_commits_is_never_offered(self):
        self.billing_task()
        code, out, err = self.check("--open-cards")
        self.assertEqual(code, 0, err)
        self.assertIn("t-003: tasks still t-003 in-progress; asked once every task using it is done or parked; job billing not over yet "
                      "(no commits since its fork)", out)
        self.assertFalse(os.path.isdir(os.path.join(self.dir, "decisions")) and os.listdir(os.path.join(self.dir, "decisions")))

    def test_b3_unmerged_or_unknown_or_cut_is_not_offered(self):
        cases = {"unmerged": "1 of 2 commits not merged into main", "uncommitted": "1 uncommitted file(s)",
                 "unknown": "merged unknown", "cut": "cannot tell (more commits than described)",
                 "error": "cannot tell (git broke)", "not described": "cannot tell (not every repository was described)"}
        for case, why in cases.items():
            with self.subTest(case=case):
                self.ledger({"version": 1, "tasks": []})
                shutil.rmtree(os.path.join(self.dir, "decisions"), ignore_errors=True)
                sh(self.proj, "git", "worktree", "remove", "--force", self.bill)
                sh(self.proj, "git", "branch", "-q", "-D", "t-003-billing")
                self.bill = self.job_tree("t-003-billing")
                self.billing_task()
                self.merged_commit(self.bill)
                saved = pa_hooks.describe_worktree, pa_hooks.MD_COMMITS
                if case == "unmerged":
                    self.commit(self.bill, "b.txt", "b\n", "not merged yet")
                elif case == "uncommitted":
                    write(os.path.join(self.bill, "u.txt"), "x\n")
                elif case == "cut":
                    self.merged_commit(self.bill, "c.txt", "second")
                    pa_hooks.MD_COMMITS = 1
                else:
                    def fake(row, facts, profile, _real=saved[0], case=case):
                        desc = _real(row, facts, profile)
                        if case == "unknown":
                            desc["repos"][0]["commits"][0]["merged"] = None
                        elif case == "error":
                            desc["repos"][0]["error"] = "git broke"
                        else:
                            desc["notDescribed"] = ["other"]
                        return desc
                    pa_hooks.describe_worktree = fake
                try:
                    code, out, err = self.check("--open-cards")
                finally:
                    pa_hooks.describe_worktree, pa_hooks.MD_COMMITS = saved
                self.assertEqual(code, 0, err)
                self.assertIn("job billing not over yet ({})".format(why), out)
                self.assertIsNone(self.card())

    def test_b4_pushed_only_is_not_enough_while_in_progress(self):
        bare = os.path.join(self.tmp, "remote.git")
        sh(self.tmp, "git", "init", "-q", "--bare", bare)
        sh(self.proj, "git", "remote", "add", "origin", bare)
        self.billing_task()
        self.commit(self.bill, "a.txt", "a\n", "pushed, not merged")
        sh(self.bill, "git", "push", "-q", "origin", "HEAD:refs/heads/t-003-billing")
        code, out, err = self.check("--open-cards")
        self.assertEqual(code, 0, err)
        self.assertIn("job billing not over yet (1 of 1 commits not merged into main)", out)
        self.assertIsNone(self.card())
        # once the task is done, rule (a): offered, and the card says it is pushed
        led = self.ledger()
        led["tasks"][0]["state"] = "done"
        self.ledger(led)
        self.assertIn("opened card d-wt-t-003-billing", self.check("--open-cards")[1])
        self.assertEqual(self.card()["rule"], "a")
        self.assertIn("(not merged, pushed)", self.card()["context"])

    def test_b5_a_terminal_inside_blocks_a_sibling_worker_does_not(self):
        self.billing_task(workers=[{"address": "t31", "role": "build", "name": "t-003-main"}])
        self.merged_commit(self.bill)
        self.term("t31", "t-003-main", cwd=self.wt)  # live, in the main worktree
        self.term("t77", "someone", cwd=os.path.join(self.bill, "sub"))
        code, out, err = self.check("--open-cards")
        self.assertEqual(code, 0, err)
        self.assertIn("job billing not over yet (another terminal is working in it: t77)", out)
        self.assertIsNone(self.card())
        del self.terms["t77"]
        self.assertIn("opened card d-wt-t-003-billing", self.check("--open-cards")[1])
        # a live worker of this job's worktree blocks it
        shutil.rmtree(os.path.join(self.dir, "decisions"))
        led = self.ledger()
        led["tasks"][0]["workers"].append({"address": "t32", "role": "build", "name": "t-003-billing", "worktree": self.bill})
        self.ledger(led)
        self.term("t32", "t-003-billing", cwd=self.tmp)
        self.assertIn("job billing not over yet (in use by t32)", self.check("--open-cards")[1])
        self.assertIsNone(self.card())

    def test_b6_open_b_card_withdrawn_on_new_commit_file_or_terminal(self):
        for case in ("commit", "file", "terminal"):
            with self.subTest(case=case):
                self.ledger({"version": 1, "tasks": []})
                self.terms.clear()
                shutil.rmtree(os.path.join(self.dir, "decisions"), ignore_errors=True)
                self.bill = self.job_tree("t-003-b6-" + case)
                self.billing_task()
                self.merged_commit(self.bill)
                self.assertIn("opened card d-wt-t-003-billing", self.check("--open-cards")[1])
                why = {"commit": "1 of 2 commits not merged into main", "file": "1 uncommitted file(s)",
                       "terminal": "another terminal is working in it: t77"}[case]
                if case == "commit":
                    self.commit(self.bill, "b.txt", "b\n", "more work")
                elif case == "file":
                    write(os.path.join(self.bill, "u.txt"), "x\n")
                else:
                    self.term("t77", "someone", cwd=self.bill)
                before = raw(os.path.join(self.dir, "decisions", "d-wt-t-003-billing.json"))
                self.assertIn("t-003: card d-wt-t-003-billing is open, but the job is no longer over ({}); --open-cards withdraws it".format(why),
                              self.check()[1])
                self.assertEqual(raw(os.path.join(self.dir, "decisions", "d-wt-t-003-billing.json")), before)
                out = self.check("--open-cards")[1]
                self.assertIn("t-003: withdrew card d-wt-t-003-billing: the job is no longer over: {}".format(why), out)
                card = self.card()
                self.assertEqual((card["status"], card["answer"]), ("withdrawn", None))
                self.assertIn("the job is no longer over: " + why, card["note"])
                self.assertEqual((self.journal()[-1]["trigger"], self.journal()[-1]["task"]), ("worktree", "t-003"))
                self.assertIn("withdrew card d-wt-t-003-billing", self.journal()[-1]["why"])
                self.assertIn("job billing not over yet ({})".format(why), self.check("--open-cards")[1])
                self.assertEqual(self.card()["status"], "withdrawn")

    def test_b7_main_worktree_of_in_progress_task_same_rule(self):
        self.add_task("t-003", "in-progress", self.wt, [{"address": "t31", "role": "build", "name": "t-003-main", "finished": True}])
        self.merged_commit(self.wt)
        code, out, err = self.check("--open-cards")
        self.assertEqual(code, 0, err)
        self.assertIn("t-003: opened card d-wt-t-003 (recommends Delete)", out)
        card = self.card("d-wt-t-003")
        self.assertEqual((card["job"], card["main"], card["rule"]), ("main", True, "b"))
        self.assertEqual(card["question"], 'Delete the worktree for t-003 "Title of t-003", job main?')
        self.assertIn("Task still in-progress; this job's commits are all merged", card["context"].split("\n"))
        self.assertEqual(card["evidence"], ["WORKTREE.md"])
        self.assertEqual(card["context"].split("\n")[-1], "Full list: WORKTREE.md")

    def test_b8_card_ids_and_md_names_per_job_old_d_wt_card_still_valid(self):
        self.assertEqual(pa_hooks.card_id("t-003", "billing", False), "d-wt-t-003-billing")
        self.assertEqual(pa_hooks.card_id("t-003", "tool", True), "d-wt-t-003")
        self.assertEqual(pa_hooks.md_name("billing", False), "WORKTREE-billing.md")
        self.assertEqual(pa_hooks.md_name("tool", True), "WORKTREE.md")
        self.assertEqual(pa_hooks.card_path(self.dir, "t-003", "billing", False), os.path.join(self.dir, "decisions", "d-wt-t-003-billing.json"))
        self.assertEqual(pa_hooks.card_path(self.dir, "t-003"), os.path.join(self.dir, "decisions", "d-wt-t-003.json"))
        self.assertTrue(pa_hooks.tool_file(pa_hooks.md_name("billing-script", False)))
        # a card from before J2 (no job, main or rule) on the main worktree still counts, for check and for remove
        self.add_task("t-003", "done", self.wt, [])
        dump({"id": "d-wt-t-003", "task": "t-003", "worktree": self.wt, "status": "answered", "answer": "Delete", "snapshot": {}},
             os.path.join(self.dir, "decisions", "d-wt-t-003.json"))
        self.assertIn("t-003: card d-wt-t-003 answered 'Delete': run `pa-worktree remove t-003`", self.check()[1])
        code, out, err = self.remove()
        self.assertEqual(code, 0, err)
        self.assertIn("(card d-wt-t-003)", out)
        self.assertFalse(os.path.isdir(self.wt))

    def test_b9_question_and_job_lines(self):
        legacy = self.job_tree("t-007b-tool")
        self.add_task("t-007", "in-progress", None, [], extraWorktrees=[legacy])
        self.merged_commit(legacy)
        self.assertIn("t-007: opened card d-wt-t-007-t-007b-tool", self.check("--open-cards")[1])
        card = self.card("d-wt-t-007-t-007b-tool")
        self.assertEqual(card["question"], 'Delete the worktree for t-007 "Title of t-007", job t-007b-tool?')
        self.assertIn("Job: t-007b-tool (named from its folder; not recorded)", card["context"].split("\n"))
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "evidence", "t-007", "WORKTREE-t-007b-tool.md")))
        # two tasks on one job worktree, both unfinished
        self.ledger({"version": 1, "tasks": []})
        shutil.rmtree(os.path.join(self.dir, "decisions"))
        self.billing_task()
        self.add_task("t-004", "review", None, [], worktrees=[{"path": self.bill, "job": "shared"}])
        self.merged_commit(self.bill)
        # a worktree two tasks record keeps rule (a): not offered while either is unfinished (spec AC7.1, VP2)
        self.assertIn("t-003: tasks still t-003 in-progress, t-004 review; asked once every task using it is done or parked; "
                      "rule (b) is only for a worktree one task records", self.check("--open-cards")[1])
        self.assertIsNone(self.card())
        # a done task's main worktree with the default job: today's question, no Job line
        self.ledger({"version": 1, "tasks": []})
        shutil.rmtree(os.path.join(self.dir, "decisions"), ignore_errors=True)
        self.add_task("t-005", "done", self.wt, [])
        self.check("--open-cards")
        card = self.card("d-wt-t-005")
        self.assertEqual((card["question"], card["rule"], card["job"], card["main"]), ('Delete the worktree for t-005 "Title of t-005"?', "a", "main", True))
        self.assertFalse(any(l.startswith("Job: ") for l in card["context"].split("\n")))

    def test_b10_remove_job_refusals(self):
        self.billing_task()
        self.merged_commit(self.bill)
        self.check("--open-cards")
        self.answer()
        before = self.snapshot()
        # unknown job
        code, _, err = self.remove("t-003", "--job", "nope")
        self.assertEqual(code, 2)
        self.assertIn("task t-003 has no worktree for job nope", err)
        # wrong card: it names another folder
        card = self.card()
        card["worktree"] = self.wt
        dump(card, os.path.join(self.dir, "decisions", "d-wt-t-003-billing.json"))
        code, _, err = self.remove("t-003", "--job", "billing")
        self.assertEqual(code, 2)
        self.assertIn("the card is for {}, not {}".format(self.wt, os.path.realpath(self.bill)), err)
        card["worktree"] = os.path.realpath(self.bill)
        dump(card, os.path.join(self.dir, "decisions", "d-wt-t-003-billing.json"))
        # not over any more: a new commit
        self.commit(self.bill, "b.txt", "b\n", "more")
        code, _, err = self.remove("t-003", "--job", "billing")
        self.assertEqual(code, 2)
        self.assertIn("tasks still t-003 in-progress and job billing is not over (1 of 2 commits not merged into main); nothing was removed", err)
        self.assertTrue(os.path.isdir(self.bill))
        # a rule (a) card while a task is unfinished: today's refusal
        sh(self.proj, "git", "merge", "-q", "--ff-only", sh(self.bill, "git", "rev-parse", "HEAD"))
        card = self.card()
        card["rule"] = "a"
        dump(card, os.path.join(self.dir, "decisions", "d-wt-t-003-billing.json"))
        code, _, err = self.remove("t-003", "--job", "billing")
        self.assertEqual(code, 2)
        self.assertIn("tasks still t-003 in-progress; a worktree is removed only once every task using it is done or parked. Nothing was removed", err)
        # several worktrees and no main one: name the job
        self.ledger({"version": 1, "tasks": []})
        other = self.job_tree("t-003-other")
        self.add_task("t-003", "done", None, [], worktrees=[{"path": self.bill, "job": "billing"}, {"path": other, "job": "other"}])
        code, _, err = self.remove("t-003")
        self.assertEqual(code, 2)
        self.assertIn("task t-003 has no main worktree; name one: --job billing, --job other", err)
        # a job's worktree outside any worktrees folder
        self.ledger({"version": 1, "tasks": []})
        loose = self.worktree(self.proj, "t-003-loose")
        self.add_task("t-003", "done", self.wt, [], worktrees=[{"path": loose, "job": "loose"}])
        self.assertIn("t-003: {} is not under a worktrees folder; not offered".format(os.path.realpath(loose)), self.check("--open-cards")[1])
        self.assertIsNone(self.card("d-wt-t-003-loose"))
        dump({"id": "d-wt-t-003-loose", "task": "t-003", "worktree": os.path.realpath(loose), "job": "loose", "main": False, "rule": "a",
              "status": "answered", "answer": "Delete", "snapshot": {}}, os.path.join(self.dir, "decisions", "d-wt-t-003-loose.json"))
        code, _, err = self.remove("t-003", "--job", "loose")
        self.assertEqual(code, 2)
        self.assertIn("{} is not under a worktrees folder; nothing was removed".format(os.path.realpath(loose)), err)
        for path in (self.bill, other, loose, self.wt):
            self.assertTrue(os.path.isdir(path))
        self.assertNotIn(["git", "worktree", "remove"], [c[:3] for c in self.calls])

    def test_b11_remove_job_removes_only_that_worktree(self):
        self.billing_task()
        self.merged_commit(self.bill)
        self.check("--open-cards")
        self.answer()
        self.assertIn("t-003: card d-wt-t-003-billing answered 'Delete': run `pa-worktree remove t-003 --job billing`; it re-checks",
                      self.check()[1])
        code, out, err = self.remove("t-003", "--job", "billing")
        self.assertEqual(code, 0, err)
        self.assertIn("Removed the worktree {}. Branch t-003-billing is kept. (card d-wt-t-003-billing)".format(os.path.realpath(self.bill)), out)
        self.assertFalse(os.path.exists(self.bill))
        self.assertTrue(os.path.isdir(self.wt))
        # one worktree and no main one: remove without --job takes it
        self.ledger({"version": 1, "tasks": []})
        other = self.job_tree("t-003-other")
        self.add_task("t-003", "done", None, [], worktrees=[{"path": other, "job": "other"}])
        self.check("--open-cards")
        self.answer("d-wt-t-003-other")
        code, out, err = self.remove("t-003")
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(other))
        self.assertTrue(os.path.isdir(self.wt))

    def forged_b_card(self, path, name="d-wt-t-003", job="main", main=True):
        """An answered rule-(b) Delete card, as if the person had answered one: remove must refuse on its own."""
        dump({"id": name, "task": "t-003", "worktree": os.path.realpath(path), "job": job, "main": main, "rule": "b",
              "status": "answered", "answer": "Delete", "snapshot": {}}, os.path.join(self.dir, "decisions", name + ".json"))

    def assert_shared_kept(self, still, path=None, name="d-wt-t-003"):
        path = path or self.wt
        code, out, err = self.check("--open-cards")
        self.assertEqual(code, 0, err)
        self.assertIn("t-003: tasks still {}; asked once every task using it is done or parked; rule (b) is only for a worktree "
                      "one task records".format(still), out)
        self.assertIsNone(self.card(name), "a shared worktree is not offered while a task using it is unfinished")
        self.forged_b_card(path, name)
        before = self.snapshot()
        code, _, err = self.remove("t-003")
        self.assertEqual(code, 2, err)
        self.assertIn("tasks still {}; a worktree is removed only once every task using it is done or parked. Nothing was removed"
                      .format(still), err)
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(self.snapshot(), before, "no task's entry is marked removed")
        self.assertNotIn(["git", "worktree", "remove"], [c[:3] for c in self.calls])

    def test_b13_shared_row_keeps_rule_a(self):
        # the reviewer's probe: t-003 done with main W; t-009 records W as job shared; one merged commit in W
        cases = [("done", "in-progress"), ("done", "review"), ("done", None),
                 ("in-progress", "in-progress"), ("in-progress", "review"), ("in-progress", None), ("in-progress", "done")]
        for mine, other in cases:
            with self.subTest(t003=mine, t009=other):
                self.ledger({"version": 1, "tasks": []})
                shutil.rmtree(os.path.join(self.dir, "decisions"), ignore_errors=True)
                self.add_task("t-003", mine, self.wt, [])
                self.add_task("t-009", other, None, [], worktrees=[{"path": self.wt, "job": "shared"}])
                self.merged_commit(self.wt)
                still = ", ".join("{} {}".format(t, pa_hooks.state_word(s)) for t, s in (("t-003", mine), ("t-009", other))
                                  if s not in ("done", "parked"))
                self.assert_shared_kept(still)

    def test_b14_old_shape_shared_main_keeps_rule_a(self):
        for other in ("in-progress", None):
            with self.subTest(t009=other):
                self.ledger({"version": 1, "tasks": []})
                shutil.rmtree(os.path.join(self.dir, "decisions"), ignore_errors=True)
                self.add_task("t-003", "done", self.wt, [])
                self.add_task("t-009", other, self.wt, [])
                self.assertNotIn("worktrees", self.ledger()["tasks"][1])
                self.merged_commit(self.wt)
                self.assert_shared_kept("t-009 " + pa_hooks.state_word(other))

    def test_b15_rule_b_card_then_a_second_task(self):
        self.billing_task()
        self.merged_commit(self.bill)
        self.assertIn("t-003: opened card d-wt-t-003-billing", self.check("--open-cards")[1])
        self.assertEqual(self.card()["rule"], "b")
        self.answer()
        # a second task records the path after the person answered
        self.add_task("t-004", "review", None, [], worktrees=[{"path": self.bill, "job": "shared"}])
        before = self.snapshot()
        code, _, err = self.remove("t-003", "--job", "billing")
        self.assertEqual(code, 2, err)
        self.assertIn("tasks still t-003 in-progress, t-004 review; a worktree is removed only once every task using it is done or "
                      "parked. Nothing was removed", err)
        self.assertTrue(os.path.isdir(self.bill))
        self.assertEqual(self.snapshot(), before)
        self.assertIn("t-003: card d-wt-t-003-billing answered 'Delete', but tasks still t-003 in-progress, t-004 review; "
                      "pa-worktree remove refuses until every task using it is done or parked", self.check()[1])
        # an open rule-(b) card: the next --open-cards withdraws it
        card = self.card()
        card.update(status="open", answer=None)
        dump(card, os.path.join(self.dir, "decisions", "d-wt-t-003-billing.json"))
        self.assertIn("t-003: card d-wt-t-003-billing is open, but tasks still t-003 in-progress, t-004 review; --open-cards withdraws it",
                      self.check()[1])
        self.assertIn("t-003: withdrew card d-wt-t-003-billing: tasks still t-003 in-progress, t-004 review", self.check("--open-cards")[1])
        self.assertEqual(self.card()["status"], "withdrawn")
        self.assertNotIn(["git", "worktree", "remove"], [c[:3] for c in self.calls])

    # ---- round 3: which tasks a worktree belongs to (REVIEW M2, VERIFY M2) ----
    def forge_card(self, path, name, job="main", main=True, rule="b", status="answered", answer="Delete", tasks=None):
        """A card as if check had opened it (and the person answered it); `tasks` None: a card from before round 3."""
        card = {"id": name, "task": "t-003", "worktree": os.path.realpath(path), "job": job, "main": main, "rule": rule,
                "status": status, "answer": answer, "snapshot": {}}
        if tasks is not None:
            card["tasks"] = tasks
        dump(card, os.path.join(self.dir, "decisions", name + ".json"))

    def reset(self):
        self.ledger({"version": 1, "tasks": []})
        shutil.rmtree(os.path.join(self.dir, "decisions"), ignore_errors=True)
        os.makedirs(os.path.join(self.dir, "decisions"))

    def assert_refused(self, still, path, name, job=None):
        """check offers no card; remove refuses a forged rule-(b) card and writes nothing."""
        code, out, err = self.check("--open-cards")
        self.assertEqual(code, 0, err)
        self.assertIn("t-003: tasks still {}; asked once every task using it is done or parked; rule (b) is only for a worktree "
                      "one task records".format(still), out)
        self.assertIsNone(self.card(name), "not offered while a task that records a path in it or around it is unfinished")
        self.forge_card(path, name, *((job, False) if job else ()))
        before = self.snapshot()
        code, _, err = self.remove("t-003", *(("--job", job) if job else ()))
        self.assertEqual(code, 2, err)
        self.assertIn("tasks still {}; a worktree is removed only once every task using it is done or parked. Nothing was removed"
                      .format(still), err)
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(self.snapshot(), before, "no task's entry is marked removed")
        self.assertNotIn(["git", "worktree", "remove"], [c[:3] for c in self.calls])

    @staticmethod
    def still_of(*pairs):
        return ", ".join("{} {}".format(t, pa_hooks.state_word(s)) for t, s in pairs if s not in ("done", "parked"))

    def test_b16_a_task_that_records_a_package_inside_the_worktree_counts_on_it(self):
        # the reviewer's P1: a profile-kind worktree K (a workspace) is t-003's main; t-009 records K/src/A (where its worker starts)
        kind = self.kind_profile(["A"], unpushed=0)
        rm = os.path.join(self.tmp, "rm.sh")
        write(rm, '#!/bin/sh\nrm -rf "$1"\n')
        os.chmod(rm, 0o755)
        prof = os.path.join(pa.ROOT, "profiles", "test2", "profile.json")
        p = load(prof)
        p["worktree"]["remove"] = rm
        dump(p, prof)
        pkg = self.package(kind, "A", 1, merged=True)
        for mine, other in (("in-progress", "in-progress"), ("done", "in-progress"), ("in-progress", None), ("done", "review")):
            with self.subTest(t003=mine, t009=other):
                self.reset()
                self.add_task("t-003", mine, kind, [])
                self.add_task("t-009", other, None, [], worktrees=[{"path": pkg, "job": "pkg"}])
                self.assert_refused(self.still_of(("t-003", mine), ("t-009", other)), kind, "d-wt-t-003")
                self.assertTrue(os.path.isdir(pkg))
        # both done: one rule-(a) card for both tasks; the removal marks t-009's package entry too
        self.reset()
        self.add_task("t-003", "done", kind, [])
        self.add_task("t-009", "done", None, [], worktrees=[{"path": pkg, "job": "pkg"}])
        self.assertIn("t-003: opened card d-wt-t-003", self.check("--open-cards")[1])
        card = self.card("d-wt-t-003")
        self.assertEqual((card["rule"], card["tasks"]), ("a", ["t-003", "t-009"]))
        self.assertEqual(card["question"], 'Delete the worktree for t-003 "Title of t-003", t-009 "Title of t-009"?')
        self.answer("d-wt-t-003")
        code, out, err = self.remove("t-003")
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(kind))
        t9 = next(t for t in self.ledger()["tasks"] if t["id"] == "t-009")
        self.assertTrue(t9["worktrees"][0].get("removed"), "t-009's entry no longer names a folder that is gone")
        self.assertEqual(pa.task_worktrees(t9), [])

    def test_b17_a_task_that_records_the_container_counts_on_the_worktree(self):
        # the reviewer's P3: t-009 records the container <tmp>/worktrees/t-003-billing that holds t-003's billing worktree
        container = os.path.dirname(self.bill)
        for mine, other in (("in-progress", "in-progress"), ("done", "in-progress"), ("in-progress", "parked"), ("done", None)):
            with self.subTest(t003=mine, t009=other):
                self.reset()
                self.billing_task(mine)
                self.add_task("t-009", other, None, [], worktrees=[{"path": container, "job": "c"}])
                self.merged_commit(self.bill)
                if other == "parked":  # t-009 is parked: the row's one unfinished task is t-003, but two tasks count on it
                    self.assert_refused("t-003 in-progress", self.bill, "d-wt-t-003-billing", job="billing")
                else:
                    self.assert_refused(self.still_of(("t-003", mine), ("t-009", other)), self.bill, "d-wt-t-003-billing", job="billing")
                self.assertTrue(os.path.isdir(container))
        self.reset()
        self.billing_task("done")
        self.add_task("t-009", "done", None, [], worktrees=[{"path": container, "job": "c"}])
        self.check("--open-cards")
        card = self.card()
        self.assertEqual((card["rule"], card["tasks"]), ("a", ["t-003", "t-009"]))
        self.answer()
        code, out, err = self.remove("t-003", "--job", "billing")
        self.assertEqual(code, 0, err)
        self.assertIn("Removed the empty folder {}.".format(container), out)
        self.assertFalse(os.path.exists(container))
        t9 = next(t for t in self.ledger()["tasks"] if t["id"] == "t-009")
        self.assertTrue(t9["worktrees"][0].get("removed"), "the container is gone: t-009's entry is marked removed")

    def test_b17b_a_container_that_stays_is_not_marked(self):
        container = os.path.dirname(self.bill)
        write(os.path.join(container, "notes.txt"), "kept\n")
        self.billing_task("done")
        self.add_task("t-009", "done", None, [], worktrees=[{"path": container, "job": "c"}])
        self.check("--open-cards")
        self.answer()
        code, out, err = self.remove("t-003", "--job", "billing")
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(self.bill))
        self.assertTrue(os.path.isdir(container))
        t9 = next(t for t in self.ledger()["tasks"] if t["id"] == "t-009")
        self.assertFalse(t9["worktrees"][0].get("removed"), "the container is still there: t-009's entry still names it")
        t3 = next(t for t in self.ledger()["tasks"] if t["id"] == "t-003")
        self.assertTrue(t3["worktrees"][0].get("removed"))

    def test_b18_an_answered_card_is_for_the_tasks_it_asked_about(self):
        # the verifier's probe2: a real rule-(b) card for t-003 alone, answered Delete; then t-004 records the path; both finish
        self.billing_task()
        self.merged_commit(self.bill)
        self.check("--open-cards")
        self.assertEqual((self.card()["rule"], self.card()["tasks"]), ("b", ["t-003"]))
        self.answer()
        self.add_task("t-004", "review", None, [], worktrees=[{"path": self.bill, "job": "shared"}])
        led = self.ledger()
        led["tasks"][0]["state"], led["tasks"][1]["state"] = "done", "parked"
        self.ledger(led)
        why = "the card was asked for t-003; the worktree is recorded by t-003, t-004 now"
        self.assertIn("t-003: card d-wt-t-003-billing answered 'Delete', but {}; pa-worktree remove refuses it and withdraws it, "
                      "and the next --open-cards asks again".format(why), self.check()[1])
        code, _, err = self.remove("t-003", "--job", "billing")
        self.assertEqual(code, 2, err)
        self.assertIn(why + "; the card is withdrawn and the next `pa-worktree check --open-cards` asks again. Nothing was removed", err)
        self.assertTrue(os.path.isdir(self.bill))
        self.assertEqual((self.card()["status"], self.card()["previousAnswer"]), ("withdrawn", "Delete"))
        self.assertNotIn(["git", "worktree", "remove"], [c[:3] for c in self.calls])
        # asked again, for both tasks, under rule (a)
        self.assertIn("t-003: opened card d-wt-t-003-billing", self.check("--open-cards")[1])
        card = self.card()
        self.assertEqual((card["rule"], card["tasks"], card["status"]), ("a", ["t-003", "t-004"], "open"))
        self.answer()
        code, out, err = self.remove("t-003", "--job", "billing")
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(self.bill))
        # the rule-(a) form (RL-V3): t-003 done with main W, answered; then t-009 (done) records W
        self.reset()
        self.add_task("t-003", "done", self.wt, [])
        self.check("--open-cards")
        self.assertEqual(self.card("d-wt-t-003")["tasks"], ["t-003"])
        self.answer("d-wt-t-003")
        self.add_task("t-009", "done", self.wt, [])
        code, _, err = self.remove("t-003")
        self.assertEqual(code, 2, err)
        self.assertIn("the card was asked for t-003; the worktree is recorded by t-003, t-009 now; the card is withdrawn", err)
        self.assertTrue(os.path.isdir(self.wt))

    def test_b19_an_open_card_whose_tasks_or_rule_no_longer_match_is_withdrawn(self):
        self.billing_task()
        self.merged_commit(self.bill)
        self.check("--open-cards")
        self.assertEqual((self.card()["rule"], self.card()["status"]), ("b", "open"))
        led = self.ledger()
        led["tasks"][0]["state"] = "done"
        self.ledger(led)
        why = ("the card was asked under rule (b), and every task using the worktree is done or parked now; it is asked again "
               "under rule (a)")
        self.assertIn("t-003: card d-wt-t-003-billing is open, but {}; --open-cards withdraws it".format(why), self.check()[1])
        self.assertIn("t-003: withdrew card d-wt-t-003-billing: " + why, self.check("--open-cards")[1])
        self.assertEqual(self.card()["status"], "withdrawn")
        self.assertIn("t-003: opened card d-wt-t-003-billing", self.check("--open-cards")[1])
        self.assertEqual((self.card()["rule"], self.card()["tasks"]), ("a", ["t-003"]))
        # an open rule-(a) card, then a second (done) task records the path
        self.add_task("t-009", "done", None, [], worktrees=[{"path": self.bill, "job": "shared"}])
        why = "the card was asked for t-003; the worktree is recorded by t-003, t-009 now"
        self.assertIn("t-003: card d-wt-t-003-billing is open, but {}; --open-cards withdraws it".format(why), self.check()[1])
        self.assertIn("t-003: withdrew card d-wt-t-003-billing: " + why, self.check("--open-cards")[1])
        self.assertIn("t-003: opened card d-wt-t-003-billing", self.check("--open-cards")[1])
        self.assertEqual(self.card()["tasks"], ["t-003", "t-009"])

    def test_b20_a_card_from_before_round_3(self):
        # no task list on it: accepted for a worktree one task records ...
        self.billing_task("done")
        self.merged_commit(self.bill)
        self.forge_card(self.bill, "d-wt-t-003-billing", "billing", False, rule="a")
        code, out, err = self.remove("t-003", "--job", "billing")
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(self.bill))
        # ... and treated as asked for other tasks when two or more record it
        self.reset()
        old = self.job_tree("t-003-old")
        self.add_task("t-003", "done", None, [], worktrees=[{"path": old, "job": "old"}])
        self.add_task("t-009", "done", None, [], worktrees=[{"path": old, "job": "shared"}])
        self.forge_card(old, "d-wt-t-003-old", "old", False, rule="a")
        why = "the card does not record which tasks it was asked for, and t-003, t-009 record the worktree now"
        self.assertIn("t-003: card d-wt-t-003-old answered 'Delete', but {}; pa-worktree remove refuses it".format(why), self.check()[1])
        code, _, err = self.remove("t-003", "--job", "old")
        self.assertEqual(code, 2, err)
        self.assertIn(why + "; the card is withdrawn", err)
        self.assertTrue(os.path.isdir(old))
        self.forge_card(old, "d-wt-t-003-old", "old", False, rule="a", status="open", answer=None)
        self.assertIn("t-003: withdrew card d-wt-t-003-old: " + why, self.check("--open-cards")[1])

    def kind_profile(self, repos, uncommitted=0, unpushed=1):
        kind = os.path.join(self.tmp, "Ws", "worktrees", "t-003-pkg")
        write(os.path.join(kind, "marker.txt"), "")
        status = os.path.join(self.tmp, "status.sh")
        write(status, "#!/bin/sh\ncat <<'JSON'\n{}\nJSON\n".format(json.dumps(
            {"uncommitted": uncommitted, "unpushed": unpushed, "branch": "A:feat, B:feat", "removesBranches": True, "kind": "custom worktree",
             "repos": [os.path.join(kind, "src", r) for r in repos]})))
        os.chmod(status, 0o755)
        dump({"name": "test2", "claude": ["true"], "codex": ["true"],
              "worktree": {"marker": "marker.txt", "status": status, "remove": status}}, os.path.join(pa.ROOT, "profiles", "test2", "profile.json"))
        agent = load(os.path.join(self.dir, "agent.json"))
        agent["profile"] = "test2"
        dump(agent, os.path.join(self.dir, "agent.json"))
        return kind

    def package(self, kind, name, commits, merged):
        folder = os.path.join(kind, "src", name)
        os.makedirs(folder)
        sh(folder, "git", "init", "-q", "-b", "main")
        self.commit(folder, "README", "base\n", "base")
        sh(folder, "git", "checkout", "-q", "-b", "feat")
        for i in range(commits):
            self.commit(folder, "f{}.txt".format(i), "x\n", "{} commit {}".format(name, i))
        if merged:  # merged into the local main branch only, on no remote
            sh(folder, "git", "checkout", "-q", "main")
            sh(folder, "git", "merge", "-q", "--ff-only", "feat")
            sh(folder, "git", "checkout", "-q", "feat")
        return folder

    def kind_card(self, kind, state="done"):
        shutil.rmtree(os.path.join(self.dir, "decisions"), ignore_errors=True)
        self.ledger({"version": 1, "tasks": []})
        self.add_task("t-003", state, kind, [])
        code, out, err = self.check("--open-cards")
        self.assertEqual(code, 0, err)
        return self.card("d-wt-t-003"), out

    def test_b12_branch_deleting_kind_lost_merged_locally_kept_unknown_keep(self):
        kind = self.kind_profile(["A", "B"], uncommitted=2)
        self.package(kind, "A", 1, merged=True)
        self.package(kind, "B", 2, merged=False)
        card, _ = self.kind_card(kind)
        lines = card["context"].split("\n")
        self.assertIn("  kept if deleted: all 1 commits merged or pushed", lines)
        self.assertIn("  LOST if deleted: 2 of 2 commits neither merged nor on a remote", lines)
        self.assertLess(lines.index("  kept if deleted: all 1 commits merged or pushed"), lines.index("  LOST if deleted: 2 of 2 commits neither merged nor on a remote"))
        self.assertIn("Deleting also deletes its branches (A:feat, B:feat); 2 commits (B) are neither merged nor on a remote and would be LOST; "
                      "the 2 uncommitted files are DISCARDED.", lines)
        self.assertEqual(card["recommendation"], "Keep")
        self.assertIn("would be LOST", self.md_text("t-003", "WORKTREE.md"))
        # every commit merged (locally): kept, Delete recommended although the script counts 1 commit on no remote
        kind = self.kind_profile(["A"])
        card, _ = self.kind_card(kind)
        self.assertIn("  kept if deleted: all 1 commits merged or pushed", card["context"].split("\n"))
        self.assertIn("Deleting also deletes its branches (A:feat, B:feat); all their commits are merged or pushed.", card["context"].split("\n"))
        self.assertEqual(card["recommendation"], "Delete")
        # a package that cannot be read: unknown, Keep
        os.makedirs(os.path.join(kind, "src", "Broken"))
        kind = self.kind_profile(["A", "Broken"])
        card, _ = self.kind_card(kind)
        lines = card["context"].split("\n")
        self.assertTrue(any(l.startswith("  unknown if deleted: ") and l.endswith("; Keep is recommended") for l in lines), lines)
        self.assertIn("Deleting also deletes its branches (A:feat, B:feat); whether every commit is merged or pushed is unknown.", lines)
        self.assertEqual(card["recommendation"], "Keep")
        # rule (b) on a worktree whose delete takes its branches: offered only with every package's commits merged
        shutil.rmtree(os.path.join(kind, "src", "Broken"))
        kind = self.kind_profile(["A"])
        card, out = self.kind_card(kind, state="in-progress")
        self.assertEqual((card["rule"], card["recommendation"]), ("b", "Delete"))
        kind = self.kind_profile(["A", "B"])
        card, out = self.kind_card(kind, state="in-progress")
        self.assertIsNone(card)
        self.assertIn("job main not over yet (2 of 2 commits not merged into main)", out)

    def md_text(self, task, name):
        with open(os.path.join(self.dir, "evidence", task, name)) as f:
            return f.read()


class CardPerWorktree(Base):
    """t-073 (R1-R4): a worktree card answers only for the worktree path (and branch) it names. An answered card for
    another path, a removed one or another branch never counts; the worktree gets its own card under a new id; remove
    refuses on such a card. The four live cases: t-020, t-032, t-037, t-046."""

    LIVE = (("t-020", "t-020-j2", "t-020-unrecorded"), ("t-032", "t-032-price-list", "t-032-price-fetch"),
            ("t-037", "t-037-pa-restart", "t-037-end-terminal"), ("t-046", "t-046-other-answer", "t-046-panel"))
    ANSWERED, REMOVED = "2026-11-01T00:00:00Z", "2026-11-02T00:00:00Z"

    def setUp(self):
        super().setUp()
        self.proj = self.repo()

    job_tree = JobCards.job_tree
    merged_commit = JobCards.merged_commit
    check = JobCards.check
    card = JobCards.card
    answer = JobCards.answer

    def gone(self, name):
        """A worktree path that was removed: recorded removed in the ledger, no folder."""
        return os.path.realpath(self.tmp) + "/worktrees/{}/proj".format(name)

    def dec(self, name, folder="decisions"):
        return os.path.join(self.dir, folder, name + ".json")

    def old_card(self, cid, task, worktree, answer="Delete", folder="decisions", **extra):
        card = dict({"id": cid, "task": task, "worktree": worktree, "job": "main", "main": True, "rule": "b", "tasks": [task],
                     "status": "answered", "answer": answer, "note": None, "answered": self.ANSWERED, "snapshot": {}}, **extra)
        dump(card, self.dec(cid, folder))
        return raw(self.dec(cid, folder))

    def live_case(self, task, old, new, folder="decisions"):
        """A task in progress whose old worktree is removed and whose `worktree` names a new one with a merged commit;
        its old card d-wt-<task> answered Delete for the old path. Returns (new path, old card bytes)."""
        path = self.job_tree(new)
        self.merged_commit(path)
        self.add_task(task, "in-progress", path, [], worktrees=[{"path": self.gone(old), "job": "j1", "removed": self.REMOVED}])
        return path, self.old_card("d-wt-" + task, task, self.gone(old), folder=folder)

    def remove(self, task, *extra):
        return self.run_cmd("pa-worktree", "remove", task, "--space", "sp1", *extra)

    def test_k1_live_cases_old_delete_is_not_the_new_worktrees(self):
        made = {task: self.live_case(task, old, new) for task, old, new in self.LIVE}
        code, out, err = self.check()
        self.assertEqual(code, 0, err)
        for task, _old, _new in self.LIVE:
            self.assertNotIn("{}: card d-wt-{} answered".format(task, task), out)
            self.assertIn("{}: candidate, job main is over (every commit merged); --open-cards asks the person".format(task), out)
        code, out, err = self.check("--open-cards")
        self.assertEqual(code, 0, err)
        for task, _old, _new in self.LIVE:
            with self.subTest(task=task):
                path, before = made[task]
                self.assertIn("{}: opened card d-wt-{}-main (recommends Delete)".format(task, task), out)
                card = self.card("d-wt-{}-main".format(task))
                self.assertEqual((card["id"], card["task"], card["worktree"], card["job"], card["main"], card["rule"], card["status"]),
                                 ("d-wt-{}-main".format(task), task, os.path.realpath(path), "main", True, "b", "open"))
                self.assertEqual(card["evidence"], ["WORKTREE-main.md"])
                self.assertEqual(raw(self.dec("d-wt-" + task)), before, "the old card is never touched")
        self.assertIn("card d-wt-t-020-main is open", self.check("--open-cards")[1])

    def test_k2_remove_refuses_an_old_delete_for_another_path(self):
        for task, old, new in self.LIVE:
            with self.subTest(task=task):
                path, before = self.live_case(task, old, new)
                ledger = self.snapshot()
                code, _, err = self.remove(task)
                self.assertEqual(code, 2, err)
                self.assertIn("the card is for {}, not {} (card d-wt-{} answered 'Delete')".format(self.gone(old), os.path.realpath(path), task), err)
                self.assertIn("gets its own card from `pa-worktree check --open-cards`", err)
                self.assertIn("nothing was removed", err)
                self.assertTrue(os.path.isdir(path))
                self.assertEqual(self.snapshot(), ledger)
                self.assertEqual(raw(self.dec("d-wt-" + task)), before)
        self.assertNotIn(["git", "worktree", "remove"], [c[:3] for c in self.calls])

    def test_k3_new_card_answered_delete_removes_only_its_worktree(self):
        path, before = self.live_case("t-032", "t-032-price-list", "t-032-price-fetch")
        self.assertIn("t-032: opened card d-wt-t-032-main", self.check("--open-cards")[1])
        self.answer("d-wt-t-032-main")
        code, out, err = self.remove("t-032")
        self.assertEqual(code, 0, err)
        self.assertIn("(card d-wt-t-032-main)", out)
        self.assertFalse(os.path.isdir(path))
        task = self.ledger()["tasks"][0]
        self.assertIsNone(task["worktree"])
        self.assertTrue(any(e.get("removed") and os.path.realpath(e["path"]) == os.path.realpath(path) for e in task["worktrees"]))
        self.assertTrue(self.card("d-wt-t-032-main").get("worktreeRemoved"), "a removal marks its card spent")
        self.assertEqual(raw(self.dec("d-wt-t-032")), before)

    def test_k4_t046_each_job_worktree_on_its_own_card(self):
        panel = self.job_tree("t-046-panel")  # J3: no commits yet
        core = self.job_tree("t-046-core-text")  # J2: merged
        self.merged_commit(core)
        self.add_task("t-046", "in-progress", panel, [], worktrees=[
            {"path": self.gone("t-046-other-answer"), "job": "J1", "removed": self.REMOVED}, {"path": core, "job": "J2"}])
        before = self.old_card("d-wt-t-046", "t-046", self.gone("t-046-other-answer"))
        code, out, err = self.check("--open-cards")
        self.assertEqual(code, 0, err)
        self.assertNotIn("t-046: card d-wt-t-046 answered", out)
        self.assertIn("t-046: opened card d-wt-t-046-j2 (recommends Delete)", out)
        self.assertIn("t-046: tasks still t-046 in-progress; asked once every task using it is done or parked; job main not over yet "
                      "(no commits since its fork)", out)
        self.assertEqual(self.card("d-wt-t-046-j2")["worktree"], os.path.realpath(core))
        self.assertIsNone(self.card("d-wt-t-046-main"))
        self.assertEqual(raw(self.dec("d-wt-t-046")), before)

    def test_k5_one_push_offers_every_finished_job(self):
        t063 = self.job_tree("t-063-x")
        self.merged_commit(t063)
        self.add_task("t-063", "done", t063, [])
        fetch = self.job_tree("t-032-price-fetch")
        self.merged_commit(fetch)
        self.add_task("t-032", "in-progress", fetch, [], worktrees=[{"path": self.gone("t-032-price-list"), "job": "j1", "removed": self.REMOVED}])
        self.old_card("d-wt-t-032", "t-032", self.gone("t-032-price-list"))
        panel, core = self.job_tree("t-046-panel"), self.job_tree("t-046-core-text")
        self.merged_commit(core)
        self.add_task("t-046", "in-progress", panel, [], worktrees=[{"path": core, "job": "J2"}])
        out = self.check("--open-cards")[1]
        for cid, path in (("d-wt-t-063", t063), ("d-wt-t-032-main", fetch), ("d-wt-t-046-j2", core)):
            with self.subTest(card=cid):
                self.assertIn("opened card {} (recommends Delete)".format(cid), out)
                self.assertEqual(self.card(cid)["worktree"], os.path.realpath(path))

    def test_k6_same_folder_made_again_old_card_is_spent(self):
        path = self.job_tree("t-003-again")
        self.merged_commit(path)
        # the card was answered for the worktree that was here before; the ledger says it was removed after that answer
        self.add_task("t-003", "in-progress", path, [], worktrees=[{"path": path, "job": "j1", "removed": self.REMOVED}])
        before = self.old_card("d-wt-t-003", "t-003", os.path.realpath(path))
        code, out, err = self.check()
        self.assertEqual(code, 0, err)
        self.assertNotIn("t-003: card d-wt-t-003 answered", out)
        code, _, err = self.remove("t-003")
        self.assertEqual(code, 2, err)
        self.assertTrue(os.path.isdir(path), "an answer given before the folder was made again never removes it")
        self.assertIn("t-003: opened card d-wt-t-003-j1 (recommends Delete)", self.check("--open-cards")[1])
        self.assertEqual(self.card("d-wt-t-003-j1")["worktree"], os.path.realpath(path))
        self.assertEqual(raw(self.dec("d-wt-t-003")), before)

    def test_k7_a_card_marked_removed_never_counts(self):
        path = self.job_tree("t-003-x")
        self.add_task("t-003", "done", path, [])
        before = self.old_card("d-wt-t-003", "t-003", os.path.realpath(path), rule="a", worktreeRemoved=self.REMOVED)
        self.assertNotIn("t-003: card d-wt-t-003 answered", self.check()[1])
        code, _, err = self.remove("t-003")
        self.assertEqual(code, 2, err)
        self.assertTrue(os.path.isdir(path))
        self.assertIn("t-003: opened card d-wt-t-003-main", self.check("--open-cards")[1])
        self.assertEqual(raw(self.dec("d-wt-t-003")), before)

    def test_k8_a_card_for_another_branch_never_counts(self):
        path = self.job_tree("t-003-x")
        self.add_task("t-003", "done", path, [])
        before = self.old_card("d-wt-t-003", "t-003", os.path.realpath(path), rule="a", branch="t-003-before")
        self.assertNotIn("t-003: card d-wt-t-003 answered", self.check()[1])
        code, _, err = self.remove("t-003")
        self.assertEqual(code, 2, err)
        self.assertTrue(os.path.isdir(path))
        self.assertIn("t-003: opened card d-wt-t-003-main", self.check("--open-cards")[1])
        self.assertEqual(self.card("d-wt-t-003-main")["branch"], "t-003-x")
        self.assertEqual(raw(self.dec("d-wt-t-003")), before)

    def test_k9_open_card_for_a_path_no_task_records_is_withdrawn(self):
        path = self.job_tree("t-003-x")
        self.add_task("t-003", "in-progress", path, [], worktrees=[{"path": self.gone("t-003-old"), "job": "old", "removed": self.REMOVED}])
        self.old_card("d-wt-t-003-old", "t-003", self.gone("t-003-old"), status="open", answer=None, answered=None, job="old", main=False)
        answered = self.old_card("d-wt-t-003-older", "t-003", self.gone("t-003-older"), job="older", main=False)
        before = raw(self.dec("d-wt-t-003-old"))
        code, out, err = self.check()
        self.assertEqual(code, 0, err)
        self.assertIn("t-003: card d-wt-t-003-old is open, but its worktree {} is no longer recorded; --open-cards withdraws it".format(
            self.gone("t-003-old")), out)
        self.assertEqual(raw(self.dec("d-wt-t-003-old")), before, "without --open-cards nothing is written")
        code, out, err = self.check("--open-cards")
        self.assertEqual(code, 0, err)
        self.assertIn("t-003: withdrew card d-wt-t-003-old: its worktree {} is no longer recorded".format(self.gone("t-003-old")), out)
        self.assertEqual(self.card("d-wt-t-003-old")["status"], "withdrawn")
        self.assertIn("withdrew card d-wt-t-003-old", self.journal()[-1]["why"])
        self.assertEqual(raw(self.dec("d-wt-t-003-older")), answered, "an answered card is never touched")

    def test_k10_a_new_id_never_overwrites_another_paths_card(self):
        path = self.job_tree("t-003-x")
        self.add_task("t-003", "done", path, [])
        first = self.old_card("d-wt-t-003", "t-003", self.gone("t-003-a"), answer="Keep", rule="a")
        second = self.old_card("d-wt-t-003-main", "t-003", self.gone("t-003-b"), rule="a")
        md1 = self.evidence("t-003", "WORKTREE.md", "old one\n")
        md2 = self.evidence("t-003", "WORKTREE-main.md", "old two\n")
        out = self.check("--open-cards")[1]
        self.assertIn("t-003: opened card d-wt-t-003-main-2 (recommends Delete)", out)
        card = self.card("d-wt-t-003-main-2")
        self.assertEqual((card["worktree"], card["evidence"]), (os.path.realpath(path), ["WORKTREE-main-2.md"]))
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "evidence", "t-003", "WORKTREE-main-2.md")))
        self.assertEqual((raw(self.dec("d-wt-t-003")), raw(self.dec("d-wt-t-003-main"))), (first, second))
        self.assertEqual((raw(md1), raw(md2)), (b"old one\n", b"old two\n"))

    def test_k11_an_archived_old_card_is_not_the_new_worktrees_either(self):
        path, before = self.live_case("t-020", "t-020-j2", "t-020-unrecorded", folder=pa.ARCHIVE)
        out = self.check("--open-cards")[1]
        self.assertNotIn("t-020: card d-wt-t-020 answered", out)
        self.assertIn("t-020: opened card d-wt-t-020-main", out)
        self.assertTrue(os.path.isfile(self.dec("d-wt-t-020-main")))
        self.assertEqual(raw(self.dec("d-wt-t-020", pa.ARCHIVE)), before)
        self.assertFalse(os.path.exists(self.dec("d-wt-t-020")), "the archived card stays in the archive")

    def test_k12_a_withdrawn_card_for_the_same_worktree_keeps_its_id(self):  # a guard: passes before the fix too
        path = self.job_tree("t-003-x")
        self.add_task("t-003", "done", path, [])
        self.old_card("d-wt-t-003", "t-003", os.path.realpath(path), rule="a", status="withdrawn", answer=None,
                      previousAnswer="Delete", previousAnswered=self.ANSWERED)
        self.assertIn("t-003: opened card d-wt-t-003 (recommends Delete)", self.check("--open-cards")[1])
        card = self.card("d-wt-t-003")
        self.assertEqual((card["status"], card["worktree"], card["previousAnswer"]), ("open", os.path.realpath(path), "Delete"))


    def test_k13_an_open_card_of_a_task_no_longer_the_owner_is_withdrawn(self):  # t-131 (t-073 RL4)
        path = os.path.realpath(self.job_tree("t-003-x"))
        self.add_task("t-003", "done", path, [])
        self.add_task("t-004", "done", None, [], worktrees=[{"path": path, "job": "x"}])
        self.old_card("d-wt-t-004-x", "t-004", path, status="open", answer=None, answered=None, job="x", main=False,
                      created=self.ANSWERED)
        before = raw(self.dec("d-wt-t-004-x"))
        code, out, err = self.check()
        self.assertEqual(code, 0, err)
        self.assertIn("t-004: card d-wt-t-004-x is open, but its worktree's card is now t-003's; --open-cards withdraws it", out)
        self.assertEqual(raw(self.dec("d-wt-t-004-x")), before, "without --open-cards nothing is written")
        code, out, err = self.check("--open-cards")
        self.assertEqual(code, 0, err)
        self.assertIn("t-003: opened card d-wt-t-003 (recommends Delete)", out)
        self.assertIn("t-004: withdrew card d-wt-t-004-x: its worktree's card is now t-003's", out)
        self.assertEqual(self.card("d-wt-t-004-x")["status"], "withdrawn")
        self.assertEqual(self.card("d-wt-t-003")["status"], "open", "the owner's own card stays")
        self.assertIn("card d-wt-t-003 is open", self.check("--open-cards")[1])

    def test_k14_card_folders_listed_once_per_check(self):  # t-131 (t-073 RL5)
        for n in range(3, 7):
            self.add_task("t-00{}".format(n), "in-progress", self.job_tree("t-00{}-x".format(n)), [])
        for sub in ("decisions", pa.ARCHIVE):
            os.makedirs(os.path.join(self.dir, sub), exist_ok=True)
        self.old_card("d-001", "t-003", "/elsewhere")
        lists, real = [], os.listdir

        def counting(path="."):
            if os.path.basename(str(path)) in ("decisions", pa.ARCHIVE):
                lists.append(os.path.basename(str(path)))
            return real(path)
        os.listdir = counting
        try:
            code, _out, err = self.check()
        finally:
            os.listdir = real
        self.assertEqual(code, 0, err)
        self.assertLessEqual(lists.count("decisions"), 2, "once for the worktree cards, once for the unrecorded ones (got {})".format(lists))

    def test_k15_the_path_rule_alone_refuses_an_old_card(self):  # t-131 (t-073 VERIFY RL1): no ledger `removed` entry
        for task, old, new in self.LIVE:
            with self.subTest(task=task):
                path = self.job_tree(new)
                self.merged_commit(path)
                self.add_task(task, "in-progress", path, [])
                before = self.old_card("d-wt-" + task, task, self.gone(old))
                code, out, err = self.check()
                self.assertEqual(code, 0, err)
                self.assertNotIn("{}: card d-wt-{} answered".format(task, task), out)
                code, _, err = self.remove(task)
                self.assertEqual(code, 2, err)
                self.assertIn("the card is for {}, not {}".format(self.gone(old), os.path.realpath(path)), err)
                self.assertTrue(os.path.isdir(path))
                self.assertEqual(raw(self.dec("d-wt-" + task)), before)

    def test_k16_removed_stamps_compare_as_times(self):  # t-131 (t-073 VERIFY Nit 1)
        for n, removed in enumerate(("2026-11-01", "2026-11-01T05:00:00-05:00")):
            with self.subTest(removed=removed):
                task = "t-02{}".format(n)
                path = self.job_tree("{}-again".format(task))
                self.merged_commit(path)
                self.add_task(task, "in-progress", path, [], worktrees=[{"path": path, "job": "j1", "removed": removed}])
                self.old_card("d-wt-" + task, task, os.path.realpath(path), answered="2026-11-01T09:00:00Z")
                self.assertNotIn("{}: card d-wt-{} answered".format(task, task), self.check()[1],
                                 "removed on the answer's day (or after it, in another zone): the card is spent")
                code, _, err = self.remove(task)
                self.assertEqual(code, 2, err)
                self.assertTrue(os.path.isdir(path))
        card = {"answered": "2026-11-01T09:00:00Z", "worktree": "/x"}
        for removed, spent in (("2026-10-31", False), ("2026-10-31T23:00:00-11:00", True), (True, True), ("soon", True)):
            with self.subTest(removed=removed):
                got = pa_hooks.card_spent(card, {"worktrees": [{"path": "/x", "removed": removed}]})
                self.assertEqual(bool(got), spent, got)


class Containers(Base):
    """t-020 J3 (AC6.2, AC6.3): removing a worktree also removes the empty container folder it leaves
    (<project>/worktrees/<container>), and nothing else."""

    def setUp(self):
        super().setUp()
        self.proj = self.repo()
        self.project = os.path.join(self.tmp, "Project")
        self.container = os.path.join(self.project, "worktrees", "t-003-x")
        self.wt = os.path.join(self.container, "proj")
        sh(self.proj, "git", "worktree", "add", "-q", "-b", "t-003-x", self.wt, "main")

    def removed_by_card(self, task="t-003"):
        code, out, err = self.run_cmd("pa-worktree", "check", "--open-cards", "--space", "sp1")
        self.assertEqual(code, 0, err)
        path = os.path.join(self.dir, "decisions", "d-wt-{}.json".format(task))
        card = load(path)
        card.update(status="answered", answer="Delete")
        dump(card, path)
        return self.run_cmd("pa-worktree", "remove", task, "--space", "sp1")

    def test_k1_remove_takes_the_empty_container(self):
        self.add_task("t-003", "done", self.wt, [])
        code, out, err = self.removed_by_card()
        self.assertEqual(code, 0, err)
        self.assertIn("Removed the worktree {}".format(os.path.realpath(self.wt)), out)
        self.assertIn("Removed the empty folder {}.".format(self.container), out)
        self.assertFalse(os.path.exists(self.container))
        self.assertTrue(os.path.isdir(os.path.join(self.project, "worktrees")), "the worktrees folder itself stays")
        self.assertTrue(os.path.isdir(self.project))

    def test_k2_non_empty_container_kept_and_named(self):
        for name in ("other-repo", "b.txt", "c.txt", "d.txt"):
            write(os.path.join(self.container, name, "x") if name == "other-repo" else os.path.join(self.container, name), "x\n")
        self.add_task("t-003", "done", self.wt, [])
        code, out, err = self.removed_by_card()
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(self.wt))
        self.assertIn("kept {}: not empty (b.txt, c.txt, d.txt, …)".format(self.container), out)
        self.assertEqual(sorted(os.listdir(self.container)), ["b.txt", "c.txt", "d.txt", "other-repo"])
        self.assertTrue(os.path.isfile(os.path.join(self.container, "other-repo", "x")))

    def test_k3_no_container_outside_worktrees_or_for_a_workspace_layout(self):
        # a worktree directly in a folder that is not a container under `worktrees`
        loose = os.path.join(self.tmp, "loose", "wt")
        sh(self.proj, "git", "worktree", "add", "-q", "-b", "loose", loose, "main")
        self.add_task("t-003", "done", loose, [])
        code, out, err = self.removed_by_card()
        self.assertEqual(code, 0, err)
        self.assertNotIn("empty folder", out)
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "loose")), "a parent outside a worktrees folder is never removed")
        # the function alone: a workspace's worktree (<ws>/worktrees/<name>) has no container; nothing above it goes
        ws = os.path.join(self.tmp, "Ws", "worktrees")
        os.makedirs(ws)
        self.assertIsNone(pa_hooks.remove_container(os.path.join(ws, "t-003-pkg")))
        self.assertTrue(os.path.isdir(ws))
        # a `worktrees` folder inside a `worktrees` folder is never taken for a container
        nested = os.path.join(self.tmp, "N", "worktrees", "worktrees")
        os.makedirs(nested)
        self.assertIsNone(pa_hooks.remove_container(os.path.join(nested, "name")))
        self.assertTrue(os.path.isdir(nested))
        # a container that is not a folder, or is gone: nothing
        self.assertIsNone(pa_hooks.remove_container(os.path.join(self.tmp, "Gone", "worktrees", "c", "repo")))

    def test_k4_symlinked_container_kept(self):
        real = os.path.join(self.tmp, "real-container")
        os.makedirs(real)
        link = os.path.join(self.project, "worktrees", "t-003-link")
        os.symlink(real, link)
        self.assertIsNone(pa_hooks.remove_container(os.path.join(link, "proj")))
        self.assertTrue(os.path.islink(link))
        self.assertTrue(os.path.isdir(real))
        # a container reached through a symlinked `worktrees` folder is not removed either
        elsewhere = os.path.join(self.tmp, "Else", "worktrees")
        os.makedirs(os.path.join(elsewhere, "c"))
        os.symlink(elsewhere, os.path.join(self.tmp, "Linked"))
        self.assertIsNone(pa_hooks.remove_container(os.path.join(self.tmp, "Linked", "c", "proj")))
        self.assertTrue(os.path.isdir(os.path.join(elsewhere, "c")))

    def test_k5_worker_delete_same_cleanup(self):
        self.add_task("t-003", "done", self.wt, [{"address": "t31", "role": "build", "name": "t-003-x"}])
        code, out, err = self.worker("delete", "t31")
        self.assertEqual(code, 0, err)
        self.assertIn("Removed the worktree {}".format(os.path.realpath(self.wt)), out)
        self.assertIn("Removed the empty folder {}.".format(self.container), out)
        self.assertFalse(os.path.exists(self.container))
        self.assertTrue(os.path.isdir(os.path.join(self.project, "worktrees")))
        # kept when the worktree is kept
        other = os.path.join(self.project, "worktrees", "t-004-y", "proj")
        sh(self.proj, "git", "worktree", "add", "-q", "-b", "t-004-y", other, "main")
        write(os.path.join(other, "unsaved.txt"), "x\n")
        self.add_task("t-004", "done", other, [{"address": "t41", "role": "build", "name": "t-004-y"}])
        code, out, err = self.worker("delete", "t41")
        self.assertEqual(code, 0, err)
        self.assertNotIn("empty folder", out)
        self.assertTrue(os.path.isdir(other))


class D094Limits(Base):
    """t-020, card d-094 (the person's limits to J2): an old-shape shared main worktree follows rule (a) too; pa-worktree remove
    marks the worktree removed on every task that records it; a failed removal still writes the ledger consistently; and the
    gaps J1's checkers named (a field on a shared main, a field with a trailing slash or through a symlink, a task with no state)."""

    def setUp(self):
        super().setUp()
        self.proj = self.repo()
        self.wt = self.worktree(self.proj, "t-003-x")

    def answered_main_card(self, task="t-003"):
        code, out, err = self.run_cmd("pa-worktree", "check", "--open-cards", "--space", "sp1")
        self.assertEqual(code, 0, err)
        path = os.path.join(self.dir, "decisions", "d-wt-{}.json".format(task))
        card = load(path)
        card.update(status="answered", answer="Delete")
        dump(card, path)

    def test_l2_worktree_remove_marks_every_task_that_records_it(self):
        self.add_task("t-003", "done", self.wt, [])
        self.add_task("t-004", "parked", self.wt, [])
        self.add_task("t-005", "done", None, [], worktrees=[{"path": self.wt, "job": "shared"}])
        self.answered_main_card()
        code, out, err = self.run_cmd("pa-worktree", "remove", "t-003", "--space", "sp1")
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(self.wt))
        led = self.ledger()
        for task in led["tasks"]:
            self.assertEqual(pa.task_worktrees(task), [], "no ledger entry names the removed folder ({})".format(task["id"]))
        self.assertEqual([t["worktree"] for t in led["tasks"]], [None, None, None])
        self.assertTrue(led["tasks"][2]["worktrees"][0]["removed"])
        self.assertEqual([e["job"] for e in led["tasks"][0]["worktrees"]], ["main"])
        line = self.journal()[-1]
        self.assertEqual((line["trigger"], line["task"]), ("worktree", "t-003"))
        self.assertIn("removed the worktree {} (card d-wt-t-003)".format(os.path.realpath(self.wt)), line["why"])

    def failing_kind(self, removes_folder):
        kind = os.path.join(self.tmp, "kind-wt")
        write(os.path.join(kind, "marker.txt"), "")
        status, remove = os.path.join(self.tmp, "status.sh"), os.path.join(self.tmp, "remove.sh")
        write(status, "#!/bin/sh\necho '{\"uncommitted\": 0, \"unpushed\": 0, \"branch\": \"b\", \"kind\": \"custom worktree\"}'\n")
        write(remove, "#!/bin/sh\n{}echo 'remove broke halfway'\nexit 1\n".format('rm -rf "$1"\n' if removes_folder else ""))
        for f in (status, remove):
            os.chmod(f, 0o755)
        dump({"name": "test2", "claude": ["true"], "codex": ["true"],
              "worktree": {"marker": "marker.txt", "status": status, "remove": remove}}, os.path.join(pa.ROOT, "profiles", "test2", "profile.json"))
        agent = load(os.path.join(self.dir, "agent.json"))
        agent["profile"] = "test2"
        dump(agent, os.path.join(self.dir, "agent.json"))
        return kind

    def test_l3_failed_removal_in_delete_still_writes_the_ledger(self):
        for removes_folder in (False, True):
            with self.subTest(removes_folder=removes_folder):
                self.ledger({"version": 1, "tasks": []})
                shutil.rmtree(os.path.join(self.tmp, "kind-wt"), ignore_errors=True)
                kind = self.failing_kind(removes_folder)
                self.add_task("t-003", "done", kind, [{"address": "t31", "role": "build", "name": "a"},
                                                      {"address": "t32", "role": "build", "name": "b", "finished": True}])
                self.term("t31", "a", cwd=self.tmp)
                code, out, err = self.worker("delete", "t31")
                self.assertEqual(code, 2, (out, err))
                self.assertIn("Ended and removed worker t31 (a).", out)
                self.assertIn("Could not remove the worktree {}: ".format(os.path.realpath(kind)), out)
                self.assertIn("remove broke halfway", out)
                self.assertIn("the worker is ended and the ledger updated", err)
                task = self.ledger()["tasks"][0]
                self.assertEqual([w["address"] for w in task["workers"]], ["t32"], "the ended worker is not left listed")
                if removes_folder:
                    self.assertIsNone(task["worktree"], "a folder that is gone is marked removed")
                    self.assertTrue(task["worktrees"][0]["removed"])
                else:
                    self.assertEqual(task["worktree"], kind, "a folder still there is not marked removed")
                    self.assertNotIn("worktrees", task)
                self.assertIn("the person deleted worker t31", self.journal()[-1]["why"])

    def test_l3b_failed_removal_in_worktree_remove_marks_a_gone_folder(self):
        kind = self.failing_kind(True)
        self.add_task("t-003", "done", kind, [])
        self.add_task("t-004", "done", kind, [])
        self.answered_main_card()
        code, out, err = self.run_cmd("pa-worktree", "remove", "t-003", "--space", "sp1")
        self.assertEqual(code, 2)
        self.assertIn("remove broke halfway", err)
        self.assertFalse(os.path.exists(kind))
        self.assertEqual([t["worktree"] for t in self.ledger()["tasks"]], [None, None])

    def test_l4a_field_on_a_main_shared_with_an_unfinished_task(self):
        self.add_task("t-009", "in-progress", self.wt, [])
        self.add_task("t-003", "done", self.wt, [{"address": "t31", "role": "build", "name": "a", "worktree": self.wt}])
        before = json.dumps(self.ledger()["tasks"][0], sort_keys=True)
        code, out, err = self.worker("delete", "t31")
        self.assertEqual(code, 0, err)
        self.assertIn("Kept the worktree: still used by task t-009 (in-progress).", out)
        self.assertTrue(os.path.isdir(self.wt))
        led = self.ledger()
        self.assertEqual(json.dumps(led["tasks"][0], sort_keys=True), before)
        self.assertEqual((led["tasks"][1]["worktree"], led["tasks"][1]["workers"]), (self.wt, []))

    def test_l4b_field_with_a_trailing_slash_or_through_a_symlink(self):
        billing = self.worktree(self.proj, "t-003-billing")
        link = os.path.join(self.tmp, "link-to-billing")
        os.symlink(billing, link)
        for label, field in (("slash", billing + "/"), ("symlink", link), ("symlink, slash", link + "/")):
            with self.subTest(field=label):
                self.ledger({"version": 1, "tasks": []})
                self.add_task("t-003", "done", self.wt, [{"address": "t32", "role": "build", "name": "b", "worktree": field}],
                              worktrees=[{"path": billing, "job": "billing"}])
                task = self.ledger()["tasks"][0]
                self.assertEqual(pa_hooks.worker_tree(task, task["workers"][0]), os.path.realpath(billing))
                self.assertEqual(pa_hooks.uncommitted_in(task, None, task["workers"][0])[0], os.path.realpath(billing))
                facts, _row = pa_hooks.worker_facts(self.dir, {"terminal": "t0"}, None, "t32")
                self.assertEqual(facts["worktree"]["path"], os.path.realpath(billing))
        code, out, err = self.worker("delete", "t32")
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(billing))
        self.assertTrue(os.path.isdir(self.wt), "the main worktree is not the worker's")
        task = self.ledger()["tasks"][0]
        self.assertEqual(task["worktree"], self.wt)
        self.assertTrue(task["worktrees"][0]["removed"])

    def test_l4c_a_task_with_no_state_counts_as_unfinished(self):
        self.add_task("t-009", "in-progress", self.wt, [])
        self.add_task("t-003", "done", self.wt, [{"address": "t31", "role": "build", "name": "a"}])
        led = self.ledger()
        del led["tasks"][0]["state"]
        self.ledger(led)
        code, out, _ = self.run_cmd("pa-worktree", "check", "--open-cards", "--space", "sp1")
        self.assertIn("t-009: tasks still t-009 no state; asked once every task using it is done or parked", out)
        code, out, err = self.worker("delete", "t31")
        self.assertEqual(code, 0, err)
        self.assertIn("Kept the worktree: still used by task t-009 (no state).", out)
        self.assertTrue(os.path.isdir(self.wt))


class WorkerDeleteCounts(Base):
    """t-048 (t-020 RL-V5): pa-worker delete counts the tasks on a worktree the way pa-worktree does (row_tasks: the
    worktree, a folder inside it, its container): an unfinished one keeps it; a removal marks their entries once gone."""
    job_tree, reset = JobCards.job_tree, JobCards.reset  # not a subclass: JobCards' own tests would run again

    def setUp(self):
        super().setUp()
        self.proj = self.repo()

    def setup_case(self, name, t009, where, extra_file=False):
        """t-003 (done) has the worktree <tmp>/worktrees/<name>/proj as its main, its worker w-1 finished; t-009 (t009)
        records a folder inside it ("inside") or its container ("container")."""
        self.reset()
        self.terms.clear()
        bill = self.job_tree(name)
        container = os.path.dirname(bill)
        inner = os.path.join(bill, "sub")
        os.makedirs(inner)
        if extra_file:
            write(os.path.join(container, "notes.txt"), "kept\n")
        path = inner if where == "inside" else container
        self.add_task("t-003", "done", bill, [{"address": "w-1", "role": "build", "name": "t-003 build", "finished": True}])
        self.add_task("t-009", t009, None, [], worktrees=[{"path": path, "job": "o"}])
        return bill, container, path

    def t009(self):
        return next(t for t in self.ledger()["tasks"] if t["id"] == "t-009")

    def assert_kept(self, bill, path, state):
        before = json.dumps(self.t009(), sort_keys=True)
        facts, _row = pa_hooks.worker_facts(self.dir, {"terminal": "t0"}, None, "w-1")
        self.assertEqual(facts["worktree"]["heldBy"], ["t-009"])
        self.assertEqual(facts["worktree"]["sharedWith"], ["task t-009 ({})".format(pa_hooks.state_word(state))])
        code, out, err = self.worker("delete", "w-1")
        self.assertEqual(code, 0, err)
        self.assertIn("Kept the worktree: still used by task t-009 ({}).".format(pa_hooks.state_word(state)), out)
        self.assertNotIn("Removed the worktree", out)
        self.assertTrue(os.path.isdir(bill))
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(json.dumps(self.t009(), sort_keys=True), before, "t-009's entry is untouched")
        t3 = next(t for t in self.ledger()["tasks"] if t["id"] == "t-003")
        self.assertEqual((t3["worktree"], t3["workers"]), (bill, []))

    def test_d1_an_unfinished_task_inside_the_worktree_keeps_it(self):  # RL-V5's case
        for state in ("in-progress", "review", None):
            with self.subTest(t009=state):
                bill, _c, inner = self.setup_case("t-003-d1-{}".format(state), state, "inside")
                self.assert_kept(bill, inner, state)

    def test_d2_a_finished_task_inside_is_marked_with_the_removal(self):
        for state in ("done", "parked"):
            with self.subTest(t009=state):
                bill, container, inner = self.setup_case("t-003-d2-" + state, state, "inside")
                code, out, err = self.worker("delete", "w-1")
                self.assertEqual(code, 0, err)
                self.assertIn("Removed the worktree {}".format(os.path.realpath(bill)), out)
                self.assertFalse(os.path.exists(bill))
                t9 = self.t009()
                self.assertTrue(t9["worktrees"][0].get("removed"), "the folder inside is gone: t-009's entry is marked")
                self.assertEqual(pa.task_worktrees(t9), [])
                t3 = next(t for t in self.ledger()["tasks"] if t["id"] == "t-003")
                self.assertIsNone(t3["worktree"])

    def test_d3_a_task_that_records_the_container(self):
        bill, container, _p = self.setup_case("t-003-d3-a", "in-progress", "container")
        self.assert_kept(bill, container, "in-progress")
        bill, container, _p = self.setup_case("t-003-d3-b", "done", "container")
        code, out, err = self.worker("delete", "w-1")
        self.assertEqual(code, 0, err)
        self.assertIn("Removed the empty folder {}.".format(container), out)
        self.assertFalse(os.path.exists(container))
        self.assertTrue(self.t009()["worktrees"][0].get("removed"), "the container is gone: t-009's entry is marked")

    def test_d3b_a_container_that_stays_is_not_marked(self):
        bill, container, _p = self.setup_case("t-003-d3c", "done", "container", extra_file=True)
        code, out, err = self.worker("delete", "w-1")
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(bill))
        self.assertTrue(os.path.isdir(container))
        self.assertFalse(self.t009()["worktrees"][0].get("removed"), "the container is still there: t-009's entry still names it")
        t3 = next(t for t in self.ledger()["tasks"] if t["id"] == "t-003")
        self.assertIsNone(t3["worktree"])
        self.assertTrue(t3["worktrees"][0]["removed"])

    def test_d4_plain_case_unchanged(self):
        self.reset()
        bill = self.job_tree("t-003-d4")
        self.add_task("t-003", "done", bill, [{"address": "w-1", "role": "build", "name": "t-003 build", "finished": True}])
        facts, _row = pa_hooks.worker_facts(self.dir, {"terminal": "t0"}, None, "w-1")
        self.assertEqual((facts["worktree"]["heldBy"], facts["worktree"]["sharedWith"]), ([], []))
        code, out, err = self.worker("delete", "w-1")
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(bill))
        self.assertIn("Removed the empty folder {}.".format(os.path.dirname(bill)), out)
        led = self.ledger()
        self.assertEqual(len(led["tasks"]), 1)
        self.assertIsNone(led["tasks"][0]["worktree"])
        self.assertEqual(pa.task_worktrees(led["tasks"][0]), [])



class CloseWhenIdle(Base):
    """t-042: pa-worker close <t> --evidence F --when-idle closes now, or leaves one background waiter that closes the
    worker once it is idle, under the same guards, and journals the result with no message to the Project Agent."""

    def setUp(self):
        super().setUp()
        self.proj = self.repo()
        self.wt = self.worktree(self.proj, "t-003-x")
        self.add_task("t-003", "in-progress", self.wt, [{"address": "t31", "role": "build", "name": "t-003-tests"}])
        self.term("t31", "t-003-tests")
        self.evidence("t-003")
        pa_hooks.by_an_agent = lambda: True
        self.spawned = []
        self.saved_idle = {k: getattr(pa_hooks, k) for k in ("spawn_idle_closer", "WHEN_IDLE_POLL", "WHEN_IDLE_LIMIT")}
        pa_hooks.spawn_idle_closer = lambda *a, **k: self.spawned.append((a, k))
        pa_hooks.WHEN_IDLE_POLL = 0.01
        pa_hooks.WHEN_IDLE_LIMIT = 3

    def tearDown(self):
        for k, v in self.saved_idle.items():
            setattr(pa_hooks, k, v)
        super().tearDown()

    def when_idle(self, *extra):
        return self.worker("close", "t31", "--evidence", "FINDINGS.md", "--when-idle", *extra)

    def waiter(self):
        """The background waiter's own run, in-process, against the fake hc."""
        return self.worker("close", "t31", "--evidence", "FINDINGS.md", "--idle-waiter")

    def flip_after(self, reads, status, then=None):
        """hc get of t31 reports `working` until it was read `reads` times, then `status` (and runs `then` once)."""
        real, seen = self.fake_get, []
        self.terms["t31"]["status"] = "working"

        def get(address):
            if address == "t31" and address in self.terms:
                seen.append(address)
                if len(seen) == reads:
                    self.terms["t31"]["status"] = status
                    if then:
                        then()
            return real(address)
        pa.hc_get = get
        return seen

    def no_send(self):
        self.assertEqual([c for c in self.calls if c[:2] == ["hc", "send"]], [], "the waiter starts no Project Agent pass")

    def assertNotClosed(self):
        self.assertEqual(self.ended(), [])
        self.assertFalse(self.entry("t-003", "t31").get("finished"))

    # R1
    def test_i1_an_idle_worker_is_closed_at_once(self):
        code, out, err = self.when_idle()
        self.assertEqual(code, 0, err)
        self.assertIn("Closed worker t31 (t-003-tests, task t-003)", out)
        self.assertEqual(self.ended(), [["hc", "kill", "t31"], ["hc", "rm", "t31"]])
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.journal()[0]["trigger"], "close")

    # R2
    def test_i2_a_working_worker_leaves_one_waiter_and_closes_nothing_now(self):
        self.terms["t31"]["status"] = "working"
        before = self.snapshot()
        code, out, err = self.when_idle()
        self.assertEqual(code, 0, err)
        self.assertIn("t31 (t-003-tests) is working; it will be closed once it is idle", out)
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.spawned[0][0][1:], ("sp1", "t31", "FINDINGS.md"))
        self.assertEqual(self.snapshot(), before)
        self.assertNotClosed()
        self.no_send()

    # R3
    def test_i3_other_refusals_are_given_at_once(self):
        led = self.ledger()
        led["tasks"][0]["workers"][0].update(keep=True, keptAt="2026-11-16T10:00:00Z", keepQuote="leave it")
        self.ledger(led)
        self.terms["t31"]["status"] = "working"
        self.assertRefused(self.when_idle(), "is marked keep")
        led["tasks"][0]["workers"][0]["keep"] = False
        self.ledger(led)
        self.assertRefused(self.worker("close", "t31", "--evidence", "NOPE.md", "--when-idle"), "evidence NOPE.md is missing")
        self.terms["t31"]["status"] = "blocked"
        self.assertRefused(self.when_idle(), "is blocked on a question or a permission")
        self.assertEqual(self.spawned, [])

    def test_i3b_evidence_older_than_the_worker_is_refused_at_once(self):
        led = self.ledger()
        led["tasks"][0]["workers"][0]["started"] = "2099-01-01T00:00:00Z"
        self.ledger(led)
        self.terms["t31"]["status"] = "working"
        self.assertRefused(self.when_idle(), "was last written before t31 started")
        self.assertEqual(self.spawned, [])

    def test_i3c_usage(self):
        for argv in (("close", "--leftovers", "--when-idle"), ("close", "--card", "d-leftovers", "--when-idle"),
                     ("close", "t31", "--when-idle")):
            code, _, err = self.worker(*argv)
            self.assertEqual(code, 2, argv)
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.ended(), [])

    # R2, R4: the waiter
    def test_i4_waiter_closes_once_idle_and_journals_it(self):
        seen = self.flip_after(4, "idle")
        code, _, err = self.waiter()
        self.assertEqual(code, 0, err)
        self.assertGreaterEqual(len(seen), 4)
        self.assertEqual(self.ended(), [["hc", "kill", "t31"], ["hc", "rm", "t31"]])
        w = self.entry("t-003", "t31")
        self.assertEqual((w["finished"], w["closedBy"], w["report"]), (True, "agent", "FINDINGS.md"))
        lines = self.journal()
        self.assertEqual(len(lines), 1)
        self.assertEqual((lines[0]["trigger"], lines[0]["task"]), ("close-when-idle", "t-003"))
        self.assertIn("closed worker t31 (t-003-tests) after reading its report FINDINGS.md", lines[0]["why"])
        self.no_send()

    def test_i5_waiter_never_closes_a_blocked_worker(self):
        self.flip_after(3, "blocked")
        code, _, err = self.waiter()
        self.assertEqual(code, 0, err)
        self.assertNotClosed()
        lines = self.journal()
        self.assertEqual(len(lines), 1)
        self.assertEqual((lines[0]["trigger"], lines[0]["task"]), ("close-when-idle", "t-003"))
        self.assertIn("t31 (t-003-tests) is blocked on a question or a permission", lines[0]["why"])
        self.assertIn("left for the person; not closed", lines[0]["why"])
        self.no_send()

    def test_i6_waiter_gives_up_at_its_limit(self):
        self.terms["t31"]["status"] = "working"
        pa_hooks.WHEN_IDLE_LIMIT = 0.2
        start = time.monotonic()
        code, _, err = self.waiter()
        self.assertEqual(code, 0, err)
        self.assertLess(time.monotonic() - start, 10)
        self.assertNotClosed()
        lines = self.journal()
        self.assertEqual(len(lines), 1)
        self.assertIn("still working after", lines[0]["why"])
        self.assertIn("not closed", lines[0]["why"])
        self.no_send()

    def test_i7_waiter_rechecks_uncommitted_files_at_the_close(self):
        write(os.path.join(self.wt, "half.txt"), "x\n")
        self.flip_after(3, "idle")
        code, _, err = self.waiter()
        self.assertEqual(code, 0, err)
        self.assertNotClosed()
        lines = self.journal()
        self.assertEqual(len(lines), 1)
        self.assertIn("has 1 uncommitted file(s) (half.txt)", lines[0]["why"])
        self.assertIn("not closed", lines[0]["why"])

    def test_i7b_work_committed_while_it_waited_does_not_stop_the_close(self):
        write(os.path.join(self.wt, "half.txt"), "x\n")
        self.flip_after(3, "idle", then=lambda: (sh(self.wt, "git", "add", "half.txt"), sh(self.wt, "git", "commit", "-q", "-m", "w")))
        self.assertEqual(self.when_idle_spawn_then_wait(), 0)
        self.assertTrue(self.entry("t-003", "t31")["finished"])

    def when_idle_spawn_then_wait(self):
        code, out, err = self.when_idle()  # working, with uncommitted files: not refused, a waiter is left
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.spawned), 1, out)
        return self.waiter()[0]

    def test_i8_one_waiter_per_worker(self):
        import fcntl
        self.terms["t31"]["status"] = "idle"
        with open(os.path.join(self.dir, ".close-when-idle-t31.lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            code, _, err = self.waiter()
        self.assertEqual(code, 0, err)
        self.assertNotClosed()
        self.assertEqual(self.journal(), [])

    def test_i9_waiter_journals_a_worker_that_was_kept_meanwhile(self):
        def keep():
            led = self.ledger()
            led["tasks"][0]["workers"][0].update(keep=True, keptAt="2026-11-18T10:00:00Z", keepQuote="stay")
            self.ledger(led)
        self.flip_after(2, "idle", then=keep)
        self.assertEqual(self.waiter()[0], 0)
        self.assertNotClosed()
        self.assertIn("is marked keep", self.journal()[0]["why"])

    def test_i10_spawn_is_detached_and_silent(self):
        got = []

        class FakePopen(object):
            def __init__(self, argv, **kw):
                got.append((argv, kw))
        fake = type("S", (), {"Popen": FakePopen, "DEVNULL": subprocess.DEVNULL})
        saved_sub, saved_argv = pa_hooks.subprocess, sys.argv
        pa_hooks.subprocess, sys.argv = fake, ["/opt/bin/pa-worker"]
        try:
            self.saved_idle["spawn_idle_closer"](self.dir, "sp1", "t31", "FINDINGS.md")
        finally:
            pa_hooks.subprocess, sys.argv = saved_sub, saved_argv
        self.assertEqual(len(got), 1)
        argv, kw = got[0]
        self.assertEqual(argv, [sys.executable, "/opt/bin/pa-worker", "close", "t31", "--evidence", "FINDINGS.md",
                                "--space", "sp1", "--idle-waiter"])
        self.assertTrue(kw.get("start_new_session"))
        for k in ("stdin", "stdout", "stderr"):
            self.assertEqual(kw.get(k), subprocess.DEVNULL, k)
        self.assertEqual(kw.get("cwd"), self.dir)


class T133CloseWhenIdleLimits(Base):
    """t-133 (t-042 Recorded limits 1-3): a follow-up after --when-idle is not cut off (a new turn stops the waiter;
    the Project Agent can cancel it), and the waiter's bound holds when hc hangs or the Mac sleeps."""

    def setUp(self):
        Base.setUp(self)
        self.proj = self.repo()
        self.wt = self.worktree(self.proj, "t-003-x")
        self.add_task("t-003", "in-progress", self.wt, [{"address": "t31", "role": "build", "name": "t-003-tests"}])
        self.term("t31", "t-003-tests")
        self.evidence("t-003")
        pa_hooks.by_an_agent = lambda: True
        self.spawned = []
        self.saved_idle = {k: getattr(pa_hooks, k) for k in ("spawn_idle_closer", "WHEN_IDLE_POLL", "WHEN_IDLE_LIMIT")}
        pa_hooks.spawn_idle_closer = lambda *a, **k: self.spawned.append((a, k))
        pa_hooks.WHEN_IDLE_POLL = 0.01
        pa_hooks.WHEN_IDLE_LIMIT = 3

    def tearDown(self):
        for k, v in self.saved_idle.items():
            setattr(pa_hooks, k, v)
        Base.tearDown(self)

    when_idle, waiter, flip_after = CloseWhenIdle.when_idle, CloseWhenIdle.waiter, CloseWhenIdle.flip_after
    no_send, assertNotClosed = CloseWhenIdle.no_send, CloseWhenIdle.assertNotClosed

    def waiter_turn(self, ms):
        return self.worker("close", "t31", "--evidence", "FINDINGS.md", "--idle-waiter", "--turn-ms", str(ms))

    def cancel(self):
        return self.worker("close", "t31", "--cancel-when-idle")

    # RL1 (a): the waiter knows the turn it was left for
    def test_rl1_spawn_passes_the_turn_it_saw(self):
        self.terms["t31"].update(status="working", status_ms=1700000000000)
        code, out, err = self.when_idle()
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.spawned[0][1].get("turn_ms"), 1700000000000)

    def test_rl1_spawn_argv_carries_the_turn(self):
        got = []

        class FakePopen(object):
            def __init__(self, argv, **kw):
                got.append(argv)
        fake = type("S", (), {"Popen": FakePopen, "DEVNULL": subprocess.DEVNULL})
        saved_sub, saved_argv = pa_hooks.subprocess, sys.argv
        pa_hooks.subprocess, sys.argv = fake, ["/opt/bin/pa-worker"]
        try:
            self.saved_idle["spawn_idle_closer"](self.dir, "sp1", "t31", "FINDINGS.md", turn_ms=1700000000000)
        finally:
            pa_hooks.subprocess, sys.argv = saved_sub, saved_argv
        self.assertEqual(got[0][-2:], ["--turn-ms", "1700000000000"])

    def test_rl1_a_new_turn_stops_the_waiter(self):
        self.terms["t31"].update(status="working", status_ms=1000)
        real, seen = self.fake_get, []

        def get(address):
            seen.append(address)
            if len(seen) == 3:  # the turn ended and a follow-up began between two checks
                self.terms["t31"]["status_ms"] = 2000
            if len(seen) == 8:
                self.terms["t31"]["status"] = "idle"  # the follow-up's turn ends later
            return real(address)
        pa.hc_get = get
        code, _, err = self.waiter_turn(1000)
        self.assertEqual(code, 0, err)
        self.assertNotClosed()
        lines = self.journal()
        self.assertEqual(len(lines), 1, lines)
        self.assertEqual(lines[0]["trigger"], "close-when-idle")
        self.assertIn("a new turn began", lines[0]["why"])
        self.assertIn("not closed", lines[0]["why"])
        self.no_send()

    def test_rl1_the_same_turn_ending_is_closed(self):
        self.terms["t31"].update(status="working", status_ms=1000)
        real, seen = self.fake_get, []

        def get(address):
            seen.append(address)
            if len(seen) == 4:
                self.terms["t31"].update(status="idle", status_ms=3000)
            return real(address)
        pa.hc_get = get
        self.assertEqual(self.waiter_turn(1000)[0], 0)
        self.assertTrue(self.entry("t-003", "t31")["finished"])

    # RL1 (b): --cancel-when-idle
    def hold_waiter_lock(self):
        import fcntl
        lock = open(os.path.join(self.dir, ".close-when-idle-t31.lock"), "a")
        fcntl.flock(lock, fcntl.LOCK_EX)
        self.addCleanup(lock.close)
        return lock

    def test_rl1_cancel_with_no_waiter_says_so_and_writes_nothing(self):
        code, out, err = self.cancel()
        self.assertEqual(code, 0, err)
        self.assertIn("no pa-worker close --when-idle is waiting for t31", out)
        self.assertFalse(os.path.exists(os.path.join(self.dir, ".close-when-idle-t31.cancel")))
        self.assertNotClosed()

    def test_rl1_cancel_with_a_waiter_marks_it_and_journals(self):
        self.hold_waiter_lock()
        code, out, err = self.cancel()
        self.assertEqual(code, 0, err)
        self.assertIn("cancelled", out)
        self.assertTrue(os.path.exists(os.path.join(self.dir, ".close-when-idle-t31.cancel")))
        lines = self.journal()
        self.assertEqual(lines[-1]["trigger"], "close-when-idle")
        self.assertIn("cancel", lines[-1]["why"])
        self.assertNotClosed()

    def test_rl1_a_cancelled_waiter_closes_nothing_and_cleans_up(self):
        self.flip_after(3, "idle", then=lambda: write(os.path.join(self.dir, ".close-when-idle-t31.cancel"), "x\n"))
        code, _, err = self.waiter()
        self.assertEqual(code, 0, err)
        self.assertNotClosed()
        self.assertIn("cancelled", self.journal()[-1]["why"])
        self.assertFalse(os.path.exists(os.path.join(self.dir, ".close-when-idle-t31.cancel")))

    def test_rl1_cancel_after_the_waiter_closed_says_it_is_closed(self):
        # fix round 2 (M3): the waiter closed the worker and still holds its own lock when the cancel gets the ledger lock
        self.hold_waiter_lock()
        led = self.ledger()
        led["tasks"][0]["workers"][0]["finished"] = "2026-01-01T00:00:00Z"
        self.ledger(led)
        code, out, err = self.cancel()
        self.assertEqual(code, 0, err)
        self.assertNotIn("stays open", out)
        self.assertIn("already closed", out)
        self.assertFalse(os.path.exists(os.path.join(self.dir, ".close-when-idle-t31.cancel")))
        lines = self.journal() if os.path.exists(os.path.join(self.dir, "journal.jsonl")) else []
        self.assertFalse(any("stays open" in l.get("why", "") for l in lines), lines)

    def test_rl1_cancel_checked_under_the_lock_before_the_kill(self):
        # the cancel lands after the first status read of a close but before its recheck under the ledger lock
        self.terms["t31"]["status"] = "idle"
        real, seen = self.fake_get, []

        def get(address):
            seen.append(address)
            if len(seen) == 2:
                write(os.path.join(self.dir, ".close-when-idle-t31.cancel"), "x\n")
            return real(address)
        pa.hc_get = get
        self.assertEqual(self.waiter()[0], 0)
        self.assertNotClosed()

    def test_rl1_a_new_when_idle_clears_an_old_cancel(self):
        write(os.path.join(self.dir, ".close-when-idle-t31.cancel"), "x\n")
        self.terms["t31"]["status"] = "working"
        self.assertEqual(self.when_idle()[0], 0)
        self.assertFalse(os.path.exists(os.path.join(self.dir, ".close-when-idle-t31.cancel")))

    def test_rl1_plain_close_ignores_a_cancel_file(self):
        write(os.path.join(self.dir, ".close-when-idle-t31.cancel"), "x\n")
        code, _, err = self.worker("close", "t31", "--evidence", "FINDINGS.md")
        self.assertEqual(code, 0, err)
        self.assertTrue(self.entry("t-003", "t31")["finished"])

    def test_rl1_cancel_usage(self):
        for argv in (("close", "--cancel-when-idle"), ("close", "t31", "--cancel-when-idle", "--when-idle", "--evidence", "FINDINGS.md"),
                     ("keep", "t31", "--cancel-when-idle")):
            self.assertEqual(self.worker(*argv)[0], 2, argv)

    # RL2: a worker the waiter closed but whose entry a later rewrite lost is recorded by a second close
    def test_rl2_close_again_records_a_closed_worker(self):
        del self.terms["t31"]  # the waiter ended it; a stale rewrite dropped `finished`
        code, out, err = self.worker("close", "t31", "--evidence", "FINDINGS.md", "--when-idle")
        self.assertEqual(code, 0, err)
        self.assertTrue(self.entry("t-003", "t31")["finished"])
        self.assertEqual(self.ended(), [])

    # RL3: the bound
    def test_rl3_pa_call_gives_up_after_call_timeout(self):
        pa.call = self.saved["call"]
        saved = getattr(pa, "CALL_TIMEOUT", "missing")
        self.assertIsNone(saved, "the default is no timeout")
        pa.CALL_TIMEOUT = 0.3
        try:
            start = time.monotonic()
            with self.assertRaises(pa.Problem) as e:
                pa.call([sys.executable, "-c", "import time; time.sleep(5)"])
            self.assertLess(time.monotonic() - start, 4)
            self.assertIn("did not answer", str(e.exception))
        finally:
            pa.CALL_TIMEOUT = None

    def test_rl3_the_waiter_sets_an_hc_timeout_and_restores_it(self):
        during = []
        real = self.fake_call

        def call(argv, check=True):
            during.append(pa.CALL_TIMEOUT)
            return real(argv, check)
        pa.call = call
        self.terms["t31"]["status"] = "idle"
        self.assertEqual(self.waiter()[0], 0)
        self.assertTrue(during and all(t == pa_hooks.WHEN_IDLE_HC_TIMEOUT for t in during), during)
        self.assertIsNone(pa.CALL_TIMEOUT)

    def test_rl3_wall_clock_deadline(self):
        self.terms["t31"]["status"] = "working"
        pa_hooks.WHEN_IDLE_LIMIT = 50
        real_time, clock = pa_hooks.time, [1000.0]

        class Frozen(object):  # monotonic stands still (the Mac asleep); the wall clock moves
            monotonic = staticmethod(lambda: 5.0)
            strftime, gmtime = staticmethod(real_time.strftime), staticmethod(real_time.gmtime)

            @staticmethod
            def time():
                return clock[0]

            @staticmethod
            def sleep(_s):
                clock[0] += 20
                if clock[0] > 2000:  # the old monotonic-only deadline never ends: fail, do not hang
                    raise RuntimeError("the waiter kept waiting past its wall-clock limit")
        pa_hooks.time = Frozen
        try:
            code, _, err = self.waiter()
        finally:
            pa_hooks.time = real_time
        self.assertEqual(code, 0, err)
        self.assertNotClosed()
        self.assertIn("still working after", self.journal()[-1]["why"])


class T133ReportOnlyStop(Base):
    """t-133 G1: the affected-tests hook (Stop, TaskCompleted) runs nothing for a worker whose worktree has not changed
    since it started (a reviewer, verifier, check or design worker); the session-start hook records that state once."""

    def setUp(self):
        super().setUp()
        self.proj = self.repo()
        self.wt = self.worktree(self.proj, "t-003-x")
        self.marker = os.path.join(self.tmp, "ran")
        agent = load(os.path.join(self.dir, "agent.json"))
        agent["commands"] = {"affected": "echo ran >> {}".format(self.marker)}
        dump(agent, os.path.join(self.dir, "agent.json"))
        dump({"task": "t-003", "space": "sp1", "fix": False}, os.path.join(self.wt, ".claude", "pa-task.json"))
        self.saved_start = pa_hooks.START_DIR
        pa_hooks.START_DIR = os.path.join(self.tmp, "starts")
        os.environ["HC_TERMINAL_ID"] = "t41"

    def tearDown(self):
        pa_hooks.START_DIR = self.saved_start
        os.environ.pop("HC_TERMINAL_ID", None)
        super().tearDown()

    def data(self, sid="s-1"):
        return {"cwd": self.wt, "session_id": sid}

    def start(self, sid="s-1"):
        self.assertEqual(pa_hooks.hook_session_start(self.data(sid), []) or 0, 0)

    def stop(self, sid="s-1", args=("--stop",)):
        try:
            return pa_hooks.hook_affected(self.data(sid), list(args)) or 0
        except SystemExit as e:
            return e.code

    def ran(self):
        return os.path.exists(self.marker)

    def test_g1_report_only_worker_runs_nothing(self):
        self.start()
        write(os.path.join(self.wt, "notes.txt"), "x\n")  # a scratch file made and removed again: no change left
        os.unlink(os.path.join(self.wt, "notes.txt"))
        self.assertEqual(self.stop(), 0)
        self.assertFalse(self.ran())

    def test_g1_uncommitted_files_from_before_the_start_do_not_count(self):
        write(os.path.join(self.wt, "README"), "the builder's edit\n")
        write(os.path.join(self.wt, "new.txt"), "the builder's file\n")
        self.start()
        self.assertEqual(self.stop(), 0)
        self.assertFalse(self.ran(), "a reviewer in a worktree the builder left dirty runs no tests")
        self.assertEqual(self.stop(args=()), 0)  # TaskCompleted too
        self.assertFalse(self.ran())

    def test_g1_an_edit_to_an_already_changed_file_counts(self):
        write(os.path.join(self.wt, "README"), "the builder's edit\n")
        self.start()
        write(os.path.join(self.wt, "README"), "the reviewer's edit\n")
        self.assertEqual(self.stop(), 0)
        self.assertTrue(self.ran())

    def test_g1_a_new_file_counts(self):
        self.start()
        write(os.path.join(self.wt, "new.txt"), "x\n")
        self.assertEqual(self.stop(), 0)
        self.assertTrue(self.ran())

    def test_g1_a_file_put_back_after_the_start_counts(self):
        write(os.path.join(self.wt, "README"), "dirty\n")
        self.start()
        write(os.path.join(self.wt, "README"), "base\n")  # back to HEAD: no longer changed
        write(os.path.join(self.wt, "other.txt"), "y\n")
        self.assertEqual(self.stop(), 0)
        self.assertTrue(self.ran())

    def test_g1_a_new_commit_with_changes_left_counts(self):
        write(os.path.join(self.wt, "a.txt"), "1\n")
        self.start()
        sh(self.wt, "git", "add", "a.txt")
        sh(self.wt, "git", "commit", "-q", "-m", "a")
        write(os.path.join(self.wt, "a.txt"), "1\n2\n")
        self.assertEqual(self.stop(), 0)
        self.assertTrue(self.ran())

    def test_g1_a_commit_with_earlier_changes_left_as_they_were_counts(self):
        # fix round 2 (M2): the worker commits a file; the earlier worker's dirty file is left as it was
        write(os.path.join(self.wt, "README"), "the builder's edit\n")
        self.start()
        write(os.path.join(self.wt, "b.txt"), "the worker's file\n")
        sh(self.wt, "git", "add", "b.txt")
        sh(self.wt, "git", "commit", "-q", "-m", "b")
        self.assertEqual(self.stop(), 0)
        self.assertTrue(self.ran(), "a new commit counts even when the uncommitted files are as they were")

    def test_g1_an_edit_inside_an_untracked_folder_counts(self):
        # fix round 2 (M1): git status shows only scratch/ for a folder nothing in it is tracked
        write(os.path.join(self.wt, "scratch", "a.txt"), "1\n")
        self.start()
        write(os.path.join(self.wt, "scratch", "a.txt"), "2\n")
        self.assertEqual(self.stop(), 0)
        self.assertTrue(self.ran())

    def test_g1_a_new_file_in_a_nested_untracked_folder_counts(self):
        write(os.path.join(self.wt, "scratch", "sub", "a.txt"), "1\n")
        self.start()
        write(os.path.join(self.wt, "scratch", "sub", "b.py"), "x = 1\n")
        self.assertEqual(self.stop(), 0)
        self.assertTrue(self.ran())

    def test_g1_no_start_record_runs_as_before(self):
        write(os.path.join(self.wt, "new.txt"), "x\n")
        self.assertEqual(self.stop(), 0)
        self.assertTrue(self.ran(), "hooks installed before t-133: today's behaviour")

    def test_g1_a_restart_keeps_the_first_start(self):
        self.start("s-1")
        write(os.path.join(self.wt, "new.txt"), "x\n")
        self.start("s-2")  # resumed or compacted in the same terminal: a new session id
        self.assertEqual(self.stop("s-2"), 0)
        self.assertTrue(self.ran(), "work done before the restart still counts")

    def test_g1_each_worker_has_its_own_start(self):
        self.start()
        write(os.path.join(self.wt, "new.txt"), "x\n")  # the first worker's change
        os.environ["HC_TERMINAL_ID"] = "t42"  # the next worker in the same worktree
        self.start()
        self.assertEqual(self.stop(), 0)
        self.assertFalse(self.ran())

    def test_g1_no_terminal_keys_by_session(self):
        os.environ.pop("HC_TERMINAL_ID", None)
        self.start("s-9")
        self.assertEqual(self.stop("s-9"), 0)
        self.assertFalse(self.ran())
        write(os.path.join(self.wt, "new.txt"), "x\n")
        self.assertEqual(self.stop("s-other"), 0)
        self.assertTrue(self.ran(), "another session has no start record of its own")

    def test_g1_start_writes_nothing_in_the_worktree(self):
        before = sh(self.wt, "git", "status", "--porcelain", "--untracked-files=all", "--ignored")
        self.start()
        self.assertEqual(sh(self.wt, "git", "status", "--porcelain", "--untracked-files=all", "--ignored"), before)

    def test_g1_outside_a_task_worktree_nothing(self):
        data = {"cwd": self.tmp, "session_id": "s-1"}
        self.assertEqual(pa_hooks.hook_session_start(data, []) or 0, 0)
        self.assertEqual(os.listdir(pa_hooks.START_DIR) if os.path.isdir(pa_hooks.START_DIR) else [], [])

    def test_g1_registered_as_a_session_start_hook(self):
        settings = pa_hooks.hook_settings()
        cmds = [h["command"] for e in settings["SessionStart"] for h in e["hooks"]]
        self.assertEqual(cmds, [os.path.join("$HOME", ".local", "bin", "pa-hook") + " session-start"])
        self.assertIn("session-start", pa_hooks.HOOKS)
        self.assertTrue(pa_hooks.PA_HOOK_COMMAND.match(cmds[0]), "pa-trust accepts it as a pa-hook command")


class Unrecorded(Base):
    """t-020 J4 (R4, R5): pa-worktree check finds worktrees under the scanned `worktrees` folders that no task of any space
    on the same project records, lists them once on a card d-wt-unrecorded-<n> (never a Delete), counts the person's own
    and the empty folders. A second space sp2 has the same project."""

    def setUp(self):
        super().setUp()
        self.proj = self.repo()
        self.project = os.path.join(self.tmp, "Project")
        os.makedirs(os.path.join(self.project, "worktrees"))
        agent = load(os.path.join(self.dir, "agent.json"))
        agent["project"] = self.project
        dump(agent, os.path.join(self.dir, "agent.json"))
        self.folder = os.path.realpath(os.path.join(self.project, "worktrees"))
        self.space2()

    def space2(self, tasks=(), project=None, name="sp2"):
        folder = os.path.join(pa.ROOT, "spaces", name)
        dump({"space": name, "terminal": "t90", "profile": "test", "name": "S2", "project": project or self.project},
             os.path.join(folder, "agent.json"))
        dump({"version": 1, "tasks": list(tasks)}, os.path.join(folder, "ledger.json"))
        return folder

    def tree(self, name, commits=0):
        """A git worktree <project>/worktrees/<name>/proj on branch <name>; its realpath."""
        path = os.path.join(self.project, "worktrees", name, "proj")
        sh(self.proj, "git", "worktree", "add", "-q", "-b", name, path, "main")
        for i in range(commits):
            self.commit(path, "u{}.txt".format(i), "x\n", "work {}".format(i))
        return os.path.realpath(path)

    def check(self, *extra):
        return self.run_cmd("pa-worktree", "check", "--space", "sp1", *extra)

    def card(self, n=1):
        return pa.read_json(os.path.join(self.dir, "decisions", "d-wt-unrecorded-{}.json".format(n)))

    def answer(self, n, label):
        path = os.path.join(self.dir, "decisions", "d-wt-unrecorded-{}.json".format(n))
        card = load(path)
        card.update(status="answered", answer=label, answered="2026-11-18T00:00:00Z")
        dump(card, path)
        return path

    def unrecorded_cards(self):
        names = []
        for sub in ("decisions", "decisions-archive"):
            folder = os.path.join(self.dir, sub)
            names += [n for n in os.listdir(folder) if n.startswith("d-wt-unrecorded-")] if os.path.isdir(folder) else []
        return sorted(names)

    def test_u1_unrecorded_listed_once_with_guess(self):
        mine = self.tree("t-003-x")
        self.add_task("t-003", "in-progress", mine)
        self.add_task("t-017", "in-progress")
        timing = self.tree("t-017-timing-test", commits=1)
        self.term("t77", "someone", cwd=timing)
        code, out, err = self.check()
        self.assertEqual(code, 0, err)
        self.assertIn("unrecorded: {} (guess t-017, job timing-test); --open-cards lists it on a card".format(timing), out)
        self.assertNotIn("unrecorded: " + mine, out)
        self.assertEqual(self.unrecorded_cards(), [], "without --open-cards no card is written")
        code, out, err = self.check("--open-cards")
        self.assertEqual(code, 0, err)
        self.assertIn("opened card d-wt-unrecorded-1 (1 worktree no task records)", out)
        card = self.card()
        self.assertEqual((card["id"], card["task"], card["kind"], card["status"], card["paths"]),
                         ("d-wt-unrecorded-1", None, "unrecorded-worktrees", "open", [timing]))
        self.assertNotIn("worktree", card)
        self.assertEqual(card["items"], [{"path": timing, "kind": "git worktree", "branch": "t-017-timing-test", "commits": 1, "merged": 0,
                                          "pushed": 0, "uncommitted": 0, "terminals": ["t77"], "guess": "t-017", "job": "timing-test"}])
        self.assertEqual(card["question"], "Record these worktrees no task records under the tasks they seem to belong to?")
        self.assertEqual(card["context"].split("\n"), [
            "Worktrees under {} that no task records. Nothing on this card deletes anything.".format(self.folder),
            "{} (git worktree): branch t-017-timing-test, 1 commits (0 merged, 0 pushed), 0 uncommitted; terminals in it: t77; "
            "guess: t-017, job timing-test".format(timing),
            'In the note you can say per folder: "t-017-timing-test: t-017" or "t-017-timing-test: leave".'])
        self.assertEqual(card["options"], [
            {"label": "Record as guessed", "detail": "Add each to its guessed task's worktrees with that job; it then gets its own "
                                                      "Delete/Keep card when its job is over."},
            {"label": "Leave them alone", "detail": "Record nothing; these are not listed again."}])
        self.assertEqual(card["recommendation"], "Record as guessed")
        # listed once: the next check leaves the same card
        code, out, err = self.check("--open-cards")
        self.assertEqual(code, 0, err)
        self.assertIn("card d-wt-unrecorded-1 is open", out)
        self.assertEqual(self.unrecorded_cards(), ["d-wt-unrecorded-1.json"])
        self.assertTrue(os.path.isdir(timing), "nothing is deleted")

    def test_u2_recorded_in_another_space_same_project_not_listed(self):
        self.add_task("t-017", "in-progress")
        theirs = self.tree("t-017-theirs")
        gone = self.tree("t-017-gone")
        elsewhere = self.tree("t-017-elsewhere")
        self.space2([{"id": "t-001", "state": "in-progress", "worktree": theirs,
                      "worktrees": [{"path": gone, "job": "gone", "removed": "2026-11-01T00:00:00Z"}]}])
        self.space2([{"id": "t-001", "state": "in-progress", "worktree": elsewhere}], project=os.path.join(self.tmp, "Other"), name="sp3")
        code, out, err = self.check()
        self.assertEqual(code, 0, err)
        self.assertNotIn("unrecorded: " + theirs, out)
        self.assertNotIn("unrecorded: " + gone, out, "a removed entry still counts as recorded")
        self.assertIn("unrecorded: {} (guess t-017, job elsewhere)".format(elsewhere), out, "another project's space does not count")
        # an unreadable ledger of a space on the same project: nothing is listed as unrecorded (D5), and it is named
        write(os.path.join(pa.ROOT, "spaces", "sp2", "ledger.json"), "{not json")
        code, out, err = self.check("--open-cards")
        self.assertEqual(code, 0, err)
        self.assertIn("could not read the ledger of space sp2", out)
        self.assertNotIn("unrecorded: ", out)
        self.assertEqual(self.unrecorded_cards(), [])

    def test_u3_person_named_worktree_only_counted(self):
        self.add_task("t-017", "in-progress")
        mine = self.tree("my-experiment", commits=1)
        other = self.tree("t-999-x")
        code, out, err = self.check("--open-cards")
        self.assertEqual(code, 0, err)
        self.assertIn("No task in the ledger has a worktree.", out)
        self.assertIn("2 worktrees under {} not named after a task of this ledger: left alone, not listed".format(self.folder), out)
        self.assertNotIn(mine, out)
        self.assertNotIn(other, out)
        self.assertEqual(self.unrecorded_cards(), [])

    def test_u4_empty_containers_counted_not_listed(self):
        self.add_task("t-005", "done")
        os.makedirs(os.path.join(self.project, "worktrees", "t-005-old"))
        os.makedirs(os.path.join(self.project, "worktrees", "empty"))
        write(os.path.join(self.project, "worktrees", "t-005-notes", "a.txt"), "not a worktree\n")
        code, out, err = self.check("--open-cards")
        self.assertEqual(code, 0, err)
        self.assertIn("2 empty folders under {} (left by earlier removals)".format(self.folder), out)
        self.assertNotIn("t-005-notes", out)
        self.assertNotIn("unrecorded: ", out)
        self.assertEqual(self.unrecorded_cards(), [])
        self.assertTrue(os.path.isdir(os.path.join(self.project, "worktrees", "t-005-old")), "an empty folder is never removed by check")

    def test_u5_open_card_updated_in_place_new_card_after_answer(self):
        self.add_task("t-017", "in-progress")
        a = self.tree("t-017-a")
        self.assertEqual(self.check("--open-cards")[0], 0)
        self.assertEqual(self.card()["paths"], [a])
        self.assertIsNone(self.card().get("updated"))
        b = self.tree("t-017-b")
        code, out, err = self.check("--open-cards")
        self.assertEqual(code, 0, err)
        self.assertIn("updated card d-wt-unrecorded-1 (2 worktrees no task records)", out)
        card = self.card()
        self.assertEqual((card["id"], card["paths"], card["status"]), ("d-wt-unrecorded-1", [a, b], "open"))
        self.assertTrue(card.get("updated"))
        self.assertEqual(self.unrecorded_cards(), ["d-wt-unrecorded-1.json"])
        self.answer(1, "Record as guessed")
        c = self.tree("t-017-c")
        code, out, err = self.check("--open-cards")
        self.assertEqual(code, 0, err)
        self.assertIn("opened card d-wt-unrecorded-2 (1 worktree no task records)", out)
        self.assertEqual(self.card(2)["paths"], [c])
        self.assertEqual(self.card()["status"], "answered", "the answered card is left as it is")
        # once the orchestrator records it, the open card has nothing left: withdrawn
        led = self.ledger()
        led["tasks"][0]["worktrees"] = [{"path": c, "job": "c"}]
        self.ledger(led)
        code, out, err = self.check("--open-cards")
        self.assertEqual(code, 0, err)
        self.assertIn("withdrew card d-wt-unrecorded-2: no unrecorded worktree is left", out)
        card = self.card(2)
        self.assertEqual((card["status"], card["note"]), ("withdrawn", "Withdrawn by pa-worktree check: no unrecorded worktree is left"))
        self.assertTrue(any("d-wt-unrecorded-2" in j.get("why", "") for j in self.journal()))

    def test_u6_left_alone_never_listed_again(self):
        self.add_task("t-017", "in-progress")
        a = self.tree("t-017-a")
        self.check("--open-cards")
        self.answer(1, "Leave them alone")
        code, out, err = self.check("--open-cards")
        self.assertEqual(code, 0, err)
        self.assertNotIn(a, out)
        self.assertEqual(self.unrecorded_cards(), ["d-wt-unrecorded-1.json"])
        # an answered card in decisions-archive/ (t-054) still counts, and its number is not used again
        os.makedirs(os.path.join(self.dir, "decisions-archive"))
        os.rename(os.path.join(self.dir, "decisions", "d-wt-unrecorded-1.json"),
                  os.path.join(self.dir, "decisions-archive", "d-wt-unrecorded-1.json"))
        b = self.tree("t-017-b")
        code, out, err = self.check("--open-cards")
        self.assertEqual(code, 0, err)
        self.assertNotIn(a, out)
        self.assertIn("opened card d-wt-unrecorded-2 (1 worktree no task records)", out)
        self.assertEqual(self.card(2)["paths"], [b])
        self.assertEqual(load(os.path.join(self.dir, "decisions-archive", "d-wt-unrecorded-1.json"))["status"], "answered")

    def test_u7_card_has_no_worktree_field_remove_refuses_it(self):
        self.add_task("t-017", "in-progress")
        a = self.tree("t-017-a")
        self.check("--open-cards")
        self.assertNotIn("worktree", self.card())
        self.answer(1, "Delete")  # forged: the card has no Delete option
        before = self.snapshot()
        for task in ("unrecorded-1", "t-017"):
            code, out, err = self.run_cmd("pa-worktree", "remove", task, "--space", "sp1")
            self.assertEqual(code, 2, (out, err))
            self.assertIn("has no worktree in the ledger", err)
        self.assertTrue(os.path.isdir(a))
        self.assertEqual(self.snapshot(), before)

    def test_u8_workspace_folder_scanned_via_recorded_one(self):
        ws = os.path.join(self.tmp, "Ws", "worktrees")
        status = os.path.join(self.tmp, "status.sh")
        write(status, '#!/bin/sh\nprintf \'{"uncommitted": 0, "unpushed": 1, "branch": "A:feat", "removesBranches": true, '
                      '"kind": "workspace worktree", "repos": ["%s/src/A"]}\' "$1"\n')
        os.chmod(status, 0o755)
        dump({"name": "test2", "claude": ["true"], "codex": ["true"],
              "worktree": {"marker": "marker.txt", "status": status, "remove": status}}, os.path.join(pa.ROOT, "profiles", "test2", "profile.json"))
        agent = load(os.path.join(self.dir, "agent.json"))
        agent["profile"] = "test2"
        dump(agent, os.path.join(self.dir, "agent.json"))
        for name in ("t-003-pkg", "t-017-pkg"):
            write(os.path.join(ws, name, "marker.txt"), "")
            pkg = os.path.join(ws, name, "src", "A")
            os.makedirs(pkg)
            sh(pkg, "git", "init", "-q", "-b", "main")
            self.commit(pkg, "README", "base\n", "base")
            sh(pkg, "git", "checkout", "-q", "-b", "feat")
            self.commit(pkg, "f.txt", "x\n", "feat work")
        self.add_task("t-003", "in-progress", os.path.join(ws, "t-003-pkg"))
        self.add_task("t-017", "in-progress")
        code, out, err = self.check("--open-cards")
        self.assertEqual(code, 0, err)
        theirs = os.path.realpath(os.path.join(ws, "t-017-pkg"))
        card = self.card()
        self.assertEqual(card["paths"], [theirs])
        item = card["items"][0]
        self.assertEqual((item["kind"], item["branch"], item["commits"], item["guess"], item["job"]),
                         ("workspace worktree", "A:feat", 1, "t-017", "pkg"))
        self.assertEqual(card["context"].split("\n")[0],
                         "Worktrees under {} that no task records. Nothing on this card deletes anything.".format(os.path.realpath(ws)))

    def test_u9_cap_20_and_more_line(self):
        self.add_task("t-017", "in-progress")
        paths = [self.tree("t-017-j{:02d}".format(i)) for i in range(1, 23)]
        code, out, err = self.check("--open-cards")
        self.assertEqual(code, 0, err)
        card = self.card()
        self.assertEqual(card["paths"], paths[:20])
        self.assertEqual(len(card["items"]), 20)
        lines = card["context"].split("\n")
        self.assertIn("… and 2 more (more than 20; listed on the next card)", lines)
        self.assertEqual(lines[-1], 'In the note you can say per folder: "t-017-j01: t-017" or "t-017-j01: leave".')
        self.answer(1, "Leave them alone")
        self.assertEqual(self.check("--open-cards")[0], 0)
        self.assertEqual(self.card(2)["paths"], paths[20:])

    def test_u10_outside_scanned_folders_never_seen(self):
        self.add_task("t-017", "in-progress")
        loose = os.path.realpath(self.worktree(self.proj, "t-017-loose"))
        far = os.path.join(self.tmp, "elsewhere", "t-017-far")
        sh(self.proj, "git", "worktree", "add", "-q", "-b", "t-017-far", far, "main")
        os.symlink(far, os.path.join(self.project, "worktrees", "t-017-link"))
        code, out, err = self.check("--open-cards")
        self.assertEqual(code, 0, err)
        self.assertNotIn(loose, out)
        self.assertNotIn("t-017-far", out)
        self.assertNotIn("t-017-link", out)
        self.assertEqual(self.unrecorded_cards(), [])

    def test_u11_pa_decide_accepts_it_in_conversation(self):
        self.add_task("t-017", "in-progress")
        self.tree("t-017-a")
        self.check("--open-cards")
        with contextlib.redirect_stdout(io.StringIO()):
            pa.cmd_decide(argparse.Namespace(space="sp1", card="d-wt-unrecorded-1", answer="Record as guessed",
                                             quote="yes, record them", withdraw=False, why=None))
        card = self.card()
        self.assertEqual((card["status"], card["answer"], card["note"]), ("answered", "Record as guessed", "yes, record them"))
        code, out, err = self.check("--open-cards")
        self.assertEqual(code, 0, err)
        self.assertNotIn("unrecorded: ", out)
        self.assertEqual(self.unrecorded_cards(), ["d-wt-unrecorded-1.json"], "answered: not listed again")


    def test_u12_a_recorded_project_folder_hides_nothing(self):  # t-131 (t-020 J4-a)
        self.add_task("t-003", "in-progress", self.project)
        self.add_task("t-017", "in-progress")
        a = self.tree("t-017-a")
        code, out, err = self.check()
        self.assertEqual(code, 0, err)
        self.assertIn("unrecorded: {} (guess t-017, job a)".format(a), out)
        # a recorded container inside the scanned folder still holds its worktree
        self.add_task("t-018", "in-progress", os.path.join(self.project, "worktrees", "t-017-a"))
        self.assertNotIn("unrecorded: " + a, self.check()[1])

    def test_u13_an_unreadable_agent_json_of_another_space_lists_nothing(self):  # t-131 (t-020 RL-R1 a)
        self.add_task("t-003", "in-progress")
        self.tree("t-003-x")
        write(os.path.join(pa.ROOT, "spaces", "sp2", "agent.json"), "{broken")
        os.makedirs(os.path.join(pa.ROOT, "spaces", "sp4"))  # no agent.json at all: not a space
        code, out, err = self.check("--open-cards")
        self.assertEqual(code, 0, err)
        self.assertIn("could not read the agent.json of space sp2", out)
        self.assertNotIn("space sp4", out)
        self.assertNotIn("unrecorded: ", out)
        self.assertEqual(self.unrecorded_cards(), [])

    def test_u14_the_scan_takes_no_index_lock_and_never_probes_the_persons(self):  # t-131 (t-020 RL-R2)
        self.add_task("t-017", "in-progress")
        a = self.tree("t-017-a")
        mine = self.tree("my-experiment", commits=1)
        index = {}
        for tree in (a, mine):
            name = subprocess.run(["git", "-C", tree, "rev-parse", "--git-path", "index"], stdout=subprocess.PIPE,
                                  universal_newlines=True).stdout.strip()
            index[tree] = os.path.join(tree, name) if not os.path.isabs(name) else name
            subprocess.run(["git", "-C", tree, "status", "--porcelain"], stdout=subprocess.PIPE)  # a clean index first
            later = time.time() + 5
            for f in os.listdir(tree):
                if os.path.isfile(os.path.join(tree, f)) and f != ".git":
                    os.utime(os.path.join(tree, f), (later, later))  # same content, newer stat: a plain status refreshes the index
        before = {t: raw(i) for t, i in index.items()}
        probed, real = [], pa_hooks.worktree_facts
        pa_hooks.worktree_facts = lambda path, profile=None: (probed.append(os.path.realpath(path)), real(path, profile))[1]
        try:
            code, out, err = self.check()
        finally:
            pa_hooks.worktree_facts = real
        self.assertEqual(code, 0, err)
        self.assertIn("unrecorded: " + a, out)
        self.assertIn("1 worktrees under {} not named after a task of this ledger".format(self.folder), out)
        self.assertEqual(raw(index[a]), before[a], "git status ran with --no-optional-locks: the index is not rewritten")
        self.assertFalse([p for p in probed if pa_hooks.inside(p, os.path.dirname(mine))], "the person's worktree is never probed")

    def test_u15_a_workspace_folder_from_a_deeper_recorded_path(self):  # t-131 (t-020 RL-R3)
        ws = os.path.join(self.tmp, "Ws", "worktrees")
        status = os.path.join(self.tmp, "status.sh")
        write(status, '#!/bin/sh\nprintf \'{"uncommitted": 0, "unpushed": 0, "branch": "A:feat", "kind": "workspace worktree"}\' "$1"\n')
        os.chmod(status, 0o755)
        dump({"name": "test2", "claude": ["true"], "codex": ["true"],
              "worktree": {"marker": "marker.txt", "status": status, "remove": status}}, os.path.join(pa.ROOT, "profiles", "test2", "profile.json"))
        agent = load(os.path.join(self.dir, "agent.json"))
        agent["profile"] = "test2"
        dump(agent, os.path.join(self.dir, "agent.json"))
        for name in ("t-003-pkg", "t-017-pkg"):
            write(os.path.join(ws, name, "marker.txt"), "")
            os.makedirs(os.path.join(ws, name, "src", "A"))
        self.add_task("t-003", "in-progress", os.path.join(ws, "t-003-pkg", "src", "A"))  # a package path, three levels down
        self.add_task("t-017", "in-progress")
        code, out, err = self.check()
        self.assertEqual(code, 0, err)
        self.assertIn("unrecorded: {} (guess t-017, job pkg)".format(os.path.realpath(os.path.join(ws, "t-017-pkg"))), out)
        self.assertNotIn("unrecorded: " + os.path.realpath(os.path.join(ws, "t-003-pkg")), out)

    def test_u16_described_only_for_a_card_being_written(self):  # t-131 (t-020 RL-J4a)
        self.add_task("t-017", "in-progress")
        self.tree("t-017-a")
        calls, real = [], pa_hooks.describe_worktree
        pa_hooks.describe_worktree = lambda row, facts, profile: (calls.append(row["path"]), real(row, facts, profile))[1]
        try:
            self.assertEqual(self.check()[0], 0)
            self.assertEqual(calls, [], "a check without --open-cards describes nothing")
            self.assertEqual(self.check("--open-cards")[0], 0)
            self.assertEqual(len(calls), 1, "the card's one worktree, once")
            self.assertEqual(self.card()["items"][0]["branch"], "t-017-a")
            self.assertIn("card d-wt-unrecorded-1 is open", self.check("--open-cards")[1])
            self.assertEqual(len(calls), 1, "the same set: the open card stays, nothing described")
        finally:
            pa_hooks.describe_worktree = real


class T136WaiterLimits(Base):
    """t-136 (t-133 RL3, RL4, N3): a cancel right after --when-idle reaches the waiter that is still starting; an hc
    kill or rm that does not answer is journaled as maybe closed, not as not closed."""

    setUp, tearDown = T133CloseWhenIdleLimits.setUp, T133CloseWhenIdleLimits.tearDown
    when_idle, waiter, flip_after = CloseWhenIdle.when_idle, CloseWhenIdle.waiter, CloseWhenIdle.flip_after
    cancel, hold_waiter_lock, assertNotClosed = (T133CloseWhenIdleLimits.cancel, T133CloseWhenIdleLimits.hold_waiter_lock,
                                                 CloseWhenIdle.assertNotClosed)

    def marker(self):
        return os.path.join(self.dir, ".close-when-idle-t31.starting")

    def test_rl3_when_idle_leaves_a_starting_marker(self):
        self.terms["t31"]["status"] = "working"
        self.assertEqual(self.when_idle()[0], 0)
        self.assertEqual(len(self.spawned), 1)
        self.assertTrue(os.path.exists(self.marker()), "written before the waiter is started")

    def test_rl3_a_cancel_before_the_waiter_holds_its_lock_lands(self):
        write(self.marker(), "x\n")  # spawned; the child has not taken its lock yet
        code, out, err = self.cancel()
        self.assertEqual(code, 0, err)
        self.assertIn("cancelled; the worker stays open", out)
        self.assertTrue(os.path.exists(os.path.join(self.dir, ".close-when-idle-t31.cancel")))
        self.assertIn("cancel", self.journal()[-1]["why"])
        self.assertNotClosed()

    def test_rl3_a_waiter_that_locks_between_the_cancels_checks_is_seen(self):
        # t-136 round 2 (REVIEW M1): the cancel's probe finds the lock free and lets go; the waiter then takes it and
        # unlinks .starting before the cancel reads the marker. The cancel must still see a waiter.
        import fcntl
        write(self.marker(), "x\n")
        real, state = fcntl.flock, {}

        def flock(f, op):
            real(f, op)
            if op == fcntl.LOCK_UN and not state:  # the probe just let go: the waiter starts here
                state["lock"] = self.hold_waiter_lock()
                os.unlink(self.marker())
        fcntl.flock = flock
        self.addCleanup(setattr, fcntl, "flock", real)
        code, out, err = self.cancel()
        self.assertEqual(code, 0, err)
        self.assertIn("cancelled; the worker stays open", out)
        self.assertTrue(os.path.exists(os.path.join(self.dir, ".close-when-idle-t31.cancel")))
        self.assertNotClosed()

    def test_rl3_then_the_waiter_closes_nothing(self):
        write(self.marker(), "x\n")
        self.assertEqual(self.cancel()[0], 0)
        self.terms["t31"]["status"] = "idle"
        self.assertEqual(self.waiter()[0], 0)
        self.assertNotClosed()
        self.assertIn("cancelled", self.journal()[-1]["why"])

    def test_rl3_an_old_marker_is_no_waiter(self):
        write(self.marker(), "x\n")
        old = time.time() - 600
        os.utime(self.marker(), (old, old))  # a waiter that died before it took its lock
        code, out, err = self.cancel()
        self.assertEqual(code, 0, err)
        self.assertIn("no pa-worker close --when-idle is waiting for t31", out)
        self.assertFalse(os.path.exists(os.path.join(self.dir, ".close-when-idle-t31.cancel")))

    def test_rl3_the_waiter_removes_the_marker(self):
        write(self.marker(), "x\n")
        self.terms["t31"]["status"] = "idle"
        self.assertEqual(self.waiter()[0], 0)
        self.assertFalse(os.path.exists(self.marker()))

    def test_rl3_a_spawn_that_fails_leaves_no_marker(self):
        def fail(*a, **k):
            raise OSError("no python")
        pa_hooks.spawn_idle_closer = fail
        self.terms["t31"]["status"] = "working"
        with self.assertRaises(OSError):
            self.when_idle()
        self.assertFalse(os.path.exists(self.marker()))

    def test_rl4_a_kill_that_does_not_answer_may_have_closed(self):
        real = self.fake_call

        def call(argv, check=True):
            if argv[:2] == ["hc", "kill"]:
                real(argv, check)  # it lands, but hc does not answer in time
                raise pa.Problem("hc kill t31 did not answer in 30 s")
            return real(argv, check)
        pa.call = call
        self.terms["t31"]["status"] = "idle"
        self.assertEqual(self.waiter()[0], 0)
        why = self.journal()[-1]["why"]
        self.assertIn("may have been closed", why)
        self.assertIn("pa-worker close t31 again", why)
        self.assertNotIn("not closed", why)

    def test_rl4_an_rm_that_does_not_answer_may_have_closed(self):
        real = self.fake_call

        def call(argv, check=True):
            if argv[:2] == ["hc", "rm"]:
                raise pa.Problem("hc rm t31 did not answer in 30 s")
            return real(argv, check)
        pa.call = call
        self.terms["t31"]["status"] = "idle"
        self.assertEqual(self.waiter()[0], 0)
        self.assertIn("may have been closed", self.journal()[-1]["why"])

    def test_n3_already_closed_line(self):
        self.hold_waiter_lock()
        led = self.ledger()
        led["tasks"][0]["workers"][0]["finished"] = "2026-01-01T00:00:00Z"
        self.ledger(led)
        code, out, err = self.cancel()
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "t31: already closed by close --when-idle; nothing to cancel.")


class T136SnapshotLimits(Base):
    """t-136 (t-133 RL2, RL5, RL6, N2): the start record in the space's own folder, private to the user; a snapshot that
    cannot be taken in time runs the tests; quoted paths, symlinks and nested repositories are read as they are."""

    def setUp(self):
        Base.setUp(self)
        self.proj = self.repo()
        self.wt = self.worktree(self.proj, "t-003-x")
        self.marker = os.path.join(self.tmp, "ran")
        agent = load(os.path.join(self.dir, "agent.json"))
        agent["commands"] = {"affected": "echo ran >> {}".format(self.marker)}
        dump(agent, os.path.join(self.dir, "agent.json"))
        dump({"task": "t-003", "space": "sp1", "fix": False}, os.path.join(self.wt, ".claude", "pa-task.json"))
        self.saved_start = pa_hooks.START_DIR
        pa_hooks.START_DIR = os.path.join(self.tmp, "starts")
        os.environ["HC_TERMINAL_ID"] = "t41"

    def tearDown(self):
        pa_hooks.START_DIR = self.saved_start
        os.environ.pop("HC_TERMINAL_ID", None)
        Base.tearDown(self)
    data, start, stop, ran = T133ReportOnlyStop.data, T133ReportOnlyStop.start, T133ReportOnlyStop.stop, T133ReportOnlyStop.ran

    def records(self):
        folder = os.path.join(self.dir, ".hook-starts")
        return [os.path.join(folder, n) for n in os.listdir(folder)] if os.path.isdir(folder) else []

    def test_rl2_the_record_is_in_the_space_folder_and_private(self):
        pa_hooks.START_DIR = None
        tmp = os.path.join(self.tmp, "system-temp")
        os.makedirs(tmp)
        saved, tempfile.tempdir = tempfile.tempdir, tmp
        try:
            write(os.path.join(self.wt, "README"), "the builder's edit\n")
            self.start()
            self.assertEqual(os.listdir(tmp), [], "nothing in the temp folder")
            self.assertEqual(len(self.records()), 1)
            self.assertEqual(os.stat(self.records()[0]).st_mode & 0o777, 0o600)
            self.assertEqual(self.stop(), 0)
            self.assertFalse(self.ran(), "the record is read back from the space folder")
        finally:
            tempfile.tempdir = saved

    def test_rl2_a_record_others_can_write_is_not_trusted(self):
        pa_hooks.START_DIR = None
        write(os.path.join(self.wt, "README"), "the builder's edit\n")
        self.start()
        os.chmod(self.records()[0], 0o666)
        self.assertEqual(self.stop(), 0)
        self.assertTrue(self.ran())

    def test_rl2_a_record_in_place_is_kept(self):  # first write wins, in the new place too
        pa_hooks.START_DIR = None
        self.start("s-1")
        before = raw(self.records()[0])
        write(os.path.join(self.wt, "new.txt"), "x\n")
        self.start("s-2")
        self.assertEqual(raw(self.records()[0]), before)
        self.assertEqual(len(self.records()), 1)

    def timeout_on(self, flag):
        real = pa_hooks.subprocess.run

        def run(argv, *a, **k):
            if argv[:2] == ["git", "status"] and flag in argv:
                raise subprocess.TimeoutExpired(argv, 30)
            return real(argv, *a, **k)
        return mock.patch.object(pa_hooks.subprocess, "run", run)

    def test_rl5_a_snapshot_whose_git_status_times_out_runs_the_tests(self):
        write(os.path.join(self.wt, "README"), "the builder's edit\n")
        with self.timeout_on("--untracked-files=all"):
            self.start()
            write(os.path.join(self.wt, "new.txt"), "the worker's file\n")
            self.assertEqual(self.stop(), 0)
        self.assertTrue(self.ran(), "a timeout at the start and the end is not 'unchanged'")

    def test_rl5_a_snapshot_over_its_budget_runs_the_tests(self):
        write(os.path.join(self.wt, "README"), "the builder's edit\n")
        self.start()
        saved = getattr(pa_hooks, "SNAPSHOT_LIMIT", None)
        pa_hooks.SNAPSHOT_LIMIT = -1
        try:
            self.assertEqual(self.stop(), 0)
        finally:
            pa_hooks.SNAPSHOT_LIMIT = saved
        self.assertTrue(self.ran())

    def test_rl6_an_edit_to_a_quoted_path_counts(self):
        write(os.path.join(self.wt, "été.txt"), "1\n")
        self.start()
        write(os.path.join(self.wt, "été.txt"), "2\n")
        self.assertEqual(self.stop(), 0)
        self.assertTrue(self.ran())

    def test_rl6_changed_files_reads_paths_as_they_are(self):
        write(os.path.join(self.wt, "été.txt"), "1\n")
        write(os.path.join(self.wt, "a b.txt"), "1\n")
        sh(self.wt, "git", "mv", "README", "READ ME.md")
        self.assertEqual(sorted(pa_hooks.changed_files(self.wt)), ["READ ME.md", "a b.txt", "été.txt"])

    def test_n2_a_symlink_pointed_elsewhere_counts(self):
        a, b = os.path.join(self.tmp, "A"), os.path.join(self.tmp, "B")
        write(os.path.join(a, "x.txt"), "a\n")
        write(os.path.join(b, "x.txt"), "b\n")
        os.symlink(a, os.path.join(self.wt, "lnk"))
        self.start()
        os.unlink(os.path.join(self.wt, "lnk"))
        os.symlink(b, os.path.join(self.wt, "lnk"))
        self.assertEqual(self.stop(), 0)
        self.assertTrue(self.ran())

    def test_n2_an_edit_inside_a_nested_repository_counts(self):
        sub = os.path.join(self.wt, "sub")
        write(os.path.join(sub, "a.txt"), "1\n")
        sh(sub, "git", "init", "-q")
        self.start()
        write(os.path.join(sub, "a.txt"), "2\n")
        self.assertEqual(self.stop(), 0)
        self.assertTrue(self.ran())

    def test_n2_an_untouched_nested_repository_skips(self):
        sub = os.path.join(self.wt, "sub")
        write(os.path.join(sub, "a.txt"), "1\n")
        sh(sub, "git", "init", "-q")
        self.start()
        self.assertEqual(self.stop(), 0)
        self.assertFalse(self.ran())


if __name__ == "__main__":
    unittest.main()
