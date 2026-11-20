"""t-140: a queue card that also pushes (spec Part A, R1-R9), and the person's rule as their standing approval to merge
and push (Part B, R10-R14).

Run: python3 -m unittest -v tests/test_pa_merge_push.py
Every case runs in scratch repositories under a mkdtemp folder and a fake space (tests/queue_fixtures.py, MBase of
tests/test_pa_queue_merge.py). The only remote is a bare repository under that folder (file://). The push tool's
callers are given as an environment and an ancestor list (the test seam C0 reads); no real panel, agent, space or
push. "The push tool" below is pa_push.py; its run is called as a function, never from a shell.
"""
import fcntl
import json
import os
import stat
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from queue_fixtures import *  # noqa: F401,F403
from queue_fixtures import setUpModule, tearDownModule  # noqa: F401
from test_pa_queue_merge import MBase, review_file  # noqa: E402

LIMITS = "Recorded limits: t-A none; t-B none; t-C none"
ALLP = "Merge all in order and push"
CONTEXT_PUSH = "Your answer also pushes the merged commits; the panel shows what it would push, read now."
CONTEXT_TODAY = "Nothing is pushed: the push card follows."
PERSON_ENV = {"HOME": TEST_ENV["HOME"], "PATH": "/usr/bin:/bin"}
PANEL_TREE = [{"pid": 31, "comm": "/Users/p/.local/bin/herdr-core", "args": "/Users/p/.local/bin/herdr-core"},
              {"pid": 1, "comm": "/sbin/launchd", "args": "/sbin/launchd"}]
PA_ENV = {"HOME": TEST_ENV["HOME"], "PATH": "/usr/bin:/bin", "CLAUDECODE": "1", "HC_TERMINAL_ID": "t0"}
PA_TREE = [{"pid": 21, "comm": "claude", "args": "claude"}, {"pid": 31, "comm": "/Users/p/.local/bin/herdr-core", "args": "herdr-core"},
           {"pid": 1, "comm": "/sbin/launchd", "args": "/sbin/launchd"}]
RULE = "r-standing-approval-to-merge-and-push-in-t"
RULE_TEXT = ("In this space, the person's standing approval stands in for the merge card and the push card. When a "
             "merge-queue entry has passed pa-gate and its checks, the Project Agent merges it with pa-queue merge, pushes "
             "it with pa-push-main, then runs pa-install-all. If entries are stacked, a short review must first find "
             "them independent. Each merge, push and install is reported to the person in one line. It stops, and falls "
             "back to cards, on a hold, an Other answer or note on any card, a failed check or gate, a commit after the "
             "checked commit, or a push pa-push-main refuses. Every refusal pa-push-main has today stays: allowlist, "
             "fast-forward only, the gate's commit.")
EARLY = "2000-01-01T00:00:00Z"
LATER = "2999-01-01T00:00:00Z"


def args_of(**kw):
    base = {"action": None, "path": None, "space": None, "url": None, "branch": None, "note": None, "pre_push": None,
            "standing": None, "unbind": False}
    base.update(kw)
    return type("Args", (), base)()


class Base(MBase):
    """MBase, plus a stand-in push tool on PATH (only where a case asks for it) and the push tool's callers."""

    def setUp(self):
        super().setUp()
        self.saved_path = os.environ.get("PATH", "")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.saved_lock_wait = pa_queue.LOCK_WAIT

    def tearDown(self):
        os.environ["PATH"] = self.saved_path
        pa_queue.LOCK_WAIT = self.saved_lock_wait
        super().tearDown()

    def tool_on_path(self):
        path = os.path.join(self.bin, "pa-push-main")
        write(path, "#!/bin/sh\nexit 0\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        os.environ["PATH"] = self.bin + os.pathsep + self.saved_path

    def set_up_push(self):
        self.allow()
        self.tool_on_path()

    def two(self, suites=("t-A", "t-B")):
        a = self.joined("t-A", "fa.txt")
        b = self.joined("t-B", "fb.txt")
        if suites:
            self.passed(*suites)
        return a, b

    def check(self, cid="d-queue-1", env=None, tree=None):
        return pa_push.check_card(self.dir, SPACE, cid, PERSON_ENV if env is None else env, PANEL_TREE if tree is None else tree)

    def run_card(self, label, cid="d-queue-1", seen=None, note=None, env=None, tree=None):
        if seen is None:
            seen = self.check(cid)["seen"]
        return pa_push.run_push(SPACE, cid, label, note, seen, PERSON_ENV if env is None else env, PANEL_TREE if tree is None else tree)

    def all_journal(self):
        path = os.path.join(self.dir, "journal.jsonl")
        return [json.loads(l) for l in read(path).splitlines() if l.strip()] if os.path.exists(path) else []

    def push_cards(self):
        folder = os.path.join(self.dir, "decisions")
        return sorted(n[:-5] for n in os.listdir(folder) if n.startswith("d-push-"))


# ---- R1: pa-queue card marks a pushing card ------------------------------------------------------------

class A1Card(Base):
    def test_a1_1_a_pushing_card(self):
        self.set_up_push()
        self.two()
        code, out, err = self.make_card(LIMITS)
        self.assertEqual(code, 0, out + err)
        c = self.card("d-queue-1")
        self.assertEqual(c["queue"]["push"], {"version": 1, "path": self.proj, "url": self.url, "branch": "main"})
        self.assertEqual([o["label"] for o in c["options"]], [ALLP, "Merge up to t-A and push", "Not now"])
        self.assertEqual([o["count"] for o in c["options"]], [2, 1, 0])
        self.assertEqual(c["recommendation"], ALLP)
        self.assertEqual(c["question"], "Merge 2 queued changes into main of proj, in order, and push main?")
        self.assertEqual(c["options"][0]["detail"], "Merges t-A, t-B into local main in order, then pushes main to {} as a "
                                                    "fast-forward of exactly those commits. No force.".format(self.url))
        self.assertEqual(c["options"][1]["detail"], "Merges t-A into local main in order, then pushes main to {} as a "
                                                    "fast-forward of exactly those commits. No force.".format(self.url))
        self.assertEqual(c["options"][2]["detail"], "Nothing merges or is pushed.")
        self.assertIn(CONTEXT_PUSH, c["context"])
        self.assertNotIn(CONTEXT_TODAY, c["context"])
        self.assertIn(LIMITS, c["context"])

    def today(self):
        code, out, err = self.make_card(LIMITS)
        self.assertEqual(code, 0, out + err)
        c = self.card("d-queue-1")
        self.assertNotIn("push", c["queue"])
        self.assertEqual([o["label"] for o in c["options"]], ["Merge all in order", "Merge up to t-A", "Not now"])
        self.assertEqual(c["options"][-1]["detail"], "Nothing merges.")
        self.assertEqual(c["recommendation"], "Merge all in order")
        self.assertTrue(c["question"].startswith("Merge 2 queued changes into main of proj, in order?"), c["question"])
        self.assertIn(CONTEXT_TODAY, c["context"])
        self.assertNotIn(CONTEXT_PUSH, c["context"])

    def test_a1_2_no_allowlist(self):
        self.tool_on_path()
        self.two()
        self.today()

    def test_a1_2_allowlist_off(self):
        self.set_up_push()
        allow = os.path.join(pa.ROOT, "push-allow.json")
        data = pa.read_json(allow)
        data["enabled"] = False
        dump(data, allow)
        self.two()
        self.today()

    def test_a1_2_entry_off(self):
        self.set_up_push()
        allow = os.path.join(pa.ROOT, "push-allow.json")
        data = pa.read_json(allow)
        data["repos"][0]["enabled"] = False
        dump(data, allow)
        self.two()
        self.today()

    def test_a1_2_entry_for_another_space(self):
        self.set_up_push()
        allow = os.path.join(pa.ROOT, "push-allow.json")
        data = pa.read_json(allow)
        data["repos"][0]["space"] = "sp2"
        dump(data, allow)
        self.two()
        self.today()

    def test_a1_2_tool_not_on_path(self):
        self.allow()
        self.two()
        self.today()

    def test_a1_3_and_push_labels_approve_by_count(self):
        self.set_up_push()
        self.two()
        self.assertEqual(self.make_card(LIMITS)[0], 0)
        path = os.path.join(self.dir, "decisions", "d-queue-1.json")
        c = pa.read_json(path)
        c.update(status="answered", answer="Merge up to t-A and push", answered=pa.now(), answeredBy="person, in the panel")
        dump(c, path)
        code, out, err = self.refresh("--no-merge")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.approval("t-A")["card"], "d-queue-1")
        self.assertIsNone(self.approval("t-B"))


