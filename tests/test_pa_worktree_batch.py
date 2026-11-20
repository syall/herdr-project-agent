"""t-168: one Delete card for many worktrees (Part A, R1-R11) and deleting clean worktrees on the person's standing rule
(Part B, R12-R16). Each test is named after the verification point it proves (spec.md, V<r>.<n>).

Run: python3 -m unittest -v tests/test_pa_worktree_batch.py
Uses the fixture of tests/test_pa_worker.py (temp homes, a stub hc, git only under a temp folder). Only Base is bound in
this module, so none of that file's tests run here a second time.
"""
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_pa_worker as tw  # noqa: E402
from test_pa_worker import Base, setUpModule, tearDownModule  # noqa: E402,F401 (the module's temp homes and environment)

pa, pa_hooks, load, dump, raw, write, sh = tw.pa, tw.pa_hooks, tw.load, tw.dump, tw.raw, tw.write, tw.sh
PANEL = "person, in the panel"


class Batch(Base):
    """A project repo; helpers for done tasks (rule (a)) and merged job worktrees of tasks in progress (rule (b))."""

    def setUp(self):
        super().setUp()
        self.proj = self.repo()
        self.trees = {}

    # ---- worktrees ----
    def done(self, tid, state="done", **extra):
        """A task whose main worktree is a candidate under rule (a)."""
        path = self.worktree(self.proj, tid + "-x")
        self.trees[tid] = path
        self.add_task(tid, state, path, [], **extra)
        return path

    def job_tree(self, name):
        path = os.path.join(self.tmp, "worktrees", name, "proj")
        sh(self.proj, "git", "worktree", "add", "-q", "-b", name, path, "main")
        return path

    def merged_commit(self, path):
        self.merges = getattr(self, "merges", 0) + 1
        f = "m{}.txt".format(self.merges)
        self.commit(path, f, f + "\n", "job work {}".format(self.merges))
        sh(self.proj, "git", "merge", "-q", "--no-edit", sh(path, "git", "rev-parse", "HEAD"))

    def job(self, tid, job="billing"):
        """A task in progress whose job worktree (merged) is a candidate under rule (b); its main worktree is not."""
        main = self.worktree(self.proj, tid + "-main")
        path = self.job_tree("{}-{}".format(tid, job))
        self.merged_commit(path)
        self.trees[tid] = path
        self.add_task(tid, "in-progress", main, [], worktrees=[{"path": path, "job": job}])
        return path

    def set_state(self, tid, state):
        led = self.ledger()
        next(t for t in led["tasks"] if t["id"] == tid)["state"] = state
        self.ledger(led)

    # ---- commands ----
    def check(self, *extra, open_cards=True):
        return self.run_cmd("pa-worktree", "check", "--space", "sp1", *((["--open-cards"] if open_cards else []) + list(extra)))

    def remove_card(self, cid="d-wt-batch-1", *extra):
        return self.run_cmd("pa-worktree", "remove", "--card", cid, "--space", "sp1", *extra)

    # ---- cards ----
    def path(self, cid, folder="decisions"):
        return os.path.join(self.dir, folder, cid + ".json")

    def dec(self, cid, folder="decisions"):
        return pa.read_json(self.path(cid, folder))

    def files(self):
        folder = os.path.join(self.dir, "decisions")
        return sorted(os.listdir(folder)) if os.path.isdir(folder) else []

    def answer(self, cid, label, note=None, by=PANEL):
        card = load(self.path(cid))
        if label == "Other":
            card.update(other=True)
        card.update(status="answered", answer=label, note=note, answered=pa.now(), answeredBy=by)
        dump(card, self.path(cid))
        return card

    def label(self, cid, scope):
        return next(o["label"] for o in self.dec(cid)["options"] if o.get("scope") == scope)

    def items(self, cid="d-wt-batch-1"):
        return {i["task"]: i for i in self.dec(cid)["items"]}


