"""t-107 J2: run-all.sh's own runs on a machine (R12-R15), the node shim, and the timing records and report (d-249,
T1-T2). The slot step (python3 pa_suite.py slot ... -- sh <tree>/tests/run-all.sh --affected) runs here as
run-all.sh starts it: a subprocess, no pa.py. Nothing leaves this Mac: hc is tests/stub-hc (box-1 is an invented
machine), and the temp HERDR_PA_HOME, space and worktree come from test_pa_run's fixture.

  s1  R12: --affected and --module go to the machine: first line, output, exit, the tree there, PA_CHANGED_FILES
  s2  R12: when the step stays here (no machines, a caller's slot, PA_SUITE_REMOTE, PA_SUITE_LOCAL_RUN, a plain run)
  s3  R11: unreachable or a setup failure: this Mac with "mac", 125 with "none"
  s4  R8: a secret path or a bad PA_CHANGED_FILES path: 125, nothing shipped
  s5  R14: PA_SUITE_DEADLINE ends the wait (deadline - median, else - 300 s) and the run (killed there, 124, `did not
      finish`: the run had started)
  s6  R13: the slot step's hc calls get PA_SUITE_HC_*; the command gets neither HC_* nor PA_SUITE_HC_*
  s7  R19, T1: one test-runs.jsonl line per slot-step run (remote and local); pa-full-suite writes a full line
  s8  R14, R15: hook_affected's deadline and its could-not-run message; a run that started and did not end (the
      hook's own limit, the deadline during the run) is a failure to fix, never "could not run" (MF-J2-1); t-079 I2:
      the hook's limit is a whole slot wait plus a whole run (s8a, s8i)
  s9  TERM during a remote slot-step run: 143, cleaned
  n1  the machine's node a script that finds its real binary beside $0: pa-run -- node runs it
  k1  T1: the kind inferred when --for is not given; --for wins
  t1  T1: a local pa-run records the command's own time and its setup apart
  t2  T2: pa-run --report: counts, medians, a verdict per kind
t-117 (spec R-numbers): s3a also R15 (the fallback's note and overhead), s5c also R8b (ran and waited), s8c R7 (no
space rule named);
  q3  R3: the slot step's refusals before a Run (PA_CHANGED_FILES, the profile) write one test-runs.jsonl line
  q4  R4: TERM during tree_to_run is 143 and recorded; any other exception in slot_run is 125 with its line
  q7  R7: could not run only for 124/125 with the slot step's own last line; no space rule in runtime/
  q8  R8: no remote run when the deadline leaves no time for it; PA_SUITE_RUN_LIMIT ends a remote run
  q13 R13: the slot step with no usable machine and "none": 125
  q14 R14: pa-full-suite's remote ran and copyback from the run folder's stamps
  q18 R18 (X15): this Mac's slot wait ends before the deadline
  q20 R20 (M11): a remote run past REMOTE_TIMEOUT: 124, `timed out after 2 s there`
  q21 R21: a start snapshot with no change runs nothing; an edit runs the command with the deadline
"""
import json
import os
import signal
import subprocess
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_pa_run as tpr  # noqa: E402 (its fixture: only names bound here are collected)
from test_pa_suite import pa, pa_hooks, pa_suite, dump, write, sh  # noqa: E402

REPO = tpr.REPO
SUITE = os.path.join(REPO, "runtime", "tools", "pa_suite.py")
HC_NAMES = ("HC_TERMINAL_ID", "HC_SOCKET", "HC_TERMINAL_SOCKET", "HC_SCOPE")

# The worktree's tests/run-all.sh in these cases: --affected and --module print what they saw (where, the tree, the
# changed files, the hc variables) and a run-all-like summary as the last lines.
FAKE = r"""#!/bin/sh
case "${1:-}" in
  --affected|--module)
    echo "fake $* in $(pwd) remote=${PA_SUITE_REMOTE:-unset}"
    echo "pcf [$(printf '%s' "${PA_CHANGED_FILES:-}" | tr '\n' ' ')]"
    echo "tree $(git write-tree 2>&1)"
    echo "hcvars [$(env | grep -E '^(HC_|PA_SUITE_HC_)' | sort | tr '\n' ' ')]"
    echo "runlimit [${PA_SUITE_RUN_LIMIT-unset}]"
    if [ -n "${FAKE_GO:-}" ]; then while [ ! -f "$FAKE_GO" ]; do sleep 0.1; done; fi
    echo "=== summary (each part's whole output: /tmp/x)"
    echo "  unit tests: test_a: ok (1s)"
    exit "${FAKE_EXIT:-0}" ;;
  *) echo "fake whole run $* remote=${PA_SUITE_REMOTE:-unset}"; exit "${FAKE_EXIT:-0}" ;;
esac
"""


def setUpModule():
    tpr.setUpModule()


tearDownModule = tpr.tearDownModule


class Slot(tpr.Run):
    def setUp(self):
        super().setUp()
        write(os.path.join(self.wt, "tests", "run-all.sh"), FAKE)
        os.chmod(os.path.join(self.wt, "tests", "run-all.sh"), 0o755)
        write(os.path.join(self.wt, "tests", "test_x.py"), "x = 1\n")
        sh(self.wt, "git", "add", "-A")
        sh(self.wt, "git", "commit", "-q", "-m", "fake run-all with --affected")
        self.envlog = os.path.join(self.tmp, "hc-env.log")
        self.setenv(STUB_HC_ENVLOG=self.envlog, PA_SUITE_DEADLINE=None, PA_SUITE_LOCAL_RUN=None, PA_CHANGED_FILES=None,
                    FAKE_EXIT=None, **{k: None for k in HC_NAMES}, **{"PA_SUITE_HC_" + k[3:]: None for k in HC_NAMES})

    def slot_argv(self, *flags):
        return [sys.executable, SUITE, "slot", "--for", " ".join(("tests/run-all.sh",) + flags), "--worker", "w9",
                "--dir", self.wt, "--", "sh", os.path.join(self.wt, "tests", "run-all.sh")] + list(flags)

    def slot(self, *flags, **env):
        """The slot step as run-all.sh starts it: (exit, stdout, stderr, seconds)."""
        began = time.time()
        r = subprocess.run(self.slot_argv(*flags), cwd=self.wt, env=self.env(**env), stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=180)
        return r.returncode, r.stdout, r.stderr, time.time() - began

    def env(self, **extra):
        env = dict(os.environ)
        for k, v in extra.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
        return env

    def set_fallback(self, value):
        cfg = pa.read_json(os.path.join(pa.ROOT, "config.json"), {}) or {}
        cfg["suiteFallback"] = value
        dump(cfg, os.path.join(pa.ROOT, "config.json"))

    def terminals(self, machine="box-1"):
        folder = os.path.join(self.stub, machine, "terminals")
        return [n for n in os.listdir(folder) if n.endswith(".json")] if os.path.isdir(folder) else []

    def remote_folders(self, machine="box-1"):
        folder = os.path.join(self.stub, machine, "tmp")
        return [n for n in os.listdir(folder) if n.startswith("pa-suite-")] if os.path.isdir(folder) else []

    def rows(self, **match):
        return [r for r in self.run_rows() if all(r.get(k) == v for k, v in match.items())]


