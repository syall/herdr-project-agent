"""Unit tests for pa-queue (t-052 J2): the merge queue's plan, add, show, out, restack, verify and refresh, and the
gate's content check with the queue (onto from queue.json; change lines only moved, with a short review).

Run: python3 -m unittest -v tests/test_pa_queue.py (all of it; tests/run-all.sh runs it in shards, tests/unit_shards.py)
Every case runs in scratch repositories under a mkdtemp folder and a fake space (tests/queue_fixtures.py).
"""
import fcntl
import importlib.util
import json
import os
import subprocess
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from queue_fixtures import *  # noqa: F401,F403
from queue_fixtures import setUpModule, tearDownModule  # noqa: F401
from unit_shards import load_tests  # noqa: F401 (tests/run-all.sh runs this module in shards, t-078)


def review_text(tip, reviewer, start=None, end=None, verdict="pass", checked=True):
    start = start or pa.now()
    end = end or pa.now()
    lines = ["# short review", ""]
    if start != "-":
        lines.append("Start: " + start)
    lines.append("End: " + end)
    if reviewer:
        lines.append("Reviewer: " + reviewer)
    lines += ["", "- a.txt: yes, the change still means what was reviewed.", ""]
    if checked:
        lines.append("Checked: " + tip)
    if verdict:
        lines.append("Verdict: " + verdict)
    return "\n".join(lines) + "\n"


class Q1Plan(QBase):
    def test_q1_shared_file_starts_from_the_branch(self):
        self.task("t-A", [("README", "a readme\n")])
        code, out, err = self.queue("plan", "t-A", "--repo", self.proj, "--files", "README")
        self.assertEqual(code, 0, out + err)
        self.assertIn("start from main", out)
        code, out, err = self.queue("plan", "t-B", "--repo", self.proj, "--files", "README", "x.txt")
        self.assertEqual(code, 0, out + err)
        self.assertIn("start from t-A-x", out)
        code, out, err = self.queue("plan", "t-C", "--repo", self.proj, "--files", "b.txt")
        self.assertIn("start from main", out)
        planned = {p["task"]: p for p in self.qrepo()["planned"]}
        self.assertEqual(planned["t-B"]["dependsOn"], {"task": "t-A", "job": None})
        self.assertIsNone(planned["t-C"]["dependsOn"])
        self.assertEqual(len(self.journal("plan")), 3)


class Q2Q3Add(QBase):
    def test_q2_empty_queue_joins_on_main(self):
        self.task("t-A", [("a.txt", (10, "ten"))])
        tip = self.tip("t-A")
        self.add("t-A")
        e = self.entry("t-A")
        self.assertEqual((e["state"], e["onto"], e["tip"], e["rebases"]), ("checking", "main", tip, []))
        self.assertEqual(e["base"], self.ref("main"))
        self.assertEqual(e["checked"]["commit"], tip)
        self.assertEqual(len(self.journal("add")), 1)

    def test_q3_second_task_is_stacked_clean(self):
        self.task("t-A", [("a.txt", (10, "ten"))])
        self.add("t-A")
        self.task("t-B", [("b.txt", "bee\n")])
        old = self.tip("t-B")
        code, out, err = self.add("t-B")
        e = self.entry("t-B")
        self.assertEqual(e["onto"], "t-A-x")
        self.assertEqual(e["state"], "checking")
        self.assertEqual(e["rebases"][0]["result"], "clean")
        self.assertEqual(e["rebases"][0]["from"], old)
        self.assertNotEqual(self.tip("t-B"), old)
        self.assertEqual(self.base_of("t-B", "t-A-x"), self.ref("t-A-x"))
        self.assertEqual(e["base"], self.ref("t-A-x"))
        self.assertEqual(e["checked"]["commit"], old)
        self.assertEqual(len(self.journal("rebase")), 1)
        code, out, _ = self.gate("t-B")  # the gate takes onto from the queue (C14)
        self.assertEqual(code, 0, out)
        self.assertIn("same content as the checked", out)


class Q4Refusals(QBase):
    def setUp(self):
        super().setUp()
        self.task("t-A", [("a.txt", (10, "ten"))])

    def refused(self, tid, word):
        before, refs = snapshot(self.qfile()), self.refs()
        code, out, err = self.queue("add", tid, "--repo", self.proj)
        self.assertEqual(code, 1, out + err)
        self.assertIn(word, err)
        self.assertEqual(snapshot(self.qfile()), before)
        self.assertEqual(self.refs(), refs)

    def test_q4a_gate_fails(self):
        os.remove(os.path.join(self.evidence("t-A"), "REVIEW.md"))
        self.refused("t-A", "gate")

    def test_q4b_pin_not_tip(self):
        g = os.path.join(self.evidence("t-A"), "GATE.json")
        data = pa.read_json(g)
        data["commits"][0]["commit"] = "0" * 40
        dump(data, g)
        self.refused("t-A", "pin")

    def test_q4c_dirty(self):
        write(os.path.join(self.trees["t-A"], "b.txt"), "dirty\n")
        self.refused("t-A", "uncommitted")

    def test_q4d_no_checked_line(self):
        write(os.path.join(self.evidence("t-A"), "VERIFY.md"), "text\nVerdict: pass\n")
        self.refused("t-A", "Checked")

    def test_q4d2_no_checked_line_in_flight(self):
        os.remove(os.path.join(self.dir, "content", "since.json"))
        write(os.path.join(self.evidence("t-A"), "VERIFY.md"), "text\nVerdict: pass\n")
        self.assertEqual(self.gate("t-A")[0], 0)
        self.refused("t-A", "no Checked: line")

    def test_q4e_merge_commit(self):
        wt = self.trees["t-A"]
        sh(wt, "git", "checkout", "-q", "-b", "side", "main")
        write(os.path.join(wt, "s.txt"), "s\n")
        sh(wt, "git", "add", "s.txt")
        sh(wt, "git", "commit", "-q", "-m", "side")
        sh(wt, "git", "checkout", "-q", "t-A-x")
        sh(wt, "git", "merge", "-q", "--no-ff", "-m", "merge", "side")
        self.checked("t-A")
        self.gate("t-A")
        self.refused("t-A", "merge commit")

    def test_q4f_depends_on_not_ahead(self):
        self.queue("plan", "t-A", "--repo", self.proj, "--files", "a.txt")
        self.queue("plan", "t-B", "--repo", self.proj, "--files", "a.txt")
        self.task("t-B", [("b.txt", "bee\n")])
        self.refused("t-B", "t-A")

    def test_q4g_already_queued(self):
        self.add("t-A")
        self.refused("t-A", "already")

    def test_q4h_open_worker_when_a_rebase_is_needed(self):
        self.add("t-A")
        self.task("t-B", [("b.txt", "bee\n")])
        self.add_worker("t-B", "t-Bx", "build", finished=False)
        self.refused("t-B", "open worker")

    def test_q4i_risky_setting_when_a_rebase_is_needed(self):
        self.add("t-A")
        self.task("t-B", [("b.txt", "bee\n")])
        sh(self.trees["t-B"], "git", "config", "merge.x.driver", "touch {}".format(os.path.join(self.tmp, "marker")))
        self.refused("t-B", "merge.x.driver")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "marker")))


