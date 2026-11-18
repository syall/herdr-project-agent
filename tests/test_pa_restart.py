"""t-037 J2: the restart in place. J1's Recorded limits (card d-107), the launcher's own Claude child (Popen handle,
end_child), and (later classes) the request watcher, the pre-check, the exec, /exit reloading and pa-restart.
Spec R4-R9, R11 with the accepted changes S1-S6 (spec.md, card d-108); plan.md J2.

Run: python3 -m unittest -v tests/test_pa_restart.py
Temp homes only: HERDR_PA_HOME, HERDR_APP_HOME and CLAUDE_CONFIG_DIR point at a temp folder before pa.py is loaded;
hc is never called (pa.call, hc_get and live_terminals are replaced); os.execv is replaced by a recorder that
raises when a test did not expect it; no real Claude, no real launcher, no signal to any process but a test's
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
        self.saved_os = os.execv
        self.calls = []
        pa.call = lambda argv, check=True: self.calls.append(list(argv)) or ""
        pa.hc_get = lambda address: {}
        pa.live_terminals = lambda: {}

        def no_exec(*a):
            raise AssertionError("os.execv called unexpectedly: {}".format(a))

        os.execv = no_exec  # os.kill is not trapped: subprocess ends its own timed-out child with it (AC9.1 greps pa.py)

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(pa, k, v)
        os.execv = self.saved_os

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


def flag(argv, name):
    """The value of `name` in an exec argv: `--name=value` (round 2) or `--name value`; None when absent."""
    for i, word in enumerate(argv):
        if word.startswith(name + "="):
            return word[len(name) + 1:]
        if word == name and i + 1 < len(argv):
            return argv[i + 1]
    return None


class Requests(Base):
    def setUp(self):
        super().setUp()
        self.agent(sessionStarted=True)
        self.execs = []

        def recorder(path, argv):
            self.execs.append((path, list(argv)))
            raise Execd()
        self.recorder = recorder
        self.saved_timeouts = (pa.SELF_CHECK_TIMEOUT, pa.PROBE_TIMEOUT, pa.restart_precheck)

    def tearDown(self):
        pa.SELF_CHECK_TIMEOUT, pa.PROBE_TIMEOUT, pa.restart_precheck = self.saved_timeouts
        super().tearDown()

    def ctx(self, child=None):
        ctx = self.record()
        ctx.update(beat=True, previous=None, child=child if child is not None else FakeChild(), directory=self.dir, project=self.project,
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
        env = dict(os.environ, PATH="/usr/bin:/bin", HERDR_PA_HOME=pa.ROOT, HERDR_APP_HOME=pa.APP)  # no hc on PATH
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
                            "--space", SP, "--profile", "nope"], env=dict(os.environ, HERDR_PA_HOME=pa.ROOT),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
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
        self.assertEqual(argv, [sys.executable, os.path.join(pa.ROOT, "tools", "pa.py"), "session", "--space=" + SP,
                                "--project=" + self.project, "--profile=test", "--name=proj",
                                "--restarted=" + req["id"], "--from-instance=" + ctx["launcher"]["instance"],
                                "--restart-by=pa-restart"])  # J2 round 2: every value as --flag=value
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
        self.assertEqual(flag(self.execs[0][1], "--restarted"), req["id"])

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
        self.assertTrue(flag(argv, "--restarted").startswith("exit-"))
        self.assertEqual(flag(argv, "--restart-by"), "exit")

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
        starts, how, out = self.run_loop_with_record_exits(old, exits=2)
        self.assertEqual((len(starts), how, self.execs), (3, "Stop", []))
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
        # J2 round 2 (REVIEW A M5): the record is the session's first prompt; a rebuilt start does not replace it
        self.assertEqual(load(os.path.join(self.dir, "agent.json"))["prompt"]["core"], core1)

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



# ---- pa-restart (R5, R8, R9; S3, S4) -----------------------------------------------------------------------------

INSTANCE = "abcdefab-cdef-abcd-efab-cdefabcdefab"


class Command(Base):
    def setUp(self):
        super().setUp()
        self.agent()
        self.rec = {"version": 2, "accepts": ["restart"], "instance": INSTANCE, "pid": 777, "terminal": "t1", "space": SP,
                    "started": pa.now(), "code": {"tools": "T" * 64, "commit": "c" * 40, "installed": "T" * 64},
                    "profile": {"name": "test", "readAt": pa.now()},
                    "session": {"id": SID, "resume": True, "prompt": "rebuilt", "promptCore": "C" * 64, "core": "C" * 64},
                    "restart": None, "restartedFrom": None}
        dump(self.rec, os.path.join(self.dir, "launcher.json"))
        dump({"version": 1, "tools": "T" * 64, "core": "C" * 64}, os.path.join(pa.ROOT, "installed.json"))
        write(os.path.join(self.dir, "heartbeat"), "{} t1\n".format(int(time.time())))
        dump({"version": 1, "tasks": [{"id": "t-1", "workers": [{"address": "w1"}, {"address": "w2", "finished": True}]}]},
             os.path.join(self.dir, "ledger.json"))
        self.term = {"address": "t1", "state": "live", "labels": {"herdr.pa": SP}, "pid": 777, "parent": None,
                     "status": {"state": "idle"}}
        self.terms = {"t1": self.term,
                      "w1": {"address": "w1", "state": "live", "parent": "t1", "name": "t-1-build"},
                      "w4": {"address": "w4", "state": "live", "parent": "t1", "name": "helper"},
                      "w3": {"address": "w3", "state": "live", "parent": "t9", "name": "other-space"}}
        pa.hc_get = lambda address: dict(self.terms.get(address) or {})
        pa.live_terminals = lambda: {k: dict(v) for k, v in self.terms.items()}
        self.saved_env = {k: os.environ.get(k) for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_SANDBOX",
                                                         "HC_TERMINAL_ID")}
        for k in self.saved_env:
            os.environ.pop(k, None)
        self.saved_poll, self.saved_relay = pa.RESTART_POLL, pa.relay
        pa.RESTART_POLL = 0.05

    def tearDown(self):
        pa.RESTART_POLL, pa.relay = self.saved_poll, self.saved_relay
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def run_cmd(self, **over):
        args = dict(space=SP, on=None, now=False, fresh=False, why=None, wait=1, dry_run=False, json=False)
        args.update(over)
        return quiet(pa.cmd_restart, argparse.Namespace(**args))

    def request_file(self):
        path = os.path.join(self.dir, "restart-request.json")
        return load(path) if os.path.exists(path) else None

    def no_ending_calls(self):
        self.assertFalse([c for c in self.calls if "kill" in c or "rm" in c], self.calls)


class AC5_1_Identification(Command):
    def refused(self, part, **over):
        code, out, err = self.run_cmd(**over)
        self.assertEqual(code, 2, out + err)
        self.assertIn(part, out + err)
        self.assertIsNone(self.request_file())
        self.no_ending_calls()

    def test_no_agent_json(self):
        os.remove(os.path.join(self.dir, "agent.json"))
        self.refused("no Project Agent")

    def test_closed(self):
        self.agent(closed=pa.now())
        self.refused("closed")

    def test_no_launcher_json_is_the_r10_message(self):
        os.remove(os.path.join(self.dir, "launcher.json"))
        self.refused("started before restart support")

    def test_a_j1_record_is_the_r10_message(self):  # spec S4
        dump(dict(self.rec, version=1, accepts=None), os.path.join(self.dir, "launcher.json"))
        self.refused("started before restart support")

    def test_launcher_json_for_another_terminal(self):
        dump(dict(self.rec, terminal="t2"), os.path.join(self.dir, "launcher.json"))
        self.refused("terminal")

    def test_stale_heartbeat(self):
        write(os.path.join(self.dir, "heartbeat"), "{} t1\n".format(int(time.time()) - 300))
        self.refused("heartbeat")

    def test_heartbeat_for_another_terminal(self):
        write(os.path.join(self.dir, "heartbeat"), "{} t5\n".format(int(time.time())))
        self.refused("heartbeat")

    def test_label_missing(self):
        self.term["labels"] = {}
        self.refused("label")

    def test_not_live(self):
        self.term["state"] = "finished"
        self.refused("live")

    def test_has_a_parent(self):
        self.term["parent"] = "t0"
        self.refused("parent")

    def test_pid_mismatch(self):
        self.term["pid"] = 778
        self.refused("pid")


class AC5_2_Summary(Command):
    def test_dry_run_lists_the_live_workers_and_writes_nothing(self):
        code, out, err = self.run_cmd(dry_run=True)
        self.assertEqual(code, 0, err)
        self.assertIn("w1", out)
        self.assertIn("w4", out)
        self.assertNotIn("w2", out)
        self.assertNotIn("w3", out)
        self.assertIn("keeps running", out)
        self.assertIn("restarts when this turn ends", out)
        self.assertIsNone(self.request_file())
        self.no_ending_calls()

    def test_mid_turn_and_now_words(self):
        self.term["status"] = {"state": "working"}
        _c, out, _e = self.run_cmd(dry_run=True)
        self.assertIn("mid-turn", out)
        _c, out, _e = self.run_cmd(dry_run=True, now=True)
        self.assertIn("cut off", out)


class Answering(threading.Thread):
    """Stands in for the launcher: once the request is there, writes what a launcher writes."""

    def __init__(self, case, test):
        super().__init__(daemon=True)
        self.case, self.test = case, test

    def run(self):
        path = os.path.join(self.case.dir, "restart-request.json")
        for _ in range(200):
            if os.path.exists(path):
                break
            time.sleep(0.01)
        req = load(path)
        self.test(req)


class AC5_3_Wait(Command):
    def answer(self, fn):
        a = Answering(self, fn)
        a.start()
        return a

    def test_success_prints_what_was_loaded(self):
        def ok(req):
            time.sleep(0.1)
            rec = dict(self.rec, instance="new-instance", restartedFrom=INSTANCE,
                       restart={"request": req["id"], "outcome": "ok", "by": "pa-restart"})
            dump(rec, os.path.join(self.dir, "launcher.json"))
        self.answer(ok)
        code, out, err = self.run_cmd(wait=5)
        self.assertEqual(code, 0, out + err)
        self.assertIn("Restarted", out)
        self.assertIn("core text: current", out)
        self.assertIn("resumed", out)

    def test_the_request(self):
        seen = {}
        self.answer(lambda req: seen.update(req))
        self.run_cmd(wait=0.5, now=True, fresh=True, why="the person said so")
        self.assertEqual((seen["space"], seen["instance"], seen["terminal"], seen["by"], seen["now"], seen["fresh"]),
                         (SP, INSTANCE, "t1", "pa-restart", True, True))
        self.assertEqual(pa.parse_time(seen["expires"]) - pa.parse_time(seen["at"]), 600)

    def test_recorded_says_fresh(self):
        def ok(req):
            rec = dict(self.rec, instance="new", restartedFrom=INSTANCE,
                       restart={"request": req["id"], "outcome": "ok"},
                       session={"id": SID, "resume": True, "prompt": "recorded", "promptCore": "B" * 64, "core": "C" * 64})
            dump(rec, os.path.join(self.dir, "launcher.json"))
        self.answer(ok)
        code, out, _err = self.run_cmd(wait=5)
        self.assertEqual(code, 0)
        self.assertIn("pa-restart --fresh", out)

    def test_a_failed_outcome(self):
        def failed(req):
            dump(dict(self.rec, restart={"request": req["id"], "outcome": "failed", "why": "boom: self-check"}),
                 os.path.join(self.dir, "launcher.json"))
        self.answer(failed)
        code, out, err = self.run_cmd(wait=5)
        self.assertEqual(code, 1)
        self.assertIn("boom: self-check", out + err)

    def test_timeout_removes_its_own_request(self):
        code, out, err = self.run_cmd(wait=0.3)
        self.assertEqual(code, 1)
        self.assertIn("did not happen", out + err)
        self.assertIsNone(self.request_file())

    def test_timeout_keeps_a_newer_request(self):
        def newer(req):
            dump(dict(req, id="someone-newer"), os.path.join(self.dir, "restart-request.json"))
        self.answer(newer)
        code, _out, _err = self.run_cmd(wait=0.4)
        self.assertEqual(code, 1)
        self.assertEqual(self.request_file()["id"], "someone-newer")

    def test_wait_is_capped(self):
        self.assertEqual(pa.restart_wait(10000), pa.RESTART_WAIT_MAX)
        self.assertLess(pa.RESTART_WAIT_MAX, 600)


class AC5_4_On(Command):
    def test_on_relays_through_hc_exec_and_reads_nothing_here(self):
        calls = []
        pa.relay = lambda argv: calls.append(list(argv)) or 0
        empty = tempfile.mkdtemp(prefix="empty-", dir=TMP)
        saved_root = pa.ROOT
        pa.ROOT = empty
        try:
            code, _out, err = self.run_cmd(on="m2", space=SP, now=True, why="it's 'quoted'", wait=30)
        finally:
            pa.ROOT = saved_root
        self.assertEqual(code, 0, err)
        self.assertEqual(os.listdir(empty), [])
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual(c[:9], ["hc", "exec", "--on", "m2", "--timeout", "90", "--", "sh", "-c"])  # round 2: wait + 60
        script = c[9]
        self.assertTrue(script.startswith('"$HOME/.config/herdr-pa/tools/pa.py" restart --space ' + SP))
        for part in ("--now", "--wait 30", "--why"):
            self.assertIn(part, script)
        import shlex
        words = shlex.split(script)
        self.assertEqual(words[words.index("--why") + 1], "it's 'quoted'")
        self.assertEqual(self.calls, [])

    def test_on_needs_a_space_and_says_when_missing_there(self):
        code, _o, err = self.run_cmd(on="m2", space=None)
        self.assertEqual(code, 2)
        self.assertIn("--space", err)
        pa.relay = lambda argv: 127
        code, out, err = self.run_cmd(on="m2")
        self.assertEqual(code, 127)
        self.assertIn("pa-sync --on m2", out + err)


class AC5_5_FromTheProjectAgent(Command):
    def test_needs_why(self):
        os.environ["CLAUDECODE"] = "1"
        code, _o, err = self.run_cmd()
        self.assertEqual(code, 2)
        self.assertIn("--why", err)
        self.assertIsNone(self.request_file())

    def test_with_why_journals_and_does_not_wait(self):
        os.environ["CLAUDECODE"] = "1"
        os.environ["HC_TERMINAL_ID"] = "t1"
        began = time.time()
        code, out, err = self.run_cmd(why="restart it now please", wait=30)
        self.assertEqual(code, 0, err)
        self.assertLess(time.time() - began, 5)
        self.assertIn("when this turn ends", out)
        req = self.request_file()
        self.assertEqual((req["by"], req["why"]), ("orchestrator", "restart it now please"))
        with open(os.path.join(self.dir, "journal.jsonl")) as f:
            lines = [json.loads(x) for x in f.read().splitlines() if x.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual((lines[0]["by"], lines[0]["trigger"], lines[0]["why"]),
                         ("orchestrator", "conversation", "restart: restart it now please"))


class MainAndInstall(Base):
    def test_main_knows_pa_restart(self):
        for argv0, extra in (("pa-restart", []), ("pa.py", ["restart"])):
            seen = {}
            saved = (sys.argv, pa.cmd_restart)
            sys.argv = [argv0] + extra + ["--space", SP, "--dry-run", "--now", "--fresh", "--why", "w", "--wait", "9", "--on", "m"]
            pa.cmd_restart = lambda args: seen.update(vars(args))
            try:
                pa.main()
            finally:
                sys.argv, pa.cmd_restart = saved
            self.assertEqual((seen["space"], seen["dry_run"], seen["now"], seen["fresh"], seen["why"], seen["wait"], seen["on"]),
                             (SP, True, True, True, "w", 9.0, "m"))

    def test_install_links_pa_restart(self):
        home = tempfile.mkdtemp(prefix="home-", dir=TMP)
        env = {k: v for k, v in os.environ.items() if not k.startswith("HC_") and k != "CLAUDECODE"}
        env.update(HOME=home, HERDR_PA_HOME=os.path.join(home, ".config", "herdr-pa"),
                   HERDR_APP_HOME=os.path.join(home, ".config", "herdr-app"))
        r = subprocess.run(["sh", os.path.join(REPO, "install.sh")], env=env, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, universal_newlines=True)
        self.assertEqual(r.returncode, 0, r.stdout[-2000:])
        link = os.path.join(home, ".local", "bin", "pa-restart")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.path.realpath(link), os.path.realpath(os.path.join(env["HERDR_PA_HOME"], "tools", "pa.py")))


class AC9_1_OnlyTheOwnChild(unittest.TestCase):
    def test_signals_only_in_end_child_and_run_child(self):
        import ast
        with open(os.path.join(REPO, "runtime", "tools", "pa.py")) as f:
            text = f.read()
        self.assertNotIn("os.kill(", text)
        self.assertNotIn("killpg", text)
        self.assertEqual(text.count('"kill"'), 2, "hc kill only in cmd_cleanup and end_terminal (spec AC9.1, R10)")
        tree = ast.parse(text)
        hc_kill = set()
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef):
                for node in ast.walk(fn):
                    if (isinstance(node, ast.List) and len(node.elts) >= 2
                            and [getattr(e, "value", None) for e in node.elts[:2]] == ["hc", "kill"]):
                        hc_kill.add(fn.name)
        self.assertEqual(hc_kill, {"cmd_cleanup", "end_terminal"})
        found = []
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef):
                for node in ast.walk(fn):
                    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and node.func.attr in ("send_signal", "kill", "terminate")):
                        found.append(fn.name)
        self.assertTrue(found)
        self.assertEqual(set(found) - {"end_child", "run_child"}, set(), found)


# ---- J2 round 2: the seven Must fix items (REVIEW J2 A M1-M5, B M1-M3; VERIFY J2 MF1-MF4; handoff-fix-j2.md) -------

class R2_1_Fields(Requests):
    """Item 1: every request field is checked before use; the exec passes values as --flag=value; the self-check
    parses the exact exec argv; execv raising anything resumes the loop."""

    def test_bad_fields_are_refused_before_anything_is_ended(self):
        cases = {"by dash": {"by": "-x"}, "by nul": {"by": "pa-restart\u0000"}, "by unknown": {"by": "someone"},
                 "by exit": {"by": "exit"}, "id dash": {"id": "-x"}, "id long": {"id": "a" * 100},
                 "id nul": {"id": "ab\u0000"}, "instance space": {"instance": "a b"}, "terminal newline": {"terminal": "t1\n"},
                 "space bad": {"space": "../sp1"}, "why nul": {"why": "x\u0000y"}, "why long": {"why": "w" * 2001},
                 "at long": {"at": "2026-11-18T07:24:47Z" + " " * 40}}
        for name, over in cases.items():
            with self.subTest(case=name):
                self.passing_precheck()
                ctx = self.ctx()
                self.request(ctx, now=True, **over)
                self.poll(ctx)
                out = self.outcome()
                self.assertEqual(out["outcome"], "refused", out)
                self.assertTrue(self.gone())
                self.assertEqual(ctx["child"].signals, [])
                self.assertNotIn("after", ctx)

    def test_a_good_why_with_a_newline_is_taken(self):
        self.passing_precheck()
        ctx = self.ctx()
        self.request(ctx, now=True, why="the person said:\n'restart it'")
        self.poll(ctx)
        self.assertEqual(ctx["child"].signals, [signal.SIGTERM])

    def test_a_name_starting_with_a_dash_parses_in_the_real_launcher(self):
        real_tools()
        self.stamp()
        self.agent(sessionStarted=True, name="-x")
        ctx = self.ctx()
        req = self.request(ctx, now=True)
        argv = pa.restart_argv(ctx, req["id"], "pa-restart", False, self.project, self.dir)
        self.assertIn("--name=-x", argv)
        pre, why = pa.restart_precheck(ctx, SP, "test", argv)
        self.assertIsNone(why)
        self.assertEqual(pre["argv"], argv)

    def test_the_self_check_parses_the_exec_argv(self):  # REVIEW B M3: a parser without --restart-by
        real_tools()
        path = os.path.join(pa.ROOT, "tools", "pa.py")
        with open(path) as f:
            text = f.read()
        line = '        parser.add_argument("--restart-by", help=argparse.SUPPRESS)\n'
        self.assertIn(line, text)
        write(path, text.replace(line, ""))
        self.stamp()
        ctx = self.ctx()
        self.request(ctx, now=True)
        self.poll(ctx)
        out = self.outcome()
        self.assertEqual(out["outcome"], "failed", out)
        self.assertIn("self-check", out["why"])
        self.assertEqual(ctx["child"].signals, [])
        self.assertNotIn("after", ctx)

    def test_execv_value_error_resumes_the_loop(self):  # REVIEW A M1: a NUL reached execv
        def nul(path, argv):
            raise ValueError("embedded null byte")
        starts, ctx, req, e = AC6_1_Exec.loop(self, nul)
        self.assertEqual(type(e).__name__, "Stop")
        self.assertEqual(len(starts), 2)
        out = self.outcome()
        self.assertEqual((out["outcome"], out["request"]), ("failed", req["id"]))
        self.assertNotIn("restarting", ctx)


class R2_2_NoChild(Requests):
    """Item 2: a request taken while no Claude handle is held is acted on at the next safe point."""

    def test_p3_run_child_does_not_start_claude_once_a_restart_is_taken(self):
        ctx = self.ctx()
        ctx["child"] = None  # the loop is past take_after, building the next launch
        self.request(ctx, now=True)
        self.passing_precheck()
        self.poll(ctx)
        self.assertTrue(ctx.get("after"))
        spawned = []
        saved = pa.spawn
        pa.spawn = lambda argv, cwd=None: spawned.append(argv) or FakeChild()
        try:
            code = pa.run_child(ctx, ["claude"], self.project)
        finally:
            pa.spawn = saved
        self.assertEqual(spawned, [])
        self.assertIsNone(code)
        self.assertEqual(ctx.get("ended"), "restart")
        self.assertIsNone(ctx.get("child"))

    def test_taken_during_the_probe_execs_before_any_claude_starts(self):
        ctx = self.ctx(child=None)
        ctx.pop("child")
        req = self.request(ctx, now=True)
        starts = []

        class Stop(Exception):
            pass

        def fake_call(argv, cwd=None):
            starts.append(list(argv))
            raise Stop()
        saved_flag = pa.snapshot_flag

        def probing(p, c=None):  # the watcher takes the request while the main thread runs the --help probe
            if not ctx.get("restarting"):
                pa.begin_restart(ctx, self.dir, "t1", dict(req), {"tools": pa.hash_or_none(os.path.join(pa.ROOT, "tools"), True)})
            return False
        undo = child_fakes.patch(pa, fake_call)
        pa.snapshot_flag = probing
        os.execv = self.recorder
        try:
            with self.assertRaises(Execd), contextlib.redirect_stdout(io.StringIO()):
                pa.run_agent(types.SimpleNamespace(fresh=False), "t1", self.project, pa.load_profile("test"), self.dir,
                             load(os.path.join(self.dir, "agent.json")), ctx, time.time())
        finally:
            undo()
            pa.snapshot_flag = saved_flag
        self.assertEqual(starts, [])
        self.assertEqual(flag(self.execs[0][1], "--restarted"), req["id"])

    def pipe_stdin(self):
        r, w = os.pipe()
        saved = sys.stdin
        sys.stdin = os.fdopen(r)
        self.addCleanup(os.close, w)
        self.addCleanup(lambda: (sys.stdin.close(), setattr(sys, "stdin", saved)))

    def later(self, ctx, after=0.3):
        def go():
            time.sleep(after)
            ctx["after"] = {"kind": "exec", "request": {"id": "rX"}, "by": "pa-restart"}
        threading.Thread(target=go, daemon=True).start()

    def test_the_enter_wait_returns_when_a_restart_is_taken(self):  # the quick-exit wait (FINDINGS L5)
        ctx = self.ctx(child=None)
        self.pipe_stdin()
        self.later(ctx)
        began = time.time()
        self.assertFalse(pa.wait_enter(ctx))
        self.assertLess(time.time() - began, 5)

    def test_error_wait_returns_when_a_restart_is_taken(self):
        ctx = self.ctx(child=None)
        self.pipe_stdin()
        self.later(ctx)
        saved = signal.getsignal(signal.SIGINT)
        began = time.time()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                pa.error_wait("t1", pa.Problem("x"), ctx)
        finally:
            signal.signal(signal.SIGINT, saved)
        self.assertLess(time.time() - began, 5)
        self.assertIn(["hc", "label", "t1", "herdr.pa.error="], self.calls)

    def test_cmd_session_execs_a_restart_taken_during_error_wait(self):
        ctx_seen, runs = {}, []

        class Stop(Exception):
            pass
        req = None

        def run_session(args, address, labels, space, project, ctx):
            runs.append(1)
            if len(runs) > 1:
                raise Stop()
            ctx.update(self.ctx(child=None))
            ctx_seen["ctx"] = ctx
            raise pa.Problem("the project folder is gone")

        def error_wait(address, problem, ctx=None):
            c = ctx_seen["ctx"]
            r = self.request(c, now=True)
            ctx_seen["req"] = r
            pa.begin_restart(c, self.dir, "t1", dict(r), {"tools": pa.hash_or_none(os.path.join(pa.ROOT, "tools"), True)})

        class Tty(io.StringIO):
            def isatty(self):
                return True
        saved = (pa.run_session, pa.error_wait, sys.stdin, os.environ.get("HC_TERMINAL_ID"))
        pa.run_session, pa.error_wait, sys.stdin = run_session, error_wait, Tty()
        os.environ["HC_TERMINAL_ID"] = "t1"
        os.execv = self.recorder
        try:
            args = types.SimpleNamespace(space=SP, project=self.project, profile="test", name=None, auto=False, ask=False,
                                         fresh=False, restarted=None)
            with self.assertRaises(Execd), contextlib.redirect_stdout(io.StringIO()):
                pa.cmd_session(args)
        finally:
            pa.run_session, pa.error_wait, sys.stdin = saved[:3]
            if saved[3] is None:
                os.environ.pop("HC_TERMINAL_ID", None)
            else:
                os.environ["HC_TERMINAL_ID"] = saved[3]
        self.assertEqual(len(runs), 1)
        self.assertEqual(flag(self.execs[0][1], "--restarted"), ctx_seen["req"]["id"])


class R2_3_Withdrawn(Requests):
    """Item 3: a request withdrawn or replaced before the exec is not acted on."""

    def test_p2_withdrawn_during_the_precheck_ends_nothing(self):
        ctx = self.ctx()
        req = self.request(ctx, now=True)
        path = os.path.join(self.dir, "restart-request.json")

        def precheck(*a, **k):  # pa-restart's timeout removes its own request while the pre-check runs
            os.remove(path)
            return ({"tools": "x", "core": "y"}, None)
        pa.restart_precheck = precheck
        self.poll(ctx)
        self.assertFalse(ctx.get("after"))
        self.assertEqual(ctx["child"].signals, [])
        out = self.outcome()
        self.assertEqual((out["outcome"], out["request"]), ("refused", req["id"]))
        self.assertIn("withdrawn", out["why"])
        self.assertNotIn(["hc", "label", "t1", "herdr.pa.restarting=" + req["id"]], self.calls)

    def taken(self, **over):
        self.passing_precheck()
        ctx = self.ctx()
        req = self.request(ctx, now=True, **over)
        self.poll(ctx)
        self.assertEqual(ctx["child"].signals, [signal.SIGTERM])
        return ctx, req

    def exec_now(self, ctx):
        os.execv = self.recorder
        with contextlib.redirect_stdout(io.StringIO()):
            pa.exec_restart(ctx, ctx.pop("after"), self.project, self.dir, "t1")

    def test_withdrawn_before_the_exec(self):
        ctx, req = self.taken()
        os.remove(os.path.join(self.dir, "restart-request.json"))
        self.exec_now(ctx)
        self.assertEqual(self.execs, [])
        out = self.outcome()
        self.assertEqual((out["outcome"], out["request"]), ("refused", req["id"]))
        self.assertIn("withdrawn", out["why"])
        self.assertIn(["hc", "label", "t1", "herdr.pa.restarting="], self.calls)
        self.assertNotIn("restarting", ctx)

    def test_replaced_before_the_exec_keeps_the_newer(self):
        ctx, req = self.taken()
        newer = dict(req, id="newer-one")
        dump(newer, os.path.join(self.dir, "restart-request.json"))
        self.exec_now(ctx)
        self.assertEqual(self.execs, [])
        self.assertEqual(self.outcome()["outcome"], "refused")
        self.assertEqual(load(os.path.join(self.dir, "restart-request.json"))["id"], "newer-one")

    def test_expired_before_the_exec(self):
        at = time.time()
        ctx, req = self.taken(at=pa.iso_time(at), expires=pa.iso_time(at + 2))
        time.sleep(3.1)
        self.exec_now(ctx)
        self.assertEqual(self.execs, [])
        out = self.outcome()
        self.assertEqual(out["outcome"], "refused")
        self.assertIn("expired", out["why"])

    def test_still_there_execs(self):
        ctx, req = self.taken()
        with self.assertRaises(Execd):
            self.exec_now(ctx)
        self.assertEqual(flag(self.execs[0][1], "--restarted"), req["id"])


class R2_4_OnTimeout(Command):
    """Item 4: the relay passes hc exec a timeout that covers the wait; a relay timeout is unknown, not failed."""

    def relayed(self, **over):
        calls = []
        pa.relay = lambda argv: calls.append(list(argv)) or 0
        code, _out, err = self.run_cmd(on="m2", space=SP, **over)
        self.assertEqual(code, 0, err)
        return calls[0]

    def test_the_timeout_covers_the_wait(self):
        for wait, timeout in ((30, "90"), (None, "240"), (540, "600"), (10000, "600"), (0.5, "61")):
            with self.subTest(wait=wait):
                c = self.relayed(wait=wait)
                self.assertEqual(c[:7], ["hc", "exec", "--on", "m2", "--timeout", timeout, "--"])

    def test_a_relay_timeout_is_unknown(self):
        saved = pa.__dict__.get("RELAY_MARGIN")
        pa.RELAY_MARGIN = 1
        self.addCleanup(lambda: setattr(pa, "RELAY_MARGIN", saved) if saved is not None else pa.__dict__.pop("RELAY_MARGIN", None))
        pa.relay = lambda argv: time.sleep(1.05) or 1
        code, out, err = self.run_cmd(on="m2", space=SP, wait=0)
        self.assertEqual(code, 3, out + err)
        self.assertIn("unknown", out + err)

    def test_a_quick_failure_is_failed(self):
        pa.relay = lambda argv: 1
        code, out, err = self.run_cmd(on="m2", space=SP, wait=30)
        self.assertEqual(code, 1)
        self.assertNotIn("unknown", out + err)


class R2_5_OnInsideClaude(Command):
    """Item 5: the CLAUDECODE rule and the journal line hold for --on too."""

    def test_p1_on_inside_claude_needs_why(self):
        os.environ["CLAUDECODE"] = "1"
        os.environ["HC_TERMINAL_ID"] = "t1"
        calls = []
        pa.relay = lambda argv: calls.append(list(argv)) or 0
        code, _out, err = self.run_cmd(on="m2", space=SP, why=None)
        self.assertEqual((code, calls), (2, []))
        self.assertIn("--why", err)

    def test_on_inside_claude_with_why_journals_here(self):
        os.environ["CLAUDECODE"] = "1"
        os.environ["HC_TERMINAL_ID"] = "t1"
        calls = []
        pa.relay = lambda argv: calls.append(list(argv)) or 0
        code, _out, err = self.run_cmd(on="m2", space="sp2", why="restart the other one")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(calls), 1)
        with open(os.path.join(self.dir, "journal.jsonl")) as f:
            lines = [json.loads(x) for x in f.read().splitlines() if x.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual((lines[0]["by"], lines[0]["trigger"], lines[0]["on"], lines[0]["space"]),
                         ("orchestrator", "conversation", "m2", "sp2"))
        self.assertIn("restart the other one", lines[0]["why"])


class R2_6_PromptRecord(Requests):
    """Item 6: the recorded prompt is the session's first; a rebuilt start reports the current core without
    replacing it; a later recorded resume reports the first (spec S1)."""

    def test_new_then_rebuilt_then_recorded(self):
        ctx = self.record()
        agent = load(os.path.join(self.dir, "agent.json"))
        core_a = pa.hash_or_none(os.path.join(pa.ROOT, "core"))
        pa.launcher_session(ctx, self.dir, agent, False)
        write(os.path.join(pa.ROOT, "core", "orchestrator.md"), "# B\n")
        core_b = pa.hash_or_none(os.path.join(pa.ROOT, "core"))
        pa.launcher_session(ctx, self.dir, agent, True, snapshot=True)
        s = load(os.path.join(self.dir, "launcher.json"))["session"]
        self.assertEqual((s["prompt"], s["promptCore"]), ("rebuilt", core_b))
        pa.launcher_session(ctx, self.dir, agent, True, snapshot=False)
        s = load(os.path.join(self.dir, "launcher.json"))["session"]
        self.assertEqual((s["prompt"], s["promptCore"]), ("recorded", core_a))
        word, _detail = pa.code_state(load(os.path.join(self.dir, "launcher.json")),
                                      {"tools": s and load(os.path.join(self.dir, "launcher.json"))["code"]["tools"],
                                       "core": core_b}, "t1")
        self.assertEqual(word, "older (core)")


class R2_7_RestartedPathRaises(Base):
    """Item 7: a launcher exec'd by a restart that raises anything goes to error_wait, and Enter loads the
    installed launcher again; the terminal and its workers stay."""

    def main_with(self, argv, raising, precheck=None, error_wait=None):
        """precheck: restart_precheck for the Enter re-exec (round 3; default: passes); error_wait: its stand-in
        (default: records the call and returns, i.e. Enter)."""
        seen = []
        saved = (sys.argv, pa.cmd_session, pa.error_wait, os.environ.get("HC_TERMINAL_ID"), pa.restart_precheck)
        sys.argv = argv
        pa.cmd_session = lambda args: (_ for _ in ()).throw(raising)
        pa.error_wait = error_wait or (lambda address, problem, ctx=None: seen.append((address, str(problem))))
        if error_wait:
            seen = error_wait.seen
        pa.restart_precheck = precheck or (lambda ctx, space, profile, argv=None: ({"tools": "T", "core": "C",
                                                                                   "argv": list(argv)}, None))
        os.environ["HC_TERMINAL_ID"] = "t1"
        execs = []

        def recorder(path, a):
            execs.append(list(a))
            raise Execd()
        os.execv = recorder
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                try:
                    pa.main()
                except BaseException as e:  # noqa: BLE001 - what main let through
                    return seen, execs, e
            return seen, execs, None
        finally:
            sys.argv, pa.cmd_session, pa.error_wait = saved[:3]
            pa.restart_precheck = saved[4]
            if saved[3] is None:
                os.environ.pop("HC_TERMINAL_ID", None)
            else:
                os.environ["HC_TERMINAL_ID"] = saved[3]

    def test_any_exception_goes_to_error_wait_and_enter_reloads(self):
        flags = ["--space", SP, "--restarted=r1", "--from-instance=i0", "--restart-by=pa-restart"]
        seen, execs, e = self.main_with(["pa-session"] + flags, RuntimeError("a bug only on the restarted path"))
        self.assertIsInstance(e, Execd)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], "t1")
        self.assertIn("RuntimeError", seen[0][1])
        self.assertIn("a bug only on the restarted path", seen[0][1])
        self.assertIn("Enter", seen[0][1])
        self.assertEqual(execs[0], [sys.executable, os.path.join(pa.ROOT, "tools", "pa.py"), "session"] + flags)

    def test_a_plain_start_still_raises(self):
        seen, execs, e = self.main_with(["pa-session", "--space", SP], RuntimeError("plain"))
        self.assertIsInstance(e, RuntimeError)
        self.assertEqual((seen, execs), ([], []))



# ---- J2 round 3 (handoff-fix-j2-r3.md) --------------------------------------------------------------------------

class TtyPipe:
    """A selectable stdin that says it is a tty (cmd_session shows errors only to a tty)."""

    def __init__(self, f):
        self.f = f

    def isatty(self):
        return True

    def fileno(self):
        return self.f.fileno()

    def readline(self):
        return self.f.readline()


class R3_1_SigintIgnoredAtEveryExec(Requests):
    """Item 1 (REVIEW A M1 = B M1, spec S5): SIGINT is ignored at the restart's execv whatever path leads there."""

    def sigint_recorder(self, seen, raising=Execd):
        def recorder(path, argv):
            seen.append(signal.getsignal(signal.SIGINT))
            raise raising() if raising is Execd else raising
        return recorder

    def test_p_s5_a_restart_taken_during_error_wait_execs_with_sigint_ignored(self):  # the reviewers' probe
        runs, seen, box = [], [], {}
        r, w = os.pipe()
        stdin = os.fdopen(r)
        self.addCleanup(os.close, w)
        self.addCleanup(stdin.close)

        def run_session(args, address, labels, space, project, ctx):
            runs.append(1)
            if len(runs) > 1:
                raise AssertionError("run_session again: the restart was not exec'd")
            ctx.update(self.ctx(child=None))

            def take():
                time.sleep(0.3)
                req = self.request(ctx, now=True)
                box["req"] = req
                pa.begin_restart(ctx, self.dir, "t1", dict(req),
                                 {"tools": pa.hash_or_none(os.path.join(pa.ROOT, "tools"), True)})
            threading.Thread(target=take, daemon=True).start()
            raise pa.Problem("the project folder is gone")

        saved = (pa.run_session, sys.stdin, os.environ.get("HC_TERMINAL_ID"), signal.getsignal(signal.SIGINT))
        pa.run_session, sys.stdin = run_session, TtyPipe(stdin)
        os.environ["HC_TERMINAL_ID"] = "t1"
        os.execv = self.sigint_recorder(seen)
        try:
            args = types.SimpleNamespace(space=SP, project=self.project, profile="test", name=None, auto=False, ask=False,
                                         fresh=False, restarted=None)
            with self.assertRaises(Execd), contextlib.redirect_stdout(io.StringIO()):
                pa.cmd_session(args)  # the real error_wait: it installs default_int_handler for its Enter/Ctrl-C
        finally:
            pa.run_session, sys.stdin = saved[:2]
            signal.signal(signal.SIGINT, saved[3])
            if saved[2] is None:
                os.environ.pop("HC_TERMINAL_ID", None)
            else:
                os.environ["HC_TERMINAL_ID"] = saved[2]
        self.assertEqual(seen, [signal.SIG_IGN])

    def test_exec_restart_ignores_sigint_at_the_exec_and_restores_it_when_the_exec_fails(self):
        ctx = self.ctx()
        self.request(ctx, now=True)
        self.passing_precheck()
        self.poll(ctx)
        seen = []
        os.execv = self.sigint_recorder(seen, OSError("no such file"))
        saved = signal.signal(signal.SIGINT, signal.default_int_handler)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                pa.exec_restart(ctx, ctx.pop("after"), self.project, self.dir, "t1")
            after = signal.getsignal(signal.SIGINT)
        finally:
            signal.signal(signal.SIGINT, saved)
        self.assertEqual(seen, [signal.SIG_IGN])
        self.assertIs(after, signal.default_int_handler)  # the handler from before the exec is back
        self.assertEqual(self.outcome()["outcome"], "failed")