class S1Remote(Slot):
    def test_s1a_affected_goes_to_the_machine(self):
        self.dirty()
        want = self.expected_tree()
        code, out, err, _ = self.slot("--affected", PA_CHANGED_FILES="a.txt\nn.txt")
        self.assertEqual(code, 0, out + err)
        lines = out.splitlines()
        self.assertRegex(lines[0], r"^run-all\.sh: on box-1 \(\S+\)$", out)
        self.assertIn("fake --affected in ", out)
        self.assertIn("remote=1", out)
        self.assertIn("pcf [a.txt n.txt]", out)
        self.assertIn("tree " + want, out)
        self.assertEqual(lines[-1], "  unit tests: test_a: ok (1s)", out)  # the summary stays last
        self.assertEqual(len(self.remote_runs()), 1)
        self.assertEqual(self.terminals(), [])
        self.assertEqual(self.remote_folders(), [])

    def test_s1b_the_exit_is_the_runs(self):
        code, out, err, _ = self.slot("--affected", FAKE_EXIT="1")
        self.assertEqual(code, 1, out + err)
        self.assertIn("remote=1", out)

    def test_s1c_module_goes_too(self):
        code, out, err, _ = self.slot("--module", "test_x")
        self.assertEqual(code, 0, out + err)
        self.assertIn("fake --module test_x in ", out)
        self.assertIn("remote=1", out)


class S2StaysHere(Slot):
    def assert_here(self, code, out, err, remote="unset"):
        self.assertEqual(code, 0, out + err)
        self.assertIn("remote=" + remote, out)
        self.assertNotIn("run-all.sh: on box-1", out)
        self.assertEqual(self.remote_runs(), [])

    def test_s2a_no_machines(self):
        self.configure([])
        self.assert_here(*self.slot("--affected")[:3])

    def test_s2b_on_a_machine_already(self):
        self.assert_here(*self.slot("--affected", PA_SUITE_REMOTE="1")[:3], remote="1")

    def test_s2c_placed_here_by_pa_run(self):
        self.assert_here(*self.slot("--affected", PA_SUITE_LOCAL_RUN="0123456789ab")[:3])
        self.assertEqual(self.rows(kind="affected"), [], "pa-run records its own run")

    def test_s2d_under_a_callers_slot(self):
        # an outer slot step (its command not run-all.sh's --affected: it stays here and holds this Mac's slot)
        inner = self.slot_argv("--affected")
        argv = [sys.executable, SUITE, "slot", "--for", "outer", "--dir", self.wt, "--"] + inner
        r = subprocess.run(argv, cwd=self.wt, env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=120)
        self.assert_here(r.returncode, r.stdout, r.stderr)
        self.assertEqual(self.rows(kind="affected"), [], "a run under a caller's slot is the caller's")

    def test_s2e_a_plain_run_stays_here_with_mac(self):
        code, out, err, _ = self.slot()
        self.assertEqual(code, 0, out + err)
        self.assertIn("fake whole run  remote=unset", out)
        self.assertEqual(self.remote_runs(), [])

    def test_s2f_a_plain_run_with_none_is_125(self):
        self.set_fallback("none")
        for flags in ((), ("--except-integration",)):
            code, out, err, _ = self.slot(*flags)
            self.assertEqual(code, 125, out + err)
            self.assertIn("use pa-full-suite", out + err)
            self.assertNotIn("fake whole run", out)


class S3Fallback(Slot):
    def test_s3a_unreachable_with_mac_runs_here_and_says_why(self):
        self.setenv(STUB_HC_FAIL="down:box-1")
        code, out, err, _ = self.slot("--affected")
        self.assertEqual(code, 0, out + err)
        self.assertIn("remote=unset", out)
        self.assertRegex(out + err, r"run-all\.sh: on this Mac: box-1 not reachable")
        # t-117 R15 (Nit-J2-5): the local line says why it ran here and how long the step spent before falling back
        rows = self.rows(kind="affected")
        self.assertEqual(len(rows), 1, self.run_rows())
        self.assertEqual(rows[0]["place"], "local")
        self.assertRegex(rows[0].get("note") or "", r"^on this Mac: box-1 not reachable", rows)
        self.assertIsInstance((rows[0].get("overhead") or {}).get("fallback"), (int, float), rows)
        self.assertIsInstance(rows[0]["ran"], (int, float))

    def test_s3b_unreachable_with_none_is_125_never_here(self):
        self.set_fallback("none")
        self.setenv(STUB_HC_FAIL="down:box-1")
        code, out, err, _ = self.slot("--affected")
        self.assertEqual(code, 125, out + err)
        self.assertNotIn("fake --affected", out)
        self.assertRegex(out.splitlines()[-1], r"^run-all\.sh: not run on box-1: not reachable")

    def test_s3c_a_setup_failure_with_none_is_125(self):
        self.set_fallback("none")
        self.setenv(STUB_HC_FAIL="write")
        code, out, err, _ = self.slot("--affected")
        self.assertEqual(code, 125, out + err)
        self.assertRegex(out.splitlines()[-1], r"^run-all\.sh: not run on box-1: setup failed")
        self.assertEqual(self.remote_folders(), [])


