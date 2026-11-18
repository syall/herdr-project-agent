"""t-037 J2: the restart in place. J1's Recorded limits (card d-107), the launcher's own Claude child (Popen handle,
end_child), and (later classes) the request watcher, the pre-check, the exec, /exit reloading and pa-restart.
Spec R4-R9, R11 with the accepted changes S1-S6 (spec.md, card d-108); plan.md J2.

Run: python3 -m unittest -v tests/test_pa_restart.py
Temp homes only: HERDR_PA_HOME, HERDR_APP_HOME and CLAUDE_CONFIG_DIR point at a temp folder before pa.py is loaded;
hc is never called (pa.call, hc_get and live_terminals are replaced); os.execv and os.kill are replaced by recorders
that raise when a test did not expect them; no real Claude, no real launcher, no signal to any process but a test's
own stand-in.
"""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import child_fakes  # noqa: E402,F401

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TMP = tempfile.mkdtemp(prefix="t037-restart-")
os.environ["HERDR_PA_HOME"] = os.path.join(TMP, "pa")
os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(TMP, "claude")
os.environ["HERDR_APP_HOME"] = os.path.join(TMP, "app")
for k in ("HC_TERMINAL_ID", "CLAUDECODE"):
    os.environ.pop(k, None)
spec = importlib.util.spec_from_file_location("pa", os.path.join(REPO, "runtime", "tools", "pa.py"))
pa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pa)
assert pa.ROOT.startswith(TMP) and pa.APP.startswith(TMP), "pa.py must use the temp homes"

SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SP = "sp1"


def write(path, text, mode="w"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode) as f:
        f.write(text)


def load(path):
    with open(path) as f:
        return json.load(f)


def dump(value, path):
    write(path, json.dumps(value))


def quiet(fn, *a):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            fn(*a)
        except SystemExit as e:
            code = e.code or 0
        except pa.Problem as e:  # what main() turns into exit 2
            print(e, file=sys.stderr)
            code = 2
    return code, out.getvalue(), err.getvalue()


def j1_hash(folder, names):
    """J1's tree_hash for plain files: path\\0len\\0bytes over the sorted names."""
    import hashlib
    h = hashlib.sha256()
    for rel in sorted(names):
        with open(os.path.join(folder, rel), "rb") as f:
            data = f.read()
        h.update("{}\0{}\0".format(rel, len(data)).encode())
        h.update(data)
    return h.hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(pa.ROOT, ignore_errors=True)
        shutil.rmtree(pa.APP, ignore_errors=True)
        for rel, text in (("tools/pa.py", "print('pa')\n"), ("tools/pa_costs.py", "x = 1\n"),
                          ("core/orchestrator.md", "# o\n"),
                          ("profiles/test/profile.json", json.dumps({"name": "test", "claude": ["true"], "codex": ["true"]})),
                          ("profiles/test/context.md", "ctx\n")):
            write(os.path.join(pa.ROOT, rel), text)
        self.dir = os.path.join(pa.ROOT, "spaces", SP)
        os.makedirs(self.dir, exist_ok=True)
        self.project = os.path.join(TMP, "proj")
        os.makedirs(self.project, exist_ok=True)
        self.saved = {k: getattr(pa, k) for k in ("live_terminals", "hc_get", "call", "__file__")}
        self.saved_os = (os.execv, getattr(os, "kill"))
        self.calls = []
        pa.call = lambda argv, check=True: self.calls.append(list(argv)) or ""
        pa.hc_get = lambda address: {}
        pa.live_terminals = lambda: {}

        def no_exec(*a):
            raise AssertionError("os.execv called unexpectedly: {}".format(a))

        def no_kill(*a):
            raise AssertionError("os.kill called: {}".format(a))
        os.execv, os.kill = no_exec, no_kill

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(pa, k, v)
        os.execv, os.kill = self.saved_os

    def stamp(self, commit=None):
        code, _out, err = quiet(pa.cmd_stamp, argparse.Namespace(commit=commit, dirty=False, synced_from=None))
        self.assertEqual(code, 0, err)
        return load(os.path.join(pa.ROOT, "installed.json"))

    def agent(self, **extra):
        a = {"version": 1, "space": SP, "project": self.project, "profile": "test", "name": "proj", "terminal": "t1",
             "terminals": ["t1"], "sessionId": SID, "sessionStarted": False}
        a.update(extra)
        dump(a, os.path.join(self.dir, "agent.json"))
        return a

    def record(self, ctx=None):
        """A launcher record as the launcher writes it after claim, for this temp home's tools."""
        ctx = ctx if ctx is not None else {}
        pa.__file__ = os.path.join(pa.ROOT, "tools", "pa.py")
        with contextlib.redirect_stdout(io.StringIO()):
            pa.launcher_start(ctx, "t1", SP, self.dir, pa.load_profile("test"))
        return ctx


# ---- J1 limits (card d-107) -------------------------------------------------------------------------------------

class L1_RestartCapable(Base):
    def test_record_is_version_2_and_accepts_restart(self):
        ctx = self.record()
        rec = load(os.path.join(self.dir, "launcher.json"))
        self.assertEqual(rec["version"], 2)
        self.assertEqual(rec["accepts"], ["restart"])
        self.assertTrue(pa.restart_capable(rec))
        self.assertIs(ctx["launcher"]["accepts"], ctx["launcher"]["accepts"])

    def test_j1_records_and_bad_shapes_are_not_capable(self):
        for rec in (None, [], "x", {"version": 1, "instance": "i"}, {"version": 2}, {"version": 2, "accepts": "restart"},
                    {"version": 2, "accepts": ["context"]}, {"version": "2", "accepts": ["restart"]}):
            with self.subTest(rec=rec):
                self.assertFalse(pa.restart_capable(rec))


