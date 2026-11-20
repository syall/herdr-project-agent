"""pa-full-suite's record, reuse, machine cap and part times (t-056). Temp git repos and a temp HERDR_PA_HOME
only: no real space, no real ~/.config/herdr-pa, and the slot folder is under the temp home.

  w1 one record per run: the t-052 line plus run, start, end, load, parts, untracked, waited; --last prints it
  w2 one run per tree: a passing clean tree is not run again (--again runs it); a failing record, uncommitted or
     untracked files, or another command are never reused
  w3 the cap: a run past the cap waits, says for whom, and starts when the slot frees; a dead holder never blocks
  w4 part times: run-all.sh prints each part's seconds; --report lists parts by median, slowest first
  w6 (t-072) pa-full-suite running run-all.sh, which now takes the slot itself, does not wait for itself
  w7 (t-076) the run's output file (its path first), its progress file, --status (waiting, running, done, gone),
     --wait (heartbeat, timeout, the run's exit), a heartbeat while it runs
  w8 (t-076) --module <test_x>: tests/run-all.sh --module as pa-full-suite's command, recorded and reused per module
"""
import contextlib
import fcntl
import importlib.util
import io
import shlex
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(HERE, "..", "runtime", "tools")
TMP = os.path.realpath(tempfile.mkdtemp(prefix="t056-suite-"))
sys.path.insert(0, HERE)
import test_run_all as tra  # noqa: E402 (its fixture, not its tests: only names bound here are collected)

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
_spec = importlib.util.spec_from_file_location("pa_suite", os.path.join(TOOLS, "pa_suite.py"))
pa_suite = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pa_suite)
if _saved_mod is None:
    del sys.modules["pa"]
else:
    sys.modules["pa"] = _saved_mod
for _k, _v in _saved_env.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v