class S4NeverSent(Slot):
    def test_s4a_a_secret_file_refuses(self):
        write(os.path.join(self.wt, ".env"), "TOKEN=x\n")
        code, out, err, _ = self.slot("--affected")
        self.assertEqual(code, 125, out + err)
        self.assertIn(".env", out.splitlines()[-1])
        self.assertNotIn("fake --affected", out)
        self.assertFalse([c for c in self.calls() if c[:2] == ["file", "write"]])

    def test_s4b_a_bad_changed_file_path_refuses(self):
        for bad in ("../x.txt", "/etc/hosts", "sub/.env"):
            code, out, err, _ = self.slot("--affected", PA_CHANGED_FILES="a.txt\n" + bad)
            self.assertEqual(code, 125, bad + out + err)
            self.assertIn(bad, out.splitlines()[-1])
        self.assertEqual(self.remote_runs(), [])


class S5Deadline(Slot):
    def test_s5a_the_wait_ends_300_s_before_the_deadline_without_a_median(self):
        self.hold(self.machine_slot("box-1"))
        code, out, err, took = self.slot("--affected", PA_SUITE_DEADLINE=str(int(time.time()) + 304))
        self.assertEqual(code, 124, out + err)
        self.assertGreaterEqual(took, 2, out + err)
        self.assertLess(took, 30, out + err)
        self.assertRegex(out.splitlines()[-1], r"^run-all\.sh: not run on box-1: no slot came free")
        self.assertEqual(self.remote_runs(), [])

    def test_s5b_the_wait_ends_the_kinds_median_before_the_deadline(self):
        rows = [{"at": pa.now(), "run": "r{}".format(i), "kind": "affected", "place": "box-1", "machine": "box-1",
                 "seconds": 100, "waited": 0, "exit": 0} for i in range(3)]
        write(os.path.join(pa.ROOT, "test-runs.jsonl"), "".join(json.dumps(r) + "\n" for r in rows))
        self.hold(self.machine_slot("box-1"))
        code, out, err, took = self.slot("--affected", PA_SUITE_DEADLINE=str(int(time.time()) + 104))
        self.assertEqual(code, 124, out + err)
        self.assertGreaterEqual(took, 2, out + err)  # deadline - 300 s is past already: the median was used
        self.assertLess(took, 30, out + err)

    def test_s5c_a_run_past_the_deadline_is_ended_there(self):
        # t-117 R8a: with a 1 s median there the run is started (deadline - median is still ahead)
        write(os.path.join(pa.ROOT, "test-runs.jsonl"), "".join(json.dumps(
            {"at": pa.now(), "run": "m{}".format(i), "kind": "affected", "place": "box-1", "machine": "box-1",
             "seconds": 1, "waited": 0, "exit": 0}) + "\n" for i in range(3)))
        go = os.path.join(self.tmp, "never")
        code, out, err, took = self.slot("--affected", FAKE_GO=go, PA_SUITE_DEADLINE=str(int(time.time()) + 8))
        self.assertEqual(code, 124, out + err)
        self.assertLess(took, 40, out + err)
        self.assertIn("deadline", out.splitlines()[-1])
        self.assertRegex(out.splitlines()[-1], r"^run-all\.sh: did not finish on box-1: ")  # MF-J2-1: it started
        # t-117 R8b (AC8.2): how long it ran there and how long it waited
        self.assertRegex(out.splitlines()[-1], r"^run-all\.sh: did not finish on box-1: the deadline \(PA_SUITE_DEADLINE\) "
                                               r"came after it ran \d+ s there \(waited \d+ s for the slot\); the run "
                                               r"there was ended$")
        self.assertTrue(pa_hooks.did_not_finish(out), out)
        self.assertEqual(self.terminals(), [])
        self.assertEqual(self.remote_folders(), [])


class S6HcVariables(Slot):
    def test_s6a_hc_calls_get_them_the_command_does_not(self):
        given = {"PA_SUITE_HC_TERMINAL_ID": "w9", "PA_SUITE_HC_SOCKET": "/x/sock", "PA_SUITE_HC_TERMINAL_SOCKET": "/x/t",
                 "PA_SUITE_HC_SCOPE": "s1"}
        code, out, err, _ = self.slot("--affected", **given)
        self.assertEqual(code, 0, out + err)
        calls = [json.loads(l) for l in pa.read_text(self.envlog).splitlines() if l.strip()]
        self.assertTrue(calls)
        for c in calls:
            self.assertEqual(c["env"], {"HC_TERMINAL_ID": "w9", "HC_SOCKET": "/x/sock", "HC_TERMINAL_SOCKET": "/x/t",
                                        "HC_SCOPE": "s1"}, c)
        self.assertIn("hcvars []", out)

    def test_s6b_a_local_command_gets_none_either(self):
        self.configure([])
        code, out, err, _ = self.slot("--affected", PA_SUITE_HC_SOCKET="/x/sock", PA_SUITE_HC_TERMINAL_ID="w9")
        self.assertEqual(code, 0, out + err)
        self.assertIn("remote=unset", out)
        self.assertIn("hcvars []", out)


