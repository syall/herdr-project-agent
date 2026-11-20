"""t-163: t-140's Recorded limits on the person's standing rule to merge and push (spec R1-R14, with the person's
answers on d-291: Q1 a failure refresh sees drops only that entry; Q2 a stacked entry's review covers the whole stack
below it; Q3 an entry rebased straight onto main is no longer stacked; Q4 a declined card holds its entries).

Run: python3 -m unittest -v tests/test_pa_standing_limits.py
Scratch repositories and a fake space only (tests/queue_fixtures.py, Standing of tests/test_pa_merge_push.py; that
module is imported, not star-imported, so its tests are not collected here). The push tool's callers are given as an
environment and an ancestor list; no real panel, agent, space or push.
"""
import ast
import json
import os
import shutil
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from queue_fixtures import *  # noqa: F401,F403
from queue_fixtures import setUpModule, tearDownModule  # noqa: F401
import test_pa_merge_push as mp  # noqa: E402
from test_pa_queue_merge import review_file  # noqa: E402

RULE = mp.RULE
EARLY, LATER = mp.EARLY, mp.LATER
T1 = "2026-11-19T10:00:00Z"
T2 = "2026-11-19T11:00:00Z"
TOOLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "runtime", "tools")


def rule_with(*history, **over):
    return mp.person_rule(history=[{"at": EARLY, "by": "person", "change": "created"}] + list(history), **over)


class Limits(mp.Standing):
    def card_answer(self, label, cid="d-queue-1", archive=False):
        self.answer(cid, label)
        if archive:
            os.rename(os.path.join(self.dir, "decisions", cid + ".json"),
                      os.path.join(self.dir, pa.ARCHIVE, cid + ".json"))

    def one_card(self, *extra):
        code, out, err = self.queue("card", "--repo", self.proj, "--limits", mp.LIMITS, *extra)
        self.assertEqual(code, 0, out + err)

    def stop_lines(self):
        return self.stops()

    def rejournal(self, *lines):
        with open(os.path.join(self.dir, "journal.jsonl"), "a", encoding="utf-8") as f:
            for line in lines:
                f.write((line if isinstance(line, str) else json.dumps(line)) + "\n")


# ---- R1 (Q4): a declined card holds its entries back from the rule ------------------------------------------

class R1Declined(Limits):
    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.dir, pa.ARCHIVE), exist_ok=True)

    def test_r1_not_now_holds_the_listed_entry(self):
        self.joined("t-A", "fa.txt")
        self.passed("t-A")
        self.one_card()
        self.card_answer("Not now")
        remote = self.remote()
        code, out, err = self.smerge()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.merged(), [], out + err)
        self.assertIn("waits at t-A: the person answered d-queue-1 'Not now' for it; open a new queue card", out)
        self.assertEqual(self.stops(), [])
        self.assertEqual(self.remote(), remote)
        self.assertIsNone(self.approval("t-A"))
        # a new card that approves the same instance lifts it: it merges as that card's
        self.one_card()
        self.card_answer("Merge all in order", cid="d-queue-2")
        code, out, err = self.smerge()
        self.assertEqual(code, 0, out + err)
        self.assertEqual([(m["task"], m.get("card"), m.get("standing")) for m in self.qrepo()["merged"]],
                         [("t-A", "d-queue-2", None)], out + err)

    def test_r1_merge_up_to_holds_the_entries_after_it(self):
        self.two()
        self.one_card()
        self.card_answer("Merge up to t-A")
        code, out, err = self.smerge()
        self.assertEqual(code, 0, out + err)
        self.assertEqual([(m["task"], m.get("card")) for m in self.qrepo()["merged"]], [("t-A", "d-queue-1")], out + err)
        self.assertIn("waits at t-B: the person answered d-queue-1 'Merge up to t-A' for it", out)
        self.assertEqual(self.standing_merged(), [])
        self.assertEqual(self.stops(), [])

    def test_r1_an_archived_card_holds(self):
        self.joined("t-A", "fa.txt")
        self.passed("t-A")
        self.one_card()
        self.card_answer("Not now", archive=True)
        code, out, err = self.smerge()
        self.assertEqual(self.merged(), [], out + err)
        self.assertIn("answered d-queue-1 'Not now'", out)

    def test_r1_a_rejoined_instance_is_not_held(self):
        self.joined("t-A", "fa.txt")
        self.passed("t-A")
        self.one_card()
        self.card_answer("Not now")
        self.refresh("--no-merge")
        code, out, err = self.queue("out", "t-A", "--repo", self.proj, "--why", "again")
        self.assertEqual(code, 0, out + err)
        self.add("t-A")
        self.passed("t-A")
        code, out, err = self.smerge()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.standing_merged(), ["t-A"], out + err)

    def test_r1_a_withdrawn_card_or_another_repository_holds_nothing(self):
        self.joined("t-A", "fa.txt")
        self.passed("t-A")
        self.one_card()
        path = os.path.join(self.dir, "decisions", "d-queue-1.json")
        c = pa.read_json(path)
        c.update(status="withdrawn")
        dump(c, path)
        code, out, err = self.smerge()
        self.assertEqual(self.standing_merged(), ["t-A"], out + err)

    def test_r1_a_card_of_another_repository_holds_nothing(self):
        self.joined("t-A", "fa.txt")
        self.passed("t-A")
        self.one_card()
        path = os.path.join(self.dir, "decisions", "d-queue-1.json")
        c = pa.read_json(path)
        c.update(status="answered", answer="Not now", answered=pa.now(), answeredBy="person, in the panel")
        c["queue"]["common"] = os.path.join(self.tmp, "elsewhere", ".git")
        dump(c, path)
        code, out, err = self.smerge()
        self.assertEqual(self.standing_merged(), ["t-A"], out + err)


