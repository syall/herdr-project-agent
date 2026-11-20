"""t-193 R5: `pa-restart --recover` brings back a Project Agent whose launcher is not running (a stale heartbeat).

It ends the space's stopped launchers (their groups and the other processes on their terminals, never the tab's shell)
and the orphaned Claudes of its sessions, then the panel files its open request. It refuses a launcher that runs while
its heartbeat is stale, and anything that only mentions the space id (spec.md R5.1, R5.2, H11-H19).

Run: python3 -m unittest -v tests/test_pa_recover.py
Temp homes only (HERDR_PA_HOME, HERDR_APP_HOME, CLAUDE_CONFIG_DIR set before pa.py is loaded); no hc. Every process
signalled is one these tests started (python stand-ins in sessions of their own).
"""
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
PA_PY = os.path.join(REPO, "runtime", "tools", "pa.py")
TMP = tempfile.mkdtemp(prefix="t193-rec-")
os.environ["HERDR_PA_HOME"] = os.path.join(TMP, "pa")
os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(TMP, "claude")
os.environ["HERDR_APP_HOME"] = os.path.join(TMP, "app")
for k in ("HC_TERMINAL_ID", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_SANDBOX"):
    os.environ.pop(k, None)
spec = importlib.util.spec_from_file_location("pa", PA_PY)
pa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pa)
assert pa.ROOT.startswith(TMP) and pa.CLAUDE_HOME.startswith(TMP), "pa.py must use the temp homes"

SP = "sp1deab" + __import__("uuid").uuid4().hex[:10]  # one per run: a stand-in left by another run never matches
CUR = "abcdefab-cdef-4abc-8def-abcdefabcdef"
PREV = "fedcbafe-dcba-4fed-8cba-fedcbafedcba"


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, universal_newlines=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


def state(pid):
    return subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          universal_newlines=True).stdout.strip()


def row(pid, argv, ppid=1, stat="S", tty="?", pgid=None, uid=None):
    return {"pid": pid, "ppid": ppid, "pgid": pgid if pgid is not None else pid, "uid": os.getuid() if uid is None else uid,
            "tty": tty, "stat": stat, "lstart": "Fri Nov 20 17:31:53 2026", "etime": 60, "at": 1794000000.0, "argv": argv}


class H12_LauncherForm(unittest.TestCase):
    """The launcher form (spec): whole elements only, the --space value exactly the id."""

    def test_forms(self):
        yes = [["/usr/bin/python3", "/h/.config/herdr-pa/tools/pa.py", "session", "--space=" + SP, "--project=/p"],
               ["/h/.local/bin/pa-session", "--space", SP, "--auto"],
               ["/h/.local/bin/pa-session", "--space=" + SP],
               ["python3.11", "/h/bin/pa-session", "--space", SP],
               ["/Library/Frameworks/Python.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python",
                "/h/.config/herdr-pa/tools/pa.py", "session", "--space=" + SP]]
        for argv in yes:
            self.assertTrue(pa.launcher_form(argv, SP), argv)

    def test_h12_mentions_only(self):
        no = [["grep", SP], ["less", "/h/.config/herdr-pa/spaces/{}/session.log".format(SP)],
              ["python3", "/x/other.py", "--space", SP], ["python3", "/x/pa.py", "restart", "--space", SP],
              ["python3", "/x/pa.py", "session", "--project=/p --space=" + SP], ["vim", "--space", SP],
              ["python3", "/x/tools/pa.py", "session", "--space=" + SP + "x"],
              ["python3", "/x/tools/pa.py", "session", "--space=sp1de"],
              ["zsh", "-lic", '"$@";', "exec", '"$0"', "-li", "zsh", "pa-session", "--space", SP], [], None, "pa-session"]
        for argv in no:
            self.assertFalse(pa.launcher_form(argv, SP), argv)

    def test_h11_prefix_or_substring_space(self):
        argv = ["/h/.local/bin/pa-session", "--space", SP]
        for other in ("sp1de", SP[:-1], SP + "0", "", "..", "a/" + SP, "-" + SP):
            self.assertFalse(pa.launcher_form(argv, other), other)


