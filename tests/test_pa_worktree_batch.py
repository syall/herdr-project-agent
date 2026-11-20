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

    def test_v7_2b_keep_all_with_a_note_is_withdrawn_too(self):  # R7: a Delete or Keep answer with a note
        self.opened(2, label="keep", note="keep these but delete t-004 please")
        code, out, err = self.check()
        self.assertEqual(code, 0, err)
        for tid in ("t-003", "t-004"):
            self.assertIn("{}: card d-wt-batch-1 answered 'Keep all 2' with a note: run `pa-worktree remove --card d-wt-batch-1`".format(tid), out)
            self.assertNotIn("{}: card d-wt-batch-1 answered 'Keep all 2': kept".format(tid), out)
        code, out, err = self.remove_card()
        self.assertEqual(code, 2)
        self.assertIn('say "keep tNN"', err)
        card = self.dec("d-wt-batch-1")
        self.assertEqual((card["status"], card["previousAnswer"], card["previousNote"]),
                         ("withdrawn", "Keep all 2", "keep these but delete t-004 please"))
        self.assertTrue(all(os.path.isdir(p) for p in self.trees.values()))
        self.check()
        self.assertEqual(self.dec("d-wt-batch-2")["status"], "open")

    def test_v5_1l_a_job_worktree_not_under_a_worktrees_folder(self):  # RL2: the check on the --card path
        self.job("t-003")
        self.job("t-004")
        self.job("t-005")
        self.check()
        self.answer("d-wt-batch-1", self.label("d-wt-batch-1", "all"))
        saved = pa_hooks.in_worktrees_folder
        moved = os.path.realpath(self.trees["t-004"])
        self.addCleanup(setattr, pa_hooks, "in_worktrees_folder", saved)
        pa_hooks.in_worktrees_folder = lambda path: os.path.realpath(path) != moved and saved(path)
        code, out, err = self.remove_card()
        self.assertEqual(code, 1, err + out)
        self.assertIn("is not under a worktrees folder; nothing was removed", out)
        self.assertRegex(out, r"Removed 2 of 3 on card d-wt-batch-1: t-003 billing, t-005 billing; skipped 1: t-004 billing \(")
        for t, p in self.trees.items():
            self.assertEqual(os.path.isdir(p), t == "t-004", t)

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


RULE_TEXT = ("Delete a worktree with no card when every commit in it is merged into main and pushed, nothing is uncommitted "
             "or untracked, no terminal is in it, and no keep is set.")
AGENT_ENV = {"CLAUDECODE": "1", "HC_TERMINAL_ID": "t0"}
AGENT_TREE = [{"pid": 40, "comm": "claude", "args": "claude"}, {"pid": 1, "comm": "launchd", "args": ""}]
PERSON_TREE = [{"pid": 50, "comm": "zsh", "args": "-zsh"}, {"pid": 1, "comm": "launchd", "args": ""}]


class Standing(Batch):
    """Part B: pa-worktree-allow (R12) and pa-worktree remove --standing (R13-R16). A remote holds main; clean worktrees
    sit under <tmp>/worktrees/<task>/proj."""

    def setUp(self):
        super().setUp()
        self.remote = os.path.join(self.tmp, "remote.git")
        sh(self.tmp, "git", "init", "-q", "--bare", self.remote)
        sh(self.proj, "git", "remote", "add", "origin", self.remote)
        sh(self.proj, "git", "push", "-q", "origin", "main")
        self.rule()
        self.bind()
        here = os.getcwd()
        os.chdir(self.tmp)  # no task record above the caller's folder
        self.addCleanup(os.chdir, here)
        self.push = pa_hooks.push_tool()

    # ---- the rule and its binding ----
    def rule(self, **over):
        rule = dict({"id": "r-wt", "text": RULE_TEXT, "status": "active", "appliesTo": "all", "source": "person",
                     "history": [{"at": "2026-11-01T00:00:00Z", "by": "person", "change": "created"}]}, **over)
        dump({"version": 1, "rules": [rule]}, os.path.join(self.dir, "rules.json"))
        return rule

    def allow_file(self):
        return os.path.join(pa.ROOT, "worktree-allow.json")

    def bind(self, rule="r-wt", text=RULE_TEXT, enabled=True, entry_enabled=True, **extra):
        dump(dict({"version": 1, "enabled": enabled, "spaces": [{"space": "sp1", "enabled": entry_enabled, "standing": {
            "rule": rule, "textSha": self.push.text_sha(text) if hasattr(self, "push") else pa_hooks.push_tool().text_sha(text)}}]}, **extra),
             self.allow_file())

    # ---- worktrees ----
    def clean(self, tid, commit=True, state="done"):
        """A worktree under a worktrees folder whose every commit is merged into main and pushed."""
        path = os.path.join(self.tmp, "worktrees", tid, "proj")
        sh(self.proj, "git", "worktree", "add", "-q", "-b", tid, path, "main")
        if commit:
            self.commit(path, tid + ".txt", tid + "\n", "work of " + tid)
            sh(self.proj, "git", "merge", "-q", "--no-edit", tid)
            sh(self.proj, "git", "push", "-q", "origin", "main")
        self.trees[tid] = path
        self.add_task(tid, state, path, [])
        return path

    # ---- running it ----
    def standing(self, rule="r-wt", env=AGENT_ENV, tree=AGENT_TREE):
        drop = {k: "" for k in ("CLAUDE_PROJECT_DIR",)}
        with mock.patch.dict(os.environ, dict(env)), mock.patch.object(self.push, "ancestors", lambda *a, **k: tree):
            for k in drop:
                os.environ.pop(k, None)
            return self.run_cmd("pa-worktree", "remove", "--standing", rule, "--space", "sp1")

    def allow(self, *argv, env=None, tree=PERSON_TREE):
        with mock.patch.dict(os.environ, dict(env or {})), mock.patch.object(self.push, "ancestors", lambda *a, **k: tree):
            return self.run_cmd("pa-worktree-allow", *argv)

    def stops(self):
        return [l for l in self.journal() if l.get("standing") == "stopped"]

    def refused(self, result, *fragments, stop=False):
        code, out, err = result
        self.assertEqual(code, 2, out + err)
        for f in fragments:
            self.assertIn(f, err)
        self.assertTrue(all(os.path.isdir(p) for p in self.trees.values()), "nothing is removed")
        self.assertEqual(bool(self.stops()), stop, self.journal())


