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


class Round3Addition(Round3):
    """VERIFY.md round 2: the migration case through the real start path (A1.3), and the launch flock."""
    test_u18_retry_keeps_the_first_start_time = None  # inherited helpers only, not Round3's tests
    test_u19_claim_clears_the_starting_label = None
    test_u20_launch_lock_wait_is_bounded = None

    def test_u21_migration_resumes_through_the_real_start_path(self):
        # The remote space this task exists for: sessionStarted false, no exit line, and the previous
        # launcher (t9) kept beating for a day after the start. claim() must not erase that evidence.
        began = int(time.time()) - 2 * 86400
        self.fixtures3(began)
        self.heartbeat(began + 86400, "t9")
        argv = self.run_once({"beat": False})
        self.assertIn("--resume", argv)
        self.assertEqual(argv[argv.index("--resume") + 1], SID)
        with open(os.path.join(self.dir, "session.log")) as f:
            self.assertIn("start resume=True session={} reason=ran".format(SID), f.read().splitlines()[-1])

    def test_u28_resume_prompt_continues_in_flight_work(self):  # t-017 R3 AC3.1-AC3.3, G5 (J4)
        began = int(time.time()) - 2 * 86400
        self.fixtures3(began)
        self.heartbeat(began + 86400, "t9")
        argv = self.run_once({"beat": False})
        self.assertIn("--resume", argv)
        self.assertEqual(argv[-1], "Your session was restarted. Read your state folder (ledger.json, journal.jsonl, "
                         "decisions/, rules.json), then say in one line where things stand. Continue where you left "
                         "off without asking, as orchestrator.md defines it under \"After a restart or a "
                         "compaction\": read the reports that came in, take the next planned step, and start a fresh "
                         "worker for a job whose worker stopped. Start nothing else until asked: no new task, no move "
                         "to a phase that needs the person, no worker for a task with an open card. If the last hold "
                         "or resume line in the journal is a hold, start nothing at all until the person says go. "
                         "`paused` in `schedule.json` stops timed passes only; it is not a hold.")  # t-034
        self.assertNotIn("Do not start new work until asked.", argv[-1])
        self.assertNotIn("start nothing new", argv[-1])
        self.assertNotIn("passes are paused", argv[-1])
        # t-034: the prompt and orchestrator.md agree word for word where they overlap
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "..", "runtime", "core", "orchestrator.md"), encoding="utf-8") as f:
            core = " ".join(f.read().split())
        for clause in ("where you left off without asking",
                       "Start nothing else until asked: no new task, no move to a phase that needs the person, no "
                       "worker for a task with an open card.",
                       "is a hold, start nothing at all until the person says go.",
                       "`paused` in `schedule.json` stops timed passes only; it is not a hold."):
            self.assertIn(clause, argv[-1])
            self.assertIn(clause, core)

    def test_u22_two_launchers_never_claim_at_once(self):
        # Two launchers for one space start together. While the first is inside claim (init_state,
        # labelling, first heartbeat), the second must not enter it: the launch lock covers claim.
        import threading
        self.fixtures3(int(time.time()) - 3600)
        inside, gate, entered, errors = [], threading.Event(), threading.Event(), []

        class Stop(Exception):
            pass

        def fake_init_state(*a, **k):
            inside.append(threading.current_thread().name)
            if len(inside) == 1:
                entered.set()
                gate.wait(10)  # the first launcher stays inside claim until released
            raise Stop()
        pa.init_state = fake_init_state

        def launcher():
            try:
                args = types.SimpleNamespace(space="sp1", project=self.project, profile="test", name=None, auto=False, ask=False, fresh=False)
                pa.run_session(args, "t1", {}, "sp1", self.project, {"beat": True})
            except Stop:
                pass
            except Exception as e:  # noqa: BLE001 - reported below
                errors.append(e)
        first = threading.Thread(target=launcher, name="first", daemon=True)
        first.start()
        self.assertTrue(entered.wait(5), "the first launcher reached claim")
        second = threading.Thread(target=launcher, name="second", daemon=True)
        second.start()
        time.sleep(1.5)
        self.assertEqual(inside, ["first"], "the second launcher entered claim while the first was inside it")
        gate.set()
        first.join(10)
        second.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(inside, ["first", "second"])