class S7Records(Slot):
    def test_s7a_a_remote_slot_step_run_is_one_line(self):
        code, out, err, _ = self.slot("--affected")
        self.assertEqual(code, 0, out + err)
        rows = self.rows(kind="affected")
        self.assertEqual(len(rows), 1, self.run_rows())
        row = rows[0]
        self.assertEqual((row["place"], row["machine"], row["task"], row["worker"], row["exit"]),
                         ("box-1", "box-1", "t-1", "w9", 0))
        for key in ("probe", "ship", "start", "copyback", "cleanup"):
            self.assertIn(key, row["overhead"])
        self.assertIsInstance(row["ran"], (int, float))
        self.assertIsInstance(row["seconds"], (int, float))
        self.assertEqual(self.lines(), [])  # never a suite record

    def test_s7b_a_local_slot_step_run_is_one_line(self):
        self.configure([])
        code, out, err, _ = self.slot("--module", "test_x")
        self.assertEqual(code, 0, out + err)
        rows = self.rows(kind="module")
        self.assertEqual(len(rows), 1, self.run_rows())
        self.assertEqual((rows[0]["place"], rows[0]["machine"]), ("local", None))
        self.assertIsInstance(rows[0]["ran"], (int, float))
        self.assertIsInstance(rows[0]["waited"], (int, float))

    def test_s7c_pa_full_suite_writes_one_full_line_and_its_run_all_none(self):
        self.configure([])
        self.set_command("sh tests/run-all.sh")
        code, out = self.main("--local")
        self.assertEqual(code, 0, out)
        rows = self.run_rows()
        self.assertEqual([(r["kind"], r["place"]) for r in rows], [("full", "local")], rows)
        self.assertIsInstance(rows[0]["ran"], (int, float))

    def test_s7d_a_remote_full_suite_line_has_its_overhead(self):
        self.set_command("sh tests/run-all.sh")
        os.remove(os.path.join(self.wt, "tests", "run-all.sh"))
        write(os.path.join(self.wt, "tests", "run-all.sh"), tpr.tpr.FAKE_RUN_ALL)
        sh(self.wt, "git", "add", "-A")
        sh(self.wt, "git", "commit", "-q", "-m", "fake run-all for the full suite")
        code, out = self.main()
        self.assertEqual(code, 0, out)
        rows = [r for r in self.run_rows() if r["kind"] == "full"]
        self.assertEqual(len(rows), 1, self.run_rows())
        self.assertEqual(rows[0]["place"], "box-1")
        for key in ("probe", "ship", "start", "cleanup"):
            self.assertIn(key, rows[0]["overhead"])


class S8Hook(Slot):
    def hook(self, script, *args):
        agent = pa.read_json(os.path.join(self.dir, "agent.json"))
        agent["commands"] = {"affected": "sh " + script}
        dump(agent, os.path.join(self.dir, "agent.json"))
        write(os.path.join(self.wt, "a.txt"), "a changed\n")
        err = __import__("io").StringIO()
        code = None
        with __import__("contextlib").redirect_stderr(err):
            try:
                code = pa_hooks.hook_affected({"cwd": self.wt, "session_id": "s-{}".format(time.time())}, list(args))
            except SystemExit as e:
                code = ("exit", e.code)
        return code, err.getvalue()

    def script(self, body):
        path = os.path.join(self.tmp, "affected.sh")
        write(path, body)
        return path

    def test_s8a_the_command_gets_a_deadline_1710_s_on_and_a_run_limit_of_870_s(self):
        # t-079 I2: the hook's limit is the slot wait's (840 s) plus the run's own 900 s; the deadline is 30 s before
        # it, and the run's own limit (870 s, counted from when the slot is taken) comes in PA_SUITE_RUN_LIMIT
        seen = os.path.join(self.tmp, "deadline")
        began = time.time()
        code, _ = self.hook(self.script('echo "$PA_SUITE_DEADLINE $PA_SUITE_RUN_LIMIT" > {}\nexit 0\n'.format(seen)))
        self.assertEqual(code, 0)
        at, run = pa.read_text(seen).split()
        self.assertAlmostEqual(int(at), began + 1710, delta=5)
        self.assertEqual(run, "870")

    def test_s8i_the_hooks_limit_holds_a_whole_wait_and_a_whole_run(self):
        # t-079 I2 (t-072 RL-5): a run that waited the slot step's whole wait still has its own budget, and the
        # installed hook timeouts are over the hook's own limit
        self.assertEqual(pa_hooks.AFFECTED_WAIT, pa_suite.SLOT_WAIT_LIMIT)
        self.assertEqual(pa_hooks.AFFECTED_LIMIT, pa_hooks.AFFECTED_WAIT + pa_hooks.AFFECTED_RUN)
        self.assertGreaterEqual(pa_hooks.AFFECTED_RUN, 900)
        settings = pa_hooks.hook_settings()
        for event in ("TaskCompleted", "Stop"):
            with self.subTest(event=event):
                self.assertEqual(settings[event][0]["hooks"][0]["timeout"], pa_hooks.AFFECTED_LIMIT + 30)

    def test_s8b_a_failure_keeps_todays_message(self):
        code, err = self.hook(self.script("echo FAIL x\nexit 1\n"))
        self.assertEqual(code, ("exit", 2))
        self.assertIn("The tests affected by this session's changes fail (task t-1)", err)

    def test_s8c_125_and_124_could_not_run(self):
        for exit_code, line in ((125, "run-all.sh: not run on box-1: not reachable (box-1: needs_login)"),
                                (124, "run-all.sh: not run on box-1: no slot came free in 570 s")):
            code, err = self.hook(self.script("echo some output\necho '{}'\nexit {}\n".format(line, exit_code)))
            self.assertEqual(code, ("exit", 2), err)
            self.assertIn("The affected tests could not run (task t-1): {}. Do not change code for this; report "
                          "it.\n".format(line), err)  # t-117 R7: no space rule named
            self.assertNotIn("r-cloud-runs", err)
            self.assertNotIn("Fix the code", err)

    def test_s8d_the_stop_variant_lets_the_session_stop(self):
        code, err = self.hook(self.script("echo 'run-all.sh: not run on box-1: setup failed: x'\nexit 125\n"), "--stop")
        self.assertEqual(code, 1, err)
        self.assertIn("The affected tests could not run (task t-1): run-all.sh: not run on box-1: setup failed: x.",
                      err)

    def assert_did_not_finish(self, code, err, line):
        self.assertEqual(code, ("exit", 2), err)
        self.assertIn("The affected tests did not finish (task t-1): {}. A test may hang: fix the code and run them "
                      "again.".format(line), err)
        self.assertNotIn("could not run", err)
        self.assertNotIn("Do not change code", err)

    def test_s8e_the_hooks_own_timeout_is_a_failure_to_fix(self):
        # MF-J2-1 (review-j2-hang.py): a test the change makes hang outlives run_shell's limit (900 s; 1 s here)
        saved = pa_hooks.AFFECTED_LIMIT
        pa_hooks.AFFECTED_LIMIT = 1
        self.addCleanup(setattr, pa_hooks, "AFFECTED_LIMIT", saved)
        code, err = self.hook(self.script("echo '=== unit tests: test_x'\nexec sleep 20\n"))
        self.assert_did_not_finish(code, err, "(timed out after 1 s)")

    def test_s8f_review_j2_hang_run_shell_timing_out(self):
        # the reviewer's case as it was written: run_shell returns what it returns on its own timeout
        saved = pa_hooks.run_shell
        pa_hooks.run_shell = lambda *a, **k: (124, "=== unit tests: test_x\n...\n(timed out after 900 s)")
        self.addCleanup(setattr, pa_hooks, "run_shell", saved)
        code, err = self.hook(self.script("exit 0\n"))
        self.assert_did_not_finish(code, err, "(timed out after 900 s)")

    def test_s8g_the_deadline_during_a_started_run_is_a_failure_to_fix(self):
        line = ("run-all.sh: did not finish on box-1: the deadline (PA_SUITE_DEADLINE) came after 870 s; the run "
                "there was ended")
        code, err = self.hook(self.script("echo '--- unit tests'\necho '{}'\nexit 124\n".format(line)))
        self.assert_did_not_finish(code, err, line)

    def test_s8h_the_stop_variant_holds_the_session_for_a_run_that_did_not_finish(self):
        line = "run-all.sh: did not finish on box-1: timed out after 3600 s there"
        code, err = self.hook(self.script("echo '{}'\nexit 124\n".format(line)), "--stop")
        self.assert_did_not_finish(code, err, line)