class H13_SessionFlags(unittest.TestCase):
    def test_h13(self):
        ids = {CUR, PREV}
        self.assertTrue(pa.session_flag(["claude", "--session-id", PREV], ids))
        self.assertTrue(pa.session_flag(["claude", "--resume", CUR, "--name", "PA: x"], ids))
        self.assertTrue(pa.session_flag(["claude", "--session-id=" + PREV], ids))
        self.assertTrue(pa.session_flag(["claude", "--resume=" + CUR], ids))
        self.assertFalse(pa.session_flag(["claude", "--append-system-prompt", "... {} ...".format(PREV)], ids))
        self.assertFalse(pa.session_flag(["claude", "--append-system-prompt", "--resume " + PREV], ids))
        self.assertFalse(pa.session_flag(["claude", "--session-id", PREV + "x"], ids))
        self.assertFalse(pa.session_flag(["claude", "--session-id"], ids))
        self.assertFalse(pa.session_flag(["claude", "--session-id", ""], {""}))


class Targets(unittest.TestCase):
    """recover_targets(rows, space, ids, me): what --recover would end, from one ps read."""
    ME = 4000

    def rows(self):
        return [row(1, ["/sbin/init"], ppid=0),
                row(3999, ["sshd"], ppid=1),
                row(self.ME, ["python3", "pa.py", "restart"], ppid=3999),
                row(90, ["zsh", "-lic", '"$@";', "exec", '"$0"', "-li", "zsh", "pa-session", "--space", SP], ppid=1,
                    stat="Ss", tty="pts/1"),  # the tab's shell: session leader
                row(100, ["python3", "/h/tools/pa.py", "session", "--space=" + SP], ppid=90, stat="T", tty="pts/1"),
                row(101, ["claude", "--resume", CUR], ppid=100, stat="T", tty="pts/1", pgid=101),  # its new Claude
                row(102, ["claude", "--session-id", PREV], ppid=1, stat="Tl", tty="pts/1", pgid=100),  # the orphan
                row(103, ["mcp-server"], ppid=102, stat="T", tty="?", pgid=100),  # in the launcher's group
                row(200, ["claude", "--resume", PREV], ppid=1, stat="S", tty="pts/4"),  # an orphan elsewhere
                row(300, ["claude", "--resume", PREV], ppid=310, stat="S", tty="pts/5"),  # H14: a person's own
                row(310, ["zsh"], ppid=1, stat="Ss", tty="pts/5"),
                row(400, ["less", "/h/spaces/{}/session.log".format(SP)], ppid=1, stat="T", tty="pts/6")]

    def test_what_it_ends(self):
        got = pa.recover_targets(self.rows(), SP, {CUR, PREV}, self.ME)
        self.assertEqual(sorted(r["pid"] for r in got["ended"]), [100, 101, 102, 103, 200])
        self.assertEqual(got["running"], [])
        whats = {r["pid"]: r["what"] for r in got["ended"]}
        self.assertEqual(whats[100], "launcher")
        self.assertEqual(whats[200], "orphan")

    def test_h14_a_persons_resume_and_mentions_not_ended(self):
        got = pa.recover_targets(self.rows(), SP, {CUR, PREV}, self.ME)
        pids = {r["pid"] for r in got["ended"]}
        for pid in (90, 300, 310, 400, self.ME, 3999, 1):
            self.assertNotIn(pid, pids)

    def test_h15_running_launcher_is_reported(self):
        rows = [r if r["pid"] != 100 else dict(r, stat="S+") for r in self.rows()]
        got = pa.recover_targets(rows, SP, {CUR, PREV}, self.ME)
        self.assertEqual([r["pid"] for r in got["running"]], [100])

    def test_user_init_parent_counts_as_orphaned(self):
        rows = self.rows() + [row(500, ["/lib/systemd/systemd", "--user"], ppid=1),
                              row(501, ["claude", "--resume", CUR], ppid=500, tty="?")]
        got = pa.recover_targets(rows, SP, {CUR, PREV}, self.ME)
        self.assertIn(501, {r["pid"] for r in got["ended"]})

    def test_own_group_and_other_uid_never(self):
        rows = self.rows() + [row(600, ["claude", "--resume", CUR], ppid=1, pgid=os.getpgrp()),
                              row(601, ["claude", "--resume", CUR], ppid=1, uid=os.getuid() + 1)]
        got = pa.recover_targets(rows, SP, {CUR, PREV}, os.getpid())
        pids = {r["pid"] for r in got["ended"]}
        self.assertNotIn(601, pids)


