"""pa-push-main's run checks C0 to C2 (t-007 part 7b, A3).

Run: python3 -m unittest -v tests/test_pa_push_runs_c0_c2.py
The fixture (temp homes, the bare origin under a temp folder, the person-only check's injected environment) is
tests/push_fixtures.py; see its docstring. Split from tests/test_pa_push.py by t-030 so the parts run at once.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from push_fixtures import *  # noqa: F401,F403 (the shared fixture and helpers)
from push_fixtures import setUpModule, tearDownModule  # noqa: F401 (the module's temp homes and environment)

# ---- A3: the run's checks C0 to C2 ---------------------------------------------

class RunChecks(Runs):
    def test_r_c0_env_markers(self):
        for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_SANDBOX", "CODEX_HOME_SESSION", "CODEX_ANYTHING", "CLAUDE_CODE_SSE_PORT"):
            r = self.assertRefused(self.run_push(env=dict(PERSON_ENV, **{k: "1"})), "C0", k, attempts=0)
            self.assertIn("the person's command", r["refusal"]["message"])
        self.assertEqual(self.calls, [])  # nothing was read

    def test_r_c0_ancestor_claude(self):
        for tree in ([{"pid": 7, "comm": "/opt/bin/claude", "args": "claude --resume"}] + PANEL_TREE,
                     PANEL_TREE[:1] + [{"pid": 8, "comm": "node", "args": "node /usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"}]):
            self.assertRefused(self.run_push(tree=tree), "C0", "ancestor", attempts=0)
        self.assertEqual(self.calls, [])

    def test_r_c0_ancestor_codex(self):
        for tree in ([{"pid": 7, "comm": "codex", "args": "codex"}] + PANEL_TREE,
                     [{"pid": 8, "comm": "/usr/bin/node", "args": "node /x/@openai/codex/bin/codex.js"}]):
            self.assertRefused(self.run_push(tree=tree), "C0", "codex", attempts=0)

    def test_r_c0_ps_fails_closed(self):
        code, r = pa_push.run_push(SPACE, CARD, "Push", None, self.seen, PERSON_ENV, None, None)
        self.assertEqual((code, r["refusal"]["check"]), (1, "C0"))
        self.assertIn("ps failed", r["refusal"]["message"])
        self.assertIsNone(pa_push.ancestors(table={}))
        self.assertIsNone(pa_push.ancestors(pid=5, table={9: (1, "x", "x")}))
        tree = pa_push.ancestors(pid=5, table={5: (4, "python3", "python3 pa_push.py"), 4: (3, "/bin/zsh", "-zsh"), 3: (0, "launchd", "launchd")})
        self.assertEqual([a["comm"] for a in tree], ["/bin/zsh", "launchd"])

    def test_r_c0_hc_terminal(self):
        self.assertRefused(self.run_push(env=dict(PERSON_ENV, HC_TERMINAL_ID="t7")), "C0", "Herdr terminal", attempts=0)

    def test_r_c0_orphan(self):
        """A process whose parent chain holds nothing but the system's first process: the caller is unknown."""
        for tree in ([{"pid": 1, "comm": "/sbin/launchd", "args": "/sbin/launchd"}], [{"pid": 1, "comm": "launchd", "args": "launchd"}],
                     [{"pid": 1, "comm": "/sbin/init", "args": "/sbin/init splash"}], [{"pid": 1, "comm": "systemd", "args": "/sbin/init"}]):
            self.assertRefused(self.run_push(tree=tree), "C0", "the caller is not the panel", attempts=0)
        self.assertEqual(self.calls, [])
        tree = pa_push.ancestors(pid=5, table={5: (1, "python3", "python3 pa_push.py"), 1: (0, "/sbin/launchd", "/sbin/launchd")})
        self.assertEqual([a["pid"] for a in tree], [1])
        self.assertIn("the caller is not the panel", pa_push.caller_refusal(PERSON_ENV, tree) or "")
        self.assertIn("orphaned", pa_push.caller_refusal(PERSON_ENV, tree, strict=False) or "")
        self.assertIsNone(pa_push.caller_refusal(PERSON_ENV, PANEL_TREE))
        self.assertIsNone(pa_push.caller_refusal(PERSON_ENV, PERSON_TREE, strict=False))

    def test_r_c0_not_the_panel(self):
        """d-069: check and run pass only with the Herdr app or Herdr core among the ancestors (and no agent)."""
        launchd = {"pid": 1, "comm": "/sbin/launchd", "args": "/sbin/launchd"}
        user_systemd = {"pid": 41, "comm": "systemd", "args": "/usr/lib/systemd/systemd --user"}
        init = {"pid": 1, "comm": "systemd", "args": "/sbin/init splash"}
        detached = ([{"pid": 40, "comm": "/bin/sh", "args": "/bin/sh -c x; true"}, launchd],
                    [launchd],
                    [user_systemd, init],
                    [{"pid": 42, "comm": "sh", "args": "sh"}, user_systemd, init],
                    PERSON_TREE,
                    [{"pid": 43, "comm": "/bin/zsh", "args": "-zsh"}, {"pid": 44, "comm": "login", "args": "login -fp p"},
                     {"pid": 45, "comm": "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal", "args": "Terminal"}, launchd],
                    [{"pid": 46, "comm": "/tmp/herdrx", "args": "herdrx"}, launchd])
        for tree in detached:
            self.assertRefused(self.run_push(tree=tree), "C0", "the caller is not the panel", attempts=0)
            r = self.check(tree=tree)
            self.assertFalse(r["caller"]["ok"])
            self.assertIn("the caller is not the panel", r["caller"]["why"])
        for tree in (PANEL_TREE, PANEL_TREE[1:], [PANEL_TREE[0], launchd],
                     [{"pid": 47, "comm": "/bin/sh", "args": "sh -c pa-push-main"}] + PANEL_TREE,
                     [{"pid": 48, "comm": "herdr-core", "args": "herdr-core"}, user_systemd, init]):
            self.assertIsNone(pa_push.caller_refusal(PERSON_ENV, tree), tree)
        # the existing refusals still come first, with their own messages
        self.assertIn("CLAUDECODE", pa_push.caller_refusal(dict(PERSON_ENV, CLAUDECODE="1"), PANEL_TREE))
        self.assertIn("ancestor process is claude",
                      pa_push.caller_refusal(PERSON_ENV, [{"pid": 7, "comm": "claude", "args": "claude"}] + PANEL_TREE))
        self.assertIn("Herdr terminal", pa_push.caller_refusal(dict(PERSON_ENV, HC_TERMINAL_ID="t1"), PANEL_TREE))
        self.assertIn("ps failed", pa_push.caller_refusal(PERSON_ENV, None))
        # pa-push-allow (typed by the person in a plain terminal) keeps today's rule
        self.assertIsNone(pa_push.caller_refusal(PERSON_ENV, PERSON_TREE, strict=False))
        self.assertIsNone(pa_push.caller_refusal(PERSON_ENV, detached[0], strict=False))
        self.assertIn("orphaned", pa_push.caller_refusal(PERSON_ENV, [launchd], strict=False))
        self.assertEqual(set(pa_push.PANEL_NAMES), {"herdr", "herdr-core"})

    def test_r_c0_cli_refuses_in_this_session(self):
        """The real command line, started from this agent session: refused by C0, with the JSON contract."""
        code, out, err = self.cli("pa-push-main", "run", CARD, "--answer", "Push", "--space", SPACE, "--seen", self.seen, "--json",
                                  env=dict(os.environ, CLAUDECODE="1"))
        self.assertEqual(code, 1, err)
        r = json.loads(out)
        self.assertEqual(set(r), {"ok", "done", "answer", "refusal", "result", "message"})
        self.assertEqual(r["refusal"]["check"], "C0")
        self.assertEqual(self.origin_head(), self.origin_before)
        self.assertEqual(self.card()["push"]["attempts"], [])
        code, _o, _e = self.cli("pa-push-main", "run", CARD, "--space", SPACE)
        self.assertEqual(code, 2)  # --answer is required: a usage error

    def test_r_c1_no_space_state(self):
        self.assertRefused(self.run_push(space="sp9"), "C1", "no Project Agent state for space sp9", attempts=0)
        self.assertRefused(self.run_push(space=None), "C1", "--space", attempts=0)

    def test_r_c2_lock_held(self):
        with pa_push.push_lock(self.dir):
            t = time.monotonic()
            self.assertRefused(self.run_push(), "C2", "another push for this space is running", attempts=0)
            self.assertGreaterEqual(time.monotonic() - t, 4)

if __name__ == "__main__":
    unittest.main()
