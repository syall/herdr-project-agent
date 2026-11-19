"""pa-task next, pause and unpause, and the open-card refusal (t-097 J1: R11, R17-R22).

Run: python3 -m unittest -v tests/test_pa_task_order.py
Uses test_pa_worker's temp homes and stub hc (its module setup runs here too): no real space is read, no hc is called.
The space sp1's Project Agent is t0; a fresh heartbeat naming t0 and a live t0 labelled with the space make it
"running", so pa-task sends its notice (the only hc call it may make: it never starts or stops a worker).
"""
import contextlib
import io
import json
import os
import sys
import time
import unittest

import test_pa_worker as w
from test_pa_worker import Base, dump, load, raw

pa = w.pa

setUpModule = w.setUpModule
tearDownModule = w.tearDownModule

NEXT = ("The person asked to start task t-1 next: it is in progress now. Its next job goes first in the waiting list "
        "only while it is at the top of the task order (the panel's Start next puts it there; pa-task next alone "
        "changes only its state). Its after tasks, cards and holds still apply; no worker was started.")  # t-120 (J3 RL-1)
PAUSE = ("The person paused task t-1: let its running workers finish their job; start no new job for it, and block "
         "its waiting jobs (\"paused by the person\"), until they start it again.")
UNPAUSE = ("The person unpaused task t-1: unblock its waiting jobs paused by the person; its turn comes at its place "
           "in the task order.")
R17 = "Answer d-7 first, or ask the Project Agent to withdraw it."
PAUSED = {"at": "2026-11-19T00:00:00Z", "by": "person"}