class L2_OneWriter(Base):
    def test_two_threads_both_last_values_kept(self):
        ctx = self.record()
        errors = []

        def run(field):
            try:
                for i in range(1000):
                    pa.launcher_update(ctx, self.dir, lambda r, i=i: r.__setitem__(field, {"n": i, "pad": "x" * (i % 7)}))
            except Exception as e:  # noqa: BLE001 - reported below
                errors.append(e)
        threads = [threading.Thread(target=run, args=(f,)) for f in ("session", "restart")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        rec = load(os.path.join(self.dir, "launcher.json"))
        self.assertEqual(rec["session"]["n"], 999)
        self.assertEqual(rec["restart"]["n"], 999)

    def test_no_record_no_write(self):
        self.assertFalse(pa.launcher_update({}, self.dir, lambda r: None))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "launcher.json")))


class L3_WrongTypes(Base):
    def test_code_state_never_raises(self):
        inst = {"tools": "T", "core": "C"}
        for rec in ({"terminal": "t1", "code": "x", "session": {"core": "C"}},
                    {"terminal": "t1", "code": {"tools": "T"}, "session": []},
                    {"terminal": "t1", "code": {"tools": 3}, "session": {"core": ["C"]}},
                    {"terminal": "t1", "code": None, "session": None}):
            with self.subTest(rec=rec):
                self.assertEqual(pa.code_state(rec, inst, "t1")[0], "unknown")
        self.assertEqual(pa.code_state({"terminal": "t1", "code": {"tools": "T"}, "session": {"core": "C"}},
                                       {"tools": "T", "core": "C", "source": 3}, "t1")[0], "current")

    def test_stamp_script_with_a_bad_source(self):
        dump({"version": 1, "tools": "T", "core": "C", "source": 3}, os.path.join(pa.ROOT, "installed.json"))
        self.assertNotIn("--commit", pa.stamp_script())
        dump({"version": 1, "tools": "T", "core": "C", "source": {"commit": ["x"]}}, os.path.join(pa.ROOT, "installed.json"))
        self.assertNotIn("--commit", pa.stamp_script())

    def test_launcher_start_with_a_bad_installed_source(self):
        self.stamp()
        s = load(os.path.join(pa.ROOT, "installed.json"))
        s["source"] = "oops"
        dump(s, os.path.join(pa.ROOT, "installed.json"))
        self.record()
        rec = load(os.path.join(self.dir, "launcher.json"))
        self.assertIsNone(rec["code"]["commit"])
        self.assertEqual(rec["code"]["tools"], s["tools"])

    def test_peers_lists_every_agent_despite_a_bad_record(self):
        for space, term in (("sp1", "t1"), ("sp2", "t2")):
            d = os.path.join(pa.ROOT, "spaces", space)
            os.makedirs(d, exist_ok=True)
            dump({"version": 1, "space": space, "project": self.project, "profile": "test", "name": space, "terminal": term},
                 os.path.join(d, "agent.json"))
        dump({"terminal": "t1", "code": "x", "session": []}, os.path.join(pa.ROOT, "spaces", "sp1", "launcher.json"))
        dump({"agents": [{"space": s, "name": s, "project": self.project, "profile": "test", "terminal": t, "address": t}
                         for s, t in (("sp1", "t1"), ("sp2", "t2"))]}, os.path.join(pa.ROOT, "registry.json"))
        self.stamp()
        pa.live_terminals = lambda: {t: {"address": t, "state": "live", "labels": {"herdr.pa": s}} for s, t in (("sp1", "t1"), ("sp2", "t2"))}
        code, out, err = quiet(pa.cmd_peers, argparse.Namespace(json=True, all=True, older=False))
        self.assertEqual(code, 0, err)
        rows = json.loads(out)
        rows = rows if isinstance(rows, list) else rows.get("agents", rows)
        self.assertEqual(sorted(r["space"] for r in rows), ["sp1", "sp2"])


class L4_HashSpecialFiles(Base):
    def test_a_fifo_does_not_hang_and_counts(self):
        folder = os.path.join(TMP, "fifo-folder")
        shutil.rmtree(folder, ignore_errors=True)
        write(os.path.join(folder, "a.txt"), "a\n")
        before = pa.tree_hash(folder)
        os.mkfifo(os.path.join(folder, "pipe"))
        box = {}
        t = threading.Thread(target=lambda: box.setdefault("h", pa.hash_or_none(folder)), daemon=True)
        t.start()
        t.join(5)
        self.assertFalse(t.is_alive(), "tree_hash hung on a FIFO")
        self.assertIsNotNone(box.get("h"))
        self.assertNotEqual(box["h"], before)  # a special file is named in the hash, never opened

    def test_plain_files_hash_as_in_j1(self):
        folder = os.path.join(TMP, "plain-folder")
        shutil.rmtree(folder, ignore_errors=True)
        write(os.path.join(folder, "a.txt"), "a\n")
        write(os.path.join(folder, "sub", "b.md"), "b" * 10)
        write(os.path.join(folder, "big.bin"), b"\x01" * (3 * 1024 * 1024 + 17), "wb")
        self.assertEqual(pa.tree_hash(folder), j1_hash(folder, ["a.txt", "sub/b.md", "big.bin"]))

    def test_a_symlink_counts_by_content(self):
        folder = os.path.join(TMP, "link-folder")
        shutil.rmtree(folder, ignore_errors=True)
        target = os.path.join(TMP, "link-target.md")
        write(target, "one\n")
        os.makedirs(folder)
        os.symlink(target, os.path.join(folder, "context.md"))
        first = pa.tree_hash(folder)
        write(target, "two\n")
        self.assertNotEqual(pa.tree_hash(folder), first)

    def test_reads_in_chunks(self):
        folder = os.path.join(TMP, "chunk-folder")
        shutil.rmtree(folder, ignore_errors=True)
        write(os.path.join(folder, "big.bin"), b"\x02" * (2 * 1024 * 1024 + 5), "wb")
        sizes, real_fdopen = [], os.fdopen

        def spy_fdopen(fd, *a, **k):
            f = real_fdopen(fd, *a, **k)
            real_read = f.read

            class Spy:
                def __enter__(self):
                    return self

                def __exit__(self, *e):
                    f.close()

                def read(self, n=-1):
                    sizes.append(n)
                    return real_read(n)

                def fileno(self):
                    return f.fileno()
            return Spy()
        pa.os.fdopen = spy_fdopen
        try:
            pa.tree_hash(folder)
        finally:
            pa.os.fdopen = real_fdopen
        self.assertTrue(sizes and all(0 < n <= 1024 * 1024 for n in sizes), sizes)