class Recover(unittest.TestCase):
    """pa-restart --recover end to end, in process (pa.main), with real stand-in processes."""

    def setUp(self):
        shutil.rmtree(pa.ROOT, ignore_errors=True)
        self.dir = os.path.join(pa.ROOT, "spaces", SP)
        os.makedirs(self.dir)
        self.agent = {"version": 1, "space": SP, "project": TMP, "profile": "test", "name": "proj", "terminal": "t1",
                      "sessionId": CUR, "previousSessions": [PREV, "older"]}
        with open(os.path.join(self.dir, "agent.json"), "w") as f:
            json.dump(self.agent, f)
        self.procs = []
        saved = {k: pa.__dict__.get(k) for k in ("RECOVER_GAP", "hc_get", "call", "recover_wait", "user_init")}
        pa.RECOVER_GAP = 0.2
        pa.hc_get = lambda address: {}
        pa.call = lambda argv, check=True: ""

        def undo():
            for k, v in saved.items():
                if v is None:
                    pa.__dict__.pop(k, None)
                else:
                    setattr(pa, k, v)
            for p in self.procs:
                if alive(p):
                    try:
                        os.kill(p, signal.SIGKILL)
                    except OSError:
                        pass
        self.addCleanup(undo)

    def reap(self, p):
        """Ends a stand-in this test started (SIGKILL ends a stopped one too) and waits for it."""
        if p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass
        p.wait()

    def beat(self, age):
        with open(os.path.join(self.dir, "heartbeat"), "w") as f:
            f.write("{} t1\n".format(int(time.time() - age)))

    def launcher(self, stopped=True, space=SP, form="pa.py"):
        tools = os.path.join(TMP, "inst-" + str(len(self.procs)), "tools")
        os.makedirs(tools, exist_ok=True)
        body = "import os, signal, time\n" + ("os.kill(os.getpid(), signal.SIGSTOP)\n" if stopped else "") + "time.sleep(300)\n"
        if form == "pa.py":
            path = os.path.join(tools, "pa.py")
            argv = [sys.executable, path, "session", "--space=" + space]
        else:
            path = os.path.join(tools, "pa-session")
            argv = [sys.executable, path, "--space", space]
        with open(path, "w") as f:
            f.write(body)
        pid = self.detached(argv)
        end = time.time() + 10
        while time.time() < end:
            st = state(pid)
            if (stopped and st.startswith("T")) or (not stopped and st):
                break
            time.sleep(0.05)
        return pid

    def detached(self, argv):
        """argv run in a session of its own and re-parented (its starter exits), as a launcher left in a tab is: not
        a descendant of this test process (which --recover, run here, never ends)."""
        out = subprocess.run(["/bin/sh", "-c", '"$0" -c "import os, sys; os.setsid(); os.execv(sys.argv[1], sys.argv[1:])" "$@" '
                              '</dev/null >/dev/null 2>&1 & echo $!', sys.executable] + argv,
                             stdout=subprocess.PIPE, universal_newlines=True, timeout=10).stdout
        pid = int(out.strip())
        self.procs.append(pid)
        return pid

    def orphan(self, session):
        # in a session of its own, as a Claude left by a tab is (never in --recover's own group)
        pid = self.detached([sys.executable, "-c", "import time; time.sleep(300)", "--session-id", session])
        end = time.time() + 5
        rows = None
        while time.time() < end:
            rows = {r["pid"]: r for r in pa.ps_rows() or []}
            if pid in rows and rows[pid]["ppid"] != os.getpid():
                break
            time.sleep(0.05)
        parent = rows[pid]["ppid"]
        real = pa.user_init
        # The orphan's new parent is pid 1, or a subreaper of this machine's own (systemd --user, or the runner's)
        pa.user_init = lambda r, by_pid: r.get("ppid") == parent or real(r, by_pid)
        return pid

    def run_tool(self, *flags):
        argv = ["pa-restart", "--space", SP, "--recover", "--json"] + list(flags)
        saved = sys.argv
        sys.argv = argv
        out, err = io.StringIO(), io.StringIO()
        code = 0
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                pa.main()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
        except pa.Problem as e:
            err.write(str(e))
            code = 2
        finally:
            sys.argv = saved
        lines = [x for x in out.getvalue().splitlines() if x.startswith("{")]
        return code, (json.loads(lines[-1]) if lines else None), out.getvalue() + err.getvalue()

    def log(self):
        try:
            with open(os.path.join(self.dir, "session.log")) as f:
                return f.read()
        except OSError:
            return ""

    def journal(self):
        try:
            with open(os.path.join(self.dir, "journal.jsonl")) as f:
                return [json.loads(x) for x in f if x.strip()]
        except OSError:
            return []

    # ---- F7 ----

    def test_f7_stopped_launcher_and_orphan_are_ended(self):
        self.beat(120)
        launcher = self.launcher()
        orphan = self.orphan(PREV)
        code, answer, text = self.run_tool()
        self.assertEqual(code, 0, text)
        self.assertEqual(answer["outcome"], "recovered", answer)
        ended = {e["pid"]: e["what"] for e in answer["ended"]}
        self.assertEqual(ended.get(launcher), "launcher", answer)
        self.assertEqual(ended.get(orphan), "orphan", answer)
        self.assertFalse(alive(launcher))
        self.assertFalse(alive(orphan))
        self.assertIn("recover ended pid={}".format(launcher), self.log())
        self.assertTrue(any(j.get("by") and "recover" in str(j.get("why")) for j in self.journal()), self.journal())
        self.assertFalse(os.path.exists(os.path.join(self.dir, "restart-next.json")))

    def test_f7_pa_session_form_too(self):
        self.beat(120)
        launcher = self.launcher(form="pa-session")
        code, answer, text = self.run_tool()
        self.assertEqual((code, answer["outcome"]), (0, "recovered"), text)
        self.assertFalse(alive(launcher))

    def test_f7_fresh_heartbeat_refused(self):
        self.beat(2)
        launcher = self.launcher()
        code, answer, text = self.run_tool()
        self.assertEqual(code, 2, text)
        self.assertEqual(answer["outcome"], "refused")
        self.assertTrue(alive(launcher))

    def test_h15_running_launcher_refused(self):
        self.beat(120)
        launcher = self.launcher(stopped=False)
        code, answer, text = self.run_tool()
        self.assertEqual(code, 2, text)
        self.assertEqual(answer["outcome"], "refused")
        self.assertIn("runs but does not beat: pid {}".format(launcher), answer["why"])
        self.assertTrue(alive(launcher))
        self.assertEqual(answer["ended"], [])

    def test_f7_another_spaces_launcher_and_a_mention_not_ended(self):
        self.beat(120)
        other = self.launcher(space=SP + "0")
        prefix = self.launcher(space=SP[:5])
        mention = types.SimpleNamespace(pid=self.detached([sys.executable, "-c", "import os, signal, time; os.kill(os.getpid(), signal.SIGSTOP); time.sleep(300)",
                                                          "--space=" + SP, os.path.join(pa.ROOT, "spaces", SP, "session.log")]))
        time.sleep(0.5)
        code, answer, text = self.run_tool()
        self.assertEqual(code, 0, text)
        self.assertEqual(answer["outcome"], "nothing", answer)
        for pid in (other, prefix, mention.pid):
            self.assertTrue(alive(pid), pid)

    def test_h16_heartbeat_fresh_on_the_second_read(self):
        self.beat(120)
        launcher = self.launcher()
        pa.recover_wait = lambda: self.beat(0)
        code, answer, text = self.run_tool()
        self.assertEqual(code, 0, text)
        self.assertEqual(answer["outcome"], "nothing")
        self.assertEqual(answer["ended"], [])
        self.assertTrue(alive(launcher))

    def test_two_reads_10s_apart(self):
        self.assertEqual(pa.__dict__.get("RECOVER_GAP_DEFAULT", 10), 10)
        waits = []
        self.beat(120)
        pa.recover_wait = lambda: waits.append(1)
        self.run_tool()
        self.assertEqual(waits, [1])

    def test_missing_heartbeat_acts(self):
        launcher = self.launcher()
        code, answer, text = self.run_tool()
        self.assertEqual((code, answer["outcome"]), (0, "recovered"), text)
        self.assertFalse(alive(launcher))

    def test_h17_odd_heartbeat_files_read_as_missing(self):
        outside = os.path.join(TMP, "outside-heartbeat")
        with open(outside, "w") as f:
            f.write("{} t1\n".format(int(time.time())))  # fresh, but not in the state folder
        hb = os.path.join(self.dir, "heartbeat")
        for make in ("symlink", "fifo", "empty", "garbage"):
            if os.path.lexists(hb):
                os.unlink(hb)
            if make == "symlink":
                os.symlink(outside, hb)
            elif make == "fifo":
                os.mkfifo(hb)
            else:
                with open(hb, "w") as f:
                    f.write("" if make == "empty" else "soon t1\n")
            self.assertIsNone(pa.recover_heartbeat(self.dir), make)
        os.unlink(hb)
        os.symlink(outside, hb)
        launcher = self.launcher()
        began = time.time()
        code, answer, text = self.run_tool()
        self.assertLess(time.time() - began, 20)
        self.assertEqual((code, answer["outcome"]), (0, "recovered"), text)
        self.assertFalse(alive(launcher))

    def test_fresh_writes_restart_next(self):
        self.beat(120)
        self.launcher()
        code, answer, text = self.run_tool("--fresh")
        self.assertEqual(code, 0, text)
        with open(os.path.join(self.dir, "restart-next.json")) as f:
            nxt = json.load(f)
        self.assertIs(nxt["fresh"], True)
        self.assertRegex(nxt["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertIn("by", nxt)

    def test_dry_run_ends_and_writes_nothing(self):
        self.beat(120)
        launcher = self.launcher()
        code, answer, text = self.run_tool("--dry-run", "--fresh")
        self.assertEqual(code, 0, text)
        self.assertTrue(alive(launcher))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "restart-next.json")))
        self.assertEqual(self.journal(), [])
        self.assertIn(launcher, [e["pid"] for e in answer["ended"]])

    def test_writes_only_its_log_and_journal(self):
        self.beat(120)
        self.launcher()
        before = sorted(os.listdir(self.dir))
        self.run_tool()
        after = sorted(os.listdir(self.dir))
        self.assertEqual(sorted(set(after) - set(before)), sorted({"session.log", "journal.jsonl"} - set(before)))

    # ---- H11, H19 ----

    def test_h11_bad_space_ids(self):
        for bad in ("..", "a/" + SP, "-x", "", SP[:5]):
            saved = sys.argv
            sys.argv = ["pa-restart", "--space=" + bad, "--recover", "--json"]
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises((pa.Problem, SystemExit)):
                        pa.main()
            finally:
                sys.argv = saved

    def test_h19_usage(self):
        for flags in (["--now"], ["--expect-instance", "i-1"], ["--end-terminal"]):
            code, answer, text = self.run_tool(*flags)
            self.assertEqual(code, 2, (flags, text))
            self.assertIn("--recover", text)

    def test_relay_passes_recover(self):
        args = types.SimpleNamespace(space=SP, now=False, fresh=True, dry_run=False, json=True, why=None, wait=None,
                                     expect_instance=None, by="panel", recover=True)
        flags = pa.restart_flags(args)
        self.assertIn("--recover", flags)
        self.assertIn("--fresh", flags)


