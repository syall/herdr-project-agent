"""t-046 J1: an Other answer, the person's own words, through pa-decide, pa-worktree and pa-worker close --card
(spec R5, R6, R8, R9, R10, R12; plan J1). No tool acts on it; each says so and quotes the words.

Run: python3 -m unittest -v tests/test_pa_other_worker.py
Uses the fixture of tests/test_pa_worker.py (temp homes, a stub hc, git only under a temp folder). Only Base is
bound in this module, so none of that file's tests run here a second time.
"""
import argparse
import contextlib
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_pa_worker as tw  # noqa: E402
from test_pa_worker import Base, setUpModule, tearDownModule  # noqa: E402,F401 (the module's temp homes and environment)

pa, pa_hooks, load, dump, raw = tw.pa, tw.pa_hooks, tw.load, tw.dump, tw.raw


class Cards(Base):
    def card_path(self, cid):
        return os.path.join(self.dir, "decisions", cid + ".json")

    def put(self, cid, options=("A", "B"), **fields):
        card = dict({"id": cid, "question": "Which one?", "options": [{"label": o} for o in options], "status": "open",
                     "answer": None, "note": None}, **fields)
        dump(card, self.card_path(cid))
        return card

    def read(self, cid):
        return load(self.card_path(cid))

    def decide(self, cid, answer=None, quote=None, withdraw=False, why=None):
        """(exit code, stdout, the refusal) of pa-decide, called as main() would."""
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                pa.cmd_decide(argparse.Namespace(space="sp1", card=cid, answer=answer, quote=quote, withdraw=withdraw, why=why))
        except pa.Problem as e:
            return 2, out.getvalue(), str(e)
        return 0, out.getvalue(), ""