class AllowFile(Standing):
    def test_v12_1_set_refuses_an_agent(self):
        os.unlink(self.allow_file())
        code, out, err = self.allow("set", "--space", "sp1", "--standing", "r-wt", env=AGENT_ENV, tree=AGENT_TREE)
        self.assertNotEqual(code, 0)
        self.assertIn("is the person's command", err)
        self.assertFalse(os.path.exists(self.allow_file()))

    def test_v12_2_set_as_the_person(self):
        before = raw(self.allow_file())
        self.rule(text=RULE_TEXT + " Edited.", history=[{"at": "2026-11-01T00:00:00Z", "by": "person", "change": "created"}])
        code, out, err = self.allow("set", "--space", "sp1", "--standing", "r-wt")
        self.assertEqual(code, 0, err)
        data = load(self.allow_file())
        self.assertEqual(data["spaces"][0]["standing"], {"rule": "r-wt", "textSha": self.push.text_sha(RULE_TEXT + " Edited.")})
        self.assertEqual(raw(self.allow_file() + ".bak-pa-worktree-allow"), before)
        self.assertIsNone(pa_hooks.worktree_allow_validate(data))

    def test_v12_2b_show_unbind_on_off(self):
        code, out, err = self.allow("show", "--space", "sp1", env=AGENT_ENV, tree=AGENT_TREE)
        self.assertEqual(code, 0, err)
        self.assertIn("on", out)
        self.assertIn("r-wt", out)
        code, out, _ = self.allow("show", "--json")
        self.assertTrue(json.loads(out)["valid"])
        self.assertEqual(self.allow("off", "--space", "sp1")[0], 0)
        self.assertFalse(load(self.allow_file())["spaces"][0]["enabled"])
        self.assertEqual(self.allow("off")[0], 0)
        self.assertFalse(load(self.allow_file())["enabled"])
        self.assertEqual(self.allow("on")[0], 0)
        self.assertEqual(self.allow("unbind", "--space", "sp1")[0], 0)
        self.assertNotIn("standing", load(self.allow_file())["spaces"][0])

    def test_v12_3_an_invalid_file_refuses(self):
        self.clean("t-003")
        data = load(self.allow_file())
        data["extra"] = 1
        dump(data, self.allow_file())
        self.refused(self.standing(), "worktree-allow.json is not valid: unknown key extra")

    def test_v12_3b_validation_is_all_or_nothing(self):
        good = load(self.allow_file())
        sha = good["spaces"][0]["standing"]["textSha"]
        bad = [dict(good, version=2), dict(good, enabled="yes"), dict(good, spaces={}),
               dict(good, spaces=[{"space": "sp1", "enabled": True, "bogus": 1}]),
               dict(good, spaces=[{"space": "s p", "enabled": True}]),
               dict(good, spaces=[{"space": "sp1", "enabled": True, "standing": {"rule": "r-wt", "textSha": "x"}}]),
               dict(good, spaces=[{"space": "sp1", "enabled": True, "note": "n" * 201}]),
               dict(good, spaces=[{"space": "sp1", "enabled": True, "standing": {"rule": "r-wt", "textSha": sha}},
                                  {"space": "sp1", "enabled": False}])]
        for data in bad:
            with self.subTest(data=data):
                self.assertIsNotNone(pa_hooks.worktree_allow_validate(data))
        self.assertIsNone(pa_hooks.worktree_allow_validate(good))