class S9Interrupt(Slot):
    def test_s9_term_during_a_remote_run_ends_it_and_cleans(self):
        go = os.path.join(self.tmp, "never")
        log = os.path.join(self.tmp, "slot.out")
        with open(log, "w") as f:
            p = subprocess.Popen(self.slot_argv("--affected"), cwd=self.wt, env=self.env(FAKE_GO=go),
                                 stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT)
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.wait()))
        self.assertTrue(self.until(lambda: self.remote_runs()), pa.read_text(log))
        self.assertTrue(self.until(lambda: "fake --affected" in (pa.read_text(log) or "")), pa.read_text(log))
        p.send_signal(signal.SIGTERM)
        self.assertEqual(p.wait(timeout=60), 143, pa.read_text(log))
        self.assertEqual(self.terminals(), [])
        self.assertEqual(self.remote_folders(), [])


class N1Node(Slot):
    def test_n1_a_node_script_that_finds_its_binary_beside_it_runs(self):
        folder = os.path.join(self.tmp, "nodebin")
        write(os.path.join(folder, "node"), '#!/bin/sh\nexec "$(dirname "$0")/node_real" "$@"\n')
        write(os.path.join(folder, "node_real"), '#!/bin/sh\nif [ "${1:-}" = --version ]; then echo v24.1.0; exit 0; fi\n'
                                                  'echo "node_real ran $*"\n')
        for name in ("node", "node_real"):
            os.chmod(os.path.join(folder, name), 0o755)
        self.setenv(PATH=folder + os.pathsep + os.environ["PATH"])
        code, out = self.pa_run("--", "node", "hello.js")
        self.assertEqual(code, 0, out)
        self.assertIn("node_real ran hello.js", out)


class N2Binaries(Slot):
    def test_n2_a_binary_tool_started_with_a_minimal_env_gets_nothing_added(self):
        # A test starting python3 with env={"PATH": ...} (test_pa_costs j2n6) sees only what it gave: a binary is
        # linked into bin/, not wrapped in a sh that would add SHLVL and PWD.
        code, out = self.pa_run("--", "python3", "-c", "import os, subprocess; print('child env', subprocess.run("
                                "['python3', '-c', 'import os; print(sorted(k for k in os.environ if k in (\\'SHLVL\\', "
                                "\\'PWD\\')))'], env={'PATH': os.environ['PATH']}, stdout=subprocess.PIPE, "
                                "universal_newlines=True).stdout.strip())")
        self.assertEqual(code, 0, out)
        self.assertIn("child env []", out)


class K1Kinds(Slot):
    def test_k1_the_kind_is_inferred_unless_given(self):
        cases = [(["--", "sh", "tests/run-all.sh", "--affected"], "affected"),
                 (["--", "sh", "tests/run-all.sh", "--module", "test_x"], "module"),
                 (["--", "python3", "-m", "unittest", "--help"], "single"),
                 (["--commit", "HEAD", "--overlay", "a.txt", "--", "sh", "probe.sh"], "failing-first"),
                 (["--", "sh", "probe.sh"], "other"),
                 (["--for", "mutation", "--", "sh", "tests/run-all.sh", "--affected"], "mutation")]
        for args, kind in cases:
            code, out = self.pa_run("--local", *args)
            self.assertEqual(code, 0, out)
            self.assertEqual(self.run_rows()[-1]["kind"], kind, args)


class T1LocalTimes(Slot):
    def test_t1_a_local_run_has_its_own_time_and_setup_apart(self):
        code, out = self.pa_run("--local", "--", "sh", "probe.sh")
        self.assertEqual(code, 0, out)
        row = self.run_rows()[-1]
        self.assertIsInstance(row["ran"], (int, float))
        self.assertEqual(row["overhead"].get("setup"), 0)
        code, out = self.pa_run("--local", "--commit", "HEAD", "--overlay", "a.txt", "--", "sh", "probe.sh")
        self.assertEqual(code, 0, out)
        row = self.run_rows()[-1]
        self.assertIsInstance(row["ran"], (int, float))
        self.assertIsInstance(row["overhead"].get("setup"), (int, float))
        self.assertLessEqual(row["ran"], row["seconds"])