class CheckSide(Batch):
    """R1-R3, R8-R10: what pa-worktree check opens, withdraws and says."""

    def three(self):
        self.done("t-003")
        self.done("t-004")
        self.job("t-005")

    def test_v1_1_three_candidates_one_batch_card(self):
        self.three()
        code, out, err = self.check()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.files(), ["d-wt-batch-1.json"], out)
        card = self.dec("d-wt-batch-1")
        self.assertEqual((card["kind"], card["task"], card["status"]), ("worktrees", None, "open"))
        self.assertEqual([(i["task"], i["rule"]) for i in card["items"]], [("t-003", "a"), ("t-004", "a"), ("t-005", "b")])
        self.assertIn("opened card d-wt-batch-1", out)

    def test_v1_2_one_candidate_single_card(self):
        self.done("t-003")
        code, out, err = self.check()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.files(), ["d-wt-t-003.json"])
        self.assertIn("opened card d-wt-t-003", out)

    def test_v1_3_ids_are_never_reused(self):
        self.three()
        dump({"id": "d-wt-batch-1", "kind": "worktrees", "status": "answered", "answer": "Keep all 2", "items": []},
             self.path("d-wt-batch-1", pa.ARCHIVE))
        self.check()
        self.assertEqual(self.files(), ["d-wt-batch-2.json"])

    def test_v1_4_without_open_cards_nothing_is_written(self):
        self.three()
        code, out, err = self.check(open_cards=False)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.files(), [])
        for tid in ("t-003", "t-004", "t-005"):
            self.assertRegex(out, r"{}: candidate, [^\n]*; --open-cards puts it on one card with the other 2\n".format(tid))

    def test_v2_1_items_carry_every_field_and_the_single_snapshot(self):
        self.done("t-003")
        write(os.path.join(self.trees["t-003"], "wip.txt"), "x\n")
        self.check()
        single = self.dec("d-wt-t-003")
        os.unlink(self.path("d-wt-t-003"))
        self.done("t-004")
        self.check()
        item = self.items()["t-003"]
        for key in ("path", "branch", "task", "job", "main", "rule", "tasks", "lossless", "snapshot", "md", "removed",
                    "skipped", "withdrawn"):
            self.assertIn(key, item)
        self.assertEqual((item["removed"], item["skipped"], item["withdrawn"]), (None, None, None))
        self.assertEqual((item["path"], item["branch"], item["job"], item["main"], item["tasks"]),
                         (single["worktree"], single["branch"], single["job"], single["main"], single["tasks"]))
        self.assertEqual(item["snapshot"], single["snapshot"])

    def test_v2_2_one_line_per_item_and_no_line_from_a_newline(self):
        self.three()
        self.check()
        context = self.dec("d-wt-batch-1")["context"]
        for tid, job, branch in (("t-003", "main", "t-003-x"), ("t-004", "main", "t-004-x"), ("t-005", "billing", "t-005-billing")):
            line = [l for l in context.split("\n") if l.startswith("  {} ".format(tid))]
            self.assertEqual(len(line), 1, context)
            self.assertIn("{} {} · {} · ".format(tid, job, branch), line[0])
            self.assertRegex(line[0], r"\d+ commits?, \d+ merged, \d+ pushed")
        self.assertIn("  t-005 billing · t-005-billing · 1 commit, 1 merged, 0 pushed · clean", context)
        item = {"task": "t-009", "job": "main", "branch": "evil\nbranch", "lossless": True, "cost": None,
                "counts": {"commits": 0, "merged": 0, "pushed": 0, "uncommitted": 0}}
        line = pa_hooks.worktree_line(item)
        self.assertNotIn("\n", line)
        self.assertIn("evil\\nbranch", line)

    def test_v2_3_a_lossy_worktree_is_listed_with_its_cost(self):
        self.done("t-003")
        self.done("t-004")
        write(os.path.join(self.trees["t-004"], "a.txt"), "x\n")
        write(os.path.join(self.trees["t-004"], "b.txt"), "x\n")
        self.check()
        lines = self.dec("d-wt-batch-1")["context"].split("\n")
        lost = lines.index("Deleting loses something (1):")
        self.assertIn("Nothing is lost (1):", lines)
        self.assertTrue(lines[lost + 1].startswith("  t-004 main · t-004-x · "), lines)
        self.assertIn("2 uncommitted", lines[lost + 1])
        self.assertIn("discarded", lines[lost + 1])
        self.assertEqual((self.items()["t-003"]["lossless"], self.items()["t-004"]["lossless"]), (True, False))

    def test_v2_4_each_item_has_its_worktree_md(self):
        self.three()
        self.check()
        for tid, name in (("t-003", "WORKTREE.md"), ("t-004", "WORKTREE.md"), ("t-005", "WORKTREE-billing.md")):
            self.assertTrue(os.path.isfile(os.path.join(self.dir, "evidence", tid, name)), (tid, name))
            self.assertEqual(self.items()[tid]["md"], name)

    def test_v3_1_options_in_order(self):
        self.three()
        write(os.path.join(self.trees["t-004"], "a.txt"), "x\n")
        self.check()
        card = self.dec("d-wt-batch-1")
        self.assertEqual([(o["label"], o["scope"]) for o in card["options"]],
                         [("Delete the 2 where nothing is lost", "lossless"), ("Delete all 3", "all"), ("Keep all 3", "keep")])
        self.assertEqual(card["recommendation"], "Delete the 2 where nothing is lost")
        self.assertIn("including the 1 listed under Deleting loses something", card["options"][1]["detail"])
        self.assertEqual(card["question"], "Delete 3 worktrees whose jobs are over?")
        os.unlink(self.path("d-wt-batch-1"))
        os.unlink(os.path.join(self.trees["t-004"], "a.txt"))
        self.check()
        card = self.dec("d-wt-batch-1")  # its file is gone, so its id is free again
        self.assertEqual([o["label"] for o in card["options"]], ["Delete all 3", "Keep all 3"])
        self.assertEqual(card["recommendation"], "Delete all 3")

    def test_v3_3_keep_all_is_not_asked_again(self):
        self.three()
        self.check()
        self.answer("d-wt-batch-1", "Keep all 3")
        before = self.files()
        code, out, err = self.check()
        self.assertEqual(code, 0, err)
        for tid in ("t-003", "t-004", "t-005"):
            self.assertIn("{}: card d-wt-batch-1 answered 'Keep all 3': kept, not asked again".format(tid), out)
        self.assertEqual(self.files(), before)

    def test_v8_1_an_open_single_card_moves_to_the_batch(self):
        self.done("t-003")
        self.check()
        self.assertEqual(self.dec("d-wt-t-003")["status"], "open")
        self.done("t-004")
        code, out, err = self.check()
        self.assertEqual(code, 0, err)
        single = self.dec("d-wt-t-003")
        self.assertEqual(single["status"], "withdrawn")
        self.assertIn("moved to card d-wt-batch-1", single["note"])
        self.assertEqual(sorted(self.items()), ["t-003", "t-004"])
        self.assertEqual(self.items()["t-003"]["md"], "WORKTREE.md")

    def test_v8_2_an_answered_keep_stays_with_its_card(self):
        self.done("t-003")
        self.check()
        self.answer("d-wt-t-003", "Keep")
        self.done("t-004")
        self.done("t-006")
        code, out, err = self.check()
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(self.items()), ["t-004", "t-006"])
        self.assertIn("t-003: card d-wt-t-003 answered 'Keep': kept, not asked again", out)
        self.assertEqual(self.dec("d-wt-t-003")["status"], "answered")

    def test_v8_2b_a_withdrawn_single_card_joins_the_batch(self):
        self.done("t-003")
        self.check()
        card = load(self.path("d-wt-t-003"))
        card.update(status="withdrawn", answer=None, answered=pa.now())
        dump(card, self.path("d-wt-t-003"))
        self.done("t-004")
        self.check()
        self.assertEqual(sorted(self.items()), ["t-003", "t-004"])

    def test_v9_1_a_new_candidate_withdraws_and_reopens(self):
        self.done("t-003")
        self.done("t-004")
        self.check()
        self.done("t-005")
        code, out, err = self.check()
        self.assertEqual(code, 0, err)
        old = self.dec("d-wt-batch-1")
        self.assertEqual(old["status"], "withdrawn")
        self.assertIn("the worktrees changed: + t-005", old["note"])
        self.assertEqual([i["task"] for i in self.dec("d-wt-batch-2")["items"]], ["t-003", "t-004", "t-005"])

    def test_v9_1b_without_open_cards_it_only_says_so(self):
        self.done("t-003")
        self.done("t-004")
        self.check()
        self.done("t-005")
        before = raw(self.path("d-wt-batch-1"))
        code, out, err = self.check(open_cards=False)
        self.assertIn("card d-wt-batch-1 is open, but the worktrees changed: + t-005; --open-cards withdraws it and opens it again", out)
        self.assertEqual(raw(self.path("d-wt-batch-1")), before)
        self.assertEqual(self.files(), ["d-wt-batch-1.json"])

    def test_v9_2_a_reopened_task_leaves(self):
        self.three()
        self.check()
        self.set_state("t-004", "in-progress")
        code, out, err = self.check()
        self.assertEqual(code, 0, err)
        old = self.dec("d-wt-batch-1")
        self.assertEqual(old["status"], "withdrawn")
        self.assertIn("- t-004 (tasks still t-004 in-progress)", old["note"])
        self.assertEqual([i["task"] for i in self.dec("d-wt-batch-2")["items"]], ["t-003", "t-005"])

    def test_v9_2b_a_terminal_or_keep_also_leaves(self):
        self.three()
        self.check()
        self.term("t77", "someone", cwd=self.trees["t-003"])
        led = self.ledger()
        led["tasks"][1]["keepWorktree"] = True
        self.ledger(led)
        self.check()
        note = self.dec("d-wt-batch-1")["note"]
        self.assertIn("- t-003 (another terminal is working in it: t77)", note)
        self.assertIn("- t-004 (keep is set)", note)
        self.assertEqual(self.files(), ["d-wt-batch-1.json", "d-wt-t-005-billing.json"])

    def test_v9_3_one_left_gets_a_single_card(self):
        self.done("t-003")
        self.done("t-004")
        self.check()
        self.set_state("t-004", "review")
        self.check()
        self.assertEqual(self.dec("d-wt-batch-1")["status"], "withdrawn")
        self.assertEqual(self.dec("d-wt-t-003")["status"], "open")
        self.assertIsNone(self.dec("d-wt-batch-2"))

    def test_v9_4_an_unchanged_set_writes_nothing(self):
        self.three()
        self.check()
        before = raw(self.path("d-wt-batch-1"))
        code, out, err = self.check()
        self.assertEqual(code, 0, err)
        self.assertEqual(raw(self.path("d-wt-batch-1")), before)
        self.assertEqual(self.files(), ["d-wt-batch-1.json"])
        self.assertIn("card d-wt-batch-1 is open (3 worktrees)", out)

    def test_v10_1_forty_eight_fit_on_one_card(self):
        for n in range(48):
            self.done("t-{:03d}".format(100 + n))
        started = time.time()
        code, out, err = self.check()
        took = time.time() - started
        self.assertEqual(code, 0, err)
        card = self.dec("d-wt-batch-1")
        self.assertEqual(len(card["items"]), 48)
        self.assertLessEqual(len(card["context"]), pa_hooks.BATCH_CONTEXT_MAX)
        self.assertEqual(pa_hooks.BATCH_CONTEXT_MAX, 6000)
        self.assertNotIn("more wait", card["context"])
        self.assertEqual(sum(1 for l in card["context"].split("\n") if l.startswith("  t-")), 48)
        sys.stderr.write("[V10.1 check --open-cards with 48 candidates: {:.1f} s] ".format(took))

    def test_v10_2_seventy_wait_in_two_cards(self):
        for n in range(70):
            self.done("t-{:03d}".format(100 + n))
        self.check()
        card = self.dec("d-wt-batch-1")
        self.assertEqual(len(card["items"]), pa_hooks.BATCH_MAX)
        self.assertEqual(pa_hooks.BATCH_MAX, 60)
        self.assertIn("… and 10 more wait for the next card, once this one is answered", card["context"])
        before = raw(self.path("d-wt-batch-1"))
        code, out, _ = self.check()
        self.assertEqual(raw(self.path("d-wt-batch-1")), before, "the next card waits while this one is open")
        self.assertEqual(self.files(), ["d-wt-batch-1.json"])
        self.answer("d-wt-batch-1", "Delete all 60")
        code, out, err = self.remove_card()
        self.assertEqual(code, 0, err + out)
        self.check()
        self.assertEqual(len(self.dec("d-wt-batch-2")["items"]), 10)


