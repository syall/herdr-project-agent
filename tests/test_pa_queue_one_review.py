"""t-170: one short review per rebase. A stacked entry whose rebase needs the shared-file short review (or the
r-disjoint-carry independence review) and the standing rule's stacked independence review gets both questions in one
pa-queue brief and one SHORT-REVIEW file, asked at the same time (one queue worker); refresh and merge accept that
file for both. No check is dropped: a file without the stacked answer counts for the other question only, and the
stacked review is then asked apart.

Run: python3 -m unittest -v tests/test_pa_queue_one_review.py
Scratch repositories and a fake space only (tests/queue_fixtures.py, Standing of tests/test_pa_merge_push.py; that
module is imported, not star-imported, so its tests are not collected here).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from queue_fixtures import *  # noqa: F401,F403
from queue_fixtures import setUpModule, tearDownModule  # noqa: F401
import test_pa_merge_push as mp  # noqa: E402
from test_pa_queue_merge import MBase, review_file  # noqa: E402

FRESH = "a fresh reviewer writes"


def joint_file(tip, reviewer, verdict="pass", lines=()):
    text = ["# short review", "", "Start: " + pa.now(), "End: " + pa.now(), "Reviewer: " + reviewer, ""]
    text += list(lines)
    text += ["", "Checked: " + tip, "Verdict: " + verdict]
    return "\n".join(text) + "\n"


class OneReview(mp.Standing):
    def moved_pair(self):
        """t-A edits a.txt line 30; t-B, started on main, edits a.txt line 100 and joins on t-A: its rebase moves
        the line numbers (the shared-file review) and it is stacked (the rule's independence review)."""
        self.task("t-A", [("a.txt", (30, "thirty"))])
        self.add("t-A")
        a = self.tip("t-A")
        self.task("t-B", [("a.txt", (100, "hundred"))])
        code, out, err = self.add("t-B")
        self.assertEqual(self.entry("t-B")["rebases"][-1]["result"], "moved", out + err)
        return a, out

    def write_review(self, tid, name, text):
        write(os.path.join(self.evidence(tid), name), text)

    def stacked_reviews(self, tid):
        return [r for r in pa_hooks.review_runs(self.dir) if r.get("task") == tid and r.get("standing")]

    def test_r1_r3_one_need_file_for_both_at_add(self):
        a, _out = self.moved_pair()
        needs = self.entry("t-B")["needs"]
        self.assertIn("shortReview", needs)
        self.assertIn("stacked", needs, needs)
        st, sr = needs["stacked"], needs["shortReview"]
        self.assertTrue(st["standing"])
        self.assertEqual(st["file"], sr["file"], needs)
        self.assertEqual(st["tip"], sr["tip"])
        self.assertEqual((st["oldBase"], st["newBase"]), (self.m0, a), st)
        self.assertEqual(st["since"], sr["since"])

    def test_r1_brief_asks_both_questions_in_one_block(self):
        a, _out = self.moved_pair()
        tip = self.tip("t-B")
        name = self.entry("t-B")["needs"]["shortReview"]["file"]
        code, out, err = self.brief("t-B")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(out.count(FRESH), 1, out)
        self.assertIn(name, out)
        self.assertNotIn("-stacked-", out)
        self.assertIn("within about 30 lines", out)  # the shared-file question
        self.assertIn("the entry it was stacked on (merged before it)", out)  # the stacked question
        self.assertIn("what it is stacked on: git diff {} {}".format(self.m0, a), out)
        self.assertIn("Independent: yes|no", out)
        self.assertEqual(out.count("the file has the lines"), 1, out)
        self.assertIn("Checked: " + tip, out)
        self.assertIn("run pa-full-suite", out)  # the suite in the same brief (one queue worker)

    def test_r2_one_file_recorded_for_both_and_the_rule_merges(self):
        a, _out = self.moved_pair()
        self.passed("t-A")
        code, out, err = self.smerge()
        self.assertEqual(self.standing_merged(), ["t-A"], out + err)
        tip = self.tip("t-B")
        name = self.entry("t-B")["needs"]["shortReview"]["file"]
        self.add_worker("t-B", "t-Bq", "reviewer")
        self.write_review("t-B", name, review_file(tip, "t-Bq", "pass", "yes"))
        self.suite("t-B", 0)
        code, out, err = self.refresh("--no-merge")
        self.assertEqual(code, 0, out + err)
        lines = [r for r in pa_hooks.review_runs(self.dir) if r.get("task") == "t-B" and r.get("tip") == tip]
        self.assertEqual(sorted((r["about"], bool(r.get("standing"))) for r in lines),
                         [("independent", True), ("moved", False)], lines)
        self.assertEqual({r["file"] for r in lines}, {name})
        st = self.stacked_reviews("t-B")[0]
        self.assertEqual((st["oldBase"], st["newBase"]), (self.m0, a))
        self.assertIs(st["independent"], True)
        self.assertEqual(self.gate("t-B")[0], 0)
        self.refresh("--no-merge")
        code, out, err = self.smerge()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.standing_merged(), ["t-A", "t-B"], out + err)
        self.assertNotIn("-stacked-", out + err)
        files = [l.get("file") for l in self.journal("standing-need")]
        self.assertFalse(any("-stacked-" in (f or "") for f in files), files)

    def test_r4_a_file_without_the_stacked_answer_counts_for_the_shared_file_only(self):
        a, _out = self.moved_pair()
        self.passed("t-A")
        self.smerge()
        tip = self.tip("t-B")
        name = self.entry("t-B")["needs"]["shortReview"]["file"]
        self.add_worker("t-B", "t-Bq", "reviewer")
        self.write_review("t-B", name, review_file(tip, "t-Bq", "pass"))  # no Independent: line
        self.suite("t-B", 0)
        code, out, err = self.refresh("--no-merge")
        self.assertEqual(code, 0, out + err)
        moved = [r for r in pa_hooks.review_runs(self.dir) if r.get("task") == "t-B" and r.get("about") == "moved"]
        self.assertEqual(len(moved), 1, out)
        self.assertEqual(self.stacked_reviews("t-B"), [])
        apart = "SHORT-REVIEW-t-B-stacked-1.md"
        self.assertEqual(self.entry("t-B")["needs"]["stacked"]["file"], apart)
        self.assertIn(apart, out)
        code, out, err = self.brief("t-B")
        self.assertIn(apart, out)
        self.assertIn("the entry it was stacked on (merged before it)", out)
        self.assertEqual(self.gate("t-B")[0], 0)
        self.refresh("--no-merge")
        code, out, err = self.smerge()
        self.assertEqual(self.standing_merged(), ["t-A"], out + err)
        self.assertIn("waiting for its independence review " + apart, out)
        self.independence("t-B")
        code, out, err = self.smerge()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.standing_merged(), ["t-A", "t-B"], out + err)

    def test_r4_a_stacked_no_in_the_one_file_stops_the_rule(self):
        self.moved_pair()
        self.passed("t-A")
        self.smerge()
        tip = self.tip("t-B")
        name = self.entry("t-B")["needs"]["shortReview"]["file"]
        self.add_worker("t-B", "t-Bq", "reviewer")
        self.write_review("t-B", name, review_file(tip, "t-Bq", "pass", "no"))
        self.suite("t-B", 0)
        self.refresh("--no-merge")
        self.assertEqual(self.gate("t-B")[0], 0)
        self.refresh("--no-merge")
        code, out, err = self.smerge()
        self.assertEqual(code, 1, out + err)
        self.assertEqual(self.standing_merged(), ["t-A"], out + err)
        self.assertIn("did not find it independent", out + err)

    def test_r3_clean_stacked_entry_brief_asks_the_suite_and_the_stacked_review(self):
        a = self.joined("t-A", "fa.txt")
        self.joined("t-B", "fb.txt")
        need = self.entry("t-B")["needs"]["stacked"]
        self.assertEqual(need["file"], "SHORT-REVIEW-t-B-stacked-1.md")
        self.assertEqual((need["oldBase"], need["newBase"]), (self.m0, a))
        code, out, err = self.brief("t-B")
        self.assertEqual(code, 0, out + err)
        self.assertIn("run pa-full-suite", out)
        self.assertIn(need["file"], out)
        self.assertIn("what it is stacked on: git diff {} {}".format(self.m0, a), out)

    def test_r1_carry_and_stacked_one_file_with_labelled_answers(self):
        self.joined("t-X", "fx.txt")
        self.joined("t-A", "fa.txt")
        self.joined("t-B", "fb.txt")
        self.suite("t-X", 1)
        self.suite("t-A", 0)
        self.suite("t-B", 0)
        code, out, err = self.refresh("--no-merge")
        self.assertEqual([e["task"] for e in self.entries()], ["t-A", "t-B"], out + err)
        needs = self.entry("t-B")["needs"]
        self.assertEqual(needs["stacked"]["file"], needs["independence"]["file"], needs)
        code, out, err = self.brief("t-B")
        self.assertEqual(out.count(FRESH), 1, out)
        self.assertIn("Independent (moved): yes|no", out)
        self.assertIn("Independent (stacked): yes|no", out)
        self.assertIn("Independent (both): yes|no", out)
        tip = self.tip("t-B")
        self.add_worker("t-B", "t-Bq", "reviewer")
        self.write_review("t-B", needs["independence"]["file"], joint_file(
            tip, "t-Bq", "pass", ["Independent (moved): yes", "Independent (stacked): no"]))
        code, out, err = self.refresh("--no-merge")
        runs = [r for r in pa_hooks.review_runs(self.dir) if r.get("task") == "t-B" and r.get("tip") == tip]
        self.assertEqual(sorted((bool(r.get("standing")), r["independent"]) for r in runs), [(False, True), (True, False)], runs)

    def test_r1_both_label_answers_both(self):
        self.joined("t-X", "fx.txt")
        self.joined("t-A", "fa.txt")
        self.joined("t-B", "fb.txt")
        self.suite("t-X", 1)
        self.suite("t-A", 0)
        self.suite("t-B", 0)
        self.refresh("--no-merge")
        tip = self.tip("t-B")
        need = self.entry("t-B")["needs"]["independence"]
        self.add_worker("t-B", "t-Bq", "reviewer")
        self.write_review("t-B", need["file"], joint_file(tip, "t-Bq", "pass", ["Independent (both): yes"]))
        self.refresh("--no-merge")
        runs = [r for r in pa_hooks.review_runs(self.dir) if r.get("task") == "t-B" and r.get("tip") == tip]
        self.assertEqual(sorted((bool(r.get("standing")), r["independent"]) for r in runs), [(False, True), (True, True)], runs)

    def test_r4_a_plain_line_in_a_carry_and_stacked_file_answers_the_carry_only(self):
        self.joined("t-X", "fx.txt")
        self.joined("t-A", "fa.txt")
        self.joined("t-B", "fb.txt")
        self.suite("t-X", 1)
        self.suite("t-A", 0)
        self.suite("t-B", 0)
        self.refresh("--no-merge")
        tip = self.tip("t-B")
        need = self.entry("t-B")["needs"]["independence"]
        self.add_worker("t-B", "t-Bq", "reviewer")
        self.write_review("t-B", need["file"], review_file(tip, "t-Bq", "pass", "yes"))
        code, out, err = self.refresh("--no-merge")
        self.assertEqual(self.stacked_reviews("t-B"), [], out)
        self.assertEqual(self.entry("t-B")["needs"]["stacked"]["file"], "SHORT-REVIEW-t-B-stacked-1.md", out)


class NoRule(MBase):
    def test_r5_no_rule_no_early_stacked_need(self):
        self.allow()
        self.task("t-A", [("a.txt", (30, "thirty"))])
        self.add("t-A")
        self.task("t-B", [("a.txt", (100, "hundred"))])
        self.add("t-B")
        needs = self.entry("t-B")["needs"]
        self.assertIn("shortReview", needs)
        self.assertNotIn("stacked", needs)
        code, out, err = self.brief("t-B")
        self.assertNotIn("stacked on (merged before it)", out)
        code, out, err = self.refresh("--no-merge")
        self.assertNotIn("stacked", self.entry("t-B")["needs"])


if __name__ == "__main__":
    unittest.main()