class RuleInForce(Standing):
    """R13: each condition, read now; a refusal removes nothing and writes no stop line, but for a new stop cause."""

    def setUp(self):
        super().setUp()
        self.clean("t-003")

    def test_v13_0_in_force_removes(self):
        code, out, err = self.standing()
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.isdir(self.trees["t-003"]))

    def test_v13_1_no_file(self):
        os.unlink(self.allow_file())
        self.refused(self.standing(), "no worktree-allow.json")

    def test_v13_2_file_off(self):
        self.bind(enabled=False)
        self.refused(self.standing(), "turned off")

    def test_v13_3_space_off_or_not_listed(self):
        self.bind(entry_enabled=False)
        self.refused(self.standing(), "turned off for space sp1")
        dump({"version": 1, "enabled": True, "spaces": []}, self.allow_file())
        self.refused(self.standing(), "space sp1 is not in worktree-allow.json")

    def test_v13_4_bound_to_another_rule(self):
        self.bind(rule="r-other")
        self.refused(self.standing(), "bound to rule r-other, not r-wt")

    def test_v13_4b_a_push_binding_does_not_count(self):
        os.unlink(self.allow_file())
        dump({"version": 1, "enabled": True, "repos": [{"path": self.proj, "url": "file://" + self.remote, "branch": "main",
              "space": "sp1", "enabled": True, "standing": {"rule": "r-wt", "textSha": self.push.text_sha(RULE_TEXT)}}]},
             os.path.join(pa.ROOT, "push-allow.json"))
        self.refused(self.standing(), "no worktree-allow.json")

    def test_v13_5_rule_missing_or_twice(self):
        dump({"version": 1, "rules": []}, os.path.join(self.dir, "rules.json"))
        self.refused(self.standing(), "no rule r-wt in this space's rules.json")
        rule = self.rule()
        dump({"version": 1, "rules": [rule, rule]}, os.path.join(self.dir, "rules.json"))
        self.refused(self.standing(), "2 rules have the id r-wt")

    def test_v13_6_not_active(self):
        self.rule(status="paused")
        self.refused(self.standing(), "rule r-wt is paused, not active")

    def test_v13_7_applies_to_all_only(self):
        self.rule(appliesTo="release")
        self.refused(self.standing(), "applies to release, not all")

    def test_v13_8_the_persons_only(self):
        self.rule(source="orchestrator", history=[{"at": "2026-11-01T00:00:00Z", "by": "orchestrator", "change": "created"}])
        self.refused(self.standing(), "is not the person's: the person never approved it")
        self.rule(history=[{"at": "2026-11-01T00:00:00Z", "by": "person", "change": "created"},
                           {"at": "2026-11-02T00:00:00Z", "by": "orchestrator", "change": "edited"}])
        self.refused(self.standing(), "a later change by orchestrator")

    def test_v13_9_text_changed(self):
        self.rule(text=RULE_TEXT + " And more.")
        self.refused(self.standing(), "text of rule r-wt changed since the person bound it")

    def test_v13_10_a_hold_stops_it_with_a_stop_line(self):
        with open(os.path.join(self.dir, "journal.jsonl"), "a") as f:
            f.write(json.dumps({"at": pa.now(), "hold": "on"}) + "\n")
        self.refused(self.standing(), "a hold is on", stop=True)
        self.refused(self.standing(), "the rule stopped", stop=True)
        self.assertEqual(len(self.stops()), 1, "an existing stop writes no second line")

    def test_v13_11_an_other_answer_or_a_panel_note_stops_it(self):
        dump({"id": "d-x", "status": "answered", "answer": "Other", "other": True, "note": "no", "answered": pa.now(),
              "answeredBy": PANEL}, self.path("d-x"))
        self.refused(self.standing(), "card d-x was answered Other", stop=True)
        os.unlink(self.path("d-x"))
        self.journal_reset()
        dump({"id": "d-y", "status": "answered", "answer": "A", "note": "but wait", "answered": pa.now(), "answeredBy": PANEL},
             self.path("d-y"))
        self.refused(self.standing(), "card d-y has a note written in the panel", stop=True)

    def journal_reset(self):
        write(os.path.join(self.dir, "journal.jsonl"), "")