class Round4(Round3):
    """REVIEW.md round 3 (R3-M2) and VERIFY.md round 3 survivors (L2, L4, L5, N10)."""
    test_u18_retry_keeps_the_first_start_time = None  # inherited helpers only
    test_u19_claim_clears_the_starting_label = None
    test_u20_launch_lock_wait_is_bounded = None

    def cleared(self):
        last = self.labels[-1] if self.labels else []
        return "herdr.pa.starting=" in last and "herdr.pa.error=" in last

    def test_u23_starting_is_set_before_the_slow_steps_and_cleared_on_any_exit(self):
        seen = {}

        class Crash(Exception):
            pass

        def run_session(*a):
            seen["before"] = [c for c in self._calls if c[:2] == ["hc", "label"]]
            raise Crash()
        self._calls = []
        pa.call = lambda argv, check=True: self._calls.append(list(argv)) or ""
        with self.assertRaises(Crash):
            self.session_with(run_session)
        self.assertTrue(any("herdr.pa.starting=sp1" in c for c in seen["before"]), "L2: starting label set before run_session: {}".format(seen["before"]))
        self.assertTrue(self.cleared(), "R3-M2: labels cleared when pa-session dies: {}".format(self.labels))

    def session_with(self, run_session):
        calls = self._calls
        pa.hc_get = lambda address: {"labels": {"herdr.space": "sp1"}} if address else {}
        saved = (pa.run_session, sys.stdin, os.environ.get("HC_TERMINAL_ID"), builtins.input)

        class TTY(io.StringIO):
            def isatty(self):
                return True
        pa.run_session = run_session
        sys.stdin = TTY("")
        os.environ["HC_TERMINAL_ID"] = "t1"
        args = types.SimpleNamespace(space="sp1", project=self.project, profile="test", name=None, auto=False, ask=False, fresh=False)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                pa.cmd_session(args)
        finally:
            pa.run_session, sys.stdin, builtins.input = saved[0], saved[1], saved[3]
            if saved[2] is None:
                os.environ.pop("HC_TERMINAL_ID", None)
            else:
                os.environ["HC_TERMINAL_ID"] = saved[2]
            self.labels = [c for c in calls if c[:2] == ["hc", "label"]]

    def test_u24_ctrl_c_outside_error_wait_clears_the_labels(self):
        def run_session(*a):
            raise KeyboardInterrupt()  # e.g. while it waits on the launch lock or another launcher's heartbeat
        self._calls = []
        pa.call = lambda argv, check=True: self._calls.append(list(argv)) or ""
        code = None
        try:
            self.session_with(run_session)
        except KeyboardInterrupt:
            self.fail("R3-M2: Ctrl-C outside error-wait escaped cmd_session, leaving labels: {}".format(self.labels))
        except SystemExit as e:
            code = e.code
        self.assertEqual(code, 2)
        self.assertTrue(self.cleared(), "labels cleared: {}".format(self.labels))

    def test_u25_error_wait_clears_starting(self):
        self._calls = []
        pa.call = lambda argv, check=True: self._calls.append(list(argv)) or ""
        saved_input, saved_sig = builtins.input, signal.getsignal(signal.SIGINT)
        builtins.input = lambda *a: ""
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                pa.error_wait("t1", pa.Problem("x"))
        finally:
            builtins.input = saved_input
            signal.signal(signal.SIGINT, saved_sig)
        first = [c for c in self._calls if c[:2] == ["hc", "label"]][0]
        self.assertIn("herdr.pa.starting=", first, "L4: the error label replaces 'starting'")

    def test_u26_ctrl_c_in_error_wait_clears_starting(self):
        def run_session(*a):
            raise pa.Problem("project folder does not exist")

        def interrupt(*a):
            raise KeyboardInterrupt()
        self._calls = []
        pa.call = lambda argv, check=True: self._calls.append(list(argv)) or ""
        saved_input = builtins.input
        builtins.input = interrupt
        saved_sig = signal.getsignal(signal.SIGINT)
        try:
            with self.assertRaises(SystemExit):
                self.session_with(run_session)
        finally:
            builtins.input = saved_input
            signal.signal(signal.SIGINT, saved_sig)
        self.assertTrue(self.cleared(), "L5: {}".format(self.labels))

    def test_u27_a_launcher_started_right_after_a_claim_is_refused(self):
        # N10: claim() writes its first heartbeat at once, so a second launcher that starts before the
        # first beat thread has run sees the first as alive and is refused.
        self.fixtures3(int(time.time()) - 3600)
        self.run_once({"beat": True})  # t1 claims (labels via the stub), its beat thread never runs
        pa.live_terminals = lambda: {"t1": {"state": "live", "labels": {"herdr.pa": "sp1"}}}
        args = types.SimpleNamespace(space="sp1", project=self.project, profile="test", name=None, auto=False, ask=False, fresh=False)
        with self.assertRaises(pa.Problem) as e:
            pa.run_session(args, "t2", {}, "sp1", self.project, {"beat": True})
        self.assertIn("already has a Project Agent", str(e.exception))