class Q5Q6Q7Decide(QBase):
    def test_q5_conflict_leaves_the_branch_unchanged(self):
        self.task("t-A", [("a.txt", (2, "two A"))])
        self.add("t-A")
        self.task("t-B", [("a.txt", (2, "two B"))])
        old = self.tip("t-B")
        code, out, err = self.add("t-B", ok=False)
        self.assertEqual(code, 1, out + err)
        self.assertIn("needs a rebase worker onto t-A-x", err)
        self.assertEqual(self.tip("t-B"), old)
        gitdir = sh(self.trees["t-B"], "git", "rev-parse", "--absolute-git-dir")
        self.assertFalse(any(os.path.exists(os.path.join(gitdir, n)) for n in ("rebase-merge", "rebase-apply")))
        self.assertEqual([e["task"] for e in self.entries()], ["t-A"])
        self.assertEqual(self.journal("refused")[-1]["task"], "t-B")

    def test_q6a_moved_joins_with_a_short_review_needed(self):
        self.task("t-A", [("a.txt", (30, "thirty"))])
        self.add("t-A")
        self.task("t-B", [("a.txt", (100, "hundred"))])
        code, out, err = self.add("t-B")
        e = self.entry("t-B")
        self.assertEqual(e["rebases"][0]["result"], "moved")
        self.assertEqual(e["state"], "checking")
        need = e["needs"]["shortReview"]
        self.assertEqual(need["tip"], self.tip("t-B"))
        self.assertEqual([f["path"] for f in need["files"]], ["a.txt"])
        self.assertTrue(need["file"].startswith("SHORT-REVIEW-t-B"))
        self.assertIn("short review", out)
        self.assertIn(need["file"], out)

    def test_q6b_changed_is_refused_and_kept_rebased(self):
        self.task("t-A", [("a.txt", (100, "hundred"))])
        self.add("t-A")
        self.task("t-B", [("a.txt", (100, "hundred")), ("c.txt", "c\n")])
        old = self.tip("t-B")
        code, out, err = self.add("t-B", ok=False)
        self.assertEqual(code, 1, out + err)
        self.assertIn("needs the R8 check on", err)
        self.assertIn("a.txt", err)
        self.assertNotEqual(self.tip("t-B"), old)
        self.assertEqual([e["task"] for e in self.entries()], ["t-A"])

    def test_q7_near_places(self):
        self.task("t-A", [("a.txt", (90, "ninety"))])
        self.add("t-A")
        self.task("t-B", [("a.txt", (100, "hundred"))])
        self.add("t-B")
        f = self.entry("t-B")["needs"]["shortReview"]["files"][0]
        self.assertTrue(f["near"], f)
        self.assertTrue(f["mainHunks"] and f["changeHunks"])

    def test_q7b_far_still_needs_a_review(self):
        self.task("t-A", [("a.txt", (10, "ten"))])
        self.add("t-A")
        self.task("t-B", [("a.txt", (150, "one fifty"))])
        self.add("t-B")
        f = self.entry("t-B")["needs"]["shortReview"]["files"][0]
        self.assertEqual(f["near"], [])


class Q6MovedOnly(QBase):
    """pa_hooks.moved_only on blob pairs."""

    def blob(self, text):
        p = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=self.proj, input=text.encode("utf-8") if isinstance(text, str) else text,
                           stdout=subprocess.PIPE, check=True)
        return p.stdout.decode().strip()

    def rec(self, base, tip, mode="100644", commits=None):
        return {"files": [{"path": "f", "status": "M", "baseMode": "100644", "tipMode": mode, "baseBlob": base, "tipBlob": tip}],
                "commits": commits or []}

    def moved(self, a, b):
        run = pa_hooks.content_git(pa_hooks.git)
        return pa_hooks.moved_only(run, self.proj, a, b)

    def test_q6c_cases(self):
        ten = "".join("l{}\n".format(i) for i in range(10))
        b0, t0 = self.blob(ten), self.blob(ten.replace("l5\n", "five\n"))
        b1, t1 = self.blob("new\n" + ten), self.blob("new\n" + ten.replace("l5\n", "five\n"))
        ok, why, shared = self.moved(self.rec(b0, t0), self.rec(b1, t1))
        self.assertTrue(ok, why)
        self.assertEqual([s["path"] for s in shared], ["f"])
        ws = self.blob("new\n" + ten.replace("l5\n", "five \n"))
        self.assertFalse(self.moved(self.rec(b0, t0), self.rec(b1, ws))[0])
        last = self.blob("new\n" + ten.replace("l9\n", "nine \n"))
        last0 = self.blob(ten.replace("l9\n", "nine\n"))
        self.assertFalse(self.moved(self.rec(b0, last0), self.rec(b1, last))[0], "trailing whitespace on the last line")
        binary = self.blob(b"\0bin\n")
        binary2 = self.blob(b"\0bin2\n")
        self.assertFalse(self.moved(self.rec(binary, t0), self.rec(binary2, t1))[0])
        self.assertFalse(self.moved(self.rec(b0, t0), self.rec(b1, t1, mode="100755"))[0])
        self.assertFalse(self.moved(self.rec(b0, t0), self.rec(b0, t1))[0], "same base, other tip: the branch changed it")
        missing = [{"sha": "1" * 40, "raw": "x", "message": "m"}]
        self.assertFalse(self.moved(self.rec(b0, t0, commits=missing), self.rec(b1, t1, commits=[{"sha": "2" * 40, "raw": "y", "message": "m"}]))[0])


class Q8Q9Review(QBase):
    def setUp(self):
        super().setUp()
        self.task("t-A", [("a.txt", (30, "thirty"))])
        self.add("t-A")
        self.task("t-B", [("a.txt", (100, "hundred"))])
        self.add("t-B")
        self.need = self.entry("t-B")["needs"]["shortReview"]
        self.path = os.path.join(self.evidence("t-B"), self.need["file"])

    def reviews(self):
        p = os.path.join(self.dir, "content", "reviews.jsonl")
        return [json.loads(l) for l in read(p).splitlines()] if os.path.exists(p) else []

    def test_q8_valid_review_is_recorded_with_its_times(self):
        self.add_worker("t-B", "t-Bs", "reviewer")
        write(self.path, review_text(self.tip("t-B"), "t-Bs"))
        self.suite("t-B", 0)
        code, out, err = self.queue("refresh", "--repo", self.proj)
        self.assertEqual(code, 0, out + err)
        lines = self.reviews()
        self.assertEqual(len(lines), 1, lines)
        r = lines[0]
        self.assertEqual((r["kind"], r["task"], r["tip"], r["verdict"], r["worker"]), ("short-review", "t-B", self.tip("t-B"), "pass", "t-Bs"))
        for k in ("started", "ended", "seconds", "tree", "fileSha256"):
            self.assertIn(k, r)
        timing = [l for l in self.journal() if l.get("timing")]
        self.assertTrue(timing)
        self.assertIn(timing[-1]["timing"]["bottleneck"], ("suite", "review"))
        code, out, err = self.queue("show", "--repo", self.proj)
        self.assertIn("bottleneck", out)
        self.queue("refresh", "--repo", self.proj)
        self.assertEqual(len(self.reviews()), 1, "recorded once")

    def test_q8_invalid_reviews_are_not_recorded(self):
        tip = self.tip("t-B")
        self.add_worker("t-B", "t-Bs", "reviewer")
        self.add_worker("t-B", "t-Bold", "reviewer", started=SINCE)
        cases = [("no start", review_text(tip, "t-Bs", start="-")),
                 ("not the tip", review_text("f" * 40, "t-Bs")),
                 ("an author", review_text(tip, "t-Bb")),
                 ("started before the rebase", review_text(tip, "t-Bold")),
                 ("no verdict", review_text(tip, "t-Bs", verdict=None)),
                 ("no reviewer", review_text(tip, None))]
        for what, text in cases:
            with self.subTest(what):
                write(self.path, text)
                code, out, err = self.queue("refresh", "--repo", self.proj)
                self.assertEqual(self.reviews(), [], what)
                self.assertEqual(self.entry("t-B")["state"], "checking")

    def test_q9_gate_and_moved(self):  # C15
        code, out, _ = self.gate("t-B")
        self.assertEqual(code, 1, out)
        self.assertIn("a short review is needed", out)
        self.assertIn("a.txt", out)
        self.add_worker("t-B", "t-Bs", "reviewer")
        write(self.path, review_text(self.tip("t-B"), "t-Bs"))
        self.queue("refresh", "--repo", self.proj)
        code, out, _ = self.gate("t-B")
        self.assertEqual(code, 0, out)
        self.assertIn("short review", out)
        good = self.reviews()
        path = os.path.join(self.dir, "content", "reviews.jsonl")
        for what, change in (("must fix", {"verdict": "must fix"}), ("started before HEAD", {"started": SINCE}),
                             ("an author", {"worker": "t-Bb"}), ("another tree", {"tree": "0" * 40})):
            with self.subTest(what):
                write(path, json.dumps(dict(good[0], **change)) + "\n")
                code, out, _ = self.gate("t-B")
                self.assertEqual(code, 1, out)

    def test_q9b_content_change_with_a_review_still_fails(self):
        self.add_worker("t-B", "t-Bs", "reviewer")
        write(self.path, review_text(self.tip("t-B"), "t-Bs"))
        self.queue("refresh", "--repo", self.proj)
        self.change("t-B", "a.txt", (150, "more"))
        code, out, _ = self.gate("t-B")
        self.assertEqual(code, 1, out)