class R3_2_RecoveryPreCheck(Base):
    """Item 2 (REVIEW A M2 = B M2 = VERIFY MF-r2-1): the Enter re-exec of restarted_session runs the restart's
    pre-check (installed.json, the exact argv's --self-check) first; on failure it stays in error_wait and says why."""

    FLAGS = ["--space", SP, "--restarted=r1", "--from-instance=i0", "--restart-by=pa-restart"]
    main_with = R2_7_RestartedPathRaises.main_with

    def waits(self, enters):
        """An error_wait stand-in: Enter `enters` times, then Ctrl-C."""
        def error_wait(address, problem, ctx=None):
            error_wait.seen.append((address, str(problem)))
            if len(error_wait.seen) > enters:
                raise KeyboardInterrupt()
        error_wait.seen = []
        return error_wait

    def test_enter_prechecks_the_exact_argv_it_execs(self):
        checked = []

        def precheck(ctx, space, profile, argv=None):
            checked.append((space, argv))
            return {"tools": "T", "core": "C", "argv": list(argv)}, None
        seen, execs, e = self.main_with(["pa-session"] + self.FLAGS, RuntimeError("bug"), precheck=precheck)
        self.assertIsInstance(e, Execd)
        argv = [sys.executable, os.path.join(pa.ROOT, "tools", "pa.py"), "session"] + self.FLAGS
        self.assertEqual(checked, [(SP, argv)])
        self.assertEqual(execs, [argv])

    def test_a_failing_precheck_stays_in_error_wait_and_says_why(self):
        answers = [(None, "the installed launcher's self-check failed (exit 1): SyntaxError: invalid syntax"),
                   ({"tools": "T", "core": "C"}, None)]

        def precheck(ctx, space, profile, argv=None):
            return answers.pop(0)
        wait = self.waits(enters=5)
        seen, execs, e = self.main_with(["pa-session"] + self.FLAGS, RuntimeError("bug"), precheck=precheck,
                                        error_wait=wait)
        self.assertIsInstance(e, Execd)
        self.assertEqual(len(seen), 2, seen)  # the first Enter was refused: error_wait again
        self.assertIn("self-check failed", seen[1][1])
        self.assertIn("SyntaxError", seen[1][1])
        self.assertIn("Enter", seen[1][1])
        self.assertEqual(len(execs), 1)  # only after the pre-check passed

    def test_d4_a_fix_that_does_not_compile_is_not_execd(self):  # VERIFY D4, the real pre-check
        write(os.path.join(pa.ROOT, "tools", "pa.py"), "def broken(:\n")
        self.stamp()  # installed.json matches: only the self-check can catch it
        wait = self.waits(enters=1)
        seen, execs, e = self.main_with(["pa-session"] + self.FLAGS, RuntimeError("bug"), precheck=pa.restart_precheck,
                                        error_wait=wait)
        self.assertEqual(execs, [])
        self.assertIsInstance(e, SystemExit)  # the Ctrl-C after the second wait, nothing else
        self.assertEqual(len(seen), 2, seen)
        self.assertIn("self-check", seen[1][1])

    def test_an_install_half_done_is_not_execd(self):
        real_tools()
        self.stamp()
        write(os.path.join(pa.ROOT, "tools", "pa_costs.py"), "y = 2\n")  # the files no longer match installed.json
        wait = self.waits(enters=1)
        seen, execs, e = self.main_with(["pa-session"] + self.FLAGS, RuntimeError("bug"), precheck=pa.restart_precheck,
                                        error_wait=wait)
        self.assertEqual(execs, [])
        self.assertEqual(len(seen), 2, seen)
        self.assertIn("installed.json", seen[1][1])


