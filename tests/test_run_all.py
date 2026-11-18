"""tests/run-all.sh itself (t-017 J6, R7): parts in parallel, output in a fixed order, a summary that names
every part and every failure, PA_SERIAL=1, --affected; J9: TERM, INT and HUP stop every part and its children,
and each unit test module (tests/test_*.py) is its own part; t-031: the panel harness runs as four parts, one per
shard (run.mjs --shard=<k>/4), each named in the summary; t-072: every run takes pa-full-suite's slot (s1-s7). The real script runs in a temp copy of the repo whose
parts are stubs (stub.sh, chosen per part by STUB_<part>), so no real suite runs here."""
import fcntl
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
NAMES = ["python 3.7 parse", "unit tests: test_stub", "keeper harness"] + SHARDS + ["store harness",
         "pa-session integration (private hc core)"]
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
        for name in ("pa", "pa_hooks", "pa_guard", "pa_costs", "pa_push", "pa_queue"):
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
        write(os.path.join(self.repo, "tests", "pa_session_check.sh"), "exec sh stub.sh integration\n")
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
        self.assertEqual(len([f for f in files if f.endswith(".log")]), 9, files)
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
        for name in ["python 3.7 parse", "store harness", "pa-session integration (private hc core)"] + SHARDS[:1] + SHARDS[2:]:
            self.assertIn("{}: ok".format(name), tail)
        self.outputs(out)

    def test_r2b_quiet_failure(self):
        code, out = self.run_all(store="quiet")
        self.assertEqual(code, 1, out)
        self.assertIn("--- store harness: FAILED (no failing test named; its last 40 lines:)", self.summary(out))
        self.assertTrue(out.rstrip().splitlines()[-1].startswith("=== 1 part(s) failed: store harness (outputs: "), out)

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
        self.assertEqual(pairs, [["start", k, "end", k] for k in ("keeper", "panel1", "panel2", "panel3", "panel4", "store", "integration")], events)
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
        for name in NAMES[:-1]:
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
                                                   "integration"]), seen)
            dirs = [os.path.realpath(d) for d in seen.values()]
            self.assertEqual(len(set(dirs)), 8, seen)
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
        self.assertEqual(len([f for f in os.listdir(folder) if f.endswith(".log")]), 11, os.listdir(folder))

    def test_r7_usage(self):
        code, out = self.run_all("--nope")
        self.assertEqual(code, 2, out)

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


def pa_text(path):
    with open(path) as f:
        return f.read()


if __name__ == "__main__":
    unittest.main()