# ---- R2: merge --standing applies the answered cards first --------------------------------------------------

class R2ApplyFirst(Limits):
    def test_r2_a_card_answered_since_the_last_refresh_merges_as_the_cards(self):
        self.joined("t-A", "fa.txt")
        self.passed("t-A")
        self.one_card()
        self.card_answer("Merge all in order")  # no refresh after it
        code, out, err = self.smerge()
        self.assertEqual(code, 0, out + err)
        self.assertEqual([(m["task"], m.get("card"), m.get("standing")) for m in self.qrepo()["merged"]],
                         [("t-A", "d-queue-1", None)], out + err)
        self.assertEqual(len(self.push_cards()), 1, out + err)
        self.assertNotIn("next: pa-push-main run --standing", out)


# ---- R3: a void card approval is not replaced by the rule; a plain merge that voids writes the stop -----------

class R3Void(Limits):
    def approved_then_changed(self):
        a = self.joined("t-A", "fa.txt")
        self.passed("t-A")
        self.one_card()
        self.card_answer("Merge all in order")
        self.refresh("--no-merge")
        self.change("t-A", "fa.txt", "later\n")
        return a

    def test_r3_a_plain_merge_that_voids_writes_the_stop_line(self):
        self.approved_then_changed()
        code, out, err = self.merge()
        self.assertTrue(self.approval("t-A")["void"], out + err)
        s = self.stops()
        self.assertEqual(len(s), 1, out + err)
        self.assertEqual((s[0]["rule"], s[0].get("task")), (RULE, "t-A"), s)
        self.assertIn("a commit after the check", s[0]["why"])

    def test_r3_without_a_binding_no_stop_line(self):
        args = mp.args_of(action="set", path=self.proj, space=SPACE, unbind=True)
        with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            pa_push.allow_change(args, mp.PERSON_ENV, [{"pid": 11, "comm": "/bin/zsh", "args": "-zsh"},
                                                       {"pid": 1, "comm": "launchd", "args": "launchd"}])
        self.approved_then_changed()
        code, out, err = self.merge()
        self.assertTrue(self.approval("t-A")["void"], out + err)
        self.assertEqual(self.stops(), [])

    def void_with(self, **approval):
        self.joined("t-A", "fa.txt")
        self.passed("t-A")
        self.one_card()
        self.card_answer("Merge all in order")
        self.refresh("--no-merge")
        self.set_entries(lambda r: r["entries"][0]["approval"].update(**approval))

    def test_r3_a_void_card_approval_waits_for_a_new_card(self):
        self.void_with(void="a commit after the check: by hand")
        remote = self.remote()
        code, out, err = self.smerge()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.merged(), [], out + err)
        self.assertIn("waits at t-A: its approval by d-queue-1 is void (a commit after the check: by hand); open a new "
                      "queue card", out)
        self.assertEqual(self.stops(), [])
        self.assertEqual(self.remote(), remote)
        self.assertEqual(self.approval("t-A")["card"], "d-queue-1")

    def test_r3_a_falsy_void_is_not_void(self):
        self.void_with(void="")
        code, out, err = self.smerge()
        self.assertEqual(code, 0, out + err)
        self.assertEqual([(m["task"], m.get("card")) for m in self.qrepo()["merged"]], [("t-A", "d-queue-1")], out + err)

    def test_r3_a_void_standing_approval_may_be_given_again(self):
        self.joined("t-A", "fa.txt")
        self.passed("t-A")
        self.set_entries(lambda r: r["entries"][0].update(approval={
            "standing": RULE, "textSha": "0" * 64, "answeredAt": pa.now(), "card": None, "void": "a dead run"}))
        code, out, err = self.smerge()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.standing_merged(), ["t-A"], out + err)