class Decide(Cards):
    """R5, R6 AC2, R10: pa-decide records Other from conversation, changes no rule on it, and withdraws it."""

    def test_d01_other_from_conversation(self):
        self.put("d-1")
        code, out, err = self.decide("d-1", answer="other", quote="  neither: ask the reviewer first  ")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "d-1 answered Other (person, in conversation).\n")
        c = self.read("d-1")
        self.assertEqual((c["status"], c["answer"], c["other"], c["note"], c["answeredBy"]),
                         ("answered", "Other", True, "neither: ask the reviewer first", "person, in conversation"))
        self.assertEqual(self.journal()[-1]["why"], "answered Other: neither: ask the reviewer first")

    def test_d02_other_needs_words(self):
        self.put("d-1")
        before = raw(self.card_path("d-1"))
        code, _o, err = self.decide("d-1", answer="Other", quote="x")
        self.assertEqual(code, 2)
        self.assertIn("--quote needs the person's own words", err)
        code, _o, err = self.decide("d-1", answer="Other", quote="z" * 2001)
        self.assertEqual(code, 2)
        self.assertIn("longer than 2000 characters; shorten it, or write it in a file and give the path", err)
        self.assertEqual(raw(self.card_path("d-1")), before)

    def test_d03_other_on_a_rule_card_changes_no_rule(self):
        rules = os.path.join(self.dir, "rules.json")
        dump({"rules": [{"id": "r-1", "status": "proposed", "text": "Always run lint."}]}, rules)
        before = raw(rules)
        self.put("d-2", options=("Approve", "Decline"), rule="r-1")
        code, out, err = self.decide("d-2", answer="Other", quote="Approve, but only for tests")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "d-2 answered Other (person, in conversation).\n"
                              "Rule r-1 stays proposed: an Other answer is not Approve or Decline.\n")
        self.assertEqual(raw(rules), before)
        self.assertEqual(self.read("d-2")["answer"], "Other")

    def test_d04_a_listed_answer_is_as_before(self):
        rules = os.path.join(self.dir, "rules.json")
        dump({"rules": [{"id": "r-1", "status": "proposed"}]}, rules)
        self.put("d-2", options=("Approve", "Decline"), rule="r-1")
        code, out, err = self.decide("d-2", answer="approve", quote="yes, approve it")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "d-2 answered Approve (person, in conversation).\n")
        self.assertNotIn("other", self.read("d-2"))
        self.assertEqual(load(rules)["rules"][0]["status"], "active")

    def test_d05_refusal_lists_other_last_once(self):
        self.put("d-1")
        code, _o, err = self.decide("d-1", answer="C", quote="the third one")
        self.assertEqual(code, 2)
        self.assertIn("--answer must be one of this card's options: A, B, Other", err)
        self.put("d-3", options=("A", "other"))
        code, _o, err = self.decide("d-3", answer="C", quote="the third one")
        self.assertIn("--answer must be one of this card's options: A, Other", err)
        self.assertNotIn("other,", err)

    def test_d06_a_cards_own_other_option_is_an_other_answer(self):
        self.put("d-3", options=("A", "other"))
        code, _o, err = self.decide("d-3", answer="OTHER", quote="my own idea")
        self.assertEqual(code, 0, err)
        c = self.read("d-3")
        self.assertEqual((c["answer"], c["other"], c["note"]), ("Other", True, "my own idea"))

    def test_d07_panel_only_cards_still_refused(self):
        for cid, fields, text in (("d-wt-t-1", {"worktree": "/tmp/x"}, "a card that deletes a worktree is answered in the panel"),
                                  ("d-leftovers", {"kind": "leftovers"}, "a card that closes workers is answered in the panel")):
            self.put(cid, **fields)
            before = raw(self.card_path(cid))
            code, _o, err = self.decide(cid, answer="Other", quote="keep it for now")
            self.assertEqual(code, 2)
            self.assertIn(text, err)
            self.assertEqual(raw(self.card_path(cid)), before)

    def test_d08_withdraw_an_other_answer(self):
        for cid, flag in (("d-1", {"other": True, "answer": "Other"}), ("d-4", {"answer": "other"})):  # the marker, or the label alone
            self.put(cid, status="answered", note="ask me again tomorrow", answeredBy="person, in the panel", **flag)
            code, out, err = self.decide(cid, withdraw=True, why="the person asked for the question again")
            self.assertEqual(code, 0, err)
            self.assertEqual(out, "{} withdrawn (orchestrator: withdrawn).\n".format(cid))
            c = self.read(cid)
            self.assertEqual((c["status"], c["answer"], c["previousAnswer"], c["previousNote"], c["note"], c["answeredBy"]),
                             ("withdrawn", None, "Other", "ask me again tomorrow", "the person asked for the question again",
                              "orchestrator: withdrawn"))
            self.assertEqual(self.journal()[-1]["why"],
                             'withdrawn (was Other: "ask me again tomorrow"): the person asked for the question again')

    def test_d12_withdraw_keeps_when_and_cuts_the_journal_words(self):
        """J1b: the withdraw keeps when the Other answer was given, and its journal line keeps the words (cut, said so)."""
        self.put("d-1", status="answered", answer="Other", other=True, note="w" * 150 + "\n" + "v" * 150,
                 answered="2026-11-01T10:00:00Z", answeredBy="person, in the panel")
        code, _o, err = self.decide("d-1", withdraw=True, why="asked again")
        self.assertEqual(code, 0, err)
        c = self.read("d-1")
        self.assertEqual((c["previousAnswered"], c["previousNote"]), ("2026-11-01T10:00:00Z", "w" * 150 + "\n" + "v" * 150))
        self.assertNotEqual(c["answered"], "2026-11-01T10:00:00Z")
        words = ("w" * 150 + " " + "v" * 150)[:199]
        self.assertEqual(self.journal()[-1]["why"], 'withdrawn (was Other: "{}… (the rest is on the card)"): asked again'.format(words))
        self.put("d-2")  # an open card: as before
        self.assertEqual(self.decide("d-2", withdraw=True, why="moot")[0], 0)
        self.assertEqual(self.journal()[-1]["why"], "withdrawn: moot")
        self.assertNotIn("previousAnswered", self.read("d-2"))

    def test_d13_withdraw_other_without_words_has_no_previous_note(self):
        """J1b round 2: absent words leave no previousNote key; an older carried one is dropped."""
        self.put("d-1", status="answered", answer="Other", note=None, answered="2026-11-01T10:00:00Z",
                 previousAnswer="Delete", previousNote="older words", previousAnswered="2026-10-01T10:00:00Z")
        self.assertEqual(self.decide("d-1", withdraw=True, why="asked again")[0], 0)
        c = self.read("d-1")
        self.assertEqual((c["previousAnswer"], c["previousAnswered"]), ("Other", "2026-11-01T10:00:00Z"))
        self.assertNotIn("previousNote", c)

    def test_d09_withdraw_any_other_answered_card_is_refused(self):
        self.put("d-1", status="answered", answer="B", note="B it is")
        self.put("d-5", status="withdrawn", answer=None, previousAnswer="Other", other=True, note="old")
        for cid, state in (("d-1", "answered"), ("d-5", "withdrawn")):
            before = raw(self.card_path(cid))
            code, _o, err = self.decide(cid, withdraw=True, why="moot")
            self.assertEqual(code, 2)
            self.assertIn("{} is already {}".format(cid, state), err)
            self.assertEqual(raw(self.card_path(cid)), before)

    def test_d10_withdraw_other_on_a_worktree_or_leftovers_card(self):
        for cid, fields in (("d-wt-t-1", {"worktree": "/tmp/x"}), ("d-leftovers", {"kind": "leftovers"})):
            self.put(cid, status="answered", answer="Other", other=True, note="later", **fields)
            code, _o, err = self.decide(cid, withdraw=True, why="asked again")
            self.assertEqual(code, 0, err)
            self.assertEqual(self.read(cid)["status"], "withdrawn")

    def test_d11_help_names_other(self):
        code, out, _e = self.run_help()
        self.assertIn("Other for the person's own answer", " ".join(out.split()))

    def run_help(self):
        out, saved = io.StringIO(), sys.argv
        sys.argv = ["pa-decide", "--help"]
        try:
            with contextlib.redirect_stdout(out):
                try:
                    pa.main()
                except SystemExit as e:
                    return e.code, out.getvalue(), ""
        finally:
            sys.argv = saved
        return 0, out.getvalue(), ""


