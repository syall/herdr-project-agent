"""pa-push-main's push card for a merge-queue batch (t-052 J5, plan.md JJ.3 P1-P6).

Run: python3 -m unittest -v tests/test_pa_push_queue.py
The fixture (temp homes, the bare origin under a temp folder, the person-only check's injected environment) is
tests/push_fixtures.py; see its docstring. The queue's merges are made by the test itself: it fast-forwards local
main and writes queue.json's merged[] as pa-queue does (J2's record; J3b adds `by: "queue"` and `suite`). No real
space, no real queue.json, no real push: the only remote is the fixture's bare repository.
"""
import fcntl
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from push_fixtures import *  # noqa: F401,F403 (the shared fixture and helpers)
from push_fixtures import setUpModule, tearDownModule  # noqa: F401 (the module's temp homes and environment)

TASK_A = "t-A"
CARD_A = "d-push-t-A-1"


class Queued(Repo):
    """Repo, plus task t-A on t-A-x (worktree wtA, one commit) ahead of t-1 in the queue: t-1-x is rebased onto
    t-A-x, so t-1's own commit sits on A's. Both tasks are in the ledger with passing evidence. LOCAL_FIRST: a commit
    made on local main by hand before A's branch was made (not from any queued task)."""

    LOCAL_FIRST = False

    def setUp(self):
        Repo.setUp(self)
        self.m0 = self.head(self.main)
        if self.LOCAL_FIRST:
            self.hand = self.commit_on(self.main, "h.txt", "by hand on main")
            sh(self.wt, "git", "rebase", "-q", "main")
        self.wta = os.path.join(self.tmp, "wtA")
        sh(self.main, "git", "worktree", "add", "-q", "-b", "t-A-x", self.wta, "main")
        self.a1 = self.commit_on(self.wta, "aa.txt", "work of A")
        sh(self.wt, "git", "rebase", "-q", "t-A-x")
        self.b1 = self.head(self.wt)
        path = os.path.join(self.dir, "ledger.json")
        led = load(path)
        led["tasks"].append({"id": TASK_A, "title": "A", "state": "in-progress", "tier": "high", "type": "feature",
                             "worktree": self.wta, "workers": [{"address": "t41", "role": "build"}, {"address": "t42", "role": "reviewer"}]})
        dump(led, path)
        for name in REVIEW_FILES:
            write(os.path.join(self.dir, "evidence", TASK_A, name), "text\n")
        self.queue = {"version": 1, "repos": {self.main: {"common": os.path.realpath(os.path.join(self.main, ".git")), "main": "main",
                                                          "mainAt": self.head(self.main), "planned": [], "entries": [], "left": [],
                                                          "merged": []}}}

    def save_queue(self):
        self.queue["repos"][self.main]["mainAt"] = self.head(self.main)
        dump(self.queue, os.path.join(self.dir, "queue.json"))

    def merge(self, task, branch, suite=None):
        """What pa-queue's merge does to local main and queue.json: a fast-forward, and a merged[] record."""
        frm = self.head(self.main)
        sh(self.main, "git", "merge", "-q", "--ff-only", branch)
        rec = {"task": task, "job": None, "tip": self.head(self.main, "refs/heads/" + branch), "from": frm, "to": self.head(self.main),
               "at": "2026-11-18T10:00:00Z", "by": "queue"}
        if suite:
            rec["suite"] = suite
        self.queue["repos"][self.main]["merged"].append(rec)
        self.save_queue()
        return rec

    def plan_for(self, task):
        args = pa_push.main_parser().parse_args(["plan", task, "--repo", self.main, "--space", SPACE])
        return self.quiet(pa_push.cmd_plan, args)

    def card_path(self, card):
        return os.path.join(self.dir, "decisions", card + ".json")

    def card(self, card):
        return load(self.card_path(card))

    def read(self, path):
        with open(path, "rb") as f:
            return f.read()

    def journal(self):
        path = os.path.join(self.dir, "journal.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(l) for l in f if l.strip()]

    def batch(self, suite_a=None):
        """A then t-1 merged by the queue, each gated (before its merge and again after: J1 round 2's L1 case)."""
        self.assertEqual(self.gate(TASK_A), 0)
        self.merge(TASK_A, "t-A-x", suite=suite_a)
        self.assertEqual(self.gate(TASK_A), 0)
        self.assertEqual(self.gate(TASK), 0)
        self.merge(TASK, "t-1-x")
        self.assertEqual(self.gate(TASK), 0)


# ---- P1, P2: queue labels and one line per merged entry ------------------------------------------

class P1Labels(Queued):
    def test_p1a_commits_labelled_by_queue_task(self):
        self.batch()
        code, _o, err = self.plan_for(TASK)
        self.assertEqual(code, 0, err)
        c = self.card(CARD)
        rows = [(x["sha"], x.get("queueTask"), x.get("queueJob")) for x in c["push"]["commits"]]
        self.assertEqual(rows, [(self.b1, TASK, None), (self.a1, TASK_A, None)])
        self.assertIn("- {} work of A (t <t@example.invalid>) (from t-A)".format(self.a1[:7]), c["context"])
        self.assertIn("- {} task work (t <t@example.invalid>) (from t-1)".format(self.b1[:7]), c["context"])
        self.assertNotIn("not from this task", c["context"])

    def test_p1b_without_queue_json_the_card_is_as_before(self):
        """Pin: no queue.json, so every commit and context line is what it was before J5."""
        self.assertEqual(self.gate(), 0)
        code, _o, err = self.plan_for(TASK)
        self.assertEqual(code, 0, err)
        c = self.card(CARD)
        p = c["push"]
        for x in p["commits"]:
            self.assertEqual(set(x), {"sha", "subject", "author", "fromTask"})
        self.assertNotIn("replaces", p)
        self.assertNotIn("batch", p)
        lines = ["Repository: {}".format(self.main),
                 "Push to: {} branch main (on your push allowlist). The remote main is at {} now.".format(self.url, self.m0[:7]),
                 "Local main is at {}; this card fast-forwards it to {} first.".format(self.m0[:7], self.b1[:7]),
                 "Gated commit: {} on t-1-x (pa-gate review passed at {}).".format(self.b1[:7], p["gateAt"]),
                 "Commits to push (2):",
                 "- {} task work (t <t@example.invalid>)".format(self.b1[:7]),
                 "- {} work of A (t <t@example.invalid>) (not from this task)".format(self.a1[:7])]
        got = c["context"].split("\n")  # origin's URL is the allowlist URL here: no origin note
        self.assertEqual(got[:len(lines)], lines)
        self.assertTrue(got[len(lines)].startswith("Will run: "))
        self.assertEqual(got[len(lines) + 1:], ["The panel checks every fact again when you look, and the push runs only when you answer Push."])

    def test_p2_one_line_per_merged_entry_in_merge_order(self):
        tree = sh(self.wta, "git", "rev-parse", "HEAD^{tree}")
        self.batch(suite_a={"tree": tree, "worker": "t42", "started": "2026-11-18T09:00:00Z", "ended": "2026-11-18T09:10:00Z"})
        code, _o, err = self.plan_for(TASK)
        self.assertEqual(code, 0, err)
        c = self.card(CARD)
        lines = c["context"].split("\n")
        head = lines.index("Merged into local main by the queue (2), in merge order:")
        self.assertEqual(lines[head + 1:head + 3], [
            "- t-A: 1 commit, tip {}, suite passed on tree {}".format(self.a1[:7], tree[:7]),
            "- t-1: 1 commit, tip {}".format(self.b1[:7])])
        self.assertLess(head, lines.index("Commits to push (2):"))
        self.assertEqual([(b["task"], b["count"], b["tip"]) for b in c["push"]["batch"]], [(TASK_A, 1, self.a1), (TASK, 1, self.b1)])

    def test_p2b_an_entry_already_pushed_gets_no_line(self):
        self.assertEqual(self.gate(TASK_A), 0)
        self.merge(TASK_A, "t-A-x")
        sh(self.main, "git", "push", "-q", "origin", "main")
        self.assertEqual(self.gate(TASK), 0)
        self.merge(TASK, "t-1-x")
        self.assertEqual(self.gate(TASK), 0)
        code, _o, err = self.plan_for(TASK)
        self.assertEqual(code, 0, err)
        c = self.card(CARD)
        self.assertIn("Merged into local main by the queue (1), in merge order:", c["context"])
        self.assertNotIn("- t-A:", c["context"])
        self.assertEqual([b["task"] for b in c["push"]["batch"]], [TASK])


class P1HandCommit(Queued):
    LOCAL_FIRST = True

    def test_p1c_a_commit_from_no_queued_task(self):
        self.batch()
        code, _o, err = self.plan_for(TASK)
        self.assertEqual(code, 0, err)
        c = self.card(CARD)
        self.assertEqual([x.get("queueTask") for x in c["push"]["commits"]], [TASK, TASK_A, None])
        self.assertIn("- {} by hand on main (t <t@example.invalid>) (not from a queued task)".format(self.hand[:7]), c["context"])


# ---- P3: one push card for several merges --------------------------------------------------------

class P3Replace(Queued):
    def open_a(self):
        self.assertEqual(self.gate(TASK_A), 0)
        self.merge(TASK_A, "t-A-x")
        self.assertEqual(self.gate(TASK_A), 0)
        code, _o, err = self.plan_for(TASK_A)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.card(CARD_A)["status"], "open")
        self.assertEqual(self.gate(TASK), 0)
        self.merge(TASK, "t-1-x")
        self.assertEqual(self.gate(TASK), 0)

    def test_p3a_new_card_replaces_the_open_one_it_contains(self):
        self.open_a()
        code, out, err = self.plan_for(TASK)
        self.assertEqual(code, 0, err)
        self.assertIn(CARD, out)
        old, new = self.card(CARD_A), self.card(CARD)
        self.assertEqual(old["status"], "withdrawn")
        self.assertEqual(old["replacedBy"], CARD)
        self.assertEqual(old["note"], "replaced by {}: local main moved to {}, which holds its commits".format(CARD, self.b1[:7]))
        self.assertFalse(old["push"]["done"])
        self.assertEqual(new["status"], "open")
        self.assertEqual(new["push"]["replaces"], CARD_A)
        self.assertIn("Replaces {}: this card also pushes its 1 commit.".format(CARD_A), new["context"])
        # never stands for a commit it does not list: sha by sha, the new card lists every commit of the old one
        new_shas = [x["sha"] for x in new["push"]["commits"]]
        for x in old["push"]["commits"]:
            self.assertIn(x["sha"], new_shas)
        self.assertEqual(new_shas, [self.b1, self.a1])
        why = [l for l in self.journal() if l.get("decision") == CARD_A and "replaced by" in (l.get("why") or "")]
        self.assertEqual(len(why), 1, self.journal())

    def test_p3b_an_open_card_not_contained_is_refused(self):
        self.open_a()
        # A's card stands for a commit the new one does not hold (a side commit, on no branch)
        side = sh(self.main, "git", "commit-tree", "-p", self.m0, "-m", "side", self.head(self.main, "HEAD^{tree}"))
        c = self.card(CARD_A)
        c["push"]["commit"] = side
        dump(c, self.card_path(CARD_A))
        before = self.read(self.card_path(CARD_A))
        code, _o, err = self.plan_for(TASK)
        self.assertEqual(code, 1)
        self.assertIn("an open push card {} exists for {} and {}; withdraw {} first".format(CARD_A, TASK_A, self.main, CARD_A), err)
        self.assertEqual(self.read(self.card_path(CARD_A)), before)
        self.assertFalse(os.path.exists(self.card_path(CARD)))

    def test_p3c_the_replaced_card_never_pushes(self):
        self.open_a()
        origin = self.origin_head()
        self.assertEqual(self.plan_for(TASK)[0], 0)
        code, r = pa_push.run_push(SPACE, CARD_A, "Push", None, "x" * 64, PERSON_ENV, PANEL_TREE)
        self.assertEqual(code, 1, r)
        self.assertEqual(r["refusal"]["check"], "C4", r)
        self.assertIn("withdrawn", r["refusal"]["message"])
        self.assertEqual(self.origin_head(), origin)
        self.assertEqual(self.card(CARD)["status"], "open")

    def test_p3d_plan_while_the_push_lock_is_held(self):
        self.open_a()
        before = self.read(self.card_path(CARD_A))
        with open(os.path.join(self.dir, ".push.lock"), "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                code, _o, err = self.plan_for(TASK)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        self.assertEqual(code, 1)
        self.assertIn("another push for this space is running", err)
        self.assertEqual(self.read(self.card_path(CARD_A)), before)
        self.assertFalse(os.path.exists(self.card_path(CARD)))

    def test_p3e_same_commit_again_is_refused_as_before(self):
        self.open_a()
        self.assertEqual(self.plan_for(TASK)[0], 0)
        before = self.read(self.card_path(CARD))
        code, _o, err = self.plan_for(TASK)
        self.assertEqual(code, 1)
        self.assertIn("an open push card {} exists for {} and {}; withdraw {} first".format(CARD, TASK, self.main, CARD), err)
        self.assertEqual(self.read(self.card_path(CARD)), before)


# ---- P4: C8h, the gate compared the commit against the push branch -------------------------------

class P4Onto(Queued):
    def test_p4a_checked_on_a_queued_entry_is_refused(self):
        self.assertEqual(self.gate(), 0)
        code, _o, err = self.plan_for(TASK)  # no queue yet: compared against main
        self.assertEqual(code, 0, err)
        rec = self.queue["repos"][self.main]
        rec["entries"] = [{"task": TASK_A, "job": None, "branch": "t-A-x", "onto": "main", "state": "checking"},
                          {"task": TASK, "job": None, "branch": "t-1-x", "onto": "t-A-x", "state": "checking"}]
        self.save_queue()
        r = pa_push.check_card(self.dir, SPACE, CARD, PERSON_ENV, PANEL_TREE)
        self.assertFalse(r["ok"])
        self.assertEqual(r["refusal"]["check"], "C8h", r["refusal"])
        self.assertIn("its checks compared it against t-A-x; it merges through pa-queue after the task of that branch", r["refusal"]["message"])
        os.remove(self.card_path(CARD))
        self.assertEqual(self.gate(), 0)
        code, _o, err = self.plan_for(TASK)
        self.assertEqual(code, 1)
        self.assertIn("compared it against t-A-x", err)
        self.assertFalse(os.path.exists(self.card_path(CARD)))

    def test_p4b_after_the_queue_merged_both_the_last_one_plans(self):
        write(os.path.join(self.dir, "evidence", TASK_A, "VERIFY.md"), "Checked: {}\n".format(self.a1))
        write(os.path.join(self.dir, "evidence", TASK, "VERIFY.md"), "Checked: {}\n".format(self.b1))
        self.batch()
        code, _o, err = self.plan_for(TASK)
        self.assertEqual(code, 0, err)
        r = pa_push.check_card(self.dir, SPACE, CARD, PERSON_ENV, PANEL_TREE)
        self.assertTrue(r["ok"], r["refusal"])


# ---- P5: the main checkout's settings, one function ---------------------------------------------

class P5Settings(Queued):
    def test_p5_main_settings_refusal(self):
        s = pa_push.Session()
        try:
            self.assertIsNone(pa_push.main_settings_refusal(s, self.main))
            sh(self.main, "git", "config", "core.sshCommand", "true")
            why = pa_push.main_settings_refusal(s, self.main)
        finally:
            s.close()
        self.assertEqual(why, "{} has git setting core.sshcommand (local), which can run a program or redirect the push; remove it, "
                              "or push by hand".format(self.main))


# ---- P6: a card never stands for a commit it does not list (COMMITS_MAX) -------------------------

class P6Max(Repo):
    def grow(self, n):
        """n empty commits on t-1-x, fast-forwarded into the worktree."""
        tip = self.head(self.wt)
        tree = self.head(self.wt, "HEAD^{tree}")
        for i in range(n):
            tip = sh(self.wt, "git", "commit-tree", "-p", tip, "-m", "c{}".format(i), tree)
        sh(self.wt, "git", "merge", "-q", "--ff-only", tip)

    def plan(self):
        args = pa_push.main_parser().parse_args(["plan", TASK, "--repo", self.main, "--space", SPACE])
        return self.quiet(pa_push.cmd_plan, args)

    def test_p6_more_than_200_commits_refused(self):
        self.grow(pa_push.COMMITS_MAX - 1)  # with the task's commit: 200
        self.assertEqual(self.gate(), 0)
        code, _o, err = self.plan()
        self.assertEqual(code, 0, err)
        path = os.path.join(self.dir, "decisions", CARD + ".json")
        self.assertEqual(len(load(path)["push"]["commits"]), 200)
        os.remove(path)
        self.grow(1)
        self.assertEqual(self.gate(), 0)
        code, _o, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("201 commits", err)
        self.assertIn("at most 200", err)
        self.assertEqual(os.listdir(os.path.join(self.dir, "decisions")), [])
        s = pa_push.Session()
        try:
            push = {"version": 1, "space": SPACE, "task": TASK, "path": self.main}
            with self.assertRaises(pa_push.Refused) as e:
                pa_push.gather(s, self.dir, SPACE, push, pa_push.blank_facts(), plan=True)
        finally:
            s.close()
        self.assertEqual(e.exception.check, "C11d")


if __name__ == "__main__":
    unittest.main()