# ---- R4' (Q1): a failure refresh sees drops only that entry; the rule goes on ---------------------------------

class R4RefreshFailure(Limits):
    def test_r4_a_failed_suite_seen_by_refresh_writes_no_stop_line(self):
        self.joined("t-X", "fx.txt")
        self.joined("t-A", "fa.txt")
        self.suite("t-X", 1)
        code, out, err = self.refresh("--no-merge")
        self.assertEqual(code, 0, out + err)
        self.assertIn("t-X failed", out)
        self.assertEqual(self.stops(), [])
        self.assertEqual([e["task"] for e in self.entries()], ["t-A"])


# ---- R5: the stop scan fails closed on time ----------------------------------------------------------------

class R5Times(Limits):
    def setUp(self):
        super().setUp()
        self.rule = rule_with({"at": T1, "by": "person", "change": "turned on"})
        self.rules([self.rule])

    def other_card(self, answered, **extra):
        card = {"id": "d-8", "task": "t-Z", "status": "answered", "answer": "Other", "other": True, "note": "wait",
                "question": "Q?", "options": []}
        if answered is not mock.DEFAULT:
            card["answered"] = answered
        card.update(extra)
        dump(card, os.path.join(self.dir, "decisions", "d-8.json"))

    def stop_of(self):
        return pa_push.standing_stops(self.dir, self.rule)

    def test_r5_an_other_card_with_no_readable_time_counts(self):
        for answered in (mock.DEFAULT, None, "", "yesterday", "2026-11-19T12:00:00+00:00"):
            with self.subTest(answered=answered):
                self.other_card(answered)
                stop = self.stop_of()
                self.assertIsNotNone(stop, answered)
                self.assertIn("Other", stop[0])
                self.assertIn("time unreadable", stop[0])

    def test_r5_an_other_card_answered_in_the_future_counts(self):
        self.other_card(LATER)
        self.assertIsNotNone(self.stop_of())

    def test_r5_an_other_card_before_since_does_not_count(self):
        self.other_card("2026-11-19T09:59:59Z")
        self.assertIsNone(self.stop_of())

    def test_r5_a_stop_line_in_the_same_second_as_since_counts(self):
        self.rejournal({"at": T1, "by": "agent", "trigger": "standing", "standing": "stopped", "rule": RULE, "why": "x"})
        stop = self.stop_of()
        self.assertIsNotNone(stop)
        self.assertFalse(stop[1])

    def test_r5_a_stop_line_one_second_before_since_does_not_count(self):
        self.rejournal({"at": "2026-11-19T09:59:59Z", "by": "agent", "trigger": "standing", "standing": "stopped",
                        "rule": RULE, "why": "x"})
        self.assertIsNone(self.stop_of())

    def test_r5_a_stop_line_with_no_readable_time_counts(self):
        for at in (None, "", "soon"):
            with self.subTest(at=at):
                os.remove(os.path.join(self.dir, "journal.jsonl")) if os.path.exists(os.path.join(self.dir, "journal.jsonl")) else None
                line = {"by": "agent", "trigger": "standing", "standing": "stopped", "rule": RULE, "why": "x"}
                if at is not None:
                    line["at"] = at
                self.rejournal(line)
                stop = self.stop_of()
                self.assertIsNotNone(stop, at)
                self.assertIn("time unreadable", stop[0])

    def test_r5_an_other_card_in_the_same_second_as_since_counts(self):
        self.other_card(T1)
        self.assertIsNotNone(self.stop_of())

    def test_r5_a_worktree_note_with_no_readable_time_counts(self):  # t-168's previousAnswered too
        dump({"id": "d-9", "kind": "worktrees", "status": "withdrawn", "previousNote": "keep t-003", "question": "Q?",
              "options": []}, os.path.join(self.dir, "decisions", "d-9.json"))
        stop = self.stop_of()
        self.assertIsNotNone(stop)
        self.assertIn("time unreadable", stop[0])

    def test_r5_merge_standing_refuses_on_a_timeless_other_card(self):
        self.joined("t-A", "fa.txt")
        self.passed("t-A")
        self.other_card(mock.DEFAULT)
        code, out, err = self.smerge()
        self.assertEqual(code, 1, out + err)
        self.assertIn("Other", err)
        self.assertEqual(self.merged(), [])


# ---- R6: only off and on resumes ------------------------------------------------------------------------------

