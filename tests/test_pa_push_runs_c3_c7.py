"""pa-push-main's run checks C3 to C7 and their order (t-007 part 7b, A3).

Run: python3 -m unittest -v tests/test_pa_push_runs_c3_c7.py
The fixture (temp homes, the bare origin under a temp folder, the person-only check's injected environment) is
tests/push_fixtures.py; see its docstring. Split from tests/test_pa_push.py by t-030 so the parts run at once.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from push_fixtures import *  # noqa: F401,F403 (the shared fixture and helpers)
from push_fixtures import setUpModule, tearDownModule  # noqa: F401 (the module's temp homes and environment)

# ---- A3: the run's checks C3 to C7 ---------------------------------------------

class RunChecks(Runs):
    def test_r_c3_not_push_card(self):
        dump({"id": "d-1", "question": "Q", "options": [{"label": "Push"}], "status": "open"}, os.path.join(self.dir, "decisions", "d-1.json"))
        self.assertRefused(self.run_push(card="d-1"), "C3", "d-1 is not a push card", attempts=0)
        self.assertRefused(self.run_push(card="d-none"), "C3", "no decision card d-none", attempts=0)
        self.assertRefused(self.run_push(card="../x"), "C3", "no decision card", attempts=0)
        self.card_edit(version=2)
        self.assertRefused(self.run_push(), "C3", "push version 2", attempts=0)

    def test_r_c3_other_space(self):
        self.card_edit(space="sp2")
        self.assertRefused(self.run_push(), "C3", "belongs to space sp2, not sp1", attempts=0)

    def test_r_c4_answered_or_withdrawn(self):
        for status in ("withdrawn", "answered"):
            c = self.card()
            c["status"] = status
            dump(c, self.card_path())
            self.assertRefused(self.run_push(), "C4", "{} is {}; nothing was pushed".format(CARD, status), attempts=0)

    def test_r_c5_bad_answer(self):
        self.assertRefused(self.run_push(answer="Yes"), "C5", "--answer must be one of: Push, Not now", attempts=0)

    def test_r_c6a(self):
        os.remove(self.allow)
        self.assertRefused(self.run_push(), "C6a", "no push allowlist")

    def test_r_c6b(self):
        write(self.allow, '{"version": 1, "enabled": true, "repos": [], "x": 1}')
        self.assertRefused(self.run_push(), "C6b", "the push allowlist is not valid: unknown key x")

    def test_r_c6c(self):
        dump(dict(load(self.allow), enabled=False), self.allow)
        self.assertRefused(self.run_push(), "C6c", "turned off (enabled: false)")

    def test_r_c6d(self):
        self.set_allow(path=os.path.join(self.tmp, "elsewhere"))
        self.assertRefused(self.run_push(), "C6d", "no allowlist entry for " + self.main)

    def test_r_c6e(self):
        self.card_edit(url="file://" + os.path.join(self.tmp, "other.git"))
        self.assertRefused(self.run_push(), "C6e", "has URL " + self.url)

    def test_r_c6f(self):
        self.card_edit(branch="dev")
        self.assertRefused(self.run_push(), "C6f", "has branch main, the card says dev")

    def test_r_c6g(self):
        self.set_allow(space="sp2")
        self.assertRefused(self.run_push(), "C6g", "names space sp2, not this space sp1")

    def test_r_c6h(self):
        self.set_allow(enabled=False)
        self.assertRefused(self.run_push(), "C6h", "is turned off")

    def test_r_c6i(self):
        self.set_allow(note="changed after the card")
        self.assertRefused(self.run_push(), "C6i", "changed since this card was made")

    def test_r_order_earlier_check_wins(self):
        self.set_allow(enabled=False)  # C6h, and the file's SHA changes too (C6i)
        self.assertRefused(self.run_push(), "C6h", "turned off")
        self.set_allow(enabled=True, note="x")  # C6i
        self.commit_on(self.wt, "late.txt", "late")  # C8f too
        self.assertRefused(self.run_push(), "C6i", "changed", attempts=2)

    def test_r_c7b_hookspath(self):
        sh(self.main, "git", "config", "core.hooksPath", os.path.join(self.tmp, "planted"))
        self.assertRefused(self.run_push(), "C7b", "has git setting core.hookspath (local)")

    def test_r_c7b_sshcommand(self):
        sh(self.main, "git", "config", "core.sshCommand", "/tmp/planted")
        self.assertRefused(self.run_push(), "C7b", "core.sshcommand")
        sh(self.main, "git", "config", "--unset", "core.sshCommand")
        sh(self.main, "git", "config", "remote.origin.pushurl", "file:///elsewhere")
        self.assertRefused(self.run_push(), "C7b", "remote.origin.pushurl", attempts=2)

    def test_r_c7c_pushinsteadof(self):
        gitconfig = os.path.join(os.environ["HOME"], ".gitconfig")
        self.addCleanup(lambda: os.path.exists(gitconfig) and os.remove(gitconfig))
        write(gitconfig, '[url "file:///nowhere/"]\n\tpushInsteadOf = file://{}\n'.format(TMP))
        self.assertRefused(self.run_push(), "C7c", "a URL rewrite")

if __name__ == "__main__":
    unittest.main()
