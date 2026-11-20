"""t-169: pa-handoff writes a worker's handoff from the ledger, git, the active rules and the known-flake list.

No hc calls and no real homes: HOME, HERDR_PA_HOME, CLAUDE_CONFIG_DIR and HERDR_APP_HOME point at a temp folder made
with mkdtemp; a fake space sp1 holds agent.json, ledger.json, rules.json, flakes.txt and evidence/. A temp git repository
(main and the branch t-1-widget, fixed dates) is built once and copied per test (tests/repo_template.py). The golden
handoffs are tests/samples/handoff/<kind>.md with <EV>, <WT> and <C0>.. <C4> (short commit ids) filled in per test.

Run: python3 -m unittest -v tests/test_pa_handoff.py
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

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.realpath(os.path.join(HERE, ".."))
TOOLS = os.path.join(REPO, "runtime", "tools")
GOLDEN = os.path.join(HERE, "samples", "handoff")
TMP = os.path.realpath(tempfile.mkdtemp(prefix="t169-handoff-"))
SPACE = "sp1"
sys.path.insert(0, HERE)
import repo_template  # noqa: E402

_HOMES = {"HERDR_PA_HOME": os.path.join(TMP, "pa"), "CLAUDE_CONFIG_DIR": os.path.join(TMP, "claude"),
          "HERDR_APP_HOME": os.path.join(TMP, "app"), "HOME": os.path.join(TMP, "home")}
DROP_ENV = ("HC_TERMINAL_ID", "HC_SOCKET", "HC_TERMINAL_SOCKET", "HC_SCOPE", "CLAUDECODE")
_saved_env = {k: os.environ.get(k) for k in list(_HOMES) + list(DROP_ENV)}
_saved_mods = {k: sys.modules.get(k) for k in ("pa", "pa_hooks", "pa_handoff")}
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
    pa_handoff = _load("pa_handoff")
except FileNotFoundError:  # before the tool exists (the failing-tests commit)
    pa_handoff = None
for _k, _v in _saved_mods.items():
    if _v is None:
        sys.modules.pop(_k, None)
    else:
        sys.modules[_k] = _v
for _k in _HOMES:
    if _saved_env[_k] is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _saved_env[_k]
assert pa.ROOT.startswith(TMP), "pa.py must use the temp homes"

FEW = ("In this space, a verifier's or check worker's mutations are a small sample, not a sweep: low tier at most 2, "
       "normal and high tier at most 5 on a first check, and none on a recheck unless the fix changed logic (then at "
       "most 3, on the changed lines only). Each mutation runs only the tests that cover it, never the suite.")
RULES = {"version": 1, "rules": [
    {"id": "r-few-mutations", "title": "Few mutations", "text": FEW, "status": "active", "order": 110},
    {"id": "r-test-runs", "title": "Where test runs go", "text": "Every other test run runs with pa-run.",
     "status": "active", "order": 175}]}
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
           "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z", "GIT_CONFIG_NOSYSTEM": "1"}


def setUpModule():
    for k in DROP_ENV:
        os.environ.pop(k, None)
    os.environ.update(_HOMES)
    os.makedirs(_HOMES["HOME"], exist_ok=True)


def tearDownModule():
    for k, v in _saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    shutil.rmtree(TMP, ignore_errors=True)


def read(path):
    with open(path) as f:
        return f.read()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def git(path, *args):
    env = dict(os.environ, **GIT_ENV)
    r = subprocess.run(["git", "-C", path] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True, env=env)
    if r.returncode:
        raise AssertionError("git {}: {}".format(args, r.stderr))
    return r.stdout.strip()


def commit(path, name, text, subject):
    write(os.path.join(path, name), text)
    git(path, "add", name)
    git(path, "commit", "-q", "-m", subject)
    return git(path, "rev-parse", "--short=7", "HEAD")


def build_repo(path):
    git(path, "init", "-q", "-b", "main")
    c0 = commit(path, "a.txt", "base\n", "base")
    git(path, "checkout", "-q", "-b", "t-1-widget")
    commit(path, "tests/test_w.py", "x = 1\n", "t-1 tests (failing at {})".format(c0))
    commit(path, "widget.py", "y = 1\n", "t-1: the widget")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="case-", dir=TMP)
        self.wt = repo_template.copy(TMP, "handoff-repo", os.path.join(self.tmp, "wt"), build_repo)
        self.space = pa.space_dir(SPACE)
        shutil.rmtree(self.space, ignore_errors=True)
        os.makedirs(self.space)
        self.addCleanup(shutil.rmtree, self.space, True)
        write(os.path.join(self.space, "agent.json"),
              json.dumps({"version": 1, "space": SPACE, "profile": "work", "terminal": "t9pa"}))
        self.write_rules(RULES)
        write(os.path.join(self.space, "flakes.txt"),
              "# known flakes, one per line\ntest_a test_x1\n\ntest_b test_y (a race)\n")
        log = git(self.wt, "log", "--format=%h", "--reverse", "--topo-order", "--abbrev=7", "t-1-widget").split()
        self.c = {"C0": log[0], "C1": log[1], "C2": log[2]}
        self.ledger = {"version": 1, "tasks": [
            {"id": "t-1", "title": "Widget: one thing", "tier": "low", "worktree": self.wt,
             "worktrees": [{"path": self.wt, "job": "main"}], "evidence": "evidence/t-1",
             "workers": [{"address": "t1b", "role": "build", "round": 1}]},
            {"id": "t-2", "title": "Gadget: two things", "tier": "normal", "worktree": self.wt,
             "worktrees": [{"path": self.wt, "job": "main"}, {"path": self.wt, "job": "j2"}], "evidence": "evidence/t-2",
             "workers": [{"address": "t2b", "role": "build", "round": 1}]}]}
        self.write_ledger()
        for name in ("intent.md", "plan.md", "FINDINGS.md", "DECISIONS.md", "build.log"):
            write(os.path.join(self.ev("t-1"), name), name + "\n")
        for name in ("plan.md", "FINDINGS.md", "DECISIONS.md", "build.log"):
            write(os.path.join(self.ev("t-2"), name), name + "\n")

    def ev(self, task):
        return os.path.join(self.space, "evidence", task)

    def write_rules(self, doc):
        write(os.path.join(self.space, "rules.json"), json.dumps(doc))

    def write_ledger(self):
        write(os.path.join(self.space, "ledger.json"), json.dumps(self.ledger))

    def task(self, tid):
        return next(t for t in self.ledger["tasks"] if t["id"] == tid)

    def run_tool(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = pa_handoff.main(list(argv) + ["--space", SPACE])
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def ok(self, *argv):
        code, out, err = self.run_tool(*argv)
        self.assertEqual(code, 0, err)
        path = out.strip()
        self.assertTrue(os.path.isfile(path), out)
        return path, read(path), err

    def golden(self, kind, task):
        text = read(os.path.join(GOLDEN, kind + ".md"))
        for k, v in dict(self.c, EV=self.ev(task), WT=self.wt).items():
            text = text.replace("<{}>".format(k), v)
        return text

    def round2(self):
        """The round 1 check found 1 Must fix at C2; round 2 committed a failing test (C3) and the fix (C4)."""
        full = git(self.wt, "rev-parse", "HEAD")
        write(os.path.join(self.ev("t-1"), "REVIEW.md"),
              "# Review\n\n## Must fix\n\nM1 the lock.\n\nChecked: {}\nVerdict: must fix (1)\n".format(full))
        write(os.path.join(self.ev("t-1"), "VERIFY.md"), "# Verify\n\nChecked: {}\nVerdict: pass\n".format(full))
        self.c["C3"] = commit(self.wt, "tests/test_w.py", "x = 2\n", "t-1 round 2 test (failing at {})".format(self.c["C2"]))
        self.c["C4"] = commit(self.wt, "widget.py", "y = 2\n", "t-1 round 2 fix (REVIEW M1)")


class Golden(Base):
    def test_g1_check(self):
        path, text, _ = self.ok("check", "t-1", "--public", "--items", "Judge D2 (the cache).")
        self.assertEqual(path, os.path.join(self.ev("t-1"), "handoff-check.md"))
        self.assertEqual(text, self.golden("check", "t-1"))

    def test_g2_recheck(self):
        self.round2()
        self.task("t-1")["workers"] += [{"address": "t1c", "role": "check", "round": 1},
                                        {"address": "t1f", "role": "build", "round": 2}]
        self.write_ledger()
        path, text, _ = self.ok("recheck", "t-1", "--public", "--logic", "--items", "M1: the lock is read first.")
        self.assertEqual(path, os.path.join(self.ev("t-1"), "handoff-check-r2.md"))
        self.assertEqual(text, self.golden("recheck", "t-1"))

    def test_g3_fix(self):
        full = git(self.wt, "rev-parse", "HEAD")
        write(os.path.join(self.ev("t-1"), "REVIEW.md"), "## Must fix\n\nM1\n\nChecked: {}\nVerdict: must fix (1)\n".format(full))
        write(os.path.join(self.wt, ".claude", "pa-task.json"), json.dumps({"task": "t-1", "space": SPACE}))
        self.task("t-1")["workers"].append({"address": "t1c", "role": "check", "round": 1})
        self.write_ledger()
        path, text, _ = self.ok("fix", "t-1", "--public", "--items", "M1 only, in widget.py.")
        self.assertEqual(path, os.path.join(self.ev("t-1"), "handoff-fix.md"))
        self.assertEqual(text, self.golden("fix", "t-1"))

    def test_g4_verify(self):
        path, text, _ = self.ok("verify", "t-2", "--job", "j2", "--public")
        self.assertEqual(path, os.path.join(self.ev("t-2"), "handoff-verify-j2.md"))
        self.assertEqual(text, self.golden("verify", "t-2"))

    def test_g5_review(self):
        items = os.path.join(self.tmp, "items.txt")
        write(items, "The guard is not weakened.\n")
        path, text, _ = self.ok("review", "t-2", "--job", "j2", "--items-file", items)
        self.assertEqual(path, os.path.join(self.ev("t-2"), "handoff-review-j2.md"))
        self.assertEqual(text, self.golden("review", "t-2"))

    def test_g6_queue(self):
        seen = []
        brief = ("t-1 (position 1 of 1, on main at {C0}, tip {C2}, state checking)\nworktree: {WT}\n"
                 "report with hc send t9pa when done: what ran, its result, and the review file's path").format(WT=self.wt, **self.c)

        def fake(directory, repo, task, job):
            seen.append((directory, repo, task, job))
            return brief
        old, pa_handoff.queue_brief = pa_handoff.queue_brief, fake
        self.addCleanup(setattr, pa_handoff, "queue_brief", old)
        path, text, _ = self.ok("queue", "t-1", "--items", "Look at the README change.")
        self.assertEqual(path, os.path.join(self.ev("t-1"), "handoff-queue.md"))
        self.assertEqual(text, self.golden("queue", "t-1"))
        # the repo is the worktree's main checkout (its git common dir's parent): here the copy itself
        self.assertEqual(seen, [(self.space, os.path.realpath(self.wt), "t-1", None)])


class Numbers(Base):
    """R3: the numbers come from the active rules, never from copied text."""

    def test_n1_field_wins_over_text(self):
        doc = json.loads(json.dumps(RULES))
        doc["rules"][0]["mutations"] = {"low": 1, "normal": 4, "high": 6, "recheck": 2}
        self.write_rules(doc)
        _p, text, _ = self.ok("check", "t-1")
        self.assertIn("3. At most 1 mutations on changed logic (r-few-mutations, low tier)", text)

    def test_n2_no_rule_no_number(self):
        doc = json.loads(json.dumps(RULES))
        doc["rules"][0]["status"] = "retired"
        doc["rules"][1]["status"] = "retired"
        self.write_rules(doc)
        _p, text, _ = self.ok("check", "t-1")
        self.assertIn("3. A few mutations on changed logic (no active r-few-mutations rule gives a number), each running "
                      "only the tests that cover it -> check-mut.log.", text)
        self.assertNotIn("At most", text)
        self.assertIn("(pa-run --local; say so)", text)  # no r-test-runs: the old wording

    def test_n3_unparsed_text_is_noted(self):
        doc = json.loads(json.dumps(RULES))
        doc["rules"][0]["text"] = "Mutations are few."
        self.write_rules(doc)
        _p, text, err = self.ok("verify", "t-2")
        self.assertIn("A few mutations on changed logic (no active r-few-mutations rule gives a number)", text)
        self.assertIn("r-few-mutations", err)

    def test_n4_recheck_without_logic_asks_none(self):
        self.round2()
        _p, text, _ = self.ok("recheck", "t-1", "--round", "2")
        self.assertIn("3. No mutations (r-few-mutations: a recheck whose fix changed no logic).", text)
        self.assertNotIn("check-mut-r2.log", text)

    def test_n5_no_mutations_flag(self):
        _p, text, _ = self.ok("check", "t-1", "--no-mutations")
        self.assertIn("3. No mutations (r-few-mutations: a docs-only or test-only change).", text)

    def test_n6_high_tier(self):
        self.task("t-2")["tier"] = "high"
        self.write_ledger()
        _p, text, _ = self.ok("verify", "t-2")
        self.assertIn("At most 5 mutations on changed logic (r-few-mutations, high tier, first check)", text)
        _p, text, _ = self.ok("fix", "t-2", "--base", self.c["C2"])
        self.assertIn("pa-full-suite once at the end of the Test phase, before the commit.", text)
        self.assertNotIn("No pa-full-suite", text)

    def test_n7_verify_diff(self):
        self.round2()
        shutil.copy(os.path.join(self.ev("t-1"), "REVIEW.md"), os.path.join(self.ev("t-2"), "REVIEW.md"))
        _p, text, _ = self.ok("verify", "t-2", "--diff", "--round", "2", "--logic")
        self.assertIn("Diff: {}..{}.".format(self.c["C2"], self.c["C4"]), text)
        self.assertIn("At most 3 mutations, on the changed lines only (r-few-mutations, a fix that changed logic)", text)
        self.assertIn("Append '## Round 2 (diff {}..{})' to VERIFY.md".format(self.c["C2"], self.c["C4"]), text)
        self.assertTrue(_p.endswith("handoff-verify-r2.md"), _p)

    def test_n8_flakes(self):
        self.assertEqual(pa_handoff.parse_flakes("# c\n\n a b \nc\n"), ["a b", "c"])
        os.unlink(os.path.join(self.space, "flakes.txt"))
        _p, text, _ = self.ok("check", "t-1")
        self.assertNotIn("Known flake", text)
        self.assertIn("output to full-suite-check.out.\n", text)


class Refusals(Base):
    def refused(self, *argv):
        code, out, err = self.run_tool(*argv)
        self.assertNotEqual(code, 0, out)
        self.assertEqual(out, "")
        return err

    def test_r1_unknown_task(self):
        self.assertIn("t-9", self.refused("check", "t-9"))

    def test_r2_unknown_job(self):
        self.assertIn("j9", self.refused("verify", "t-2", "--job", "j9"))

    def test_r3_existing_handoff_needs_force(self):
        path, first, _ = self.ok("check", "t-1")
        write(path, "the PA's edit\n")
        self.assertIn("--force", self.refused("check", "t-1"))
        self.assertEqual(read(path), "the PA's edit\n")
        _p, again, _ = self.ok("check", "t-1", "--force")
        self.assertEqual(again, first)

    def test_r4_check_is_low_tier_only(self):
        self.assertIn("low tier", self.refused("check", "t-2"))
        self.assertIn("low tier", self.refused("recheck", "t-2", "--base", self.c["C1"]))

    def test_r5_recheck_needs_a_checked_commit(self):
        self.assertIn("--base", self.refused("recheck", "t-1"))
        self.assertIn("REVIEW.md", self.refused("fix", "t-1"))

    def test_r6_writes_only_the_handoff(self):
        before = {n: read(os.path.join(self.space, n)) for n in ("ledger.json", "rules.json", "agent.json")}
        listing = sorted(os.listdir(self.space))
        self.ok("check", "t-1")
        self.assertEqual({n: read(os.path.join(self.space, n)) for n in before}, before)
        self.assertEqual(sorted(os.listdir(self.space)), listing)
        self.assertEqual(sorted(os.listdir(self.ev("t-1"))),
                         sorted(["intent.md", "plan.md", "FINDINGS.md", "DECISIONS.md", "build.log", "handoff-check.md"]))

    def test_r7_items_and_items_file_exclusive(self):
        items = os.path.join(self.tmp, "i.txt")
        write(items, "x\n")
        code, _out, _err = self.run_tool("check", "t-1", "--items", "a", "--items-file", items)
        self.assertNotEqual(code, 0)

    def test_r8_no_pa_address(self):
        write(os.path.join(self.space, "agent.json"), json.dumps({"version": 1, "space": SPACE}))
        self.assertIn("terminal", self.refused("check", "t-1"))


class Round2(Base):
    """t-169 round 2: RL1 test commits by the files they touch, RL2 queue from pa-queue's entry, N1 the pass form."""

    def test_rl1_test_commits_by_files(self):
        c3 = commit(self.wt, "widget.py", "y = 3\n", "t-1: the widget, failing-first test fixed (failing-test commit)")
        c4 = commit(self.wt, "tests/test_v.py", "v = 1\n", "t-1 more tests")
        write(os.path.join(self.wt, "tests", "test_w.py"), "x = 9\n")
        write(os.path.join(self.wt, "widget.py"), "y = 9\n")
        git(self.wt, "add", "tests/test_w.py", "widget.py")
        git(self.wt, "commit", "-q", "-m", "t-1 tests and code (failing at {})".format(c4))
        _p, text, _ = self.ok("check", "t-1")
        self.assertIn("(tests {}, {}; base main {})".format(self.c["C1"], c4, self.c["C0"]), text)
        self.assertIn("Failing-first: the tests of {}, {} fail at".format(self.c["C1"], c4), text)
        where = next(l for l in text.splitlines() if l.startswith("Worktree "))
        self.assertNotIn(c3, where)

    def queue_entry(self, job=None):
        repo = os.path.join(self.tmp, "main-checkout")
        entry = {"task": "t-1", "job": job, "branch": "t-1-widget", "worktree": os.path.join(self.tmp, "jwt"),
                 "onto": "main", "state": "checking"}
        write(os.path.join(self.space, "queue.json"),
              json.dumps({"version": 1, "repos": {repo: {"common": repo, "main": "main", "entries": [entry],
                                                         "planned": [], "left": [], "merged": []}}}))
        return repo

    def fake_brief(self):
        seen = []

        def fake(directory, repo, task, job):
            seen.append((repo, task, job))
            return "t-1 (position 1 of 1)"
        old, pa_handoff.queue_brief = pa_handoff.queue_brief, fake
        self.addCleanup(setattr, pa_handoff, "queue_brief", old)
        return seen

    def test_rl2_queue_without_ledger_worktree(self):
        repo = self.queue_entry(None)
        gone = os.path.join(self.tmp, "gone")
        self.task("t-1")["worktree"] = gone
        self.task("t-1")["worktrees"] = [{"path": gone, "job": "main"}, {"path": gone, "job": "J7"}]
        self.write_ledger()
        seen = self.fake_brief()
        path, text, _ = self.ok("queue", "t-1")
        self.assertEqual(path, os.path.join(self.ev("t-1"), "handoff-queue.md"))
        self.assertTrue(text.startswith("t-1 (position 1 of 1)\n"), text)
        self.assertEqual(seen, [(repo, "t-1", None)])

    def test_rl2_queue_job_from_entry(self):
        repo = self.queue_entry("J7")
        self.task("t-1")["worktrees"] = []
        self.task("t-1")["worktree"] = None
        self.write_ledger()
        seen = self.fake_brief()
        path, _text, _ = self.ok("queue", "t-1")
        self.assertEqual(seen, [(repo, "t-1", "J7")])
        self.assertTrue(path.endswith("handoff-queue-J7.md"), path)

    def passed_review(self):
        full = git(self.wt, "rev-parse", "HEAD")
        write(os.path.join(self.ev("t-1"), "REVIEW.md"),
              "# Review\n\n## Must fix\nNone.\n\n## Recorded limits\n- RL1 x\n\nChecked: {}\nVerdict: pass\n".format(full))

    def test_n1_fix_after_a_pass(self):
        self.passed_review()
        _p, text, _ = self.ok("fix", "t-1", "--items", "RL1 only.")
        self.assertIn("The check at {} passed with Recorded limits to fix: read {}/REVIEW.md '## Recorded limits' "
                      "(and Nits). Also read plan.md".format(self.c["C2"], self.ev("t-1")), text)
        self.assertIn("\nFix only the Recorded limits and Nits named here; nothing else. RL1 only.\n", text)
        self.assertNotIn("Must fix", text)

    def test_n1_recheck_after_a_pass(self):
        self.passed_review()
        self.c["C3"] = commit(self.wt, "tests/test_w.py", "x = 2\n", "t-1 round 2 test (failing at {})".format(self.c["C2"]))
        self.c["C4"] = commit(self.wt, "widget.py", "y = 2\n", "t-1 round 2 fix (REVIEW RL1)")
        _p, text, _ = self.ok("recheck", "t-1", "--round", "2", "--logic")
        self.assertIn("The round 1 check at {} passed with Recorded limits to fix (read {}/REVIEW.md '## Recorded "
                      "limits'); round 2 fixed them: read FINDINGS.md".format(self.c["C2"], self.ev("t-1")), text)
        self.assertIn("4. Review: each Recorded limit to fix is closed and nothing else changed.", text)
        self.assertIn("Must-fix bar: breaks a fix, a verification point, safety or security.", text)
        self.assertNotIn("'## Must fix'", text)