class TaskBase(Base):
    def setUp(self):
        super().setUp()
        self.saved_env = {k: os.environ.pop(k, None) for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_SANDBOX")}
        self.term("t0", "project agent", labels={"herdr.pa": "sp1"})
        self.agent_running(True)

    def tearDown(self):
        for k, v in self.saved_env.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
        super().tearDown()

    def agent_running(self, yes):
        path = os.path.join(self.dir, "heartbeat")
        if yes:
            with open(path, "w") as f:
                f.write("{} t0\n".format(int(time.time())))
        elif os.path.exists(path):
            os.remove(path)

    def task(self, state, **extra):
        self.ledger({"version": 1, "tasks": [dict({"id": "t-1", "title": "One", "state": state, "workers": [],
                                                    "created": "2026-11-01T00:00:00Z"}, **extra),
                                              {"id": "t-2", "title": "Two", "state": "queued", "workers": []}]})

    def card(self, cid, task="t-1", status="open"):
        dump({"id": cid, "task": task, "question": "Q?", "options": [{"label": "A"}, {"label": "B"}], "status": status,
              "answer": "A" if status == "answered" else None, "created": "2026-11-19T00:00:00Z",
              "answered": "2026-11-19T01:00:00Z" if status == "answered" else None},
             os.path.join(self.dir, "decisions", cid + ".json"))

    def one(self):
        return next(t for t in self.ledger()["tasks"] if t["id"] == "t-1")

    def run_task(self, *argv):
        out, err, code = io.StringIO(), io.StringIO(), 0
        saved = sys.argv
        sys.argv = ["pa-task"] + list(argv) + ["--space", "sp1"]
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    pa.main()
                except pa.Problem as e:
                    print("pa-task: {}".format(e), file=sys.stderr)
                    code = 2
                except SystemExit as e:
                    code = e.code if isinstance(e.code, int) else (0 if e.code is None else 2)
        finally:
            sys.argv = saved
        return code, out.getvalue(), err.getvalue()

    def ok(self, *argv):
        code, out, err = self.run_task(*argv)
        self.assertEqual(code, 0, out + err)
        return out

    def snapshot(self):
        return raw(os.path.join(self.dir, "ledger.json")), raw(os.path.join(self.dir, "journal.jsonl"))

    def refused(self, *argv):
        before = self.snapshot()
        self.calls[:] = []
        code, out, err = self.run_task(*argv)
        self.assertNotEqual(code, 0, out + err)
        self.assertEqual(self.snapshot(), before, "nothing is written")
        self.assertEqual(self.calls, [], "no notice")
        return err

    def sends(self):
        return [c for c in self.calls if c[:2] == ["hc", "send"]]

    def last_line(self):
        return self.journal()[-1]


class Next(TaskBase):  # R18, R21
    def test_queued_to_in_progress(self):
        self.task("queued")
        out = self.ok("next", "t-1")
        self.assertIn("t-1: queued -> in-progress.", out)
        t = self.one()
        self.assertEqual(t["state"], "in-progress")
        self.assertNotIn("paused", t)
        line = self.last_line()
        self.assertEqual({k: line[k] for k in ("by", "trigger", "task", "from", "to", "why")},
                         {"by": "person", "trigger": "panel", "task": "t-1", "from": "queued", "to": "in-progress",
                          "why": "changed by the person with pa-task next"})
        self.assertEqual(self.calls, [["hc", "send", "t0", NEXT]])  # R11: no worker started, only the notice

    def test_clears_paused(self):  # R19: Start next on a paused task
        self.task("queued", paused=dict(PAUSED))
        self.ok("next", "t-1")
        self.assertNotIn("paused", self.one())
        self.assertEqual(self.one()["state"], "in-progress")

    def test_after_not_done_still_moves(self):  # R18, spec Q3: allowed; the after tasks still hold
        self.task("queued", after=["t-2"])
        self.ok("next", "t-1")
        self.assertEqual(self.one()["after"], ["t-2"])
        self.assertEqual(self.one()["state"], "in-progress")

    def test_other_states_refused(self):  # R21: each refuses a state it does not move from, the state named
        for state in ("in-progress", "investigating", "review", "needs-decision", "parked", "done"):
            with self.subTest(state=state):
                self.task(state)
                err = self.refused("next", "t-1")
                self.assertIn("t-1 is {}".format(state), err)
                self.assertIn("only a queued task", err)


class Pause(TaskBase):  # R19, R21
    def test_in_progress_and_investigating_to_queued(self):
        for state in ("in-progress", "investigating"):
            with self.subTest(state=state):
                self.task(state)
                self.calls[:] = []
                out = self.ok("pause", "t-1")
                self.assertIn("t-1: {} -> queued.".format(state), out)
                t = self.one()
                self.assertEqual(t["state"], "queued")
                self.assertEqual(set(t["paused"]), {"at", "by"})
                self.assertEqual(t["paused"]["by"], "person")
                self.assertRegex(t["paused"]["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
                line = self.last_line()
                self.assertEqual((line["from"], line["to"], line["why"]), (state, "queued", "changed by the person with pa-task pause"))
                self.assertEqual(self.calls, [["hc", "send", "t0", PAUSE]])  # nothing is stopped

    def test_other_states_refused(self):
        for state in ("queued", "review", "needs-decision", "parked", "done"):
            with self.subTest(state=state):
                self.task(state)
                err = self.refused("pause", "t-1")
                self.assertIn("t-1 is {}".format(state), err)
                self.assertIn("only an in-progress or investigating task", err)

    def test_workers_are_left_alone(self):  # R11: a live worker of the task is not ended
        self.term("t5", "t-1-build")
        self.task("in-progress", workers=[{"address": "t5", "role": "build"}])
        self.ok("pause", "t-1")
        self.assertEqual(self.ended(), [])
        self.assertEqual(self.terms["t5"]["state"], "live")


class Unpause(TaskBase):  # R19
    def test_clears_paused_and_stays_queued(self):
        self.task("queued", paused=dict(PAUSED))
        out = self.ok("unpause", "t-1")
        self.assertIn("t-1", out)
        t = self.one()
        self.assertEqual(t["state"], "queued")
        self.assertNotIn("paused", t)
        line = self.last_line()
        self.assertEqual((line["from"], line["to"], line["why"]), ("queued", "queued", "changed by the person with pa-task unpause"))
        self.assertEqual(self.calls, [["hc", "send", "t0", UNPAUSE]])

    def test_not_paused_changes_nothing(self):
        self.task("queued")
        before = self.snapshot()
        out = self.ok("unpause", "t-1")
        self.assertIn("t-1 is not paused.", out)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.calls, [])

    def test_pause_then_unpause_then_next(self):
        self.task("in-progress")
        self.ok("pause", "t-1")
        self.ok("unpause", "t-1")
        self.ok("next", "t-1")
        self.assertEqual(self.one()["state"], "in-progress")
        self.assertEqual([l["why"] for l in self.journal()][-3:],
                         ["changed by the person with pa-task pause", "changed by the person with pa-task unpause",
                          "changed by the person with pa-task next"])


class OpenCard(TaskBase):  # R17
    def test_refused_with_an_open_card(self):
        for action in ("done", "park", "pause", "next"):
            with self.subTest(action=action):
                self.task("needs-decision")
                self.card("d-7")
                err = self.refused(action, "t-1")
                self.assertIn(R17, err)
                self.assertIn("d-7", err)
                self.assertEqual(load(os.path.join(self.dir, "decisions", "d-7.json"))["status"], "open")  # answers nothing

    def test_allowed_once_the_card_is_answered(self):
        self.task("needs-decision")
        self.card("d-7", status="answered")
        self.ok("park", "t-1")
        self.assertEqual(self.one()["state"], "parked")

    def test_another_task_s_card_does_not_count(self):
        self.task("needs-decision")
        self.card("d-7", task="t-2")
        self.ok("done", "t-1")
        self.assertEqual(self.one()["state"], "done")

    def test_an_open_card_on_another_state_does_not_refuse(self):  # R17 is about leaving Needs decision
        self.task("in-progress")
        self.card("d-7")
        self.ok("pause", "t-1")
        self.assertEqual(self.one()["state"], "queued")

    def test_delete_is_not_refused(self):  # R17: Delete stays; it answers nothing
        self.task("needs-decision")
        self.card("d-7")
        self.ok("delete", "t-1")
        self.assertNotIn("t-1", [t["id"] for t in self.ledger()["tasks"]])
        trash = os.path.join(self.dir, "trash")
        moved = [os.path.join(trash, n, "decisions", "d-7.json") for n in os.listdir(trash)]
        self.assertEqual(load(moved[0])["status"], "open")


class Unchanged(TaskBase):  # R6, R20, R1: the order file is never written; done, park, reopen as before
    def test_task_order_file_never_written(self):
        path = os.path.join(self.dir, "task-order.json")
        self.task("queued")
        self.ok("next", "t-1")
        self.ok("pause", "t-1")
        self.ok("unpause", "t-1")
        self.assertFalse(os.path.exists(path))
        text = json.dumps({"version": 1, "updated": "U1", "by": "person", "order": ["t-2", "t-1"], "last": []}) + "\n"
        with open(path, "w") as f:
            f.write(text)
        self.ok("next", "t-1")
        self.ok("pause", "t-1")
        self.ok("park", "t-1")
        self.ok("reopen", "t-1")
        self.assertEqual(raw(path), text.encode())

    def test_done_park_reopen_as_today(self):
        self.task("in-progress")
        self.ok("done", "t-1")
        self.assertEqual(self.sends()[-1], ["hc", "send", "t0", "The person changed task t-1 from in-progress to done in the panel. "
                                            "Re-read ledger.json before you write it again, and do not undo this."])
        self.ok("reopen", "t-1")
        self.ok("park", "t-1")
        self.assertEqual(self.one()["state"], "parked")
        self.assertIn("already parked", self.ok("park", "t-1"))

    def test_no_notice_when_the_agent_is_not_running(self):
        self.agent_running(False)
        self.task("queued")
        out = self.ok("next", "t-1")
        self.assertIn("The Project Agent is not running", out)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.one()["state"], "in-progress")

    def test_an_agent_may_run_them(self):  # J1-D5: as done and park, no agent refusal
        os.environ["CLAUDECODE"] = "1"
        self.task("in-progress")
        self.ok("pause", "t-1")
        self.ok("next", "t-1")
        self.assertEqual(self.one()["state"], "in-progress")

    def test_unknown_task(self):
        self.task("queued")
        err = self.refused("next", "t-9")
        self.assertIn("no task t-9", err)


def places_tool():
    """runtime/tools/pa_places.py over this module's pa (sys.modules restored after the load)."""
    import importlib.util
    saved = {k: sys.modules.get(k) for k in ("pa", "pa_hooks")}
    sys.modules["pa"], sys.modules["pa_hooks"] = pa, w.pa_hooks
    try:
        spec = importlib.util.spec_from_file_location("pa_places_t120", os.path.join(w.TOOLS, "pa_places.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


class T120Reopen(TaskBase):  # t-120 (t-097 J3 rebase RL-R1, R5): a reopened done task starts at the bottom of Queued
    def order_ledger(self, state, task_order):
        self.ledger({"version": 1, "taskOrder": task_order,
                     "tasks": [{"id": "t-1", "title": "One", "state": state, "workers": [], "created": "2026-11-01T00:00:00Z"},
                               {"id": "t-2", "title": "Two", "state": "queued", "workers": []}]})

    def test_reopen_of_done_stamps_and_leaves_the_agent_order(self):
        self.order_ledger("done", {"order": ["t-1", "t-2"], "chat": [{"task": "t-1", "quote": "now", "over": None},
                                                                     {"task": "t-2", "quote": "too", "over": None}]})
        self.ok("reopen", "t-1")
        t = self.one()
        self.assertEqual(t["state"], "queued")
        self.assertEqual(t["reopened"], t["updated"])
        self.assertEqual(self.ledger()["taskOrder"], {"order": ["t-2"], "chat": [{"task": "t-2", "quote": "too", "over": None}]})

    def test_reopen_of_parked_keeps_its_place(self):  # R5: a parked task comes back where it was
        self.order_ledger("parked", {"order": ["t-1", "t-2"], "chat": []})
        self.ok("reopen", "t-1")
        self.assertNotIn("reopened", self.one())
        self.assertEqual(self.ledger()["taskOrder"], {"order": ["t-1", "t-2"], "chat": []})

    def test_reopen_with_no_task_order_writes_none(self):
        self.task("done")
        self.ok("reopen", "t-1")
        self.assertIn("reopened", self.one())
        self.assertNotIn("taskOrder", self.ledger())

    def test_the_ranking_puts_it_last(self):  # through pa_places with the file still naming it first
        pa_places = places_tool()
        path = os.path.join(self.dir, "task-order.json")
        with open(path, "w") as f:
            f.write(json.dumps({"version": 1, "updated": "2026-11-01T00:00:00Z", "by": "person", "order": ["t-1"], "last": []}))
        self.order_ledger("done", {"order": ["t-1"], "chat": []})
        self.ok("reopen", "t-1")
        doc, _e, _w = pa_places.read_task_order(raw(path).decode())
        got = pa_places.effective_tasks(self.ledger()["tasks"], self.ledger()["taskOrder"], doc)
        self.assertEqual([r["id"] for r in got], ["t-2", "t-1"])


if __name__ == "__main__":
    unittest.main()