class RemoveCard(Batch):
    """R3.2, R4-R7, R8.3, R11: pa-worktree remove --card <batch id>."""

    def opened(self, n=3, label="all", note=None, by=PANEL):
        tids = ["t-{:03d}".format(3 + i) for i in range(n)]
        for tid in tids:
            self.done(tid)
        self.check()
        self.answer("d-wt-batch-1", self.label("d-wt-batch-1", label), note, by)
        return tids

    def test_v3_2_lossless_removes_only_those(self):
        self.done("t-003")
        self.done("t-004")
        self.done("t-005")
        write(os.path.join(self.trees["t-004"], "a.txt"), "x\n")
        self.check()
        self.answer("d-wt-batch-1", "Delete the 2 where nothing is lost")
        code, out, err = self.remove_card()
        self.assertEqual(code, 0, err + out)
        self.assertFalse(os.path.isdir(self.trees["t-003"]))
        self.assertFalse(os.path.isdir(self.trees["t-005"]))
        self.assertTrue(os.path.isdir(self.trees["t-004"]))
        self.assertIn("Removed 2 of 2 on card d-wt-batch-1: t-003, t-005", out)
        code, out, err = self.check()
        self.assertIn("opened card d-wt-t-004", out)
        self.assertEqual(self.dec("d-wt-t-004")["status"], "open")

    def test_v4_1_pa_decide_refuses_a_batch_card(self):
        self.done("t-003")
        self.done("t-004")
        self.check()
        before = raw(self.path("d-wt-batch-1"))
        import argparse
        with self.assertRaises(pa.Problem) as e:
            pa.cmd_decide(argparse.Namespace(space="sp1", card="d-wt-batch-1", answer="Delete all 2", quote="delete them all",
                                             withdraw=False, why=None))
        self.assertIn("a card that deletes worktrees is answered in the panel, not in conversation", str(e.exception))
        self.assertEqual(raw(self.path("d-wt-batch-1")), before)

    def test_v4_2_answered_in_conversation_is_refused(self):
        self.opened(2, by="person, in conversation")
        code, out, err = self.remove_card()
        self.assertEqual(code, 2)
        self.assertIn("is answered in the panel only; nothing was removed", err)
        self.assertTrue(all(os.path.isdir(p) for p in self.trees.values()))

    def test_v4_3_open_withdrawn_and_unknown_cards_are_refused(self):
        self.done("t-003")
        self.done("t-004")
        self.check()
        code, _o, err = self.remove_card()
        self.assertEqual(code, 2)
        self.assertIn("card d-wt-batch-1 is not answered yet", err)
        code, _o, err = self.remove_card("d-wt-batch-9")
        self.assertEqual(code, 2)
        self.assertIn("no batch card d-wt-batch-9", err)
        self.assertTrue(all(os.path.isdir(p) for p in self.trees.values()))

    def test_v5_3_ledger_and_journal(self):
        tids = self.opened(3)
        code, out, err = self.remove_card()
        self.assertEqual(code, 0, err + out)
        self.assertIn("Removed 3 of 3 on card d-wt-batch-1: t-003, t-004, t-005", out)
        for t in self.ledger()["tasks"]:
            self.assertTrue(any(e.get("removed") for e in t.get("worktrees") or []), t)
        lines = [l for l in self.journal() if "(card d-wt-batch-1)" in str(l.get("why"))]
        self.assertEqual(len(lines), 3, self.journal())
        items = self.items()
        self.assertTrue(all(items[t]["removed"] for t in tids))

    # V5.1: one item fails each check, the other two are removed
    def one_fails(self, change, reason, withdrawn=False, tid="t-004"):
        self.opened(3)
        change()
        code, out, err = self.remove_card()
        self.assertEqual(code, 1, err + out)
        self.assertIn(reason, out)
        self.assertRegex(out, r"Removed 2 of 3 on card d-wt-batch-1: t-003, t-005; skipped 1: {} \(".format(tid))
        for t, p in self.trees.items():
            self.assertEqual(os.path.isdir(p), t == tid, t)
        item = self.items()[tid]
        self.assertIn(reason, item["skipped"])
        self.assertEqual(bool(item["withdrawn"]), withdrawn)
        return item

    def test_v5_1a_branch_changed(self):
        self.one_fails(lambda: sh(self.trees["t-004"], "git", "checkout", "-q", "-b", "other"), "it was asked for branch t-004-x", True)

    def test_v5_1b_spent_in_the_ledger(self):
        def spend():
            led = self.ledger()
            t = next(t for t in led["tasks"] if t["id"] == "t-004")
            t["worktrees"] = [{"path": self.trees["t-004"], "job": "main", "removed": "2099-01-01T00:00:00Z"},
                              {"path": self.trees["t-004"], "job": "main"}]
            self.ledger(led)
        self.one_fails(spend, "removed after the card", True)

    def test_v5_1c_unreadable(self):
        saved = pa_hooks.worktree_facts
        def facts(path, profile=None):
            return {"unreadable": "git status failed"} if path == os.path.realpath(self.trees["t-004"]) else saved(path, profile)
        self.addCleanup(setattr, pa_hooks, "worktree_facts", saved)
        self.one_fails(lambda: setattr(pa_hooks, "worktree_facts", facts), "could not read")

    def test_v5_1d_keep_set(self):
        def keep():
            led = self.ledger()
            led["tasks"][1]["keepWorktree"] = True
            self.ledger(led)
        self.one_fails(keep, "keepWorktree is set")

    def test_v5_1e_task_reopened(self):
        self.one_fails(lambda: self.set_state("t-004", "in-progress"), "tasks still t-004 in-progress")

    def test_v5_1f_live_worker(self):
        def live():
            led = self.ledger()
            led["tasks"][1]["workers"] = [{"address": "t41", "role": "build", "name": "t-004-fix"}]
            self.ledger(led)
            self.term("t41", "t-004-fix")
        self.one_fails(live, "in use by t41")

    def test_v5_1g_other_terminal(self):
        self.one_fails(lambda: self.term("t77", "someone", cwd=self.trees["t-004"]), "in use by t77")

    def test_v5_1h_task_set_changed(self):
        def share():
            led = self.ledger()
            led["tasks"].append({"id": "t-009", "title": "x", "state": "done", "worktree": self.trees["t-004"], "workers": []})
            self.ledger(led)
        self.one_fails(share, "the card was asked for t-004", True)

    def test_v5_1i_new_uncommitted_file(self):
        self.one_fails(lambda: write(os.path.join(self.trees["t-004"], "new.txt"), "x\n"), "more uncommitted files now", True)

    def test_v5_1j_new_commit(self):  # also V6.1
        self.one_fails(lambda: self.commit(self.trees["t-004"], "late.txt", "x\n", "late"), "has new commits since the card", True)

    def test_v5_1k_main_checkout_is_never_removed(self):
        self.opened(3)
        card = load(self.path("d-wt-batch-1"))
        card["items"][1]["path"] = os.path.realpath(self.proj)
        dump(card, self.path("d-wt-batch-1"))
        led = self.ledger()
        led["tasks"][1]["worktree"] = self.proj
        self.ledger(led)
        code, out, err = self.remove_card()
        self.assertEqual(code, 1, err + out)
        self.assertIn("main checkout", out)
        self.assertTrue(os.path.isdir(os.path.join(self.proj, ".git")))

    def test_v5_2_a_terminal_that_starts_after_item_one(self):
        self.opened(3)
        saved = pa_hooks.remove_worktree
        def after_first(path, facts):
            done = saved(path, facts)
            self.term("t88", "late", cwd=self.trees["t-004"])
            return done
        pa_hooks.remove_worktree = after_first
        self.addCleanup(setattr, pa_hooks, "remove_worktree", saved)
        code, out, err = self.remove_card()
        self.assertEqual(code, 1, err + out)
        self.assertIn("t-004 (still in use by t88", out)
        self.assertTrue(os.path.isdir(self.trees["t-004"]))

    def test_v6_1_v6_2_v6_3_a_changed_one_is_asked_again(self):
        self.opened(3)
        self.commit(self.trees["t-004"], "late.txt", "x\n", "late")
        code, out, err = self.remove_card()
        self.assertEqual(code, 1)
        self.assertIn("the next pa-worktree check --open-cards asks again about the ones that changed", out)
        self.assertTrue(self.items()["t-004"]["withdrawn"])
        code, out, err = self.check()
        self.assertIn("opened card d-wt-t-004", out)
        code, out, err = self.remove_card()
        self.assertEqual(code, 0, err + out)
        self.assertIn("t-003: already removed", out)
        self.assertIn("t-004: skipped earlier", out)
        self.assertTrue(os.path.isdir(self.trees["t-004"]))

    def test_v6_4_a_git_refusal_is_failed_and_the_run_goes_on(self):
        self.opened(3)
        saved = pa_hooks.remove_worktree
        def refuse(path, facts):
            if path == os.path.realpath(self.trees["t-004"]):
                raise pa.Problem("git would not remove it: locked")
            return saved(path, facts)
        pa_hooks.remove_worktree = refuse
        self.addCleanup(setattr, pa_hooks, "remove_worktree", saved)
        code, out, err = self.remove_card()
        self.assertEqual(code, 1)
        self.assertIn("t-004 (failed: git would not remove it: locked)", out)
        self.assertFalse(os.path.isdir(self.trees["t-005"]))

    def test_v6_5_a_folder_already_gone(self):
        self.opened(3)
        sh(self.proj, "git", "worktree", "remove", self.trees["t-004"])
        code, out, err = self.remove_card()
        self.assertEqual(code, 0, err + out)
        self.assertIn("t-004: already gone", out)
        t = next(t for t in self.ledger()["tasks"] if t["id"] == "t-004")
        self.assertTrue(any(e.get("removed") for e in t.get("worktrees") or []))

    def test_v7_1_other_removes_nothing(self):
        self.opened(3)
        card = self.answer("d-wt-batch-1", "Other", note="keep t-004, delete the rest")
        before = raw(self.path("d-wt-batch-1"))
        code, out, err = self.remove_card()
        self.assertEqual(code, 2)
        self.assertIn("keep t-004, delete the rest", err)
        self.assertEqual(raw(self.path("d-wt-batch-1")), before)
        self.assertTrue(all(os.path.isdir(p) for p in self.trees.values()))

    def test_v7_2_a_note_withdraws_the_card(self):
        self.opened(3, note="but not t-004")
        code, out, err = self.remove_card()
        self.assertEqual(code, 2)
        self.assertIn('say "keep tNN"', err)
        card = self.dec("d-wt-batch-1")
        self.assertEqual((card["status"], card["previousAnswer"], card["previousNote"]), ("withdrawn", "Delete all 3", "but not t-004"))
        self.assertTrue(all(os.path.isdir(p) for p in self.trees.values()))
        self.check()
        self.assertEqual(self.dec("d-wt-batch-2")["status"], "open")
        self.assertEqual(self.dec("d-wt-batch-2")["previousNote"], "but not t-004")

    def test_v7_3_keep_all_removes_nothing(self):
        self.opened(3, label="keep")
        code, out, err = self.remove_card()
        self.assertEqual(code, 0, err)
        self.assertIn("answered 'Keep all 3': nothing is removed", out)
        self.assertTrue(all(os.path.isdir(p) for p in self.trees.values()))

    def test_v8_3_remove_task_on_a_batch_item_points_at_the_card(self):
        self.opened(2)
        code, out, err = self.run_cmd("pa-worktree", "remove", "t-003", "--space", "sp1")
        self.assertEqual(code, 2)
        self.assertIn("pa-worktree remove --card d-wt-batch-1", err)
        self.assertTrue(os.path.isdir(self.trees["t-003"]))

    def test_v11_1_a_second_run_waits_then_refuses(self):
        self.opened(2)
        lock = open(os.path.join(self.dir, ".worktree.lock"), "a+")
        self.addCleanup(lock.close)
        fcntl.flock(lock, fcntl.LOCK_EX)
        with mock.patch.object(pa_hooks, "WORKTREE_LOCK_WAIT", 0.3):
            code, out, err = self.remove_card()
        self.assertEqual(code, 2)
        self.assertIn("another pa-worktree remove is running", err)
        self.assertTrue(all(os.path.isdir(p) for p in self.trees.values()))