class QueueWiring(unittest.TestCase):
    def test_q1_queue_brief_calls_pa_queue_brief(self):
        calls = []
        fake = types.ModuleType("pa_queue")

        class Refused(Exception):
            pass

        def cmd_brief(args):
            calls.append((args.task, args.job, args.repo, args.space))
            print("brief line")
            return 0
        fake.Refused, fake.cmd_brief = Refused, cmd_brief
        saved = sys.modules.get("pa_queue")
        sys.modules["pa_queue"] = fake
        try:
            self.assertEqual(pa_handoff.queue_brief("/s/sp1", "/r", "t-1", None), "brief line")
        finally:
            if saved is None:
                sys.modules.pop("pa_queue", None)
            else:
                sys.modules["pa_queue"] = saved
        self.assertEqual(calls, [("t-1", None, "/r", "sp1")])


class Wiring(unittest.TestCase):
    def test_w1_install_links_pa_handoff(self):
        lines = [l for l in read(os.path.join(REPO, "install.sh")).splitlines() if "pa_handoff.py" in l]
        self.assertEqual(lines, ['for n in pa-handoff; do ln -sfn "$PA/tools/pa_handoff.py" "$HOME/.local/bin/$n"; done'])

    def test_w2_core_tells_the_pa(self):
        text = read(os.path.join(REPO, "runtime", "core", "orchestrator.md"))
        workers = text.split("## Workers", 1)[1].split("\n## ", 1)[0]
        self.assertIn("pa-handoff", " ".join(workers.split()))

    def test_w3_readme_and_37_parse(self):
        self.assertIn("## Handoffs (`pa-handoff`)", read(os.path.join(REPO, "README.md")))
        self.assertIn("runtime/tools/pa_handoff.py", read(os.path.join(REPO, "tests", "run-all.sh")))


if __name__ == "__main__":
    unittest.main()