class T2Report(Slot):
    def row(self, kind, place, seconds, waited=0, exit=0):
        return {"at": pa.now(), "run": "x", "kind": kind, "place": place, "machine": None if place == "local" else place,
                "seconds": seconds, "waited": waited, "ran": seconds - 1, "exit": exit,
                "overhead": {"probe": 0.3, "ship": 3.0} if place != "local" else {"setup": 0}}

    def test_t2_the_report_gives_counts_medians_and_a_verdict(self):
        rows = []
        rows += [self.row("affected", "local", s) for s in (400, 420, 380)]
        rows += [self.row("affected", "box-1", s, waited=50) for s in (150, 160, 140)]
        rows += [self.row("single", "local", s) for s in (2, 3, 4)]
        rows += [self.row("single", "box-1", s) for s in (8, 9, 10)]
        rows += [self.row("module", "local", s) for s in (20, 21, 22)]
        rows += [self.row("module", "box-1", s) for s in (21, 22, 23)]
        rows += [self.row("mutation", "local", s) for s in (30, 31)]
        rows += [self.row("single", "local", 900, exit=125), self.row("single", "local", 900, exit=124)]
        write(os.path.join(pa.ROOT, "test-runs.jsonl"), "".join(json.dumps(r) + "\n" for r in rows))
        os.chdir(self.tmp)  # no worktree needed
        code, out = self.pa_run("--report")
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"affected: faster on box-1 \(local 3 runs, median 400(\.0)?s; box-1 3 runs, median "
                              r"100(\.0)?s\)")
        self.assertRegex(out, r"single: faster locally \(local 3 runs, median 3(\.0)?s; box-1 3 runs, median 9(\.0)?s\)")
        self.assertRegex(out, r"module: about the same \(local 3 runs")
        self.assertIn("mutation: not enough runs (2 local, 0 on a machine; 3 each needed)", out)
        self.assertRegex(out, r"setup\+transfer")


# ---- t-117 ----

def slot_is_free(path):
    import fcntl
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        fcntl.flock(f, fcntl.LOCK_UN)
    return True


def median_rows(seconds=1, kind="affected", place="box-1"):
    """Three recorded runs of kind at place, so the slot step's wait_end there is the deadline less `seconds`."""
    write(os.path.join(pa.ROOT, "test-runs.jsonl"), "".join(json.dumps(
        {"at": pa.now(), "run": "m{}".format(i), "kind": kind, "place": place, "machine": None if place == "local"
         else place, "seconds": seconds, "waited": 0, "exit": 0}) + "\n" for i in range(3)))


class HookCase(Slot):
    """S8Hook's helpers without its tests."""
    hook, script = S8Hook.hook, S8Hook.script

    def could_not_run(self, line, exit_code):
        path = os.path.join(self.tmp, "line.txt")
        write(path, "some output\n" + line + "\n")
        code, err = self.hook(self.script("cat {}\nexit {}\n".format(path, exit_code)))
        self.assertEqual(code, ("exit", 2), err)
        self.assertIn("The affected tests could not run (task t-1): {}.".format(line.rstrip(".")), err)
        self.assertNotIn("Fix the code", err)


class Q3SlotRecords(HookCase):
    """R3 (AC3.2): the slot step's refusals before a Run exists write one test-runs.jsonl line (exit 125, the note)."""

    def check(self, code, out, err, reason):
        self.assertEqual(code, 125, out + err)
        last = out.splitlines()[-1]
        self.assertIn(reason, last)
        rows = self.rows(kind="affected")
        self.assertEqual(len(rows), 1, self.run_rows())
        self.assertEqual((rows[0]["exit"], rows[0]["place"]), (125, None), rows)
        self.assertIn(reason, rows[0].get("note") or "", rows)
        self.assertEqual(self.remote_runs(), [])
        self.could_not_run(last, 125)

    def test_q3d_a_bad_changed_files_path(self):
        code, out, err, _ = self.slot("--affected", PA_CHANGED_FILES="a.txt\n../x.txt")
        self.check(code, out, err, "../x.txt")

    def test_q3e_a_named_profile_that_cannot_be_read(self):
        path = os.path.join(pa.ROOT, "profiles", "test", "profile.json")
        kept = pa.read_text(path)
        write(path, "{not json\n")
        code, out, err, _ = self.slot("--affected")
        write(path, kept)  # the hook (in check) reads it
        self.check(code, out, err, "cannot be read")


class Q4SlotStep(Slot):
    """R4 for the slot step: TERM during tree_to_run is 143 with a record `interrupted`; any other exception in
    slot_run is 125 with `the slot step failed (...)`, never 1 (which the hook reads as a test failure)."""

    def test_q4b_term_during_git_add_is_143(self):
        folder = os.path.join(self.tmp, "stallbin")
        write(os.path.join(folder, "git"), tpr.GIT_STALL)
        os.chmod(os.path.join(folder, "git"), 0o755)
        mark = os.path.join(self.tmp, "stalled")
        log = os.path.join(self.tmp, "slot.out")
        env = self.env(PATH=folder + os.pathsep + os.environ["PATH"], GIT_STALL_MARK=mark,
                       GIT_STALL_REAL=__import__("shutil").which("git"))
        with open(log, "w") as f:
            p = subprocess.Popen(self.slot_argv("--affected"), cwd=self.wt, env=env, stdin=subprocess.DEVNULL, stdout=f,
                                 stderr=subprocess.STDOUT)
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.wait()))
        self.assertTrue(self.until(lambda: os.path.exists(mark)), pa.read_text(log))
        p.send_signal(signal.SIGTERM)
        self.assertEqual(p.wait(timeout=60), 143, pa.read_text(log))
        self.assertNotIn("Traceback", pa.read_text(log))
        rows = self.rows(kind="affected")
        self.assertEqual(len(rows), 1, self.run_rows())
        self.assertEqual(rows[0]["exit"], 143, rows)
        self.assertIn("interrupted", rows[0].get("note") or "", rows)
        steps = [pa_suite.read_progress(os.path.join(pa_suite.runs_dir(), n)) for n in os.listdir(pa_suite.runs_dir())
                 if n.endswith(".json")]
        steps = [s for s in steps if s.get("kind") == "run-all.sh"]
        self.assertTrue(steps)
        self.assertEqual([s.get("state") for s in steps], ["done"] * len(steps), steps)

    def test_q4c_an_exception_in_slot_run_is_125_with_its_line(self):
        # AC4.2: the evidence folder cannot be made (a file is in its place): os.makedirs raises
        evidence = os.path.join(self.dir, "evidence", "t-1")
        if os.path.isdir(evidence):
            __import__("shutil").rmtree(evidence)
        write(evidence, "a file where the folder goes\n")
        code, out, err, _ = self.slot("--affected")
        self.assertEqual(code, 125, out + err)
        self.assertNotIn("Traceback", out + err)
        self.assertRegex(out.splitlines()[-1], r"^run-all\.sh: not run on box-1: the slot step failed \(\w+: .+\)$")
        self.assertEqual(self.remote_runs(), [])