# ---- R2: the push tool's check of a pushing queue card -------------------------------------------------

class A2Check(Base):
    def setUp(self):
        super().setUp()
        self.set_up_push()

    def test_a2_1_two_stacked_passed_entries(self):
        a, b = self.two()
        self.assertEqual(self.make_card(LIMITS)[0], 0)
        r = self.check()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["kind"], "queue")
        opt = r["options"][0]
        self.assertEqual(opt["label"], ALLP)
        self.assertEqual(opt["mergeNow"], ["t-A", "t-B"])
        self.assertIsNone(opt["waits"])
        self.assertTrue(opt["push"]["ok"], opt["push"])
        self.assertEqual([c["sha"] for c in opt["push"]["commits"]], [b, a])
        self.assertEqual(opt["push"]["commit"], b)
        self.assertEqual(opt["extra"], [])
        self.assertEqual(r["options"][1]["mergeNow"], ["t-A"])
        self.assertEqual([c["sha"] for c in r["options"][1]["push"]["commits"]], [a])
        self.assertEqual(len(r["options"]), 2)
        self.assertEqual((r["facts"]["url"], r["facts"]["branch"], r["facts"]["remoteNow"], r["facts"]["localNow"]),
                         (self.url, "main", self.m0, self.m0))
        self.assertTrue(isinstance(r["seen"], str) and len(r["seen"]) == 64)
        self.assertTrue(r["caller"]["ok"], r["caller"])
        self.assertEqual(self.check()["seen"], r["seen"], "the token is stable while nothing changes")

    def test_a2_2_the_second_suite_not_passed(self):
        self.two(suites=("t-A",))
        self.assertEqual(self.make_card(LIMITS)[0], 0)
        r = self.check()
        opt = r["options"][0]
        self.assertEqual(opt["mergeNow"], ["t-A"])
        self.assertEqual(opt["waits"]["task"], "t-B")
        self.assertIn("suite", opt["waits"]["why"])
        self.assertTrue(opt["push"]["ok"])
        self.assertEqual([c["sha"] for c in opt["push"]["commits"]], [self.tip("t-A")])

    def test_a2_3_allowlist_removed_after_the_card(self):
        self.two()
        self.assertEqual(self.make_card(LIMITS)[0], 0)
        os.remove(os.path.join(pa.ROOT, "push-allow.json"))
        r = self.check()
        opt = r["options"][0]
        self.assertEqual(opt["mergeNow"], ["t-A", "t-B"])
        self.assertFalse(opt["push"]["ok"])
        self.assertEqual(opt["push"]["refusal"]["check"], "C6a")

    def test_a2_4_the_check_writes_nothing(self):
        self.two()
        self.assertEqual(self.make_card(LIMITS)[0], 0)
        before, refs = snapshot(self.dir), self.refs()
        self.check()
        self.assertEqual(snapshot(self.dir), before)
        self.assertEqual(self.refs(), refs)

    def test_a2_6_the_tool_run_as_a_script(self):  # D15: run as a script, the queue tool shares this module
        import subprocess
        self.two()
        self.assertEqual(self.make_card(LIMITS)[0], 0)
        r = subprocess.run([sys.executable, os.path.join(TOOLS, "pa_push.py"), "main", "check", "d-queue-1", "--space", SPACE, "--json"],
                           cwd=self.tmp, env=dict(os.environ, HERDR_PA_HOME=pa.ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True)
        out = json.loads(r.stdout or "{}")
        self.assertTrue(out.get("ok"), r.stdout + r.stderr)
        self.assertEqual(out["options"][0]["mergeNow"], ["t-A", "t-B"])

    def test_a2_5_a_queue_card_that_does_not_push(self):
        os.environ["PATH"] = self.saved_path
        self.two()
        self.assertEqual(self.make_card(LIMITS)[0], 0)
        r = self.check()
        self.assertFalse(r["ok"])
        self.assertEqual(r["refusal"]["check"], "C3")
        self.assertIn("not a pushing queue card", r["refusal"]["message"])


# ---- R3, R5, R6: the run on a pushing queue card ------------------------------------------------------

class A3Run(Base):
    def setUp(self):
        super().setUp()
        self.set_up_push()

    def ready(self, suites=("t-A", "t-B")):
        a, b = self.two(suites)
        self.assertEqual(self.make_card(LIMITS)[0], 0)
        return a, b

    def test_a3_1_merge_and_push(self):
        a, b = self.ready()
        code, out = self.run_card(ALLP)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.ref("main"), b)
        self.assertEqual(self.remote(), "refs/heads/main " + b)
        self.assertEqual(self.merged(), ["t-A", "t-B"])
        c = self.card("d-queue-1")
        self.assertEqual((c["status"], c["answer"], c["answeredBy"]), ("answered", ALLP, "person, in the panel"))
        res = c["queue"]["push"]["result"]
        self.assertEqual((res["state"], res["from"], res["to"]), ("pushed", self.m0, b))
        self.assertEqual(res["merged"], ["t-A", "t-B"])
        self.assertEqual(len(self.journal("merge")), 2)
        pushes = [l for l in self.all_journal() if l.get("trigger") == "panel" and l.get("decision") == "d-queue-1"]
        self.assertEqual(len(pushes), 1, pushes)
        self.assertEqual(pushes[0]["by"], "person")
        self.assertEqual((pushes[0]["push"]["from"], pushes[0]["push"]["to"]), (self.m0, b))
        self.assertEqual(self.push_cards(), [])
        self.assertTrue(out["ok"] and out["done"], out)
        self.assertIn("merged t-A, t-B", out["message"])
        self.assertIn("pushed {} main".format(self.url), out["message"])

    def test_a3_2_a_stale_token(self):
        self.ready()
        refs, remote = self.refs(), self.remote()
        code, out = self.run_card(ALLP, seen="0" * 64)
        self.assertEqual(code, 1, out)
        self.assertEqual(out["refusal"]["check"], "C12")
        self.assertIn("look at the card again", out["refusal"]["message"])
        self.assertEqual((self.refs(), self.remote()), (refs, remote))
        self.assertIsNone(self.approval("t-A"))
        c = self.card("d-queue-1")
        self.assertEqual(c["status"], "open")
        self.assertNotIn("applied", c["queue"])
        self.assertEqual(c["queue"]["attempts"][-1]["check"], "C12")

    def test_a3_3_agent_callers_are_refused(self):
        self.ready()
        seen = self.check()["seen"]
        before = snapshot(self.dir)
        for env, tree in ((dict(PERSON_ENV, CLAUDECODE="1"), PANEL_TREE),
                          (PERSON_ENV, [{"pid": 9, "comm": "claude", "args": "claude"}] + PANEL_TREE),
                          (dict(PERSON_ENV, HC_TERMINAL_ID="t0"), PANEL_TREE)):
            code, out = self.run_card(ALLP, seen=seen, env=env, tree=tree)
            self.assertEqual(code, 1, out)
            self.assertEqual(out["refusal"]["check"], "C0")
        self.assertEqual(snapshot(self.dir), before)
        self.assertEqual(self.remote(), "refs/heads/main " + self.m0)

    def test_a3_4_not_now_and_other(self):
        self.ready()
        refs = self.refs()
        code, out = self.run_card("Other", seen="")
        self.assertEqual(code, 1, out)
        self.assertEqual(out["refusal"]["check"], "C5")
        self.assertEqual(self.card("d-queue-1")["status"], "open")
        code, out = self.run_card("Other", seen="", note="merge only A, and do not push yet")
        self.assertEqual(code, 0, out)
        c = self.card("d-queue-1")
        self.assertEqual((c["status"], c["answer"], c.get("other")), ("answered", "Other", True))
        self.assertEqual(self.refs(), refs)
        code, out2, err = self.refresh()
        self.assertEqual(self.merged(), [], out2 + err)
        self.assertIsNone(self.approval("t-A"))

    def test_a3_4b_not_now(self):
        self.ready()
        refs = self.refs()
        code, out = self.run_card("Not now", seen="")
        self.assertEqual(code, 0, out)
        c = self.card("d-queue-1")
        self.assertEqual((c["status"], c["answer"]), ("answered", "Not now"))
        self.assertEqual(self.refs(), refs)
        self.assertEqual(self.remote(), "refs/heads/main " + self.m0)
        self.refresh()
        self.assertEqual(self.merged(), [])

    def test_a3_4c_a_label_not_on_the_card(self):
        self.ready()
        code, out = self.run_card("Push")
        self.assertEqual((code, out["refusal"]["check"]), (1, "C5"))

    def test_a3_5_the_queue_lock_held(self):
        self.ready()
        seen = self.check()["seen"]
        pa_queue.LOCK_WAIT = 0.3
        refs = self.refs()
        with open(os.path.join(self.dir, ".queue.lock"), "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            code, out = self.run_card(ALLP, seen=seen)
        self.assertEqual(code, 1, out)
        self.assertEqual(out["refusal"]["check"], "C2")
        self.assertIn("another queue command is running; try again", out["refusal"]["message"])
        self.assertEqual(self.refs(), refs)
        self.assertEqual(self.card("d-queue-1")["status"], "open")

    def test_a3_5b_the_push_lock_held(self):
        self.ready()
        seen = self.check()["seen"]
        refs = self.refs()
        with open(os.path.join(self.dir, ".push.lock"), "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            code, out = self.run_card(ALLP, seen=seen)
        self.assertEqual((code, out["refusal"]["check"]), (1, "C2"), out)
        self.assertEqual(self.refs(), refs)
        self.assertEqual(self.card("d-queue-1")["status"], "open")
        # and the queue lock was let go: a queue command runs now
        self.assertEqual(self.refresh("--no-merge")[0], 0)

    def test_a3_6_a_commit_after_the_check_stops_the_merge(self):
        self.joined("t-A", "fa.txt")
        self.passed("t-A")
        self.assertEqual(self.make_card("Recorded limits: t-A none")[0], 0)
        seen = self.check()["seen"]
        real = pa_queue.apply_cards

        def late(ctx, rec):
            self.change("t-A", "fa.txt", "later\n")
            return real(ctx, rec)
        with mock.patch.object(pa_queue, "apply_cards", late):
            code, out = self.run_card("Merge all in order and push", seen=seen)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.merged(), [])
        self.assertEqual(self.remote(), "refs/heads/main " + self.m0)
        self.assertIn("a commit after the check", self.approval("t-A")["void"])
        res = self.card("d-queue-1")["queue"]["push"]["result"]
        self.assertEqual(res["state"], "none")

    # R5 rows
    def test_a5_1_nothing_could_merge(self):
        self.two(suites=())
        self.assertEqual(self.make_card(LIMITS)[0], 0)
        code, out = self.run_card(ALLP)
        self.assertEqual(code, 0, out)
        c = self.card("d-queue-1")
        self.assertEqual(c["status"], "answered")
        self.assertEqual(c["queue"]["push"]["result"]["state"], "none")
        self.assertEqual(self.approval("t-A")["card"], "d-queue-1")
        self.assertEqual(self.merged(), [])
        self.assertEqual(self.remote(), "refs/heads/main " + self.m0)
        self.assertEqual(self.push_cards(), [])
        # later, its suite passes and refresh merges it: a push card follows (Q1)
        self.passed("t-A", "t-B")
        code, out2, err = self.refresh()
        self.assertEqual(self.merged(), ["t-A", "t-B"], out2 + err)
        self.assertEqual(self.push_cards(), ["d-push-t-B-1"])
        self.assertEqual(self.remote(), "refs/heads/main " + self.m0)

    def test_a5_2_refused_before_the_push(self):
        a, b = self.ready()
        allow = os.path.join(pa.ROOT, "push-allow.json")
        data = pa.read_json(allow)
        data["repos"][0]["prePush"] = ["/bin/sh", "-c", "exit 3"]
        dump(data, allow)
        code, out = self.run_card(ALLP)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.ref("main"), b)
        self.assertEqual(self.remote(), "refs/heads/main " + self.m0)
        res = self.card("d-queue-1")["queue"]["push"]["result"]
        self.assertEqual((res["state"], res["check"]), ("refused", "C13"))
        self.assertEqual(res["pushCard"], "d-push-t-B-1")
        self.assertEqual(self.push_cards(), ["d-push-t-B-1"])
        self.assertIn("push card d-push-t-B-1 opened", out["message"])

    def test_a5_3_git_push_failed(self):
        a, b = self.ready()
        hook = os.path.join(self.origin, "hooks", "pre-receive")
        write(hook, "#!/bin/sh\nexit 1\n")
        os.chmod(hook, 0o755)
        code, out = self.run_card(ALLP)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.ref("main"), b)
        self.assertEqual(self.remote(), "refs/heads/main " + self.m0)
        res = self.card("d-queue-1")["queue"]["push"]["result"]
        self.assertEqual(res["state"], "failed")
        self.assertEqual(self.push_cards(), ["d-push-t-B-1"])

    def test_a5_4_read_back_differs(self):
        a, b = self.ready()
        real = pa_push.read_remote
        calls = []

        def odd(s, path, url, branch):
            calls.append(real(s, path, url, branch))
            return "f" * 40
        with mock.patch.object(pa_push, "read_remote", odd):
            code, out = self.run_card(ALLP)
        self.assertEqual(code, 0, out)
        res = self.card("d-queue-1")["queue"]["push"]["result"]
        self.assertEqual(res["state"], "unknown")
        self.assertEqual(self.push_cards(), [])
        self.assertIn("look at the remote", res["message"])

    def test_a6_1_local_main_ahead_of_the_remote(self):
        hand = self.on_main("README", "by hand\n")
        a, b = self.ready()
        code, out = self.run_card(ALLP)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.ref("main"), b)
        self.assertEqual(self.remote(), "refs/heads/main " + self.m0)
        res = self.card("d-queue-1")["queue"]["push"]["result"]
        self.assertEqual(res["state"], "refused")
        self.assertIn("local main has commits that are not from this card: 1; a push card lists them", res["message"])
        p = self.card(res["pushCard"])
        self.assertIn("- {} main README (t <t@example.invalid>) (not from a queued task)".format(hand[:7]), p["context"])

    def test_a6_2_the_check_shows_the_extra_commit(self):
        hand = self.on_main("README", "by hand\n")
        self.ready()
        self.assertEqual(self.check()["options"][0]["extra"], [hand])

    def test_a9_allowlist_removed_after_the_card(self):
        a, b = self.ready()
        os.remove(os.path.join(pa.ROOT, "push-allow.json"))
        code, out = self.run_card(ALLP)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.ref("main"), b)
        res = self.card("d-queue-1")["queue"]["push"]["result"]
        self.assertEqual((res["state"], res["check"]), ("refused", "C6a"))
        self.assertIsNone(res["pushCard"])
        self.assertIn("pushing is the person's step", res["message"])
        self.assertEqual(self.push_cards(), [])

    def test_a3_7_the_card_form_from_an_agent_is_still_refused(self):  # A14.5
        self.ready()
        code, out = self.run_card(ALLP, seen="x", env=PA_ENV, tree=PA_TREE)
        self.assertEqual((code, out["refusal"]["check"]), (1, "C0"))


