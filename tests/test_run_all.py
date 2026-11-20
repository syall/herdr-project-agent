"""tests/run-all.sh itself (t-017 J6, R7): parts in parallel, output in a fixed order, a summary that names
every part and every failure, PA_SERIAL=1, --affected; J9: TERM, INT and HUP stop every part and its children,
and each unit test module (tests/test_*.py) is its own part; t-031: the panel harness runs as four parts, one per
shard (run.mjs --shard=<k>/4), each named in the summary; t-072: every run takes pa-full-suite's slot (s1-s7);
t-076: a part past its limit (3x its median, at least PA_PART_LIMIT) is stopped and named hung (h1-h3), every run
has a progress file under HERDR_PA_HOME/suite-runs (p1-p2), --module runs one unit part (m1); t-079: a wait that
answers at once for a running part waits for its end (r2d), a killed slot step stops the run (r8c), the run's own
limit after the slot (s10, s11), git link guards (g5, g6); t-117: the slot step's own variables are not the parts'
(s12, R16), an inherited PA_SUITE_HC_SCOPE without HC_SCOPE reaches no hc call (h3, R19); t-103: the part list
after its `=== parts` line, a failed part's tail never read as parts (f1, R14). The real script runs
in a temp copy of the repo whose parts are stubs (stub.sh, chosen per part by STUB_<part>), so no real suite runs here."""
import json
import fcntl
import glob
import importlib.util
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SHARDS = ["panel harness {}/4".format(k) for k in (1, 2, 3, 4)]
# t-112: the integration check is three parts, each its own pa_session_check.sh --part <k> with its own core.
INTEGRATION = ["pa-session integration {}/3 (private hc core)".format(k) for k in (1, 2, 3)]
NAMES = ["python 3.7 parse", "unit tests: test_stub", "keeper harness"] + SHARDS + ["store harness"] + INTEGRATION
# The stub pa_session_check.sh: records its arguments and runs stub.sh as integration<k> for --part <k> (plain
# "integration" without one), so STUB_integration<k> picks each part's mode.
CHECK = '''echo "$*" >> "$STUB_DIR/integrationargs"
k=; [ "${2:-}" = --part ] && k=${3:-}
exec sh stub.sh "integration$k"
'''
# The stub panel run.mjs: records its arguments and runs stub.sh as panel<k> for --shard=<k>/<n> (plain "panel"
# without one), so STUB_panel<k> picks each shard's mode.
PANEL = '''echo "$*" >> "$STUB_DIR/panelargs"
k=${1#--shard=}; k=${k%%/*}
exec sh stub.sh "panel$k"
'''

STUB = r'''k=$1
eval "m=\${STUB_$k:-pass}"
echo "start $k" >> "$STUB_DIR/events"
echo "$k ${TMPDIR:-unset}" >> "$STUB_DIR/tmpdirs"
echo "$k says hello"
case "$m" in
  pass) ;;
  fail) echo "PASS ok-case"; echo "FAIL K-x: boom"; echo "FAIL K-y: bang"; echo "end $k" >> "$STUB_DIR/events"; exit 1 ;;
  crash) i=1; while [ $i -le 50 ]; do echo "$k line $i"; i=$((i + 1)); done; exit 3 ;;
  quiet) exit 1 ;;
  parts) echo "  inner part: ok (1s)"; echo "  inner other: FAILED (2s)"; echo "=== parts"; exit 3 ;;
  sleep:*) sleep "${m#sleep:}" ;;
  child) sleep 300 & c=$!
    echo "$$ $c" > "$STUB_DIR/$k.pids.new"; mv "$STUB_DIR/$k.pids.new" "$STUB_DIR/$k.pids"; wait ;;
  probe) "$PY_PROBE" -c "import fcntl, sys
f = open(sys.argv[1], 'a+')
try:
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.exit(0)
print('FAIL slot: free during the run'); sys.exit(1)" "$HERDR_PA_HOME/suite-slots/slot-0" || exit 1 ;;
  envs) env | grep -E '^(HC_|PA_SUITE_HC_)' | sort > "$STUB_DIR/$k.env"
    env | grep -E '^PA_SUITE_(DEADLINE|LOCAL_RUN)=' | sort > "$STUB_DIR/$k.slotenv"
    env | grep -E '^PA_SUITE_RUN_LIMIT=' > "$STUB_DIR/$k.runenv" ;;
  git) { echo "git=$(command -v git)"; echo "python3=$(command -v python3)"; echo "path=$PATH"; } > "$STUB_DIR/$k.git" ;;
  wait:*) other=${m#wait:}; n=0
    until grep -q "start $other" "$STUB_DIR/events"; do
      n=$((n + 1)); [ $n -gt 200 ] && { echo "FAIL barrier: $other never started"; exit 1; }; sleep 0.1
    done ;;
esac
echo "$k done"
echo "end $k" >> "$STUB_DIR/events"
'''

UNITS = '''import os, tempfile, unittest
M = os.environ.get("STUB_units", "pass")
class T(unittest.TestCase):
    def test_one(self):
        with open(os.path.join(os.environ["STUB_DIR"], "tmpdirs"), "a") as f:
            f.write("units %s\\n" % tempfile.gettempdir())
        if M == "loud":
            print("\\n".join("noise %d" % i for i in range(2000)))
        if M == "fail":
            self.assertEqual(1, 2)
    def test_two(self):
        if M == "fail":
            raise RuntimeError("boom")
'''


# A unit module that records each test it ran; test_x waits until the other module has started (so the two run
# at once) and fails when STUB_<me>=fail.
UNIT_MODULE = '''import os, time, unittest
D = os.environ["STUB_DIR"]
def mark(line):
    with open(os.path.join(D, "ran"), "a") as f:
        f.write(line + "\\n")
class T(unittest.TestCase):
    def test_x(self):
        open(os.path.join(D, "started-{me}"), "w").close()
        end = time.time() + 20
        while not os.path.exists(os.path.join(D, "started-{other}")) and time.time() < end:
            time.sleep(0.05)
        mark("{me}.test_x")
        self.assertTrue(os.path.exists(os.path.join(D, "started-{other}")), "test_{other} never started")
        self.assertNotEqual(os.environ.get("STUB_{me}"), "fail")
    def test_y(self):
        mark("{me}.test_y")
'''


# A long module that run-all.sh runs in shards (t-078): the real unit_shards.py hook, a class fixture that records
# its shard, and test methods (filled in) that record themselves; STUB_fail names one test that fails.
SHARDED = '''import os, unittest
from unit_shards import load_tests  # noqa: F401
D = os.environ["STUB_DIR"]
def mark(line):
    with open(os.path.join(D, "ran"), "a") as f:
        f.write(line + "\\n")
    if os.environ.get("STUB_fail") == line:
        raise AssertionError("boom " + line)
class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        mark("class {me} " + os.environ.get("PA_UNIT_SHARD", "-"))
{tests}'''


# Holds slot-0 of the suite slots (an flock, its holder line as pa_suite writes it), then touches the ready file.
# With a command after them, runs it as its child with PA_SUITE_SLOT naming the slot (as pa-full-suite does; not
# with STUB_NO_MARKER, as the pa-full-suite before t-072 does) and exits with its code; else sleeps until killed.
HOLDER = r'''import fcntl, json, os, subprocess, sys, time
path, ready = sys.argv[1], sys.argv[2]
os.makedirs(os.path.dirname(path), exist_ok=True)
f = open(path, "a+")
fcntl.flock(f, fcntl.LOCK_EX)
f.seek(0)
f.truncate()
f.write(json.dumps({"task": "t-holder", "worker": "w-holder", "pid": os.getpid(), "start": "then"}))
f.flush()
open(ready, "w").close()
if len(sys.argv) > 3:  # STUB_NO_MARKER: a caller from before t-072, which holds the slot but sets no PA_SUITE_SLOT
    marker = {} if os.environ.get("STUB_NO_MARKER") else {"PA_SUITE_SLOT": path}
    sys.exit(subprocess.run(sys.argv[3:], env=dict(os.environ, **marker)).returncode)
time.sleep(600)
'''


