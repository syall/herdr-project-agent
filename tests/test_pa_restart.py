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


if __name__ == "__main__":
    unittest.main()