# ---- R8: conversation answers -------------------------------------------------------------------------

class A8Decide(Base):
    def setUp(self):
        super().setUp()
        self.set_up_push()
        self.joined("t-A", "fa.txt")
        self.assertEqual(self.make_card("Recorded limits: t-A none")[0], 0)

    def test_a8_1_refused(self):
        code, out, err = self.run_main(pa, ["pa-decide", "d-queue-1", "--answer", ALLP, "--quote", "yes please", "--space", SPACE])
        self.assertNotEqual(code, 0, out + err)
        self.assertIn("a queue card that pushes is answered in the panel", out + err)
        self.assertEqual(self.card("d-queue-1")["status"], "open")

    def test_a8_2_withdraw_allowed(self):
        code, out, err = self.run_main(pa, ["pa-decide", "d-queue-1", "--withdraw", "--why", "moot", "--space", SPACE])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.card("d-queue-1")["status"], "withdrawn")

    def test_a8_3_withdraw_waits_for_the_push_lock(self):
        with open(os.path.join(self.dir, ".push.lock"), "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            with mock.patch.object(pa, "PUSH_LOCK_WAIT", 0.2):
                code, out, err = self.run_main(pa, ["pa-decide", "d-queue-1", "--withdraw", "--why", "moot", "--space", SPACE])
        self.assertNotEqual(code, 0, out + err)
        self.assertEqual(self.card("d-queue-1")["status"], "open")


# ---- Part B: the person's rule (R10-R14) -----------------------------------------------------------------

def person_rule(**over):
    r = {"id": RULE, "title": "Standing approval to merge and push", "text": RULE_TEXT, "why": "the person's", "appliesTo": "all",
         "status": "active", "order": 113, "source": "person",
         "history": [{"at": EARLY, "by": "person", "change": "created"}, {"at": EARLY, "by": "person", "change": "edited"}],
         "needsAttention": "the tools cannot follow it until t-140 ships"}
    r.update(over)
    return r


class Standing(Base):
    def setUp(self):
        super().setUp()
        self.allow()
        self.rules([person_rule()])
        self.bind()

    def rules(self, rules, directory=None):
        dump({"version": 1, "rules": rules}, os.path.join(directory or self.dir, "rules.json"))

    def bind(self, rule=RULE, env=None, tree=None):
        args = args_of(action="set", path=self.proj, space=SPACE, standing=rule)
        with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            return pa_push.allow_change(args, PERSON_ENV if env is None else env,
                                        [{"pid": 11, "comm": "/bin/zsh", "args": "-zsh"}, {"pid": 1, "comm": "/sbin/launchd", "args": "launchd"}]
                                        if tree is None else tree)

    def entry_of_allow(self):
        return pa.read_json(os.path.join(pa.ROOT, "push-allow.json"))["repos"][0]

    def in_force(self, rule=RULE):
        return pa_push.standing_rule(self.dir, SPACE, self.proj, rule)

    def not_in_force(self, words):
        with self.assertRaises(pa_push.Refused) as e:
            self.in_force()
        self.assertIn(words, e.exception.message)

    def smerge(self):
        return self.queue("merge", "--repo", self.proj, "--standing", RULE)

    def spush(self, env=None, tree=None, cwd=None):
        return pa_push.run_standing(SPACE, RULE, self.proj, PA_ENV if env is None else env, PA_TREE if tree is None else tree,
                                    cwd=cwd or self.tmp)

    def standing_merged(self):
        return [m["task"] for m in self.qrepo().get("merged") or [] if m.get("standing") == RULE]

    def stops(self):
        return [l for l in self.all_journal() if l.get("standing") == "stopped"]

    def stacked_need(self, tid):
        """The rule's independence need of a stacked entry (fix round 3: needs.stacked, beside a carry need)."""
        needs = self.entry(tid).get("needs") or {}
        return needs.get("stacked") or needs.get("independence")

    def independence(self, tid):
        reviewer = tid + "i"
        self.add_worker(tid, reviewer, "reviewer")
        need = self.stacked_need(tid)
        write(os.path.join(self.evidence(tid), need["file"]), review_file(self.tip(tid), reviewer, "pass", "yes"))
        code, out, err = self.refresh("--no-merge")
        self.assertEqual(code, 0, out + err)


class A10Binding(Standing):
    def test_a10_1_the_persons_rule_bound(self):
        e = self.entry_of_allow()
        import hashlib
        self.assertEqual(e["standing"], {"rule": RULE, "textSha": hashlib.sha256(RULE_TEXT.encode("utf-8")).hexdigest()})
        rule, entry = self.in_force()
        self.assertEqual(rule["id"], RULE)
        self.assertEqual(os.path.realpath(entry["path"]), self.proj)

    def test_a10_2_status_off(self):
        self.rules([person_rule(status="off")])
        self.not_in_force("not active")

    def test_a10_2_text_edited_after_binding(self):
        self.rules([person_rule(text=RULE_TEXT + " Also on Sundays.")])
        self.not_in_force("text changed")

    def test_a10_2_binding_missing(self):
        args = args_of(action="set", path=self.proj, space=SPACE, unbind=True)
        with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(pa_push.allow_change(args, PERSON_ENV, [{"pid": 11, "comm": "/bin/zsh", "args": "-zsh"},
                                                                     {"pid": 1, "comm": "launchd", "args": "launchd"}]), 0)
        self.assertNotIn("standing", self.entry_of_allow())
        self.not_in_force("not bound")

    def test_a10_2_binding_names_another_id(self):
        self.rules([person_rule(), person_rule(id="r-other")])
        self.assertEqual(self.bind("r-other"), 0)
        self.not_in_force("not bound")

    def test_a10_2_orchestrator_without_approval(self):
        self.rules([person_rule(source="orchestrator", history=[{"at": EARLY, "by": "orchestrator", "change": "proposed"}])])
        self.bind()
        self.not_in_force("not the person's")

    def test_a10_2_orchestrator_approved_by_the_person(self):
        self.rules([person_rule(source="orchestrator", history=[{"at": EARLY, "by": "orchestrator", "change": "proposed"},
                                                                {"at": EARLY, "by": "person", "change": "approved"}])])
        self.bind()
        self.in_force()

    def test_a10_2_a_later_entry_by_another_writer(self):
        self.rules([person_rule(history=person_rule()["history"] + [{"at": EARLY, "by": "orchestrator", "change": "edited"}])])
        self.not_in_force("not the person's")

    def test_a10_2_the_rule_in_another_space(self):
        other = os.path.join(pa.ROOT, "spaces", "sp2")
        self.rules([person_rule()], other)
        self.rules([])
        self.not_in_force("no rule")

    def test_a10_2_a_repo_without_the_binding(self):
        with self.assertRaises(pa_push.Refused) as e:
            pa_push.standing_rule(self.dir, SPACE, self.tmp, RULE)
        self.assertIn("not bound", e.exception.message)

    def test_a10_2_two_rules_with_the_id(self):
        self.rules([person_rule(), person_rule()])
        self.not_in_force("2 rules")

    def test_a10_2_applies_to_a_role(self):
        self.rules([person_rule(appliesTo="reviewer")])
        self.not_in_force("applies to")

    def test_a10_3_binding_from_an_agent_shell(self):
        before = snapshot(os.path.join(pa.ROOT, "push-allow.json"))
        self.assertEqual(self.bind(env=dict(PERSON_ENV, CLAUDECODE="1")), 1)
        self.assertEqual(self.bind(tree=PA_TREE), 1)
        self.assertEqual(snapshot(os.path.join(pa.ROOT, "push-allow.json")), before)

    def test_a10_3b_binding_needs_the_rule(self):
        self.assertEqual(self.bind("r-missing"), 1)

    def test_a10_4_needs_attention_has_no_effect(self):
        r = person_rule()
        r.pop("needsAttention")
        self.rules([r])
        self.in_force()
        self.rules([person_rule(needsAttention="something else")])
        self.in_force()

    def test_a10_5_the_allowlist_validates_the_binding(self):
        allow = os.path.join(pa.ROOT, "push-allow.json")
        data = pa.read_json(allow)
        data["repos"][0]["standing"] = {"rule": RULE}
        self.assertIn("standing", pa_push.validate(data))
        data["repos"][0]["standing"] = {"rule": RULE, "textSha": "x", "more": 1}
        self.assertIn("standing", pa_push.validate(data))


class A11Merge(Standing):
    def test_a11_1_two_stacked_entries_merge_on_the_rule(self):
        a, b = self.two()
        code, out, err = self.smerge()
        self.assertEqual(self.standing_merged(), ["t-A"], out + err)
        self.assertIn("independence", out)
        self.assertEqual(self.stops(), [])
        self.independence("t-B")
        code, out, err = self.smerge()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.standing_merged(), ["t-A", "t-B"], out + err)
        self.assertEqual(self.ref("main"), b)
        self.assertEqual(self.remote(), "refs/heads/main " + self.m0)
        self.assertEqual(self.push_cards(), [])
        lines = self.journal("merge")
        self.assertEqual([l["task"] for l in lines], ["t-A", "t-B"])
        for l in lines:
            self.assertEqual(l["rule"], RULE)
            self.assertEqual(len(l["textSha"]), 64)
        m = self.qrepo()["merged"]
        self.assertEqual([(x.get("card"), x.get("standing")) for x in m], [(None, RULE), (None, RULE)])
        self.assertIn("--standing " + RULE, out)

    def test_a11_1b_the_independence_review_sees_what_it_is_stacked_on(self):  # fix round 2, REVIEW-2 MF-1
        a, b = self.two()
        code, out, err = self.smerge()
        self.assertEqual(self.standing_merged(), ["t-A"], out + err)
        need = self.stacked_need("t-B")
        self.assertNotEqual(need["oldBase"], need["newBase"], need)
        self.assertEqual((need["oldBase"], need["newBase"]), (self.m0, a), need)
        stacked_on = sh(self.proj, "git", "diff", "--name-only", need["oldBase"], need["newBase"]).split()
        self.assertEqual(stacked_on, ["fa.txt"])
        change = sh(self.proj, "git", "diff", "--name-only", need["newBase"], need["tip"]).split()
        self.assertEqual(change, ["fb.txt"])
        self.assertIn("what it is stacked on: git diff {} {}".format(self.m0, a), out)
        question = [l for l in out.splitlines() if l.strip().startswith("question:")]
        self.assertEqual(len(question), 1, out)
        self.assertIn("stacked on", question[0])
        self.assertNotIn("since its passing suite", question[0])

    def test_a11_2_rule_not_in_force(self):
        self.two()
        self.rules([person_rule(status="off")])
        before, refs = snapshot(self.qfile()), self.refs()
        code, out, err = self.smerge()
        self.assertEqual(code, 1, out + err)
        self.assertIn("not active", err)
        self.assertEqual(snapshot(self.qfile()), before)
        self.assertEqual(self.refs(), refs)

    def test_a11_3_entry_2_suite_still_running(self):
        self.two(suites=("t-A",))
        code, out, err = self.smerge()
        self.assertEqual(self.standing_merged(), ["t-A"], out + err)
        self.assertEqual([e["task"] for e in self.entries()], ["t-B"])
        self.assertIsNone(self.approval("t-B"))
        self.assertEqual(self.stops(), [])

    def test_a11_4_refresh_never_merges_on_the_rule(self):
        self.joined("t-A", "fa.txt")
        self.passed("t-A")
        code, out, err = self.refresh()
        self.assertEqual(self.merged(), [], out + err)
        # a standing approval left over (a crash) is void at the next refresh
        self.set_entries(lambda r: r["entries"][0].update(approval={
            "standing": RULE, "textSha": "0" * 64, "answeredAt": pa.now(), "commit": r["entries"][0]["checked"]["commit"],
            "change": r["entries"][0]["checked"]["change"], "sequence": r["entries"][0]["checked"]["sequence"],
            "added": r["entries"][0]["added"], "instance": r["entries"][0]["instance"], "void": None}))
        code, out, err = self.refresh()
        self.assertEqual(self.merged(), [], out + err)
        self.assertTrue(self.approval("t-A")["void"])

    def test_a11_5_a_void_cause_writes_the_stop(self):
        self.joined("t-A", "fa.txt")
        self.passed("t-A")
        self.change("t-A", "fa.txt", "later\n")
        code, out, err = self.smerge()
        self.assertEqual(code, 1, out + err)
        self.assertEqual(self.merged(), [])
        s = self.stops()
        self.assertEqual(len(s), 1)
        self.assertEqual(s[0]["rule"], RULE)
        self.assertIn("a commit after the check", s[0]["why"])
        code, out, err = self.smerge()
        self.assertEqual(code, 1, out + err)
        self.assertIn("stopped", err)

    def test_a11_6_high_tier_waits_for_a_card(self):  # Q2
        self.joined("t-A", "fa.txt")
        led = self.ledger()
        next(t for t in led["tasks"] if t["id"] == "t-A")["tier"] = "high"
        self.set_ledger(led)
        self.passed("t-A")
        code, out, err = self.smerge()
        self.assertEqual(self.merged(), [], out + err)
        self.assertIn("high tier", out)
        self.assertEqual(self.stops(), [])

    def test_a11_7_a_paused_task_or_an_open_card_waits(self):
        self.joined("t-A", "fa.txt")
        self.passed("t-A")
        dump({"id": "d-9", "task": "t-A", "status": "open", "question": "Q?", "options": [{"label": "A"}]},
             os.path.join(self.dir, "decisions", "d-9.json"))
        code, out, err = self.smerge()
        self.assertEqual(self.merged(), [], out + err)
        self.assertIn("open card", out)
        os.remove(os.path.join(self.dir, "decisions", "d-9.json"))
        led = self.ledger()
        next(t for t in led["tasks"] if t["id"] == "t-A")["paused"] = True
        self.set_ledger(led)
        code, out, err = self.smerge()
        self.assertEqual(self.merged(), [], out + err)
        self.assertIn("paused", out)

    def test_a11_8_recorded_limits_in_the_report(self):  # Q3
        self.joined("t-A", "fa.txt")
        self.passed("t-A")
        write(os.path.join(self.evidence("t-A"), "REVIEW.md"), "text\n- **Recorded limit** RL-1: slow on big repos; follow-up t-200\n")
        self.gate("t-A")
        code, out, err = self.smerge()
        self.assertEqual(self.standing_merged(), ["t-A"], out + err)
        self.assertIn("RL-1: slow on big repos; follow-up t-200", out)