def write(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, mode)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class RunAll(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="t017-runall-"))
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "tests"))
        shutil.copy(os.path.join(HERE, "run-all.sh"), os.path.join(self.repo, "tests", "run-all.sh"))
        for name in ("pa", "pa_hooks", "pa_guard", "pa_costs", "pa_push", "pa_queue", "pa_places", "pa_statusline", "pa_handoff"):
            write(os.path.join(self.repo, "runtime", "tools", name + ".py"), "x = 1\n")
        shutil.copy(os.path.join(HERE, "..", "runtime", "tools", "pa_suite.py"), os.path.join(self.repo, "runtime", "tools"))
        self.home = os.path.join(self.tmp, "pa")  # HERDR_PA_HOME: the slots are under it, never the real one
        self.slot = os.path.join(self.home, "suite-slots", "slot-0")
        write(os.path.join(self.repo, "stub.sh"), STUB)
        write(os.path.join(self.repo, "tests", "test_stub.py"), UNITS)
        for part in ("keeper", "store"):
            write(os.path.join(self.repo, "tests", part, "run.mjs"), "exec sh stub.sh {}\n".format(part))
        write(os.path.join(self.repo, "tests", "panel", "run.mjs"), PANEL)
        write(os.path.join(self.repo, "tests", "setup-scratch.sh"), "exit 0\n")
        write(os.path.join(self.repo, "tests", "pa_session_check.sh"), CHECK)
        self.node = os.path.join(self.tmp, "node")
        write(self.node, '#!/bin/sh\nexec sh "$@"\n', 0o755)
        self.events = os.path.join(self.tmp, "events")
        write(self.events, "")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env_for(self, **stubs):
        # PA_SUITE_*: a marker or cap inherited from the suite running this test is not this run's.
        env = {k: v for k, v in os.environ.items() if not k.startswith(("STUB_", "PA_SERIAL", "PA_CHANGED", "PA_SUITE_"))}
        env.update({"NODE": self.node, "STUB_DIR": self.tmp, "TMPDIR": self.tmp, "HERDR_PA_HOME": self.home,
                    "PY_PROBE": sys.executable})
        env.update({k if k.startswith("PA_") else "STUB_" + k: v for k, v in stubs.items()})
        return env

    def run_all(self, *args, **stubs):
        r = subprocess.run(["sh", os.path.join(self.repo, "tests", "run-all.sh")] + list(args), cwd=self.tmp,
                           env=self.env_for(**stubs), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True, timeout=120)
        return r.returncode, r.stdout

    def outputs(self, out):
        last = out.rstrip().splitlines()[-1]
        self.assertIn("(outputs: ", last, last)
        folder = last.rsplit("(outputs: ", 1)[1].rstrip(")")
        self.assertTrue(folder.startswith(self.tmp) and os.path.isdir(folder), folder)
        return folder

    def summary(self, out):
        self.assertIn("\n=== summary", out)
        return out.rsplit("\n=== summary", 1)[1]

    def test_r1_all_pass(self):
        code, out = self.run_all()
        self.assertEqual(code, 0, out)
        self.assertTrue(out.rstrip().splitlines()[-1].startswith("=== all passed (outputs: "), out)
        folder = self.outputs(out)
        files = sorted(os.listdir(folder))
        self.assertEqual(len([f for f in files if f.endswith(".log")]), 11, files)
        keeper = [f for f in files if "keeper" in f and f.endswith(".log")][0]
        with open(os.path.join(folder, keeper)) as f:
            self.assertEqual(f.read(), "keeper says hello\nkeeper done\n")
        tail = self.summary(out)
        for name in NAMES:
            self.assertIn("{}: ok".format(name), tail)

    def test_r2_failures_named(self):
        code, out = self.run_all(units="fail", keeper="fail", panel2="crash")
        self.assertEqual(code, 1, out)
        last = out.rstrip().splitlines()[-1]
        self.assertTrue(last.startswith("=== 3 part(s) failed: unit tests: test_stub, keeper harness, panel harness 2/4 (outputs: "), last)
        tail = self.summary(out)
        self.assertIn("--- unit tests: test_stub: FAILED", tail)
        self.assertIn("FAIL: test_one", tail)
        self.assertIn("ERROR: test_two", tail)
        self.assertIn("FAIL K-x: boom", tail)
        self.assertIn("FAIL K-y: bang", tail)
        self.assertNotIn("PASS ok-case", tail)
        self.assertIn("--- panel harness 2/4: FAILED (no failing test named; its last 40 lines:)", tail)
        self.assertIn("panel2 line 11\n", tail)
        self.assertIn("panel2 line 50\n", tail)
        self.assertNotIn("panel2 line 10\n", tail)
        for name in ["python 3.7 parse", "store harness"] + INTEGRATION + SHARDS[:1] + SHARDS[2:]:
            self.assertIn("{}: ok".format(name), tail)
        self.outputs(out)

    @unittest.skipUnless(os.path.exists("/bin/bash"), "needs /bin/bash (its stand-in sh is bash with BASH_ENV)")
    def test_r2c_a_part_wait_cannot_find_keeps_its_own_code(self):
        # t-113: on a Linux machine (sh is bash there) `wait <pid>` said "pid N is not a child of this shell"
        # (127) for parts that had passed, and run-all.sh marked them FAILED. Here run-all.sh runs under a `sh` that
        # is bash with a `wait` that, in run-all.sh only, reaps the pid and then answers 127 the same way: each part's
        # code must still be its own (passing parts ok, a failing one FAILED with its code), in parallel and serial.
        bin_dir = os.path.join(self.tmp, "bin")
        lose = os.path.join(self.tmp, "lose.sh")
        write(lose, 'wait() {\n'
                    '  builtin wait "$@"; c=$?\n'
                    '  [ "${0##*/}" = run-all.sh ] && [ $# = 1 ] || return $c\n'
                    '  echo "wait: pid $1 is not a child of this shell" >&2; return 127\n'
                    '}\n')
        write(os.path.join(bin_dir, "sh"), '#!/bin/bash\nBASH_ENV="{}" exec /bin/bash "$@"\n'.format(lose), 0o755)
        path = bin_dir + os.pathsep + os.environ.get("PATH", "")
        for serial in ("", "1"):
            with self.subTest(serial=serial):
                write(self.events, "")
                env = dict(self.env_for(PA_SERIAL=serial, store="crash"), PATH=path)
                r = subprocess.run(["sh", os.path.join(self.repo, "tests", "run-all.sh")], cwd=self.tmp, env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                                   timeout=120)
                out = r.stdout
                self.assertIn("is not a child of this shell", out)  # the stand-in wait did answer 127
                self.assertEqual(r.returncode, 1, out)
                tail = self.summary(out)
                for name in NAMES:
                    self.assertIn("  {}: {}".format(name, "FAILED" if name == "store harness" else "ok"), tail)
                self.assertTrue(out.rstrip().splitlines()[-1].startswith("=== 1 part(s) failed: store harness (outputs: "), out)
                folder = self.outputs(out)
                with open(os.path.join(folder, "parts")) as f:
                    store = [line.split("\t")[0] for line in f if line.split("\t")[1] == "store harness"][0]
                with open(os.path.join(folder, store + ".code")) as f:
                    self.assertEqual(f.read().strip(), "3")  # the part's own code, not wait's 127

    @unittest.skipUnless(os.path.exists("/bin/bash"), "needs /bin/bash (its stand-in sh is bash with BASH_ENV)")
    def test_r2d_a_wait_that_answers_at_once_for_a_running_part_waits_for_its_end(self):
        # t-079 I4 (t-115 L1, t-113 REVIEW L1): `wait` in run-all.sh answers 127 at once, reaping nothing, while the
        # part still runs: the run must wait for the part's end and take its own code, not mark it FAILED and move on.
        bin_dir = os.path.join(self.tmp, "bin")
        lose = os.path.join(self.tmp, "lose.sh")
        write(lose, 'wait() {\n'
                    '  [ "${0##*/}" = run-all.sh ] && [ $# = 1 ] || { builtin wait "$@"; return $?; }\n'
                    '  echo "wait: pid $1 is not a child of this shell" >&2; return 127\n'
                    '}\n')
        write(os.path.join(bin_dir, "sh"), '#!/bin/bash\nBASH_ENV="{}" exec /bin/bash "$@"\n'.format(lose), 0o755)
        path = bin_dir + os.pathsep + os.environ.get("PATH", "")
        for serial in ("", "1"):
            with self.subTest(serial=serial):
                write(self.events, "")
                env = dict(self.env_for(PA_SERIAL=serial, keeper="sleep:2", store="crash"), PATH=path)
                r = subprocess.run(["sh", os.path.join(self.repo, "tests", "run-all.sh")], cwd=self.tmp, env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                                   timeout=120)
                out = r.stdout
                self.assertIn("is not a child of this shell", out)  # the stand-in wait did answer at once
                tail = self.summary(out)
                for name in NAMES:
                    self.assertIn("  {}: {}".format(name, "FAILED" if name == "store harness" else "ok"), tail)
                self.assertEqual(r.returncode, 1, out)
                # the stand-in's own "not a child" lines (stderr, between a part's lines in serial mode) left out
                body = "".join(l for l in out.splitlines(True) if not l.startswith("wait: pid "))
                self.assertIn("=== keeper harness\nkeeper says hello\nkeeper done\n--- keeper harness: ok\n", body)

    def test_r2b_quiet_failure(self):
        code, out = self.run_all(store="quiet")
        self.assertEqual(code, 1, out)
        self.assertIn("--- store harness: FAILED (no failing test named; its last 40 lines:)", self.summary(out))
        self.assertTrue(out.rstrip().splitlines()[-1].startswith("=== 1 part(s) failed: store harness (outputs: "), out)

    def test_f1_a_failed_parts_tail_is_not_read_as_parts(self):
        """t-103 R14: a failing part whose last lines look like parts (and hold a marker of their own): the record's
        parts (parse_parts) are the summary's list only, which comes right after the last `=== parts` line."""
        code, out = self.run_all(keeper="parts")
        self.assertEqual(code, 1, out)
        lines = out.rstrip().splitlines()
        self.assertIn("    inner part: ok (1s)", lines)  # the tail, as before
        self.assertEqual(lines[-len(NAMES) - 2], "=== parts", out)
        spec = importlib.util.spec_from_file_location("pa_suite_f1", os.path.join(HERE, "..", "runtime", "tools",
                                                                                "pa_suite.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        parts = module.parse_parts(out)
        self.assertEqual([p["name"] for p in parts], NAMES, parts)
        self.assertEqual([p["name"] for p in parts if not p["ok"]], ["keeper harness"])

    def test_r3_fixed_order(self):
        code, out = self.run_all(keeper="sleep:2")
        self.assertEqual(code, 0, out)
        with open(self.events) as f:
            events = f.read()
        self.assertLess(events.index("end store"), events.index("end keeper"), events)  # store ended first
        body = out.split("\n=== summary", 1)[0]
        at = [body.index("=== " + name + "\n") for name in NAMES]
        self.assertEqual(at, sorted(at), body)
        self.assertIn("=== keeper harness\nkeeper says hello\nkeeper done\n--- keeper harness: ok\n", body)

    def test_r4_parallel(self):
        code, out = self.run_all(keeper="wait:panel1", panel1="wait:keeper")
        self.assertEqual(code, 0, out)

    def test_r4b_serial(self):
        code, out = self.run_all(PA_SERIAL="1")
        self.assertEqual(code, 0, out)
        with open(self.events) as f:
            events = f.read().split()
        pairs = [events[i:i + 4] for i in range(0, len(events), 4)]
        self.assertEqual(pairs, [["start", k, "end", k] for k in ("keeper", "panel1", "panel2", "panel3", "panel4", "store", "integration1",
                                                               "integration2", "integration3")], events)
        self.assertTrue(out.rstrip().splitlines()[-1].startswith("=== all passed (outputs: "), out)

    def test_r4c_serial_failure(self):
        code, out = self.run_all(PA_SERIAL="1", keeper="fail")
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL K-x: boom", self.summary(out))

    def test_r5_tail_lists_every_part(self):
        code, out = self.run_all(units="loud", keeper="fail")
        self.assertEqual(code, 1, out)
        tail = "\n".join(out.rstrip().splitlines()[-40:])  # what pa-full-suite prints
        for name in NAMES:
            self.assertIn(name + (": FAILED" if name == "keeper harness" else ": ok"), tail)
        self.assertIn("FAIL K-x: boom", tail)

    def test_r6_affected(self):
        code, out = self.run_all("--affected", PA_CHANGED_FILES="tests/keeper/run.mjs")
        self.assertEqual(code, 0, out)
        with open(self.events) as f:
            self.assertEqual(f.read().split(), ["start", "keeper", "end", "keeper"])
        self.assertNotIn("=== unit tests", out)
        self.assertNotIn("integration", out)
        code, out = self.run_all("--affected", PA_CHANGED_FILES="README.md")
        self.assertEqual((code, out.strip()), (0, "no tested files changed: nothing to run"))

    def test_r6d_affected_panel_files_run_every_shard(self):
        # A change to the panel harness runs all four shards (and the store harness when the shared stubs change).
        for changed, want in (("tests/panel/run.mjs", ["panel1", "panel2", "panel3", "panel4"]),
                              ("tests/panel/stubs.mjs", ["panel1", "panel2", "panel3", "panel4", "store"]),
                              ("mods/project-agent/main.js", ["keeper", "panel1", "panel2", "panel3", "panel4", "store"])):
            with self.subTest(changed=changed):
                write(self.events, "")
                code, out = self.run_all("--affected", PA_CHANGED_FILES=changed)
                self.assertEqual(code, 0, out)
                with open(self.events) as f:
                    started = [line.split()[1] for line in f.read().splitlines() if line.startswith("start ")]
                self.assertEqual(sorted(started), sorted(want), out)
                for name in SHARDS:
                    self.assertIn("  {}: ok".format(name), self.summary(out))
                self.assertNotIn("integration", out)

    def test_r10_panel_in_four_shards(self):
        # Four panel parts at once (shard 1 and 4 each wait until the other has started), each run once with its
        # own --shard=<k>/4; a failing shard is named, with its failing cases, in the last 40 lines.
        code, out = self.run_all(panel1="wait:panel4", panel4="wait:panel1", panel3="fail")
        self.assertEqual(code, 1, out)
        with open(os.path.join(self.tmp, "panelargs")) as f:
            self.assertEqual(sorted(f.read().splitlines()), ["--shard={}/4".format(k) for k in (1, 2, 3, 4)], out)
        with open(self.events) as f:
            starts = [line for line in f.read().splitlines() if line.startswith("start panel")]
        self.assertEqual(sorted(starts), ["start panel{}".format(k) for k in (1, 2, 3, 4)], out)
        body = out.split("\n=== summary", 1)[0]
        at = [body.index("=== " + name + "\n") for name in NAMES]
        self.assertEqual(at, sorted(at), body)
        tail = "\n".join(out.rstrip().splitlines()[-40:])
        self.assertIn("--- panel harness 3/4: FAILED\n  FAIL K-x: boom\n  FAIL K-y: bang", tail)
        for name in NAMES:
            self.assertIn("  " + name + (": FAILED" if name == "panel harness 3/4" else ": ok"), tail)
        self.assertTrue(out.rstrip().splitlines()[-1].startswith("=== 1 part(s) failed: panel harness 3/4 (outputs: "), out)

    def test_r6b_affected_without_files_runs_the_fast_set(self):
        code, out = self.run_all("--affected")
        self.assertEqual(code, 0, out)
        for name in NAMES[:-3]:
            self.assertIn("{}: ok".format(name), self.summary(out))
        self.assertNotIn("integration", out)

    def test_r6c_each_part_has_its_own_temp_folder(self):
        # A test that looks for its own leftovers in the temp folder (test_pa_push c13) must not see another
        # part's, or another suite's running beside it: each part gets TMPDIR in the run folder.
        for serial in ("", "1"):
            write(os.path.join(self.tmp, "tmpdirs"), "")
            code, out = self.run_all(PA_SERIAL=serial)
            self.assertEqual(code, 0, out)
            folder = self.outputs(out)
            with open(os.path.join(self.tmp, "tmpdirs")) as f:
                seen = dict(line.split(" ", 1) for line in f.read().splitlines())
            self.assertEqual(sorted(seen), sorted(["units", "keeper", "panel1", "panel2", "panel3", "panel4", "store",
                                                   "integration1", "integration2", "integration3"]), seen)
            dirs = [os.path.realpath(d) for d in seen.values()]
            self.assertEqual(len(set(dirs)), 10, seen)
            for d in dirs:
                self.assertTrue(d.startswith(os.path.realpath(folder) + os.sep) and os.path.isdir(d), (d, folder))

    def test_r8_stop(self):
        # TERM, INT or HUP sent to the script alone (a closed pane, a kill, a Ctrl-C) stops every running part
        # and the children it started, in parallel and in PA_SERIAL=1 mode, and exits 128 + the signal.
        cases = [("", ("keeper", "panel1")), ("1", ("keeper",))]
        for serial, hung in cases:
            for sig, want in ((signal.SIGTERM, 143), (signal.SIGINT, 130), (signal.SIGHUP, 129)):
                with self.subTest(serial=serial, sig=sig.name):
                    self.stop_case(serial, hung, sig, want)

    def stop_case(self, serial, hung, sig, want):
        for k in hung:
            if os.path.exists(os.path.join(self.tmp, k + ".pids")):
                os.remove(os.path.join(self.tmp, k + ".pids"))
        env = self.env_for(PA_SERIAL=serial, **{k: "child" for k in hung})
        log = os.path.join(self.tmp, "stop.out")
        with open(log, "w") as out:
            proc = subprocess.Popen(["sh", os.path.join(self.repo, "tests", "run-all.sh")], cwd=self.tmp, env=env,
                                    stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        pids = []
        try:
            for k in hung:
                path = os.path.join(self.tmp, k + ".pids")
                end = time.time() + 30
                while not os.path.exists(path) and time.time() < end:
                    time.sleep(0.05)
                with open(path) as f:
                    pids += [int(p) for p in f.read().split()]  # the part's shell and the child it started
            os.kill(proc.pid, sig)
            try:
                code = proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                code = None
            end = time.time() + 5
            while time.time() < end and any(alive(p) for p in pids):
                time.sleep(0.05)
            left = [p for p in pids if alive(p)]
            with open(log) as f:
                text = f.read()
            self.assertEqual(code, want, text)
            self.assertIn("=== interrupted (outputs so far: ", text)
            self.assertEqual(left, [], "part processes still running after the script ended: " + text)
            self.assertTrue(self.slot_free(), "the suite slot is still held after the script ended: " + text)
        finally:
            for p in pids + [proc.pid]:
                try:
                    g = os.getpgid(p)
                    if g != os.getpgrp():
                        os.killpg(g, signal.SIGKILL)
                    os.kill(p, signal.SIGKILL)
                except OSError:
                    pass
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_r8c_a_run_whose_slot_step_is_killed_stops_its_parts(self):
        # t-079 I3 (t-072 RL-1): the slot step (pa_suite.py slot, run-all.sh's parent under the slot) killed with
        # SIGKILL frees the slot (the kernel drops its flock); the run under it must not go on without the slot: it
        # stops every part and their children, and says why. In parallel and in PA_SERIAL=1 mode.
        for serial in ("", "1"):
            with self.subTest(serial=serial):
                pids_file = os.path.join(self.tmp, "keeper.pids")
                if os.path.exists(pids_file):
                    os.remove(pids_file)
                log = os.path.join(self.tmp, "orphan.out")
                with open(log, "w") as out:
                    proc = subprocess.Popen(["sh", os.path.join(self.repo, "tests", "run-all.sh")], cwd=self.tmp,
                                            env=self.env_for(PA_SERIAL=serial, keeper="child"),
                                            stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                            start_new_session=True)
                pids = []
                try:
                    end = time.time() + 30
                    while not os.path.exists(pids_file) and time.time() < end:
                        time.sleep(0.05)
                    with open(pids_file) as f:
                        pids = [int(p) for p in f.read().split()]  # the part's shell and the child it started
                    os.kill(proc.pid, signal.SIGKILL)  # run-all.sh exec'd the slot step: this pid is the step
                    proc.wait()
                    end = time.time() + 20
                    while time.time() < end and any(alive(p) for p in pids):
                        time.sleep(0.1)
                    left = [p for p in pids if alive(p)]
                    end = time.time() + 5
                    while time.time() < end and "=== interrupted" not in pa_text(log):
                        time.sleep(0.1)
                    text = pa_text(log)
                    self.assertEqual(left, [], "part processes still running after the slot step was killed: " + text)
                    self.assertIn("run-all.sh: the slot step (pid {}) is gone: stopping the parts".format(proc.pid), text)
                    self.assertIn("=== interrupted (outputs so far: ", text)
                finally:
                    for p in pids:
                        try:
                            g = os.getpgid(p)
                            if g != os.getpgrp():
                                os.killpg(g, signal.SIGKILL)
                            os.kill(p, signal.SIGKILL)
                        except OSError:
                            pass
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)  # the session it started: run-all.sh and its watchdog
                    except OSError:
                        pass

    def test_r9_one_part_per_unit_module(self):
        # Each tests/test_*.py is its own part, run once, all at once; a failing module is named in the last 40
        # lines (what pa-full-suite prints) and the run exits 1.
        write(os.path.join(self.repo, "tests", "test_a.py"), UNIT_MODULE.format(me="a", other="b"))
        write(os.path.join(self.repo, "tests", "test_b.py"), UNIT_MODULE.format(me="b", other="a"))
        code, out = self.run_all(b="fail")
        self.assertEqual(code, 1, out)
        with open(os.path.join(self.tmp, "ran")) as f:
            ran = sorted(f.read().split())
        self.assertEqual(ran, ["a.test_x", "a.test_y", "b.test_x", "b.test_y"], out)
        parts = ["unit tests: test_a", "unit tests: test_b", "unit tests: test_stub"]
        body = out.split("\n=== summary", 1)[0]
        at = [body.index("=== " + name + "\n") for name in parts]
        self.assertEqual(at, sorted(at), body)
        self.assertLess(body.index("=== python 3.7 parse\n"), at[0])
        self.assertLess(at[-1], body.index("=== keeper harness\n"))
        tail = "\n".join(out.rstrip().splitlines()[-40:])
        self.assertIn("--- unit tests: test_b: FAILED", tail)
        self.assertIn("FAIL: test_x (test_b.T", tail)
        for name in parts[:1] + parts[2:]:
            self.assertIn("  {}: ok".format(name), tail)
        self.assertTrue(out.rstrip().splitlines()[-1].startswith("=== 1 part(s) failed: unit tests: test_b (outputs: "), out)
        folder = self.outputs(out)
        self.assertEqual(len([f for f in os.listdir(folder) if f.endswith(".log")]), 13, os.listdir(folder))

    def sharded_modules(self):
        """Stub test_pa_queue.py (nine tests, a class fixture) and test_pa_worker.py (five) with the real
        tests/unit_shards.py hook, so the table in run-all.sh shards them."""
        shutil.copy(os.path.join(HERE, "unit_shards.py"), os.path.join(self.repo, "tests"))
        for module, count in (("test_pa_queue", 9), ("test_pa_worker", 5)):
            write(os.path.join(self.repo, "tests", module + ".py"), SHARDED.format(me=module, tests="".join(
                "    def test_{0}(self): mark('{1}.test_{0}')\n".format(i, module) for i in range(1, count + 1))))
        return ["{}.test_{}".format(m, i) for m, c in (("test_pa_queue", 9), ("test_pa_worker", 5)) for i in range(1, c + 1)]

    def test_r11_long_modules_run_in_shards(self):
        # t-078: test_pa_queue runs as four parts and test_pa_worker as two, each with its own PA_UNIT_SHARD
        # (unit_shards.py's every n-th test), named "<module> k/n" in order; each test runs exactly once, each
        # shard sets up its class fixture; a failing shard is named with its failing test.
        everything = self.sharded_modules()
        code, out = self.run_all(PA_UNIT_SHARD="9/9")  # an outer value is not a part's
        self.assertEqual(code, 0, out)
        with open(os.path.join(self.tmp, "ran")) as f:
            ran = f.read().split("\n")
        tests = sorted(l for l in ran if ".test_" in l)
        self.assertEqual(tests, sorted(everything), out)
        fixtures = sorted(l for l in ran if l.startswith("class "))
        self.assertEqual(fixtures, sorted(["class test_pa_queue {}/4".format(k) for k in (1, 2, 3, 4)] +
                                          ["class test_pa_worker {}/2".format(k) for k in (1, 2)]), out)
        parts = (["unit tests: test_pa_queue {}/4".format(k) for k in (1, 2, 3, 4)] +
                 ["unit tests: test_pa_worker {}/2".format(k) for k in (1, 2)])
        body = out.split("\n=== summary", 1)[0]
        at = [body.index("=== " + name + "\n") for name in parts]
        self.assertEqual(at, sorted(at), body)
        self.assertNotIn("=== unit tests: test_pa_queue\n", body)
        self.assertNotIn("=== unit tests: test_pa_worker\n", body)
        for name in parts + ["unit tests: test_stub"]:
            self.assertIn("  {}: ok".format(name), self.summary(out))
        self.assertIn("Ran 3 tests", body.split("=== unit tests: test_pa_queue 1/4\n", 1)[1].split("\n=== ", 1)[0])
        os.remove(os.path.join(self.tmp, "ran"))
        code, out = self.run_all(fail="test_pa_queue.test_6")  # test_6 is in shard 2/4 (index 5)
        self.assertEqual(code, 1, out)
        tail = "\n".join(out.rstrip().splitlines()[-40:])
        self.assertIn("--- unit tests: test_pa_queue 2/4: FAILED", tail)
        self.assertIn("FAIL: test_6 (test_pa_queue.T", tail)
        self.assertTrue(out.rstrip().splitlines()[-1].startswith("=== 1 part(s) failed: unit tests: test_pa_queue 2/4 (outputs: "), out)

    def test_r6e_affected_fixture_module_runs_the_unit_tests(self):
        # A change to a test helper that is not a test_*.py module (queue_fixtures.py, push_fixtures.py,
        # unit_shards.py) runs the unit parts, the shards among them.
        self.sharded_modules()
        for changed in ("tests/queue_fixtures.py", "tests/push_fixtures.py", "tests/unit_shards.py"):
            with self.subTest(changed=changed):
                code, out = self.run_all("--affected", PA_CHANGED_FILES=changed)
                self.assertEqual(code, 0, out)
                for name in ("unit tests: test_stub", "unit tests: test_pa_queue 3/4", "unit tests: test_pa_worker 2/2"):
                    self.assertIn("  {}: ok".format(name), self.summary(out))
                self.assertNotIn("keeper harness", out)

    def test_r7_usage(self):
        code, out = self.run_all("--nope")
        self.assertEqual(code, 2, out)

    # ---- t-089 J2 (R12): the parts by place: --except-integration on a machine, --only-integration on the Mac ----

    def test_r12a_except_integration_runs_every_part_but_the_integration_one(self):
        code, out = self.run_all("--except-integration", PA_CHANGED_FILES="tests/keeper/run.mjs")
        self.assertEqual(code, 0, out)
        tail = self.summary(out)
        names = [l.split(": ok", 1)[0].strip() for l in tail.splitlines() if l.startswith("  ") and ": ok (" in l]
        self.assertEqual(names, NAMES[:-3])  # the same names and order; PA_CHANGED_FILES does not narrow it
        with open(self.events) as f:
            self.assertNotIn("integration", f.read())
        self.assertIn("  suite slot: waited", tail)

    def test_r12b_only_integration_runs_only_it(self):
        code, out = self.run_all("--only-integration")
        self.assertEqual(code, 0, out)
        tail = self.summary(out)
        names = [l.split(": ok", 1)[0].strip() for l in tail.splitlines() if l.startswith("  ") and ": ok (" in l]
        self.assertEqual(names, INTEGRATION)
        with open(self.events) as f:
            events = f.read().split()
        self.assertEqual(sorted(zip(events[::2], events[1::2])), sorted(
            (e, "integration{}".format(k)) for k in (1, 2, 3) for e in ("start", "end")), events)
        with open(os.path.join(self.tmp, "integrationargs")) as f:
            args = sorted(f.read().splitlines())
        self.assertEqual(sorted(a.split(" ", 1)[1] for a in args), ["--part 1", "--part 2", "--part 3"], args)
        self.assertTrue(all(a.split(" ", 1)[0].startswith("/tmp/t001.") for a in args), args)
        self.assertEqual(len(set(a.split(" ", 1)[0] for a in args)), 3, args)  # its own scratch per part
        self.assertIn("  suite slot: waited", tail)
        self.assertEqual(self.progress()[-1]["parts"]["total"], 3)

    def test_r12d_a_failing_integration_part_is_named_alone(self):
        # t-112 R6: a failing step makes only its part FAILED, its FAIL lines in the summary; the other parts ok.
        for serial in ("", "1"):
            with self.subTest(serial=serial):
                write(self.events, "")
                code, out = self.run_all("--only-integration", integration2="fail", PA_SERIAL=serial)
                self.assertEqual(code, 1, out)
                tail = self.summary(out)
                self.assertIn("--- pa-session integration 2/3 (private hc core): FAILED\n  FAIL K-x: boom\n  FAIL K-y: bang", tail)
                self.assertIn("  pa-session integration 1/3 (private hc core): ok (", tail)
                self.assertIn("  pa-session integration 2/3 (private hc core): FAILED (", tail)
                self.assertIn("  pa-session integration 3/3 (private hc core): ok (", tail)
                self.assertTrue(out.rstrip().splitlines()[-1].startswith(
                    "=== 1 part(s) failed: pa-session integration 2/3 (private hc core) (outputs: "), out)
        # A part that cannot start its core (SKIP, exit 2) is FAILED with its last 40 lines, the others ok.
        write(os.path.join(self.repo, "tests", "pa_session_check.sh"),
              '[ "$3" = 3 ] && { echo "SKIP: scratch core did not start"; exit 2; }\n' + CHECK)
        code, out = self.run_all("--only-integration")
        self.assertEqual(code, 1, out)
        tail = self.summary(out)
        self.assertIn("--- pa-session integration 3/3 (private hc core): FAILED (no failing test named; its last 40 lines:)\n"
                      "  SKIP: scratch core did not start", tail)
        for k in (1, 2):
            self.assertIn("  pa-session integration {}/3 (private hc core): ok (".format(k), tail)

    def test_r12c_the_place_flags_take_no_other_argument(self):
        for args in (("--except-integration", "x"), ("--only-integration", "--affected"), ("--except-integration", "--only-integration"),
                     ("--part",), ("--part", "1"), ("--only-integration=2",), ("--only-integration", "--part", "2")):
            code, out = self.run_all(*args)
            self.assertEqual(code, 2, (args, out))
            self.assertIn("usage", out)

    # ---- t-072: the suite slot (pa_suite.py's flock slots under HERDR_PA_HOME/suite-slots) ----

    def holder(self, *command, **stubs):
        """A process holding slot-0 (running command under it when given), once it holds it."""
        script = os.path.join(self.tmp, "holder.py")
        write(script, HOLDER)
        ready = os.path.join(self.tmp, "ready")
        if os.path.exists(ready):
            os.remove(ready)
        p = subprocess.Popen([sys.executable, script, self.slot, ready] + list(command), cwd=self.tmp,
                             env=self.env_for(**stubs), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             universal_newlines=True)
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.wait(), p.stdout.close()))
        end = time.time() + 20
        while not os.path.exists(ready) and time.time() < end:
            time.sleep(0.05)
        self.assertTrue(os.path.exists(ready), "the holder never took the slot")
        return p

    def slot_free(self):
        os.makedirs(os.path.dirname(self.slot), exist_ok=True)
        with open(self.slot, "a+") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return False
            fcntl.flock(f, fcntl.LOCK_UN)
        return True

    def waited(self, out):
        m = re.search(r"^  suite slot: waited ([0-9.]+)s ", self.summary(out), re.M)
        self.assertTrue(m, out)
        return float(m.group(1))

    def test_s1_a_run_waits_for_the_holder_says_for_whom_and_records_the_wait(self):
        # Full and --affected alike: the run waits while another holds the slot, says so naming the holder, and
        # starts once it frees; the summary and the run folder carry the seconds waited.
        for args in ((), ("--affected",)):
            with self.subTest(args=args):
                p = self.holder()
                log = os.path.join(self.tmp, "s1.out")
                with open(log, "w") as f:
                    run = subprocess.Popen(["sh", os.path.join(self.repo, "tests", "run-all.sh")] + list(args),
                                           cwd=self.tmp, env=self.env_for(), stdin=subprocess.DEVNULL, stdout=f,
                                           stderr=subprocess.STDOUT)
                try:
                    time.sleep(1.5)
                    self.assertIsNone(run.poll(), pa_text(log))
                    with open(self.events) as f:
                        self.assertEqual(f.read(), "", "a part started while the slot was held")
                    text = pa_text(log)
                    self.assertIn("waiting for the suite slot", text)
                    self.assertIn("t-holder", text)
                    self.assertIn(str(p.pid), text)
                    p.kill()
                    p.wait()
                    code = run.wait(timeout=60)
                finally:
                    if run.poll() is None:
                        run.kill()
                        run.wait()
                out = pa_text(log)
                self.assertEqual(code, 0, out)
                self.assertGreaterEqual(self.waited(out), 1.0, out)
                with open(os.path.join(self.outputs(out), "slot")) as f:
                    self.assertIn("waited", f.read())
                self.assertTrue(self.slot_free())
                write(self.events, "")

    def test_s2_a_dead_holder_never_blocks(self):
        p = self.holder()
        p.kill()
        p.wait()
        code, out = self.run_all("--affected")
        self.assertEqual(code, 0, out)
        self.assertNotIn("waiting for the suite slot", out)
        self.assertLess(self.waited(out), 1.0, out)

    def test_s3_a_run_that_selects_no_part_takes_no_slot(self):
        self.holder()
        began = time.time()
        code, out = self.run_all("--affected", PA_CHANGED_FILES="README.md", PA_SUITE_WAIT_LIMIT="5")
        self.assertEqual((code, out.strip()), (0, "no tested files changed: nothing to run"))
        self.assertLess(time.time() - began, 4)

    def test_s4_a_run_under_the_slot_holder_does_not_wait_for_itself(self):
        # pa-full-suite holds the slot and runs run-all.sh: the run sees its caller holds it and goes on.
        p = self.holder("sh", os.path.join(self.repo, "tests", "run-all.sh"), "--affected", PA_SUITE_WAIT_LIMIT="5")
        out, _ = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 0, out)
        self.assertNotIn("waiting for the suite slot", out)
        self.assertIn("  suite slot: held by the caller", self.summary(out))

    def test_s5_a_stale_marker_does_not_skip_the_slot(self):
        # A PA_SUITE_SLOT that is not this run's caller's (the holder is no ancestor) is not trusted.
        self.holder()
        code, out = self.run_all("--affected", PA_SUITE_SLOT=self.slot, PA_SUITE_SLOT_PID="1", PA_SUITE_WAIT_LIMIT="1")
        self.assertEqual(code, 124, out)
        self.assertIn("waiting for the suite slot", out)
        self.assertIn("no slot came free", out)
        with open(self.events) as f:
            self.assertEqual(f.read(), "")

    def test_s6_the_slot_is_held_while_the_parts_run_and_freed_after(self):
        for serial in ("", "1"):
            with self.subTest(serial=serial):
                code, out = self.run_all("--affected", keeper="probe", PA_SERIAL=serial)
                self.assertEqual(code, 0, out)
                self.assertIn("  keeper harness: ok", self.summary(out))
                self.assertTrue(self.slot_free())

    def test_s7_without_pa_suite_the_run_goes_on_and_says_so(self):
        os.remove(os.path.join(self.repo, "runtime", "tools", "pa_suite.py"))
        code, out = self.run_all("--affected")
        self.assertEqual(code, 0, out)
        self.assertIn("  suite slot: none", self.summary(out))

    def test_s8_a_caller_holding_the_slot_without_a_marker_is_not_waited_for(self):
        # t-072 round 2 (MF-1): a pa-full-suite from before t-072 holds the slot (its pid in the holder line) and
        # sets no PA_SUITE_SLOT. The run under it sees its caller holds the slot and goes on, not waiting for itself.
        p = self.holder("sh", os.path.join(self.repo, "tests", "run-all.sh"), "--affected", PA_SUITE_WAIT_LIMIT="5",
                        NO_MARKER="1")
        out, _ = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 0, out)
        self.assertNotIn("waiting for the suite slot", out)
        self.assertIn("  suite slot: held by the caller (task t-holder, pid {}, slot-0)".format(p.pid), self.summary(out))

    def test_s9_a_wait_never_outlasts_its_caller(self):
        # The default wait is under the shortest caller timeout (the affected-tests hook's 900 s run), and a run
        # whose caller is gone (killed, or timed out by it) stops waiting.
        self.holder()
        launcher = os.path.join(self.tmp, "launcher.py")
        write(launcher, "import subprocess, sys, time\n"
                        "p = subprocess.Popen(sys.argv[2:], stdout=open(sys.argv[1], 'w'), stderr=subprocess.STDOUT)\n"
                        "print(p.pid, flush=True)\ntime.sleep(600)\n")
        log = os.path.join(self.tmp, "s9.out")
        caller = subprocess.Popen([sys.executable, launcher, log, "sh", os.path.join(self.repo, "tests", "run-all.sh"),
                                   "--affected"], cwd=self.tmp, env=self.env_for(), stdout=subprocess.PIPE,
                                  universal_newlines=True)
        self.addCleanup(lambda: (caller.poll() is None and caller.kill(), caller.wait(), caller.stdout.close()))
        run = int(caller.stdout.readline())
        self.addCleanup(lambda: alive(run) and os.kill(run, signal.SIGKILL))
        end = time.time() + 20
        while "waiting for the suite slot" not in pa_text(log) and time.time() < end:
            time.sleep(0.05)
        self.assertIn("waiting for the suite slot", pa_text(log))
        caller.kill()
        caller.wait()
        end = time.time() + 15
        while alive(run) and time.time() < end:
            time.sleep(0.1)
        self.assertFalse(alive(run), "the run went on waiting after its caller was gone: " + pa_text(log))
        self.assertIn("caller is gone", pa_text(log))
        with open(self.events) as f:
            self.assertEqual(f.read(), "")
        spec = importlib.util.spec_from_file_location("pa_suite_s9", os.path.join(self.repo, "runtime", "tools", "pa_suite.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertLess(module.SLOT_WAIT_LIMIT, 900)

    def test_s10_a_run_past_its_run_limit_after_the_slot_is_ended_and_said_not_finished(self):
        # t-079 I2 (t-072 RL-5): the affected-tests hook gives the run its own limit, counted from when the slot is
        # taken (PA_SUITE_RUN_LIMIT): the slot step ends a run on this Mac past it (its parts stopped), its last line
        # `run-all.sh: did not finish on this Mac: ...` (the hook reads that as a failure to fix), exit 124. The wait
        # before it does not count.
        p = self.holder()
        log = os.path.join(self.tmp, "s10.out")
        pids_file = os.path.join(self.tmp, "keeper.pids")
        with open(log, "w") as f:
            run = subprocess.Popen(["sh", os.path.join(self.repo, "tests", "run-all.sh"), "--affected"], cwd=self.tmp,
                                   env=self.env_for(keeper="child", PA_SUITE_RUN_LIMIT="4"), stdin=subprocess.DEVNULL,
                                   stdout=f, stderr=subprocess.STDOUT, start_new_session=True)
        pids = []
        try:
            end = time.time() + 20
            while "waiting for the suite slot" not in pa_text(log) and time.time() < end:
                time.sleep(0.05)
            time.sleep(5)  # longer than the run limit, waiting: not counted
            self.assertIsNone(run.poll(), pa_text(log))
            p.kill()
            p.wait()
            end = time.time() + 30
            while not os.path.exists(pids_file) and time.time() < end:
                time.sleep(0.05)
            with open(pids_file) as f:
                pids = [int(x) for x in f.read().split()]
            took = time.time()
            code = run.wait(timeout=60)
            took = time.time() - took
            end = time.time() + 5
            while time.time() < end and any(alive(x) for x in pids):
                time.sleep(0.05)
            text = pa_text(log)
            self.assertEqual(code, 124, text)
            self.assertLess(took, 20, text)
            self.assertEqual([x for x in pids if alive(x)], [], text)
            last = text.rstrip().splitlines()[-1]
            self.assertTrue(last.startswith("run-all.sh: did not finish on this Mac: "), last)
            self.assertIn("4 s", last)
            self.assertTrue(self.slot_free())
        finally:
            for x in pids:
                try:
                    os.killpg(os.getpgid(x), signal.SIGKILL)
                except OSError:
                    pass
            if run.poll() is None:
                run.kill()
                run.wait()

    def test_s11_the_run_limit_is_not_passed_on_to_the_run(self):
        # t-079 I2: PA_SUITE_RUN_LIMIT is the slot step's own; a run started under it (a nested run-all.sh) gets none
        code, out = self.run_all("--affected", keeper="envs", PA_SUITE_RUN_LIMIT="300")
        self.assertEqual(code, 0, out)
        with open(os.path.join(self.tmp, "keeper.runenv")) as f:
            self.assertEqual(f.read().strip(), "", out)

    def test_s12_the_slot_steps_deadline_and_local_run_are_not_the_parts(self):
        # t-117 R16 (Nit-J2-2): the second pass (under the slot step) unsets PA_SUITE_DEADLINE and PA_SUITE_LOCAL_RUN
        # before any part starts; the first pass keeps them for the slot step (its deadline still applies: s5)
        code, out = self.run_all("--affected", keeper="envs", PA_SUITE_DEADLINE=str(int(time.time()) + 3600),
                                 PA_SUITE_LOCAL_RUN="0123456789ab")
        self.assertEqual(code, 0, out)
        self.assertIn("  keeper harness: ok", self.summary(out))
        self.assertEqual(pa_text(os.path.join(self.tmp, "keeper.slotenv")), "", out)

    # ---- t-076: the per-part limit, the progress file, --module ----

    def progress(self):
        """The progress files of this home, oldest first."""
        rows = []
        for path in sorted(glob.glob(os.path.join(self.home, "suite-runs", "*.json"))):
            with open(path) as f:
                rows.append(json.load(f))
        return sorted(rows, key=lambda r: r.get("queued") or "")

    def until(self, test, limit=30):
        end = time.time() + limit
        while time.time() < end:
            got = test()
            if got:
                return got
            time.sleep(0.1)
        return None

    def record_medians(self, seconds):
        """A suites.jsonl in the space of a task marked in the repo copy: keeper harness took these seconds."""
        write(os.path.join(self.repo, ".claude", "pa-task.json"), json.dumps({"task": "t-1", "space": "sp1"}))
        rows = [{"run": "r{}".format(i), "repo": self.repo, "exit": 0,
                 "parts": [{"name": "keeper harness", "ok": True, "seconds": s}]} for i, s in enumerate(seconds)]
        write(os.path.join(self.home, "spaces", "sp1", "content", "suites.jsonl"), "".join(json.dumps(r) + "\n" for r in rows))

    def test_h1_a_part_past_its_limit_is_stopped_and_named_hung(self):
        # The hung part and its children are stopped, the other parts finish, the run exits 1 and names it.
        for serial in ("", "1"):
            with self.subTest(serial=serial):
                pids = os.path.join(self.tmp, "keeper.pids")
                if os.path.exists(pids):
                    os.remove(pids)
                began = time.time()
                code, out = self.run_all(keeper="child", PA_PART_LIMIT="4", PA_SERIAL=serial)
                self.assertLess(time.time() - began, 60, out)
                self.assertEqual(code, 1, out)
                tail = self.summary(out)
                self.assertIn("--- keeper harness: HUNG: stopped after ", tail)
                self.assertIn("  keeper harness: HUNG (", tail)
                for name in NAMES:
                    if name != "keeper harness":
                        self.assertIn("  {}: ok".format(name), tail)
                self.assertTrue(out.rstrip().splitlines()[-1].startswith(
                    "=== 1 part(s) failed: keeper harness (hung) (outputs: "), out)
                with open(pids) as f:
                    left = [int(p) for p in f.read().split() if alive(int(p))]
                self.assertEqual(left, [], "the hung part's processes are still running")
                self.assertTrue(self.slot_free())
                write(self.events, "")

    def test_h2_the_limit_is_three_times_the_recorded_median(self):
        spec = importlib.util.spec_from_file_location("pa_suite_h2", os.path.join(self.repo, "runtime", "tools", "pa_suite.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.part_limit(None, 900), 900)
        self.assertEqual(module.part_limit(100, 900), 900)
        self.assertEqual(module.part_limit(400, 900), 1200)
        self.assertEqual(module.PART_FLOOR, 900)
        self.record_medians([1, 1, 2])  # median 1: limit 3 s over a floor of 1 s
        code, out = self.run_all("--affected", PA_CHANGED_FILES="tests/keeper/run.mjs", keeper="sleep:8", PA_PART_LIMIT="1")
        self.assertEqual(code, 1, out)
        self.assertIn("--- keeper harness: HUNG: stopped after ", out)
        self.assertIn("limit 3s", out)
        self.record_medians([10, 10, 10])  # median 10: limit 30 s, the part's 3 s are fine
        code, out = self.run_all("--affected", PA_CHANGED_FILES="tests/keeper/run.mjs", keeper="sleep:3", PA_PART_LIMIT="1")
        self.assertEqual(code, 0, out)
        self.assertNotIn("HUNG", out)

    def test_h3_hung_parts_parse_as_failed_and_hung(self):
        spec = importlib.util.spec_from_file_location("pa_suite_h3", os.path.join(self.repo, "runtime", "tools", "pa_suite.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.parse_parts("  keeper harness: HUNG (12s)\n  store harness: ok (1s)\n"), [
            {"name": "keeper harness", "ok": False, "seconds": 12, "hung": True},
            {"name": "store harness", "ok": True, "seconds": 1}])

    def test_p1_every_run_has_a_progress_file(self):
        # Full and --affected: running (parts done of total, the parts running now and their last output), then done.
        for args, total in (((), 11), (("--affected",), 8)):
            with self.subTest(args=args):
                shutil.rmtree(os.path.join(self.home, "suite-runs"), ignore_errors=True)
                log = os.path.join(self.tmp, "p1.out")
                with open(log, "w") as f:
                    run = subprocess.Popen(["sh", os.path.join(self.repo, "tests", "run-all.sh")] + list(args),
                                           cwd=self.tmp, env=self.env_for(keeper="sleep:4"), stdin=subprocess.DEVNULL,
                                           stdout=f, stderr=subprocess.STDOUT)
                try:
                    row = self.until(lambda: [r for r in self.progress() if r.get("state") == "running"
                                              and (r.get("parts") or {}).get("total") == total
                                              and [p for p in r["parts"].get("running", []) if p["name"] == "keeper harness"]
                                              and r["parts"].get("done", 0) >= 1])
                    self.assertTrue(row, (self.progress(), pa_text(log)))
                    row = row[-1]
                    self.assertEqual(row["kind"], "run-all.sh")
                    self.assertEqual(row["dir"], self.repo)
                    self.assertLess(row["parts"]["done"], total)
                    keeper = [p for p in row["parts"]["running"] if p["name"] == "keeper harness"][0]
                    for k in ("since", "last_output", "limit"):
                        self.assertIn(k, keeper)
                    self.assertTrue(row.get("folder") and os.path.isdir(row["folder"]), row)
                    self.assertEqual(run.wait(timeout=60), 0, pa_text(log))
                finally:
                    if run.poll() is None:
                        run.kill()
                        run.wait()
                done = self.progress()[-1]
                self.assertEqual((done["state"], done["exit"], done["parts"]["done"], done["parts"]["total"]),
                                 ("done", 0, total, total), done)
                self.assertTrue(done.get("start") and done.get("end"), done)
                write(self.events, "")

    def test_p2_a_waiting_run_says_for_whom_in_its_progress_file(self):
        p = self.holder()
        log = os.path.join(self.tmp, "p2.out")
        with open(log, "w") as f:
            run = subprocess.Popen(["sh", os.path.join(self.repo, "tests", "run-all.sh"), "--affected"], cwd=self.tmp,
                                   env=self.env_for(), stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT)
        try:
            # t-165: the row says "waiting" first and for whom in a later write (once it read the holders): wait for that
            row = self.until(lambda: [r for r in self.progress() if r.get("state") == "waiting" and "waiting_for" in r])
            self.assertTrue(row, (self.progress(), pa_text(log)))
            self.assertIn("t-holder", row[-1]["waiting_for"])
            self.assertIn(str(p.pid), row[-1]["waiting_for"])
            p.kill()
            p.wait()
            self.assertEqual(run.wait(timeout=60), 0, pa_text(log))
        finally:
            if run.poll() is None:
                run.kill()
                run.wait()
        self.assertEqual(self.progress()[-1]["state"], "done")

    def test_m1_module_runs_one_unit_part_under_the_slot(self):
        write(os.path.join(self.repo, "tests", "test_a.py"), UNIT_MODULE.format(me="a", other="a"))
        code, out = self.run_all("--module", "test_stub")
        self.assertEqual(code, 0, out)
        tail = self.summary(out)
        self.assertIn("  unit tests: test_stub: ok", tail)
        self.assertNotIn("test_a", out)
        for name in NAMES:
            if name != "unit tests: test_stub":
                self.assertNotIn("  {}: ok".format(name), tail)
        self.assertIn("  suite slot: waited", tail)
        with open(self.events) as f:
            self.assertEqual(f.read(), "")
        self.assertEqual(self.progress()[-1]["parts"]["total"], 1)
        code, out = self.run_all("--module", "test_nope")
        self.assertEqual(code, 2, out)
        code, out = self.run_all("--module")
        self.assertEqual(code, 2, out)

    # t-093 (t-062 F1): on a Mac /usr/bin/git is xcrun's front end; run-all.sh asks xcrun once for the real git and
    # puts a folder holding only a link named git to it (and links to its prefix's other folders) first on PATH for
    # the parts. Here a fake xcrun first on PATH names a fake prefix, whose bin also holds a python3 that must not
    # shadow the caller's. Without xcrun, or when it fails or names no <prefix>/bin/git, PATH is unchanged.
    def git_run(self, xcrun="ok", *args, git_mode=0o755, path=None, **stubs):
        fake = os.path.join(self.tmp, "fakebin")
        write(os.path.join(fake, "xcrun"), '#!/bin/sh\necho "$*" >> "$STUB_DIR/xcrun-calls"\n'
              'case "$STUB_xcrun:$*" in\n  ok:"--find git") echo "$STUB_DIR/prefix/bin/git" ;;\n'
              '  odd:*) echo "$STUB_DIR/prefix/git" ;;\n  *) echo "xcrun: error: no such tool" >&2; exit 1 ;;\nesac\n', 0o755)
        for name in ("git", "python3"):
            write(os.path.join(self.tmp, "prefix", "bin", name), "#!/bin/sh\necho fake {}\n".format(name),
                  git_mode if name == "git" else 0o755)
        write(os.path.join(self.tmp, "prefix", "git"), "#!/bin/sh\necho odd git\n", 0o755)
        os.makedirs(os.path.join(self.tmp, "prefix", "libexec", "git-core"))
        path = path or fake + os.pathsep + os.environ["PATH"]
        env = self.env_for(xcrun=xcrun, **stubs)
        env["PATH"] = path
        r = subprocess.run(["sh", os.path.join(self.repo, "tests", "run-all.sh")] + list(args), cwd=self.tmp, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout)
        seen = {}
        for part in ("keeper", "integration1", "integration2", "integration3"):
            if os.path.exists(os.path.join(self.tmp, part + ".git")):
                with open(os.path.join(self.tmp, part + ".git")) as f:
                    seen[part] = dict(line.rstrip("\n").split("=", 1) for line in f)
        return path, r.stdout, seen

    def test_g1_the_parts_get_the_real_git_named_git_and_nothing_else_of_its_folder(self):
        path, out, seen = self.git_run("ok", keeper="git", integration2="git")
        folder = self.outputs(out)
        self.assertEqual(sorted(seen), ["integration2", "keeper"], out)
        for part, s in seen.items():
            with self.subTest(part=part):
                self.assertEqual(os.path.basename(s["git"]), "git")  # pa_guard's name check sees "git"
                self.assertTrue(s["git"].startswith(folder + os.sep), s["git"])
                self.assertEqual(os.path.realpath(s["git"]), os.path.join(self.tmp, "prefix", "bin", "git"))
                libexec = os.path.join(os.path.dirname(os.path.dirname(s["git"])), "libexec")
                self.assertEqual(os.path.realpath(libexec), os.path.join(self.tmp, "prefix", "libexec"))
                self.assertEqual(s["path"], os.path.dirname(s["git"]) + os.pathsep + path)
                self.assertEqual(s["python3"], shutil.which("python3", path=path) or "")
                self.assertEqual(os.listdir(os.path.dirname(s["git"])), ["git"])

    def test_g2_when_xcrun_fails_the_path_is_unchanged(self):
        path, out, seen = self.git_run("fail", keeper="git", integration2="git")
        self.assertEqual(sorted(seen), ["integration2", "keeper"], out)
        for part, s in seen.items():
            with self.subTest(part=part):
                self.assertEqual(s["path"], path)
                self.assertEqual(s["git"], shutil.which("git", path=path) or "")

    def test_g3_when_xcrun_names_no_prefix_bin_git_the_path_is_unchanged(self):
        path, out, seen = self.git_run("odd", keeper="git")
        self.assertEqual(seen["keeper"]["path"], path, out)

    def test_g5_when_xcrun_names_a_git_that_cannot_run_the_path_is_unchanged(self):
        # t-079 I8 (t-102, t-093 REVIEW R1: mutant m4, the `-x "$real"` check dropped): a <prefix>/bin/git of mode
        # 0644 is not linked and PATH is as it was
        path, out, seen = self.git_run("ok", keeper="git", integration2="git", git_mode=0o644)
        self.assertEqual(sorted(seen), ["integration2", "keeper"], out)
        for part, s in seen.items():
            with self.subTest(part=part):
                self.assertEqual(s["path"], path)
        self.assertFalse(os.path.exists(os.path.join(self.outputs(out), "git")), out)

    def test_g6_without_xcrun_on_path_the_path_is_unchanged(self):
        # t-079 I8 (t-102, t-093 REVIEW R2): a PATH of links to the tools run-all.sh, its stubs and pa_suite.py use,
        # and no xcrun (on a Mac /usr/bin holds one, so /usr/bin is not on it): PATH is as it was, no git link made
        tools = os.path.join(self.tmp, "tools")
        os.makedirs(tools)
        for name in ("sh", "dirname", "basename", "mktemp", "cat", "date", "mkdir", "tail", "head", "grep", "cut",
                     "sed", "wc", "tr", "sleep", "mv", "rm", "ps", "ln", "ls", "sort", "env", "printf", "true",
                     "uname", "touch"):
            found = shutil.which(name)
            if found:
                os.symlink(found, os.path.join(tools, name))
        os.symlink(sys.executable, os.path.join(tools, "python3"))
        self.assertIsNone(shutil.which("xcrun", path=tools))
        path, out, seen = self.git_run("ok", keeper="git", integration2="git", path=tools)
        self.assertEqual(sorted(seen), ["integration2", "keeper"], out)
        for part, s in seen.items():
            with self.subTest(part=part):
                self.assertEqual(s["path"], tools)
        self.assertFalse(os.path.exists(os.path.join(self.outputs(out), "git")), out)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "xcrun-calls")), out)

    def test_g4_xcrun_is_asked_once_per_run(self):
        self.git_run("ok")
        with open(os.path.join(self.tmp, "xcrun-calls")) as f:
            self.assertEqual(f.read(), "--find git\n")



# ---- t-112: the integration check's three parts (R9: the table, read from the script's text, never run; §10: the
# real script refuses a bad --part or scratch before anything is written and before any hc call) ----

CHECK_SCRIPT = os.path.join(HERE, "pa_session_check.sh")
SETUP_SCRATCH = os.path.join(HERE, "setup-scratch.sh")
REAP_SCRIPT = os.path.join(HERE, "pa_session_reap.sh")
STEP_NAME = r"(?:S[1-9][0-9]*[a-z]?|R)"
CONTROLS = ["S9", "R"]  # the per-core controls, last in every part (R4)


def table_problems(text):
    """What is wrong with pa_session_check.sh's part table, read from its text: [] when nothing."""
    problems = []
    m = re.search(r"^part_table='\n(.*?)^'$", text, re.M | re.S)
    if not m:
        return ["no part_table='...' block"]
    parts = {}
    for line in m.group(1).splitlines():
        if not line.strip():
            continue
        lm = re.match(r"^\s*([0-9]+)\s*:(.*)$", line)
        if not lm:
            problems.append("cannot parse the table line {!r}".format(line))
            continue
        k, names = lm.group(1), lm.group(2).split()
        for n in names:
            if not re.fullmatch(STEP_NAME, n):
                problems.append("part {}: {!r} is not a step name".format(k, n))
        if k in parts:
            problems.append("part {} listed twice".format(k))
        parts[k] = names
    if sorted(parts) != ["1", "2", "3"]:
        problems.append("parts {} (want 1, 2, 3)".format(sorted(parts)))
    defined = re.findall(r"^step_([A-Za-z0-9_]+)\(\) \{", text, re.M)
    for f in defined:
        if not re.fullmatch(STEP_NAME, f):
            problems.append("step_{} is not a step name".format(f))
    if len(set(defined)) != len(defined):
        problems.append("a step_ function defined twice: {}".format(defined))
    for k, names in sorted(parts.items()):
        if not [n for n in names if n not in CONTROLS]:
            problems.append("part {} has no steps".format(k))
        if names[-2:] != CONTROLS or names.count("S9") != 1 or names.count("R") != 1:
            problems.append("part {} does not end with S9 R once: {}".format(k, names))
        for n in names:
            if n not in defined:
                problems.append("part {}: {} has no step_{} function".format(k, n, n))
    for f in defined:
        if f in CONTROLS:
            continue
        where = [k for k, names in sorted(parts.items()) for n in names if n == f]
        if len(where) != 1:
            problems.append("step {} is in {} parts ({})".format(f, len(where), where))
    am = re.search(r"^steps_all=\"([^\"]*)\"$", text, re.M)
    if not am:
        problems.append("no steps_all=\"...\" line")
        return problems
    whole = am.group(1).split()
    if whole != defined:
        problems.append("steps_all {} is not the step_ functions in their order {}".format(whole, defined))
    for k, names in sorted(parts.items()):
        at = [whole.index(n) for n in names if n in whole]
        if at != sorted(at):
            problems.append("part {} is not in the whole run's order: {}".format(k, names))
    return problems


# A pa_suite.py in front of the real one (t-107 R13): it writes the PA_SUITE_HC_* and HC_* names it got, then runs the
# real one with the same arguments.
SLOT_SPY = """import os, sys
with open(os.path.join(os.environ["STUB_DIR"], "slot.env"), "a") as f:
    f.write("".join("%s=%s\\n" % kv for kv in sorted(os.environ.items()) if kv[0].startswith(("HC_", "PA_SUITE_HC_"))))
os.execv(sys.executable, [sys.executable, {real!r}] + sys.argv[1:])
"""
HC_GIVEN = {"HC_TERMINAL_ID": "w9", "HC_SOCKET": "/x/sock", "HC_TERMINAL_SOCKET": "/x/t", "HC_SCOPE": "s1"}


class Fixture(unittest.TestCase):
    """RunAll's fixture and helpers without its tests (a subclass of RunAll would run them all again)."""
    setUp, tearDown, env_for, summary = RunAll.setUp, RunAll.tearDown, RunAll.env_for, RunAll.summary

    def run_with(self, env, *args):
        r = subprocess.run(["sh", os.path.join(self.repo, "tests", "run-all.sh")] + list(args), cwd=self.tmp, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=120)
        return r.returncode, r.stdout


class HcForTheSlotStep(Fixture):
    """t-107 R13: run-all.sh keeps the hc variables for its slot step only (as PA_SUITE_HC_*); no part sees them."""

    def test_h1_the_slot_step_gets_them_and_no_part_does(self):
        write(os.path.join(self.repo, "runtime", "tools", "pa_suite.py"),
              SLOT_SPY.format(real=os.path.join(HERE, "..", "runtime", "tools", "pa_suite.py")))
        env = self.env_for(keeper="envs", PA_SUITE_HC_SCOPE="inherited")
        env.update(HC_GIVEN)
        code, out = self.run_with(env, "--affected")
        self.assertEqual(code, 0, out)
        self.assertIn("  keeper harness: ok", self.summary(out))
        self.assertEqual(pa_text(os.path.join(self.tmp, "keeper.env")), "", "a part saw hc variables")
        got = dict(l.split("=", 1) for l in pa_text(os.path.join(self.tmp, "slot.env")).splitlines() if l)
        self.assertEqual(got, {"PA_SUITE_" + k: v for k, v in HC_GIVEN.items()})

    def test_h2_without_them_the_slot_step_gets_none(self):
        write(os.path.join(self.repo, "runtime", "tools", "pa_suite.py"),
              SLOT_SPY.format(real=os.path.join(HERE, "..", "runtime", "tools", "pa_suite.py")))
        env = {k: v for k, v in self.env_for().items() if not k.startswith(("HC_", "PA_SUITE_HC_"))}
        code, out = self.run_with(env, "--affected")
        self.assertEqual(code, 0, out)
        self.assertEqual(pa_text(os.path.join(self.tmp, "slot.env")), "")


class RemoteAffected(Fixture):
    """t-107 R12 end to end: the real run-all.sh and slot step in a git fixture with box-1 configured (hc is
    tests/stub-hc: the machine is a folder here). --affected runs there, its output keeps run-all.sh's shape, the
    run there does not ship again, the slot step's hc calls carry the hc variables and its parts do not."""

    def setUp(self):
        super().setUp()
        for name in ("pa_run", "pa_hooks"):
            shutil.copy(os.path.join(HERE, "..", "runtime", "tools", name + ".py"), os.path.join(self.repo, "runtime", "tools"))
        for argv in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.com"], ["config", "user.name", "T"],
                     ["add", "-A"], ["commit", "-q", "-m", "fixture"]):
            subprocess.run(["git", "-C", self.repo] + argv, check=True, stdout=subprocess.DEVNULL)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        os.symlink(os.path.join(HERE, "stub-hc"), os.path.join(self.bin, "hc"))
        os.symlink(self.node, os.path.join(self.bin, "node"))
        write(os.path.join(self.home, "config.json"), json.dumps({"suiteMachines": [{"machine": "box-1"}]}))
        self.stub = os.path.join(self.tmp, "stub")
        self.hclog = os.path.join(self.tmp, "hc.log")
        self.envlog = os.path.join(self.tmp, "hc-env.log")

    def remote_env(self, **stubs):
        env = {k: v for k, v in self.env_for(**stubs).items() if not k.startswith(("HC_", "PA_SUITE_HC_"))}
        env.update(HC_GIVEN, PATH=self.bin + os.pathsep + env.get("PATH", ""), STUB_HC_ROOT=self.stub,
                   STUB_HC_LOG=self.hclog, STUB_HC_ENVLOG=self.envlog, STUB_HC_MACHINES="box-1",
                   PA_SUITE_REMOTE_POLL="0.2")
        return env

    def test_e1_affected_runs_there_in_run_all_shape(self):
        code, out = self.run_with(self.remote_env(keeper="envs"), "--affected")
        self.assertEqual(code, 0, out)
        lines = out.rstrip().splitlines()
        self.assertRegex(lines[0], r"^run-all\.sh: on box-1 \(\S+\)$", out)
        self.assertRegex(lines[-1], r"^=== all passed \(outputs: ", out)
        summary = self.summary(out)
        for name in ("python 3.7 parse", "unit tests: test_stub", "keeper harness", "store harness"):
            self.assertIn("  {}: ok".format(name), summary)
        self.assertNotIn("pa-session integration", out)
        calls = [json.loads(l) for l in pa_text(self.hclog).splitlines() if l.strip()]
        self.assertEqual(len([c for c in calls if c[0] == "run"]), 1, calls)  # PA_SUITE_REMOTE=1 there: no second ship
        envs = [json.loads(l)["env"] for l in pa_text(self.envlog).splitlines() if l.strip()]
        self.assertTrue(envs)
        for e in envs:
            self.assertEqual(e, HC_GIVEN)
        self.assertEqual(pa_text(os.path.join(self.tmp, "keeper.env")), "", "a part there saw hc variables")
        self.assertEqual(os.listdir(os.path.join(self.stub, "box-1", "tmp")), [], "the run folder there is removed")

    def test_e2_a_full_run_by_hand_stays_here(self):
        code, out = self.run_with(self.remote_env(), "--except-integration")
        self.assertEqual(code, 0, out)
        self.assertNotIn("run-all.sh: on box-1", out)
        self.assertFalse(os.path.exists(self.hclog) and pa_text(self.hclog), "no hc call")