class R6Since(Limits):
    def since(self, *history):
        return pa_push.since_of(rule_with(*history))

    def test_r6_the_four_changes_move_since(self):
        for change in ("created", "approved", "edited and approved", "turned on"):
            with self.subTest(change=change):
                self.assertEqual(self.since({"at": T1, "by": "person", "change": change}), pa.parse_time(T1))

    def test_r6_other_changes_do_not_move_since(self):
        on = {"at": T1, "by": "person", "change": "turned on"}
        for change in ("Turned on", "turned on ", "turned on by agent", "edited", "reordered", "imported", "declined",
                       "turned off", "approved; reverted", ""):
            with self.subTest(change=change):
                self.assertEqual(self.since(on, {"at": T2, "by": "person", "change": change}), pa.parse_time(T1))
        self.assertEqual(self.since(on, {"at": T2, "by": "orchestrator", "change": "turned on"}), pa.parse_time(T1))

    def test_r6_an_entry_with_no_readable_time_is_skipped(self):
        on = {"at": T1, "by": "person", "change": "turned on"}
        for at in (None, "", "2026-11-19T12:00:00+00:00", "2026-11-19T12:00:00.5Z"):
            with self.subTest(at=at):
                late = {"by": "person", "change": "turned on"}
                if at is not None:
                    late["at"] = at
                self.assertEqual(self.since(on, late), pa.parse_time(T1))

    def test_r6_none_when_no_entry_matches(self):
        self.assertIsNone(pa_push.since_of(mp.person_rule(history=[{"at": T1, "by": "person", "change": "edited"}])))

    def test_r6_a_reorder_after_a_stop_does_not_resume_it(self):
        self.joined("t-A", "fa.txt")
        self.passed("t-A")
        self.rules([rule_with({"at": T1, "by": "person", "change": "turned on"})])
        self.rejournal({"at": T2, "by": "agent", "trigger": "standing", "standing": "stopped", "rule": RULE, "why": "x"})
        self.rules([rule_with({"at": T1, "by": "person", "change": "turned on"},
                              {"at": LATER, "by": "person", "change": "reordered"},
                              {"at": LATER, "by": "person", "change": "edited"})])
        code, out, err = self.smerge()
        self.assertEqual(code, 1, out + err)
        self.assertIn("stopped", err)
        self.assertEqual(self.merged(), [])
        self.rules([rule_with({"at": T1, "by": "person", "change": "turned on"},
                              {"at": LATER, "by": "person", "change": "turned off"},
                              {"at": LATER, "by": "person", "change": "turned on"})])
        code, out, err = self.smerge()
        self.assertEqual(self.standing_merged(), ["t-A"], out + err)

    def test_r6_a_turned_on_older_than_a_stop_does_not_lift_it(self):
        self.rules([rule_with({"at": T1, "by": "person", "change": "turned on"})])
        self.rejournal({"at": T2, "by": "agent", "trigger": "standing", "standing": "stopped", "rule": RULE, "why": "x"})
        self.assertIsNotNone(pa_push.standing_stops(self.dir, rule_with({"at": T1, "by": "person", "change": "turned on"})))


# ---- R7 (Q2): a stacked entry is reviewed against the whole stack below it --------------------------------------