class A11StackedOrder(Standing):
    """Fix round 3: on a stacked entry, merge_check's stops come first, then the stacked review that names this pair
    and asks its own question, then the merge; the push card plans for the last merge of the run."""

    def carry_review(self, tid):
        reviewer = tid + "c"
        self.add_worker(tid, reviewer, "reviewer")
        need = self.entry(tid)["needs"]["independence"]
        write(os.path.join(self.evidence(tid), need["file"]), review_file(self.tip(tid), reviewer, "pass", "yes"))
        return need

    def test_a11_1c_a_carry_review_is_not_the_stacked_review(self):  # REVIEW-r2-1 MF-1
        self.joined("t-X", "fx.txt")
        a_old = self.joined("t-A", "fa.txt")
        self.joined("t-B", "fb.txt")
        self.suite("t-X", 1)
        self.suite("t-A", 0)
        self.suite("t-B", 0)
        code, out, err = self.refresh("--no-merge")
        self.assertEqual([e["task"] for e in self.entries()], ["t-A", "t-B"], out + err)
        for t in ("t-A", "t-B"):
            self.gate(t)
        self.carry_review("t-A")
        carry = self.carry_review("t-B")
        self.assertEqual((carry["oldBase"], carry["newBase"]), (a_old, self.tip("t-A")), carry)
        code, out, err = self.refresh("--no-merge")
        self.assertEqual([e["state"] for e in self.entries()], ["passed", "passed"], out + err)
        self.approve("Merge up to t-A")
        code, out, err = self.merge()
        self.assertEqual(self.merged(), ["t-A"], out + err)
        a_from = self.qrepo()["merged"][-1]["from"]
        remote = self.remote()
        code, out, err = self.smerge()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.standing_merged(), [], out + err)
        self.assertEqual(self.remote(), remote)
        self.assertIn("waits at t-B", out)
        self.assertEqual(self.stops(), [])
        needs = self.entry("t-B")["needs"]
        self.assertEqual(needs["independence"]["oldBase"], a_old, needs)  # the carry need is kept
        self.assertTrue(needs["stacked"]["standing"], needs)
        self.assertEqual((needs["stacked"]["oldBase"], needs["stacked"]["newBase"]), (a_from, self.tip("t-A")), needs)
        self.assertNotEqual(needs["stacked"]["file"], needs["independence"]["file"], needs)
        self.assertIn("what it is stacked on: git diff {} {}".format(a_from, self.tip("t-A")), out)
        # the stacked review, recorded with its pair, lets the rule merge it
        self.independence("t-B")
        line = [r for r in pa_hooks.review_runs(self.dir) if r.get("task") == "t-B" and r.get("standing")]
        self.assertEqual(len(line), 1)
        self.assertEqual((line[0]["oldBase"], line[0]["newBase"]), (a_from, self.tip("t-A")))
        code, out, err = self.smerge()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.standing_merged(), ["t-B"], out + err)

    def test_a11_5b_a_commit_after_the_check_on_a_stacked_entry_stops(self):  # REVIEW-r2-2 MF-1 (probe P1)
        a, b = self.two()
        code, out, err = self.smerge()
        self.assertEqual(self.standing_merged(), ["t-A"], out + err)
        self.change("t-B", "fb.txt", "later\n")
        new = self.tip("t-B")
        code, out, err = self.smerge()
        self.assertEqual(code, 1, out + err)
        self.assertEqual(self.standing_merged(), ["t-A"], out + err)
        s = self.stops()
        self.assertEqual(len(s), 1, out + err)
        self.assertIn("a commit after the check", s[0]["why"])
        self.assertNotIn("Checked: " + new, out)
        needs = self.entry("t-B").get("needs") or {}
        self.assertFalse(any(isinstance(n, dict) and n.get("tip") == new for n in needs.values()), needs)

    def test_a11_5c_a_failing_gate_on_a_stacked_entry_stops(self):  # REVIEW-r2-2 MF-1
        a, b = self.two()
        code, out, err = self.smerge()
        self.assertEqual(self.standing_merged(), ["t-A"], out + err)
        os.remove(os.path.join(self.evidence("t-B"), "REVIEW.md"))
        code, out, err = self.smerge()
        self.assertEqual(code, 1, out + err)
        self.assertEqual(self.standing_merged(), ["t-A"], out + err)
        self.assertNotIn("SHORT-REVIEW-t-B", out)
        s = self.stops()
        self.assertEqual(len(s), 1, out + err)
        self.assertIn("the gate fails now", s[0]["why"])

    def card_entry_stops(self, spoil, words):
        """Fix round 4, REVIEW-r3-2 MF-1 (probe P2): a card-approved front entry that fails for an R13 cause in a
        --standing run writes the stop; the next run refuses on it, though the branch is back at its checked tip."""
        a = self.joined("t-A", "fa.txt")
        self.passed("t-A")
        self.approve()
        self.refresh("--no-merge")
        self.assertEqual(self.approval("t-A").get("card"), "d-queue-1")
        undo = spoil(a)
        remote = self.remote()
        code, out, err = self.smerge()
        self.assertEqual(code, 1, out + err)
        self.assertEqual(self.merged(), [], out + err)
        s = self.stops()
        self.assertEqual(len(s), 1, out + err)
        self.assertIn(words, s[0]["why"])
        self.assertEqual(s[0].get("task"), "t-A", s)
        undo()
        code, out, err = self.smerge()
        self.assertEqual(code, 1, out + err)
        self.assertIn("stopped", err)
        self.assertEqual(self.merged(), [], out + err)
        self.assertEqual(self.remote(), remote)

    def test_a11_5d_a_commit_after_the_check_on_a_card_entry_stops(self):  # REVIEW-r3-2 MF-1 (probe P2)
        def spoil(a):
            self.change("t-A", "fa.txt", "later\n")
            return lambda: sh(self.trees["t-A"], "git", "reset", "-q", "--hard", a)
        self.card_entry_stops(spoil, "a commit after the check")

    def test_a11_5e_a_failing_gate_on_a_card_entry_stops(self):  # REVIEW-r3-2 MF-1
        def spoil(a):
            path = os.path.join(self.evidence("t-A"), "REVIEW.md")
            text = read(path)
            os.remove(path)
            return lambda: write(path, text)
        self.card_entry_stops(spoil, "the gate fails now")

    def not_independent(self, verdict, ind):
        """Fix round 4, VERIFY MF-r3-1: a stacked review that does not find t-B independent stops the rule."""
        a, b = self.two()
        code, out, err = self.smerge()
        self.assertEqual(self.standing_merged(), ["t-A"], out + err)
        self.add_worker("t-B", "t-Bi", "reviewer")
        need = self.stacked_need("t-B")
        write(os.path.join(self.evidence("t-B"), need["file"]), review_file(self.tip("t-B"), "t-Bi", verdict, ind))
        code, out, err = self.refresh("--no-merge")
        self.assertEqual(code, 0, out + err)
        remote = self.remote()
        code, out, err = self.smerge()
        self.assertEqual(code, 1, out + err)
        self.assertEqual(self.standing_merged(), ["t-A"], out + err)
        self.assertEqual(self.ref("main"), a)
        self.assertEqual(self.remote(), remote)
        s = self.stops()
        self.assertEqual(len(s), 1, out + err)
        self.assertIn("did not find it independent", s[0]["why"])
        self.assertIn("stopped at t-B: its independence review did not find it independent; its approval is void", out)
        self.assertTrue(self.approval("t-B") and self.approval("t-B").get("void"), self.approval("t-B"))
        ev = [l for l in self.all_journal() if l.get("event") == "merge-stopped" and l.get("task") == "t-B"]
        self.assertEqual(len(ev), 1, ev)

    def test_a11_1d_a_stacked_review_saying_not_independent_stops(self):  # VERIFY MF-r3-1
        self.not_independent("pass", "no")

    def test_a11_1e_a_stacked_review_with_must_fix_stops(self):  # VERIFY MF-r3-1
        self.not_independent("must fix (1)", "yes")

    def test_a11_9_a_card_merge_after_a_rule_merge_opens_the_push_card(self):  # REVIEW-r2-2 MF-2 (probe P6)
        a, b = self.two()
        self.approve()
        self.change("t-A", "fa.txt", "later\n")
        code, out, err = self.merge()
        self.assertTrue(self.approval("t-A")["void"], out + err)
        sh(self.trees["t-A"], "git", "reset", "-q", "--hard", a)
        code, out, err = self.smerge()
        self.assertEqual(code, 0, out + err)
        self.assertEqual([(m["task"], m.get("standing")) for m in self.qrepo()["merged"]], [("t-A", RULE), ("t-B", None)])
        self.assertEqual(self.ref("main"), b)
        self.assertNotIn("push card not opened", out)
        cards = self.push_cards()
        self.assertEqual(len(cards), 1, out + err)
        card = pa.read_json(os.path.join(self.dir, "decisions", cards[0] + ".json"))
        self.assertIn(b, json.dumps(card))