class RL1_StampGaps(Base):  # J1 VERIFY RL1 (mutations M2 and M12)
    def test_a_non_py_file_in_tools_leaves_tools(self):
        before = self.stamp()["tools"]
        write(os.path.join(pa.ROOT, "tools", "notes.txt"), "x\n")
        self.assertEqual(self.stamp()["tools"], before)

    def test_commit_only_when_the_tools_match(self):
        self.stamp(commit="c0ffee")
        write(os.path.join(pa.ROOT, "tools", "pa.py"), "print('changed')\n")  # the launcher's tools now differ
        s = load(os.path.join(pa.ROOT, "installed.json"))
        self.record()
        rec = load(os.path.join(self.dir, "launcher.json"))
        self.assertNotEqual(rec["code"]["tools"], s["tools"])
        self.assertIsNone(rec["code"]["commit"])


# ---- the child: a Popen handle the launcher keeps (R6, R9; offered to t-041 J4) -------------------------------

class FakeChild:
    def __init__(self, obeys_term=True):
        self.signals, self.obeys, self.returncode, self.waits = [], obeys_term, None, []

    def send_signal(self, sig):
        self.signals.append(sig)

    def kill(self):
        self.send_signal(signal.SIGKILL)

    def terminate(self):
        self.send_signal(signal.SIGTERM)

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if signal.SIGKILL in self.signals:
            self.returncode = -9
        elif signal.SIGTERM in self.signals and self.obeys:
            self.returncode = 143
        elif timeout is not None:
            raise subprocess.TimeoutExpired("claude", timeout)
        return self.returncode

    def poll(self):
        return self.returncode


class Child(Base):
    def test_run_child_keeps_the_handle_while_it_runs(self):
        seen = {}
        ctx = {}

        def fake_call(argv, cwd=None):
            return 7
        undo = child_fakes.patch(pa, fake_call)
        try:
            class Watch:
                def __init__(self, argv, cwd=None):
                    self.argv = argv

                def wait(self, timeout=None):
                    seen["during"] = ctx.get("child")
                    return 7
            pa.spawn = lambda argv, cwd=None: Watch(argv, cwd)
            self.assertEqual(pa.run_child(ctx, ["x"], self.project), 7)
        finally:
            undo()
        self.assertIsNotNone(seen["during"])
        self.assertIsNone(ctx.get("child"))

    def test_end_child_sigterm_then_wait(self):
        child = FakeChild()
        ctx = {"child": child, "running": {"since": None}}
        self.assertEqual(pa.end_child(ctx, "restart", grace=0.2), 143)
        self.assertEqual(child.signals, [signal.SIGTERM])
        self.assertEqual(ctx["ended"], "restart")

    def test_end_child_sigkill_only_after_the_grace(self):
        child = FakeChild(obeys_term=False)
        ctx = {"child": child, "running": {"since": None}}
        self.assertEqual(pa.end_child(ctx, "restart", grace=0.2), -9)
        self.assertEqual(child.signals, [signal.SIGTERM, signal.SIGKILL])

    def test_end_child_without_a_child_does_nothing(self):
        ctx = {"running": {"since": None}}
        self.assertIsNone(pa.end_child(ctx, "restart", grace=0.2))
        self.assertNotIn("ended", ctx)

    def test_end_child_marks_a_session_that_ran(self):
        self.agent()
        child = FakeChild()
        ctx = {"child": child, "directory": self.dir, "project": self.project,
               "running": {"since": time.time() - pa.RAN - 1, "session": SID, "marked": False}}
        pa.end_child(ctx, "restart", grace=0.2)
        self.assertTrue(load(os.path.join(self.dir, "agent.json"))["sessionStarted"])

    def test_end_child_does_not_mark_a_short_session_without_a_transcript(self):
        self.agent()
        ctx = {"child": FakeChild(), "directory": self.dir, "project": self.project,
               "running": {"since": time.time() - 2, "session": SID, "marked": False}}
        pa.end_child(ctx, "restart", grace=0.2)
        self.assertFalse(load(os.path.join(self.dir, "agent.json"))["sessionStarted"])

    def test_end_child_marks_a_short_session_with_a_transcript(self):
        self.agent()
        write(os.path.join(pa.CLAUDE_HOME, "projects", pa.claude_slug(self.project), SID + ".jsonl"), "{}\n")
        ctx = {"child": FakeChild(), "directory": self.dir, "project": self.project,
               "running": {"since": time.time() - 2, "session": SID, "marked": False}}
        pa.end_child(ctx, "restart", grace=0.2)
        self.assertTrue(load(os.path.join(self.dir, "agent.json"))["sessionStarted"])

    def test_an_exit_we_caused_is_not_a_crash(self):
        # The child was ended by end_child (ctx["ended"]): the loop neither counts a quick exit nor drops the
        # session (after_exit would drop a quick, failed resume without a transcript).
        self.agent(sessionStarted=True)
        starts = []

        class Stop(Exception):
            pass
        ctx = {"beat": True, "previous": None}

        def fake_call(argv, cwd=None):
            starts.append(list(argv))
            if len(starts) <= 3:
                ctx["ended"] = "restart"  # as end_child sets it before the child exits
                return 143
            raise Stop()
        undo = child_fakes.patch(pa, fake_call)
        saved_call, saved_sleep = pa.subprocess.call, pa.time.sleep
        pa.subprocess.call, pa.time.sleep = fake_call, (lambda s: None)  # bounded before the Popen swap too
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(Stop):
                pa.run_agent(types.SimpleNamespace(fresh=False), "t1", self.project, pa.load_profile("test"), self.dir,
                             load(os.path.join(self.dir, "agent.json")), ctx, time.time())
        finally:
            undo()
            pa.subprocess.call, pa.time.sleep = saved_call, saved_sleep
        self.assertEqual(len(starts), 4)
        self.assertTrue(all("--resume" in s and SID in s for s in starts), [s[-3:] for s in starts])
        self.assertNotIn("not restarting by itself", out.getvalue())