TEST_ENV = dict(_HOMES, HOME=os.path.join(TMP, "home"), GIT_CONFIG_NOSYSTEM="1")
DROP_ENV = ("HC_TERMINAL_ID", "GIT_DIR", "GIT_WORK_TREE")
_module_env = {}


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


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def sh(cwd, *argv):
    r = subprocess.run(list(argv), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    if r.returncode:
        raise AssertionError("{} failed in {}: {}".format(" ".join(argv), cwd, r.stderr))
    return r.stdout.strip()


SUMMARY = """=== summary (each part's whole output: /tmp/x)
--- keeper harness: FAILED (no failing test named; its last 40 lines:)
  boom
  python 3.7 parse: ok (1s)
  unit tests: test_a: ok (12s)
  keeper harness: FAILED (7s)
  panel harness 1/4: ok (30s)
=== 1 part(s) failed: keeper harness (outputs: /tmp/x)
"""


class Base(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(pa.ROOT, ignore_errors=True)
        self.tmp = os.path.realpath(tempfile.mkdtemp(dir=TMP))
        self.dir = os.path.join(pa.ROOT, "spaces", "sp1")
        dump({"space": "sp1", "terminal": "t0", "terminals": ["t0"], "profile": "test", "name": "S"}, os.path.join(self.dir, "agent.json"))
        dump({"name": "test", "claude": ["true"], "codex": ["true"]}, os.path.join(pa.ROOT, "profiles", "test", "profile.json"))
        dump({"version": 1, "tasks": [{"id": "t-1", "title": "T", "state": "in-progress", "tier": "low"}]},
             os.path.join(self.dir, "ledger.json"))
        self.wt = os.path.join(self.tmp, "wt")
        os.makedirs(self.wt)
        sh(self.wt, "git", "init", "-q", "-b", "main")
        sh(self.wt, "git", "config", "user.email", "t@example.com")
        sh(self.wt, "git", "config", "user.name", "T")
        write(os.path.join(self.wt, ".gitignore"), ".claude/\n")
        write(os.path.join(self.wt, "a.txt"), "a\n")
        sh(self.wt, "git", "add", "-A")
        sh(self.wt, "git", "commit", "-q", "-m", "a")
        write(os.path.join(self.wt, ".claude", "pa-task.json"), json.dumps({"task": "t-1", "space": "sp1"}))
        self.count = os.path.join(self.tmp, "count")
        self.cwd = os.getcwd()
        os.chdir(self.wt)

    def tearDown(self):
        os.chdir(self.cwd)

    def set_command(self, command):
        agent = pa.read_json(os.path.join(self.dir, "agent.json"))
        agent["commands"] = {"full": command}
        dump(agent, os.path.join(self.dir, "agent.json"))

    def counting(self, tail="true"):
        """A suite command that counts its runs in self.count, prints a run-all-like summary, then runs tail."""
        return "echo x >> {}; printf '  unit tests: test_a: ok (3s)\\n  keeper harness: ok (7s)\\n'; {}".format(self.count, tail)

    def runs(self):
        return len(pa.read_text(self.count).splitlines()) if os.path.exists(self.count) else 0

    def main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        saved, sys.argv = sys.argv, ["pa-full-suite"] + list(args)
        code = 0
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    pa_hooks.main()
                except SystemExit as e:
                    code = e.code or 0
        finally:
            sys.argv = saved
        return code, out.getvalue() + err.getvalue()

    def lines(self):
        path = os.path.join(self.dir, "content", "suites.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(l) for l in f if l.strip()]


class W0Parse(unittest.TestCase):
    def test_w0_pa_suite_parses_as_python_37(self):  # run-all.sh's parse part lists its files by name (other tests pin it)
        import ast
        with open(os.path.join(TOOLS, "pa_suite.py")) as f:
            ast.parse(f.read(), feature_version=(3, 7))


class W1Record(Base):
    def test_w1a_parts_parse_from_the_summary_list(self):
        self.assertEqual(pa_suite.parse_parts(SUMMARY), [
            {"name": "python 3.7 parse", "ok": True, "seconds": 1},
            {"name": "unit tests: test_a", "ok": True, "seconds": 12},
            {"name": "keeper harness", "ok": False, "seconds": 7},
            {"name": "panel harness 1/4", "ok": True, "seconds": 30}])
        self.assertEqual(pa_suite.parse_parts("no summary here\n  x: ok\n"), [])

    def test_w1c_only_the_lines_after_the_last_parts_marker(self):
        """t-103 R14: run-all.sh prints `=== parts` right before its part list; a failed part's tail above it (with
        part-shaped lines, a marker of a run inside it) is not read. Text with no marker is read whole (old trees)."""
        text = ("--- keeper harness: FAILED (no failing test named; its last 40 lines:)\n"
                "  inner: ok (1s)\n  === parts\n  inner two: FAILED (2s)\n=== parts\n"
                "  unit tests: test_a: ok (3s)\n  keeper harness: FAILED (7s)\n=== 1 part(s) failed: keeper harness\n")
        self.assertEqual(pa_suite.parse_parts(text), [{"name": "unit tests: test_a", "ok": True, "seconds": 3},
                                                      {"name": "keeper harness", "ok": False, "seconds": 7}])
        self.assertEqual([p["name"] for p in pa_suite.parse_parts("=== parts\n  early: ok (1s)\n=== parts\n"
                                                                    "  late: ok (2s)\n")], ["late"])
        self.assertEqual(len(pa_suite.parse_parts(SUMMARY)), 4)

    def test_w1e_a_local_runs_record_has_the_list_after_the_marker(self):
        self.set_command("printf '  fixture: ok (1s)\\n=== parts\\n  unit tests: test_a: ok (3s)\\n'")
        code, out = self.main()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.lines()[-1]["parts"], [{"name": "unit tests: test_a", "ok": True, "seconds": 3}])

    def test_w1b_one_record_with_the_new_fields(self):
        self.set_command(self.counting())
        code, out = self.main()
        self.assertEqual(code, 0, out)
        lines = self.lines()
        self.assertEqual(len(lines), 1, lines)
        l = lines[0]
        head = sh(self.wt, "git", "rev-parse", "HEAD")
        tree = sh(self.wt, "git", "rev-parse", "HEAD^{tree}")
        for k in ("at", "task", "worker", "repo", "commit", "tree", "uncommitted", "exit", "seconds", "command"):
            self.assertIn(k, l)  # t-052 J1's line is kept
        self.assertEqual((l["commit"], l["tree"], l["uncommitted"], l["untracked"], l["exit"]), (head, tree, 0, 0, 0))
        for k in ("run", "start", "end", "waited"):
            self.assertTrue(l.get(k) is not None, k)
        self.assertLessEqual(l["start"], l["end"])
        self.assertEqual(sorted(l["load"]), ["15m", "1m", "5m", "cpus"])
        self.assertEqual(l["parts"], [{"name": "unit tests: test_a", "ok": True, "seconds": 3},
                                      {"name": "keeper harness", "ok": True, "seconds": 7}])
        self.assertIn("suites.jsonl", out)  # it says where the record is

    def test_w1c_last_prints_the_last_run_for_a_tree_or_commit(self):
        self.set_command(self.counting())
        self.main()
        first = self.lines()[-1]
        write(os.path.join(self.wt, "b.txt"), "b\n")
        sh(self.wt, "git", "add", "-A")
        sh(self.wt, "git", "commit", "-q", "-m", "b")
        self.main()
        second = self.lines()[-1]
        code, out = self.main("--last")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out.strip().splitlines()[-1])["run"], second["run"])
        code, out = self.main("--last", "--commit", first["commit"][:10])
        self.assertEqual((code, json.loads(out.strip().splitlines()[-1])["run"]), (0, first["run"]))
        code, out = self.main("--last", "--tree", first["tree"])
        self.assertEqual((code, json.loads(out.strip().splitlines()[-1])["run"]), (0, first["run"]))
        code, out = self.main("--last", "--tree", "0" * 40)
        self.assertEqual(code, 1, out)
        self.assertIn("no run recorded", out)

    def test_w1d_last_with_space_outside_a_worktree(self):
        self.set_command(self.counting())
        self.main()
        os.chdir(self.tmp)
        code, out = self.main("--last", "--space", "sp1", "--commit", self.lines()[-1]["commit"])
        self.assertEqual(code, 0, out)