# ---- J3: pa-restart --end-terminal, the bridge for launchers from before J2 (spec R10, AC10.1; plan.md J3) --------

PA_ARGV = ["pa-session", "--space", SP, "--project", "/p", "--profile", "test", "--name", "proj"]


class EndTerminal(Command):
    """A launcher from before restart support (no launcher.json) in t1 with workers w1 (ledger) and w4 (child of
    t1); w3 is another space's. The fake hc ends t1 and its children on `hc kill t1`; nothing real is called."""

    def setUp(self):
        super().setUp()
        os.remove(os.path.join(self.dir, "launcher.json"))
        self.term["argv"] = list(PA_ARGV)
        self.kill_fails = None  # None | "live" | "finished"
        self.keeper = False
        self.at_kill = None
        self.asked = []
        self.answer = "yes"
        self.started = []
        self.app = "/usr/bin/herdr-app-fake"
        self.relayed = []
        self.relay_answer = (0, "")
        self.saved_j3 = {k: getattr(pa, k) for k in ("ask_yes", "interactive", "cmd_start", "herdr_app",
                                                     "KEEPER_WAIT", "END_WAIT", "END_POLL", "relay_output")}
        self.saved_kill = os.kill
        pa.KEEPER_WAIT, pa.END_WAIT, pa.END_POLL = 0.3, 0.3, 0.02

        def ask(prompt):
            self.asked.append(prompt)
            a = self.answer
            return a() if callable(a) else a

        pa.ask_yes = ask
        pa.interactive = lambda: True
        pa.cmd_start = lambda args: self.started.append(vars(args))
        pa.herdr_app = lambda: self.app

        def relay_output(argv):
            self.relayed.append(list(argv))
            return self.relay_answer

        pa.relay_output = relay_output
        pa.relay = lambda argv: self.relayed.append(list(argv)) or 0

        def fake_call(argv, check=True):
            self.calls.append(list(argv))
            if argv[:2] == ["hc", "kill"]:
                self.at_kill = (os.path.exists(os.path.join(self.dir, "session.log")),
                                os.path.exists(os.path.join(self.dir, "journal.jsonl")))
                if self.kill_fails != "live":
                    self.terms[argv[2]]["state"] = "finished"
                    for v in self.terms.values():
                        if v.get("parent") == argv[2]:
                            v["state"] = "finished"
                    if self.keeper:
                        self.terms["t7"] = {"address": "t7", "state": "live", "labels": {"herdr.pa": SP}, "pid": 900}
                        a = load(os.path.join(self.dir, "agent.json"))
                        a["terminal"] = "t7"
                        dump(a, os.path.join(self.dir, "agent.json"))
                        write(os.path.join(self.dir, "heartbeat"), "{} t7\n".format(int(time.time())))
                if self.kill_fails:
                    raise pa.Problem("hc kill failed: boom")
            return ""

        pa.call = fake_call

    def tearDown(self):
        for k, v in self.saved_j3.items():
            setattr(pa, k, v)
        os.kill = self.saved_kill
        super().tearDown()

    def run_end(self, **over):
        args = dict(end_terminal=True, anyway=False, confirmed_terminal=None, confirmed_workers=None,
                    relay_issue=False, relay_token=None)
        args.update(over)
        return self.run_cmd(**args)

    def kills(self):
        return [c for c in self.calls if "kill" in c or "rm" in c or "pane.close" in " ".join(c)]

    def nothing_written(self):
        for name in ("session.log", "journal.jsonl", "restart-request.json"):
            self.assertFalse(os.path.exists(os.path.join(self.dir, name)), name)

    def no_workers(self):
        for k in ("w1", "w4"):
            self.terms[k]["state"] = "finished"


