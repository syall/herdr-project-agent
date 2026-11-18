"""t-046 J1: an Other answer on a push card (pa-push-main run --answer Other) pushes nothing, and pa-decide
withdraws an Other-answered push card under the push lock (spec R7, R10, R12; plan J1).

Run: python3 -m unittest -v tests/test_pa_other_push.py
The fixture (temp homes, the bare origin under a temp folder, the person-only check's injected environment) is
tests/push_fixtures.py; see its docstring. No real space, no real repository, no host is contacted.
"""
import argparse
import contextlib
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from push_fixtures import *  # noqa: F401,F403 (the shared fixture and helpers)
from push_fixtures import setUpModule, tearDownModule  # noqa: F401 (the module's temp homes and environment)


class OtherPush(Runs):
    """R7: Other closes the card like Not now, under the lock; no git runs, no ref moves, --seen is ignored."""

    def assertNothingPushed(self):
        self.assertEqual(self.calls, [], "an Other answer runs no git at all")
        self.assertEqual(self.origin_head(), self.origin_before)
        self.assertEqual(self.head(self.main, "refs/heads/main"), self.main_before)

    def test_o01_other_closes_the_card_and_pushes_nothing(self):
        attempts = list(self.card()["push"].get("attempts") or [])
        code, r = self.run_push(answer="Other", note="  push it after the docs change lands  ")
        self.assertEqual((code, r["ok"], r["done"], r["answer"], r["refusal"]), (0, True, False, "Other", None))
        self.assertEqual(r["message"], "{} answered Other; nothing was pushed.".format(CARD))
        c = self.card()
        self.assertEqual((c["status"], c["answer"], c["other"], c["note"], c["answeredBy"], c["push"]["done"]),
                         ("answered", "Other", True, "push it after the docs change lands", "person, in the panel", False))
        self.assertTrue(c["answered"])
        self.assertEqual(list(c["push"].get("attempts") or []), attempts, "an Other answer is not an attempt")
        j = self.journal()[-1]
        self.assertEqual((j["by"], j["trigger"], j["decision"], j["why"]),
                         ("person", "panel", CARD, "answered Other: push it after the docs change lands"))
        self.assertNothingPushed()

    def test_o02_other_ignores_a_valid_seen_token(self):
        token = self.check()["seen"]
        self.calls[:] = []
        code, r = self.run_push(answer="Other", seen=token, note="not yet")
        self.assertEqual((code, r["answer"]), (0, "Other"))
        self.assertNothingPushed()

    def test_o03_other_needs_words(self):
        for note in (None, "", "   \n "):
            self.assertRefused(self.run_push(answer="Other", note=note), "C5",
                               "an Other answer needs --note with the person's own words", attempts=0)
            self.assertEqual(self.card()["status"], "open")
            self.assertNotIn("other", self.card())
        self.assertEqual(self.calls, [])

    def test_o04_other_words_at_most_2000(self):
        self.assertRefused(self.run_push(answer="Other", note="x" * 2001), "C5",
                           "longer than 2000 characters; shorten it, or write it in a file and give the path", attempts=0)
        self.assertEqual(self.card()["status"], "open")
        code, r = self.run_push(answer="Other", note="y" * 2000)
        self.assertEqual((code, r["answer"]), (0, "Other"))
        self.assertEqual(self.card()["note"], "y" * 2000)
        self.assertNothingPushed()

    def test_o05_bad_answer_lists_other(self):
        self.assertRefused(self.run_push(answer="Yes"), "C5", "--answer must be one of: Push, Not now, Other", attempts=0)

    def test_o06_only_push_reaches_the_push_path(self):
        for answer in ("push", "PUSH", "other", "Push ", "not now"):
            self.assertRefused(self.run_push(answer=answer, note="words"), "C5", "--answer must be one of", attempts=0)
        self.assertEqual(self.calls, [])

    def test_o07_already_pushed_answers_push(self):
        c = self.card()
        c.update(status="answered", answer="Push")
        c["push"].update(done=True, result={"at": "2026-11-18T10:00:00Z", "from": "a" * 40, "to": "b" * 40})
        dump(c, self.card_path())
        before = snapshot(self.dir)
        code, r = self.run_push(answer="Other", note="do not push")
        self.assertEqual((code, r["ok"], r["done"], r["answer"]), (0, True, True, "Push"))
        self.assertEqual(snapshot(self.dir), before)

    def test_o08_answered_other_is_not_open(self):
        self.assertEqual(self.run_push(answer="Other", note="later")[0], 0)
        self.assertRefused(self.run_push(answer="Push", seen=FRESH), "C4", "is answered; nothing was pushed", attempts=0)

    def test_o09_not_now_and_push_write_no_other_field(self):
        code, _r = self.run_push(answer="Not now", note="later")
        self.assertEqual(code, 0)
        self.assertNotIn("other", self.card())


class DecidePushCard(Runs):
    """R10 with the person's answer on card d-131's plan review: pa-decide withdraws an Other-answered push card,
    and every pa-decide write of a push card takes the space's push lock."""

    def decide(self, **kw):
        args = argparse.Namespace(**dict({"space": SPACE, "card": CARD, "withdraw": False, "answer": None, "quote": None, "why": None}, **kw))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            pa.cmd_decide(args)
        return out.getvalue()

    def test_o10_other_from_conversation_refused_on_a_push_card(self):
        before = snapshot(self.dir)
        with self.assertRaises(pa.Problem) as e:
            self.decide(answer="Other", quote="push it tomorrow")
        self.assertIn("a push card is answered in the panel, not in conversation", str(e.exception))
        self.assertEqual(snapshot(self.dir), before)

    def test_o11_withdraw_an_other_push_card(self):
        self.assertEqual(self.run_push(answer="Other", note="ask me after the release")[0], 0)
        out = self.decide(withdraw=True, why="the person asked to be asked again")
        self.assertIn("{} withdrawn".format(CARD), out)
        c = self.card()
        self.assertEqual((c["status"], c["answer"], c["previousAnswer"], c["previousNote"], c["note"], c["answeredBy"], c["push"]["done"]),
                         ("withdrawn", None, "Other", "ask me after the release", "the person asked to be asked again",
                          "orchestrator: withdrawn", False))
        self.assertEqual(self.origin_head(), self.origin_before)

    def test_o12_withdraw_of_a_push_card_waits_for_the_push_lock(self):
        saved = pa.PUSH_LOCK_WAIT
        pa.PUSH_LOCK_WAIT = 0.3
        try:
            for answered in (False, True):
                if answered:
                    self.assertEqual(self.run_push(answer="Other", note="later")[0], 0)
                before = snapshot(self.dir)
                with pa_push.push_lock(self.dir):
                    with self.assertRaises(pa.Problem) as e:
                        self.decide(withdraw=True, why="moot")
                self.assertIn("another push for this space is running", str(e.exception))
                self.assertEqual(snapshot(self.dir), before)
        finally:
            pa.PUSH_LOCK_WAIT = saved
        self.decide(withdraw=True, why="moot")  # the lock is free again
        self.assertEqual(self.card()["status"], "withdrawn")

    def test_o13_withdraw_of_a_push_card_answered_not_now_is_refused(self):
        self.assertEqual(self.run_push(answer="Not now", note="later")[0], 0)
        before = snapshot(self.dir)
        with self.assertRaises(pa.Problem) as e:
            self.decide(withdraw=True, why="moot")
        self.assertIn("{} is already answered".format(CARD), str(e.exception))
        self.assertEqual(snapshot(self.dir), before)


if __name__ == "__main__":
    unittest.main()