class W2Reuse(Base):
    def test_w2a_a_passing_clean_tree_is_not_run_again(self):
        self.set_command(self.counting())
        self.assertEqual(self.main()[0], 0)
        code, out = self.main()
        self.assertEqual((code, self.runs()), (0, 1), out)
        self.assertIn("already passed", out)
        self.assertIn(self.lines()[0]["run"], out)
        self.assertEqual(len(self.lines()), 1)  # a reuse is no new run
        self.assertIn("already passed", pa.read_text(os.path.join(self.dir, "evidence", "t-1", "build.log")))
        code, out = self.main("--again")
        self.assertEqual((code, self.runs(), len(self.lines())), (0, 2, 2), out)

    def test_w2b_a_failing_record_is_never_reused(self):
        self.set_command(self.counting("exit 3"))
        self.assertEqual(self.main()[0], 3)
        self.assertEqual(self.main()[0], 3)
        self.assertEqual(self.runs(), 2)

    def test_w2c_uncommitted_or_untracked_files_run_again(self):
        self.set_command(self.counting())
        write(os.path.join(self.wt, "a.txt"), "changed\n")
        self.main()
        self.main()
        self.assertEqual(self.runs(), 2)
        sh(self.wt, "git", "checkout", "-q", "a.txt")
        write(os.path.join(self.wt, "new.txt"), "n\n")
        self.main()
        self.main()
        self.assertEqual(self.runs(), 4)
        os.remove(os.path.join(self.wt, "new.txt"))
        self.main()  # clean now, but no clean passing record yet: runs
        self.assertEqual(self.runs(), 5)
        self.main()
        self.assertEqual(self.runs(), 5)

    def test_w2d_another_command_runs(self):
        self.set_command(self.counting())
        self.main()
        self.set_command(self.counting("true 2"))
        self.main()
        self.assertEqual(self.runs(), 2)

    def test_w2e_a_record_without_untracked_is_not_trusted(self):
        tree = sh(self.wt, "git", "rev-parse", "HEAD^{tree}")
        command = self.counting()
        self.set_command(command)
        write(os.path.join(self.dir, "content", "suites.jsonl"), json.dumps(
            {"repo": os.path.realpath(self.wt), "tree": tree, "commit": "x", "uncommitted": 0, "exit": 0,
             "command": command}) + "\n")
        self.main()
        self.assertEqual(self.runs(), 1)


