"""t-037 J1: the stamp. installed.json (pa-stamp, install.sh, pa-sync), launcher.json (written by the launcher),
and "older code" (pa-peers, pa-doctor). Spec R1, R2, R3 (AC1.1-AC1.3, AC2.1, AC2.2, AC3.1).

Run: python3 -m unittest -v tests/test_pa_stamp.py
Temp homes only: HERDR_PA_HOME, HERDR_APP_HOME and CLAUDE_CONFIG_DIR point at a temp folder before pa.py is loaded;
hc is never called (pa.call, hc_get and live_terminals are replaced). install.sh runs with HOME in the temp folder.
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
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import child_fakes  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TMP = tempfile.mkdtemp(prefix="t037-stamp-")
os.environ["HERDR_PA_HOME"] = os.path.join(TMP, "pa")
os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(TMP, "claude")
os.environ["HERDR_APP_HOME"] = os.path.join(TMP, "app")
os.environ.pop("HC_TERMINAL_ID", None)
spec = importlib.util.spec_from_file_location("pa", os.path.join(REPO, "runtime", "tools", "pa.py"))
pa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pa)
assert pa.ROOT.startswith(TMP) and pa.APP.startswith(TMP), "pa.py must use the temp homes"

SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def write(path, text, mode="w"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode) as f:
        f.write(text)


def load(path):
    with open(path) as f:
        return json.load(f)


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


class Base(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(pa.ROOT, ignore_errors=True)
        shutil.rmtree(pa.APP, ignore_errors=True)
        for rel, text in (("tools/pa.py", "print('pa')\n"), ("tools/pa_costs.py", "x = 1\n"),
                          ("core/orchestrator.md", "# o\n"), ("core/hooks/a.sh", "#!/bin/sh\n"),
                          ("profiles/test/profile.json", json.dumps({"name": "test", "claude": ["true"], "codex": ["true"]})),
                          ("profiles/test/context.md", "ctx\n")):
            write(os.path.join(pa.ROOT, rel), text)
        self.saved = {k: getattr(pa, k) for k in ("live_terminals", "hc_get", "call", "init_state", "__file__")}
        self.calls = []

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(pa, k, v)

    def stamp(self, *argv):
        return quiet(pa.cmd_stamp, argparse.Namespace(**dict({"commit": None, "dirty": False, "synced_from": None}, **dict(argv))))


# ---- R1: installed.json ---------------------------------------------------------------------------------------

class R1_Stamp(Base):
    def test_ac1_1_stamp_fields_equal_tree_hash(self):
        code, _out, err = self.stamp(("commit", "abc123"), ("dirty", True))
        self.assertEqual(code, 0, err)
        s = load(os.path.join(pa.ROOT, "installed.json"))
        self.assertEqual(s["version"], 1)
        self.assertEqual(s["by"], "install.sh")
        self.assertEqual(s["source"], {"commit": "abc123", "dirty": True})
        self.assertEqual(s["tools"], pa.tree_hash(os.path.join(pa.ROOT, "tools"), top_py_only=True))
        self.assertEqual(s["core"], pa.tree_hash(os.path.join(pa.ROOT, "core")))
        self.assertIsNone(s["mods"])  # no app mods folder on this machine
        self.assertIsNone(s["syncedFrom"])
        self.assertTrue(pa.parse_time(s["at"]))

    def test_ac1_1_no_commit_means_no_source_and_mods_hashed_when_present(self):
        write(os.path.join(pa.APP, "mods", "project-agent", "main.js"), "x\n")
        self.stamp(("synced_from", "mac"))
        s = load(os.path.join(pa.ROOT, "installed.json"))
        self.assertIsNone(s["source"])
        self.assertEqual(s["by"], "pa-sync")
        self.assertEqual(s["syncedFrom"], "mac")
        self.assertEqual(s["mods"], pa.tree_hash(os.path.join(pa.APP, "mods", "project-agent")))

    def test_ac1_2_one_byte_changes_tools_bak_and_pyc_do_not(self):
        tools = os.path.join(pa.ROOT, "tools")
        first = pa.tree_hash(tools, top_py_only=True)
        write(os.path.join(tools, "pa.py.bak-x"), "old\n")
        write(os.path.join(tools, "__pycache__", "pa.cpython-39.pyc"), "bytes", "w")
        write(os.path.join(tools, "pa.pyc"), "bytes")
        write(os.path.join(tools, ".hidden.py"), "x\n")
        self.assertEqual(pa.tree_hash(tools, top_py_only=True), first)
        write(os.path.join(tools, "pa_costs.py"), "x = 2\n")
        self.assertNotEqual(pa.tree_hash(tools, top_py_only=True), first)
        core = os.path.join(pa.ROOT, "core")
        c1 = pa.tree_hash(core)
        write(os.path.join(core, "worker.md.bak-t1"), "old\n")
        self.assertEqual(pa.tree_hash(core), c1)
        write(os.path.join(core, "hooks", "a.sh"), "#!/bin/sh\n#\n")
        self.assertNotEqual(pa.tree_hash(core), c1)

    def test_ac1_2_a_rename_changes_the_hash(self):
        tools = os.path.join(pa.ROOT, "tools")
        first = pa.tree_hash(tools, top_py_only=True)
        os.rename(os.path.join(tools, "pa_costs.py"), os.path.join(tools, "pa_other.py"))
        self.assertNotEqual(pa.tree_hash(tools, top_py_only=True), first)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads anything")
    def test_ac1_3_unreadable_keeps_the_previous_stamp(self):
        self.stamp()
        path = os.path.join(pa.ROOT, "installed.json")
        before = open(path).read()
        bad = os.path.join(pa.ROOT, "tools", "pa_costs.py")
        os.chmod(bad, 0)
        try:
            code, _out, err = self.stamp(("commit", "new"))
        finally:
            os.chmod(bad, 0o644)
        self.assertNotEqual(code, 0)
        self.assertEqual(open(path).read(), before)

    def test_ac1_3_missing_core_refuses(self):
        self.stamp()
        path = os.path.join(pa.ROOT, "installed.json")
        before = open(path).read()
        shutil.rmtree(os.path.join(pa.ROOT, "core"))
        code, _out, _err = self.stamp(("commit", "new"))
        self.assertNotEqual(code, 0)
        self.assertEqual(open(path).read(), before)

    def test_main_knows_pa_stamp(self):
        saved = sys.argv
        sys.argv = ["pa-stamp", "--commit", "c1", "--dirty"]
        try:
            quiet(pa.main)
        finally:
            sys.argv = saved
        self.assertEqual(load(os.path.join(pa.ROOT, "installed.json"))["source"], {"commit": "c1", "dirty": True})


class R1_Sync(Base):
    def test_sync_stamps_the_other_machine(self):
        self.stamp(("commit", "c0ffee"))
        write(os.path.join(pa.ROOT, "config.json"), json.dumps({"version": 1, "defaultProfile": "test"}))
        bin_ = os.path.join(pa.HOME, ".local", "bin")
        calls = []
        pa.call = lambda argv, check=True: calls.append(argv) or ""
        saved_run, saved_home = pa.subprocess.run, pa.HOME
        fake_home = os.path.join(TMP, "home")
        os.makedirs(os.path.join(fake_home, ".local", "bin"), exist_ok=True)
        pa.HOME = fake_home
        pa.subprocess.run = lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        try:
            code, out, err = quiet(pa.cmd_sync, argparse.Namespace(on="m", dry_run=False))
        finally:
            pa.subprocess.run, pa.HOME = saved_run, saved_home
        self.assertEqual(code, 0, err)
        stamps = [c for c in calls if "stamp" in " ".join(c)]
        self.assertEqual(len(stamps), 1, calls)
        c = stamps[0]
        self.assertEqual(c[:5], ["hc", "exec", "--on", "m", "--"])
        script = " ".join(c[5:])
        self.assertIn('"$HOME/.config/herdr-pa/tools/pa.py" stamp', script)
        self.assertIn("--synced-from", script)
        self.assertIn("--commit c0ffee", script)
        self.assertNotIn("--dirty", script)
        # the stamp is written after the files are copied and linked
        self.assertEqual(calls.index(c), len(calls) - 1)
        del bin_

    def test_sync_dry_run_calls_no_hc(self):
        calls = []
        pa.call = lambda argv, check=True: calls.append(argv) or ""
        code, out, _err = quiet(pa.cmd_sync, argparse.Namespace(on="m", dry_run=True))
        self.assertEqual(calls, [])
        self.assertIn("would stamp", out)


class R1_Install(unittest.TestCase):
    """install.sh in a scratch home: links pa-stamp and writes installed.json last."""

    def test_install_writes_the_stamp_last(self):
        home = tempfile.mkdtemp(prefix="home-", dir=TMP)
        env = {k: v for k, v in os.environ.items() if not k.startswith("HC_") and k != "CLAUDECODE"}
        env.update(HOME=home, HERDR_PA_HOME=os.path.join(home, ".config", "herdr-pa"),
                   HERDR_APP_HOME=os.path.join(home, ".config", "herdr-app"))
        os.makedirs(os.path.join(env["HERDR_APP_HOME"], "mods"))
        r = subprocess.run(["sh", os.path.join(REPO, "install.sh")], env=env, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, universal_newlines=True)
        self.assertEqual(r.returncode, 0, r.stdout[-2000:])
        root = env["HERDR_PA_HOME"]
        path = os.path.join(root, "installed.json")
        s = load(path)
        self.assertEqual(s["by"], "install.sh")
        self.assertEqual(s["tools"], pa.tree_hash(os.path.join(root, "tools"), top_py_only=True))
        self.assertEqual(s["core"], pa.tree_hash(os.path.join(root, "core")))
        self.assertEqual(s["mods"], pa.tree_hash(os.path.join(env["HERDR_APP_HOME"], "mods", "project-agent")))
        head = subprocess.run(["git", "-C", REPO, "rev-parse", "HEAD"], stdout=subprocess.PIPE, universal_newlines=True)
        if head.returncode == 0:
            self.assertEqual((s["source"] or {}).get("commit"), head.stdout.strip())
            self.assertIsInstance(s["source"]["dirty"], bool)
        stamp_at = os.path.getmtime(path)
        for top in (root, env["HERDR_APP_HOME"]):
            for folder, _d, names in os.walk(top):
                for n in names:
                    p = os.path.join(folder, n)
                    if p != path:
                        self.assertLessEqual(os.path.getmtime(p), stamp_at + 1e-6, p)
        link = os.path.join(home, ".local", "bin", "pa-stamp")
        self.assertTrue(os.path.islink(link))
        # t-147: linked to pa_launch.py, which runs pa.py for it (test_pa_launch L1)
        self.assertEqual(os.path.realpath(link), os.path.realpath(os.path.join(root, "tools", "pa_launch.py")))
        self.assertIn("pa-stamp", r.stdout + open(os.path.join(REPO, "install.sh")).read())

    def test_install_check_writes_no_stamp(self):
        home = tempfile.mkdtemp(prefix="home-", dir=TMP)
        env = {k: v for k, v in os.environ.items() if not k.startswith("HC_") and k != "CLAUDECODE"}
        env.update(HOME=home, HERDR_PA_HOME=os.path.join(home, "pa"), HERDR_APP_HOME=os.path.join(home, "app"))
        subprocess.run(["sh", os.path.join(REPO, "install.sh"), "--check"], env=env, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT)
        self.assertFalse(os.path.exists(os.path.join(home, "pa", "installed.json")))


# ---- R2: launcher.json ----------------------------------------------------------------------------------------

class R2_Launcher(Base):
    def fixtures(self):
        self.dir = os.path.join(pa.ROOT, "spaces", "sp1")
        os.makedirs(self.dir, exist_ok=True)
        self.project = os.path.join(TMP, "proj")
        os.makedirs(self.project, exist_ok=True)
        with open(os.path.join(self.dir, "agent.json"), "w") as f:
            json.dump({"version": 1, "space": "sp1", "project": self.project, "profile": "test", "name": "proj",
                       "terminal": "t9", "sessionId": SID, "sessionStarted": True}, f)
        pa.hc_get = lambda address: {}
        pa.live_terminals = lambda: {}
        pa.call = lambda argv, check=True: self.calls.append(argv) or ""

    def launch(self, starts=1, ctx=None):
        """run_session until the `starts`-th Claude start, which raises Stop. Returns the argv lists."""
        started = []

        class Stop(Exception):
            pass

        class NoThread:
            def __init__(self, target=None, daemon=None):
                pass

            def start(self):
                pass

        def fake_call(argv, cwd=None):
            started.append(load(os.path.join(self.dir, "launcher.json")) if os.path.exists(os.path.join(self.dir, "launcher.json")) else None)
            if len(started) >= starts:
                raise Stop()
            return 0
        saved = (pa.threading.Thread, pa.subprocess.call, signal.getsignal(signal.SIGINT), time.sleep)
        pa.threading.Thread, pa.subprocess.call = NoThread, fake_call
        undo_child = child_fakes.patch(pa, fake_call)  # t-037 J2: the launcher starts Claude with pa.spawn
        time.sleep = lambda s: None  # the 3 s pause after an exit
        try:
            args = types.SimpleNamespace(space="sp1", project=self.project, profile="test", name=None, auto=False, ask=False, fresh=False)
            with self.assertRaises(Stop), contextlib.redirect_stdout(io.StringIO()) as out:
                pa.run_session(args, "t1", {}, "sp1", self.project, ctx if ctx is not None else {"beat": False})
        finally:
            undo_child()
            pa.threading.Thread, pa.subprocess.call, time.sleep = saved[0], saved[1], saved[3]
            signal.signal(signal.SIGINT, saved[2])
        self.out = out.getvalue()
        return started

    def test_ac2_1_fields(self):
        self.fixtures()
        quiet(pa.cmd_stamp, argparse.Namespace(commit="c1", dirty=False, synced_from=None))
        # the running pa.py is the installed one
        pa.__file__ = os.path.join(pa.ROOT, "tools", "pa.py")
        seen = self.launch()
        rec = seen[0]
        self.assertIsNotNone(rec, "launcher.json is written before Claude starts")
        self.assertEqual(rec["version"], 2)  # t-037 J2, spec S4 (card d-108): version 2 takes restart requests
        self.assertEqual(rec["pid"], os.getpid())
        self.assertEqual(rec["terminal"], "t1")
        self.assertEqual(rec["space"], "sp1")
        self.assertTrue(pa.parse_time(rec["started"]))
        import uuid
        uuid.UUID(rec["instance"])
        tools = pa.tree_hash(os.path.join(pa.ROOT, "tools"), top_py_only=True)
        self.assertEqual(rec["code"], {"tools": tools, "file": os.path.realpath(os.path.join(pa.ROOT, "tools", "pa.py")),
                                       "installed": tools, "commit": "c1"})
        prof = os.path.join(pa.ROOT, "profiles", "test")
        self.assertEqual(rec["profile"]["name"], "test")
        self.assertEqual(rec["profile"]["mtime"], os.path.getmtime(os.path.join(prof, "profile.json")))
        self.assertEqual(rec["profile"]["hash"], pa.tree_hash(prof))
        self.assertTrue(pa.parse_time(rec["profile"]["readAt"]))
        s = rec["session"]
        self.assertEqual(s["id"], SID)
        self.assertIs(s["resume"], True)
        self.assertEqual(s["core"], pa.tree_hash(os.path.join(pa.ROOT, "core")))
        self.assertEqual(s["context"], os.path.getmtime(os.path.join(prof, "context.md")))
        self.assertTrue(pa.parse_time(s["at"]))
        self.assertIsNone(rec["restart"])
        self.assertIsNone(rec["restartedFrom"])
        log = open(os.path.join(self.dir, "session.log")).read().splitlines()
        lines = [l for l in log if l.split()[1:2] == ["launcher"]]
        self.assertEqual(len(lines), 1, log)
        self.assertIn("instance=" + rec["instance"], lines[0])
        self.assertIn("tools=" + tools[:12], lines[0])
        self.assertIn("core=" + s["core"][:12], lines[0])
        self.assertIn("profile=test@", lines[0])

    def test_ac2_1_second_start_updates_session_keeps_instance(self):
        self.fixtures()
        seen = self.launch(starts=2)
        first, second = seen
        self.assertEqual(first["instance"], second["instance"])
        write(os.path.join(pa.ROOT, "core", "orchestrator.md"), "# changed\n")
        last = load(os.path.join(self.dir, "launcher.json"))
        self.assertEqual(last["instance"], first["instance"])
        self.assertGreaterEqual(pa.parse_time(last["session"]["at"]), pa.parse_time(first["session"]["at"]))
        log = open(os.path.join(self.dir, "session.log")).read().splitlines()
        self.assertEqual(len([l for l in log if l.split()[1:2] == ["launcher"]]), 1, "one launcher line per launcher start")

    def test_ac2_1_session_core_is_read_at_each_start(self):
        self.fixtures()
        ctx = {"beat": False}
        self.launch(ctx=ctx)
        old = load(os.path.join(self.dir, "launcher.json"))
        write(os.path.join(pa.ROOT, "core", "orchestrator.md"), "# changed\n")
        agent = load(os.path.join(self.dir, "agent.json"))
        pa.launcher_session(ctx, self.dir, agent, True)
        new = load(os.path.join(self.dir, "launcher.json"))
        self.assertEqual(new["instance"], old["instance"])
        self.assertNotEqual(new["session"]["core"], old["session"]["core"])
        self.assertEqual(new["session"]["core"], pa.tree_hash(os.path.join(pa.ROOT, "core")))

    def test_ac2_1_a_retry_in_the_same_process_keeps_the_instance(self):
        self.fixtures()
        ctx = {"beat": False}
        self.launch(ctx=ctx)
        first = load(os.path.join(self.dir, "launcher.json"))["instance"]
        self.launch(ctx=ctx)
        self.assertEqual(load(os.path.join(self.dir, "launcher.json"))["instance"], first)
        # a new process (a fresh ctx) is a new instance
        self.launch(ctx={"beat": False})
        self.assertNotEqual(load(os.path.join(self.dir, "launcher.json"))["instance"], first)

    def test_ac2_1_no_installed_json_and_a_checkout_launcher(self):
        self.fixtures()
        seen = self.launch()
        rec = seen[0]
        self.assertIsNone(rec["code"]["installed"])
        self.assertIsNone(rec["code"]["commit"])
        self.assertEqual(rec["code"]["file"], os.path.realpath(pa.__file__))
        self.assertEqual(rec["code"]["tools"], pa.tree_hash(os.path.dirname(os.path.realpath(pa.__file__)), top_py_only=True))

    def test_ac2_2_a_failed_write_never_stops_the_launch(self):
        self.fixtures()
        saved = pa.write_launcher

        def broken(*a, **k):
            raise OSError("disk full")
        pa.write_launcher = broken
        try:
            seen = self.launch()  # Claude still starts
        finally:
            pa.write_launcher = saved
        self.assertEqual(len(seen), 1)
        self.assertIn("launcher.json not written", self.out)
        self.assertIn("launcher.json not written", open(os.path.join(self.dir, "session.log")).read())

    def test_ac2_2_unreadable_core_does_not_stop_the_launch(self):
        self.fixtures()
        shutil.rmtree(os.path.join(pa.ROOT, "core"))
        seen = self.launch()
        self.assertEqual(len(seen), 1)
        self.assertIsNone(seen[0]["session"]["core"])

    def test_run_agent_alone_writes_no_launcher_json(self):
        # run_agent called without run_session (older tests, t-009) has no launcher record to update.
        self.fixtures()
        p = pa.load_profile("test")
        agent = load(os.path.join(self.dir, "agent.json"))

        class Stop(Exception):
            pass

        def fake_call(argv, cwd=None):
            raise Stop()
        saved = pa.subprocess.call
        pa.subprocess.call = fake_call
        undo_child = child_fakes.patch(pa, fake_call)  # t-037 J2: the launcher starts Claude with pa.spawn
        try:
            with self.assertRaises(Stop):
                pa.run_agent(types.SimpleNamespace(fresh=False), "t1", self.project, p, self.dir, agent,
                             {"beat": True, "previous": None}, time.time())
        finally:
            undo_child()
            pa.subprocess.call = saved
        self.assertFalse(os.path.exists(os.path.join(self.dir, "launcher.json")))


# ---- R3: older code -------------------------------------------------------------------------------------------

class R3_Older(Base):
    def rec(self, tools="T", core="C", terminal="t1"):
        return {"version": 1, "terminal": terminal, "code": {"tools": tools}, "session": {"core": core}}

    def test_ac3_1_table(self):
        inst = {"tools": "T", "core": "C"}
        self.assertEqual(pa.code_state(self.rec(), inst, "t1")[0], "current")
        self.assertEqual(pa.code_state(self.rec(tools="X"), inst, "t1")[0], "older (tools)")
        self.assertEqual(pa.code_state(self.rec(core="X"), inst, "t1")[0], "older (core)")
        self.assertEqual(pa.code_state(self.rec(tools="X", core="Y"), inst, "t1")[0], "older (tools, core)")
        word, detail = pa.code_state(None, inst, "t1")
        self.assertEqual(word, "unknown")
        self.assertIn("started before restart support", detail)
        word, detail = pa.code_state(self.rec(), None, "t1")
        self.assertEqual(word, "unknown")
        self.assertIn("installed.json", detail)
        word, detail = pa.code_state(self.rec(terminal="t2"), inst, "t1")
        self.assertEqual(word, "unknown")
        self.assertIn("another terminal", detail)
        # a launcher whose session has no core hash yet (or could not read it) is not "current"
        self.assertEqual(pa.code_state(self.rec(core=None), inst, "t1")[0], "unknown")

    def peers_fixture(self):
        for sp, term in (("sp1", "t1"), ("sp2", "t2"), ("sp3", "t3")):
            os.makedirs(os.path.join(pa.ROOT, "spaces", sp), exist_ok=True)
        with open(os.path.join(pa.ROOT, "registry.json"), "w") as f:
            json.dump({"agents": [{"space": s, "terminal": t, "address": t, "name": s, "project": "/p/" + s, "profile": "test"}
                                  for s, t in (("sp1", "t1"), ("sp2", "t2"), ("sp3", "t3"))]}, f)
        with open(os.path.join(pa.ROOT, "installed.json"), "w") as f:
            json.dump({"version": 1, "tools": "T", "core": "C"}, f)
        for sp, rec in (("sp1", self.rec()), ("sp2", self.rec(tools="OLD", terminal="t2"))):
            with open(os.path.join(pa.ROOT, "spaces", sp, "launcher.json"), "w") as f:
                json.dump(rec, f)
        pa.live_terminals = lambda: {t: {"state": "live", "labels": {"herdr.pa": s}} for s, t in (("sp1", "t1"), ("sp2", "t2"), ("sp3", "t3"))}

    def test_peers_column_json_and_older(self):
        self.peers_fixture()
        code, out, _ = quiet(pa.cmd_peers, argparse.Namespace(json=True, all=False, older=False))
        rows = {r["space"]: r for r in json.loads(out)}
        self.assertEqual(rows["sp1"]["code"], "current")
        self.assertEqual(rows["sp2"]["code"], "older (tools)")
        self.assertEqual(rows["sp3"]["code"], "unknown")
        self.assertIn("started before restart support", rows["sp3"]["codeNote"])
        code, out, _ = quiet(pa.cmd_peers, argparse.Namespace(json=True, all=False, older=True))
        self.assertEqual(sorted(r["space"] for r in json.loads(out)), ["sp2", "sp3"])
        code, out, _ = quiet(pa.cmd_peers, argparse.Namespace(json=False, all=False, older=False))
        line = [l for l in out.splitlines() if "sp2" in l][0]
        self.assertIn("older (tools)", line)
        self.assertIn("current", [l for l in out.splitlines() if "sp1" in l][0])

    def test_peers_older_none(self):
        self.peers_fixture()
        os.remove(os.path.join(pa.ROOT, "spaces", "sp2", "launcher.json"))
        pa.live_terminals = lambda: {"t1": {"state": "live", "labels": {"herdr.pa": "sp1"}}}
        code, out, _ = quiet(pa.cmd_peers, argparse.Namespace(json=False, all=False, older=True))
        self.assertIn("No Project Agent", out)
        self.assertIn("older", out)

    def test_main_knows_peers_older(self):
        self.peers_fixture()
        saved = sys.argv
        sys.argv = ["pa-peers", "--older", "--json"]
        try:
            _c, out, _e = quiet(pa.main)
        finally:
            sys.argv = saved
        self.assertEqual(sorted(r["space"] for r in json.loads(out)), ["sp2", "sp3"])

    def test_doctor_note(self):
        self.peers_fixture()
        d = os.path.join(pa.ROOT, "spaces", "sp2")
        with open(os.path.join(d, "agent.json"), "w") as f:
            json.dump({"version": 1, "space": "sp2", "name": "sp2", "profile": "test", "terminal": "t2"}, f)
        with open(os.path.join(d, "ledger.json"), "w") as f:
            json.dump({"version": 1, "tasks": []}, f)
        pa.hc_get = lambda a: {"state": "live", "labels": {"herdr.pa": "sp2"}} if a == "t2" else {}
        _c, out, _e = quiet(pa.cmd_doctor, argparse.Namespace(space="sp2"))
        self.assertIn("code: older (tools)", out)


class NoEnding(unittest.TestCase):
    def test_v9_no_new_process_ending(self):
        with open(os.path.join(REPO, "runtime", "tools", "pa.py")) as f:
            text = f.read()
        # t-193: the one os.kill is signal_pid's (end_leftovers' and pa-restart --recover's steps, end_pids, call it)
        import ast as _ast
        _tree = _ast.parse(text)
        _kills = {fn.name for fn in _ast.walk(_tree) if isinstance(fn, _ast.FunctionDef) for n in _ast.walk(fn)
                  if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Attribute) and n.func.attr == "kill"
                  and getattr(n.func.value, "id", None) == "os"}
        # t-195 R2.7: and AdoptedChild.send_signal's (the adopted Claude, only while not reaped; end_child calls it)
        self.assertEqual(_kills, {"signal_pid", "send_signal"}, "os.kill outside signal_pid and AdoptedChild.send_signal")
        self.assertEqual(text.count("os.kill("), 2)
        _adopted = [c for c in _ast.walk(_tree) if isinstance(c, _ast.ClassDef) and c.name == "AdoptedChild"]
        self.assertEqual(len(_adopted), 1)
        self.assertIn("send_signal", {n.name for n in _ast.walk(_adopted[0]) if isinstance(n, _ast.FunctionDef)
                                      for k in _ast.walk(n) if isinstance(k, _ast.Call) and getattr(k.func, "attr", None) == "kill"
                                      and getattr(k.func.value, "id", None) == "os"})
        _callers = {fn.name for fn in _ast.walk(_tree) if isinstance(fn, _ast.FunctionDef) for n in _ast.walk(fn)
                    if isinstance(n, _ast.Call) and getattr(n.func, "id", None) == "signal_pid"}
        self.assertEqual(_callers, {"end_pids", "send"}, "signal_pid called outside end_pids (its send step)")
        self.assertEqual(text.count('"kill"'), 2, "hc kill only in cmd_cleanup (today's) and end_terminal (t-037 J3, R10)")


class J4_CodeStateParity(unittest.TestCase):
    """t-037 J4 (R14): tests/samples/code-state-cases.json is the one table the panel's store.codeState and
    pa.code_state are both checked against (tests/store/run.mjs S37-code-parity reads the same file)."""

    def test_every_row(self):
        with open(os.path.join(HERE, "samples", "code-state-cases.json")) as f:
            rows = json.load(f)
        self.assertGreaterEqual(len(rows), 15)
        for row in rows:
            word, _detail = pa.code_state(row["launcher"], row["installed"], row["terminal"])
            self.assertEqual(word, row["word"], row["name"])
            self.assertEqual(pa.restart_capable(row["launcher"]), row["capable"], row["name"])


if __name__ == "__main__":
    unittest.main()