class ChainBase(QBase):
    """Four entries A, B, C, D, each its own file, each stacked on the one ahead; gates passed at their tips."""

    def build(self, c_edits=None, c_start=None, a_edits=None):
        self.task("t-A", a_edits or [("fa.txt", "a\n")])
        self.add("t-A")
        self.task("t-B", [("fb.txt", "b\n")])
        self.add("t-B")
        self.gate("t-B")
        if c_start:
            self.task("t-C", c_edits, start=c_start)
        else:
            self.task("t-C", c_edits or [("fc.txt", "c\n")])
        self.add("t-C")
        self.gate("t-C")
        self.task("t-D", [("fd.txt", "d\n")])
        self.add("t-D")
        self.gate("t-D")
        self.set_entries(lambda r: [e.update(approval={"card": "d-q", "void": None}) for e in r["entries"]])


class Chain(ChainBase):
    def test_q10_suite_counts_for_its_own_tree(self):
        self.task("t-A", [("fa.txt", "a\n")])
        self.add("t-A")
        self.task("t-B", [("fb.txt", "b\n")])
        self.suite("t-B", 0)  # its tree before the stack
        self.add("t-B")
        self.gate("t-B")
        self.suite("t-A", 0)
        code, out, err = self.queue("refresh", "--repo", self.proj)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.entry("t-A")["state"], "passed")
        self.assertEqual(self.entry("t-B")["state"], "checking")
        code, out, err = self.queue("show", "--repo", self.proj)
        self.assertIn("mergeable: t-A\n", out)

    def test_q11_failure_restacks_the_chain(self):
        self.build()
        a_tip, a_entry = self.tip("t-A"), self.entry("t-A")
        c_checked, d_checked = self.entry("t-C")["checked"], self.entry("t-D")["checked"]
        for t in ("t-A", "t-C", "t-D"):
            self.suite(t, 0)
        self.suite("t-B", 3)
        code, out, err = self.queue("refresh", "--repo", self.proj)
        self.assertEqual(code, 0, out + err)
        self.assertEqual([e["task"] for e in self.entries()], ["t-A", "t-C", "t-D"])
        self.assertEqual(self.tip("t-A"), a_tip)
        self.assertEqual(self.entry("t-A")["approval"], a_entry["approval"])
        left = self.qrepo()["left"]
        self.assertEqual(left[-1]["task"], "t-B")
        self.assertTrue(left[-1]["approval"]["void"])
        self.assertEqual(self.base_of("t-C", "t-A-x"), a_tip)
        self.assertEqual(self.entry("t-C")["onto"], "t-A-x")
        self.assertEqual(self.base_of("t-D", "t-C-x"), self.tip("t-C"), "the chain: D on C's new tip")
        self.assertEqual(self.entry("t-D")["onto"], "t-C-x")
        self.assertEqual(self.entry("t-C")["checked"], c_checked)
        self.assertEqual(self.entry("t-D")["checked"], d_checked)
        for t in ("t-C", "t-D"):
            self.assertEqual(self.entry(t)["state"], "checking")
            self.assertIsNone(self.entry(t)["approval"].get("void"))
        self.assertEqual(self.gate("t-C")[0], 0)  # C14: a cleanly restacked stacked entry passes
        self.assertEqual(self.gate("t-D")[0], 0)
        self.assertFalse(os.path.exists(os.path.join(self.trees["t-D"], "fb.txt")), "B's commits dropped from D")

    def test_q12_dependents_leave_with_it(self):
        self.task("t-A", [("fa.txt", "a\n")])
        self.add("t-A")
        self.queue("plan", "t-B", "--repo", self.proj, "--files", "fb.txt")
        self.queue("plan", "t-C", "--repo", self.proj, "--files", "fb.txt")
        self.task("t-B", [("fb.txt", "b\n")])
        self.add("t-B")
        self.gate("t-B")
        self.task("t-C", [("fb.txt", "b c\n")], start="t-B-x")
        self.add("t-C")
        self.task("t-D", [("fd.txt", "d\n")])
        self.add("t-D")
        self.gate("t-D")
        self.suite("t-B", 1)
        code, out, err = self.queue("refresh", "--repo", self.proj)
        self.assertEqual(code, 0, out + err)
        self.assertEqual([e["task"] for e in self.entries()], ["t-A", "t-D"])
        self.assertEqual([l["task"] for l in self.qrepo()["left"]], ["t-B", "t-C"])
        self.assertEqual(self.base_of("t-D", "t-A-x"), self.tip("t-A"))

    def test_q13_conflict_in_restack_leaves(self):
        self.task("t-A", [("fa.txt", "a\n")])
        self.add("t-A")
        # C is not planned on B (no dependsOn): `out` would take a dependent with it (J3a A4), and this case is the
        # conflict in the restack of an entry built on B's branch that stays in the queue.
        self.queue("plan", "t-B", "--repo", self.proj, "--files", "a.txt")
        self.task("t-B", [("a.txt", (2, "two B"))])
        self.add("t-B")
        self.gate("t-B")
        self.task("t-C", [("a.txt", (2, "two C"))], start="t-B-x")
        self.add("t-C")
        self.task("t-D", [("fd.txt", "d\n")])
        self.add("t-D")
        self.gate("t-D")
        self.set_entries(lambda r: [e.update(approval={"card": "d-q", "void": None}) for e in r["entries"]])
        c_tip = self.tip("t-C")
        code, out, err = self.queue("out", "t-B", "--repo", self.proj, "--why", "held")
        self.assertEqual(code, 0, out + err)
        code, out, err = self.queue("restack", "--repo", self.proj)
        self.assertEqual([e["task"] for e in self.entries()], ["t-A", "t-D"])
        c = self.qrepo()["left"][-1]
        self.assertEqual((c["task"], c["why"]), ("t-C", "conflict"))
        self.assertTrue(c["approval"]["void"])
        self.assertEqual(self.tip("t-C"), c_tip)
        self.assertEqual(self.base_of("t-D", "t-A-x"), self.tip("t-A"))

    def test_q14_out_dry_run_then_restack(self):
        # A and C share a.txt (C's change lines move under A's), B shares nothing: C stays moved, D clean
        self.build(a_edits=[("fa.txt", "a\n"), ("a.txt", (30, "thirty"))], c_edits=[("a.txt", (100, "hundred"))])
        code, out, err = self.queue("out", "t-B", "--repo", self.proj, "--why", "pulled")
        self.assertEqual(code, 0, out + err)
        states = {e["task"]: e["state"] for e in self.entries()}
        self.assertEqual(states, {"t-A": "checking", "t-C": "invalidated", "t-D": "invalidated"})
        watch = (self.qfile(), os.path.join(self.dir, "content"), os.path.join(self.dir, "journal.jsonl"))
        before, refs = snapshot(*watch), self.refs()
        code, out, err = self.queue("restack", "--repo", self.proj, "--dry-run")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(snapshot(*watch), before)
        self.assertEqual(self.refs(), refs)
        lines = [l for l in out.splitlines() if "git rebase --onto" in l]
        self.assertEqual(len(lines), 2, out)
        self.assertIn("t-C-x", lines[0])
        self.assertIn("t-D-x", lines[1])
        self.assertIn("<t-C-x after its restack>", lines[1])
        self.assertIn("shared: a.txt", out)
        code, out, err = self.queue("restack", "--repo", self.proj)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.entry("t-C")["rebases"][-1]["result"], "moved")
        self.assertEqual(self.entry("t-D")["rebases"][-1]["result"], "clean")


