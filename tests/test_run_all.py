"""tests/run-all.sh itself (t-017 J6, R7): parts in parallel, output in a fixed order, a summary that names
every part and every failure, PA_SERIAL=1, --affected. The real script runs in a temp copy of the repo whose
parts are stubs (stub.sh, chosen per part by STUB_<part>), so no real suite runs here."""
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NAMES = ["python 3.7 parse", "unit tests", "keeper harness", "panel harness", "store harness",
         "pa-session integration (private hc core)"]

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


def write(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, mode)


class RunAll(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="t017-runall-"))
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "tests"))
        shutil.copy(os.path.join(HERE, "run-all.sh"), os.path.join(self.repo, "tests", "run-all.sh"))
        for name in ("pa", "pa_hooks", "pa_guard", "pa_costs", "pa_push"):
            write(os.path.join(self.repo, "runtime", "tools", name + ".py"), "x = 1\n")
        write(os.path.join(self.repo, "stub.sh"), STUB)
        write(os.path.join(self.repo, "tests", "test_stub.py"), UNITS)
        for part in ("keeper", "panel", "store"):
            write(os.path.join(self.repo, "tests", part, "run.mjs"), "exec sh stub.sh {}\n".format(part))
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
        self.assertEqual(len([f for f in files if f.endswith(".log")]), 6, files)
        keeper = [f for f in files if "keeper" in f and f.endswith(".log")][0]
        with open(os.path.join(folder, keeper)) as f:
            self.assertEqual(f.read(), "keeper says hello\nkeeper done\n")
        tail = self.summary(out)
        for name in NAMES:
            self.assertIn("{}: ok".format(name), tail)

    def test_r2_failures_named(self):
        code, out = self.run_all(units="fail", keeper="fail", panel="crash")
        self.assertEqual(code, 1, out)
        last = out.rstrip().splitlines()[-1]
        self.assertTrue(last.startswith("=== 3 part(s) failed: unit tests, keeper harness, panel harness (outputs: "), last)
        tail = self.summary(out)
        self.assertIn("--- unit tests: FAILED", tail)
        self.assertIn("FAIL: test_one", tail)
        self.assertIn("ERROR: test_two", tail)
        self.assertIn("FAIL K-x: boom", tail)
        self.assertIn("FAIL K-y: bang", tail)
        self.assertNotIn("PASS ok-case", tail)
        self.assertIn("--- panel harness: FAILED (no failing test named; its last 40 lines:)", tail)
        self.assertIn("panel line 11\n", tail)
        self.assertIn("panel line 50\n", tail)
        self.assertNotIn("panel line 10\n", tail)
        for name in ("python 3.7 parse", "store harness", "pa-session integration (private hc core)"):
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
        code, out = self.run_all(keeper="wait:panel", panel="wait:keeper")
        self.assertEqual(code, 0, out)

    def test_r4b_serial(self):
        code, out = self.run_all(PA_SERIAL="1")
        self.assertEqual(code, 0, out)
        with open(self.events) as f:
            events = f.read().split()
        pairs = [events[i:i + 4] for i in range(0, len(events), 4)]
        self.assertEqual(pairs, [["start", k, "end", k] for k in ("keeper", "panel", "store", "integration")], events)
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

    def test_r6b_affected_without_files_runs_the_fast_set(self):
        code, out = self.run_all("--affected")
        self.assertEqual(code, 0, out)
        for name in NAMES[:5]:
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
            self.assertEqual(sorted(seen), sorted(["units", "keeper", "panel", "store", "integration"]), seen)
            dirs = [os.path.realpath(d) for d in seen.values()]
            self.assertEqual(len(set(dirs)), 5, seen)
            for d in dirs:
                self.assertTrue(d.startswith(os.path.realpath(folder) + os.sep) and os.path.isdir(d), (d, folder))

    def test_r7_usage(self):
        code, out = self.run_all("--nope")
        self.assertEqual(code, 2, out)


if __name__ == "__main__":
    unittest.main()