HOLDER = r'''import fcntl, json, os, sys, time
f = open(sys.argv[1], "a+")
fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
f.seek(0); f.truncate()
f.write(json.dumps({"pid": os.getpid(), "task": "t-holder", "worker": "t9", "start": "now"})); f.flush()
open(sys.argv[2], "w").close()
time.sleep(60)
'''


class W3Cap(Base):
    def holder(self, slot):
        path = os.path.join(pa_suite.slot_dir(), "slot-{}".format(slot))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        ready = os.path.join(self.tmp, "ready-{}".format(slot))
        script = os.path.join(self.tmp, "holder.py")
        write(script, HOLDER)
        p = subprocess.Popen([sys.executable, script, path, ready])
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.wait()))
        end = time.time() + 20
        while not os.path.exists(ready) and time.time() < end:
            time.sleep(0.05)
        self.assertTrue(os.path.exists(ready), "the holder never took its slot")
        return p

    def test_w3a_the_slots_are_user_level_not_a_space(self):
        self.assertEqual(pa_suite.slot_dir(), os.path.join(pa.ROOT, "suite-slots"))
        self.assertEqual(pa_suite.CAP, 1)

    def test_w3a2_the_cap_is_configurable(self):
        saved = os.environ.get("PA_SUITE_CAP")
        try:
            for value, want in (("2", 2), ("0", 1), ("x", 1), ("", 1)):
                os.environ["PA_SUITE_CAP"] = value
                self.assertEqual(pa_suite.cap(), want, value)
        finally:
            if saved is None:
                os.environ.pop("PA_SUITE_CAP", None)
            else:
                os.environ["PA_SUITE_CAP"] = saved

    def test_w3b_a_run_past_the_cap_waits_names_the_holder_and_starts_when_it_frees(self):
        p = self.holder(0)
        said, got = [], {}

        def take():
            got["slot"] = pa_suite.acquire(1, {"task": "t-1"}, poll=0.1, limit=30, say=said.append)
        t = threading.Thread(target=take)
        t.start()
        time.sleep(0.6)
        self.assertNotIn("slot", got)
        self.assertTrue(said and "waiting" in said[0] and "t-holder" in said[0] and str(p.pid) in said[0], said)
        p.kill()
        p.wait()
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertIsNotNone(got.get("slot"))
        pa_suite.release(got["slot"])

    def test_w3c_a_dead_holder_never_blocks(self):
        p = self.holder(0)
        p.kill()
        p.wait()
        said = []
        slot = pa_suite.acquire(1, {"task": "t-1"}, poll=0.1, limit=5, say=said.append)
        self.assertIsNotNone(slot)
        self.assertEqual(said, [])
        pa_suite.release(slot)

    def test_w3d_a_cap_of_two_lets_a_second_run_in(self):
        self.holder(0)
        slot = pa_suite.acquire(2, {"task": "t-1"}, poll=0.1, limit=5, say=lambda m: None)
        self.assertIsNotNone(slot)
        pa_suite.release(slot)

    def test_w3e_waiting_past_the_limit_gives_up(self):
        self.holder(0)
        said = []
        with self.assertRaises(pa_suite.Waited):
            pa_suite.acquire(1, {"task": "t-1"}, poll=0.1, limit=0.5, say=said.append)

    def test_w3f_the_slot_is_held_while_the_suite_runs(self):
        probe = os.path.join(self.tmp, "probe.py")
        write(probe, "import fcntl, sys\nf = open(sys.argv[1], 'a+')\ntry:\n    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                     "except OSError:\n    sys.exit(0)\nsys.exit(5)\n")
        self.set_command("{} {} {}".format(sys.executable, probe, os.path.join(pa_suite.slot_dir(), "slot-0")))
        code, out = self.main()
        self.assertEqual(code, 0, out)
        # and it is freed after
        f = open(os.path.join(pa_suite.slot_dir(), "slot-0"), "a+")
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        f.close()


class W4Parts(Base):
    def test_w4a_report_lists_parts_by_median_slowest_first(self):
        rows = []
        for run, (a, b) in enumerate(((10, 2), (30, 4), (20, 100))):
            parts = [{"name": "a", "ok": True, "seconds": a}, {"name": "b", "ok": True, "seconds": b}]
            for repo in ("r1", "r2"):  # two repo lines of one run count once
                rows.append({"run": "run{}".format(run), "repo": repo, "parts": parts, "exit": 0})
        rows.append({"repo": "r1", "exit": 0})  # an older line without parts
        write(os.path.join(self.dir, "content", "suites.jsonl"), "".join(json.dumps(r) + "\n" for r in rows))
        self.assertEqual(pa_suite.report(rows), [
            {"name": "a", "last": 20, "median": 20, "runs": 3},
            {"name": "b", "last": 100, "median": 4, "runs": 3}])
        code, out = self.main("--report")
        self.assertEqual(code, 0, out)
        rows = [l for l in out.strip().splitlines() if l.endswith((" a", " b"))]
        self.assertEqual([l[-1] for l in rows], ["a", "b"], out)  # one line per part, slowest median first
        self.assertEqual(rows[0].split()[:3], ["20s", "20s", "3"], out)  # median, last, runs, then the name


class W4RunAllTimes(unittest.TestCase):
    """run-all.sh's own summary list carries each part's seconds (its fixture from test_run_all.py)."""
    setUp = tra.RunAll.setUp
    tearDown = tra.RunAll.tearDown
    run_all = tra.RunAll.run_all
    env_for = tra.RunAll.env_for
    summary = tra.RunAll.summary

    def test_w4b_each_part_has_its_seconds(self):
        code, out = self.run_all(keeper="sleep:2", store="quiet")
        self.assertEqual(code, 1, out)
        parts = pa_suite.parse_parts(out)
        self.assertEqual(sorted(p["name"] for p in parts), sorted(tra.NAMES), out)
        by = {p["name"]: p for p in parts}
        self.assertTrue(2 <= by["keeper harness"]["seconds"] <= 5, by)
        self.assertEqual(by["store harness"]["ok"], False)
        self.assertTrue(all(p["ok"] for n, p in by.items() if n != "store harness"), by)


class W5Pins(Base):
    """MF-1: the record names the tree that ran. A commit made while the run waits for a slot is what runs and
    what is recorded; a change during the run itself leaves a record that is never reused."""
    holder = W3Cap.holder

    def commit(self, message):
        sh(self.wt, "git", "add", "-A")
        sh(self.wt, "git", "commit", "-q", "-m", message)
        return sh(self.wt, "git", "rev-parse", "HEAD"), sh(self.wt, "git", "rev-parse", "HEAD^{tree}")

    def test_w5a_a_commit_made_while_waiting_is_what_is_recorded(self):
        write(os.path.join(self.wt, "bad.txt"), "x\n")
        commit_x, tree_x = self.commit("x")
        self.set_command(self.counting("test ! -f bad.txt"))  # fails on tree X, passes on tree Y
        p = self.holder(0)
        got = {}
        t = threading.Thread(target=lambda: got.update(r=self.main()))
        t.start()
        time.sleep(1)
        self.assertTrue(t.is_alive(), got)  # it waits for slot 0
        os.remove(os.path.join(self.wt, "bad.txt"))
        commit_y, tree_y = self.commit("y")
        p.kill()
        p.wait()
        t.join(30)
        self.assertFalse(t.is_alive())
        self.assertEqual(got["r"][0], 0, got["r"][1])
        line = self.lines()[-1]
        self.assertEqual((line["tree"], line["commit"]), (tree_y, commit_y), "the record names the tree it tested")
        sh(self.wt, "git", "revert", "--no-edit", "HEAD")  # tree X again, clean
        self.assertEqual(sh(self.wt, "git", "rev-parse", "HEAD^{tree}"), tree_x)
        code, out = self.main()
        self.assertNotIn("already passed", out)
        self.assertEqual((code != 0, self.runs()), (True, 2), out)

    def test_w5b_a_change_during_the_run_is_never_reused(self):
        write(os.path.join(self.wt, "moveme.txt"), "m\n")
        commit_x, tree_x = self.commit("x")
        self.set_command(self.counting("if [ -f moveme.txt ]; then git rm -q moveme.txt && git commit -q -m m; fi"))
        code, out = self.main()
        self.assertEqual(code, 0, out)
        self.assertIn("changed during the run", out)
        line = self.lines()[-1]
        self.assertEqual((line["commit"], line["tree"], line.get("moved")), (commit_x, tree_x, True))
        sh(self.wt, "git", "revert", "--no-edit", "HEAD")  # tree X again, clean
        self.assertEqual(sh(self.wt, "git", "rev-parse", "HEAD^{tree}"), tree_x)
        code, out = self.main()
        self.assertNotIn("already passed", out)
        self.assertEqual(self.runs(), 2, out)


if __name__ == "__main__":
    unittest.main()


class W6RunAllUnderTheSlot(Base):
    def test_w6a_pa_full_suite_running_run_all_does_not_wait_for_itself(self):
        fx = types.SimpleNamespace()
        tra.RunAll.setUp(fx)
        self.addCleanup(tra.RunAll.tearDown, fx)
        env = {"NODE": fx.node, "STUB_DIR": fx.tmp, "TMPDIR": fx.tmp, "PA_SUITE_WAIT_LIMIT": "5"}
        saved = {k: os.environ.get(k) for k in list(env) + ["PA_SUITE_SLOT"]}
        os.environ.update(env)
        try:
            self.set_command("sh {} --affected".format(shlex.quote(os.path.join(fx.repo, "tests", "run-all.sh"))))
            code, out = self.main()
            left = os.environ.get("PA_SUITE_SLOT")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertEqual(code, 0, out)
        self.assertNotIn("waiting for the suite slot", out)
        self.assertIn("  suite slot: held by the caller", out)
        self.assertEqual(left, saved["PA_SUITE_SLOT"], "pa-full-suite left its slot marker in its environment")
        self.assertEqual(self.lines()[-1]["exit"], 0)


PA_FULL_SUITE = os.path.join(TMP, "bin", "pa-full-suite")


class W7Status(Base):
    """t-076: one way to run it (the output file named first), to see it (--status) and to wait for it (--wait)."""
    holder = W3Cap.holder

    def setUp(self):
        super().setUp()
        if not os.path.lexists(PA_FULL_SUITE):
            os.makedirs(os.path.dirname(PA_FULL_SUITE), exist_ok=True)
            os.symlink(os.path.join(os.path.realpath(TOOLS), "pa_hooks.py"), PA_FULL_SUITE)
        self.env = {}

    def setenv(self, **values):
        for k, v in values.items():
            self.env.setdefault(k, os.environ.get(k))
            os.environ[k] = v
        self.addCleanup(self.restore)

    def restore(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.env = {}

    def background(self, *args):
        """pa-full-suite in its own process (the run a worker started), its output in a file."""
        log = os.path.join(self.tmp, "bg.out")
        with open(log, "w") as f:
            p = subprocess.Popen([sys.executable, PA_FULL_SUITE] + list(args), cwd=self.wt, stdin=subprocess.DEVNULL,
                                 stdout=f, stderr=subprocess.STDOUT)
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.wait()))
        return p, log

    def until(self, test, limit=30):
        end = time.time() + limit
        while time.time() < end:
            got = test()
            if got:
                return got
            time.sleep(0.1)
        return None

    def status(self, *args):
        return self.main("--status", *args)

    def gated(self, tail="exit 0"):
        """A suite command that waits for the file go, then runs tail."""
        return "echo started; while [ ! -f {} ]; do sleep 0.1; done; {}".format(os.path.join(self.tmp, "go"), tail)

    def go(self):
        write(os.path.join(self.tmp, "go"), "")

    def test_w7a_the_output_file_is_named_first_and_holds_the_whole_output(self):
        self.set_command(self.counting("i=0; while [ $i -lt 300 ]; do echo line $i; i=$((i + 1)); done"))
        code, out = self.main()
        self.assertEqual(code, 0, out)
        first = out.splitlines()[0]
        self.assertTrue(first.startswith("pa-full-suite: run "), out)
        path = first.split("output in ", 1)[1].split(" ", 1)[0]
        self.assertEqual(os.path.dirname(path), os.path.join(self.dir, "evidence", "t-1"))
        self.assertTrue(os.path.basename(path).startswith("full-suite-"), path)
        text = pa.read_text(path)
        self.assertIn("line 0\n", text)
        self.assertIn("line 299\n", text)
        self.assertIn("  keeper harness: ok (7s)", text)
        line = self.lines()[-1]
        self.assertEqual(line["output"], path)
        self.assertIn(line["run"], path)
        self.assertEqual(line["parts"][1], {"name": "keeper harness", "ok": True, "seconds": 7})

    def test_w7b_status_and_wait_follow_a_run_to_its_end(self):
        self.set_command(self.gated("sleep 1.5; exit 3"))
        p, log = self.background()
        got = self.until(lambda: "running since" in self.status()[1] and self.status())
        self.assertTrue(got, (self.status(), pa.read_text(log)))
        self.assertEqual(got[0], 0, got[1])
        self.setenv(PA_SUITE_HEARTBEAT="0.3")
        code, out = self.main("--wait", "--timeout", "1")
        self.assertEqual(code, 75, out)
        self.assertIn("still running", out)
        self.assertIn("pa-full-suite --wait again", out)
        self.go()
        began = time.time()
        code, out = self.main("--wait")
        self.assertEqual(code, 3, out)
        self.assertGreater(time.time() - began, 1.0)
        self.assertIn("done: exit 3", out.strip().splitlines()[-1])
        self.assertGreaterEqual(out.count("running since"), 1, out)  # the heartbeat
        self.assertEqual(p.wait(timeout=30), 3, pa.read_text(log))
        code, out = self.status()
        self.assertEqual(code, 0, out)
        self.assertIn("done: exit 3", out)
        self.assertIn("suites.jsonl", out)
        head = sh(self.wt, "git", "rev-parse", "HEAD")
        code, out = self.status("--commit", head[:8])
        self.assertIn("done: exit 3", out)
        code, out = self.main("--wait")  # a run that is done already: its result at once
        self.assertEqual(code, 3, out)

    def test_w7c_a_waiting_run_says_for_whom_and_its_place(self):
        holder = self.holder(0)
        self.set_command(self.counting())
        p, log = self.background()
        got = self.until(lambda: "waiting for the suite slot" in self.status()[1] and self.status()[1])
        self.assertTrue(got, (self.status(), pa.read_text(log)))
        self.assertIn("t-holder", got)
        self.assertIn(str(holder.pid), got)
        self.assertIn("place 1 of 1", got)
        holder.kill()
        holder.wait()
        code, out = self.main("--wait")
        self.assertEqual(code, 0, out)
        self.assertEqual(p.wait(timeout=30), 0)

    def test_w7d_a_run_whose_owner_is_gone_ended_without_a_result(self):
        dead = subprocess.Popen(["true"])
        dead.wait()
        folder = os.path.join(pa.ROOT, "suite-runs")
        dump({"run": "r1", "kind": "pa-full-suite", "dir": self.wt, "pid": dead.pid, "state": "running",
              "queued": "2026-01-01T00:00:00Z", "start": "2026-01-01T00:00:01Z"}, os.path.join(folder, "r1.json"))
        code, out = self.status()
        self.assertIn("ended without a result", out)
        code, out = self.main("--wait")
        self.assertEqual(code, 1, out)
        self.assertIn("ended without a result", out)

    def test_w7e_no_run_to_show(self):
        code, out = self.status()
        self.assertEqual(code, 1, out)
        self.assertIn("no run", out)
        code, out = self.main("--wait")
        self.assertEqual(code, 1, out)

    def test_w7f_status_shows_a_run_all_run_of_this_worktree(self):
        folder = os.path.join(pa.ROOT, "suite-runs")
        dump({"run": "r2", "kind": "run-all.sh", "what": "tests/run-all.sh --affected", "dir": self.wt, "pid": os.getpid(),
              "state": "running", "queued": "2026-01-01T00:00:00Z", "start": "2026-01-01T00:00:01Z",
              "parts": {"total": 8, "done": 5, "running": [{"name": "keeper harness", "since": time.time() - 30,
                                                             "last_output": time.time() - 2, "limit": 900}]}},
             os.path.join(folder, "r2.json"))
        code, out = self.status()
        self.assertEqual(code, 0, out)
        self.assertIn("parts 5 of 8 done", out)
        self.assertIn("now: keeper harness", out)
        self.assertIn("limit 900s", out)
        self.assertIn("last output", out)
        os.chdir(self.tmp)  # outside a worktree: every run not done
        code, out = self.status()
        self.assertEqual(code, 0, out)
        self.assertIn("now: keeper harness", out)

    def test_w7g_a_heartbeat_while_it_runs_and_the_progress_file(self):
        self.setenv(PA_SUITE_HEARTBEAT="0.3")
        self.set_command(self.counting("sleep 1.5"))
        code, out = self.main()
        self.assertEqual(code, 0, out)
        self.assertGreaterEqual(out.count("pa-full-suite: running since"), 1, out)
        rows = [json.loads(pa.read_text(os.path.join(pa.ROOT, "suite-runs", n)))
                for n in os.listdir(os.path.join(pa.ROOT, "suite-runs"))]
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual((row["kind"], row["state"], row["exit"], row["task"], row["dir"]),
                         ("pa-full-suite", "done", 0, "t-1", os.path.realpath(self.wt)))
        self.assertEqual(row["run"], self.lines()[-1]["run"])

    def test_w7h_pa_full_suite_and_its_run_all_share_one_progress_file(self):
        fx = types.SimpleNamespace()
        tra.RunAll.setUp(fx)
        self.addCleanup(tra.RunAll.tearDown, fx)
        self.setenv(NODE=fx.node, STUB_DIR=fx.tmp, TMPDIR=fx.tmp, PA_SUITE_WAIT_LIMIT="5")
        self.set_command("sh {} --affected".format(shlex.quote(os.path.join(fx.repo, "tests", "run-all.sh"))))
        code, out = self.main()
        self.assertEqual(code, 0, out)
        rows = [json.loads(pa.read_text(os.path.join(pa.ROOT, "suite-runs", n)))
                for n in os.listdir(os.path.join(pa.ROOT, "suite-runs"))]
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual((rows[0]["kind"], rows[0]["state"], rows[0]["parts"]["total"], rows[0]["parts"]["done"]),
                         ("pa-full-suite", "done", 8, 8))


class W8Module(Base):
    def test_w8a_module_needs_run_all(self):
        with self.assertRaises(pa.Problem):
            self.main("--module", "test_x")

    def test_w8b_module_runs_run_all_module_recorded_and_reused(self):
        write(os.path.join(self.wt, "tests", "run-all.sh"),
              'echo "args $*" >> {}\nprintf "  unit tests: $2: ok (4s)\\n"\n'.format(self.count))
        write(os.path.join(self.wt, "tests", "test_x.py"), "")
        sh(self.wt, "git", "add", "-A")
        sh(self.wt, "git", "commit", "-q", "-m", "t")
        code, out = self.main("--module", "test_x")
        self.assertEqual(code, 0, out)
        self.assertEqual(pa.read_text(self.count), "args --module test_x\n")
        line = self.lines()[-1]
        self.assertEqual((line["module"], line["parts"]), ("test_x", [{"name": "unit tests: test_x", "ok": True, "seconds": 4}]))
        self.assertIn("--module test_x", line["command"])
        code, out = self.main("--module", "test_x")
        self.assertEqual((code, self.runs()), (0, 1), out)
        self.assertIn("already passed", out)
        with self.assertRaises(pa.Problem):
            self.main("--module", "../x")
