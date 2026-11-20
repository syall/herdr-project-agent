"""tests/unit_shards.py's load_tests (t-078): with PA_UNIT_SHARD=<k>/<n> a module runs every n-th of its tests in
loader order, so the n shards together run each test exactly once and a test added later lands in exactly one shard
without an edit; with no value the module runs all its tests; a malformed value is refused (the module errors, so
its part fails, never runs nothing). Every shard still sets up the class and module fixtures of the tests it holds.
The real sharded modules (tests/run-all.sh's unit_shards table) are only listed here, in their own processes with
their own temp folder: no case of theirs runs."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))

# Lists a module's test ids through the loader run-all.sh uses (discover -s tests -p <module>.py), running none.
LIST = r'''import sys, unittest
def flat(s):
    for t in s:
        if isinstance(t, unittest.TestSuite):
            yield from flat(t)
        else:
            yield t
for t in flat(unittest.TestLoader().discover(sys.argv[1], pattern=sys.argv[2] + ".py", top_level_dir=sys.argv[1])):
    print(t.id())
'''

# A module with class and module fixtures, eleven tests over three classes; each records what ran.
STUB = '''import os, unittest
from unit_shards import load_tests  # noqa: F401
D = os.environ["STUB_DIR"]
def mark(line):
    with open(os.path.join(D, "ran"), "a") as f:
        f.write(line + "\\n")
def setUpModule():
    mark("module " + os.environ.get("PA_UNIT_SHARD", "-"))
class A(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ready = True
        mark("class A " + os.environ.get("PA_UNIT_SHARD", "-"))
    def test_1(self): self.assertTrue(self.ready); mark("A.test_1")
    def test_2(self): self.assertTrue(self.ready); mark("A.test_2")
    def test_3(self): self.assertTrue(self.ready); mark("A.test_3")
    def test_4(self): self.assertTrue(self.ready); mark("A.test_4")
    def test_5(self): self.assertTrue(self.ready); mark("A.test_5")
class B(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ready = True
        mark("class B " + os.environ.get("PA_UNIT_SHARD", "-"))
    def test_1(self): self.assertTrue(self.ready); mark("B.test_1")
    def test_2(self): self.assertTrue(self.ready); mark("B.test_2")
    def test_3(self): self.assertTrue(self.ready); mark("B.test_3")
class C(unittest.TestCase):
    def test_1(self): mark("C.test_1")
    def test_2(self): mark("C.test_2")
    def test_3(self): mark("C.test_3")
'''
PLAIN = '''import unittest
class P(unittest.TestCase):
    def test_1(self): pass
    def test_2(self): pass
    def test_3(self): pass
'''
ALL = ["A.test_1", "A.test_2", "A.test_3", "A.test_4", "A.test_5", "B.test_1", "B.test_2", "B.test_3",
       "C.test_1", "C.test_2", "C.test_3"]


def table():
    """{module: shard count} from tests/run-all.sh's unit_shards line."""
    with open(os.path.join(HERE, "run-all.sh")) as f:
        found = re.search(r'^unit_shards="([^"]*)"', f.read(), re.M)
    if not found:
        raise AssertionError("tests/run-all.sh has no unit_shards table")
    return {m: int(n) for m, n in (item.split(":") for item in found.group(1).split())}


