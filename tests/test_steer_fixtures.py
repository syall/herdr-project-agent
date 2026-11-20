"""tests/steer_fixtures.py's forge_parent (t-079 I9, t-099; t-089 VERIFY RL1, REVIEW RL-J1-1): a git that does not read
the forged commit-graph skips the forged-graph tests (C13d, G13) with the reason, except on the Mac, where plain git
reads it: there the same reason is a failure, so the tests never stop running unnoticed.

A git wrapper first on PATH runs every git call but `commit-graph` with -c core.commitGraph=false: a git that ignores
the graph. Each case runs in its own temp repository, with no global or system git config."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import steer_fixtures  # noqa: E402

WRAPPER = '''#!/bin/sh
case " $* " in *" commit-graph "*) exec "{git}" "$@" ;; esac
exec "{git}" -c core.commitGraph=false "$@"
'''


class ForgeParent(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="t079-steer-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
               "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@example.invalid"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = os.path.join(self.tmp, "repo")
        git = steer_fixtures.git
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "root")
        root = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "child")
        self.child = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "checkout", "-q", "-b", "other", root)
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "other")
        self.parent = git(self.repo, "rev-parse", "HEAD")

    def ignoring_graph(self):
        real = shutil.which("git")
        folder = os.path.join(self.tmp, "wrap")
        os.makedirs(folder)
        with open(os.path.join(folder, "git"), "w") as f:
            f.write(WRAPPER.format(git=real))
        os.chmod(os.path.join(folder, "git"), 0o755)
        patcher = mock.patch.dict(os.environ, {"PATH": folder + os.pathsep + os.environ.get("PATH", "")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_f1_on_the_mac_a_git_that_ignores_the_graph_fails_with_the_reason(self):
        self.ignoring_graph()
        with mock.patch("sys.platform", "darwin"):
            try:
                steer_fixtures.forge_parent(self.repo, self.child, self.parent)
            except unittest.SkipTest as e:
                self.fail("skipped on the Mac, so the forged-graph tests stop running unnoticed: {}".format(e))
            except AssertionError as e:
                text = str(e)
            else:
                self.fail("the forged graph was read through a git that ignores it")
        self.assertIn("does not read a forged commit-graph", text)
        self.assertIn(self.child[:12] + "^ gives", text)
        self.assertIn("on the Mac", text)

    def test_f2_elsewhere_a_git_that_ignores_the_graph_skips_with_the_reason(self):
        self.ignoring_graph()
        with mock.patch("sys.platform", "linux"):
            with self.assertRaises(unittest.SkipTest) as caught:
                steer_fixtures.forge_parent(self.repo, self.child, self.parent)
        self.assertIn("does not read a forged commit-graph", str(caught.exception))

    def test_f3_a_git_that_reads_the_graph_gets_the_forged_parent(self):
        try:
            steer_fixtures.forge_parent(self.repo, self.child, self.parent)
        except unittest.SkipTest:
            if sys.platform == "darwin":
                raise
            self.skipTest("this git does not read a forged commit-graph (not the Mac)")
        self.assertEqual(steer_fixtures.git(self.repo, "rev-parse", self.child + "^"), self.parent)


if __name__ == "__main__":
    unittest.main()
