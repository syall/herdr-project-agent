"""Unit tests for pa-task archive and the readers of archived decision cards (t-054).

Run: python3 -m unittest -v tests/test_pa_archive.py
Uses test_pa_worker's temp homes, stub hc and git helpers (its module setup runs here too): no real space is read.
Old answered cards move from decisions/ to decisions-archive/ (the sibling folder the person already uses); open cards
never move, nothing is deleted, and every tool that reads a card by id still finds an archived one.
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import threading
import time
import unittest

import test_pa_worker as w
from test_pa_worker import Base, dump, load, raw, pa, pa_hooks

setUpModule = w.setUpModule
tearDownModule = w.tearDownModule

# pa_push, loaded against the same pa and pa_hooks.
_saved = {k: sys.modules.get(k) for k in ("pa", "pa_hooks")}
sys.modules["pa"], sys.modules["pa_hooks"] = pa, pa_hooks
_spec = importlib.util.spec_from_file_location("pa_push", os.path.join(w.TOOLS, "pa_push.py"))
pa_push = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pa_push)
for _k, _v in _saved.items():
    if _v is None:
        sys.modules.pop(_k, None)
    else:
        sys.modules[_k] = _v

OLD, NEW = "2026-01-01T10:00:00Z", None  # NEW: now, set per test


class ArchiveBase(Base):
    def setUp(self):
        super().setUp()
        self.saved_env = {k: os.environ.pop(k, None) for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_SANDBOX")}
        self.now = pa.now()

    def tearDown(self):
        for k, v in self.saved_env.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
        super().tearDown()

    def dec(self, cid):
        return os.path.join(self.dir, "decisions", cid + ".json")

    def arc(self, cid):
        return os.path.join(self.dir, "decisions-archive", cid + ".json")

    def put(self, cid, status="answered", answered=OLD, where="decisions", **extra):
        card = dict({"id": cid, "question": "Q {}?".format(cid), "options": [{"label": "A"}, {"label": "B"}], "status": status,
                     "answer": "A" if status == "answered" else None, "created": "2025-12-30T10:00:00Z", "answered": answered}, **extra)
        dump(card, os.path.join(self.dir, where, cid + ".json"))
        return card

    def run_pa(self, kind, *argv):
        out, err, code = io.StringIO(), io.StringIO(), 0
        saved = sys.argv
        sys.argv = [kind] + list(argv)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    pa.main()
                except pa.Problem as e:
                    print("{}: {}".format(kind, e), file=sys.stderr)
                    code = 2
                except SystemExit as e:
                    code = e.code if isinstance(e.code, int) else (0 if e.code is None else 2)
        finally:
            sys.argv = saved
        return code, out.getvalue(), err.getvalue()

    def archive(self, *extra):
        return self.run_pa("pa-task", "archive", "--space", "sp1", *extra)

    def names(self, sub):
        folder = os.path.join(self.dir, sub)
        return sorted(os.listdir(folder)) if os.path.isdir(folder) else []


class ArchiveTool(ArchiveBase):
    """R1, R2, R3: what moves, what stays, who may run it."""

    def test_a1_old_closed_cards_move_open_and_recent_stay(self):
        self.put("d-001")                                              # answered long ago: moves
        self.put("d-002", status="withdrawn", answered=OLD)            # withdrawn long ago: moves
        self.put("d-003", status="open", answered=None)                # open: never
        self.put("d-004", answered=self.now)                           # answered now: stays
        self.put("d-005", answered=None)                               # no stamp: age unknown, stays
        self.put("d-006", status="withdrawn", answered=None, withdrawnAt=OLD)  # pa-worker close's stamp: moves
        self.put("d-007", status="proposed")                           # another status: stays
        os.makedirs(os.path.join(self.dir, "decisions"), exist_ok=True)
        with open(self.dec("d-008"), "w") as f:
            f.write("{ not json")                                      # not a card: stays
        before = {n: raw(os.path.join(self.dir, "decisions", n)) for n in self.names("decisions")}
        code, out, err = self.archive()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.names("decisions-archive"), ["d-001.json", "d-002.json", "d-006.json"])
        self.assertEqual(self.names("decisions"), ["d-003.json", "d-004.json", "d-005.json", "d-007.json", "d-008.json"])
        for n in ("d-001.json", "d-002.json", "d-006.json"):
            self.assertEqual(raw(os.path.join(self.dir, "decisions-archive", n)), before[n], "moved as is")
        self.assertIn("3", out)
        entry = self.journal()[-1]
        self.assertEqual((entry["by"], entry["trigger"]), ("person", "archive"))
        self.assertIn("d-001", entry["why"])

    def test_a1b_days_option(self):
        self.put("d-001", answered="2026-11-10T10:00:00Z")
        code, _, err = self.archive("--days", "100000")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.names("decisions-archive"), [])
        code, _, err = self.archive("--days", "0")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.names("decisions-archive"), ["d-001.json"])

    def test_a1c_dry_run_moves_nothing(self):
        self.put("d-001")
        code, out, err = self.archive("--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("d-001", out)
        self.assertEqual(self.names("decisions"), ["d-001.json"])
        self.assertEqual(self.names("decisions-archive"), [])

    def test_a2_cards_of_a_task_not_done_or_parked_stay(self):
        self.add_task("t-001", "in-progress")
        self.add_task("t-002", "done")
        self.add_task("t-003", "parked")
        self.put("d-001", task="t-001")
        self.put("d-002", task="t-002")
        self.put("d-003", task="t-003")
        self.put("d-004", task="t-099")  # a task no longer in the ledger: by age only
        self.put("d-wt-t-002", task="t-002", answer="Keep", worktree="/x")  # every kind may move
        self.put("d-push-t-002-1", task="t-002", push={"version": 1, "done": True})
        self.put("d-leftovers", kind="leftovers", task=None)
        self.put("d-010", rule="r-1")
        code, _, err = self.archive()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.names("decisions"), ["d-001.json"])
        self.assertEqual(self.names("decisions-archive"), ["d-002.json", "d-003.json", "d-004.json", "d-010.json", "d-leftovers.json",
                                                           "d-push-t-002-1.json", "d-wt-t-002.json"])

    def test_a3_refused_inside_an_agent_except_dry_run(self):
        self.put("d-001")
        os.environ["CLAUDECODE"] = "1"
        code, _, err = self.archive()
        self.assertEqual(code, 2)
        self.assertIn("pa-task archive is the person's command", err)
        self.assertEqual(self.names("decisions-archive"), [])
        code, out, err = self.archive("--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("d-001", out)
        self.assertEqual(self.names("decisions"), ["d-001.json"])

    def test_a4_a_name_already_archived_is_left_and_reported(self):
        self.put("d-001", where="decisions-archive", note="the person's hand-moved copy")
        self.put("d-001")
        hand = raw(self.arc("d-001"))
        code, out, err = self.archive()
        self.assertEqual(code, 0, err)
        self.assertEqual(raw(self.arc("d-001")), hand, "never overwritten")
        self.assertTrue(os.path.exists(self.dec("d-001")))
        self.assertIn("d-001", out)
        self.assertIn("already in decisions-archive", out)

    def test_a5_a_card_that_turned_open_during_the_move_comes_back(self):
        self.put("d-001")
        real = pa.move_card

        def racing(src, dst):
            real(src, dst)
            if dst.endswith("d-001.json"):  # a writer replaced the card with an open one just before the move
                dump(dict(load(dst), status="open", answer=None), dst)
        pa.move_card = racing
        try:
            code, _, err = self.archive()
        finally:
            pa.move_card = real
        self.assertEqual(code, 0, err)
        self.assertEqual(self.names("decisions-archive"), [])
        self.assertEqual(load(self.dec("d-001"))["status"], "open")

    def test_a6_doctor_counts_archived_cards(self):
        self.put("d-001", where="decisions-archive")
        self.put("d-002", status="open", answered=None)
        code, out, err = self.run_pa("pa-doctor", "--space", "sp1")
        self.assertIn("1 open decision(s)", out + err)
        self.assertIn("1 archived decision(s)", out + err)


class Helpers(ArchiveBase):
    def test_h1_card_file_and_card_for_write(self):
        self.assertEqual(pa.card_file(self.dir, "d-001"), self.dec("d-001"))  # neither: decisions/
        self.put("d-001", where="decisions-archive")
        self.assertEqual(pa.card_file(self.dir, "d-001"), self.arc("d-001"))
        text = raw(self.arc("d-001"))
        self.assertEqual(pa.card_for_write(self.dir, "d-001"), self.dec("d-001"))
        self.assertEqual(raw(self.dec("d-001")), text, "moved back as is")
        self.assertFalse(os.path.exists(self.arc("d-001")))
        self.put("d-002", where="decisions-archive")
        self.put("d-002", where="decisions", note="newer")
        self.assertEqual(pa.card_file(self.dir, "d-002"), self.dec("d-002"), "decisions/ wins")
        self.assertEqual(pa.card_for_write(self.dir, "d-002"), self.dec("d-002"))
        self.assertTrue(os.path.exists(self.arc("d-002")), "the archived copy is left alone")


class Decide(ArchiveBase):
    def test_d1_answering_an_archived_card_says_already_answered(self):
        self.put("d-001", where="decisions-archive")
        code, _, err = self.run_pa("pa-decide", "d-001", "--answer", "A", "--quote", "yes", "--space", "sp1")
        self.assertEqual(code, 2)
        self.assertIn("d-001 is already answered", err)

    def test_d2_withdrawing_an_archived_other_answer_brings_it_back(self):
        self.put("d-001", where="decisions-archive", answer="Other", other=True, note="my own words")
        code, out, err = self.run_pa("pa-decide", "d-001", "--withdraw", "--why", "asked again", "--space", "sp1")
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(self.arc("d-001")))
        card = load(self.dec("d-001"))
        self.assertEqual((card["status"], card["previousAnswer"]), ("withdrawn", "Other"))


class Worktree(ArchiveBase):
    def setUp(self):
        super().setUp()
        self.proj = self.repo()
        self.wt = self.worktree(self.proj, "t-003-x")

    def check(self):
        return self.run_cmd("pa-worktree", "check", "--open-cards", "--space", "sp1")

    def test_w1_an_archived_keep_is_never_asked_again(self):
        self.add_task("t-003", "done", self.wt, [])
        self.put("d-wt-t-003", where="decisions-archive", task="t-003", worktree=self.wt, answer="Keep")
        before = raw(self.arc("d-wt-t-003"))
        code, out, err = self.check()
        self.assertEqual(code, 0, err)
        self.assertIn("kept, not asked again", out)
        self.assertFalse(os.path.exists(self.dec("d-wt-t-003")), "no new card")
        self.assertEqual(raw(self.arc("d-wt-t-003")), before, "a read moves nothing")

    def test_w2_an_archived_withdrawn_card_is_asked_again_in_decisions(self):
        self.add_task("t-003", "done", self.wt, [])
        self.put("d-wt-t-003", status="withdrawn", where="decisions-archive", task="t-003", worktree=self.wt, answer=None,
                 previousAnswer="Delete", previousNote="go", previousAnswered=OLD)
        code, out, err = self.check()
        self.assertEqual(code, 0, err)
        self.assertIn("opened card d-wt-t-003", out)
        self.assertFalse(os.path.exists(self.arc("d-wt-t-003")))
        card = load(self.dec("d-wt-t-003"))
        self.assertEqual((card["status"], card["previousAnswer"]), ("open", "Delete"))

    def test_w3_remove_reads_an_archived_delete(self):
        self.add_task("t-003", "done", self.wt, [])
        self.check()
        card = load(self.dec("d-wt-t-003"))
        card.update(status="answered", answer="Delete", answered=OLD)
        dump(card, self.dec("d-wt-t-003"))
        code, _, err = self.archive()
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.exists(self.arc("d-wt-t-003")))
        code, out, err = self.run_cmd("pa-worktree", "remove", "t-003", "--space", "sp1")
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.isdir(self.wt))

    def test_w4_remove_withdraws_an_archived_card_into_decisions(self):
        self.add_task("t-003", "done", self.wt, [])
        self.check()
        card = load(self.dec("d-wt-t-003"))
        card.update(status="answered", answer="Delete", answered=OLD)
        os.makedirs(os.path.join(self.dir, "decisions-archive"))
        dump(card, self.arc("d-wt-t-003"))
        os.rename(self.dec("d-wt-t-003"), os.path.join(self.tmp, "gone.json"))
        self.commit(self.wt, "late.txt", "late\n", "a commit after the card")
        code, _, err = self.run_cmd("pa-worktree", "remove", "t-003", "--space", "sp1")
        self.assertEqual(code, 2)
        self.assertIn("the card is withdrawn", err)
        self.assertFalse(os.path.exists(self.arc("d-wt-t-003")))
        self.assertEqual(load(self.dec("d-wt-t-003"))["status"], "withdrawn")


class Leftovers(ArchiveBase):
    def test_l1_the_last_leftovers_card_archived_still_counts(self):
        led = self.ledger()
        led["workerPolicy"] = {"style": "per-job", "since": OLD, "card": "d-leftovers", "cards": ["d-leftovers"]}
        self.ledger(led)
        self.put("d-leftovers", where="decisions-archive", kind="leftovers", task=None, answer="Close none", more=0,
                 workers=[{"address": "t31", "task": "t-001", "name": "n"}])
        code, out, err = self.worker("close", "--leftovers")
        self.assertEqual(code, 0, err)
        self.assertIn("--leftovers already ran", out)
        self.assertIn(("t31", "t-001", "n"), pa_hooks.shown_before(self.dir, led["workerPolicy"]))

    def test_l2_close_card_finds_an_archived_card(self):
        self.put("d-leftovers", where="decisions-archive", kind="leftovers", task=None, answer="Close none")
        with self.assertRaises(pa.Problem) as e:
            pa_hooks.leftovers_card(self.dir, "d-leftovers")
        self.assertIn("was answered 'Close none'", str(e.exception))
        self.put("d-leftovers-2", where="decisions-archive", kind="leftovers", task=None, answer="Other", other=True, note="keep t31")
        with self.assertRaises(pa.Problem):
            pa_hooks.leftovers_card(self.dir, "d-leftovers-2")
        self.assertFalse(os.path.exists(self.arc("d-leftovers-2")))
        self.assertEqual(load(self.dec("d-leftovers-2"))["status"], "withdrawn")


class Push(ArchiveBase):
    def test_p1_next_card_id_skips_an_archived_one(self):
        self.put("d-push-t-001-1", where="decisions-archive")
        self.assertEqual(pa_push.next_card_id(self.dir, "t-001"), "d-push-t-001-2")

    def test_p2_load_card_reads_an_archived_one(self):
        push = {"version": 1, "space": "sp1", "path": "/r", "url": "u", "branch": "main", "task": "t-001", "commit": "c", "done": True}
        self.put("d-push-t-001-1", where="decisions-archive", push=push)
        self.assertEqual(pa_push.load_card(self.dir, "sp1", "d-push-t-001-1")["push"]["done"], True)

    def test_p3_save_card_writes_in_decisions(self):
        card = self.put("d-push-t-001-1", where="decisions-archive", push={"version": 1})
        pa_push.save_card(self.dir, dict(card, note="x"))
        self.assertFalse(os.path.exists(self.arc("d-push-t-001-1")))
        self.assertEqual(load(self.dec("d-push-t-001-1"))["note"], "x")


class Trash(ArchiveBase):
    def test_t1_delete_takes_archived_cards_and_restore_puts_them_in_decisions(self):
        self.add_task("t-001", "done")
        self.put("d-001", task="t-001", where="decisions-archive")
        self.put("d-002", task="t-001")
        self.put("d-003", task="t-002", where="decisions-archive")
        code, _, err = self.run_pa("pa-task", "delete", "t-001", "--space", "sp1")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.names("decisions-archive"), ["d-003.json"])
        self.assertEqual(self.names("decisions"), [])
        code, _, err = self.run_pa("pa-task", "restore", "t-001", "--space", "sp1")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.names("decisions"), ["d-001.json", "d-002.json"])


class T131Limits(ArchiveBase):
    """t-131 (from t-064: t-054 R1, R2, N1): one lock order, no second copy from a writer racing the archive, unarchive,
    the later stamp, the README."""

    def blocked_while_locked(self, write):
        """Whether `write()` waits while another open file holds .archive.lock, and finishes once it is let go."""
        held = pa.archive_lock(self.dir)
        done = threading.Event()
        t = threading.Thread(target=lambda: (write(), done.set()))
        t.start()
        try:
            waited = not done.wait(0.4)
        finally:
            held.close()
        t.join(5)
        return waited, done.is_set()

    def test_c1_archive_takes_the_push_lock_first(self):
        self.put("d-001")
        order, real_a, real_p = [], pa.archive_lock, pa.push_card_lock
        pa.archive_lock = lambda d: (order.append("archive"), real_a(d))[1]
        pa.push_card_lock = lambda d: (order.append("push"), real_p(d))[1]
        try:
            code, _, err = self.archive()
        finally:
            pa.archive_lock, pa.push_card_lock = real_a, real_p
        self.assertEqual(code, 0, err)
        self.assertEqual(order, ["push", "archive"], "the order pa-decide and pa-push-main take them in")

    def test_c2_a_card_write_waits_for_the_archive(self):
        card = self.put("d-001", status="open", answered=None)
        waited, done = self.blocked_while_locked(lambda: pa.write_card(self.dir, self.dec("d-001"), dict(card, note="x")))
        self.assertTrue(waited, "a write of a card in decisions/ waits while pa-task archive holds its lock")
        self.assertTrue(done)
        self.assertEqual(load(self.dec("d-001"))["note"], "x")
        self.assertEqual(self.names("decisions-archive"), [])

    def test_c3_push_and_queue_card_writes_wait_too(self):
        card = self.put("d-push-t-001-1", status="open", answered=None, push={"version": 1})
        waited, done = self.blocked_while_locked(lambda: pa_push.save_card(self.dir, dict(card, note="y")))
        self.assertTrue(waited and done, "pa_push.save_card holds the archive lock")
        self.assertEqual(load(self.dec("d-push-t-001-1"))["note"], "y")
        queue = open(os.path.join(w.TOOLS, "pa_queue.py")).read()
        self.assertNotIn("pa.write_json(pa.card_for_write(", queue, "pa_queue writes cards through pa.put_card")
        self.assertIn("pa.put_card(", queue)

    def test_c4_a_writer_racing_the_archive_leaves_one_copy(self):
        card = self.put("d-001")
        real, about, go = pa.write_json, threading.Event(), threading.Event()

        def slow(path, value):  # the writer has its path in decisions/ and is about to write it
            if path.endswith("d-001.json") and value.get("note") == "later":
                about.set()
                go.wait(2)
            return real(path, value)
        pa.write_json = slow
        try:
            writer = threading.Thread(target=lambda: pa.write_card(self.dir, self.dec("d-001"), dict(card, note="later")))
            writer.start()
            self.assertTrue(about.wait(2))
            result = {}
            archiver = threading.Thread(target=lambda: result.update(zip(("code", "out", "err"), self.archive())))
            archiver.start()
            archiver.join(0.4)  # pa-task archive runs while the writer is between its path and its write
            go.set()
            writer.join(5)
            archiver.join(5)
        finally:
            pa.write_json = real
        self.assertEqual(result.get("code"), 0, result)
        both = [n for n in self.names("decisions") if n in self.names("decisions-archive")]
        self.assertEqual(both, [], "never one id in both folders")
        self.assertEqual(load(pa.card_file(self.dir, "d-001"))["note"], "later", "the writer's change is kept")

    def test_u1_unarchive_brings_a_card_back(self):
        before = self.put("d-001", where="decisions-archive")
        text = raw(self.arc("d-001"))
        code, out, err = self.run_pa("pa-task", "unarchive", "d-001", "--space", "sp1")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.names("decisions-archive"), [])
        self.assertEqual(raw(self.dec("d-001")), text, "moved as is")
        self.assertIn("d-001", out)
        entry = self.journal()[-1]
        self.assertEqual((entry["by"], entry["trigger"]), ("person", "archive"))
        self.assertIn("d-001", entry["why"])
        self.assertEqual(before["id"], "d-001")

    def test_u2_unarchive_refusals(self):
        self.put("d-001", where="decisions-archive")
        self.put("d-002", where="decisions-archive")
        self.put("d-002", note="live")
        for argv, words in ((("d-009",), "not in decisions-archive"), (("d-002",), "already in decisions/"),
                            ((), "needs a card id"), (("../x",), "card id")):
            with self.subTest(argv=argv):
                code, _, err = self.run_pa("pa-task", "unarchive", *argv, "--space", "sp1")
                self.assertEqual(code, 2, err)
                self.assertIn(words, err)
        self.assertEqual(load(self.dec("d-002"))["note"], "live")
        os.environ["CLAUDECODE"] = "1"
        code, _, err = self.run_pa("pa-task", "unarchive", "d-001", "--space", "sp1")
        self.assertEqual(code, 2)
        self.assertIn("pa-task unarchive is the person's command", err)
        self.assertEqual(self.names("decisions-archive"), ["d-001.json", "d-002.json"])

    def test_n1_the_later_stamp_counts(self):
        self.put("d-001", status="withdrawn", answered=OLD, withdrawnAt=self.now)  # answered long ago, withdrawn now
        self.put("d-002", status="withdrawn", answered=self.now, withdrawnAt=OLD)
        code, _, err = self.archive()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.names("decisions-archive"), [])
        self.assertEqual(pa.card_stamp({"answered": OLD, "withdrawnAt": "2026-02-01T00:00:00Z"}).month, 2)

    def test_r1_readme_says_archive_and_unarchive(self):
        text = " ".join(open(os.path.join(w.TOOLS, "..", "..", "README.md")).read().split())
        for words in ("`pa-task archive`", "`pa-task unarchive <card-id>`", "decisions-archive/", "--dry-run"):
            self.assertIn(words, text)


if __name__ == "__main__":
    unittest.main()