class AC10_1_Identification(EndTerminal):
    def refused(self, part, code=2, **over):
        c, out, err = self.run_end(**over)
        self.assertEqual(c, code, out + err)
        self.assertIn(part, out + err)
        self.assertEqual(self.kills(), [])
        self.nothing_written()
        self.assertEqual(self.started, [])

    def test_plain_pa_restart_names_end_terminal(self):  # the R10 message
        code, out, err = self.run_cmd()
        self.assertEqual(code, 2)
        self.assertIn("started before restart support", err)
        self.assertIn("pa-restart --space {} --end-terminal".format(SP), err)
        self.assertIn("--anyway", err)
        self.assertIn("w1", err)
        self.assertEqual(self.kills(), [])

    def test_a_no_agent_json(self):
        os.remove(os.path.join(self.dir, "agent.json"))
        self.refused("no Project Agent")

    def test_a_closed(self):
        self.agent(closed=pa.now())
        self.refused("closed")

    def test_a_no_terminal(self):
        self.agent(terminal=None)
        self.refused("names no terminal")

    def test_c_no_heartbeat(self):
        os.remove(os.path.join(self.dir, "heartbeat"))
        self.refused("heartbeat")

    def test_c_stale_heartbeat(self):
        write(os.path.join(self.dir, "heartbeat"), "{} t1\n".format(int(time.time()) - 300))
        self.refused("heartbeat")

    def test_c_heartbeat_for_another_terminal(self):
        write(os.path.join(self.dir, "heartbeat"), "{} t5\n".format(int(time.time())))
        self.refused("heartbeat")

    def test_d_not_live(self):
        self.term["state"] = "finished"
        self.refused("live")

    def test_d_label_for_another_space(self):
        self.term["labels"] = {"herdr.pa": "sp9"}
        self.refused("label")

    def test_d_has_a_parent(self):
        self.term["parent"] = "t0"
        self.refused("parent")

    def test_e_a_shell(self):
        self.term["argv"] = ["/bin/zsh", "-l"]
        self.refused("not this space's pa-session")

    def test_e_another_space(self):
        self.term["argv"] = ["pa-session", "--space", "sp9", "--project", "/p"]
        self.refused("not this space's pa-session")

    def test_e_first_tab_ask_is_refused_and_named(self):
        # card d-161 'B: keep strict': a space's first tab started with `pa-session --ask` stays refused (no --space),
        # and the refusal says so plainly, with "close it by hand"
        self.term["argv"] = ["pa-session", "--ask"]
        c, out, err = self.run_end()
        self.assertEqual(c, 2, out + err)
        self.assertIn("first tab", err)
        self.assertIn("--ask", err)
        self.assertIn("close it by hand", err)
        self.assertEqual(self.kills(), [])
        self.nothing_written()

    def test_e_no_argv(self):
        self.term.pop("argv")
        self.refused("not this space's pa-session")

    def test_e_forms(self):
        ok = [PA_ARGV, ["/home/u/.local/bin/pa-session", "--space=" + SP], ["python3", "/x/pa.py", "session", "--space", SP],
              ["/usr/bin/python3.11", "/x/tools/pa.py", "session", "--profile", "t", "--space", "m1/" + SP]]
        bad = [["python3", "/x/pa.py", "restart", "--space", SP], ["env", "X=1", "pa-session", "--space", SP],
               ["pa-session", "--space"], ["pa-session", "--project", SP], ["pa-sessions", "--space", SP],
               ["python3", "/x/pa.py", "session", "--space", SP, "--space", "sp9"], [], "pa-session --space " + SP,
               ["pa-session", 3], None]
        for argv in ok:
            self.assertTrue(pa.pa_session_argv(argv, SP), argv)
        for argv in bad:
            self.assertFalse(pa.pa_session_argv(argv, SP), argv)

    def test_restart_capable_launcher_is_pointed_to_the_in_place_restart(self):
        dump(self.rec, os.path.join(self.dir, "launcher.json"))
        self.refused("takes an in-place restart")

    def test_restart_capable_after_a_failed_restart_says_why(self):
        dump(dict(self.rec, restart={"request": "r1", "outcome": "failed", "why": "self-check: boom"}),
             os.path.join(self.dir, "launcher.json"))
        c, out, err = self.run_end()
        self.assertEqual(c, 2)
        self.assertIn("self-check: boom", err)
        self.assertIn("same installed code", err)
        self.assertEqual(self.kills(), [])

    def test_a_j1_record_with_another_pid_is_refused(self):
        dump(dict(self.rec, version=1, accepts=None, pid=778), os.path.join(self.dir, "launcher.json"))
        self.refused("disagrees")

    def test_a_j1_record_for_this_launcher_is_allowed(self):  # spec S4
        dump(dict(self.rec, version=1, accepts=None), os.path.join(self.dir, "launcher.json"))
        c, out, err = self.run_end(dry_run=True)
        self.assertEqual(c, 0, err)
        self.assertIn("Ends:", out)

    def test_a_capable_record_for_another_terminal_is_not_this_launchers(self):
        dump(dict(self.rec, terminal="t2"), os.path.join(self.dir, "launcher.json"))
        c, out, err = self.run_end(dry_run=True)
        self.assertEqual(c, 0, err)
        self.assertIn("not this launcher", out)

    def test_inside_claude_refused(self):
        os.environ["CLAUDECODE"] = "1"
        self.refused("person's command", why="the person said so")
        self.refused("person's command", on="m2", why="the person said so")
        self.assertEqual(self.relayed, [])

    def test_now_or_fresh_refused(self):
        self.refused("--now", now=True)
        self.refused("--fresh", fresh=True)

    def test_from_the_terminal_itself_or_its_child(self):
        for address in ("t1", "w4"):
            os.environ["HC_TERMINAL_ID"] = address
            self.refused("another tab", anyway=True)
        os.environ.pop("HC_TERMINAL_ID")