class Q7HookReading(HookCase):
    """R7 (RL-J2-3, Nit-J2-6): "could not run" only for 124/125 whose last line is the slot step's own."""

    def test_q7a_a_commands_own_124_and_125_are_failures_to_fix(self):
        for exit_code in (124, 125):
            for args in ((), ("--stop",)):
                with self.subTest(exit=exit_code, args=args):
                    code, err = self.hook(self.script("echo boom\nexit {}\n".format(exit_code)), *args)
                    self.assertEqual(code, ("exit", 2), err)  # --stop holds the session too
                    self.assertIn("The tests affected by this session's changes fail (task t-1)", err)
                    self.assertNotIn("could not run", err)

    def test_q7b_each_producers_line_is_could_not_run(self):
        self.hold(os.path.join(pa_suite.slot_dir(), "slot-0"))
        real = []
        try:  # this Mac's slot: no slot came free, and the caller-gone wait, as acquire says them
            pa_suite.acquire(1, {}, limit=0, say=lambda m: None, who="tests/run-all.sh")
        except pa_suite.Waited as e:
            real.append(str(e))
        try:
            pa_suite.acquire(1, {}, limit=60, say=lambda m: None, who="tests/run-all.sh",
                             gone=lambda: "its caller is gone (pid 1)")
        except pa_suite.Waited as e:
            real.append(str(e))
        self.assertEqual(len(real), 2, real)
        cases = [(real[0], 124), (real[1], 124),
                 ("run-all.sh: not run on box-1: not reachable (box-1: needs_login) (suiteFallback \"none\": never this "
                  "Mac on its own; --local runs here)", 125),
                 ("run-all.sh: not run on box-1: no slot came free in 840 s (running: box-1 (cap 1): task t-9)", 124),
                 ("run-all.sh: not run on box-1: the slot step cannot load its parts (SyntaxError: bad)", 125),
                 ("run-all.sh: not run on box-1: PA_CHANGED_FILES names '../x': not a relative path inside the tree", 125),
                 ("run-all.sh: not run on box-1: the deadline (PA_SUITE_DEADLINE) leaves no time to run (5 s left, the "
                  "run takes about 300 s there)", 124)]
        for line, exit_code in cases:
            with self.subTest(line=line):
                self.could_not_run(line, exit_code)

    def test_q7c_no_space_rule_is_named(self):
        # AC7.3
        found = []
        for root, _, files in os.walk(os.path.join(REPO, "runtime")):
            for name in files:
                try:
                    with open(os.path.join(root, name), encoding="utf-8", errors="replace") as f:
                        if "r-cloud-runs" in f.read():
                            found.append(os.path.relpath(os.path.join(root, name), REPO))
                except OSError:
                    pass
        self.assertEqual(found, [])


class Q8DeadlineAndRunLimit(HookCase):
    """R8 (RL-J2r2-1; t-079 RL-a as R8c): no remote run starts when the deadline leaves no time for it; a remote run
    is ended PA_SUITE_RUN_LIMIT s after it took the slot."""

    def test_q8a_a_deadline_that_leaves_no_time_starts_no_run(self):
        # AC8.1: a free slot, no median there (300 s), the deadline 5 s away
        code, out, err, took = self.slot("--affected", PA_SUITE_DEADLINE=str(int(time.time()) + 5))
        self.assertEqual(code, 124, out + err)
        last = out.splitlines()[-1]
        self.assertRegex(last, r"^run-all\.sh: not run on box-1: the deadline \(PA_SUITE_DEADLINE\) leaves no time to "
                               r"run \(\d+ s left, the run takes about \d+ s there\)$")
        self.assertLess(took, 5, out + err)
        self.assertEqual(self.remote_runs(), [])
        self.assertFalse([c for c in self.calls() if c[:2] == ["file", "write"]], self.calls())
        self.assertEqual(self.remote_folders(), [])
        self.assertTrue(slot_is_free(self.machine_slot("box-1")))
        self.could_not_run(last, 124)

    def test_q8c_a_remote_run_past_its_run_limit_is_ended_there(self):
        # AC8.3: PA_SUITE_RUN_LIMIT=3, a far deadline (20 s, a 1 s median there), a run that never ends
        median_rows(1)
        go = os.path.join(self.tmp, "never")
        code, out, err, took = self.slot("--affected", FAKE_GO=go, PA_SUITE_RUN_LIMIT="3",
                                         PA_SUITE_DEADLINE=str(int(time.time()) + 20))
        self.assertEqual(code, 124, out + err)
        self.assertLess(took, 15, out + err)
        self.assertEqual(out.splitlines()[-1], "run-all.sh: did not finish on box-1: it ran past its 3 s limit after "
                                               "taking the slot (PA_SUITE_RUN_LIMIT); the run there was ended")
        self.assertIn("runlimit [unset]", out)  # the command never sees it
        self.assertEqual(self.terminals(), [])
        self.assertEqual(self.remote_folders(), [])
        rows = [r for r in self.rows(kind="affected", machine="box-1") if r["run"] not in ("m0", "m1", "m2")]
        self.assertEqual([r["exit"] for r in rows], [124], self.run_rows())
        self.assertTrue(pa_hooks.did_not_finish(out), out)


class Q13SlotNoUsableMachine(Slot):
    """R13 for the slot step: suiteMachines with no usable entry and "none": --affected is 125."""

    def test_q13c_the_slot_steps_affected_is_125(self):
        self.configure([{"machine": "bad name!"}])
        self.set_fallback("none")
        code, out, err, _ = self.slot("--affected")
        self.assertEqual(code, 125, out + err)
        self.assertIn("no usable machine in suiteMachines", out + err)
        self.assertNotIn("fake --affected", out)
        self.assertEqual(self.calls(), [])


