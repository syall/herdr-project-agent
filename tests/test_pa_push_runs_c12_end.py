"""pa-push-main's run checks C12, C13 and the deadline (t-007 part 7b, A3).

Run: python3 -m unittest -v tests/test_pa_push_runs_c12_end.py
The fixture (temp homes, the bare origin under a temp folder, the person-only check's injected environment) is
tests/push_fixtures.py; see its docstring. Split from tests/test_pa_push.py by t-030 so the parts run at once.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from push_fixtures import *  # noqa: F401,F403 (the shared fixture and helpers)
from push_fixtures import setUpModule, tearDownModule  # noqa: F401 (the module's temp homes and environment)

# ---- A3: the run's checks C12, C13 and the deadline ----------------------------

class RunChecks(Runs):
    def test_r_c12_seen_missing(self):
        self.assertRefused(self.run_push(seen=""), "C12", "--seen is required")

    def test_r_c12_seen_differs(self):
        self.assertRefused(self.run_push(seen="0" * 64), "C12", "the facts changed since you looked")
        # a re-run of the gate after the person looked changes the token too
        time.sleep(1.1)
        self.gate()
        self.assertRefused(self.run_push(), "C12", "the facts changed since you looked", attempts=2)

    def test_r_c13_prepush_fails(self):
        self.replan(prePush=["/bin/sh", "-c", "echo checking; echo bad thing >&2; exit 3"])
        # t-104 R9: run_push's temp folders go to this test's own folder, so another run's pa-push-* folders in the
        # machine-wide temp dir are never counted as this run's leftovers.
        own, saved = tempfile.mkdtemp(prefix="c13-"), tempfile.tempdir
        tempfile.tempdir = own
        try:
            r = self.assertRefused(self.run_push(), "C13", "the allowlist's prePush check failed (exit 3)")
            leftovers = [n for n in os.listdir(own) if n.startswith("pa-push-")]
        finally:
            tempfile.tempdir = saved
            shutil.rmtree(own, ignore_errors=True)
        self.assertIn("bad thing", r["refusal"]["message"])
        self.assertEqual(leftovers, [], "the temporary clone and hooks folder are removed")

    def test_r_deadline(self):
        r = self.assertRefused(self.run_push(deadline=time.monotonic() + 100), "deadline", "ran out of time before pushing; nothing was pushed")
        self.assertIn("nothing was pushed", r["message"])

if __name__ == "__main__":
    unittest.main()
