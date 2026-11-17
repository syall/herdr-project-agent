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
        held = threading.Event()

        def hold():
            with open(os.path.join(self.dir, ".ledger.lock"), "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                held.set()
                time.sleep(0.5)
        t = threading.Thread(target=hold)
        t.start()
        held.wait(5)
        began = time.time()
        code, _, err = self.close("t31", "--evidence", "FINDINGS.md")
        waited = time.time() - began
        t.join()
        self.assertEqual(code, 0, err)
        self.assertGreaterEqual(waited, 0.3)
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
    """R5: pa-worker close --leftovers [--dry-run] (spec 3.4)."""

    def setUp(self):
        super().setUp()
        pa_hooks.by_an_agent = lambda: True

    def add(self, tid, address, name, status="idle", report=True, worktree=None, **entry):
        self.add_task(tid, "in-progress", worktree, [dict({"address": address, "role": "build", "name": name}, **entry)])
        self.term(address, name, status=status)
        self.evidence(tid)

    def note(self, **line):
        with open(os.path.join(self.dir, "journal.jsonl"), "a") as f:
            f.write(json.dumps(dict({"at": "2026-11-15T10:00:00Z"}, **line)) + "\n")

    def mix(self):
        """Seven old-style workers: one passes every guard, six are kept for different reasons."""
        proj = self.repo()
        dirty = self.worktree(proj, "t-004-x")
        write(os.path.join(dirty, "README"), "changed\n")
        self.add("t-001", "t2s", "t-001-build")
        self.note(trigger="report", task="t-001", why="t2s reported; read FINDINGS.md")
        self.add("t-002", "t34", "t-002-build", status="working")
        self.note(trigger="report", task="t-002", why="t34 reported")
        self.add("t-003", "t36", "t-003-build", status="blocked")
        self.note(trigger="report", task="t-003", why="t36 reported")
        self.add("t-004", "t37", "t-004-build", worktree=dirty)
        self.note(trigger="report", task="t-004", why="t37 reported")
        self.add("t-005", "t38", "t-005-build")
        self.add("t-006", "t39", "t-006-build")
        self.note(trigger="report", task="t-006", why="t39 reported; read FINDINGS.md")
        self.note(trigger="message", task="t-006", why="sent update to t39: fix the lint")
        self.add("t-007", "t40", "t-007-build", keep=True, keptAt="2026-11-16T09:00:00Z", keepQuote="keep t40")
        self.note(trigger="report", task="t-007", why="t40 reported; FINDINGS.md")
        return os.path.realpath(dirty)

    def expect_kept(self, out, dirty):
        for line in ("kept t34 (t-002-build, t-002): working", "kept t36 (t-003-build, t-003): blocked",
                     "kept t37 (t-004-build, t-004): uncommitted files in {} (1)".format(dirty),
                     "kept t38 (t-005-build, t-005): no recorded report",
                     "kept t39 (t-006-build, t-006): no recorded report after its last handoff",
                     "kept t40 (t-007-build, t-007): marked keep"):
            self.assertIn(line, out)

    def test_l1_the_mix(self):
        dirty = self.mix()
        lines = len(self.journal())
        code, out, err = self.worker("close", "--leftovers")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ended(), [["hc", "kill", "t2s"], ["hc", "rm", "t2s"]])
        self.expect_kept(out, dirty)
        w = self.entry("t-001", "t2s")
        self.assertEqual((w["finished"], w["closedBy"], w["report"]), (True, "leftovers", "FINDINGS.md"))
        for tid, address in (("t-002", "t34"), ("t-003", "t36"), ("t-004", "t37"), ("t-005", "t38"), ("t-006", "t39"), ("t-007", "t40")):
            self.assertFalse(self.entry(tid, address).get("finished"), address)
        new = self.journal()[lines:]
        self.assertEqual([l["trigger"] for l in new], ["leftovers", "leftovers"])
        self.assertIn("closing old-style workers: t2s (reports read); keeping t34 (working), t36 (blocked)", new[0]["why"])
        self.assertIn("closed worker t2s (t-001-build)", new[1]["why"])
        policy = self.ledger()["workerPolicy"]
        self.assertEqual(policy["style"], "per-job")
        self.assertRegex(policy["since"], r"^\d{4}-")

    def test_l2_dry_run_writes_nothing(self):
        dirty = self.mix()
        before = self.snapshot()
        code, out, err = self.worker("close", "--leftovers", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("would close t2s (t-001-build, t-001): report FINDINGS.md", out)
        self.expect_kept(out, dirty)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.calls, [], "a dry run makes no hc call but hc get")
        self.assertNotIn("workerPolicy", self.ledger())

    def test_l3_never_unrecorded_pa_or_gone(self):
        self.term("t77", "t-001-build")  # not in the ledger, even with a matching name
        self.add_task("t-001", "in-progress", None, [{"address": "t0", "role": "build", "name": "PA: S"},
                                                    {"address": "t2g", "role": "build", "name": "t-001-old"}])
        self.term("t0", "PA: S")
        self.evidence("t-001")
        self.note(trigger="report", task="t-001", why="t2g reported; FINDINGS.md; t0 too")
        code, out, _ = self.worker("close", "--leftovers")
        self.assertEqual(code, 0)
        self.assertEqual(self.ended(), [])
        self.assertIn("kept t0 (PA: S, t-001): it is the Project Agent", out)
        self.assertIn("kept t2g (t-001-old, t-001): gone; left for the agent to read", out)
        self.assertFalse(self.entry("t-001", "t2g").get("finished"))
        self.assertIn("t77", self.terms)

    def test_l4_nothing_to_do(self):
        self.add("t-001", "t31", "t-001-build", finished=True)
        code, out, _ = self.worker("close", "--leftovers")
        self.assertEqual(code, 0)
        self.assertIn("No old-style workers to close.", out)
        self.assertEqual(self.ledger()["workerPolicy"]["style"], "per-job")
        self.assertEqual(self.ended(), [])

    def test_l5_whole_word_address(self):
        self.add("t-001", "t31", "t-001-build")
        self.note(trigger="report", task="t-001", why="t310 reported; FINDINGS.md")
        code, out, _ = self.worker("close", "--leftovers", "--dry-run")
        self.assertIn("kept t31 (t-001-build, t-001): no recorded report", out)

    def test_l6_newest_file_and_name_match(self):
        self.add("t-001", "t31", "t-001-tests")
        old = self.evidence("t-001", "FINDINGS.md")
        os.utime(old, (time.time() - 3600, time.time() - 3600))
        self.evidence("t-001", "VERIFY.md")
        self.evidence("t-001", "EMPTY.md", "")
        # review round 1, I2: the line must say this worker reported ("t-001-tests is done" no longer counts)
        self.note(trigger="report", task="t-001", why="t-001-tests reported")
        code, out, err = self.worker("close", "--leftovers")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.entry("t-001", "t31")["report"], "VERIFY.md")

    def test_l6b_empty_evidence_folder(self):
        self.add("t-001", "t31", "t-001-tests")
        os.unlink(os.path.join(self.dir, "evidence", "t-001", "FINDINGS.md"))
        self.note(trigger="report", task="t-001", why="t31 reported")
        code, out, _ = self.worker("close", "--leftovers", "--dry-run")
        self.assertIn("kept t31 (t-001-tests, t-001): no recorded report", out)

    def test_l7_status_changes_mid_run(self):
        self.add("t-001", "t31", "t-001-a")
        self.note(trigger="report", task="t-001", why="t31 reported")
        self.add("t-002", "t32", "t-002-a")
        self.note(trigger="report", task="t-002", why="t32 reported")
        real, seen = self.fake_get, []

        def flipping(address):
            if address == "t32":
                seen.append(address)
                if len(seen) > 1:
                    self.terms["t32"]["status"] = "working"
            return real(address)
        pa.hc_get = flipping
        lines = len(self.journal())
        code, out, err = self.worker("close", "--leftovers")
        self.assertEqual(code, 0, err)
        self.assertIn("kept t32 (t-002-a, t-002): working", out)
        self.assertNotIn(["hc", "kill", "t32"], self.calls)
        new = self.journal()[lines:]
        self.assertIn("t32", new[0]["why"])  # the plan line still says it was to be closed
        self.assertFalse(any("closed worker t32" in l["why"] for l in new))
        self.assertTrue(self.entry("t-001", "t31")["finished"])
        self.assertFalse(self.entry("t-002", "t32").get("finished"))

    # ---- review round 1 ----
    def test_l8_a_line_about_another_worker_never_counts(self):  # I2
        self.add_task("t-003", "in-progress", None, [{"address": "t31", "role": "build", "name": "t-003-build"},
                                                    {"address": "t32", "role": "verifier", "name": "t-003-verify"}])
        self.term("t31", "t-003-build")
        self.term("t32", "t-003-verify")
        self.evidence("t-003")
        self.note(trigger="message", task="t-003", why="sent t32 the verify handoff")
        self.note(trigger="report", task="t-003", why="t31 reported FINDINGS.md; t32 has not reported yet")
        code, out, _ = self.worker("close", "--leftovers", "--dry-run")
        self.assertIn("would close t31 (t-003-build, t-003): report FINDINGS.md", out)
        self.assertIn("kept t32 (t-003-verify, t-003): no recorded report", out)

    def test_l8b_the_lines_from_field_counts(self):  # I2
        self.add("t-001", "t31", "t-001-build")
        self.note(trigger="report", task="t-001", **{"from": "t31"}, why="FINDINGS.md is in")
        code, out, _ = self.worker("close", "--leftovers", "--dry-run")
        self.assertIn("would close t31 (t-001-build, t-001): report FINDINGS.md", out)

    def test_l8c_a_report_older_than_the_worker_does_not_count(self):  # I4 for leftovers
        self.add("t-001", "t31", "t-001-build")
        old = os.path.join(self.dir, "evidence", "t-001", "FINDINGS.md")
        os.utime(old, (time.time() - 7200, time.time() - 7200))
        self.terms["t31"]["created_ms"] = int((time.time() - 3600) * 1000)
        self.note(trigger="report", task="t-001", why="t31 reported FINDINGS.md")
        code, out, _ = self.worker("close", "--leftovers", "--dry-run")
        self.assertIn("kept t31 (t-001-build, t-001): no recorded report", out)

    def test_l9_after_the_policy_is_set_it_does_nothing(self):  # nit
        self.add("t-001", "t31", "t-001-build")
        self.note(trigger="report", task="t-001", why="t31 reported FINDINGS.md")
        led = self.ledger()
        led["workerPolicy"] = {"style": "per-job", "since": "2026-11-16T10:00:00Z"}
        self.ledger(led)
        before = self.snapshot()
        code, out, _ = self.worker("close", "--leftovers")
        self.assertEqual(code, 0)
        self.assertIn("--leftovers already ran (workerPolicy since 2026-11-16T10:00:00Z); nothing was done", out)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.ended(), [])

    # ---- the verifier's surviving mutants (VERIFY.md section 4) ----
    def test_l10_a_handoff_that_names_a_file_keeps_the_worker(self):  # lo-handoff
        self.add("t-001", "t31", "t-001-build")
        self.note(trigger="message", task="t-001", why="sent t31 an update, see FINDINGS.md")
        self.add("t-002", "t32", "t-002-build")
        self.note(trigger="report", task="t-002", why="t32 reported FINDINGS.md; then asked t32 for a fix round")
        code, out, _ = self.worker("close", "--leftovers", "--dry-run")
        self.assertIn("kept t31 (t-001-build, t-001): no recorded report after its last handoff", out)
        self.assertIn("kept t32 (t-002-build, t-002): no recorded report after its last handoff", out)

    def test_l11_dry_run_lists_a_gone_worker_as_kept(self):  # lo-gone
        self.add("t-001", "t31", "t-001-build")
        self.note(trigger="report", task="t-001", why="t31 reported FINDINGS.md")
        del self.terms["t31"]
        code, out, _ = self.worker("close", "--leftovers", "--dry-run")
        self.assertIn("kept t31 (t-001-build, t-001): gone; left for the agent to read", out)
        self.assertNotIn("would close", out)

    def test_l12_a_real_run_takes_the_ledger_lock(self):  # lo-nolock
        import fcntl
        self.add("t-001", "t31", "t-001-build")
        self.note(trigger="report", task="t-001", why="t31 reported FINDINGS.md")
        held = threading.Event()

        def other():
            with open(os.path.join(self.dir, ".ledger.lock"), "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                led = self.ledger()  # a read-modify-write like pa-task's, under the lock
                held.set()
                time.sleep(0.4)
                led["tasks"].append({"id": "t-009", "title": "added meanwhile", "state": "queued", "workers": []})
                self.ledger(led)
        t = threading.Thread(target=other)
        t.start()
        held.wait(5)
        code, _, err = self.worker("close", "--leftovers")
        t.join()
        self.assertEqual(code, 0, err)
        led = self.ledger()
        self.assertEqual([x["id"] for x in led["tasks"]], ["t-001", "t-009"], "the other writer's task survives")
        self.assertTrue(self.entry("t-001", "t31")["finished"])
        self.assertIn("workerPolicy", led)


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
        real = shutil.which("git")
        bin_dir = os.path.join(self.tmp, "bin")
        write(os.path.join(bin_dir, "git"), '#!/bin/sh\nfor a in "$@"; do [ "$a" = log ] && exec sleep 5; done\nexec "{}" "$@"\n'.format(real))
        os.chmod(os.path.join(bin_dir, "git"), 0o755)
        self.commit(self.wt, "a.txt", "a\n", "slow")
        saved_path, saved_timeout = os.environ["PATH"], pa_hooks.GIT_TIMEOUT
        os.environ["PATH"] = bin_dir + os.pathsep + saved_path
        pa_hooks.GIT_TIMEOUT = 0.5
        try:
            began = time.time()
            card = self.open_card()
            self.assertLess(time.time() - began, 5)
        finally:
            os.environ["PATH"], pa_hooks.GIT_TIMEOUT = saved_path, saved_timeout
        self.assertIn("timed out", card["changes"]["repos"][0]["error"])
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
        write(os.path.join(bin_dir, "git"), '#!/bin/sh\nfor a in "$@"; do [ "$a" = log ] && exec sleep 5; done\nexec "{}" "$@"\n'.format(real))
        os.chmod(os.path.join(bin_dir, "git"), 0o755)
        saved = os.environ["PATH"], pa_hooks.GIT_TIMEOUT, pa_hooks.DESCRIBE_DEADLINE
        os.environ["PATH"] = bin_dir + os.pathsep + saved[0]
        pa_hooks.GIT_TIMEOUT, pa_hooks.DESCRIBE_DEADLINE = 1, 1.5
        try:
            began = time.time()
            card = self.open_card(worktree=kind)
            took = time.time() - began
        finally:
            os.environ["PATH"], pa_hooks.GIT_TIMEOUT, pa_hooks.DESCRIBE_DEADLINE = saved
        self.assertLess(took, 4)
        self.assertIn("Not described (time limit of 1.5 s): r", self.md())
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


if __name__ == "__main__":
    unittest.main()