class R7Stack(Limits):
    def test_r7_a_third_entry_asks_for_the_two_below_it_early(self):
        self.three()
        self.passed("t-A", "t-B", "t-C")
        need = self.stacked_need("t-C")
        self.assertIsNotNone(need, self.entry("t-C"))
        self.assertEqual((need["oldBase"], need["newBase"]), (self.m0, self.tip("t-B")), need)
        self.assertEqual(self.stacked_need("t-B")["oldBase"], self.m0)

    def test_r7_three_merged_on_the_rule_the_third_review_covers_both(self):
        self.three()
        self.passed("t-A", "t-B", "t-C")
        code, out, err = self.smerge()
        self.assertEqual(self.standing_merged(), ["t-A"], out + err)
        self.independence("t-B")
        code, out, err = self.smerge()
        self.assertEqual(self.standing_merged(), ["t-A", "t-B"], out + err)
        need = self.stacked_need("t-C")
        b = self.tip("t-B")
        self.assertEqual((need["oldBase"], need["newBase"]), (self.m0, b), need)
        self.assertIn("what it is stacked on: git diff {} {}".format(self.m0, b), out)
        files = sh(self.proj, "git", "diff", "--name-only", need["oldBase"], need["newBase"]).split()
        self.assertEqual(sorted(files), ["fa.txt", "fb.txt"])
        code, out, err = self.brief("t-C")
        self.assertIn("what it is stacked on: git diff {} {}".format(self.m0, b), out)
        self.independence("t-C")
        code, out, err = self.smerge()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.standing_merged(), ["t-A", "t-B", "t-C"], out + err)

    def test_r7_a_review_for_the_one_below_pair_does_not_count(self):
        self.three()
        self.passed("t-A", "t-B", "t-C")
        self.smerge()
        self.independence("t-B")
        self.smerge()
        a, b = self.ref("main"), self.tip("t-B")
        # t-C's stacked review recorded for the old one-below pair (t-B's change only)
        self.set_entries(lambda r: r["entries"][0]["needs"]["stacked"].update(oldBase=self.tip("t-A")))
        self.independence("t-C")
        line = [r for r in pa_hooks.review_runs(self.dir) if r.get("task") == "t-C" and r.get("standing")]
        self.assertEqual([(x["oldBase"], x["newBase"]) for x in line], [(self.tip("t-A"), b)])
        code, out, err = self.smerge()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.standing_merged(), ["t-A", "t-B"], out + err)
        self.assertIn("waits at t-C", out)
        need = self.stacked_need("t-C")
        self.assertEqual((need["oldBase"], need["newBase"]), (self.m0, b), need)
        self.assertEqual(a, b)

    def test_r7_a_card_merge_below_is_part_of_the_stack(self):
        self.three()
        self.passed("t-A", "t-B", "t-C")
        self.one_card("--max", "1")
        self.card_answer("Merge all in order")
        code, out, err = self.merge()
        self.assertEqual(self.merged(), ["t-A"], out + err)
        code, out, err = self.smerge()
        self.assertEqual(self.standing_merged(), [], out + err)
        self.independence("t-B")
        code, out, err = self.smerge()
        self.assertEqual(self.standing_merged(), ["t-B"], out + err)
        need = self.stacked_need("t-C")
        self.assertEqual((need["oldBase"], need["newBase"]), (self.m0, self.tip("t-B")), need)

    def test_r7_a_pre_ship_entry_below_ends_the_walk_with_no_pair(self):
        # Round 2 (REVIEW.md finding 1): t-B was queued before stackedOn existed; t-C stacked on it gets no partial
        # pair (t-B's change only) but waits for a card
        self.joined("t-A", "fa.txt")
        self.joined("t-B", "fb.txt")
        self.set_entries(lambda r: [e.pop("stackedOn", None) for e in r["entries"] if e["task"] == "t-B"])
        self.joined("t-C", "fc.txt")
        self.assertEqual(self.entry("t-C")["stackedOn"], {"task": "t-B", "job": None})
        self.passed("t-A", "t-B", "t-C")
        self.assertIsNone(self.stacked_need("t-C"), self.entry("t-C"))
        self.assertIsNone(pa_queue.stack_pair(self.qrepo(), self.entry("t-C")))

    def test_r7_two_entries_keep_todays_pair(self):
        a, b = self.two()
        self.smerge()
        need = self.stacked_need("t-B")
        self.assertEqual((need["oldBase"], need["newBase"]), (self.m0, a), need)


# ---- R15 (Q3): an entry rebased straight onto main is no longer stacked ------------------------------------------

class R15Unstacked(Limits):
    def test_r15_restacked_onto_main_merges_on_the_rule_after_its_checks(self):
        self.two(suites=None)
        self.suite("t-A", 1)
        code, out, err = self.refresh("--no-merge")
        self.assertEqual([e["task"] for e in self.entries()], ["t-B"], out + err)
        self.assertEqual(self.entry("t-B")["onto"], "main")
        self.assertEqual(self.gate("t-B")[0], 0)
        self.passed("t-B")
        code, out, err = self.smerge()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.standing_merged(), ["t-B"], out + err)
        self.assertNotIn("independence", out)

    def test_r15_after_the_entry_below_merged_it_is_still_stacked(self):
        a, b = self.two()
        self.smerge()
        self.assertEqual(self.entry("t-B")["onto"], "main")
        code, out, err = self.smerge()
        self.assertEqual(self.standing_merged(), ["t-A"], out + err)
        self.assertIn("waits at t-B: stacked: waiting for its independence review", out)

    def test_r15_the_entry_below_merged_and_main_moved_restack_onto_main_unstacks_it(self):
        # Round 2 (VERIFY.md RL-v1): Q3's literal reading: t-A merged, then main moved by another commit; restack
        # rebases t-B straight onto main, so it is no longer stacked: no stacked review, its suite runs again
        self.two()
        code, out, err = self.smerge()
        self.assertEqual(self.standing_merged(), ["t-A"], out + err)
        self.on_main("other.txt", "other\n")
        code, out, err = self.refresh("--no-merge")
        self.assertEqual(code, 0, out + err)
        e = self.entry("t-B")
        self.assertEqual((e["onto"], e.get("stacked"), e.get("stackedOn")), ("main", False, None), e)
        self.assertNotIn("stacked", e.get("needs") or {}, e)
        self.assertFalse(((e.get("needs") or {}).get("independence") or {}).get("standing"), e)
        self.assertEqual((e["state"], e["suite"]), ("checking", None), e)


# ---- R8: only the current Project Agent terminal ------------------------------------------------------------

