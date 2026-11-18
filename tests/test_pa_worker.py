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
import re
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
        card = self.open_card(worktree=kind)
        self.assertLessEqual(len(card["changes"]["repos"]), 6)
        self.assertTrue(card["changes"]["truncated"])
        md = self.md()
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
        self.assertIn("t-003: opened card d-wt-t-003-billing", self.check("--open-cards")[1])
        card = self.card()
        self.assertEqual(card["question"], 'Delete the worktree for t-003 "Title of t-003", t-004 "Title of t-004", job billing?')
        self.assertIn("Tasks still t-003 in-progress, t-004 review; this job's commits are all merged", card["context"].split("\n"))
        # a done task's main worktree with the default job: today's question, no Job line
        self.ledger({"version": 1, "tasks": []})
        shutil.rmtree(os.path.join(self.dir, "decisions"))
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


if __name__ == "__main__":
    unittest.main()
