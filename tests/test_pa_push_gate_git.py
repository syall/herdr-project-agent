"""The gate's git in the task worktree is the tool's hardened git (t-007 part 7b, fix round 2).

Run: python3 -m unittest -v tests/test_pa_push_gate_git.py
The fixture (temp homes, the bare origin under a temp folder, the person-only check's injected environment) is
tests/push_fixtures.py; see its docstring. Split from tests/test_pa_push.py by t-030 so the parts run at once.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from push_fixtures import *  # noqa: F401,F403 (the shared fixture and helpers)
from push_fixtures import setUpModule, tearDownModule  # noqa: F401 (the module's temp homes and environment)

# ---- fix round 2: the gate's git in the task worktree is the tool's hardened git ----------------------

class GateGit(Runs):
    """The task worktree is the agent's: nothing its git settings name may run inside plan, check or run."""

    def program(self, name):
        self.mark = os.path.join(self.tmp, "RAN-" + name)
        prog = os.path.join(self.tmp, name + ".sh")
        write(prog, "#!/bin/sh\necho ran >> '{}'\ncat >/dev/null\nexit 1\n".format(self.mark))
        os.chmod(prog, 0o755)
        return prog

    def worktree_scope(self):
        """extensions.worktreeConfig on, as a Bash append would turn it on (not a risky key), with an empty main file."""
        sh(self.main, "git", "config", "--local", "extensions.worktreeConfig", "true")
        write(os.path.join(self.main, ".git", "config.worktree"), "")

    def assertNeverRan(self):
        self.assertFalse(os.path.exists(self.mark), "a program from the task worktree's git settings ran")

    def test_g01_worktree_fsmonitor_check_and_run(self):
        prog = self.program("fsmonitor")
        self.worktree_scope()
        sh(self.wt, "git", "config", "--worktree", "core.fsmonitor", prog)
        r = self.check()
        self.assertNeverRan()
        self.assertFalse(r["ok"])
        self.assertEqual(r["refusal"]["check"], "C8a", r["refusal"])
        self.assertIn("core.fsmonitor", r["refusal"]["message"])
        r = self.assertRefused(self.run_push(), "C8a", "core.fsmonitor")
        self.assertIn("worktree", r["refusal"]["message"])
        self.assertNeverRan()
        # and plan refuses a new card the same way
        os.remove(self.card_path())
        code, _o, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("core.fsmonitor", err)
        self.assertNeverRan()

    def test_g02_redirected_git_file(self):
        prog = self.program("redirect")
        other = os.path.join(self.tmp, "other")
        sh(self.tmp, "git", "init", "-q", other)
        sh(other, "git", "config", "core.fsmonitor", prog)
        write(os.path.join(self.wt, ".git"), "gitdir: {}/.git\n".format(other))
        self.assertRefused(self.run_push(), "C8a", "core.fsmonitor")
        self.assertNeverRan()

    def test_g03_worktree_filter(self):
        prog = self.program("filter")
        self.worktree_scope()
        sh(self.wt, "git", "config", "--worktree", "filter.planted.clean", prog)
        write(os.path.join(self.main, ".git", "info", "attributes"), "* filter=planted\n")
        later = time.time() + 5
        os.utime(os.path.join(self.wt, "b.txt"), (later, later))
        self.assertRefused(self.run_push(), "C8a", "filter.planted.clean")
        self.assertNeverRan()

    def test_g04_gate_reads_only_through_git_call(self):
        """plan, check and run evaluate the gate with git_call's git (hooks and fsmonitor off, no GIT_*), never pa_hooks.git."""
        def forbidden(*a, **k):
            raise AssertionError("pa_hooks.git ran in pa-push-main: {}".format(a))
        real = pa_push.pa_hooks.git
        pa_push.pa_hooks.git = forbidden
        try:
            os.remove(self.card_path())
            code, _o, err = self.plan()
            self.assertEqual(code, 0, err)
            self.calls[:] = []
            code, r = self.run_push(seen=FRESH)
        finally:
            pa_push.pa_hooks.git = real
        self.assertEqual(code, 0, r)
        self.assertEqual(self.origin_head(), self.task_commit)
        wt = os.path.realpath(self.wt)
        status = [a for a in self.calls if a[:3] == ["git", "-C", wt] and "status" in a]
        self.assertGreaterEqual(len(status), 2, self.calls)  # the gate's and the live pin's, in check and run
        self.assertTrue(all("core.fsmonitor=false" in a and any(x.startswith("core.hooksPath=") for x in a) for a in status), status)
        self.assertTrue(all("--ignore-submodules=all" in a for a in status), status)

    def nested_repo(self, commit=True):
        """A nested repository wt/sub (a submodule entry), committed or only staged in the task worktree."""
        sub = os.path.join(self.wt, "sub")
        os.makedirs(sub)
        sh(sub, "git", "init", "-q")
        write(os.path.join(sub, "s.txt"), "s\n")
        sh(sub, "git", "add", "s.txt")
        sh(sub, "git", "commit", "-q", "-m", "s")
        write(os.path.join(self.wt, ".gitmodules"), '[submodule "sub"]\n\tpath = sub\n\turl = ./sub\n')
        sh(self.wt, "git", "add", ".gitmodules", "sub")
        if commit:
            sh(self.wt, "git", "commit", "-q", "-m", "add sub")
        return sub

    def plant_in_nested(self, sub):
        """The agent names a program in the nested repository's own settings (no place C8a's settings read covers)."""
        prog = self.program("nested")
        sh(sub, "git", "config", "filter.planted.clean", prog)
        write(os.path.join(sub, ".git", "info", "attributes"), "* filter=planted\n")
        later = time.time() + 5
        os.utime(os.path.join(sub, "s.txt"), (later, later))

    def test_g05_nested_repository_committed(self):
        sub = self.nested_repo(commit=True)
        self.assertEqual(self.gate(), 0)
        self.plant_in_nested(sub)
        r = self.check()
        self.assertNeverRan()
        self.assertFalse(r["ok"])
        self.assertEqual(r["refusal"]["check"], "C8a", r["refusal"])
        self.assertIn("nested repository", r["refusal"]["message"])
        self.assertIn("sub", r["refusal"]["message"])
        self.assertIn("committed", r["refusal"]["message"])
        self.assertRefused(self.run_push(), "C8a", "nested repository")
        self.assertNeverRan()
        os.remove(self.card_path())
        code, _o, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("nested repository", err)
        self.assertNeverRan()

    def test_g06_nested_repository_staged_only(self):
        sub = self.nested_repo(commit=False)
        self.plant_in_nested(sub)
        r = self.check()
        self.assertNeverRan()
        self.assertEqual(r["refusal"]["check"], "C8a", r["refusal"])
        self.assertIn("nested repository", r["refusal"]["message"])
        self.assertIn("staged", r["refusal"]["message"])
        self.assertRefused(self.run_push(), "C8a", "sub")
        self.assertNeverRan()

    def test_g07_status_calls_do_not_look_inside_nested_repositories(self):
        """Behind C8a: the gate's status (through the runner) and the live pin's status never enter a nested repository."""
        sub = self.nested_repo(commit=True)
        self.plant_in_nested(sub)
        s = pa_push.Session()
        try:
            wt = os.path.realpath(self.wt)
            pins = pa_hooks.gate_commits(wt, runner=s.gate_git)
            self.assertEqual(len(pins), 1)
            self.assertNeverRan()
            self.assertEqual(pa_push.tracked_changes(s, wt), [])
            self.assertNeverRan()
        finally:
            s.close()
        status = [a for a in self.calls if "status" in a]
        self.assertTrue(status and all("--ignore-submodules=all" in a for a in status), status)

if __name__ == "__main__":
    unittest.main()