class HcScopeNotInherited(RemoteAffected):
    """t-117 R19 (Nit-J2r3-1, X11): an inherited PA_SUITE_HC_SCOPE without HC_SCOPE is dropped by run-all.sh's
    unset line: the slot step's hc calls (on a machine, STUB_HC_ENVLOG) and the parts there have no HC_SCOPE."""
    test_e1_affected_runs_there_in_run_all_shape = test_e2_a_full_run_by_hand_stays_here = None  # RemoteAffected's

    def test_h3_an_inherited_scope_without_hc_scope_reaches_no_hc_call(self):
        env = self.remote_env(keeper="envs")
        env.pop("HC_SCOPE")
        env["PA_SUITE_HC_SCOPE"] = "inherited"
        code, out = self.run_with(env, "--affected")
        self.assertEqual(code, 0, out)
        self.assertRegex(out.splitlines()[0], r"^run-all\.sh: on box-1 ", out)
        envs = [json.loads(l)["env"] for l in pa_text(self.envlog).splitlines() if l.strip()]
        self.assertTrue(envs)
        want = {k: v for k, v in HC_GIVEN.items() if k != "HC_SCOPE"}
        for e in envs:
            self.assertEqual(e, want)
        self.assertEqual(pa_text(os.path.join(self.tmp, "keeper.env")), "", "a part there saw hc variables")