WORDS = "Delete it but keep the branch"


class Worktree(Cards):
    """R8: pa-worktree removes nothing on an Other answer, says so, and asks again only once the card is withdrawn."""

    def setUp(self):
        super().setUp()
        self.proj = self.repo()
        self.wt = self.worktree(self.proj, "t-003-x")
        self.add_task("t-003", "done", self.wt, [])

    def check(self, *extra):
        return self.run_cmd("pa-worktree", "check", "--open-cards", "--space", "sp1", *extra)

    def remove(self):
        return self.run_cmd("pa-worktree", "remove", "t-003", "--space", "sp1")

    def answered_other(self, **fields):
        self.assertIn("opened card d-wt-t-003", self.check()[1])
        path = self.card_path("d-wt-t-003")
        card = load(path)
        card.update(dict({"status": "answered", "answer": "Other", "other": True, "note": WORDS, "answeredBy": "person, in the panel"}, **fields))
        dump(card, path)
        return card

    def test_w01_remove_on_other_removes_nothing(self):
        self.answered_other()
        before = (raw(self.card_path("d-wt-t-003")), self.snapshot())
        code, out, err = self.remove()
        self.assertEqual(code, 2, out)
        self.assertIn('card d-wt-t-003 was answered Other, in the person\'s own words: "{}". pa-worktree removes nothing on it. '
                      'Act on their words; if they ask for the worktree to go, withdraw the card (pa-decide d-wt-t-003 --withdraw '
                      '--why "<reason>") and the next pa-worktree check --open-cards asks again.'.format(WORDS), err)
        self.assertTrue(os.path.isdir(self.wt))
        self.assertEqual((raw(self.card_path("d-wt-t-003")), self.snapshot()), before)

    def test_w02_the_label_alone_is_other(self):
        self.answered_other()
        for answer in ("other", "OTHER"):
            card = load(self.card_path("d-wt-t-003"))
            card.update(answer=answer)
            card.pop("other", None)
            dump(card, self.card_path("d-wt-t-003"))
            code, _o, err = self.remove()
            self.assertEqual(code, 2)
            self.assertIn("was answered Other", err)
            self.assertTrue(os.path.isdir(self.wt))

    def test_w03_check_reports_other_and_opens_nothing(self):
        self.answered_other()
        before = (raw(self.card_path("d-wt-t-003")), sorted(os.listdir(os.path.join(self.dir, "decisions"))))
        code, out, err = self.check()
        self.assertEqual(code, 0, err)
        self.assertIn('t-003: card d-wt-t-003 answered Other ("{}"): nothing is removed; not asked again unless the card is '
                      'withdrawn'.format(WORDS), out)
        self.assertNotIn("run `pa-worktree remove", out)
        self.assertEqual((raw(self.card_path("d-wt-t-003")), sorted(os.listdir(os.path.join(self.dir, "decisions")))), before)

    def test_w04_words_cut_at_200_in_the_messages(self):
        long = "w" * 300
        self.answered_other(note=long)
        out = self.check()[1]
        self.assertIn('answered Other ("{}…")'.format("w" * 199), out)
        err = self.remove()[2]
        self.assertIn('own words: "{}…"'.format("w" * 199), err)

    def test_w05_withdrawn_other_is_asked_again(self):
        self.answered_other()
        code, _o, err = self.decide("d-wt-t-003", withdraw=True, why="the person now wants it deleted")
        self.assertEqual(code, 0, err)
        self.assertIn("opened card d-wt-t-003", self.check()[1])
        c = self.read("d-wt-t-003")
        self.assertEqual((c["status"], c["answer"]), ("open", None))

    def test_w06_the_card_asked_again_carries_the_words(self):
        """J1b: the new card over the same file keeps what the person said on the withdrawn one."""
        self.answered_other(answered="2026-11-01T10:00:00Z")
        self.assertEqual(self.decide("d-wt-t-003", withdraw=True, why="the person now wants it deleted")[0], 0)
        self.assertIn("opened card d-wt-t-003", self.check()[1])
        c = self.read("d-wt-t-003")
        self.assertEqual((c["status"], c["answer"], c["note"], c["answered"]), ("open", None, None, None))
        self.assertEqual((c["previousAnswer"], c["previousNote"], c["previousAnswered"]), ("Other", WORDS, "2026-11-01T10:00:00Z"))
        self.assertNotIn("other", c)

    def test_w07_a_first_card_carries_nothing(self):
        self.assertIn("opened card d-wt-t-003", self.check()[1])
        c = self.read("d-wt-t-003")
        for key in ("previousAnswer", "previousNote", "previousAnswered", "previousCard"):
            self.assertNotIn(key, c)

    def delete_on_the_card_asked_again(self, note):
        """Other, withdrawn, asked again; then the person answers Delete and the worktree changes, so pa-worktree remove
        withdraws that card. Returns the card the next check opens."""
        self.answered_other(answered="2026-11-01T10:00:00Z")
        self.assertEqual(self.decide("d-wt-t-003", withdraw=True, why="the person now wants it deleted")[0], 0)
        self.assertIn("opened card d-wt-t-003", self.check()[1])
        path = self.card_path("d-wt-t-003")
        card = load(path)
        card.update(status="answered", answer="Delete", note=note, answered="2026-11-03T09:00:00Z", answeredBy="person, in the panel")
        dump(card, path)
        self.commit(self.wt, "late.txt", "late\n", "a commit after the card")
        code, _o, err = self.remove()
        self.assertEqual(code, 2)
        self.assertIn("the card is withdrawn", err)
        c = self.read("d-wt-t-003")
        self.assertEqual((c["status"], c["previousAnswer"], c["previousAnswered"]), ("withdrawn", "Delete", "2026-11-03T09:00:00Z"))
        self.assertIn("opened card d-wt-t-003", self.check()[1])
        return self.read("d-wt-t-003")

    def test_w08_a_listed_answer_withdrawn_drops_the_older_words(self):
        """J1b round 2: the three carried keys describe the Delete answer, not the older Other words and time."""
        c = self.delete_on_the_card_asked_again(None)
        self.assertEqual((c["status"], c["previousAnswer"], c["previousAnswered"]), ("open", "Delete", "2026-11-03T09:00:00Z"))
        self.assertNotIn("previousNote", c)

    def test_w09_a_listed_answer_withdrawn_carries_its_own_words(self):
        c = self.delete_on_the_card_asked_again("  keep the branch  ")
        self.assertEqual((c["previousAnswer"], c["previousNote"], c["previousAnswered"]),
                         ("Delete", "  keep the branch  ", "2026-11-03T09:00:00Z"))