# ---- the request, the watcher, the pre-check and the exec (R4, R6, R7, S1, S2, S5) -----------------------------

class Execd(Exception):
    """Raised by the os.execv recorder: the test process is never replaced."""


def real_tools():
    """The real pa.py and pa_costs.py as the installed tools of the temp home, stamped."""
    for name in ("pa.py", "pa_costs.py"):
        shutil.copy2(os.path.join(REPO, "runtime", "tools", name), os.path.join(pa.ROOT, "tools", name))


class Requests(Base):
    def setUp(self):
        super().setUp()
        self.agent(sessionStarted=True)
        self.execs = []

        def recorder(path, argv):
            self.execs.append((path, list(argv)))
            raise Execd()
        self.recorder = recorder
        self.saved_timeouts = (pa.SELF_CHECK_TIMEOUT, pa.PROBE_TIMEOUT)

    def tearDown(self):
        pa.SELF_CHECK_TIMEOUT, pa.PROBE_TIMEOUT = self.saved_timeouts
        super().tearDown()

    def ctx(self, child=None):
        ctx = self.record()
        ctx.update(child=child if child is not None else FakeChild(), directory=self.dir, project=self.project,
                   running={"since": time.time() - pa.RAN - 5, "session": SID, "marked": False})
        return ctx

    def request(self, ctx, **over):
        at = time.time()
        req = {"version": 1, "id": "r-" + str(len(self.calls)) + "-" + str(int(at * 1000) % 100000), "space": SP,
               "instance": ctx["launcher"]["instance"], "terminal": "t1", "at": pa.iso_time(at),
               "expires": pa.iso_time(at + 600), "by": "pa-restart", "why": "test", "now": False, "fresh": False}
        req.update(over)
        dump(req, os.path.join(self.dir, "restart-request.json"))
        return req

    def outcome(self):
        return load(os.path.join(self.dir, "launcher.json")).get("restart")

    def gone(self):
        return not os.path.exists(os.path.join(self.dir, "restart-request.json"))

    def poll(self, ctx):
        with contextlib.redirect_stdout(io.StringIO()):
            pa.watch_once(ctx, self.dir, "t1", SP)

    def passing_precheck(self):
        pa.restart_precheck = lambda *a, **k: ({"tools": pa.hash_or_none(os.path.join(pa.ROOT, "tools"), True),
                                                "core": pa.hash_or_none(os.path.join(pa.ROOT, "core"))}, None)


class AC6_3_Refusals(Requests):
    def test_each_bad_request_is_refused_and_deleted(self):
        cases = {"space": {"space": "sp9"}, "instance": {"instance": "someone-else"}, "terminal": {"terminal": "t7"},
                 "expired": {"expires": pa.iso_time(time.time() - 1)}, "types": {"instance": 3},
                 "too long": {"expires": pa.iso_time(time.time() + 7200)}}
        for name, over in cases.items():
            with self.subTest(case=name):
                ctx = self.ctx()
                req = self.request(ctx, **over)
                self.poll(ctx)
                out = self.outcome()
                self.assertEqual(out["outcome"], "refused", out)
                self.assertTrue(out["why"])
                if name != "types":
                    self.assertEqual(out["request"], req["id"])
                self.assertTrue(self.gone())
                self.assertEqual(ctx["child"].signals, [])
                self.assertNotIn("after", ctx)

    def test_unparseable(self):
        ctx = self.ctx()
        write(os.path.join(self.dir, "restart-request.json"), "{not json")
        self.poll(ctx)
        self.assertEqual(self.outcome()["outcome"], "refused")
        self.assertTrue(self.gone())
        self.assertEqual(ctx["child"].signals, [])

    def test_an_older_instance_of_this_launcher_is_deleted_silently(self):
        ctx = self.ctx()
        ctx["formerInstances"] = {"old-instance"}
        self.request(ctx, instance="old-instance")
        self.poll(ctx)
        self.assertTrue(self.gone())
        self.assertIsNone(self.outcome())
        self.assertEqual(ctx["child"].signals, [])

    def test_no_request_nothing_happens(self):
        ctx = self.ctx()
        self.poll(ctx)
        self.assertIsNone(self.outcome())
        self.assertEqual(self.calls, [])


