"""t-195 round 2 (VERIFY M1 and its class): tests/source_patch.py, which pa_session_check.sh's S11 uses to plant a bug
in a copy of pa.py, finds the function by name (any def line) and fails loudly ("patch did not apply", exit 3, the
file unchanged) when the patch cannot land, instead of leaving the copy as it was and the check to give a wrong
verdict.

Run: python3 -m unittest -v tests/test_source_patch.py
Temp folders only; nothing is run but the helper (as a program) and the patched functions (in-process, on a copy).
"""
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
HELPER = os.path.join(HERE, "source_patch.py")
S11 = 'if getattr({arg}, "restarted", None):\n    raise RuntimeError("S11 a bug only on the restarted path")'


class SourcePatch(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="t195-patch-")
        self.addCleanup(shutil.rmtree, self.dir)

    def file(self, text):
        path = os.path.join(self.dir, "x.py")
        with open(path, "w") as f:
            f.write(text)
        return path

    def run_helper(self, path, name="cmd_session", code=S11):
        return subprocess.run([sys.executable, HELPER, path, name, code], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=30)

    def patched_call(self, text, *args):
        path = self.file(text)
        r = self.run_helper(path)
        self.assertEqual(r.returncode, 0, r.stderr)
        mod = types.ModuleType("patched")
        with open(path) as f:
            exec(compile(f.read(), path, "exec"), mod.__dict__)
        return mod.cmd_session(*args)

    def test_the_installed_pa_py(self):  # S11's own target: the patch lands in today's cmd_session, whatever its args
        path = os.path.join(self.dir, "pa.py")
        shutil.copy(os.path.join(REPO, "runtime", "tools", "pa.py"), path)
        r = self.run_helper(path)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(path) as f:
            text = f.read()
        self.assertIn('raise RuntimeError("S11 a bug only on the restarted path")', text)
        compile(text, path, "exec")

    def test_any_def_line(self):
        restarted, plain = types.SimpleNamespace(restarted="r1"), types.SimpleNamespace(restarted=None)
        for src in ("def cmd_session(args):\n    return 'ran'\n",
                    "def cmd_session(args, ctx=None):\n    return 'ran'\n",
                    "def cmd_session(a,\n                ctx=None):\n    '''doc\n    more'''\n    return 'ran'\n",
                    "import os\n\n\ndef cmd_session(args, ctx=None):  # comment\n    \"\"\"doc\"\"\"\n    x = 1\n    return 'ran'\n",
                    "def cmd_session(args, /, ctx=None):\n        return 'ran'\n"):
            with self.subTest(src=src):
                self.assertEqual(self.patched_call(src, plain), "ran")
                with self.assertRaisesRegex(RuntimeError, "S11 a bug only on the restarted path"):
                    self.patched_call(src, restarted)

    def assert_not_applied(self, text, name="cmd_session", code=S11):
        path = self.file(text)
        r = self.run_helper(path, name, code)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("patch did not apply", r.stderr)
        with open(path) as f:
            self.assertEqual(f.read(), text, "the file was changed")

    def test_missing_function_fails_loudly(self):
        self.assert_not_applied("def cmd_start(args):\n    return 1\n")

    def test_two_functions_fail_loudly(self):
        self.assert_not_applied("def cmd_session(args):\n    return 1\n\n\ndef cmd_session(args):\n    return 2\n")

    def test_only_a_method_fails_loudly(self):
        self.assert_not_applied("class X:\n    def cmd_session(self, args):\n        return 1\n")

    def test_source_that_does_not_parse_fails_loudly(self):
        self.assert_not_applied("def broken(:\ndef cmd_session(args):\n    return 1\n")

    def test_body_on_the_def_line_fails_loudly(self):
        self.assert_not_applied("def cmd_session(args): return 1\n")

    def test_no_parameter_for_arg_fails_loudly(self):
        self.assert_not_applied("def cmd_session():\n    return 1\n")

    def test_bad_code_fails_loudly(self):
        self.assert_not_applied("def cmd_session(args):\n    return 1\n", code="if (:\n")

    def test_missing_file_fails_loudly(self):
        r = self.run_helper(os.path.join(self.dir, "nope.py"))
        self.assertEqual(r.returncode, 3)
        self.assertIn("patch did not apply", r.stderr)

    def test_the_session_check_uses_it(self):  # pa_session_check.sh S11: by name, and a miss is a failure of its own
        with open(os.path.join(HERE, "pa_session_check.sh")) as f:
            text = f.read()
        self.assertNotIn("t.replace('def cmd_session(args):", text)
        self.assertIn('python3 "$here/source_patch.py" "$S/pa/tools/pa.py" cmd_session', text)
        self.assertIn('fail S11 "patch did not apply', text)


if __name__ == "__main__":
    unittest.main()