class Leftovers(Cards):
    """R9: pa-worker close --card closes nothing on an Other answer, withdraws the card and quotes the words."""

    def setUp(self):
        super().setUp()
        pa_hooks.by_an_agent = lambda: True
        self.t0 = tw.time.time()

    add = tw.Leftovers.add
    sweep = tw.Leftovers.sweep
    ready = tw.Leftovers.ready
    answer = tw.Leftovers.answer

    def card(self):
        return pa.read_json(self.card_path("d-leftovers"))

    def test_l01_other_closes_nothing_and_withdraws(self):
        self.ready()
        self.answer("Other", note="close all but t32")
        path = self.card_path("d-leftovers")
        c = load(path)
        c["other"] = True
        dump(c, path)
        tasks = json.dumps(self.ledger()["tasks"])
        code, out, err = self.worker("close", "--card", "d-leftovers")
        self.assertEqual(code, 2, out)
        self.assertIn('card d-leftovers was answered Other, in the person\'s own words: "close all but t32". Nothing was closed; '
                      'the words are not read by this tool. Act on them (ask the person to say "keep tNN" for each worker to '
                      'keep), then run pa-worker close --leftovers to ask again on a new card.', err)
        self.assertEqual(self.ended(), [])
        self.assertEqual(json.dumps(self.ledger()["tasks"]), tasks)
        c = self.card()
        self.assertEqual((c["status"], c["answer"], c["previousAnswer"], c["previousNote"], c["answeredBy"]),
                         ("withdrawn", None, "Other", "close all but t32", "pa-worker close: withdrawn"))
        self.assertTrue(c["withdrawnAt"])
        j = self.journal()[-1]
        self.assertEqual(j["trigger"], "leftovers")
        self.assertIn("withdrew card d-leftovers: it was answered Other (close all but t32); nothing was closed", j["why"])
        self.assertRefused(self.worker("close", "t31", "--card", "d-leftovers"), "card d-leftovers is withdrawn")

    def test_l02_the_next_sweep_asks_again(self):
        self.ready()
        self.answer("Other", note="not now")
        self.worker("close", "--card", "d-leftovers")
        code, out, err = self.sweep()
        self.assertEqual(code, 0, err)
        self.assertIn("opened card d-leftovers-2", out)

    def test_l03_the_label_alone_is_other_even_without_words(self):
        self.ready()
        self.answer("other")
        code, _o, err = self.worker("close", "--card", "d-leftovers")
        self.assertEqual(code, 2)
        self.assertIn("card d-leftovers was answered Other", err)
        self.assertNotIn("answered 'other'", err)
        self.assertEqual(self.ended(), [])
        self.assertEqual(self.card()["status"], "withdrawn")

    def test_l04_the_next_card_carries_the_words(self):
        """J1b: the next leftovers card (another id) names the withdrawn card and what the person said on it."""
        self.ready()
        self.answer("Other", note="close all but t32")
        answered = self.card()["answered"]
        self.worker("close", "--card", "d-leftovers")
        self.assertEqual(self.card()["previousAnswered"], answered)
        code, out, err = self.sweep()
        self.assertEqual(code, 0, err)
        self.assertIn("opened card d-leftovers-2", out)
        c = pa.read_json(self.card_path("d-leftovers-2"))
        self.assertEqual((c["status"], c["answer"], c["note"]), ("open", None, None))
        self.assertEqual((c["previousCard"], c["previousAnswer"], c["previousNote"], c["previousAnswered"]),
                         ("d-leftovers", "Other", "close all but t32", answered))


    def test_l05_a_note_withdrawn_carries_its_own_words(self):
        """J1b round 2 (the review's chain): Other on d-leftovers, then Close all with a note on d-leftovers-2; the next
        card carries that answer, that note and that time, never the older Other words."""
        self.ready()
        self.answer("Other", note="close all but t32")
        self.worker("close", "--card", "d-leftovers")
        self.assertIn("opened card d-leftovers-2", self.sweep()[1])
        path = self.card_path("d-leftovers-2")
        card = load(path)
        card.update(status="answered", answer=pa_hooks.CLOSE_ALL, note="keep t35", answered="2026-11-04T08:00:00Z")
        dump(card, path)
        code, _o, err = self.worker("close", "--card", "d-leftovers-2")
        self.assertEqual(code, 2)
        self.assertIn("card d-leftovers-2 has a note, so nothing was closed", err)
        self.assertEqual(self.ended(), [])
        code, out, err = self.sweep()
        self.assertEqual(code, 0, err)
        self.assertIn("opened card d-leftovers-3", out)
        c = pa.read_json(self.card_path("d-leftovers-3"))
        self.assertEqual((c["status"], c["answer"], c["note"]), ("open", None, None))
        self.assertEqual((c["previousCard"], c["previousAnswer"], c["previousNote"], c["previousAnswered"]),
                         ("d-leftovers-2", pa_hooks.CLOSE_ALL, "keep t35", "2026-11-04T08:00:00Z"))

    def test_l06_other_without_words_has_no_previous_note(self):
        self.ready()
        self.answer("other")
        self.worker("close", "--card", "d-leftovers")
        c = self.card()
        self.assertEqual(c["previousAnswer"], "Other")
        self.assertNotIn("previousNote", c)

if __name__ == "__main__":
    unittest.main()
