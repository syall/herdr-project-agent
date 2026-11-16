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
        code, out, err = self.worker("keep", "t31", "--off")
        self.assertEqual(code, 0, err)
        w = self.entry("t-003", "t31")
        self.assertEqual((w["keep"], w["keepQuote"]), (False, "keep t31, I want to ask it something"))
        self.assertRegex(w["unkeptAt"], r"^\d{4}-")
        self.assertIn("no longer kept", self.journal()[-1]["why"])
        self.assertEqual(self.worker("close", "t31", "--evidence", "FINDINGS.md")[0], 0)

    def test_k1b_refusals(self):
        before = self.snapshot()
        for quote in ([], ["--quote", "  "]):
            self.assertRefused(self.worker("keep", "t31", *quote), 'pa-worker keep needs --quote "<the person\'s own words>"')
        self.assertRefused(self.worker("keep", "t99", "--quote", "x"), "t99 is not a recorded worker of this space")
        self.assertRefused(self.worker("keep", "t0", "--quote", "x"), "that is the Project Agent itself")
        self.assertRefused(self.worker("keep", "m1/t31", "--quote", "x"), "m1/t31 is on another machine")
        self.assertEqual(self.snapshot(), before)
        code, out, _ = self.worker("keep", "t31", "--off")
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


if __name__ == "__main__":
    unittest.main()