class AC10_1_Asking(EndTerminal):
    def test_workers_without_anyway_refused_and_listed(self):
        c, out, err = self.run_end()
        self.assertEqual(c, 1, out + err)
        self.assertIn("--anyway", err)
        for name in ("w1 t-1-build", "w4 helper"):
            self.assertIn(name, out)
        self.assertNotIn("w3", out)
        self.assertEqual(self.asked, [])
        self.assertEqual(self.kills(), [])
        self.nothing_written()

    def test_not_a_tty_refused(self):
        pa.interactive = lambda: False
        c, out, err = self.run_end(anyway=True)
        self.assertEqual(c, 1)
        self.assertIn("type yes", err)
        self.assertEqual(self.asked, [])
        self.assertEqual(self.kills(), [])
        self.nothing_written()

    def test_anything_but_yes_ends_nothing(self):
        for answer in ("no", "", None, "yes please", "y"):
            with self.subTest(answer=answer):
                self.answer, self.asked = answer, []
                c, out, err = self.run_end(anyway=True)
                self.assertEqual(c, 1, out + err)
                self.assertIn("Not ended", out + err)
                self.assertEqual(len(self.asked), 1)
                self.assertEqual(self.kills(), [])
                self.nothing_written()

    def test_ask_yes_maps_eof_and_ctrl_c_to_none(self):
        pa.ask_yes = self.saved_j3["ask_yes"]
        import builtins
        saved = builtins.input
        try:
            for exc in (EOFError, KeyboardInterrupt):
                def raising(prompt="", exc=exc):
                    raise exc
                builtins.input = raising
                self.assertIsNone(pa.ask_yes("? "))
            builtins.input = lambda prompt="": " YES "
            self.assertEqual(pa.ask_yes("? "), " YES ")
        finally:
            builtins.input = saved

    def test_asked_even_without_workers(self):  # Q-J3-4
        self.no_workers()
        self.answer = "no"
        c, _out, _err = self.run_end()
        self.assertEqual(c, 1)
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(self.kills(), [])

    def test_summary_says_what_ends_and_what_stays_before_the_question(self):
        out_at_ask = []
        self.answer = lambda: out_at_ask.append(sys.stdout.getvalue()) or "no"
        self.run_end(anyway=True)
        text = out_at_ask[0]
        for part in ("Ends: terminal t1", "its Claude", SID, "w1 t-1-build", "w4 helper", "Keeps:", "state folder",
                     "worktrees", "Then:"):
            self.assertIn(part, text)
        self.assertIn("2 worker", self.asked[0])

    def test_yes_ends_exactly_that_terminal_once(self):
        def trap(*a):
            raise AssertionError("os.kill called: {}".format(a))

        os.kill = trap
        c, out, err = self.run_end(anyway=True, why="replace the old one")
        os.kill = self.saved_kill
        self.assertEqual(c, 0, out + err)
        self.assertEqual(self.kills(), [["hc", "kill", "t1"]])
        self.assertEqual(self.at_kill, (True, True), "session.log and journal lines come before the kill")
        with open(os.path.join(self.dir, "session.log")) as f:
            self.assertIn("end-terminal terminal=t1 workers=2 by=pa-restart", f.read())
        with open(os.path.join(self.dir, "journal.jsonl")) as f:
            entry = json.loads(f.read().splitlines()[-1])
        self.assertEqual((entry["by"], entry["trigger"], entry["why"], entry["terminal"], entry["workers"]),
                         ("person", "pa-restart --end-terminal", "replace the old one", "t1", ["w1", "w4"]))

    def test_changed_after_the_answer_refuses(self):
        def moved():
            write(os.path.join(self.dir, "heartbeat"), "{} t2\n".format(int(time.time())))
            return "yes"

        def new_worker():
            self.terms["w5"] = {"address": "w5", "state": "live", "parent": "t1", "name": "late"}
            return "yes"

        for answer in (moved, new_worker):
            with self.subTest(answer=answer.__name__):
                write(os.path.join(self.dir, "heartbeat"), "{} t1\n".format(int(time.time())))
                self.terms.pop("w5", None)
                self.answer = answer
                c, out, err = self.run_end(anyway=True)
                self.assertEqual(c, 1, out + err)
                self.assertIn("changed since you answered", err)
                self.assertEqual(self.kills(), [])
                self.nothing_written()