class ProfileReload(Base):
    """t-033: each restart in run_agent's loop uses the profile as it is now, not as it was when the launcher began;
    a profile that cannot be read at that moment keeps the one in hand."""

    def setUp(self):
        super().setUp()
        self.prof = os.path.join(pa.ROOT, "profiles", "test")
        os.makedirs(self.prof, exist_ok=True)
        dump({"version": 1, "space": "sp1", "project": self.project, "profile": "test", "name": "proj",
              "terminal": "t1"}, os.path.join(self.dir, "agent.json"))

    def write_profile(self, status_line):
        value = {"name": "test", "claude": ["true"], "codex": ["true"]}
        if status_line:
            value["statusLine"] = {"command": ["true", "statusline"], "refreshInterval": 30}
        dump(value, os.path.join(self.prof, "profile.json"))

    def two_launches(self, between):
        """Two launches of run_agent's loop; between(): what happens to the profile after the first."""
        calls = []

        class Stop(Exception):
            pass

        def fake_call(argv, cwd=None):
            calls.append(list(argv))
            if len(calls) == 1:
                between()
                return 0
            raise Stop()
        saved = (pa.subprocess.call, pa.time.sleep, os.environ.pop("PA_NO_CC_STATUSLINE", None))
        pa.subprocess.call, pa.time.sleep = fake_call, (lambda s: None)
        p = pa.load_profile("test")
        try:
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(Stop):
                pa.run_agent(types.SimpleNamespace(fresh=True), "t1", self.project, p, self.dir,
                             load(os.path.join(self.dir, "agent.json")), {"beat": True, "previous": None}, time.time())
        finally:
            pa.subprocess.call, pa.time.sleep = saved[0], saved[1]
            if saved[2] is not None:
                os.environ["PA_NO_CC_STATUSLINE"] = saved[2]
        self.assertEqual(len(calls), 2)
        return calls

    def ours(self, argv):
        n = sum(1 for a in argv if a == "--settings" or a.startswith("--settings="))
        if not n:
            return None
        self.assertEqual(n, 1)
        self.assertEqual(argv[-2], "--settings")
        return json.loads(argv[-1])["statusLine"]

    def masked(self, argv):
        out = list(argv)
        for flag in ("--session-id", "--resume"):
            if flag in out:
                out[out.index(flag) + 1] = "<session>"
        return out

    def test_r1_key_added_after_the_first_launch_reaches_the_second(self):
        self.write_profile(False)
        first, second = self.two_launches(lambda: self.write_profile(True))
        self.assertIsNone(self.ours(first))
        sl = self.ours(second)
        self.assertIsNotNone(sl, "the restart did not pick up the profile's new statusLine: {}".format(second))
        self.assertIn("statusline --space sp1 --then", sl["command"])

    def test_r2_malformed_profile_keeps_the_one_in_hand(self):
        self.write_profile(True)

        def mangle():
            with open(os.path.join(self.prof, "profile.json"), "w") as f:
                f.write("{not json")
        first, second = self.two_launches(mangle)
        self.assertIsNotNone(self.ours(first))
        self.assertEqual(self.masked(first), self.masked(second))

    def test_r3_unchanged_profile_gives_the_same_command_line(self):
        self.write_profile(True)
        first, second = self.two_launches(lambda: None)
        self.assertIsNotNone(self.ours(first))
        self.assertEqual(self.masked(first), self.masked(second))

    def good_profile(self):
        return {"name": "test", "claude": ["true"], "codex": ["true"], "models": {"orchestrator": "model-a"},
                "statusLine": {"command": ["true", "statusline"], "refreshInterval": 30}}

    def fresh_agent(self):
        dump({"version": 1, "space": "sp1", "project": self.project, "profile": "test", "name": "proj",
              "terminal": "t1"}, os.path.join(self.dir, "agent.json"))

    def test_r2_wrong_shape_in_a_key_the_loop_reads_keeps_the_one_in_hand(self):
        # Round 2 (check M1): the file loads as JSON, but a key the loop reads has a shape the launch cannot use.
        def without_claude(v):
            del v["claude"]
        cases = {"models a list": lambda v: v.update(models=["m"]),
                 "path a number": lambda v: v.update(path=5),
                 "claude a string": lambda v: v.update(claude="true"),
                 "claude missing": without_claude}
        for name, change in cases.items():
            with self.subTest(name):
                self.fresh_agent()
                dump(self.good_profile(), os.path.join(self.prof, "profile.json"))

                def between():
                    v = self.good_profile()
                    change(v)
                    dump(v, os.path.join(self.prof, "profile.json"))
                first, second = self.two_launches(between)
                self.assertIn("model-a", first)
                self.assertIsNotNone(self.ours(first))
                self.assertEqual(self.masked(first), self.masked(second))

    def test_r2_value_the_exec_refuses_keeps_the_one_in_hand(self):
        # Round 3 (check round 2 M1): the command line builds, but an element is not a str or holds a NUL byte, so
        # the exec would raise TypeError / ValueError. Each shape on the restart and on the resume path.
        def set_model(value):
            return lambda v: v["models"].update(orchestrator=value)
        cases = {"models.orchestrator a number": set_model(5),
                 "models.orchestrator a list": set_model(["m"]),
                 "models.orchestrator an object": set_model({"a": 1}),
                 "models.orchestrator a bool": set_model(True),
                 "models.orchestrator with NUL": set_model("a\0b"),
                 "claude argument with NUL": lambda v: v.update(claude=["true", "a\0b"]),
                 "changeRequest.name with NUL": lambda v: v.update(changeRequest={"name": "a\0"}),
                 "ci with NUL": lambda v: v.update(ci=["a\0"])}
        def launches(path, change):
            self.fresh_agent()
            dump(self.good_profile(), os.path.join(self.prof, "profile.json"))

            def between():
                v = self.good_profile()
                change(v)
                dump(v, os.path.join(self.prof, "profile.json"))
                if path == "resume":
                    agent = load(os.path.join(self.dir, "agent.json"))
                    agent["sessionStarted"] = True
                    dump(agent, os.path.join(self.dir, "agent.json"))
            return self.two_launches(between)
        for path in ("restart", "resume"):
            # The first profile's command on this path: the second launch when the profile is left unchanged.
            expected = self.masked(launches(path, lambda v: None)[1])
            self.assertEqual("--resume" in expected, path == "resume")
            for name, change in cases.items():
                with self.subTest(name, path=path):
                    first, second = launches(path, change)
                    self.assertIn("model-a", first)
                    bad = [a for a in second if not isinstance(a, str) or "\0" in a]
                    self.assertEqual(bad, [], "the exec would refuse the second launch")
                    self.assertEqual(self.masked(second), expected)

    def test_r1_model_change_between_launches_is_picked_up(self):
        dump(self.good_profile(), os.path.join(self.prof, "profile.json"))

        def between():
            v = self.good_profile()
            v["models"]["orchestrator"] = "model-b"
            dump(v, os.path.join(self.prof, "profile.json"))
        first, second = self.two_launches(between)
        self.assertEqual(first[first.index("--model") + 1], "model-a")
        self.assertEqual(second[second.index("--model") + 1], "model-b")
        expected = [a.replace("model-a", "model-b") for a in self.masked(first)]  # the steering names the model too
        self.assertEqual(self.masked(second), expected)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)




