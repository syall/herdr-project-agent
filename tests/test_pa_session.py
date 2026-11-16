"""Unit tests for pa.py's launcher liveness and resume rule (t-001: U1-U15).

Run: python3 -m unittest -v tests/test_pa_session.py
No hc calls and no real homes: HERDR_PA_HOME and CLAUDE_CONFIG_DIR point at a temp folder
before pa.py is loaded, and anything that would call hc is replaced.
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import signal
import builtins
import sys
import tempfile
import time
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="t001-unit-")
os.environ["HERDR_PA_HOME"] = os.path.join(TMP, "pa")
os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(TMP, "claude")
os.environ["HERDR_APP_HOME"] = os.path.join(TMP, "app")
os.environ.pop("HC_TERMINAL_ID", None)
spec = importlib.util.spec_from_file_location("pa", os.path.join(HERE, "..", "runtime", "tools", "pa.py"))
pa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pa)
assert pa.ROOT.startswith(TMP) and pa.CLAUDE_HOME.startswith(TMP), "pa.py must use the temp homes"

SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
T = 1794000000  # a fixed start time (epoch seconds)


def load(path):
    with open(path) as f:
        return json.load(f)


def dump(value, path):
    with open(path, "w") as f:
        json.dump(value, f)


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


class Base(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(pa.ROOT, ignore_errors=True)
        shutil.rmtree(pa.CLAUDE_HOME, ignore_errors=True)
        self.dir = os.path.join(pa.ROOT, "spaces", "sp1")
        os.makedirs(self.dir)
        self.project = os.path.join(TMP, "proj")
        os.makedirs(self.project, exist_ok=True)
        self.saved = {k: getattr(pa, k) for k in ("live_terminals", "hc_get", "call", "init_state")}

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(pa, k, v)

    def log(self, *lines):
        with open(os.path.join(self.dir, "session.log"), "w") as f:
            f.write("".join(line + "\n" for line in lines))

    def heartbeat(self, epoch, address="t1"):
        with open(os.path.join(self.dir, "heartbeat"), "w") as f:
            f.write("{} {}\n".format(int(epoch), address))


class ResumeRule(Base):
    def test_u1_no_session_id(self):
        self.assertEqual(pa.should_resume({}, self.dir, self.project), (False, "new"))

    def test_u2_flag(self):
        self.assertEqual(pa.should_resume({"sessionId": SID, "sessionStarted": True}, self.dir, self.project), (True, "flag"))

    def test_u3_killed_after_5s(self):
        self.log("{} start resume=False session={}".format(iso(T), SID))
        self.heartbeat(T + 5)
        self.assertEqual(pa.should_resume({"sessionId": SID, "sessionStarted": False}, self.dir, self.project), (False, "new"))

    def test_u4_migration_remote_space(self):
        # The remote space of the incident: started 2026-11-13T22:39:46Z, heartbeat a day later.
        self.log("2026-11-13T22:39:46Z start resume=False session={}".format(SID))
        self.heartbeat(1794696344)
        self.assertEqual(pa.should_resume({"sessionId": SID, "sessionStarted": False}, self.dir, self.project), (True, "ran"))

    def test_u5_exit_after_25s(self):
        self.log("{} start resume=False session={} reason=new".format(iso(T), SID), "{} exit code=0 after=25s".format(iso(T + 25)))
        self.assertEqual(pa.should_resume({"sessionId": SID}, self.dir, self.project), (True, "ran"))

    def test_u5b_quick_exit_then_heartbeat_does_not_count(self):
        # The launcher kept beating while it waited after quick exits: the session itself never ran.
        self.log("{} start resume=False session={}".format(iso(T), SID), "{} exit code=1 after=0s".format(iso(T)))
        self.heartbeat(T + 3600)
        self.assertEqual(pa.should_resume({"sessionId": SID}, self.dir, self.project), (False, "new"))

    def test_u5c_heartbeat_of_a_later_session_does_not_count(self):
        self.log("{} start resume=False session={}".format(iso(T), SID), "{} start resume=False session=other".format(iso(T + 2)))
        self.heartbeat(T + 3600)
        self.assertFalse(pa.session_ran(self.dir, SID))

    def test_u6_transcript(self):
        self.assertEqual(pa.claude_slug("/a/b.c"), "-a-b-c")
        folder = os.path.join(pa.CLAUDE_HOME, "projects", pa.claude_slug(self.project))
        os.makedirs(folder)
        self.assertIs(pa.transcript_state(self.project, SID), False)
        open(os.path.join(folder, SID + ".jsonl"), "w").close()
        self.assertIs(pa.transcript_state(self.project, SID), True)
        self.assertEqual(pa.should_resume({"sessionId": SID}, self.dir, self.project), (True, "transcript"))

    def test_u7_layout_unknown(self):
        self.assertIsNone(pa.transcript_state(self.project, SID))  # no projects/ folder
        os.makedirs(os.path.join(pa.CLAUDE_HOME, "projects"))
        self.assertIsNone(pa.transcript_state(self.project, SID))  # no folder for this project


class AfterExit(Base):
    def agent(self):
        return {"sessionId": SID, "sessionStarted": True}

    def test_u8_one_quick_failed_resume_keeps_the_session(self):
        a = pa.after_exit(self.agent(), True, 1, 2, 1, None)
        self.assertIs(a["sessionStarted"], True)
        self.assertEqual(a["sessionId"], SID)

    def test_u9_second_quick_failed_resume_gives_up(self):
        a = pa.after_exit(self.agent(), True, 1, 2, 2, None)
        self.assertIs(a["sessionStarted"], False)
        self.assertNotIn("sessionId", a)
        self.assertEqual(pa.should_resume(a, self.dir, self.project), (False, "new"))

    def test_u10_missing_transcript_gives_up_at_once(self):
        a = pa.after_exit(self.agent(), True, 1, 2, 1, False)
        self.assertIs(a["sessionStarted"], False)

    def test_u10b_long_run_marks_started(self):
        a = pa.after_exit({"sessionId": SID, "sessionStarted": False}, False, 0, 25, 0, None)
        self.assertIs(a["sessionStarted"], True)


class Liveness(Base):
    def test_u11_launcher_alive(self):
        now = T + 1000
        self.heartbeat(now - 10, "t1")
        self.assertTrue(pa.launcher_alive(self.dir, "t1", now=now))
        self.assertTrue(pa.launcher_alive(self.dir, "machine/t1", now=now))  # machine-qualified address
        self.assertFalse(pa.launcher_alive(self.dir, "t2", now=now))
        self.heartbeat(now - 31, "t1")
        self.assertFalse(pa.launcher_alive(self.dir, "t1", now=now))
        os.unlink(os.path.join(self.dir, "heartbeat"))
        self.assertFalse(pa.launcher_alive(self.dir, "t1", now=now))

    def test_u11b_other_launcher_alive_waits_on_an_ageing_heartbeat(self):
        self.heartbeat(time.time() - 3600, "t1")
        self.assertFalse(pa.other_launcher_alive(self.dir, "t1"))
        self.heartbeat(time.time() - 2, "t1")
        self.assertTrue(pa.other_launcher_alive(self.dir, "t1"))
        self.heartbeat(time.time() - 27, "t1")  # passes 30 s while watched: dead
        began = time.time()
        self.assertFalse(pa.other_launcher_alive(self.dir, "t1"))
        self.assertLess(time.time() - began, 10)

    def session_args(self, **kw):
        a = types.SimpleNamespace(space="sp1", project=self.project, profile="test", name=None, auto=False, ask=False, fresh=False)
        a.__dict__.update(kw)
        return a

    def fixtures(self):
        prof = os.path.join(pa.ROOT, "profiles", "test")
        os.makedirs(prof, exist_ok=True)
        with open(os.path.join(prof, "profile.json"), "w") as f:
            json.dump({"name": "test", "claude": ["true"], "codex": ["true"]}, f)
        with open(os.path.join(self.dir, "agent.json"), "w") as f:
            json.dump({"version": 1, "space": "sp1", "project": self.project, "profile": "test", "name": "proj", "terminal": "t9"}, f)
        pa.hc_get = lambda address: {"labels": {"herdr.space": "sp1"}} if address else {}
        pa.live_terminals = lambda: {"t9": {"state": "live", "labels": {"herdr.pa": "sp1"}}}
        pa.call = lambda argv, check=True: ""

    def test_u13_one_per_space_by_launcher_liveness(self):
        self.fixtures()

        class Reached(Exception):
            pass

        def stop(*a, **k):
            raise Reached()
        pa.init_state = stop
        os.environ["HC_TERMINAL_ID"] = "t1"
        try:
            self.heartbeat(time.time() - 1, "t9")
            with self.assertRaises(pa.Problem) as e:
                pa.run_session(self.session_args(), "t1", {}, "sp1", self.project, {"beat": False})
            self.assertIn("already has a Project Agent", str(e.exception))
            self.heartbeat(time.time() - 3600, "t9")  # its launcher is long gone: a shell was left
            with self.assertRaises(Reached):
                pa.run_session(self.session_args(), "t1", {}, "sp1", self.project, {"beat": False})
        finally:
            os.environ.pop("HC_TERMINAL_ID", None)

    def test_u14_pa_start_dry_run(self):
        self.fixtures()
        args = types.SimpleNamespace(folder=self.project, profile="test", space="sp1", app_space=None, dry_run=True)
        self.heartbeat(time.time() - 1, "t9")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            pa.cmd_start(args)
        self.assertIn("already has a Project Agent", out.getvalue())
        self.heartbeat(time.time() - 3600, "t9")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            pa.cmd_start(args)
        self.assertEqual(json.loads(out.getvalue())["argv"][0], "pa-session")

    def test_u15_init_state_clears_closed(self):
        self.fixtures()
        path = os.path.join(self.dir, "agent.json")
        agent = load(path)
        agent["closed"] = {"at": "2026-11-16T19:00:00Z", "by": "person", "tab": "tb1"}
        dump(agent, path)
        p = pa.load_profile("test")
        pa.init_state("sp1", self.project, p, terminal="t1", terms={})
        self.assertNotIn("closed", load(path))
        reg = load(os.path.join(pa.ROOT, "registry.json"))
        self.assertEqual([a["address"] for a in reg["agents"] if a["space"] == "sp1"], ["t1"])
        # Without a terminal (no launcher started) the mark stays.
        agent = load(path)
        agent["closed"] = {"by": "person"}
        dump(agent, path)
        pa.init_state("sp1", self.project, p)
        self.assertIn("closed", load(path))


class ReviewRound(Base):
    """REVIEW.md I1 (the launcher's own heartbeat) and M4 (Ctrl-C in error-wait)."""

    def test_u16_own_heartbeat_does_not_make_a_session_ran(self):
        # The reviewer's probe: session X started an hour ago in t9 and was killed after 5 s (no exit
        # line; t9's last beat at +5 s). A new launcher in t1 must start a new session, even though
        # its own first heartbeat is written before it decides.
        began = int(time.time()) - 3600
        self.log("{} start resume=False session={}".format(iso(began), SID))
        self.heartbeat(began + 5, "t9")
        prof = os.path.join(pa.ROOT, "profiles", "test")
        os.makedirs(prof, exist_ok=True)
        dump({"name": "test", "claude": ["true"], "codex": ["true"]}, os.path.join(prof, "profile.json"))
        dump({"version": 1, "space": "sp1", "project": self.project, "profile": "test", "name": "proj",
              "terminal": "t9", "sessionId": SID, "sessionStarted": False}, os.path.join(self.dir, "agent.json"))
        pa.hc_get = lambda address: {}
        pa.live_terminals = lambda: {}
        pa.call = lambda argv, check=True: ""
        heartbeat = self.heartbeat

        class FirstBeat:  # the real beat thread writes the heartbeat at once; do that, then nothing
            def __init__(self, target=None, daemon=None):
                pass

            def start(self):
                heartbeat(time.time(), "t1")
        started = []

        class Stop(Exception):
            pass

        def fake_call(argv, cwd=None):
            started.append(argv)
            raise Stop()
        saved = (pa.threading.Thread, pa.subprocess.call, signal.getsignal(signal.SIGINT))
        pa.threading.Thread, pa.subprocess.call = FirstBeat, fake_call
        try:
            args = types.SimpleNamespace(space="sp1", project=self.project, profile="test", name=None, auto=False, ask=False, fresh=False)
            with self.assertRaises(Stop):
                pa.run_session(args, "t1", {}, "sp1", self.project, {"beat": False})
        finally:
            pa.threading.Thread, pa.subprocess.call = saved[0], saved[1]
            signal.signal(signal.SIGINT, saved[2])
        argv = started[0]
        self.assertNotIn("--resume", argv)
        self.assertIn("--session-id", argv)
        self.assertNotEqual(argv[argv.index("--session-id") + 1], SID)

    def test_u17_error_wait_lets_ctrl_c_through(self):
        pa.call = lambda argv, check=True: ""
        seen = {}

        def fake_input(*a):
            seen["handler"] = signal.getsignal(signal.SIGINT)
            return ""
        saved_input, saved = builtins.input, signal.signal(signal.SIGINT, signal.SIG_IGN)
        builtins.input = fake_input
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                pa.error_wait("t1", pa.Problem("profile command not found: x"))
        finally:
            builtins.input = saved_input
            signal.signal(signal.SIGINT, saved)
        self.assertIs(seen["handler"], signal.default_int_handler)


class Round3(Base):
    """REVIEW.md round 2: R2-M1 (first start time kept across retries), R2-M2 (a 'starting'
    label), R2-N2 (bounded launch lock)."""

    def fixtures3(self, began):
        self.log("{} start resume=False session={}".format(iso(began), SID))  # killed 5 s in: never ran
        self.heartbeat(began + 5, "t9")
        prof = os.path.join(pa.ROOT, "profiles", "test")
        os.makedirs(prof, exist_ok=True)
        dump({"name": "test", "claude": ["true"], "codex": ["true"]}, os.path.join(prof, "profile.json"))
        dump({"version": 1, "space": "sp1", "project": self.project, "profile": "test", "name": "proj",
              "terminal": "t9", "sessionId": SID, "sessionStarted": False}, os.path.join(self.dir, "agent.json"))
        pa.hc_get = lambda address: {}
        pa.live_terminals = lambda: {}
        self.calls = []
        pa.call = lambda argv, check=True: self.calls.append(argv) or ""

    def run_once(self, ctx):
        started = []

        class Stop(Exception):
            pass

        class NoThread:
            def __init__(self, target=None, daemon=None):
                pass

            def start(self):
                pass

        def fake_call(argv, cwd=None):
            started.append(argv)
            raise Stop()
        saved = (pa.threading.Thread, pa.subprocess.call, signal.getsignal(signal.SIGINT))
        pa.threading.Thread, pa.subprocess.call = NoThread, fake_call
        try:
            args = types.SimpleNamespace(space="sp1", project=self.project, profile="test", name=None, auto=False, ask=False, fresh=False)
            with self.assertRaises(Stop):
                pa.run_session(args, "t1", {}, "sp1", self.project, ctx)
        finally:
            pa.threading.Thread, pa.subprocess.call = saved[0], saved[1]
            signal.signal(signal.SIGINT, saved[2])
        return started[0]

    def test_u18_retry_keeps_the_first_start_time(self):
        # This launcher started 30 s ago, beat during an error-wait (its own heartbeat, 2 s old), and
        # is retried now: its own beats still do not make the earlier, never-run session resumable.
        began = int(time.time()) - 3600
        self.fixtures3(began)
        self.heartbeat(time.time() - 2, "t1")
        os.makedirs(os.path.join(self.dir, "decisions"), exist_ok=True)
        argv = self.run_once({"beat": True, "started": time.time() - 30})
        self.assertNotIn("--resume", argv)

    def test_u19_claim_clears_the_starting_label(self):
        self.fixtures3(int(time.time()) - 3600)
        self.run_once({"beat": True})
        labels = [a for a in self.calls if a[:2] == ["hc", "label"]]
        self.assertTrue(any("herdr.pa.starting=" in a for a in labels), labels)
        self.assertTrue(any("herdr.pa=sp1" in a and "herdr.pa.starting=" in a for a in labels),
                        "the launcher's own labels and the cleared 'starting' label go together: {}".format(labels))

    def test_u20_launch_lock_wait_is_bounded(self):
        held = pa.launch_lock("sp1")
        try:
            began = time.time()
            with self.assertRaises(pa.Problem) as e:
                pa.launch_lock("sp1", wait=1)
            self.assertLess(time.time() - began, 5)
            self.assertIn("starting", str(e.exception))
        finally:
            held.close()
        pa.launch_lock("sp1", wait=1).close()  # free again once released


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