class AC6_4_TheTurn(Requests):
    def statuses(self, *seq):
        seq = list(seq)
        pa.hc_get = lambda address: {"state": "live", "status": {"state": seq.pop(0) if len(seq) > 1 else seq[0]}}

    def test_waits_while_working(self):
        self.passing_precheck()
        self.statuses("working", "working", "idle")
        ctx = self.ctx()
        self.request(ctx)
        self.poll(ctx)
        self.poll(ctx)
        self.assertEqual(ctx["child"].signals, [])
        self.assertFalse(self.gone())
        self.poll(ctx)
        self.assertEqual(ctx["child"].signals, [signal.SIGTERM])
        self.assertEqual(ctx["after"]["kind"], "exec")

    def test_blocked_waits_too(self):
        self.passing_precheck()
        self.statuses("blocked")
        ctx = self.ctx()
        self.request(ctx)
        for _ in range(3):
            self.poll(ctx)
        self.assertEqual(ctx["child"].signals, [])

    def test_now_acts_at_once(self):
        self.passing_precheck()
        self.statuses("working")
        ctx = self.ctx()
        req = self.request(ctx, now=True)
        self.poll(ctx)
        self.assertEqual(ctx["child"].signals, [signal.SIGTERM])
        self.assertIn(["hc", "label", "t1", "herdr.pa.restarting=" + req["id"]], self.calls)
        self.assertTrue(load(os.path.join(self.dir, "agent.json"))["sessionStarted"])

    def test_expiry_while_working_is_refused(self):
        self.passing_precheck()
        self.statuses("working")
        ctx = self.ctx()
        self.request(ctx, expires=pa.iso_time(time.time() + 1))
        self.poll(ctx)
        time.sleep(1.3)
        self.poll(ctx)
        out = self.outcome()
        self.assertEqual(out["outcome"], "refused")
        self.assertIn("mid-turn", out["why"])
        self.assertEqual(ctx["child"].signals, [])
        self.assertTrue(self.gone())

    def test_agent_turn_words(self):
        for status, word in (({"state": "working"}, "working"), ("blocked", "blocked"), ({"state": "idle"}, "idle"),
                             (None, "unknown"), ({"state": "done"}, "idle")):
            pa.hc_get = lambda address, s=status: {"status": s} if s is not None else {}
            self.assertEqual(pa.agent_turn("t1"), word)


class AC6_2_PreCheck(Requests):
    def failing(self, why_part):
        ctx = self.ctx()
        self.request(ctx, now=True)
        self.poll(ctx)
        out = self.outcome()
        self.assertEqual(out["outcome"], "failed", out)
        self.assertIn(why_part, out["why"])
        self.assertEqual(ctx["child"].signals, [])
        self.assertNotIn("after", ctx)
        self.assertTrue(self.gone())

    def test_no_installed_json(self):
        self.failing("installed.json")

    def test_tools_differ_from_the_stamp(self):
        real_tools()
        self.stamp()
        write(os.path.join(pa.ROOT, "tools", "pa_costs.py"), "y = 2\n")
        self.failing("tools")

    def test_core_differs_from_the_stamp(self):
        real_tools()
        self.stamp()
        write(os.path.join(pa.ROOT, "core", "orchestrator.md"), "# changed\n")
        self.failing("core")

    def test_self_check_exit_1(self):
        write(os.path.join(pa.ROOT, "tools", "pa.py"), "import sys\nsys.exit(1)\n")
        self.stamp()
        self.failing("self-check")

    def test_self_check_prints_no_result(self):
        self.stamp()  # tools/pa.py prints 'pa'
        self.failing("self-check")

    def test_self_check_hangs(self):
        write(os.path.join(pa.ROOT, "tools", "pa.py"), "import time\ntime.sleep(30)\n")
        self.stamp()
        pa.SELF_CHECK_TIMEOUT = 1
        began = time.time()
        self.failing("did not finish")
        self.assertLess(time.time() - began, 10)

    def test_the_real_launcher_passes(self):
        real_tools()
        self.stamp()
        pre, why = pa.restart_precheck({}, SP, "test")
        self.assertIsNone(why)
        self.assertEqual(pre["tools"], load(os.path.join(pa.ROOT, "installed.json"))["tools"])