class Q15Q16Restack(ChainBase):
    def pulled(self):
        self.build()
        self.queue("out", "t-B", "--repo", self.proj, "--why", "pulled")
        return self.tip("t-C"), self.tip("t-D")

    def stops(self, word):
        c, d = self.tip("t-C"), self.tip("t-D")
        code, out, err = self.queue("restack", "--repo", self.proj)
        self.assertEqual(code, 1, out + err)
        self.assertIn(word, err)
        self.assertEqual((self.tip("t-C"), self.tip("t-D")), (c, d))
        self.assertEqual({e["task"]: e["state"] for e in self.entries()}, {"t-A": "checking", "t-C": "invalidated", "t-D": "invalidated"})

    def test_q15a_dirty(self):
        self.pulled()
        write(os.path.join(self.trees["t-C"], "fc.txt"), "dirty\n")
        self.stops("uncommitted")

    def test_q15b_open_worker(self):
        self.pulled()
        self.add_worker("t-C", "t-Cs", "verifier", finished=False)
        self.stops("open worker")

    def test_q15c_in_progress(self):
        self.pulled()
        gitdir = sh(self.trees["t-C"], "git", "rev-parse", "--absolute-git-dir")
        write(os.path.join(gitdir, "MERGE_HEAD"), self.tip("t-C") + "\n")
        self.stops("in progress")

    def test_q15d_merge_driver(self):
        self.pulled()
        sh(self.trees["t-C"], "git", "config", "merge.x.driver", "true")
        self.stops("merge.x.driver")

    def test_q16_never_main_a_remote_hooks_rerere_or_gpg(self):
        remote = os.path.join(self.tmp, "remote.git")
        sh(self.tmp, "git", "clone", "-q", "--bare", self.proj, remote)
        sh(self.proj, "git", "remote", "add", "origin", remote)
        self.build()
        marker = os.path.join(self.tmp, "marker")
        hooks = sh(self.proj, "git", "rev-parse", "--git-path", "hooks")
        hooks = hooks if os.path.isabs(hooks) else os.path.join(self.proj, hooks)
        for name in ("pre-rebase", "post-rewrite", "post-checkout", "pre-commit"):
            write(os.path.join(hooks, name), "#!/bin/sh\necho {} >> {}\n".format(name, marker))
            os.chmod(os.path.join(hooks, name), 0o755)
        gpg = os.path.join(self.tmp, "gpg.sh")
        write(gpg, "#!/bin/sh\necho gpg >> {}\nexit 1\n".format(marker))
        os.chmod(gpg, 0o755)
        write(os.path.join(TEST_ENV["HOME"], ".gitconfig"),
              "[rerere]\n\tenabled = true\n\tautoupdate = true\n[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = {}\n".format(gpg))
        main, reflog = self.ref("main"), sh(self.proj, "git", "reflog", "show", "main")
        remote_refs = sh(remote, "git", "for-each-ref")
        self.queue("out", "t-B", "--repo", self.proj, "--why", "pulled")
        code, out, err = self.queue("restack", "--repo", self.proj)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.base_of("t-C", "t-A-x"), self.tip("t-A"))
        self.assertEqual(self.ref("main"), main)
        self.assertEqual(sh(self.proj, "git", "reflog", "show", "main"), reflog)
        self.assertEqual(sh(remote, "git", "for-each-ref"), remote_refs)
        self.assertFalse(os.path.exists(marker), read(marker) if os.path.exists(marker) else "")
        common = sh(self.proj, "git", "rev-parse", "--path-format=absolute", "--git-common-dir")
        self.assertFalse(os.path.exists(os.path.join(common, "rr-cache")))


class Q17HandMerges(QBase):
    def setUp(self):
        super().setUp()
        self.task("t-A", [("fa.txt", "a\n")])
        self.add("t-A")
        self.task("t-B", [("fb.txt", "b\n")])
        self.add("t-B")
        self.gate("t-B")

    def test_q17a_hand_fast_forward_is_read_in_queue_order(self):
        sh(self.proj, "git", "merge", "-q", "--ff-only", "t-A-x")
        code, out, err = self.queue("refresh", "--repo", self.proj)
        self.assertEqual(code, 0, out + err)
        self.assertEqual([e["task"] for e in self.entries()], ["t-B"])
        m = self.qrepo()["merged"][-1]
        self.assertEqual((m["task"], m["by"]), ("t-A", "hand"))
        self.assertEqual(self.entry("t-B")["onto"], "main")
        self.assertNotEqual(self.entry("t-B")["state"], "invalidated")
        self.assertEqual(self.journal("hand-merge")[-1]["task"], "t-A")

    def test_q17b_order_broken(self):
        self.set_entries(lambda r: r["entries"].reverse())
        sh(self.proj, "git", "merge", "-q", "--ff-only", "t-A-x")
        before = snapshot(self.qfile())
        code, out, err = self.queue("refresh", "--repo", self.proj)
        self.assertEqual(code, 1, out + err)
        self.assertIn("order", err)
        self.assertEqual(snapshot(self.qfile()), before)

    def test_q17c_main_rewound(self):
        first = self.ref("main")
        self.on_main("README", "moved\n")
        self.queue("refresh", "--repo", self.proj)  # mainAt is the new main now
        sh(self.proj, "git", "reset", "-q", "--hard", first)
        before = snapshot(self.qfile())
        code, out, err = self.queue("refresh", "--repo", self.proj)
        self.assertEqual(code, 1, out + err)
        self.assertIn("by hand", err)
        self.assertEqual(snapshot(self.qfile()), before)