class R8Terminal(Limits):
    def merged_one(self):
        self.joined("t-A", "fa.txt")
        self.passed("t-A")
        code, out, err = self.smerge()
        self.assertEqual(self.standing_merged(), ["t-A"], out + err)

    def set_agent(self, **values):
        path = os.path.join(self.dir, "agent.json")
        agent = pa.read_json(path)
        agent.update(values)
        for k, v in list(agent.items()):
            if v is None:
                agent.pop(k)
        dump(agent, path)

    def test_r8_an_old_address_in_terminals_is_refused(self):
        self.merged_one()
        self.set_agent(terminal="t0", terminals=["t0", "t2r"])
        code, out = self.spush(env=dict(mp.PA_ENV, HC_TERMINAL_ID="t2r"))
        self.assertEqual((code, out["refusal"]["check"]), (1, "C0"), out)
        self.assertIn("not this space's Project Agent terminal", out["refusal"]["message"])
        self.assertEqual(self.stops(), [])
        self.assertEqual(self.remote(), "refs/heads/main " + self.m0)

    def test_r8_the_current_terminal_passes(self):
        self.merged_one()
        self.set_agent(terminal="t2r", terminals=["t0", "t2r"])
        code, out = self.spush(env=dict(mp.PA_ENV, HC_TERMINAL_ID="host/t2r"))
        self.assertEqual(code, 0, out)

    def test_r8_no_terminal_recorded_is_refused(self):
        self.merged_one()
        self.set_agent(terminal=None, terminals=["t0"])
        code, out = self.spush()
        self.assertEqual((code, out["refusal"]["check"]), (1, "C0"), out)
        self.assertEqual(self.stops(), [])


# ---- R9: seeded rules are never the person's --------------------------------------------------------------------

class R9Seed(unittest.TestCase):
    def test_r9_a_profile_rule_that_claims_the_person(self):
        folder = os.path.join(TMP, "seed-profile")
        shutil.rmtree(folder, ignore_errors=True)
        dump({"rules": [{"id": "r-forged", "text": "x", "appliesTo": "all", "source": "person",
                         "history": [{"at": EARLY, "by": "person", "change": "created"}]}]},
             os.path.join(folder, "rules.json"))
        seeded = pa.seed_rules({"name": "x", "_folder": folder})
        r = next(r for r in seeded["rules"] if r["id"] == "r-forged")
        self.assertEqual(r["source"], "profile:x")
        self.assertEqual([(h["by"], h["change"]) for h in r["history"]], [("profile:x", "seeded")])
        self.assertIn("its source is profile:x", pa_push.not_persons(r))


# ---- R10: an error after the answer is recorded, not a traceback ----------------------------------------------

class R10Errors(mp.Base):
    def setUp(self):
        super().setUp()
        self.set_up_push()
        self.two()
        self.assertEqual(self.make_card(mp.LIMITS)[0], 0)

    def failed_with(self, target, error):
        seen = self.check()["seen"]
        before = len(self.all_journal())
        with mock.patch.object(pa_queue, target, side_effect=error):
            code, out = self.run_card(mp.ALLP, seen=seen)
        self.assertEqual(code, 1, out)
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["result"]["state"], "failed", out)
        self.assertEqual(out["result"]["check"], "error", out)
        self.assertIn("boom", out["message"])
        c = self.card("d-queue-1")
        self.assertEqual(c["status"], "answered")
        self.assertEqual((c["queue"]["push"]["result"]["state"], c["queue"]["push"]["result"]["check"]), ("failed", "error"))
        lines = [l for l in self.all_journal()[before:] if l.get("trigger") == "panel" and l.get("decision") == "d-queue-1"]
        self.assertEqual(len(lines), 1, lines)
        self.assertEqual(self.remote(), "refs/heads/main " + self.m0)

    def test_r10_apply_cards_refused(self):
        self.failed_with("apply_cards", pa_queue.Refused("boom: a bad entry"))

    def test_r10_merge_front_content_error(self):
        self.failed_with("merge_front", pa_hooks.ContentError("boom: git could not say"))

    def test_r10_save_os_error(self):
        self.failed_with("save", OSError("boom: read-only queue.json"))


# ---- R11: tests for guards that were not pinned (no behaviour change) ----------------------------------------