# t-003 R9: pa-pass, pa-task's notice and pa-worker delete's notice go only to a live launcher.
hooks_spec = importlib.util.spec_from_file_location("pa_hooks", os.path.join(HERE, "..", "runtime", "tools", "pa_hooks.py"))
sys.modules["pa"] = pa  # pa_hooks imports pa: the copy loaded above, with the temp homes
pa_hooks = importlib.util.module_from_spec(hooks_spec)
hooks_spec.loader.exec_module(pa_hooks)


class LiveLauncherOnly(Base):
    def setUp(self):
        super().setUp()
        self.saved_hooks = {k: getattr(pa_hooks, k) for k in ("load_context", "worker_facts", "by_an_agent")}
        dump({"space": "sp1", "name": "proj", "project": self.project, "profile": "test", "terminal": "t1"}, os.path.join(self.dir, "agent.json"))
        dump({"tasks": [{"id": "t-1", "state": "queued", "workers": [{"address": "t5", "role": "build"}]}]}, os.path.join(self.dir, "ledger.json"))
        self.calls = []
        pa.hc_get = lambda address: {"state": "live", "status": "idle", "labels": {"herdr.pa": "sp1"}} if address else {}

        def call(argv, check=True):
            self.calls.append(list(argv))
            return ""
        pa.call = call

    def tearDown(self):
        for k, v in self.saved_hooks.items():
            setattr(pa_hooks, k, v)
        super().tearDown()

    def sends(self):
        return [c for c in self.calls if c[:2] == ["hc", "send"]]

    def run_quiet(self, fn, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fn(*args)
        return out.getvalue()

    def test_u_pass_stale_heartbeat_is_not_running(self):
        args = types.SimpleNamespace(space="sp1", pause=False, resume=False, retro=False)
        self.heartbeat(time.time() - 3600, "t1")
        with self.assertRaises(pa.Problem) as e:
            self.run_quiet(pa.cmd_pass, args)
        self.assertIn("not running", str(e.exception))
        self.assertEqual(self.sends(), [])
        self.heartbeat(time.time() - 2, "t1")  # control: a live launcher gets the pass
        self.run_quiet(pa.cmd_pass, args)
        self.assertEqual(self.sends(), [["hc", "send", "t1", "manual pass"]])

    # t-015: hc get gives status as an object {app, state, msg}; hc ls as a word. pa-pass reads both.
    def pass_with_status(self, status):
        pa.hc_get = lambda address: {"state": "live", "status": status, "labels": {"herdr.pa": "sp1"}} if address else {}
        request = os.path.join(self.dir, "pass-request.json")
        if os.path.exists(request):
            os.remove(request)
        self.calls[:] = []
        self.heartbeat(time.time() - 2, "t1")
        self.run_quiet(pa.cmd_pass, types.SimpleNamespace(space="sp1", pause=False, resume=False, retro=False))
        return self.sends(), (load(request) if os.path.exists(request) else None)

    def test_u_pass_busy_agent_is_queued_in_both_shapes(self):
        for status in ({"app": "claude-code", "state": "working", "msg": "x"}, {"app": "claude-code", "state": "blocked"},
                       "working", "blocked"):
            sends, request = self.pass_with_status(status)
            self.assertEqual(sends, [], "status {!r}: the pass was sent to a busy Project Agent".format(status))
            self.assertTrue(request and request.get("pending"), "status {!r}: no pass was queued".format(status))

    def test_u_pass_free_agent_is_sent_in_both_shapes(self):
        for status in ({"app": "claude-code", "state": "idle"}, {"app": "claude-code", "state": "done"}, "done", "idle"):
            sends, request = self.pass_with_status(status)
            self.assertEqual(sends, [["hc", "send", "t1", "manual pass"]], "status {!r}".format(status))
            self.assertIsNone(request, "status {!r}".format(status))

    def test_u_status_word_is_shared(self):
        self.assertEqual(pa.status_word({"status": {"app": "claude-code", "state": "working"}}), "working")
        self.assertEqual(pa.status_word({"status": "blocked"}), "blocked")
        self.assertEqual(pa.status_word({"status": {"app": "x"}}), "")
        self.assertEqual(pa.status_word({}), "")
        self.assertEqual(pa.status_word(None), "")
        self.assertIs(pa_hooks.status_word, pa.status_word)

    def test_u_task_stale_heartbeat_skips_the_notice(self):
        self.heartbeat(time.time() - 3600, "t1")
        out = self.run_quiet(pa.cmd_task, types.SimpleNamespace(action="done", task="t-1", space="sp1"))
        self.assertEqual(self.sends(), [])
        self.assertIn("not running", out)
        self.assertEqual(load(os.path.join(self.dir, "ledger.json"))["tasks"][0]["state"], "done")
        self.heartbeat(time.time() - 2, "t1")  # control
        self.run_quiet(pa.cmd_task, types.SimpleNamespace(action="park", task="t-1", space="sp1"))
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.sends()[0][2], "t1")

    def test_u_worker_delete_stale_heartbeat_skips_the_notice(self):
        agent = load(os.path.join(self.dir, "agent.json"))
        pa_hooks.load_context = lambda space: (self.dir, agent, {})
        pa_hooks.worker_facts = lambda directory, agent, profile, address: ({"address": "t5", "name": "w", "state": "gone", "task": "t-1", "worktree": None}, None)
        pa_hooks.by_an_agent = lambda: False
        args = types.SimpleNamespace(space="sp1", address="t5", action="delete", keep_worktree=False, discard=False, with_task=False)
        self.heartbeat(time.time() - 3600, "t1")
        self.run_quiet(pa_hooks.cmd_worker, args)
        self.assertEqual(self.sends(), [])
        self.heartbeat(time.time() - 2, "t1")  # control
        self.run_quiet(pa_hooks.cmd_worker, args)
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.sends()[0][2], "t1")