class NoteStops(Standing):
    """R13, R15, R7 (round 3 MF1): a panel note on a worktree card stops the rule, answered or then withdrawn or asked
    again, so remove --card's withdrawal never lets the next standing run delete what the note said to keep."""

    def two_on_a_batch(self, scope, note):
        self.clean("t-003")
        self.clean("t-004")
        self.check()
        self.answer("d-wt-batch-1", self.label("d-wt-batch-1", scope), note=note)

    def test_v13_11b_keep_all_with_a_note_then_remove_card_then_standing(self):  # probe A
        self.two_on_a_batch("keep", "keep t-004")
        self.assertEqual(self.remove_card()[0], 2)
        self.assertEqual(self.dec("d-wt-batch-1")["status"], "withdrawn")
        self.refused(self.standing(), "card d-wt-batch-1 carries a note", stop=True)
        self.check()  # asked again on a new card, carrying the note: the rule stays stopped
        self.refused(self.standing(), "the rule stopped", stop=True)

    def test_v13_11c_delete_all_with_a_note_then_remove_card_then_standing(self):  # probe B
        self.two_on_a_batch("all", "but keep t-004")
        self.assertEqual(self.remove_card()[0], 2)
        self.refused(self.standing(), "card d-wt-batch-1 carries a note", stop=True)

    def test_v13_11d_standing_before_remove_card(self):  # probe C: both orders end with nothing removed
        self.two_on_a_batch("keep", "keep t-004")
        self.refused(self.standing(), "card d-wt-batch-1 has a note written in the panel", stop=True)
        self.assertEqual(self.remove_card()[0], 2)
        self.refused(self.standing(), "the rule stopped", stop=True)

    def test_v13_11e_keep_all_without_a_note(self):  # probe D (control): kept, and the rule does not stop
        self.two_on_a_batch("keep", None)
        code, out, err = self.standing()
        self.assertEqual(code, 0, err)
        self.assertIn("answered 'Keep all 2' on card d-wt-batch-1", out)
        self.assertTrue(all(os.path.isdir(p) for p in self.trees.values()))
        self.assertEqual(self.stops(), [])

    def test_v13_11f_a_single_card_withdrawn_or_asked_again_with_a_note(self):
        self.clean("t-003")
        self.check()
        cid = "d-wt-t-003"
        card = self.answer(cid, "Delete", note="wait, keep it")
        pa.keep_previous(card, card["answer"])
        card.update(status="withdrawn", answer=None, answered=pa.now(), answeredBy="pa-worktree remove: withdrawn", note="Withdrawn")
        dump(card, self.path(cid))
        self.refused(self.standing(), "card d-wt-t-003 carries a note", stop=True)
        write(os.path.join(self.dir, "journal.jsonl"), "")
        card.update(status="open", answer=None, note=None, answered=None, answeredBy=None)  # asked again, the note carried
        dump(card, self.path(cid))
        self.refused(self.standing(), "card d-wt-t-003 carries a note", stop=True)

    def test_v13_11g_a_note_before_the_rule_changed_does_not_stop_it(self):
        self.clean("t-003")
        self.check()
        cid = "d-wt-t-003"
        card = self.answer(cid, "Delete", note="old words")
        card.update(answered="2026-10-01T00:00:00Z")
        pa.keep_previous(card, card["answer"])
        card.update(status="open", answer=None, note=None, answered=None, answeredBy=None)
        dump(card, self.path(cid))
        code, out, err = self.standing()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.stops(), [])  # the rule does not stop (R13) ...
        self.assertTrue(os.path.isdir(self.trees["t-003"]))  # ... and the worktree is left for its card (round 4)
        self.assertIn("its card d-wt-t-003 carries the person's note", out)

    # ---- round 4 MF1: the person's note or Keep holds each worktree on it, also once the rule is resumed ----
    def resume(self):
        """The person turns the rule off and on in the panel: a later person change; earlier stops no longer count."""
        self.rule(history=[{"at": "2026-11-01T00:00:00Z", "by": "person", "change": "created"},
                           {"at": "2099-01-01T00:00:00Z", "by": "person", "change": "resumed"}])

    def kept(self, *fragments):
        code, out, err = self.standing()
        self.assertEqual(code, 0, out + err)
        self.assertTrue(all(os.path.isdir(p) for p in self.trees.values()), out)
        for f in fragments:
            self.assertIn(f, out)
        return out

    def test_v13_11h_keep_all_with_a_note_then_resume(self):  # review r3 probe A, after a resume
        self.two_on_a_batch("keep", "keep t-004")
        self.assertEqual(self.remove_card()[0], 2)
        self.refused(self.standing(), "card d-wt-batch-1 carries a note", stop=True)
        self.resume()
        self.kept("Removed 0 worktrees", "its card d-wt-batch-1 carries the person's note")
        self.check()  # asked again on d-wt-batch-2, the note carried; still open
        self.assertEqual(self.dec("d-wt-batch-2")["status"], "open")
        self.kept("Removed 0 worktrees", "its card d-wt-batch-2 carries the person's note")

    def test_v13_11i_delete_all_with_a_note_then_resume(self):  # review r3 probe B, after a resume
        self.two_on_a_batch("all", "but keep t-004")
        self.assertEqual(self.remove_card()[0], 2)
        self.refused(self.standing(), "card d-wt-batch-1 carries a note", stop=True)
        self.resume()
        self.kept("Removed 0 worktrees", "its card d-wt-batch-1 carries the person's note")
        self.check()
        self.kept("Removed 0 worktrees", "its card d-wt-batch-2 carries the person's note")

    def test_v13_11j_answered_with_a_note_then_resume(self):  # a batch card and a single card, not yet acted on
        self.two_on_a_batch("all", "but keep t-004")
        self.refused(self.standing(), "card d-wt-batch-1 has a note written in the panel", stop=True)
        self.resume()
        self.kept("Removed 0 worktrees", "its card d-wt-batch-1 carries the person's note")

    def test_v13_11k_single_card_answered_delete_with_a_note_then_resume(self):
        self.clean("t-003")
        self.check()
        self.answer("d-wt-t-003", "Delete", note="wait, keep it")
        self.refused(self.standing(), "card d-wt-t-003 has a note written in the panel", stop=True)
        self.resume()
        self.kept("Removed 0 worktrees", "its card d-wt-t-003 carries the person's note")

    def test_v13_11l_single_card_withdrawn_with_a_note_then_resume(self):
        self.clean("t-003")
        self.check()
        cid = "d-wt-t-003"
        card = self.answer(cid, "Delete", note="wait, keep it")
        pa.keep_previous(card, card["answer"])
        card.update(status="withdrawn", answer=None, answered=pa.now(), answeredBy="pa-worktree remove: withdrawn", note="Withdrawn")
        dump(card, self.path(cid))
        self.refused(self.standing(), "card d-wt-t-003 carries a note", stop=True)
        self.resume()
        self.kept("Removed 0 worktrees", "its card d-wt-t-003 carries the person's note")
        card.update(status="open", answer=None, note=None, answered=None, answeredBy=None)  # asked again, the note carried
        dump(card, self.path(cid))
        self.kept("Removed 0 worktrees", "its card d-wt-t-003 carries the person's note")

    def test_v13_11m_a_keep_withdrawn_without_a_note_still_holds(self):
        self.clean("t-003")
        self.check()
        cid = "d-wt-t-003"
        card = self.answer(cid, "Keep")
        pa.keep_previous(card, card["answer"])
        card.update(status="open", answer=None, note=None, answered=None, answeredBy=None)  # asked again, Keep carried
        dump(card, self.path(cid))
        self.kept("Removed 0 worktrees", "its card d-wt-t-003 carries the person's answer 'Keep'")
        self.assertEqual(self.stops(), [])

    def test_v13_11n_a_later_delete_answer_replaces_the_carried_note(self):
        self.clean("t-003")
        self.check()
        cid = "d-wt-t-003"
        card = self.answer(cid, "Delete", note="old words")
        card.update(answered="2026-10-01T00:00:00Z")
        pa.keep_previous(card, card["answer"])
        card.update(status="open", answer=None, note=None, answered=None, answeredBy=None)
        dump(card, self.path(cid))
        self.answer(cid, "Delete")  # the person answers the card asked again: Delete, no note
        code, out, err = self.standing()
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.isdir(self.trees["t-003"]))

    # ---- round 5 (review r4 MF1): the hold is the worktree's, whichever card it moves to ----
    def test_v13_11o_batch_note_then_alone_on_a_single_card(self):  # review r4 P1: batch -> single
        self.two_on_a_batch("keep", "keep t-004")
        self.assertEqual(self.remove_card()[0], 2)
        self.refused(self.standing(), "card d-wt-batch-1 carries a note", stop=True)
        self.term("t98", "busy", cwd=self.trees["t-003"])  # t-003 stops being a candidate
        self.check()
        self.resume()
        self.kept("Removed 0 worktrees", "t-004 (main) (its card", "carries the person's note")
        single = self.dec("d-wt-t-004")
        self.assertEqual(single["status"], "open")
        self.assertEqual((single.get("previousAnswer"), single.get("previousNote")), ("Keep all 2", "keep t-004"),
                         "the new card shows the person's words on this worktree")
        self.answer("d-wt-t-004", "Delete")  # the person's later answer on the worktree's card replaces the hold
        code, out, err = self.standing()
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.isdir(self.trees["t-004"]), out)
        self.assertTrue(os.path.isdir(self.trees["t-003"]))

    def test_v13_11p_single_note_withdrawn_then_joins_a_batch(self):  # review r4 P2: single -> batch
        self.clean("t-003")
        self.check()
        cid = "d-wt-t-003"
        card = self.answer(cid, "Delete", note="wait, keep it")
        pa.keep_previous(card, card["answer"])
        card.update(status="withdrawn", answer=None, answered=pa.now(), answeredBy="pa-worktree remove: withdrawn", note="Withdrawn")
        dump(card, self.path(cid))
        self.refused(self.standing(), "card d-wt-t-003 carries a note", stop=True)
        self.clean("t-004")
        self.check()
        self.resume()
        self.kept("Removed 0 worktrees", "t-003 (main) (its card", "carries the person's note")
        batch = self.dec("d-wt-batch-1")
        self.assertEqual((batch.get("previousAnswer"), batch.get("previousNote")), ("Delete", "wait, keep it"))

    def test_v13_11q_an_open_single_card_carrying_a_note_moves_to_a_batch(self):
        self.clean("t-003")
        self.check()
        cid = "d-wt-t-003"
        card = self.answer(cid, "Delete", note="wait, keep it")
        pa.keep_previous(card, card["answer"])
        card.update(status="open", answer=None, note=None, answered=None, answeredBy=None)  # asked again, the note carried
        dump(card, self.path(cid))
        self.refused(self.standing(), "card d-wt-t-003 carries a note", stop=True)
        self.clean("t-004")
        self.check()  # the open single card is moved to d-wt-batch-1
        self.assertEqual(self.dec(cid)["status"], "withdrawn")
        self.assertEqual(self.dec("d-wt-batch-1")["status"], "open")
        self.resume()
        self.kept("Removed 0 worktrees", "t-003 (main) (its card", "carries the person's note")

    def test_v13_11r_a_batch_asked_again_then_answered_delete_all(self):  # N8: the later answer replaces the carried note
        self.two_on_a_batch("keep", "keep t-004")
        card = self.dec("d-wt-batch-1")
        card.update(answered="2026-10-01T00:00:00Z")  # before the rule's last change: no stop line
        dump(card, self.path("d-wt-batch-1"))
        self.assertEqual(self.remove_card()[0], 2)
        self.check()
        self.assertEqual(self.dec("d-wt-batch-2").get("previousNote"), "keep t-004")
        self.kept("Removed 0 worktrees", "carries the person's note")
        self.answer("d-wt-batch-2", self.label("d-wt-batch-2", "all"))
        code, out, err = self.standing()
        self.assertEqual(code, 0, err)
        self.assertFalse(any(os.path.isdir(p) for p in self.trees.values()), out)


