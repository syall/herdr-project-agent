"""pa-push-main's run checks C8 to C11 (t-007 part 7b, A3).

Run: python3 -m unittest -v tests/test_pa_push_runs_c8_c11.py
The fixture (temp homes, the bare origin under a temp folder, the person-only check's injected environment) is
tests/push_fixtures.py; see its docstring. Split from tests/test_pa_push.py by t-030 so the parts run at once.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from push_fixtures import *  # noqa: F401,F403 (the shared fixture and helpers)
from push_fixtures import setUpModule, tearDownModule  # noqa: F401 (the module's temp homes and environment)

# ---- A3: the run's checks C8 to C11 --------------------------------------------

class RunChecks(Runs):
    def test_r_c8a_evidence_removed(self):
        os.remove(os.path.join(self.dir, "evidence", TASK, "REVIEW.md"))
        self.assertRefused(self.run_push(), "C8a", "does not pass now: REVIEW.md (missing)")
        self.ledger_task(workers=[{"address": "t31", "role": "build"}])
        write(os.path.join(self.dir, "evidence", TASK, "REVIEW.md"), "x\n")
        self.assertRefused(self.run_push(), "C8a", "reviewer is not the author", attempts=2)

    def test_r_c8b_phase(self):
        self.gate(phase="build")
        self.assertRefused(self.run_push(), "C8b", "is from phase build")

    def test_r_c8c_no_pin(self):
        self.gate_edit(no_pin=True)
        self.assertRefused(self.run_push(), "C8c", "pins no commit for this repository")

    def test_r_c8d_uncommitted(self):
        self.gate_edit(uncommitted=2)
        self.assertRefused(self.run_push(), "C8d", "2 uncommitted file(s)")

    def test_r_c8e_pin_differs(self):
        self.gate_edit(commit=self.main_before)
        self.assertRefused(self.run_push(), "C8e", "the gate passed {}, the card says {}".format(self.main_before[:7], self.task_commit[:7]))

    def test_r_c8f_commit_after_gate(self):
        late = self.commit_on(self.wt, "late.txt", "after the gate")
        self.assertRefused(self.run_push(), "C8f", "commits were added to t-1-x after the gate (gate {}, branch now {})".format(
            self.task_commit[:7], late[:7]))

    def test_r_c8g_live_worktree_moved(self):
        sh(self.wt, "git", "checkout", "-q", "--detach", "HEAD~1")
        self.assertRefused(self.run_push(), "C8g", "not at the gated commit now")
        sh(self.wt, "git", "checkout", "-q", "t-1-x")
        write(os.path.join(self.wt, "b.txt"), "changed, not committed\n")
        self.assertRefused(self.run_push(), "C8g", "1 uncommitted change(s)", attempts=2)

    def test_r_c9_short_sha(self):
        """A short id never reaches git: with the gate pin and the card both short, the branch tip (always a full
        id) differs, so C8f refuses before C9 (C9 is the backstop)."""
        self.gate_edit(commit=self.task_commit[:7])
        self.card_edit(commit=self.task_commit[:7])
        self.assertRefused(self.run_push(), "C8f", "commits were added")
        self.assertFalse(any(self.task_commit[:7] in a for a in self.calls), self.calls)

    def test_r_c10a_local_moved(self):
        self.commit_on(self.main, "m.txt", "on main after the card")
        self.main_before = self.head(self.main)
        self.assertRefused(self.run_push(), "C10a", "local main moved since the card")

    def test_r_c10a_not_ancestor(self):
        x = self.commit_on(self.main, "m.txt", "diverged")
        self.main_before = x
        self.card_edit(localBefore=x)
        self.assertRefused(self.run_push(), "C10a", "local main ({}) is not an ancestor of {}".format(x[:7], self.task_commit[:7]))

    def test_r_c10b_not_on_branch(self):
        sh(self.main, "git", "checkout", "-q", "-b", "other")
        self.assertRefused(self.run_push(), "C10b", "is on other, not main")

    def test_r_c10b_dirty(self):
        write(os.path.join(self.main, "a.txt"), "dirty\n")
        self.assertRefused(self.run_push(), "C10b", "1 uncommitted change(s) to tracked files")

    def test_r_c11a_unreadable_url(self):
        moved = self.origin + ".away"
        os.rename(self.origin, moved)
        try:
            self.assertRefused(self.run_push(), "C11a", "could not read " + self.url, origin=False)
        finally:
            os.rename(moved, self.origin)
        self.assertEqual(self.origin_head(), self.origin_before)

    def test_r_c11a_no_branch(self):
        sh(self.origin, "git", "update-ref", "-d", "refs/heads/main")
        self.assertRefused(self.run_push(), "C11a", "has no branch main", origin=False)
        self.assertEqual(sh(self.origin, "git", "for-each-ref"), "")

    def test_r_c11b_remote_moved(self):
        other = os.path.join(self.tmp, "other")
        sh(self.tmp, "git", "clone", "-q", self.url, other)
        self.commit_on(other, "o.txt", "someone else")
        sh(other, "git", "push", "-q", "origin", "main")
        self.origin_before = self.origin_head()
        self.assertRefused(self.run_push(), "C11b", "remote main moved since the card")

    def test_r_c11c_rewritten(self):
        sh(self.main, "git", "checkout", "-q", "-b", "side", self.origin_before)
        x = self.commit_on(self.main, "s.txt", "rewritten history")
        sh(self.main, "git", "push", "-q", "origin", "side")
        sh(self.main, "git", "checkout", "-q", "main")
        sh(self.origin, "git", "update-ref", "refs/heads/main", x)
        self.origin_before = x
        self.card_edit(remoteBefore=x)
        self.assertRefused(self.run_push(), "C11c", "remote main ({}) is not an ancestor of {}; only a fast-forward".format(x[:7], self.task_commit[:7]))

if __name__ == "__main__":
    unittest.main()