class AC10_1_After(EndTerminal):
    def setUp(self):
        super().setUp()
        self.no_workers()

    def test_kill_failing_ends_nothing_and_starts_nothing(self):
        self.kill_fails = "live"
        c, out, err = self.run_end()
        self.assertEqual(c, 1)
        self.assertIn("did not end terminal t1", err)
        self.assertIn("boom", err)
        self.assertEqual(self.started, [])

    def test_kill_error_but_ended_counts_as_ended(self):
        self.kill_fails = "finished"
        c, out, err = self.run_end()
        self.assertEqual(c, 0, out + err)
        self.assertIn("ended", out)
        self.assertEqual(len(self.started), 1)

    def test_keeper_restore_seen_no_pa_start(self):
        self.keeper = True
        pa.KEEPER_WAIT = 5
        c, out, err = self.run_end(json=True)
        self.assertEqual(c, 0, out + err)
        self.assertIn("Restored by the keeper in terminal t7", out)
        self.assertEqual(self.started, [])
        self.assertEqual(json.loads(out.strip().splitlines()[-1])["restoredBy"], "keeper")

    def test_no_keeper_runs_pa_start_once(self):
        c, out, err = self.run_end(json=True)
        self.assertEqual(c, 0, out + err)
        self.assertEqual(len(self.started), 1)
        s = self.started[0]
        self.assertEqual((s["space"], s["folder"], s["profile"], s["dry_run"]), (SP, self.project, "test", False))
        self.assertEqual(json.loads(out.strip().splitlines()[-1])["restoredBy"], "pa-start")

    def test_pa_start_failing_says_open(self):
        def boom(args):
            raise pa.Problem("herdr-app is not running")

        pa.cmd_start = boom
        c, out, err = self.run_end()
        self.assertEqual(c, 1)
        self.assertIn("herdr-app is not running", err)
        self.assertIn("Open Project Agent", err)

    def test_no_herdr_app_says_open(self):
        self.app = None
        c, out, err = self.run_end()
        self.assertEqual(c, 0, out + err)
        self.assertIn("panel's Open Project Agent", out)
        self.assertEqual(self.started, [])

    def test_dry_run_ends_and_writes_nothing(self):
        c, out, err = self.run_end(dry_run=True, json=True)
        self.assertEqual(c, 0, err)
        self.assertIn("Dry run: nothing ended", out)
        self.assertEqual(json.loads(out.strip().splitlines()[-1])["outcome"], "dry-run")
        self.assertEqual(self.asked, [])
        self.assertEqual(self.kills(), [])
        self.nothing_written()