class TaskDeleteWorktrees(LiveLauncherOnly):  # t-020 J1, AC7.4: the delete note names every worktree left in place
    def delete(self, task):
        dump({"tasks": [task]}, os.path.join(self.dir, "ledger.json"))
        shutil.rmtree(os.path.join(self.dir, "trash"), ignore_errors=True)  # one trash folder per second
        pa.live_terminals = lambda: {}
        self.heartbeat(time.time() - 3600, "t1")
        saved = {k: os.environ.pop(k) for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_SANDBOX") if k in os.environ}
        try:
            return self.run_quiet(pa.cmd_task, types.SimpleNamespace(action="delete", task="t-1", space="sp1"))
        finally:
            os.environ.update(saved)

    def test_task_delete_names_every_worktree(self):
        out = self.delete({"id": "t-1", "state": "queued", "workers": [], "worktree": "/x/worktrees/t-1-tool/repo",
                           "worktrees": [{"path": "/x/worktrees/t-1-tool/repo", "job": "tool"},
                                         {"path": "/x/ws/worktrees/t-1-billing-script", "job": "billing-script"},
                                         {"path": "/x/worktrees/t-1-old/repo", "job": "old", "removed": "2026-11-17T00:00:00Z"}],
                           "extraWorktrees": ["/x/worktrees/t-1-timing/repo"]})
        self.assertIn("t-1: queued -> deleted. Its worktrees were left in place: /x/worktrees/t-1-tool/repo (tool), "
                      "/x/ws/worktrees/t-1-billing-script (billing-script), /x/worktrees/t-1-timing/repo (timing).", out)
        self.assertNotIn("t-1-old", out)

    def test_task_delete_one_worktree_keeps_the_old_sentence(self):
        out = self.delete({"id": "t-1", "state": "queued", "workers": [], "worktree": "/x/a"})
        self.assertIn("t-1: queued -> deleted. Its worktree /x/a was left in place.", out)
        out = self.delete({"id": "t-1", "state": "queued", "workers": [], "worktree": None, "worktrees": [{"path": "/x/b", "job": "b"}]})
        self.assertIn("t-1: queued -> deleted. Its worktree /x/b was left in place.", out)
        out = self.delete({"id": "t-1", "state": "queued", "workers": []})
        self.assertIn("t-1: queued -> deleted.\n", out)


if __name__ == "__main__":
    unittest.main()