class Caller(Standing):
    """R14: only this space's Project Agent."""

    def setUp(self):
        super().setUp()
        self.clean("t-003")

    def test_v14_1a_a_workers_terminal(self):
        led = self.ledger()
        led["tasks"][0]["workers"] = [{"address": "t51", "role": "build", "name": "w", "finished": True}]
        self.ledger(led)
        self.refused(self.standing(env={"CLAUDECODE": "1", "HC_TERMINAL_ID": "t51"}), "a worker of t-003")

    def test_v14_1b_a_folder_with_a_task_record(self):
        folder = os.path.join(self.tmp, "w")
        dump({"task": "t-777", "space": "sp1"}, os.path.join(folder, ".claude", "pa-task.json"))
        os.chdir(folder)
        self.refused(self.standing(), "a worker's folder")

    def test_v14_1c_the_persons_own_shell(self):
        self.refused(self.standing(env={"HC_TERMINAL_ID": "t0"}, tree=PERSON_TREE), "not an agent")

    def test_v14_1d_another_spaces_project_agent(self):
        self.refused(self.standing(env={"CLAUDECODE": "1", "HC_TERMINAL_ID": "t9"}), "not this space's Project Agent terminal")


class Clean(Standing):
    """R15: what it deletes without a card; everything else stays for the cards."""

    def kept(self, tid, why):
        self.clean("t-001")
        code, out, err = self.standing()
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.isdir(self.trees["t-001"]), "the clean one beside it is removed")
        self.assertTrue(os.path.isdir(self.trees[tid]), tid)
        self.assertIn(why, out)
        return out

    def test_v15_1a_not_under_a_worktrees_folder(self):
        self.done("t-003")
        sh(self.proj, "git", "push", "-q", "origin", "main")
        self.kept("t-003", "t-003 (main) (not under a worktrees folder)")

    def test_v15_1b_uncommitted(self):
        self.clean("t-003")
        write(os.path.join(self.trees["t-003"], "README"), "changed\n")
        self.kept("t-003", "t-003 (main) (1 uncommitted")

    def test_v15_1c_not_merged(self):
        path = self.clean("t-003", commit=False)
        self.commit(path, "x.txt", "x\n", "not merged")
        self.kept("t-003", "t-003 (main) (not every commit is merged into main and pushed")

    def test_v15_1d_a_terminal_in_it(self):
        self.clean("t-003")
        self.term("t77", "someone", cwd=self.trees["t-003"])
        self.clean("t-001")
        code, out, err = self.standing()
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.isdir(self.trees["t-003"]))
        self.assertFalse(os.path.isdir(self.trees["t-001"]))

    def test_v15_1e_keep(self):
        self.clean("t-003")
        led = self.ledger()
        led["tasks"][0]["keepWorktree"] = True
        self.ledger(led)
        self.clean("t-001")
        self.standing()
        self.assertTrue(os.path.isdir(self.trees["t-003"]))
        self.assertFalse(os.path.isdir(self.trees["t-001"]))

    def test_v15_1f_answered_keep_or_other(self):
        for label in ("Keep", "Other"):
            with self.subTest(label=label):
                tid = "t-00{}".format(3 if label == "Keep" else 4)
                path = self.clean(tid)
                dump({"id": "d-wt-" + tid, "task": tid, "worktree": os.path.realpath(path), "branch": tid, "status": "answered",
                      "answer": label, "note": "keep it" if label == "Other" else None, "answeredBy": "person, in conversation",
                      "answered": "2026-10-01T00:00:00Z", "tasks": [tid]}, self.path("d-wt-" + tid))
        self.kept("t-003", "t-003 (main) (answered 'Keep' on card d-wt-t-003)")
        self.assertTrue(os.path.isdir(self.trees["t-004"]))

    def test_v15_1g_an_open_card_is_spent(self):
        self.clean("t-003")
        self.clean("t-004")
        self.check()
        code, out, err = self.standing()
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.isdir(self.trees["t-003"]))
        self.assertTrue(all(i["removed"] for i in self.dec("d-wt-batch-1")["items"]))

    def test_v15_1h_not_a_candidate_is_not_touched(self):
        self.clean("t-003", commit=False, state="in-progress")  # rule (b): no commits since its fork, so its job is not over
        code, out, err = self.standing()
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.isdir(self.trees["t-003"]))

    def test_v15_2_pushed_to_its_own_branch_only(self):
        path = self.clean("t-003", commit=False)
        self.commit(path, "x.txt", "x\n", "on its own branch")
        sh(path, "git", "push", "-q", "origin", "t-003")
        self.kept("t-003", "not every commit is merged into main and pushed")

    def test_v15_3_an_untracked_file(self):
        self.clean("t-003")
        write(os.path.join(self.trees["t-003"], "new.txt"), "x\n")
        self.kept("t-003", "uncommitted or untracked")

    def profile_tree(self, **status):
        """t-003's worktree of the profile's kind: repositories A and B, each on branch feat, merged into main and pushed;
        the profile's status script prints `status` over clean counts, and its remove script logs its arguments."""
        kind = os.path.join(self.tmp, "Ws", "worktrees", "t-003-pkg")
        repos = []
        for name in ("A", "B"):
            folder = os.path.join(kind, "src", name)
            remote = os.path.join(self.tmp, name + ".git")
            sh(self.tmp, "git", "init", "-q", "--bare", remote)
            os.makedirs(folder)
            sh(folder, "git", "init", "-q", "-b", "main")
            self.commit(folder, "README", "base\n", "base")
            sh(folder, "git", "remote", "add", "origin", remote)
            sh(folder, "git", "checkout", "-q", "-b", "feat")
            self.commit(folder, "f.txt", "x\n", name + " work")
            sh(folder, "git", "checkout", "-q", "main")
            sh(folder, "git", "merge", "-q", "--ff-only", "feat")
            sh(folder, "git", "push", "-q", "origin", "main")
            sh(folder, "git", "checkout", "-q", "feat")
            repos.append(folder)
        write(os.path.join(kind, "marker.txt"), "")
        status_sh, remove, self.log = (os.path.join(self.tmp, n) for n in ("status.sh", "remove.sh", "remove.log"))
        write(status_sh, "#!/bin/sh\ncat <<'JSON'\n{}\nJSON\n".format(json.dumps(dict(
            {"uncommitted": 0, "unpushed": 0, "branch": "A:feat, B:feat", "removesBranches": True, "kind": "custom worktree",
             "repos": repos}, **status))))
        write(remove, '#!/bin/sh\necho "$@" >> "{}"\nmv "$1" "$1.gone"\n'.format(self.log))
        os.chmod(status_sh, 0o755)
        os.chmod(remove, 0o755)
        dump({"name": "test2", "claude": ["true"], "codex": ["true"],
              "worktree": {"marker": "marker.txt", "status": status_sh, "remove": remove}}, os.path.join(pa.ROOT, "profiles", "test2", "profile.json"))
        agent = load(os.path.join(self.dir, "agent.json"))
        agent["profile"] = "test2"
        dump(agent, os.path.join(self.dir, "agent.json"))
        self.add_task("t-003", "done", kind, [])
        self.repos = repos
        return kind

    def removals(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return f.read().splitlines()

    def test_v15_4_a_profile_worktree(self):
        kind = self.profile_tree()
        code, out, err = self.standing()
        self.assertEqual(code, 0, err + out)
        self.assertFalse(os.path.isdir(kind))
        self.assertEqual(self.removals(), [os.path.realpath(kind)], "the profile's remove script, with no --force")

    def test_v15_4b_untracked_in_a_repository_the_status_script_does_not_count(self):  # RL1
        kind = self.profile_tree()
        write(os.path.join(self.repos[1], "new.txt"), "x\n")
        code, out, err = self.standing()
        self.assertEqual(code, 0, err + out)
        self.assertTrue(os.path.isdir(kind))
        self.assertEqual(self.removals(), [])
        self.assertIn("1 uncommitted or untracked file(s) in B", out)

    # MF1: the re-read right before the removal checks every loss fact the first read checked
    def changed_at_the_re_read(self, change, kind=None, before=False):
        """Runs the standing removal with `change(row)` applied right after refresh_busy's re-read (before it, when the
        change is on disk and the re-read is to see it); t-003 must stay."""
        saved = pa_hooks.refresh_busy
        def refresh(row, agent, profile):
            if before:
                change(row)
            saved(row, agent, profile)
            if not before:
                change(row)
        with mock.patch.object(pa_hooks, "refresh_busy", refresh):
            code, out, err = self.standing()
        self.assertEqual(code, 0, err + out)
        self.assertTrue(os.path.isdir(kind or self.trees["t-003"]), "nothing is removed")
        self.assertIn("t-003 (main) (changed while it ran", out)
        self.assertIn("Removed 0 worktrees on rule r-wt: nothing is clean; 1 not clean, left for cards", out)
        return out

    def test_v15_5a_a_profile_worktree_unpushed_at_the_re_read(self):
        kind = self.profile_tree()
        self.changed_at_the_re_read(lambda row: row["facts"].update(unpushed=1), kind)
        self.assertEqual(self.removals(), [], "the remove script is not run, so never with --force")

    def test_v15_5b_a_profile_worktree_uncommitted_at_the_re_read(self):
        kind = self.profile_tree()
        self.changed_at_the_re_read(lambda row: row["facts"].update(uncommitted=2), kind)
        self.assertEqual(self.removals(), [])

    def test_v15_5c_unmerged_at_the_re_read(self):
        self.clean("t-003")
        self.changed_at_the_re_read(lambda row: self.commit(self.trees["t-003"], "late.txt", "x\n", "late, not merged"), before=True)

    def test_v15_5d_untracked_at_the_re_read(self):
        self.clean("t-003")
        self.changed_at_the_re_read(lambda row: write(os.path.join(self.trees["t-003"], "late.txt"), "x\n"), before=True)

    def test_v15_5e_unreadable_at_the_re_read(self):
        kind = self.profile_tree()
        out = self.changed_at_the_re_read(lambda row: row.update(facts={"unreadable": "the profile's worktree status script failed"}), kind)
        self.assertIn("status script failed", out)
        self.assertEqual(self.removals(), [])
        self.assertEqual(self.stops(), [], "a worktree left for its card is not a failed removal")

    def test_v15_5f_gone_at_the_re_read(self):
        self.clean("t-003")
        self.changed_at_the_re_read(lambda row: row.update(facts=None))


class OneRun(Standing):
    """R16: one run, one report line, one standing journal line."""

    def test_v16_1_three_clean_two_not(self):
        for tid in ("t-003", "t-004", "t-005"):
            self.clean(tid)
        self.clean("t-006")
        write(os.path.join(self.trees["t-006"], "README"), "x\n")
        path = self.clean("t-007", commit=False)
        self.commit(path, "x.txt", "x\n", "not merged")
        code, out, err = self.standing()
        self.assertEqual(code, 0, err)
        lines = out.strip().split("\n")
        report = [l for l in lines if l.startswith("Removed ")]
        self.assertEqual(len(report), 1, out)
        self.assertIn("Removed 3 worktrees on rule r-wt: t-003 (main), t-004 (main), t-005 (main); 2 not clean, left for cards: "
                      "t-006 (main) (", report[0])
        self.assertIn("t-007 (main) (not every commit", report[0])
        standing = [l for l in self.journal() if l.get("trigger") == "standing"]
        self.assertEqual(len(standing), 1)
        self.assertEqual((standing[0]["by"], standing[0]["rule"], standing[0]["textSha"], len(standing[0]["removed"])),
                         ("agent", "r-wt", self.push.text_sha(RULE_TEXT), 3))
        self.assertEqual(len([l for l in self.journal() if "on rule r-wt" in str(l.get("why")) and l.get("trigger") == "worktree"]), 3)

    def test_v16_1b_nothing_clean(self):
        code, out, err = self.standing()
        self.assertEqual(code, 0, err)
        self.assertIn("Removed 0 worktrees on rule r-wt: nothing is clean", out)
        self.assertEqual([l for l in self.journal() if l.get("trigger") == "standing"], [])

    def test_v16_2_a_failed_removal_stops_the_rule(self):
        for tid in ("t-003", "t-004", "t-005"):
            self.clean(tid)
        saved, calls = pa_hooks.remove_worktree, []
        def second_fails(path, facts):
            calls.append(path)
            if len(calls) == 2:
                raise pa.Problem("git would not remove it: locked")
            return saved(path, facts)
        pa_hooks.remove_worktree = second_fails
        self.addCleanup(setattr, pa_hooks, "remove_worktree", saved)
        code, out, err = self.standing()
        self.assertNotEqual(code, 0)
        self.assertFalse(os.path.isdir(self.trees["t-003"]))
        self.assertTrue(os.path.isdir(self.trees["t-004"]))
        self.assertTrue(os.path.isdir(self.trees["t-005"]), "the run stops")
        self.assertEqual(len(self.stops()), 1)
        pa_hooks.remove_worktree = saved
        code, out, err = self.standing()
        self.assertEqual(code, 2)
        self.assertIn("the rule stopped", err)
        self.assertTrue(os.path.isdir(self.trees["t-005"]))

    def test_v16_3_under_the_lock(self):
        self.clean("t-003")
        lock = open(os.path.join(self.dir, ".worktree.lock"), "a+")
        self.addCleanup(lock.close)
        fcntl.flock(lock, fcntl.LOCK_EX)
        with mock.patch.object(pa_hooks, "WORKTREE_LOCK_WAIT", 0.3):
            self.refused(self.standing(), "another pa-worktree remove is running")