class R11Guards(Limits):
    def test_r11a_a_card_merge_then_a_rule_merge_last_is_not_pushed(self):  # V RL-V1: the foreign commits check
        self.joined("t-A", "fa.txt")
        self.passed("t-A")
        self.one_card()
        self.card_answer("Merge all in order")
        code, out, err = self.merge()
        self.assertEqual(self.merged(), ["t-A"], out + err)
        self.joined("t-B", "fb.txt")
        self.passed("t-B")
        code, out, err = self.smerge()
        self.assertEqual(self.standing_merged(), ["t-B"], out + err)
        code, out = self.spush()
        self.assertEqual(code, 1, out)
        self.assertIn("not merged on the rule", out["refusal"]["message"])
        self.assertEqual(len(self.stops()), 1)
        self.assertEqual(self.remote(), "refs/heads/main " + self.m0)

    def test_r11b_an_unreadable_journal_is_an_unknown_hold(self):  # V RL-V5
        if os.geteuid() == 0:
            self.skipTest("root reads a chmod 000 file")
        self.joined("t-A", "fa.txt")
        self.passed("t-A")
        path = os.path.join(self.dir, "journal.jsonl")
        os.chmod(path, 0)
        try:
            self.assertEqual(pa_push.journal_hold(self.dir), "unknown")
            with mock.patch.object(pa_push, "write_stop"):
                code, out, err = self.smerge()
                self.assertEqual(code, 1, out + err)
                self.assertIn("hold is unknown", err)
                code, out = self.spush()
                self.assertEqual((code, out["refusal"]["check"]), (1, "C0"), out)
                self.assertIn("hold is unknown", out["refusal"]["message"])
        finally:
            os.chmod(path, 0o644)
        self.assertEqual(self.merged(), [])

    def test_r11f_a_card_entry_whose_suite_has_not_ended_only_waits(self):  # V RL-r4-1 (M2)
        self.joined("t-A", "fa.txt")
        self.one_card()
        self.card_answer("Merge all in order")
        self.refresh("--no-merge")
        code, out, err = self.smerge()
        self.assertEqual(code, 0, out + err)
        self.assertIn("stopped at t-A", out)
        self.assertEqual(self.stops(), [])

    def test_r11g_a_front_entry_whose_suite_exited_1_stops_the_rule(self):  # V RL-r4-2 (M5)
        self.joined("t-A", "fa.txt")
        self.suite("t-A", 1)  # no refresh: merge --standing sees it at the front
        code, out, err = self.smerge()
        self.assertEqual(code, 1, out + err)
        s = self.stops()
        self.assertEqual(len(s), 1, out + err)
        self.assertIn("the full suite exited", s[0]["why"])
        self.assertEqual(self.merged(), [])

    def test_r11d_a_stacked_review_for_another_pair_is_renewed(self):  # V RL-r3-1 (M10, M12)
        a, b = self.two()
        self.smerge()
        self.set_entries(lambda r: r["entries"][0]["needs"]["stacked"].update(oldBase=b))
        self.independence("t-B")
        code, out, err = self.smerge()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.standing_merged(), ["t-A"], out + err)
        self.assertIn("waits at t-B: stacked: waiting for its independence review", out)
        need = self.stacked_need("t-B")
        self.assertEqual((need["oldBase"], need["newBase"]), (self.m0, a), need)

    def test_r11d_a_need_for_another_pair_is_renewed_at_the_front(self):  # V RL-V4 (M12)
        a, b = self.two()
        self.smerge()
        self.set_entries(lambda r: r["entries"][0]["needs"]["stacked"].update(oldBase=b))  # no refresh after it
        code, out, err = self.smerge()
        self.assertEqual(code, 0, out + err)
        self.assertIn("waits at t-B: stacked: waiting for its independence review", out)
        need = self.stacked_need("t-B")
        self.assertEqual((need["oldBase"], need["newBase"]), (self.m0, a), need)
        self.assertIn("what it is stacked on: git diff {} {}".format(self.m0, a), out)

    def test_r11e_a_stacked_line_is_not_the_carry_review(self):  # V RL-r3-2 (M22)
        self.joined("t-X", "fx.txt")
        self.joined("t-A", "fa.txt")
        self.joined("t-B", "fb.txt")
        self.suite("t-X", 1)
        self.suite("t-A", 0)
        self.suite("t-B", 0)
        code, out, err = self.refresh("--no-merge")
        self.assertEqual([e["task"] for e in self.entries()], ["t-A", "t-B"], out + err)
        needs = self.entry("t-B")["needs"]
        self.assertIn("independence", needs, needs)
        self.assertIn("stacked", needs, needs)
        self.assertEqual(self.gate("t-B")[0], 0)
        self.add_worker("t-B", "t-Bs", "reviewer")
        name = needs["stacked"]["file"]
        write(os.path.join(self.evidence("t-B"), name),
              review_file(self.tip("t-B"), "t-Bs", "pass").replace("Checked:", "Independent (stacked): yes\n\nChecked:"))
        code, out, err = self.refresh("--no-merge")
        self.assertEqual(code, 0, out + err)
        lines = [r for r in pa_hooks.review_runs(self.dir) if r.get("task") == "t-B" and r.get("about") == "independent"]
        self.assertEqual([bool(r.get("standing")) for r in lines], [True], lines)
        self.assertNotEqual(self.entry("t-B")["state"], "passed")
        code, out, err = self.queue("show", "--repo", self.proj)
        self.assertIn("waiting for its independence review (r-disjoint-carry)", out)

    def test_r11c_both_hold_readers_agree(self):  # F RL-8
        with open(os.path.join(TOOLS_DIR, "pa_places.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "hold_state")
        scope = {"json": json}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "pa_places.py", "exec"), scope)
        places = scope["hold_state"]
        texts = [None, "", '{"hold": "on"}\n', '{"hold": "off"}\n', '{"hold": true}\n', '{"hold": false}\n',
                 '{"hold": 0}\n', '{"hold": 1}\n', '{"hold": "on"}\n{"hold": "o', '{"x": 1}\n{"hold": "o',
                 '[1, 2]\n{"hold": "on"}\n', '{"hold": "on"}\n[1]\n', "\n\n", '{"hold": "off"}\nnot json\n']
        for text in texts:
            with self.subTest(text=text):
                self.assertEqual(places(text), pa_push.hold_state(text))