class A13Stops(Standing):
    def setUp(self):
        super().setUp()
        self.joined("t-A", "fa.txt")
        self.passed("t-A")

    def refused(self, words):
        before, refs = snapshot(self.qfile()), self.refs()
        code, out, err = self.smerge()
        self.assertEqual(code, 1, out + err)
        self.assertIn(words, err)
        self.assertEqual(self.merged(), [])
        self.assertEqual(self.refs(), refs)

    def test_a13_1_a_hold(self):
        pa_hooks.append_journal(self.dir, {"by": "person", "hold": "on"})
        self.refused("hold")
        self.assertEqual(len(self.stops()), 1)

    def raw_journal(self, *lines):
        with open(os.path.join(self.dir, "journal.jsonl"), "a", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")

    def stops_around_broken_lines(self):
        out = []
        for line in read(os.path.join(self.dir, "journal.jsonl")).splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if value.get("standing") == "stopped":
                out.append(value)
        return out

    def test_a13_1b_a_torn_hold_line_stops_as_pa_places_reads_it(self):  # fix round 2, REVIEW-2 MF-2
        self.raw_journal('{"by": "person", "hold": "o')
        self.refused("hold is unknown")
        self.assertEqual(len(self.stops_around_broken_lines()), 1)

    def test_a13_1c_a_broken_line_after_the_last_hold_line_stops(self):  # fix round 2, REVIEW-2 MF-2
        pa_hooks.append_journal(self.dir, {"by": "person", "hold": "off"})
        self.raw_journal("not json")
        code, out = self.spush()
        self.assertEqual((code, out["refusal"]["check"]), (1, "C0"), out)
        self.assertIn("hold is unknown", out["refusal"]["message"])
        self.assertEqual(len(self.stops_around_broken_lines()), 1)
        self.assertEqual(self.merged(), [])

    def test_a13_2_an_other_answer_on_any_card(self):
        dump({"id": "d-8", "task": "t-Z", "status": "answered", "answer": "Other", "other": True, "note": "wait",
              "answered": pa.now(), "question": "Q?", "options": []}, os.path.join(self.dir, "decisions-archive", "d-8.json"))
        self.refused("Other")

    def test_a13_3_a_note_written_in_the_panel(self):
        dump({"id": "d-8", "task": "t-Z", "status": "answered", "answer": "Yes", "note": "but slowly", "answeredBy": "person, in the panel",
              "answered": pa.now(), "question": "Q?", "options": []}, os.path.join(self.dir, "decisions", "d-8.json"))
        self.refused("note")

    def test_a13_4_cards_before_since_do_not_stop(self):
        dump({"id": "d-8", "task": "t-Z", "status": "answered", "answer": "Other", "other": True, "note": "wait",
              "answered": EARLY, "question": "Q?", "options": []}, os.path.join(self.dir, "decisions", "d-8.json"))
        self.rules([person_rule(history=[{"at": EARLY, "by": "person", "change": "created"},
                                          {"at": "2000-06-01T00:00:00Z", "by": "person", "change": "edited"}])])
        code, out, err = self.smerge()
        self.assertEqual(self.standing_merged(), ["t-A"], out + err)

    def test_a13_5_resume_when_the_person_turns_it_off_and_on(self):
        pa_hooks.append_journal(self.dir, {"by": "agent", "trigger": "standing", "standing": "stopped", "rule": RULE, "why": "x"})
        self.refused("stopped")
        self.rules([person_rule(history=person_rule()["history"] + [{"at": LATER, "by": "person", "change": "turned off"},
                                                                    {"at": LATER, "by": "person", "change": "turned on"}])])
        code, out, err = self.smerge()
        self.assertEqual(self.standing_merged(), ["t-A"], out + err)


class A14Push(Standing):
    def merged_two(self):
        a, b = self.two()
        self.smerge()
        self.independence("t-B")
        code, out, err = self.smerge()
        self.assertEqual(self.standing_merged(), ["t-A", "t-B"], out + err)
        return a, b

    def test_a14_1_one_push_of_the_rule_merges(self):
        a, b = self.merged_two()
        code, out = self.spush()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.remote(), "refs/heads/main " + b)
        lines = [l for l in self.all_journal() if l.get("trigger") == "standing" and l.get("push")]
        self.assertEqual(len(lines), 1)
        self.assertEqual((lines[0]["by"], lines[0]["rule"], lines[0]["push"]["from"], lines[0]["push"]["to"]), ("agent", RULE, self.m0, b))
        self.assertEqual(self.push_cards(), [])
        self.assertEqual(self.stops(), [])

    def test_a14_2_a_worker_is_refused(self):
        self.merged_two()
        wt = self.trees["t-A"]
        dump({"task": "t-A", "space": SPACE}, os.path.join(wt, ".claude", "pa-task.json"))
        code, out = self.spush(cwd=wt)
        self.assertEqual((code, out["refusal"]["check"]), (1, "C0"), out)
        code, out = self.spush(env=dict(PA_ENV, HC_TERMINAL_ID="t-Ab"))
        self.assertEqual((code, out["refusal"]["check"]), (1, "C0"), out)
        code, out = self.spush(env=dict(PA_ENV, HC_TERMINAL_ID="t77"))
        self.assertEqual((code, out["refusal"]["check"]), (1, "C0"), out)
        self.assertEqual(self.remote(), "refs/heads/main " + self.m0)

    def test_a14_2b_the_panel_or_the_person_may_not_use_this_form(self):
        self.merged_two()
        code, out = self.spush(env=PERSON_ENV, tree=PANEL_TREE)
        self.assertEqual((code, out["refusal"]["check"]), (1, "C0"), out)
        self.assertEqual(self.remote(), "refs/heads/main " + self.m0)

    def test_a14_2c_a_person_in_the_pas_terminal_is_not_an_agent(self):  # fix round 2, VERIFY MF-1
        self.merged_two()
        code, out = self.spush(env=dict(PERSON_ENV, HC_TERMINAL_ID="t0"), tree=PANEL_TREE)
        self.assertEqual((code, out["refusal"]["check"]), (1, "C0"), out)
        self.assertIn("not an agent", out["refusal"]["message"])
        self.assertEqual(self.remote(), "refs/heads/main " + self.m0)

    def test_a14_3_rule_not_in_force_or_stopped(self):
        self.merged_two()
        self.rules([person_rule(status="off")])
        code, out = self.spush()
        self.assertEqual((code, out["refusal"]["check"]), (1, "C0"), out)
        self.assertIn("not active", out["refusal"]["message"])
        self.rules([person_rule()])
        pa_hooks.append_journal(self.dir, {"by": "agent", "trigger": "standing", "standing": "stopped", "rule": RULE, "why": "x"})
        code, out = self.spush()
        self.assertEqual((code, out["refusal"]["check"]), (1, "C0"), out)
        self.assertIn("stopped", out["refusal"]["message"])
        self.assertEqual(self.remote(), "refs/heads/main " + self.m0)

    def refused_with(self, *checks):
        code, out = self.spush()
        self.assertEqual(code, 1, out)
        self.assertIn(out["refusal"]["check"], checks, out)
        self.assertEqual(self.remote().split()[-1] != "", True)
        self.assertEqual(len(self.stops()), 1)

    def test_a14_4_a_commit_after_the_gate(self):
        self.merged_two()
        self.change("t-B", "fb.txt", "later\n")
        self.refused_with("C8a", "C8f")
        self.assertEqual(self.remote(), "refs/heads/main " + self.m0)

    def test_a14_4_local_main_ahead_of_the_merged_tip(self):
        self.merged_two()
        self.on_main("README", "by hand\n")
        self.refused_with("C10a")
        self.assertEqual(self.remote(), "refs/heads/main " + self.m0)

    def test_a14_4_remote_not_an_ancestor(self):
        self.merged_two()
        other = os.path.join(self.tmp, "other-clone")
        sh(self.tmp, "git", "clone", "-q", "-b", "main", self.url, other)
        sh(other, "git", "config", "user.name", "t")
        sh(other, "git", "config", "user.email", "t@example.invalid")
        write(os.path.join(other, "z.txt"), "z\n")
        sh(other, "git", "add", "z.txt")
        sh(other, "git", "commit", "-q", "-m", "elsewhere")
        sh(other, "git", "push", "-q", "origin", "main")
        moved = self.remote()
        self.refused_with("C11c")
        self.assertEqual(self.remote(), moved)

    def test_a14_4_a_url_rewrite(self):
        self.merged_two()
        sh(self.proj, "git", "config", "url.file:///elsewhere/.insteadOf", self.url)
        self.refused_with("C7b", "C7c")

    def test_a14_4_allowlist_off(self):
        self.merged_two()
        allow = os.path.join(pa.ROOT, "push-allow.json")
        data = pa.read_json(allow)
        data["enabled"] = False
        dump(data, allow)
        code, out = self.spush()
        self.assertEqual(code, 1, out)
        self.assertIn(out["refusal"]["check"], ("C0", "C6c"))
        self.assertEqual(self.remote(), "refs/heads/main " + self.m0)

    def test_a14_6_commits_not_merged_on_the_rule(self):
        self.joined("t-A", "fa.txt")
        self.passed("t-A")
        self.approve(cid="d-queue-1")
        code, out, err = self.merge()
        self.assertEqual(self.merged(), ["t-A"], out + err)
        code, out = self.spush()
        self.assertEqual(code, 1, out)
        self.assertIn("not merged on the rule", out["refusal"]["message"])
        self.assertEqual(self.remote(), "refs/heads/main " + self.m0)


if __name__ == "__main__":
    unittest.main()