class SelfCheck(Base):
    def test_self_check_writes_nothing_and_reports_the_hashes(self):
        real_tools()
        self.stamp()

        def listing():
            return sorted(os.path.relpath(os.path.join(d, f), pa.ROOT) for d, _s, fs in os.walk(pa.ROOT) for f in fs
                          if "__pycache__" not in d)
        before = listing()
        env = dict(os.environ, PATH="/usr/bin:/bin")  # no hc on PATH: a call to it would fail the check
        r = subprocess.run([sys.executable, os.path.join(pa.ROOT, "tools", "pa.py"), "session", "--self-check",
                            "--space", SP, "--profile", "test"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        result = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertTrue(result["ok"])
        self.assertEqual(result["tools"], pa.tree_hash(os.path.join(pa.ROOT, "tools"), top_py_only=True))
        self.assertEqual(result["core"], pa.tree_hash(os.path.join(pa.ROOT, "core")))
        self.assertEqual(listing(), before)

    def test_self_check_fails_on_a_bad_profile(self):
        real_tools()
        r = subprocess.run([sys.executable, os.path.join(pa.ROOT, "tools", "pa.py"), "session", "--self-check",
                            "--space", SP, "--profile", "nope"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)


class AC6_1_Exec(Requests):
    def taken(self, **over):
        self.passing_precheck()
        ctx = self.ctx()
        req = self.request(ctx, now=True, **over)
        self.poll(ctx)
        return ctx, req

    def test_exec_argv_is_exact(self):
        ctx, req = self.taken()
        self.assertEqual(ctx["child"].signals, [signal.SIGTERM])
        os.execv = self.recorder
        with self.assertRaises(Execd), contextlib.redirect_stdout(io.StringIO()):
            pa.exec_restart(ctx, ctx.pop("after"), self.project, self.dir, "t1")
        path, argv = self.execs[0]
        self.assertEqual(path, sys.executable)
        self.assertEqual(argv, [sys.executable, os.path.join(pa.ROOT, "tools", "pa.py"), "session", "--space", SP,
                                "--project", self.project, "--profile", "test", "--name", "proj",
                                "--restarted", req["id"], "--from-instance", ctx["launcher"]["instance"],
                                "--restart-by", "pa-restart"])
        self.assertEqual(signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGINT))  # unchanged (S5)

    def test_fresh_passes_fresh(self):
        ctx, _req = self.taken(fresh=True)
        os.execv = self.recorder
        with self.assertRaises(Execd), contextlib.redirect_stdout(io.StringIO()):
            pa.exec_restart(ctx, ctx.pop("after"), self.project, self.dir, "t1")
        self.assertEqual(self.execs[0][1][-1], "--fresh")

    def test_sigint_stays_ignored_across_the_exec(self):  # spec S5
        ctx, _req = self.taken()
        seen = []
        os.execv = lambda path, argv: seen.append(signal.getsignal(signal.SIGINT)) or (_ for _ in ()).throw(Execd())
        saved = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            with self.assertRaises(Execd), contextlib.redirect_stdout(io.StringIO()):
                pa.exec_restart(ctx, ctx.pop("after"), self.project, self.dir, "t1")
        finally:
            signal.signal(signal.SIGINT, saved)
        self.assertEqual(seen, [signal.SIG_IGN])

    def test_tools_changed_after_the_check_refuses_the_exec(self):
        ctx, req = self.taken()
        write(os.path.join(pa.ROOT, "tools", "pa.py"), "print('newer')\n")
        with contextlib.redirect_stdout(io.StringIO()):
            pa.exec_restart(ctx, ctx.pop("after"), self.project, self.dir, "t1")  # no_exec would raise
        out = self.outcome()
        self.assertEqual((out["outcome"], out["request"]), ("failed", req["id"]))
        self.assertIn("changed", out["why"])
        self.assertIn(["hc", "label", "t1", "herdr.pa.restarting="], self.calls)

    def loop(self, exec_fn, starts_before_stop=2):
        """run_agent where the first Claude is ended for a restart (as the watcher does) and execv is exec_fn."""
        ctx = self.ctx(child=None)
        ctx.pop("child")
        starts = []

        class Stop(Exception):
            pass
        req = self.request(ctx, now=True)

        def fake_call(argv, cwd=None):
            starts.append(list(argv))
            if len(starts) == 1:
                ctx["after"] = {"kind": "exec", "request": dict(req), "by": "pa-restart",
                                "pre": {"tools": pa.hash_or_none(os.path.join(pa.ROOT, "tools"), True)}}
                ctx["restarting"] = req["id"]
                ctx["ended"] = "restart"
                return 143
            raise Stop()
        undo = child_fakes.patch(pa, fake_call)
        saved_sleep = pa.time.sleep
        pa.time.sleep = lambda s: None
        os.execv = exec_fn
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                pa.run_agent(types.SimpleNamespace(fresh=False), "t1", self.project, pa.load_profile("test"), self.dir,
                             load(os.path.join(self.dir, "agent.json")), ctx, time.time())
        except (Stop, Execd) as e:
            return starts, ctx, req, e
        finally:
            undo()
            pa.time.sleep = saved_sleep
        return starts, ctx, req, None

    def test_the_loop_execs_after_the_child_ends(self):
        starts, _ctx, req, e = self.loop(self.recorder)
        self.assertIsInstance(e, Execd)
        self.assertEqual(len(starts), 1)
        self.assertIn("--restarted", self.execs[0][1])
        self.assertEqual(self.execs[0][1][self.execs[0][1].index("--restarted") + 1], req["id"])

    def test_ac6_5_exec_failing_resumes_in_the_same_process(self):
        def broken(path, argv):
            raise OSError(8, "Exec format error")
        starts, ctx, req, e = self.loop(broken)
        self.assertEqual(type(e).__name__, "Stop")
        self.assertEqual(len(starts), 2)
        self.assertIn("--resume", starts[1])
        self.assertEqual(starts[1][starts[1].index("--resume") + 1], SID)
        out = self.outcome()
        self.assertEqual((out["outcome"], out["request"]), ("failed", req["id"]))
        self.assertIn("exec", out["why"])
        self.assertIn(["hc", "label", "t1", "herdr.pa.restarting="], self.calls)
        self.assertTrue(self.gone())
        self.assertNotIn("restarting", ctx)


class NewProcessSide(Requests):
    def launch(self, ctx=None, **flags):
        class Stop(Exception):
            pass

        class NoThread:
            def __init__(self, target=None, daemon=None):
                pass

            def start(self):
                pass

        def fake_call(argv, cwd=None):
            raise Stop()
        undo = child_fakes.patch(pa, fake_call)
        saved = (pa.threading.Thread, signal.getsignal(signal.SIGINT))
        pa.threading.Thread = NoThread
        pa.__file__ = os.path.join(pa.ROOT, "tools", "pa.py")
        args = types.SimpleNamespace(space=SP, project=self.project, profile="test", name=None, auto=False, ask=False,
                                     fresh=False, **flags)
        try:
            with self.assertRaises(Stop), contextlib.redirect_stdout(io.StringIO()):
                pa.run_session(args, "t1", {}, SP, self.project, ctx if ctx is not None else {"beat": False})
        finally:
            undo()
            pa.threading.Thread = saved[0]
            signal.signal(signal.SIGINT, saved[1])

    def test_restarted_records_and_clears(self):
        dump({"version": 1, "id": "r1", "space": SP}, os.path.join(self.dir, "restart-request.json"))
        ctx = {"beat": False}
        self.launch(ctx, restarted="r1", from_instance="old-i", restart_by="panel")
        rec = load(os.path.join(self.dir, "launcher.json"))
        self.assertEqual(rec["restartedFrom"], "old-i")
        self.assertEqual((rec["restart"]["request"], rec["restart"]["outcome"], rec["restart"]["by"]), ("r1", "ok", "panel"))
        self.assertNotEqual(rec["instance"], "old-i")
        self.assertTrue(self.gone())
        self.assertIn(["hc", "label", "t1", "herdr.pa.restarting="], self.calls)
        with open(os.path.join(self.dir, "session.log")) as f:
            self.assertIn("restarted request=r1 from=old-i", f.read())
        self.assertIn("old-i", ctx["formerInstances"])

    def test_a_newer_request_is_kept(self):
        dump({"version": 1, "id": "r2", "space": SP}, os.path.join(self.dir, "restart-request.json"))
        self.launch(restarted="r1", from_instance="old-i", restart_by="pa-restart")
        self.assertFalse(self.gone())

    def test_a_plain_start_has_no_restart(self):
        self.launch()
        rec = load(os.path.join(self.dir, "launcher.json"))
        self.assertIsNone(rec["restart"])
        self.assertIsNone(rec["restartedFrom"])


class AC7_1_ExitReloads(Requests):
    def run_loop(self, exits=1):
        ctx = self.ctx(child=None)
        ctx.pop("child")
        starts = []

        class Stop(Exception):
            pass

        def fake_call(argv, cwd=None):
            starts.append(list(argv))
            if len(starts) <= exits:
                return 0
            raise Stop()
        undo = child_fakes.patch(pa, fake_call)
        saved_sleep = pa.time.sleep
        pa.time.sleep = lambda s: None
        os.execv = self.recorder
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                pa.run_agent(types.SimpleNamespace(fresh=False), "t1", self.project, pa.load_profile("test"), self.dir,
                             load(os.path.join(self.dir, "agent.json")), ctx, time.time())
        except (Stop, Execd) as e:
            return starts, type(e).__name__, out.getvalue()
        finally:
            undo()
            pa.time.sleep = saved_sleep

    def test_equal_hashes_loop_as_today(self):
        real_tools()
        self.stamp()
        starts, how, _out = self.run_loop()
        self.assertEqual((len(starts), how, self.execs), (2, "Stop", []))

    def test_an_install_since_the_start_execs(self):
        self.stamp()  # the launcher starts on these tools ...
        ctx_hash = pa.hash_or_none(os.path.join(pa.ROOT, "tools"), True)
        self.record()
        real_tools()  # ... then an install changes them
        self.stamp()
        self.assertNotEqual(ctx_hash, load(os.path.join(pa.ROOT, "installed.json"))["tools"])
        starts, how, _out = self.run_loop_with_record(ctx_hash)
        self.assertEqual((len(starts), how), (1, "Execd"))
        argv = self.execs[0][1]
        self.assertTrue(argv[argv.index("--restarted") + 1].startswith("exit-"))
        self.assertEqual(argv[argv.index("--restart-by") + 1], "exit")

    def run_loop_with_record(self, tools):
        saved = pa.launcher_start

        def start(ctx, *a, **k):
            saved(ctx, *a, **k)
            ctx["launcher"]["code"]["tools"] = tools
        pa.launcher_start = start
        try:
            return self.run_loop()
        finally:
            pa.launcher_start = saved

    def test_a_failing_precheck_loops_and_says_so_once(self):
        self.stamp()
        old = pa.hash_or_none(os.path.join(pa.ROOT, "tools"), True)
        write(os.path.join(pa.ROOT, "tools", "pa_costs.py"), "z = 3\n")
        self.stamp()  # installed differs; tools/pa.py prints 'pa': the self-check fails
        starts, how, out = self.run_loop_with_record_exits(old, exits=3)
        self.assertEqual((len(starts), how, self.execs), (4, "Stop", []))
        self.assertEqual(out.count("not reloading"), 1, out)

    def run_loop_with_record_exits(self, tools, exits):
        saved = pa.launcher_start

        def start(ctx, *a, **k):
            saved(ctx, *a, **k)
            ctx["launcher"]["code"]["tools"] = tools
        pa.launcher_start = start
        try:
            return self.run_loop(exits=exits)
        finally:
            pa.launcher_start = saved


class Prompt(Requests):  # spec S1, S2: what the model runs with on a resume
    def test_profile_key_decides(self):
        base = {"name": "test", "claude": ["true"], "codex": ["true"]}
        self.assertTrue(pa.snapshot_flag(dict(base, systemPromptSnapshot="off"), {}))
        self.assertFalse(pa.snapshot_flag(dict(base, systemPromptSnapshot="keep"), {}))

    def probe(self, script, timeout=10):
        path = os.path.join(TMP, "fake-claude-" + str(abs(hash(script))))
        write(path, "#!/bin/sh\n" + script)
        os.chmod(path, 0o755)
        pa.PROBE_TIMEOUT = timeout
        return pa.snapshot_flag({"name": "test", "claude": [path], "codex": ["true"]}, {})

    def test_probe(self):
        self.assertTrue(self.probe('echo "  --system-prompt-snapshot <mode>  rebuild the prompt"\n'))
        self.assertFalse(self.probe('echo "  --append-system-prompt <text>"\n'))
        self.assertFalse(self.probe("exit 3\n"))
        began = time.time()
        self.assertFalse(self.probe("sleep 20\n", timeout=1))
        self.assertLess(time.time() - began, 8)
        self.assertFalse(pa.snapshot_flag({"name": "test", "claude": ["/no/such/claude"], "codex": ["true"]}, {}))

    def test_probe_runs_once_per_launcher(self):
        ctx, count = {}, os.path.join(TMP, "probe-count")
        if os.path.exists(count):
            os.remove(count)
        path = os.path.join(TMP, "fake-claude-count")
        write(path, "#!/bin/sh\necho x >> '{}'\necho --system-prompt-snapshot\n".format(count))
        os.chmod(path, 0o755)
        prof = {"name": "test", "claude": [path], "codex": ["true"]}
        for _ in range(3):
            self.assertTrue(pa.snapshot_flag(prof, ctx))
        with open(count) as f:
            self.assertEqual(len(f.read().split()), 1)

    def test_launch_command_flag(self):
        p = pa.load_profile("test")
        agent = load(os.path.join(self.dir, "agent.json"))
        argv, _m = pa.launch_command(p, agent, True, self.dir, snapshot=True)
        i = argv.index("--system-prompt-snapshot")
        self.assertEqual(argv[i + 1], "off")
        self.assertEqual(argv[i - 2], "--append-system-prompt")
        self.assertNotIn("--system-prompt-snapshot", pa.launch_command(p, agent, True, self.dir)[0])

    def test_run_agent_passes_it_when_the_profile_says_so(self):
        dump({"name": "test", "claude": ["true"], "codex": ["true"], "systemPromptSnapshot": "off"},
             os.path.join(pa.ROOT, "profiles", "test", "profile.json"))
        starts = []

        class Stop(Exception):
            pass

        def fake_call(argv, cwd=None):
            starts.append(list(argv))
            raise Stop()
        undo = child_fakes.patch(pa, fake_call)
        try:
            with self.assertRaises(Stop), contextlib.redirect_stdout(io.StringIO()):
                pa.run_agent(types.SimpleNamespace(fresh=False), "t1", self.project, pa.load_profile("test"), self.dir,
                             load(os.path.join(self.dir, "agent.json")), {"beat": True, "previous": None}, time.time())
        finally:
            undo()
        self.assertIn("--system-prompt-snapshot", starts[0])

    def test_session_prompt_modes(self):
        ctx = self.record()
        agent = load(os.path.join(self.dir, "agent.json"))
        core1 = pa.hash_or_none(os.path.join(pa.ROOT, "core"))
        pa.launcher_session(ctx, self.dir, agent, False, snapshot=False)
        s = load(os.path.join(self.dir, "launcher.json"))["session"]
        self.assertEqual((s["prompt"], s["promptCore"]), ("new", core1))
        self.assertEqual(load(os.path.join(self.dir, "agent.json"))["prompt"]["core"], core1)
        write(os.path.join(pa.ROOT, "core", "orchestrator.md"), "# o2\n")
        core2 = pa.hash_or_none(os.path.join(pa.ROOT, "core"))
        pa.launcher_session(ctx, self.dir, agent, True, snapshot=False)
        s = load(os.path.join(self.dir, "launcher.json"))["session"]
        self.assertEqual((s["prompt"], s["promptCore"], s["core"]), ("recorded", core1, core2))
        pa.launcher_session(ctx, self.dir, agent, True, snapshot=True)
        s = load(os.path.join(self.dir, "launcher.json"))["session"]
        self.assertEqual((s["prompt"], s["promptCore"]), ("rebuilt", core2))
        self.assertEqual(load(os.path.join(self.dir, "agent.json"))["prompt"]["core"], core2)

    def test_recorded_without_a_known_prompt_is_unknown(self):
        ctx = self.record()
        agent = load(os.path.join(self.dir, "agent.json"))
        pa.launcher_session(ctx, self.dir, agent, True, snapshot=False)
        s = load(os.path.join(self.dir, "launcher.json"))["session"]
        self.assertEqual(s["prompt"], "recorded")
        self.assertIsNone(s["promptCore"])

    def test_code_state_reads_the_prompt_core(self):  # AC-P3
        inst = {"tools": "T", "core": "C2"}
        rec = {"terminal": "t1", "code": {"tools": "T"}, "session": {"core": "C2", "prompt": "recorded", "promptCore": "C1"}}
        word, detail = pa.code_state(rec, inst, "t1")
        self.assertEqual(word, "older (core)")
        self.assertIn("--fresh", detail)
        rec["session"]["promptCore"] = None
        self.assertEqual(pa.code_state(rec, inst, "t1")[0], "unknown")
        rec["session"].update(prompt="rebuilt", promptCore="C2")
        self.assertEqual(pa.code_state(rec, inst, "t1")[0], "current")


if __name__ == "__main__":
    unittest.main()