class Shards(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="t078-shards-"))
        self.tests = os.path.join(self.tmp, "tests")
        os.makedirs(self.tests)
        shutil.copy(os.path.join(HERE, "unit_shards.py"), self.tests)
        with open(os.path.join(self.tests, "test_stub.py"), "w") as f:
            f.write(STUB)
        with open(os.path.join(self.tests, "test_plain.py"), "w") as f:
            f.write(PLAIN)
        self.ran = os.path.join(self.tmp, "ran")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self, shard=None, **extra):
        env = {k: v for k, v in os.environ.items() if k != "PA_UNIT_SHARD"}
        env.update(STUB_DIR=self.tmp, TMPDIR=self.tmp, **extra)
        if shard is not None:
            env["PA_UNIT_SHARD"] = shard
        return env

    def run_module(self, tests, module, shard=None):
        return subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", tests, "-p", module + ".py", "-v"],
                              cwd=self.tmp, env=self.env(shard), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True, timeout=600)

    def listed(self, tests, module, shard=None):
        r = subprocess.run([sys.executable, "-c", LIST, tests, module], cwd=self.tmp, env=self.env(shard),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=600)
        self.assertEqual(r.returncode, 0, r.stderr)
        ids = r.stdout.split()
        self.assertFalse([i for i in ids if "_FailedTest" in i or "ModuleImportFailure" in i], r.stdout + r.stderr)
        return ids

    def marks(self):
        if not os.path.exists(self.ran):
            return []
        with open(self.ran) as f:
            lines = f.read().splitlines()
        os.remove(self.ran)
        return lines

    def test_u1_no_value_runs_every_test(self):
        r = self.run_module(self.tests, "test_stub")
        self.assertEqual(r.returncode, 0, r.stdout)
        got = self.marks()
        self.assertEqual(sorted(l for l in got if "." in l and " " not in l), ALL, got)
        self.assertEqual(got[0], "module -")

    def test_u2_shards_partition_the_tests_and_keep_their_fixtures(self):
        for n in (1, 2, 3, 4, 11):  # (more shards than tests: an empty shard, which unittest 3.12+ fails with exit 5)
            seen = []
            for k in range(1, n + 1):
                shard = "{}/{}".format(k, n)
                r = self.run_module(self.tests, "test_stub", shard)
                self.assertEqual(r.returncode, 0, shard + "\n" + r.stdout)
                got = self.marks()
                ran = [l for l in got if "." in l and " " not in l]
                # every n-th test in loader order (classes, then methods, by name), from the k-th
                self.assertEqual(ran, ALL[k - 1::n], shard)
                self.assertEqual(got[:1], ["module " + shard] if ran else [], shard)
                for cls in ("A", "B"):
                    held = any(t.startswith(cls + ".") for t in ran)
                    self.assertEqual(got.count("class {} {}".format(cls, shard)), 1 if held else 0, (shard, got))
                seen += ran
            self.assertEqual(sorted(seen), ALL, n)  # each test exactly once over the n shards

    def test_u3_malformed_value_is_refused(self):
        for bad in ("0/4", "5/4", "1/0", "x", "1/", "/4", "1/4/2", "-1/4", "a/b", " 1/4", "1 /4", ""):
            with self.subTest(bad=bad):
                r = self.run_module(self.tests, "test_stub", bad)
                if bad == "":
                    self.assertEqual(r.returncode, 0, r.stdout)  # an empty value is no value
                    self.assertEqual(len([l for l in self.marks() if "." in l and " " not in l]), len(ALL))
                    continue
                self.assertNotEqual(r.returncode, 0, r.stdout)
                self.assertIn("PA_UNIT_SHARD", r.stdout)
                self.assertEqual([l for l in self.marks() if "." in l and " " not in l], [], bad)

    def test_u4_a_module_without_the_hook_ignores_the_value(self):
        r = self.run_module(self.tests, "test_plain", "2/4")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("Ran 3 tests", r.stdout)

    def test_u5_real_modules_shard_into_their_whole_id_list(self):
        # Each module in run-all.sh's table: its n shards are disjoint, none is empty, and together they are the
        # module's whole id list (as listed with no shard).
        shards = table()
        self.assertEqual(sorted(shards), ["test_pa_queue", "test_pa_queue_merge", "test_pa_suite_remote", "test_pa_worker"])
        for module, n in sorted(shards.items()):
            with self.subTest(module=module):
                whole = self.listed(HERE, module)
                self.assertGreater(len(whole), n)
                parts = [self.listed(HERE, module, "{}/{}".format(k, n)) for k in range(1, n + 1)]
                for k, part in enumerate(parts, 1):
                    self.assertTrue(part, "{} shard {}/{} is empty".format(module, k, n))
                together = [i for part in parts for i in part]
                self.assertEqual(len(together), len(set(together)), module)
                self.assertEqual(sorted(together), sorted(whole), module)


if __name__ == "__main__":
    unittest.main()