# ---- R12: nothing to push is not a stop -------------------------------------------------------------------------

class R12Nothing(Limits):
    def test_r12_no_merge_yet(self):
        code, out = self.spush()
        self.assertEqual(code, 0, out)
        self.assertIn("nothing merged on the rule; nothing to push", out["message"])
        self.assertEqual(self.stops(), [])

    def card_merge(self):
        self.joined("t-A", "fa.txt")
        self.passed("t-A")
        self.one_card()
        self.card_answer("Merge all in order")
        code, out, err = self.merge()
        self.assertEqual(self.merged(), ["t-A"], out + err)

    def test_r12_a_card_merge_already_on_the_remote(self):
        self.card_merge()
        sh(self.proj, "git", "push", "-q", "origin", "main")
        code, out = self.spush()
        self.assertEqual(code, 0, out)
        self.assertIn("already on the remote", out["message"])
        self.assertEqual(self.stops(), [])

    def test_r12_a_card_merge_not_pushed_is_refused_and_stops(self):
        self.card_merge()
        code, out = self.spush()
        self.assertEqual(code, 1, out)
        self.assertIn("not merged on the rule", out["refusal"]["message"])
        self.assertEqual(len(self.stops()), 1)
        self.assertEqual(self.remote(), "refs/heads/main " + self.m0)

    def test_r12_a_card_merge_with_the_remote_unreadable_is_refused_and_stops(self):
        self.card_merge()
        shutil.rmtree(self.origin)
        code, out = self.spush()
        self.assertEqual(code, 1, out)
        self.assertEqual(len(self.stops()), 1)


# ---- R13: "edited and approved" is the person's approval -------------------------------------------------------

class R13Approved(Limits):
    def orchestrator_rule(self, change, by="person"):
        return mp.person_rule(source="orchestrator", history=[{"at": EARLY, "by": "orchestrator", "change": "proposed"},
                                                              {"at": EARLY, "by": by, "change": change}])

    def test_r13_edited_and_approved_counts(self):
        for change in ("approved", "edited and approved"):
            with self.subTest(change=change):
                self.assertIsNone(pa_push.not_persons(self.orchestrator_rule(change)))
        self.rules([self.orchestrator_rule("edited and approved")])
        self.in_force()

    def test_r13_other_strings_do_not(self):
        for change in ("Approved", "approved ", "not approved", "edited and approved; reverted", "edited", "approved by agent"):
            with self.subTest(change=change):
                self.assertEqual(pa_push.not_persons(self.orchestrator_rule(change)), "the person never approved it")
        self.assertEqual(pa_push.not_persons(self.orchestrator_rule("approved", by="orchestrator")),
                         "the person never approved it")


# ---- R14: text ------------------------------------------------------------------------------------------------

class R14Text(unittest.TestCase):
    def source(self, name):
        with open(os.path.join(TOOLS_DIR, name), encoding="utf-8") as f:
            return f.read()

    def test_r14_no_tool_names_the_old_install_rule(self):
        for name in ("pa_push.py", "pa_queue.py"):
            text = self.source(name)
            self.assertNotIn("as r-pa-installs says", text, name)
        self.assertIn("as r-pa-installs-2 says", self.source("pa_push.py"))
        self.assertIn("as r-pa-installs-2 says", self.source("pa_queue.py"))

    def test_r14_stop_cause_has_no_dead_clause(self):
        tree = ast.parse(self.source("pa_queue.py"))
        funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        said = [n.value for n in ast.walk(funcs["merge_check"]) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        self.assertFalse([s for s in said if s.startswith("short review:")], said)
        stop = [n.value for n in ast.walk(funcs["stop_cause"]) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        self.assertNotIn("short review:", stop)


if __name__ == "__main__":
    unittest.main()
