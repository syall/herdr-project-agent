"""tests/run-all.sh itself (t-017 J6, R7): parts in parallel, output in a fixed order, a summary that names
every part and every failure, PA_SERIAL=1, --affected; J9: TERM, INT and HUP stop every part and its children,
and each unit test module (tests/test_*.py) is its own part; t-031: the panel harness runs as four parts, one per
shard (run.mjs --shard=<k>/4), each named in the summary. The real script runs in a temp copy of the repo whose
parts are stubs (stub.sh, chosen per part by STUB_<part>), so no real suite runs here."""
import os
import shutil
import signal
import subprocess
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

    def run_all(self, *args, **stubs):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("STUB_", "PA_SERIAL", "PA_CHANGED"))}
        env.update({"NODE": self.node, "STUB_DIR": self.tmp, "TMPDIR": self.tmp})
        env.update({k if k.startswith("PA_") else "STUB_" + k: v for k, v in stubs.items()})
        r = subprocess.run(["sh", os.path.join(self.repo, "tests", "run-all.sh")] + list(args), cwd=self.tmp, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=120)
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
        env = {k: v for k, v in os.environ.items() if not k.startswith(("STUB_", "PA_SERIAL", "PA_CHANGED"))}
        env.update({"NODE": self.node, "STUB_DIR": self.tmp, "TMPDIR": self.tmp, "PA_SERIAL": serial})
        env.update({"STUB_" + k: "child" for k in hung})
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


if __name__ == "__main__":
    unittest.main()