class PartTable(unittest.TestCase):
    def setUp(self):
        with open(CHECK_SCRIPT) as f:
            self.text = f.read()

    def table(self, rows):
        start = self.text.index("part_table='\n")
        end = self.text.index("\n'\n", start)
        return self.text[:start] + "part_table='\n" + rows + self.text[end:]

    def test_t1_the_table_puts_every_step_in_one_part_and_the_controls_in_each(self):
        self.assertEqual(table_problems(self.text), [])
        m = re.search(r"^part_table='\n(.*?)^'$", self.text, re.M | re.S)
        rows = [l.split(":", 1) for l in m.group(1).splitlines() if l.strip()]
        self.assertEqual({k.strip(): v.split() for k, v in rows}, {
            "1": "S1 S2 S5 S6a S7 S3 S4 S12 S9 R".split(),
            "2": "S8 S10 S11 S14 S9 R".split(),
            "3": "S13 S13d S9 R".split()})  # spec R1
        self.assertIn('steps_all="S1 S2 S5 S6a S7 S3 S4 S8 S10 S11 S12 S13 S13d S14 S9 R"', self.text)

    def test_t2_each_fault_is_found(self):
        good = ("1: S1 S2 S5 S6a S7 S3 S4 S12 S9 R\n2: S8 S10 S11 S14 S9 R\n3: S13 S13d S9 R")
        self.assertEqual(table_problems(self.table(good)), [])
        self.assertEqual(table_problems(self.table("  1 :  S1  S2 S5\tS6a S7 S3 S4 S12 S9 R \n2:S8 S10 S11 S14 S9 R\n"
                                                   "3: S13 S13d S9 R")), [])  # other spacing is accepted
        cases = {
            "a step with no function": good.replace("S14 S9", "S14 S15 S9"),
            "a function in no part": good.replace(" S12 S9", " S9"),
            "a step in two parts": good.replace("S13 S13d", "S12 S13 S13d"),
            "a part with no steps": good.replace("3: S13 S13d S9 R", "3: S9 R").replace("S12 S9", "S12 S13 S13d S9"),
            "a part with nothing": good.replace("3: S13 S13d S9 R", "3:").replace("S12 S9", "S12 S13 S13d S9"),
            "a lower-case name": good.replace("S1 S2", "s1 S2"),
            "a zero-padded name": good.replace("S1 S2", "S01 S2"),
            "a line it cannot parse": good + "\nfour: S1",
            "a part listed twice": good + "\n3: S9 R",
            "no part 3": good.replace("\n3: S13 S13d S9 R", "").replace("S12 S9", "S12 S13 S13d S9"),
            "a part without R": good.replace("S13d S9 R", "S13d S9"),
            "the controls not last": good.replace("S13 S13d S9 R", "S9 R S13 S13d"),
            "out of the whole run's order": good.replace("S8 S10", "S10 S8"),
        }
        for why, rows in cases.items():
            with self.subTest(why=why):
                self.assertNotEqual(table_problems(self.table(rows)), [], rows)
        extra = self.table(good).replace("\nstep_S9() {", "\nstep_S99() {\n  :\n}\nstep_S9() {", 1)
        self.assertTrue([p for p in table_problems(extra) if "S99" in p], table_problems(extra))
        odd = self.table(good).replace("\nstep_S9() {", "\nstep_s9x() {\n  :\n}\nstep_S9() {", 1)
        self.assertTrue([p for p in table_problems(odd) if "s9x" in p], table_problems(odd))
        self.assertNotEqual(table_problems(self.text.replace("part_table='", "parts='", 1)), [])

    def test_t3_s8s_state_is_one_helper_s8_calls(self):
        # R3: part 3 starts S13 from S8's state through the same helper S8 calls, so the two cannot drift.
        self.assertEqual(len(re.findall(r"^s8_state\(\) \{", self.text, re.M)), 1)
        body = self.text.split("\nstep_S8() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("s8_state", body)
        self.assertNotIn('echo "MARKER-T037', body)  # the marker is written by s8_state only


def function_bodies(text):
    """name -> body of every top-level shell function `name() {` ... `}` in the text."""
    return dict(re.findall(r"^([A-Za-z0-9_]+)\(\) \{\n(.*?)^\}$", text, re.M | re.S))


def folder_problems(text):
    """t-112 R3: a part makes every project folder ($S/proj*) its steps use: setup-scratch.sh makes $S/proj, a step
    of the part makes one, or a replay (replays="<step>:<the step that makes it today>:<helper>") runs before a
    step whose maker is in another part."""
    problems = []
    fns = function_bodies(text)
    m = re.search(r"^part_table='\n(.*?)^'$", text, re.M | re.S)
    parts = {l.split(":", 1)[0].strip(): l.split(":", 1)[1].split() for l in m.group(1).splitlines() if l.strip()}
    rm = re.search(r"^replays=\"([^\"]*)\"$", text, re.M)
    if not rm:
        return ["no replays=\"...\" line"]
    replays = [r.split(":") for r in rm.group(1).split()]
    for r in replays:
        if len(r) != 3 or r[2] not in fns or "step_" + r[0] not in fns or "step_" + r[1] not in fns:
            problems.append("replay {} names a missing step or helper".format(r))
        elif r[2] not in fns["step_" + r[1]]:
            problems.append("replay {}: step_{} does not call {}".format(r, r[1], r[2]))

    def made(body):
        return {f for line in body.splitlines() if line.strip().startswith("mkdir ")
                for f in re.findall(r'"\$S/(proj[A-Za-z0-9]*)"', line)}

    def used(body):
        return set(re.findall(r'"\$S/(proj[A-Za-z0-9]*)"', body))

    for k, steps in sorted(parts.items()):
        have = {"proj"}
        for s in steps:
            for step, src, helper in [r for r in replays if len(r) == 3]:
                if step == s and src not in steps and helper in fns:
                    have |= made(fns[helper])
            body = fns.get("step_" + s, "")
            for name in re.findall(r"^\s*([a-z0-9_]+)\s*$", body, re.M):  # a helper the step calls on its own line
                if name in fns:
                    have |= made(fns[name])
            have |= made(body)
            missing = used(body) - have
            if missing:
                problems.append("part {}: {} uses {} that no step before it in the part makes".format(k, s, sorted(missing)))
    return problems


class PartState(unittest.TestCase):
    def setUp(self):
        with open(CHECK_SCRIPT) as f:
            self.text = f.read()

    def test_t4_each_part_makes_the_project_folders_its_steps_use(self):
        # The cloud run of c3523e1: part 1's S12 found no $S/proj12 (S11, in part 2, made it): "no launcher.json".
        self.assertEqual(folder_problems(self.text), [])
        m = re.search(r'^replays="([^"]*)"$', self.text, re.M)
        self.assertEqual(sorted(m.group(1).split()), ["S12:S11:s11_folders", "S13:S8:s8_state"])
        no_replay = self.text.replace(m.group(0), 'replays="S13:S8:s8_state"')
        self.assertTrue([p for p in folder_problems(no_replay) if "S12" in p and "proj12" in p], folder_problems(no_replay))

    def runner(self, text, steps):
        """The script's runner loop (from `ran=" "` to its `done`) run by sh with its replays line and stub step_
        and helper functions that print their names: the lines it printed (t-123, t-112 VERIFY L1)."""
        loop = re.search(r'^ran=" "\n.*?^done$', text, re.M | re.S).group(0)
        replays = re.search(r'^replays="[^"]*"$', text, re.M).group(0)
        helpers = sorted({r.split(":")[2] for r in replays.split('"')[1].split()})
        names = sorted(set(re.findall(r"^step_([A-Za-z0-9]+)\(\) \{", text, re.M)))
        script = "\n".join(["set -u", replays] + ["{0}() {{ echo 'helper {0}'; }}".format(h) for h in helpers]
                           + ["step_{0}() {{ echo 'step {0}'; }}".format(n) for n in names]
                           + ["steps='{}'".format(steps), loop, ""])
        r = subprocess.run(["sh", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout)
        return r.stdout.splitlines()

    def test_t5_the_runner_replays_a_maker_from_another_part_only(self):
        parts = dict((l.split(":", 1)[0].strip(), l.split(":", 1)[1].strip()) for l in
                     re.search(r"^part_table='\n(.*?)^'$", self.text, re.M | re.S).group(1).splitlines() if l.strip())
        whole = re.search(r'^steps_all="([^"]*)"$', self.text, re.M).group(1)
        for name, steps, before in (("part 1", parts["1"], {"S12": "s11_folders"}), ("part 2", parts["2"], {}),
                                    ("part 3", parts["3"], {"S13": "s8_state"}), ("whole run", whole, {}),
                                    ("maker first", "S11 S12 S8 S13", {})):
            with self.subTest(run=name):
                want = []
                for step in steps.split():
                    if step in before:
                        want.append("helper " + before[step])
                    want.append("step " + step)
                got = self.runner(self.text, steps)
                self.assertEqual([l for l in got if l.startswith(("helper ", "step "))], want, got)
                for step, helper in before.items():
                    self.assertIn("{} starts from {}'s state (t-112 R3: {})".format(
                        step, "S11" if step == "S12" else "S8", helper), got)


class CheckArgs(unittest.TestCase):
    """The real pa_session_check.sh with a scratch that has the two files it looks for, an hc first on PATH that
    only records its calls (and fails, so a run that gets past the arguments stops at the gate: SKIP)."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="t112-args-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.s = os.path.join(self.tmp, "s")
        self.calls = os.path.join(self.tmp, "hc-calls")
        self.bin = os.path.join(self.tmp, "fakebin")
        hc = '#!/bin/sh\necho "$HC_SESSION $*" >> "{}"\nexit 1\n'.format(self.calls)
        for d in (os.path.join(self.s, "bin"), self.bin):
            write(os.path.join(d, "hc"), hc, 0o755)
        for name in ("pa-session", "stub-claude"):
            write(os.path.join(self.s, "bin", name), "#!/bin/sh\nexit 0\n", 0o755)

    def run_check(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("HC_", "CLAUDE"))}
        env["PATH"] = self.bin + os.pathsep + env.get("PATH", "")
        r = subprocess.run(["sh", CHECK_SCRIPT] + list(args), cwd=self.tmp, env=env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=60)
        return r.returncode, r.stdout

    def tree(self):
        seen = {}
        for root, dirs, files in os.walk(self.tmp):
            for n in dirs + files:
                p = os.path.join(root, n)
                st = os.lstat(p)
                seen[p] = (st.st_size, st.st_mtime_ns)
        return seen

    def refused(self, args, why="usage"):
        before = self.tree()
        code, out = self.run_check(*args)
        self.assertEqual(code, 2, (args, out))
        self.assertIn(why, out, args)
        self.assertNotIn("scratch core", out, args)
        self.assertFalse(os.path.exists(self.calls), (args, out))  # no hc call: no core started
        self.assertEqual(self.tree(), before, args)  # nothing written

    def test_a1_a_bad_part_is_refused_before_anything(self):
        s = self.s
        for args in ([s, "--part", "0"], [s, "--part", "4"], [s, "--part", "-1"], [s, "--part", "01"],
                     [s, "--part", "+1"], [s, "--part", "1.0"], [s, "--part", " 1"], [s, "--part", "1x"],
                     [s, "--part"], [s, "--part", ""], [s, "--part=1"], [s, "--part", "--part", "1"],
                     [s, "--part", "-x"], [s, "--part", "1", "--part", "2"], [s, "--part", "1", "extra"],
                     [s, "1"], [s, "-x", "1"], [s, "extra"]):
            with self.subTest(args=args[1:]):
                self.refused(args)

    def test_a2_a_bad_scratch_is_refused_before_anything(self):
        os.symlink("/usr", os.path.join(self.tmp, "out"))  # a folder outside the three roots
        for args in (["--part", "1", self.s], ["/tmp/../etc", "--part", "1"], [self.tmp + "/s/../../../etc"],
                     ["/tmp/../etc"], [os.path.join(self.tmp, "out")], [os.path.join(self.tmp, "out"), "--part", "2"],
                     ["t001.x"], ["-x"]):
            with self.subTest(args=args):
                self.refused(args, "not a temp folder")
        for args in ([], [""], ["", "--part", "1"]):
            with self.subTest(args=args):
                self.refused(args)

    def test_a4_setup_scratch_and_the_reaper_check_the_resolved_folder(self):
        # t-123 (t-112 REVIEW 2): the Q7 checks, as given and once resolved, before anything is written.
        os.symlink("/usr", os.path.join(self.tmp, "out"))
        os.makedirs(self.s, exist_ok=True)
        for tool, extra in ((SETUP_SCRATCH, []), (REAP_SCRIPT, ["list", None, "pa-t001-x", "", ""])):
            for bad in ("/tmp/../etc", os.path.join(self.tmp, "out"), os.path.join(self.tmp, "out", "lib")):
                with self.subTest(tool=os.path.basename(tool), folder=bad):
                    before = self.tree()
                    argv = [a if a is not None else bad for a in extra] if extra else [bad]
                    r = subprocess.run(["sh", tool] + argv, cwd=self.tmp, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, universal_newlines=True, timeout=120)
                    self.assertEqual(r.returncode, 2, r.stdout)
                    self.assertIn("is not a temp folder", r.stdout)
                    self.assertEqual(self.tree(), before)
        # the controls: the same tools take a real temp folder
        r = subprocess.run(["sh", REAP_SCRIPT, "list", self.s, "pa-t001-x", "", ""], cwd=self.tmp, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout)
        good = os.path.join(self.tmp, "good")
        os.makedirs(good)
        r = subprocess.run(["sh", SETUP_SCRATCH, good], cwd=self.tmp, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, universal_newlines=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("scratch ready: " + good, r.stdout)

    def test_a5_setup_scratch_names_a_folder_that_does_not_exist(self):
        # t-123 (t-112 REVIEW 1): the caller makes the folder (mktemp -d); a missing one is said as missing, not as
        # outside the temp roots, and nothing is made.
        missing = os.path.join(self.tmp, "not-made", "s")
        r = subprocess.run(["sh", SETUP_SCRATCH, missing], cwd=self.tmp, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, universal_newlines=True, timeout=60)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("refusing: {} does not exist (make it with mktemp -d)".format(missing), r.stdout)
        self.assertFalse(os.path.exists(os.path.dirname(missing)))

    def test_a3_a_good_part_reaches_the_gate_with_its_name(self):
        # The control: the same scratch with a good --part gets past the arguments, says its part and steps
        # first, and asks its own core (session pa-t001-p<k>-<pid>); the fake hc fails, so it stops there (SKIP).
        for args, first, session in (((self.s, "--part", "2"), "part 2/3: S8 S10 S11 S14 S9 R", r"pa-t001-p2-[0-9]+"),
                                     ((self.s + "/", "--part", "3"), "part 3/3: S13 S13d S9 R", r"pa-t001-p3-[0-9]+"),
                                     ((self.s, "--part", "1"), "part 1/3: S1 S2 S5 S6a S7 S3 S4 S12 S9 R", r"pa-t001-p1-[0-9]+"),
                                     ((self.s,), "whole run: S1 S2 S5 S6a S7 S3 S4 S8 S10 S11 S12 S13 S13d S14 S9 R",
                                      r"pa-t001-[0-9]+")):
            with self.subTest(args=args[1:]):
                if os.path.exists(self.calls):
                    os.remove(self.calls)
                code, out = self.run_check(*args)
                self.assertEqual(code, 2, out)
                self.assertEqual(out.splitlines()[0], first, out)
                self.assertIn("SKIP: scratch core did not start", out)
                with open(self.calls) as f:
                    calls = f.read().splitlines()
                self.assertTrue(calls and all(re.fullmatch(session + r" server (status --json|stop)", c) for c in calls), calls)


def pa_text(path):
    with open(path) as f:
        return f.read()


if __name__ == "__main__":
    unittest.main()