class Q18Q19Callers(QBase):
    def test_q18_worker_shells_are_refused(self):
        self.task("t-A", [("fa.txt", "a\n")])
        inside = os.path.join(self.tmp, "w")
        dump({"task": "t-A", "space": SPACE}, os.path.join(inside, ".claude", "pa-task.json"))
        os.chdir(inside)
        code, out, err = self.queue("add", "t-A", "--repo", self.proj)
        self.assertEqual(code, 1, out + err)
        self.assertIn("worker", err)
        os.chdir(self.tmp)
        os.environ["HC_TERMINAL_ID"] = "t-Ab"
        try:
            code, out, err = self.queue("add", "t-A", "--repo", self.proj)
        finally:
            os.environ.pop("HC_TERMINAL_ID", None)
        self.assertEqual(code, 1, out + err)
        self.assertIn("worker", err)
        self.assertFalse(os.path.exists(self.qfile()))

    def test_q19a_lock_held(self):
        self.task("t-A", [("fa.txt", "a\n")])
        pa_queue.LOCK_WAIT = 0.3
        with open(os.path.join(self.dir, ".queue.lock"), "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            code, out, err = self.queue("add", "t-A", "--repo", self.proj)
        self.assertEqual(code, 1, out + err)
        self.assertIn("another pa-queue", err)
        self.assertFalse(os.path.exists(self.qfile()))

    def test_q19b_concurrent_adds(self):
        self.task("t-A", [("fa.txt", "a\n")])
        self.task("t-B", [("fb.txt", "b\n")])
        script = os.path.join(TOOLS, "pa_queue.py")
        procs = [subprocess.Popen([sys.executable, script, "add", t, "--repo", self.proj, "--space", SPACE], cwd=self.tmp,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True) for t in ("t-A", "t-B")]
        results = [p.communicate(timeout=120) + (p.returncode,) for p in procs]
        for out, err, code in results:
            self.assertEqual(code, 0, out + err)
        self.assertEqual(sorted(e["task"] for e in self.entries()), ["t-A", "t-B"])
        self.assertEqual(sorted(l["task"] for l in self.journal("add")), ["t-A", "t-B"])


class Q20Show(QBase):
    def test_q20_show_derives_and_writes_nothing(self):
        self.task("t-A", [("fa.txt", "a\n")])
        self.add("t-A")
        self.on_main("README", "moved\n")
        before = snapshot(self.qfile(), os.path.join(self.dir, "journal.jsonl"))
        code, out, err = self.queue("show", "--repo", self.proj, "--json")
        self.assertEqual(code, 0, out + err)
        shown = json.loads(out)
        e = shown["entries"][0]
        self.assertEqual((e["state"], e["now"]["state"]), ("checking", "invalidated"))
        self.assertEqual(snapshot(self.qfile(), os.path.join(self.dir, "journal.jsonl")), before)


class Q21GateOnto(QBase):
    def test_q21_branch_out_of_the_queue_is_against_main(self):  # C14
        self.task("t-A", [("fa.txt", "a\n")])
        self.add("t-A")
        self.task("t-B", [("fb.txt", "b\n")])
        self.add("t-B")
        self.assertEqual(self.gate("t-B")[0], 0)
        self.queue("out", "t-B", "--repo", self.proj, "--why", "pulled")
        code, out, _ = self.gate("t-B")
        self.assertEqual(code, 1, out)
        self.assertIn("fa.txt", out)


class Q23Rejoin(QBase):
    """MF-J2-1 (review round 1, probe F): a task refused for a conflict, or a 'changed' rebase add keeps rebased, gets
    the check R8 asks for on the last entry's branch and joins with pa-queue add again (spec-amendment R3 step 3, R8);
    the gate and add agree on what was checked (the record's onto is the entry's branch it sits on)."""

    def recheck(self, tid):
        self.add_worker(tid, tid + "v2", "verifier")
        self.checked(tid)
        code, out, _err = self.gate(tid)
        self.assertEqual(code, 0, out)

    def rebase_worker(self, tid, onto, f, line, text):
        wt = self.trees[tid]
        subprocess.run(["git", "rebase", onto], cwd=wt, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        write(os.path.join(wt, f), line_edit(LINES, line, text))
        sh(wt, "git", "add", f)
        sh(wt, "git", "-c", "core.editor=true", "rebase", "--continue")

    def stored_onto(self, tid):
        tip = self.tip(tid)
        return [r.get("onto") for r in pa_hooks.stored_records(self.dir, tid) if r.get("commit") == tip]

    def test_q23a_conflict_rebase_worker_then_add_joins(self):  # probe F f1
        self.task("t-A", [("a.txt", (2, "two A"))])
        self.add("t-A")
        self.task("t-B", [("a.txt", (2, "two B")), ("fb.txt", "b\n")])
        self.checked_tip = self.tip("t-B")
        code, out, err = self.add("t-B", ok=False)
        self.assertIn("needs a rebase worker onto t-A-x", err)
        self.rebase_worker("t-B", "t-A-x", "a.txt", 2, "two AB")
        self.assertEqual(self.base_of("t-B", "t-A-x"), self.tip("t-A"))
        self.recheck("t-B")
        self.assertEqual(set(self.stored_onto("t-B")), {"t-A-x"})
        tip = self.tip("t-B")
        code, out, err = self.add("t-B")
        e = self.entry("t-B")
        self.assertEqual([x["task"] for x in self.entries()], ["t-A", "t-B"])
        self.assertEqual(e["state"], "checking")
        self.assertEqual(e["onto"], "t-A-x")
        self.assertEqual(e["tip"], tip)
        self.assertEqual(e["checked"]["base"], self.tip("t-A"))
        self.assertEqual(e["rebases"], [])
        self.assertEqual(self.gate("t-B")[0], 0)
        code, out, err = self.queue("verify", "t-B", "--repo", self.proj)
        self.assertEqual(code, 0, out + err)
        self.assertIn("clean", out)

    def test_q23b_changed_kept_rebased_then_add_joins(self):  # probe F f2
        self.task("t-A", [("a.txt", (100, "hundred"))])
        self.add("t-A")
        self.task("t-B", [("a.txt", (100, "hundred")), ("c.txt", "c\n")])
        code, out, err = self.add("t-B", ok=False)
        self.assertIn("needs the R8 check on", err)
        self.recheck("t-B")
        self.assertEqual(set(self.stored_onto("t-B")), {"t-A-x"})
        tip = self.tip("t-B")
        self.add("t-B")
        e = self.entry("t-B")
        self.assertEqual([x["task"] for x in self.entries()], ["t-A", "t-B"])
        self.assertEqual((e["state"], e["onto"], e["tip"]), ("checking", "t-A-x", tip))

    def test_q23c_rejoins_after_another_entry_joined_behind_it(self):
        """The rebase worker put it on t-A-x; t-C joined after: add rebases it onto t-C-x, clean."""
        self.task("t-A", [("a.txt", (2, "two A"))])
        self.add("t-A")
        self.task("t-B", [("a.txt", (2, "two B"))])
        self.add("t-B", ok=False)
        self.rebase_worker("t-B", "t-A-x", "a.txt", 2, "two AB")
        self.recheck("t-B")
        self.task("t-C", [("fc.txt", "c\n")])
        self.add("t-C")
        self.assertEqual(self.gate("t-B")[0], 0)
        code, out, err = self.add("t-B")
        e = self.entry("t-B")
        self.assertEqual([x["task"] for x in self.entries()], ["t-A", "t-C", "t-B"])
        self.assertEqual((e["state"], e["onto"]), ("checking", "t-C-x"))
        self.assertEqual(e["rebases"][0]["result"], "clean")
        self.assertEqual(self.base_of("t-B", "t-C-x"), self.tip("t-C"))

    def test_q23e_a_gate_run_before_the_r8_check_does_not_pin_main(self):
        """pa-gate runs between the refusal and the R8 check (VERIFY.md still names the old tip, on main): it stores
        the kept-rebased tip's record against main; after the check add still joins (the record on t-A-x counts)."""
        self.task("t-A", [("a.txt", (100, "hundred"))])
        self.add("t-A")
        self.task("t-B", [("a.txt", (100, "hundred")), ("c.txt", "c\n")])
        self.add("t-B", ok=False)
        self.assertEqual(self.gate("t-B")[0], 1)
        self.assertIn("main", self.stored_onto("t-B"))
        self.recheck("t-B")
        tip = self.tip("t-B")
        self.add("t-B")
        e = self.entry("t-B")
        self.assertEqual((e["state"], e["onto"], e["tip"]), ("checking", "t-A-x", tip))
        self.assertEqual(e["checked"]["base"], self.tip("t-A"))

    def test_q23f_gate_after_a_restack_uses_the_record_the_check_made(self):
        """As Q23e, then t-A leaves and t-B is restacked onto main, clean: the gate compares the checked commit on the
        record the R8 check made (against t-A-x), not the earlier gate run's record against main."""
        self.task("t-A", [("a.txt", (100, "hundred"))])
        self.add("t-A")
        self.task("t-B", [("a.txt", (100, "hundred")), ("c.txt", "c\n")])
        self.add("t-B", ok=False)
        self.gate("t-B")
        self.recheck("t-B")
        checked = self.tip("t-B")
        self.add("t-B")
        code, out, err = self.queue("out", "t-A", "--repo", self.proj, "--why", "pulled")
        self.assertEqual(code, 0, out + err)
        code, out, err = self.queue("restack", "--repo", self.proj)
        self.assertEqual(code, 0, out + err)
        e = self.entry("t-B")
        self.assertEqual((e["onto"], e["rebases"][-1]["result"]), ("main", "clean"))
        self.assertNotEqual(self.tip("t-B"), checked)
        code, out, _err = self.gate("t-B")
        self.assertEqual(code, 0, out)

    def test_q23d_task_on_main_still_checked_against_main(self):
        """A task not queued that does not sit on an entry's branch: the record stays against main (J1)."""
        self.task("t-A", [("fa.txt", "a\n")])
        self.add("t-A")
        self.task("t-B", [("fb.txt", "b\n")])
        self.assertEqual(set(self.stored_onto("t-B")), {"main"})


class Q22Install(unittest.TestCase):
    def test_q22_install_and_parse(self):
        lines = [l for l in read(os.path.join(REPO, "install.sh")).splitlines() if "pa_queue.py" in l]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("for n in pa-queue; do", lines[0])
        line = next(l for l in read(os.path.join(REPO, "tests", "run-all.sh")).splitlines() if "parses as Python 3.7" in l)
        self.assertIn("runtime/tools/pa_queue.py", line)


def load_pa_suite():
    """runtime/tools/pa_suite.py as pa-full-suite loads it (pa_hooks.cmd_full_suite): its own module, given pa."""
    spec = importlib.util.spec_from_file_location("pa_suite", os.path.join(TOOLS, "pa_suite.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.pa = pa
    return module


pa_suite = load_pa_suite()


def t056_suite(test, tid, code, moved=False, untracked=None):
    """One suites.jsonl line as t-056's pa-full-suite writes it (pa_suite.full_suite): pins taken by pa_suite.pins,
    the extra keys run/start/end/waited/load/parts, `moved: true` when the worktree changed during the run, written by
    pa_hooks.suite_record. untracked overrides the count pins found (the files themselves are not made)."""
    h = types.SimpleNamespace(**vars(pa_hooks))
    found = pa_suite.pins(h, test.trees[tid])
    if untracked is not None:
        found = [dict(p, untracked=untracked) for p in found]
    extra = {"run": "r" * 12, "start": "2026-11-18T10:00:00Z", "end": "2026-11-18T10:00:10Z", "waited": 0.0,
             "load": None, "parts": pa_suite.parse_parts("  unit: ok (10s)\n")}
    if moved:
        extra["moved"] = True
    pa_hooks.suite_record(test.dir, {"task": tid, "root": test.trees[tid]}, "full", code, 10.0, found, extra)


class V8SuiteRuns(QBase):
    def test_suite_runs_reads_both_shapes(self):
        """J1's line and t-056's real line (pa_suite.full_suite's keys: start/end, parts {name, ok, seconds},
        untracked, moved) come out in one shape; the t-056 times are its own start and end. A J1 line has no untracked
        count, so it is not reusable (pa_suite.clean's rule; t-052 J3a A6, Q-J3-6)."""
        self.task("t-A", [("fa.txt", "a\n")])
        os.makedirs(os.path.join(self.dir, "content"), exist_ok=True)
        j1 = {"at": "2026-11-18T10:00:10Z", "task": "t-A", "worker": "t1", "repo": "/r", "commit": "c", "tree": "t",
              "uncommitted": 0, "exit": 0, "seconds": 10.0, "command": "x"}
        write(os.path.join(self.dir, "content", "suites.jsonl"), json.dumps(j1) + "\nnot json\n")
        t056_suite(self, "t-A", 0)
        t056_suite(self, "t-A", 0, moved=True)
        t056_suite(self, "t-A", 0, untracked=2)
        lines = [json.loads(l) for l in read(os.path.join(self.dir, "content", "suites.jsonl")).splitlines()[2:]]
        self.assertIn("start", lines[0])
        self.assertNotIn("started", lines[0])
        runs = pa_hooks.suite_runs(self.dir)
        self.assertEqual(len(runs), 4)
        self.assertEqual([r["started"] for r in runs], ["2026-11-18T10:00:00Z"] * 4)
        self.assertEqual([r["ended"] for r in runs], ["2026-11-18T10:00:10Z"] * 4)
        self.assertEqual([r["seconds"] for r in runs], [10.0] * 4)
        self.assertIsNone(runs[0]["parts"])
        self.assertEqual(runs[1]["parts"], [{"name": "unit", "ok": True, "seconds": 10}])
        self.assertEqual([r["moved"] for r in runs], [False, False, True, False])
        self.assertEqual([r["untracked"] for r in runs], [None, 0, 0, 2])
        self.assertEqual([r["reusable"] for r in runs], [False, True, False, False])  # J3a A6: J1's line has no untracked count


class Q24NonReusableSuite(QBase):
    """Short review J2 MF-1: a t-056 run marked moved (or with untracked files) is never a pass for its tree."""

    def setUp(self):
        super().setUp()
        self.task("t-A", [("fa.txt", "a\n")])
        self.add("t-A")

    def refresh(self):
        code, out, err = self.queue("refresh", "--repo", self.proj)
        self.assertEqual(code, 0, out + err)
        return self.queue("show", "--repo", self.proj)[1]

    def test_q24a_moved_run_is_not_the_trees_pass(self):
        t056_suite(self, "t-A", 0, moved=True)
        out = self.refresh()
        self.assertEqual(self.entry("t-A")["state"], "checking")
        self.assertNotIn("mergeable: t-A\n", out)
        t056_suite(self, "t-A", 0)
        out = self.refresh()
        self.assertEqual(self.entry("t-A")["state"], "passed")
        self.assertIn("mergeable: t-A\n", out)

    def test_q24b_run_with_untracked_files_is_not_the_trees_pass(self):
        t056_suite(self, "t-A", 0, untracked=2)
        self.refresh()
        self.assertEqual(self.entry("t-A")["state"], "checking")

    def test_q24c_moved_failing_run_does_not_fail_the_entry(self):
        """A moved run did not run on this tree throughout: its exit says nothing about the tree either way."""
        t056_suite(self, "t-A", 3, moved=True)
        self.refresh()
        self.assertEqual(self.entry("t-A")["state"], "checking")

    def test_q24d_clean_t056_run_passes(self):
        t056_suite(self, "t-A", 0)
        self.refresh()
        self.assertEqual(self.entry("t-A")["state"], "passed")


# ---- t-052 J3a: the J2 limits assigned to J3 (plan.md 'J3-J5', JJ.2) ----------------------------------------------

class A1OntoAhead(QBase):
    """RL-J2-1: an entry's onto counts for the gate only when it is main or the branch of the entry directly ahead."""

    def test_a1_gate_after_out_is_against_main_until_restack(self):
        self.task("t-A", [("fa.txt", "a\n")])
        self.add("t-A")
        self.task("t-B", [("fb.txt", "b\n")])
        self.add("t-B")
        self.gate("t-B")
        self.task("t-C", [("fc.txt", "c\n")])
        self.add("t-C")
        self.assertEqual(self.gate("t-C")[0], 0)
        code, out, err = self.queue("out", "t-B", "--repo", self.proj, "--why", "held")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.entry("t-C")["onto"], "t-B-x")  # restack has not run yet
        code, out, _ = self.gate("t-C")
        self.assertEqual(code, 1, out)
        self.assertIn("fb.txt", out)
        code, out, err = self.queue("restack", "--repo", self.proj)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.entry("t-C")["onto"], "t-A-x")
        code, out, _ = self.gate("t-C")
        self.assertEqual(code, 0, out)

    def test_a1b_onto_for_the_first_entry_is_main_only(self):
        self.task("t-A", [("fa.txt", "a\n")])
        self.add("t-A")
        common = self.qrepo()["common"]
        self.assertEqual(pa_hooks.content_onto(self.dir, "t-A", common, "t-A-x"), "main")
        self.set_entries(lambda r: r["entries"][0].update(onto="t-gone-x"))
        self.assertIsNone(pa_hooks.content_onto(self.dir, "t-A", common, "t-A-x"))


class A2DependentUpstream(QBase):
    """RL-J2-3: a task started on its parent's branch, whose parent was restacked before it joined, replays only its
    own commits (the upstream is the parent's recorded old tip, not the merge-base)."""

    def test_a2_dependent_replays_only_its_own_commits(self):
        self.task("t-A", [("a.txt", (30, "x")), ("a.txt", (30, "z"))])
        self.add("t-A")
        old = self.tip("t-A")
        code, out, err = self.queue("plan", "t-B", "--repo", self.proj, "--files", "a.txt", "fb.txt")
        self.assertEqual(code, 0, out + err)
        self.assertIn("start from t-A-x", out)
        self.task("t-B", [("fb.txt", "b\n")], start="t-A-x")
        self.on_main("b.txt", "b two\n")
        code, out, err = self.queue("restack", "--repo", self.proj)
        self.assertEqual(code, 0, out + err)
        self.assertNotEqual(self.ref("t-A-x"), old)
        code, out, err = self.add("t-B")
        e = self.entry("t-B")
        self.assertEqual(e["rebases"][-1]["result"], "clean", out + err)
        line = self.journal("rebase")[-1]
        self.assertEqual((line["task"], line["base"]), ("t-B", old))
        self.assertEqual(sh(self.proj, "git", "rev-list", "--count", "t-A-x..t-B-x"), "1")
        self.assertEqual(self.base_of("t-B", "t-A-x"), self.ref("t-A-x"))


class A3ReviewMatch(QBase):
    """RL-J2-V1, Nit-J2-1: derive takes a short review record only for the entry's task, job, repository, tip and tree."""

    def setUp(self):
        super().setUp()
        self.task("t-A", [("a.txt", (30, "thirty"))])
        self.add("t-A")
        self.task("t-B", [("a.txt", (100, "hundred"))])
        self.add("t-B")
        need = self.entry("t-B")["needs"]["shortReview"]
        self.add_worker("t-B", "t-Bs", "reviewer")
        write(os.path.join(self.evidence("t-B"), need["file"]), review_text(self.tip("t-B"), "t-Bs"))
        self.suite("t-B", 0)
        code, out, err = self.queue("refresh", "--repo", self.proj)
        self.assertEqual(code, 0, out + err)
        self.path = os.path.join(self.dir, "content", "reviews.jsonl")
        self.good = json.loads(read(self.path).splitlines()[0])

    def review_now(self):
        code, out, err = self.queue("show", "--repo", self.proj, "--json")
        self.assertEqual(code, 0, out + err)
        return next(e for e in json.loads(out)["entries"] if e["task"] == "t-B")["now"]["review"]

    def test_a3_the_matching_record_is_taken(self):
        self.assertEqual(self.review_now()["tip"], self.tip("t-B"))

    def test_a3_other_tree_job_or_repository_is_not_taken(self):
        for what, change in (("another tree", {"tree": "0" * 40}), ("another job", {"job": "j9"}),
                             ("another repository", {"common": "/elsewhere/.git"})):
            with self.subTest(what):
                write(self.path, json.dumps(dict(self.good, **change)) + "\n")
                self.assertIsNone(self.review_now(), what)


class A4OutDependents(QBase):
    """D-J2-7 (Q-J3-8): out takes the entries built on the one taken out, as a failure does."""

    def test_a4_out_takes_dependents(self):
        self.task("t-A", [("fa.txt", "a\n")])
        self.add("t-A")
        self.queue("plan", "t-B", "--repo", self.proj, "--files", "fb.txt")
        self.queue("plan", "t-C", "--repo", self.proj, "--files", "fb.txt")
        self.task("t-B", [("fb.txt", "b\n")])
        self.add("t-B")
        self.gate("t-B")
        self.task("t-C", [("fb.txt", "b c\n")], start="t-B-x")
        self.add("t-C")
        self.task("t-D", [("fd.txt", "d\n")])
        self.add("t-D")
        code, out, err = self.queue("out", "t-B", "--repo", self.proj, "--why", "held")
        self.assertEqual(code, 0, out + err)
        self.assertEqual([e["task"] for e in self.entries()], ["t-A", "t-D"])
        left = self.qrepo()["left"]
        self.assertEqual([l["task"] for l in left], ["t-B", "t-C"])
        self.assertIn("t-B", left[1]["why"])
        self.assertEqual(self.entry("t-D")["state"], "invalidated")
        self.assertEqual(self.entry("t-A")["state"], "checking")


class A5JobWorktree(QBase):
    """Job entries: a job whose worktree is not the ledger's task.worktree is refused at add, with the reason; an
    entry_below of a sibling job is not skipped (Nit-J2-R2-1)."""

    def job_tree(self):
        self.task("t-J", [("fa.txt", "a\n")])
        wt2 = os.path.join(self.tmp, "wt-t-J-2")
        sh(self.proj, "git", "worktree", "add", "-q", "-b", "t-J-y", wt2, "main")
        write(os.path.join(wt2, "fj.txt"), "j\n")
        sh(wt2, "git", "add", "fj.txt")
        sh(wt2, "git", "commit", "-q", "-m", "job two")
        led = self.ledger()
        t = next(t for t in led["tasks"] if t["id"] == "t-J")
        t["worktrees"] = [{"path": self.trees["t-J"], "job": "j1"}, {"path": wt2, "job": "j2"}]
        self.set_ledger(led)
        return wt2

    def test_a5_job_outside_the_task_worktree_is_refused(self):
        self.job_tree()
        before = snapshot(self.qfile())
        code, out, err = self.queue("add", "t-J", "--job", "j2", "--repo", self.proj)
        self.assertEqual(code, 1, out + err)
        self.assertIn("the gate reads", err)
        self.assertIn("worktree", err)
        self.assertEqual(snapshot(self.qfile()), before)

    def test_a5_job_that_is_the_task_worktree_joins(self):
        wt2 = self.job_tree()
        led = self.ledger()
        next(t for t in led["tasks"] if t["id"] == "t-J")["worktree"] = wt2
        self.set_ledger(led)
        self.trees["t-J"] = wt2
        self.checked("t-J")
        self.assertEqual(self.gate("t-J")[0], 0)
        code, out, err = self.queue("add", "t-J", "--job", "j2", "--repo", self.proj)
        self.assertEqual(code, 0, out + err)
        self.assertEqual((self.entries()[0]["task"], self.entries()[0]["job"]), ("t-J", "j2"))

    def test_a5b_entry_below_skips_only_the_same_job(self):
        self.task("t-A", [("fa.txt", "a\n")])
        self.change("t-A", "fb.txt", "b\n")
        commit = self.tip("t-A")
        below = sh(self.proj, "git", "rev-parse", commit + "^")
        sh(self.proj, "git", "branch", "t-A-j1", below)
        rec = {"main": "main"}
        entries = [{"task": "t-A", "job": "j1", "branch": "t-A-j1"}]
        self.assertEqual(pa_hooks.entry_below(pa_hooks.git, self.proj, rec, entries, "t-A", commit, job="j2"), "t-A-j1")
        self.assertIsNone(pa_hooks.entry_below(pa_hooks.git, self.proj, rec, entries, "t-A", commit, job="j1"))



# ---- t-052 J3c: d-166 doc-only conflicts (decide's `union`, plan JJ.4) ------------------------------------

def union_text(first, second, line=2):
    """LINES with line `line` replaced by two lines: what git merge-file --union gives for a conflict there."""
    lines = LINES.splitlines(True)
    lines[line - 1] = first + "\n" + second + "\n"
    return "".join(lines)


APPROVAL = {"card": "d-q1", "answeredBy": "person", "at": "2000-01-03T00:00:00Z"}


class UBase(QBase):
    """A rebase worker resolves a conflict by hand (the tool never does, AC18.1)."""

    def resolve(self, tid, onto, f, text, extra=None):
        wt = self.trees[tid]
        r = subprocess.run(["git", "rebase", onto], cwd=wt, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertNotEqual(r.returncode, 0, "the rebase was expected to conflict")
        write(os.path.join(wt, f), text)
        sh(wt, "git", "add", f)
        if extra:
            write(os.path.join(wt, extra[0]), extra[1])
            sh(wt, "git", "add", extra[0])
        sh(wt, "git", "-c", "core.editor=true", "rebase", "--continue")
        gitdir = sh(wt, "git", "rev-parse", "--absolute-git-dir")
        self.assertFalse(any(os.path.exists(os.path.join(gitdir, n)) for n in ("rebase-merge", "rebase-apply")))
        return self.tip(tid)

    def verify(self, tid):
        return self.queue("verify", tid, "--repo", self.proj)

    def review(self, tid, verdict="pass", reviewer=None):
        reviewer = reviewer or tid + "s"
        self.add_worker(tid, reviewer, "reviewer")
        need = self.entry(tid)["needs"]["shortReview"] if any(e["task"] == tid for e in self.entries()) else None
        name = need["file"] if need else "SHORT-REVIEW-{}-1.md".format(tid)
        write(os.path.join(self.evidence(tid), name), review_text(self.tip(tid), reviewer, verdict=verdict))
        return name


class U1AddConflict(UBase):
    """B's add conflicts with A on a.txt line 2 (a docstring line, say); a rebase worker keeps both sides."""

    def setUp(self):
        super().setUp()
        self.task("t-A", [("a.txt", (2, "two A"))])
        self.add("t-A")
        self.task("t-B", [("a.txt", (2, "two B")), ("fb.txt", "b\n")])
        self.checked_tip = self.tip("t-B")
        code, out, err = self.add("t-B", ok=False)
        self.assertIn("needs a rebase worker onto t-A-x", err)

    def test_u1_both_sides_kept_is_union_with_a_doc_only_review(self):
        tip = self.resolve("t-B", "t-A-x", "a.txt", union_text("two B", "two A"))
        code, out, err = self.verify("t-B")
        self.assertEqual(code, 0, out + err)
        self.assertIn(": union", out)
        self.assertIn("SHORT-REVIEW-t-B-1.md", out)
        self.assertIn("docstrings or comments", out)
        self.assertIn("a.txt", out)
        self.assertIn("lines 2-3", out)
        self.assertIn(tip, out)
        self.assertEqual(self.journal("verify")[-1]["result"], "union")

    def test_u1b_union_only_unit(self):
        self.resolve("t-B", "t-A-x", "a.txt", union_text("two B", "two A"))
        run = pa_hooks.content_git(pa_hooks.git)
        checked = pa_hooks.content_record(run, self.proj, self.checked_tip, "main", self.ref("main"))
        now = pa_hooks.content_record(run, self.proj, self.tip("t-B"), "t-A-x")
        self.assertFalse(pa_hooks.moved_only(run, self.proj, checked, now)[0])
        ok, why, unions, shared = pa_hooks.union_only(run, pa_hooks.git, self.proj, checked, now)
        self.assertTrue(ok, why)
        self.assertEqual(shared, [])
        self.assertEqual([(u["path"], u["regions"], u["order"]) for u in unions], [("a.txt", [[2, 3]], "checked first")])
        self.assertFalse(pa_hooks.union_only(run, pa_hooks.git, self.proj, checked, checked)[0], "no differing file: not a union")

    def test_u2a_one_side_only_is_changed(self):
        self.resolve("t-B", "t-A-x", "a.txt", line_edit(LINES, 2, "two A"))
        code, out, err = self.verify("t-B")
        self.assertEqual(code, 1, out + err)
        self.assertIn(": changed", out)

    def test_u2b_the_other_side_only_is_changed(self):
        self.resolve("t-B", "t-A-x", "a.txt", line_edit(LINES, 2, "two B"))
        code, out, err = self.verify("t-B")
        self.assertEqual(code, 1, out + err)
        self.assertIn(": changed", out)

    def test_u2c_a_line_edited_in_the_resolution_is_changed(self):
        self.resolve("t-B", "t-A-x", "a.txt", union_text("two B", "two A, edited"))
        code, out, err = self.verify("t-B")
        self.assertEqual(code, 1, out + err)
        self.assertIn(": changed", out)

    def test_u5_union_in_one_file_and_a_real_change_in_another_is_changed(self):
        self.resolve("t-B", "t-A-x", "a.txt", union_text("two B", "two A"), extra=("b.txt", "b, changed\n"))
        code, out, err = self.verify("t-B")
        self.assertEqual(code, 1, out + err)
        self.assertIn(": changed", out)

    def test_u6_sides_swapped_is_union(self):
        self.resolve("t-B", "t-A-x", "a.txt", union_text("two A", "two B"))
        code, out, err = self.verify("t-B")
        self.assertEqual(code, 0, out + err)
        self.assertIn(": union", out)

    def test_u6b_add_joins_and_the_gate_needs_the_doc_only_review(self):
        """No approval to keep at add (the task was not in the queue): it joins on t-A-x as union, the gate waits for
        the short review, which counts only once refresh records it (V-U3: union never counts without it)."""
        self.resolve("t-B", "t-A-x", "a.txt", union_text("two A", "two B"))
        self.gate("t-B")
        self.add("t-B")
        e = self.entry("t-B")
        self.assertEqual((e["onto"], e["state"], e["approval"]), ("t-A-x", "checking", None))
        need = e["needs"]["shortReview"]
        self.assertEqual(need["kind"], "doc-only")
        self.assertEqual([u["path"] for u in need["unions"]], ["a.txt"])
        code, out, _ = self.gate("t-B")
        self.assertEqual(code, 1, out)
        self.assertIn("a short review is needed", out)
        self.review("t-B")
        self.queue("refresh", "--repo", self.proj)
        code, out, _ = self.gate("t-B")
        self.assertEqual(code, 0, out)
        self.assertIn("short review", out)


class U3RestackConflict(UBase):
    """t-B is in the queue on t-A-x with an approval; main moves under a.txt line 2; the restack conflicts and t-B
    leaves, keeping its approval as resumable (d-166). A rebase worker keeps both sides."""

    def setUp(self):
        super().setUp()
        self.task("t-A", [("fa.txt", "a\n")])
        self.add("t-A")
        self.task("t-B", [("a.txt", (2, "two B")), ("fb.txt", "b\n")])
        self.add("t-B")
        self.set_entries(lambda r: r["entries"][1].update(approval=dict(APPROVAL)))
        self.on_main("a.txt", (2, "two M"))
        code, out, err = self.queue("restack", "--repo", self.proj)
        self.assertEqual(code, 0, out + err)
        self.assertIn("conflict", out)
        self.assertEqual([e["task"] for e in self.entries()], ["t-A"])
        self.left = self.qrepo()["left"][-1]

    def test_u3a_conflict_keeps_the_approval_resumable(self):
        self.assertEqual((self.left["task"], self.left["why"]), ("t-B", "conflict"))
        self.assertEqual(self.left["approval"]["card"], "d-q1")
        self.assertEqual(self.left["approval"]["void"], "conflict")
        self.assertIs(self.left["approval"]["resumable"], True)
        self.assertEqual(self.left["checked"]["commit"], self.checked_commit())

    def checked_commit(self):
        return pa_hooks.checked_commits(read(os.path.join(self.evidence("t-B"), "VERIFY.md")))[-1]

    def test_u3b_reviewed_then_add_restores_the_approval(self):
        self.resolve("t-B", "t-A-x", "a.txt", union_text("two B", "two M"))
        code, out, err = self.verify("t-B")
        self.assertEqual(code, 0, out + err)
        self.assertIn(": union", out)
        self.review("t-B")
        self.gate("t-B")
        code, out, err = self.add("t-B")
        e = self.entry("t-B")
        self.assertEqual(e["onto"], "t-A-x")
        self.assertEqual(e["approval"]["card"], "d-q1")
        self.assertNotIn("void", e["approval"])
        self.assertNotIn("resumable", e["approval"])
        self.assertEqual(e["approval"]["carried"], "doc-only, short review SHORT-REVIEW-t-B-1.md")
        self.assertIsNone(e.get("heldApproval"))
        self.assertTrue(self.qrepo()["left"][-1].get("resumed"))
        code, out, _ = self.gate("t-B")
        self.assertEqual(code, 0, out)
        self.assertIn("short review", out)

    def test_u3c_without_the_review_the_approval_is_held_until_refresh_records_it(self):
        self.resolve("t-B", "t-A-x", "a.txt", union_text("two B", "two M"))
        self.gate("t-B")
        self.add("t-B")
        e = self.entry("t-B")
        self.assertIsNone(e["approval"])
        self.assertEqual(e["heldApproval"]["approval"]["card"], "d-q1")
        code, out, _ = self.gate("t-B")
        self.assertEqual(code, 1, out)
        self.assertIn("a short review is needed", out)
        self.queue("refresh", "--repo", self.proj)
        self.assertIsNone(self.entry("t-B")["approval"], "no review yet: still held")
        self.review("t-B")
        code, out, err = self.queue("refresh", "--repo", self.proj)
        self.assertEqual(code, 0, out + err)
        e = self.entry("t-B")
        self.assertEqual(e["approval"]["card"], "d-q1")
        self.assertEqual(e["approval"]["carried"], "doc-only, short review SHORT-REVIEW-t-B-1.md")
        self.assertIsNone(e.get("heldApproval"))
        self.assertEqual(self.gate("t-B")[0], 0)

    def test_u3d_a_changed_resolution_does_not_restore_it(self):
        self.resolve("t-B", "t-A-x", "a.txt", line_edit(LINES, 2, "two B and M"))
        self.gate("t-B")
        code, out, err = self.add("t-B", ok=False)
        self.assertEqual(code, 1, out + err)
        self.assertEqual([e["task"] for e in self.entries()], ["t-A"])

    def test_u4_a_must_fix_short_review_voids_the_approval_for_good(self):
        self.resolve("t-B", "t-A-x", "a.txt", union_text("two B", "two M"))
        self.gate("t-B")
        self.add("t-B")
        self.review("t-B", verdict="must fix (1)")
        code, out, err = self.queue("refresh", "--repo", self.proj)
        self.assertEqual([e["task"] for e in self.entries()], ["t-A"])
        left = self.qrepo()["left"]
        self.assertEqual(left[-1]["task"], "t-B")
        self.assertIn("short review", left[-1]["why"])
        self.assertEqual(left[-1]["approval"]["card"], "d-q1")
        self.assertTrue(left[-1]["approval"]["void"])
        self.assertNotIn("resumable", left[-1]["approval"])
        self.assertTrue(left[-2].get("resumed"), "the conflict's approval was used once")


if __name__ == "__main__":
    unittest.main()