class AC10_1_On(EndTerminal):
    def dry(self, workers):
        return (0, "Ends: terminal t1 ...\n" + json.dumps({"outcome": "dry-run", "terminal": "t1", "workers": workers,
                                                            "token": "tok123"}) + "\n")

    def test_on_asks_here_then_relays_bound_to_what_was_confirmed(self):
        self.relay_answer = self.dry(["w4", "w1"])
        empty = tempfile.mkdtemp(prefix="empty-", dir=TMP)
        saved_root, pa.ROOT = pa.ROOT, empty
        try:
            c, out, err = self.run_end(on="m2", anyway=True)
        finally:
            pa.ROOT = saved_root
        self.assertEqual(c, 0, out + err)
        self.assertEqual(os.listdir(empty), [])
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(len(self.relayed), 2)
        import shlex
        first, second = self.relayed
        self.assertEqual(first[:5], ["hc", "exec", "--on", "m2", "--timeout"])
        w1 = shlex.split(first[-1])
        self.assertEqual(w1[:3], ["$HOME/.config/herdr-pa/tools/pa.py", "restart", "--space"])
        for part in ("--end-terminal", "--dry-run", "--json", "--anyway", "--relay-issue"):
            self.assertIn(part, w1)
        w2 = shlex.split(second[-1])
        self.assertEqual(w2[w2.index("--relay-token") + 1], "tok123")
        self.assertNotIn("--dry-run", w2)
        self.assertEqual(w2[w2.index("--confirmed-terminal") + 1], "t1")
        self.assertEqual(w2[w2.index("--confirmed-workers") + 1], "w1,w4")
        self.assertEqual(self.calls, [])

    def test_on_workers_without_anyway_refused_here(self):
        self.relay_answer = self.dry(["w1"])
        c, out, err = self.run_end(on="m2")
        self.assertEqual(c, 1)
        self.assertEqual(len(self.relayed), 1)
        self.assertEqual(self.asked, [])

    def test_on_not_installed_there(self):
        self.relay_answer = (127, "")
        c, out, err = self.run_end(on="m2")
        self.assertEqual(c, 127)
        self.assertIn("pa-sync --on m2", err)

    def test_on_no_yes_no_second_relay(self):
        self.relay_answer = self.dry([])
        self.answer = "no"
        c, out, err = self.run_end(on="m2")
        self.assertEqual(c, 1)
        self.assertEqual(len(self.relayed), 1)

    def test_on_not_a_tty_here_refused(self):
        self.relay_answer = self.dry([])
        pa.interactive = lambda: False
        c, out, err = self.run_end(on="m2")
        self.assertEqual(c, 1)
        self.assertEqual(len(self.relayed), 1)

    def test_remote_side_bound_to_the_confirmed_set(self):
        pa.interactive = lambda: False

        def never():
            raise AssertionError("the bound form never asks")

        self.answer = never
        c, out, err = self.run_end(anyway=True, confirmed_terminal="t1", confirmed_workers="w1",
                                   relay_token=self.issue(anyway=True))
        self.assertEqual(c, 1, out + err)
        self.assertIn("changed since you answered", err)
        self.assertEqual(self.kills(), [])
        c, out, err = self.run_end(anyway=True, confirmed_terminal="t2", confirmed_workers="w1,w4",
                                   relay_token=self.issue(anyway=True))
        self.assertEqual(c, 1, out + err)
        self.assertEqual(self.kills(), [])
        c, out, err = self.run_end(anyway=True, confirmed_terminal="t1", confirmed_workers="w1,w4",
                                   relay_token=self.issue(anyway=True))
        self.assertEqual(c, 0, out + err)
        self.assertEqual(self.kills(), [["hc", "kill", "t1"]])

    def issue(self, **over):
        """The remote side's dry run as the relay asks for it: the one-time token it hands back (round 2)."""
        saved, pa.interactive = pa.interactive, lambda: False
        try:
            c, out, err = self.run_end(dry_run=True, json=True, relay_issue=True, **over)
        finally:
            pa.interactive = saved
        self.assertEqual(c, 0, out + err)
        return json.loads(out.strip().splitlines()[-1])["token"]

    def test_remote_side_bound_no_workers(self):
        self.no_workers()
        pa.interactive = lambda: False
        c, out, err = self.run_end(confirmed_terminal="t1", confirmed_workers="-", relay_token=self.issue())
        self.assertEqual(c, 0, out + err)
        self.assertEqual(self.kills(), [["hc", "kill", "t1"]])