class Q14FullSuiteTimes(Slot):
    """R14 (RL-J2-1): pa-full-suite's remote line: ran from the run folder's t0/t1 there, copyback after t1."""
    TIMED = r"""#!/bin/sh
case "${1:-}" in
  --except-integration) sleep 2; echo "=== summary (x)"; echo "  unit tests: test_a: ok (2s)"; exit 0 ;;
  --only-integration) sleep 5; echo "  pa-session integration (private hc core): ok (5s)"; exit 0 ;;
  *) echo "fake whole run"; exit 0 ;;
esac
"""

    def test_q14a_ran_is_the_runs_own_time_there(self):
        # AC14.1 (s7d extended): the remote part sleeps 2 s, the local part 5 s
        self.set_command("sh tests/run-all.sh")
        os.remove(os.path.join(self.wt, "tests", "run-all.sh"))
        write(os.path.join(self.wt, "tests", "run-all.sh"), self.TIMED)
        sh(self.wt, "git", "add", "-A")
        sh(self.wt, "git", "commit", "-q", "-m", "a timed fake run-all")
        code, out = self.main()
        self.assertEqual(code, 0, out)
        rows = [r for r in self.run_rows() if r["kind"] == "full"]
        self.assertEqual(len(rows), 1, self.run_rows())
        row = rows[0]
        self.assertEqual(row["place"], "box-1")
        self.assertIsInstance(row["ran"], (int, float), row)
        self.assertLessEqual(abs(row["ran"] - 2), 1, row)
        self.assertIsInstance(row["overhead"].get("copyback"), (int, float), row)
        self.assertGreaterEqual(row["seconds"], row["ran"], row)


class Q18MacSlotDeadline(HookCase):
    """R18 (RL-J2r3-1, X15): this Mac's only slot held, no machines, a deadline DEADLINE_MARGIN + 4 s away: the wait
    ends before the deadline with `no slot came free` (124), read as could not run."""

    def test_q18_the_wait_on_this_macs_slot_ends_before_the_deadline(self):
        self.configure([])
        self.hold(os.path.join(pa_suite.slot_dir(), "slot-0"))
        at = int(time.time()) + pa_suite.DEADLINE_MARGIN + 4
        began = time.time()
        r = subprocess.run(self.slot_argv("--module", "test_x"), cwd=self.wt, env=self.env(PA_SUITE_DEADLINE=str(at)),
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=30)  # X15 (no cap) waits 840 s: this times out
        took = time.time() - began
        self.assertEqual(r.returncode, 124, r.stdout + r.stderr)
        self.assertLess(took, 12, r.stdout + r.stderr)
        self.assertLess(time.time(), at)
        last = r.stdout.splitlines()[-1]
        self.assertRegex(last, r"^\S+: no slot came free in \d+ s")
        self.assertNotIn("fake --module", r.stdout)
        self.could_not_run(last, 124)


# pa_suite.py's slot step with REMOTE_TIMEOUT patched (R20): loaded as the module pa_run finds as S
TIMEOUT_DRIVER = r"""import importlib.util, sys
spec = importlib.util.spec_from_file_location("pa_suite", sys.argv[1])
m = importlib.util.module_from_spec(spec)
sys.modules["pa_suite"] = m
spec.loader.exec_module(m)
m.pa = None
m.REMOTE_TIMEOUT = int(sys.argv[2])
sys.exit(m.run_under_slot(sys.argv[4:]))
"""


class Q20RemoteTimeout(Slot):
    """R20 (VERIFY J2r2 Nit-J2r2-2, M11): a remote slot-step run past S.REMOTE_TIMEOUT (2 s here) is 124, `timed out
    after 2 s there`."""

    def test_q20_a_remote_run_past_remote_timeout(self):
        driver = os.path.join(self.tmp, "timeout_driver.py")
        write(driver, TIMEOUT_DRIVER)
        go = os.path.join(self.tmp, "never")
        argv = [sys.executable, driver, SUITE, "2"] + self.slot_argv("--affected")[2:]
        began = time.time()
        r = subprocess.run(argv, cwd=self.wt, env=self.env(FAKE_GO=go, PYTHONPATH=None), stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
        self.assertEqual(r.returncode, 124, r.stdout + r.stderr)
        self.assertLess(time.time() - began, 30, r.stdout + r.stderr)
        self.assertEqual(r.stdout.splitlines()[-1], "run-all.sh: did not finish on box-1: timed out after 2 s there")
        self.assertEqual(self.terminals(), [])
        self.assertEqual(self.remote_folders(), [])


class Q21StartThenEdit(Slot):
    """R21 (Nit-J2a-2): a start snapshot and no change: 0, the command not run; then an edit: it runs with t-079's
    deadline, and its 124 `not run ...` line is could not run."""

    def test_q21_no_change_since_the_start_then_an_edit(self):
        saved = pa_hooks.START_DIR
        pa_hooks.START_DIR = os.path.join(self.tmp, "starts")
        self.addCleanup(setattr, pa_hooks, "START_DIR", saved)
        seen = os.path.join(self.tmp, "seen")
        script = S8Hook.script(self, 'echo "$PA_SUITE_DEADLINE" > {}\necho "run-all.sh: not run on box-1: no slot came '
                                     'free in 5 s (running: x)"\nexit 124\n'.format(seen))
        agent = pa.read_json(os.path.join(self.dir, "agent.json"))
        agent["commands"] = {"affected": "sh " + script}
        dump(agent, os.path.join(self.dir, "agent.json"))
        write(os.path.join(self.wt, "a.txt"), "edited before the start\n")
        data = {"cwd": self.wt, "session_id": "s-q21-{}".format(time.time())}
        self.assertEqual(pa_hooks.hook_session_start(data, []) or 0, 0)
        self.assertEqual(pa_hooks.hook_affected(data, []), 0)
        self.assertFalse(os.path.exists(seen), "the command ran with no change since the start")
        write(os.path.join(self.wt, "a.txt"), "edited after the start\n")
        err = __import__("io").StringIO()
        began = time.time()
        with __import__("contextlib").redirect_stderr(err):
            try:
                code = pa_hooks.hook_affected(data, [])
            except SystemExit as e:
                code = ("exit", e.code)
        self.assertEqual(code, ("exit", 2), err.getvalue())
        self.assertIn("The affected tests could not run (task t-1): run-all.sh: not run on box-1: no slot came free "
                      "in 5 s (running: x).", err.getvalue())
        self.assertAlmostEqual(int(pa.read_text(seen).strip()), began + pa_hooks.AFFECTED_LIMIT - pa_hooks.DEADLINE_SPARE,
                               delta=5)


if __name__ == "__main__":
    unittest.main()