class H18_RestartNext(unittest.TestCase):
    """R5.2, H18: the launcher reads restart-next.json once at its first start: a fresh start within 10 min of `at`;
    else ignored; removed either way."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=TMP)
        self.path = os.path.join(self.dir, "restart-next.json")

    def put(self, value):
        with open(self.path, "w") as f:
            f.write(value if isinstance(value, str) else json.dumps(value))

    def test_valid(self):
        self.put({"fresh": True, "at": pa.now(), "by": "panel"})
        self.assertTrue(pa.take_restart_next(self.dir))
        self.assertFalse(os.path.lexists(self.path))

    def test_h18_ignored_and_removed(self):
        for value in ({"fresh": True, "at": pa.iso_time(time.time() - 11 * 60), "by": "panel"}, "{not json",
                      {"fresh": "yes", "at": pa.now()}, {"fresh": 1, "at": pa.now()}, {"at": pa.now()}, [],
                      {"fresh": True, "at": "yesterday"}, {"fresh": True, "at": pa.iso_time(time.time() + 3600)}):
            self.put(value)
            self.assertFalse(pa.take_restart_next(self.dir), value)
            self.assertFalse(os.path.lexists(self.path), value)
        self.assertFalse(pa.take_restart_next(self.dir))  # none there

    def test_symlink_removed_not_followed(self):
        target = os.path.join(TMP, "next-target.json")
        with open(target, "w") as f:
            json.dump({"fresh": True, "at": pa.now()}, f)
        os.symlink(target, self.path)
        self.assertFalse(pa.take_restart_next(self.dir))
        self.assertFalse(os.path.lexists(self.path))
        self.assertTrue(os.path.exists(target))

    def test_first_start_is_fresh(self):
        shutil.rmtree(pa.ROOT, ignore_errors=True)
        d = os.path.join(pa.ROOT, "spaces", "sp1")
        os.makedirs(d)
        project = os.path.join(TMP, "proj18")
        os.makedirs(project, exist_ok=True)
        prof = os.path.join(pa.ROOT, "profiles", "test")
        os.makedirs(prof, exist_ok=True)
        with open(os.path.join(prof, "profile.json"), "w") as f:
            json.dump({"name": "test", "claude": ["true"], "codex": ["true"]}, f)
        with open(os.path.join(d, "agent.json"), "w") as f:
            json.dump({"version": 1, "space": "sp1", "project": project, "profile": "test", "name": "proj",
                       "terminal": "t1", "sessionId": CUR, "sessionStarted": True}, f)
        with open(os.path.join(d, "restart-next.json"), "w") as f:
            json.dump({"fresh": True, "at": pa.now(), "by": "panel"}, f)
        saved = {k: getattr(pa, k) for k in ("hc_get", "live_terminals", "call")}
        pa.hc_get, pa.live_terminals, pa.call = (lambda a: {}), (lambda: {}), (lambda argv, check=True: "")
        started = []

        class Stop(Exception):
            pass

        def fake_call(argv, cwd=None):
            started.append(argv)
            raise Stop()

        class NoThread:
            def __init__(self, target=None, daemon=None):
                pass

            def start(self):
                pass
        saved_thread = pa.threading.Thread
        saved_sig = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTTOU, signal.SIGTTIN, signal.SIGTSTP)}
        pa.threading.Thread = NoThread
        undo = child_fakes.patch(pa, fake_call)
        try:
            args = types.SimpleNamespace(space="sp1", project=project, profile="test", name=None, auto=False, ask=False,
                                         fresh=False)
            with self.assertRaises(Stop):
                pa.run_session(args, "t1", {}, "sp1", project, {"beat": False})
        finally:
            undo()
            pa.threading.Thread = saved_thread
            for k, v in saved.items():
                setattr(pa, k, v)
            for s, h in saved_sig.items():
                signal.signal(s, h)
        argv = started[0]
        self.assertNotIn("--resume", argv, "the first start after a recover --fresh resumed")
        self.assertNotEqual(argv[argv.index("--session-id") + 1], CUR)
        with open(os.path.join(d, "agent.json")) as f:
            self.assertEqual(json.load(f)["previousSessions"][0], CUR)
        self.assertFalse(os.path.exists(os.path.join(d, "restart-next.json")))


if __name__ == "__main__":
    unittest.main()