class J3R2_KillUnknown(EndTerminal):
    """Round 2, REVIEW J3 M1 (plan J3.2 step 7, V6): when hc kill failed, the terminal counts as ended only when hc
    says its state is finished; an hc get that does not answer is not ended, and nothing new starts."""

    def setUp(self):
        super().setUp()
        self.no_workers()

    def kill_fails_then(self, answer):
        def call(argv, check=True):
            self.calls.append(list(argv))
            if argv[:2] == ["hc", "kill"]:
                pa.hc_get = lambda a: dict(answer)
                raise pa.Problem("hc kill failed: connection refused")
            return ""

        pa.call = call

    def test_kill_error_and_hc_get_silent_is_not_ended(self):
        self.kill_fails_then({})
        c, out, err = self.run_end(json=True)
        self.assertEqual(c, 1, out + err)
        self.assertNotIn("Terminal t1 ended", out)
        self.assertIn("nothing new was started", err)
        self.assertIn("connection refused", err)
        self.assertEqual(self.started, [])
        self.assertEqual(json.loads(out.strip().splitlines()[-1])["outcome"], "not-ended")
        with open(os.path.join(self.dir, "session.log")) as f:
            self.assertIn("end-terminal not-ended terminal=t1", f.read())

    def test_kill_error_and_no_state_is_not_ended(self):
        self.kill_fails_then({"address": "t1", "state": ""})
        c, out, err = self.run_end()
        self.assertEqual(c, 1, out + err)
        self.assertEqual(self.started, [])

    def test_kill_error_and_finished_is_ended(self):
        self.kill_fails_then({"address": "t1", "state": "finished"})
        c, out, err = self.run_end()
        self.assertEqual(c, 0, out + err)
        self.assertIn("Terminal t1 ended", out)
        self.assertEqual(len(self.started), 1)


class J3R2_AgentsRefused(EndTerminal):
    """Round 2, REVIEW J3 RL-J3-2: every agent marker the repo knows refuses --end-terminal (as pa-task does), also
    with --on (no relay) and with the bound form."""

    def test_each_agent_marker_refused(self):
        for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_SANDBOX"):
            os.environ[k] = "seatbelt" if k == "CODEX_SANDBOX" else "1"
            try:
                for over in ({}, {"dry_run": True}, {"on": "m2"},
                             {"anyway": True, "confirmed_terminal": "t1", "confirmed_workers": "w1,w4",
                              "relay_token": "x"}):
                    c, out, err = self.run_end(**over)
                    self.assertEqual(c, 2, (k, over, out + err))
                    self.assertIn("person's command", err, k)
            finally:
                os.environ.pop(k, None)
        self.assertEqual(self.relayed, [])
        self.assertEqual(self.kills(), [])
        self.assertEqual(self.asked, [])
        self.nothing_written()


class J3R2_RelayBound(EndTerminal):
    """Round 2, REVIEW J3 RL-J3-2: --confirmed-terminal/--confirmed-workers are the --on relay's remote side only. They
    need a one-time token that the relay's own dry run (--relay-issue) wrote in the space's folder, and a run with no
    terminal (hc exec: no tty, no HC_TERMINAL_ID). A direct local run never skips the typed yes."""

    TOKEN = "end-terminal-token.json"

    def setUp(self):
        super().setUp()
        pa.interactive = lambda: False

        def never(*a):
            raise AssertionError("the bound form never asks")

        self.answer = never

    def issue(self, **over):
        c, out, err = self.run_end(dry_run=True, json=True, relay_issue=True, **over)
        self.assertEqual(c, 0, out + err)
        return json.loads(out.strip().splitlines()[-1])["token"]

    def bound(self, token, **over):
        args = dict(anyway=True, confirmed_terminal="t1", confirmed_workers="w1,w4", relay_token=token)
        args.update(over)
        return self.run_end(**args)

    def test_confirmed_flags_without_a_token_refused(self):
        c, out, err = self.bound(None)
        self.assertNotEqual(c, 0, out + err)
        self.assertIn("relay", err)
        self.assertEqual(self.kills(), [])
        self.nothing_written()

    def test_confirmed_flags_with_a_made_up_token_refused(self):
        c, out, err = self.bound("tok123")
        self.assertNotEqual(c, 0, out + err)
        self.assertEqual(self.kills(), [])
        self.nothing_written()

    def test_relay_issue_writes_a_private_token_and_answers_it(self):
        token = self.issue(anyway=True)
        self.assertTrue(isinstance(token, str) and len(token) >= 32)
        path = os.path.join(self.dir, self.TOKEN)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        rec = load(path)
        self.assertEqual((rec["terminal"], rec["workers"]), ("t1", ["w1", "w4"]))
        self.assertEqual(self.kills(), [])

    def test_the_relays_cycle_ends_once_and_the_token_is_used_up(self):
        token = self.issue(anyway=True)
        c, out, err = self.bound(token)
        self.assertEqual(c, 0, out + err)
        self.assertEqual(self.kills(), [["hc", "kill", "t1"]])
        self.assertFalse(os.path.exists(os.path.join(self.dir, self.TOKEN)))

    def test_a_wrong_token_uses_it_up(self):
        token = self.issue(anyway=True)
        c, out, err = self.bound(token[::-1])
        self.assertNotEqual(c, 0, out + err)
        self.assertFalse(os.path.exists(os.path.join(self.dir, self.TOKEN)))
        c, out, err = self.bound(token)
        self.assertNotEqual(c, 0, out + err)
        self.assertEqual(self.kills(), [])

    def test_an_expired_token_refused(self):
        token = self.issue(anyway=True)
        path = os.path.join(self.dir, self.TOKEN)
        rec = load(path)
        rec["expires"] = time.time() - 1
        dump(rec, path)
        c, out, err = self.bound(token)
        self.assertNotEqual(c, 0, out + err)
        self.assertEqual(self.kills(), [])

    def test_from_a_terminal_refused_even_with_the_token(self):
        token = self.issue(anyway=True)
        pa.interactive = lambda: True
        c, out, err = self.bound(token)
        self.assertNotEqual(c, 0, out + err)
        self.assertEqual(self.kills(), [])
        pa.interactive = lambda: False
        token = self.issue(anyway=True)
        os.environ["HC_TERMINAL_ID"] = "t5"
        try:
            c, out, err = self.bound(token)
        finally:
            os.environ.pop("HC_TERMINAL_ID", None)
        self.assertNotEqual(c, 0, out + err)
        self.assertEqual(self.kills(), [])

    def test_relay_issue_from_a_terminal_refused(self):
        pa.interactive = lambda: True
        c, out, err = self.run_end(dry_run=True, json=True, relay_issue=True, anyway=True)
        self.assertNotEqual(c, 0, out + err)
        self.assertFalse(os.path.exists(os.path.join(self.dir, self.TOKEN)))
        pa.interactive = lambda: False
        os.environ["HC_TERMINAL_ID"] = "t5"
        try:
            c, out, err = self.run_end(dry_run=True, json=True, relay_issue=True, anyway=True)
        finally:
            os.environ.pop("HC_TERMINAL_ID", None)
        self.assertNotEqual(c, 0, out + err)
        self.assertFalse(os.path.exists(os.path.join(self.dir, self.TOKEN)))

    def test_relay_issue_needs_a_dry_run(self):
        c, out, err = self.run_end(relay_issue=True, anyway=True)
        self.assertNotEqual(c, 0, out + err)
        self.assertEqual(self.kills(), [])

    def test_on_side_needs_the_token_from_the_dry_run(self):
        pa.interactive = lambda: True
        self.answer = "yes"
        self.relay_answer = (0, json.dumps({"outcome": "dry-run", "terminal": "t1", "workers": []}) + "\n")
        c, out, err = self.run_end(on="m2")
        self.assertNotEqual(c, 0, out + err)
        self.assertEqual(len(self.relayed), 1)
        self.assertEqual(self.asked, [])


class J3R2_Main(Base):
    def test_main_knows_the_relay_flags(self):
        seen = {}
        saved = (sys.argv, pa.cmd_restart)
        sys.argv = ["pa-restart", "--space", SP, "--end-terminal", "--dry-run", "--relay-issue"]
        pa.cmd_restart = lambda args: seen.update(vars(args))
        try:
            pa.main()
            self.assertEqual((seen["relay_issue"], seen["relay_token"]), (True, None))
            sys.argv = ["pa-restart", "--space", SP, "--end-terminal", "--relay-token", "abc",
                        "--confirmed-terminal", "t1", "--confirmed-workers", "-"]
            pa.main()
            self.assertEqual((seen["relay_issue"], seen["relay_token"]), (False, "abc"))
        finally:
            sys.argv, pa.cmd_restart = saved


class J3Main(Base):
    def test_main_knows_end_terminal(self):
        seen = {}
        saved = (sys.argv, pa.cmd_restart)
        sys.argv = ["pa-restart", "--space", SP, "--end-terminal", "--anyway", "--confirmed-terminal", "t1",
                    "--confirmed-workers", "-"]
        pa.cmd_restart = lambda args: seen.update(vars(args))
        try:
            pa.main()
        finally:
            sys.argv, pa.cmd_restart = saved
        self.assertEqual((seen["end_terminal"], seen["anyway"], seen["confirmed_terminal"], seen["confirmed_workers"]),
                         (True, True, "t1", "-"))


if __name__ == "__main__":
    unittest.main()
