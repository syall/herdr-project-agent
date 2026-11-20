"""t-193: a restart leaves one Project Agent running in its tab, every time.

A process left on the launcher's terminal by an earlier launch (an old Claude in the launcher's own process group)
stopped the launcher when the new Claude took the terminal: SIGTTOU went to the whole group, the tab's shell took the
terminal back and read the terminal's replies as typed input (spec.md R1-R3, FINDINGS.md).

Run: python3 -m unittest -v tests/test_pa_leftovers.py
Temp homes only: HERDR_PA_HOME, HERDR_APP_HOME and CLAUDE_CONFIG_DIR point at a temp folder before pa.py is loaded.
Every process signalled is one these tests started (stand-in shells, launchers, leftovers and Claudes, all python or
sleep), on ptys the tests open; no Claude, no hc (pa.call is replaced in every helper launcher). The in-process tests
fake ps and kill, or replace pa.foreground_tty so the test runner's terminal is never handed over.
"""
import errno
import importlib.util
import json
import os
import pty
import select
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
import child_fakes  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PA_PY = os.path.join(REPO, "runtime", "tools", "pa.py")
TMP = tempfile.mkdtemp(prefix="t193-left-")
os.environ["HERDR_PA_HOME"] = os.path.join(TMP, "pa")
os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(TMP, "claude")
os.environ["HERDR_APP_HOME"] = os.path.join(TMP, "app")
for k in ("HC_TERMINAL_ID", "CLAUDECODE"):
    os.environ.pop(k, None)
spec = importlib.util.spec_from_file_location("pa", PA_PY)
pa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pa)
assert pa.ROOT.startswith(TMP) and pa.CLAUDE_HOME.startswith(TMP), "pa.py must use the temp homes"

# R3.1, exactly (no spaces)
TTY_RESET = ("\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1005l\x1b[?1006l\x1b[?1015l\x1b[?1004l\x1b[?2004l\x1b[<u"
             "\x1b[?25h\x1b[?1049l")
JOB_SIGNALS = (signal.SIGTTOU, signal.SIGTTIN, signal.SIGTSTP)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def alive(pid):
    """A live process (a zombie waiting for its reaper counts as gone)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, universal_newlines=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


def kill_quietly(pid):
    if isinstance(pid, int) and pid > 1 and pid != os.getpid():
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def row(pid, ppid=1, tty="pts/1", uid=None, stat="S", pgid=None, lstart="Fri Nov 20 17:31:53 2026", etime=60,
        argv=None):
    return {"pid": pid, "ppid": ppid, "pgid": pgid if pgid is not None else pid, "uid": os.getuid() if uid is None else uid,
            "tty": tty, "stat": stat, "lstart": lstart, "etime": etime, "at": 1794000000.0, "argv": argv or ["x"]}


# ---- the pty harness: a stand-in job-control shell (zsh in a Herdr tab) runs a helper launcher as its job ----------

# The shell: the pty's session leader. It runs its command as a foreground job in a group of its own and waits for it
# with WUNTRACED, as zsh does: a stopped job means zsh takes the terminal back and prints its prompt (the incident).
SHELL = r"""
import fcntl, json, os, signal, sys, termios, time
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
signal.signal(signal.SIGTTOU, signal.SIG_IGN)
out, cmd = sys.argv[1], sys.argv[2:]
pid = os.fork()
if pid == 0:
    os.setpgid(0, 0)
    signal.signal(signal.SIGTTOU, signal.SIG_DFL)
    os.execv(cmd[0], cmd)
try:
    os.setpgid(pid, pid)
except OSError:
    pass
os.tcsetpgrp(0, pid)
res, end = {"state": "timeout"}, time.time() + 90
while time.time() < end:
    p, st = os.waitpid(pid, os.WUNTRACED | os.WNOHANG)
    if p == 0:
        time.sleep(0.05)
        continue
    if os.WIFSTOPPED(st):
        res = {"state": "stopped", "signal": os.WSTOPSIG(st)}
        os.killpg(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    elif os.WIFEXITED(st):
        res = {"state": "exited", "code": os.WEXITSTATUS(st)}
    else:
        res = {"state": "signalled", "signal": os.WTERMSIG(st)}
    break
if res["state"] == "timeout":
    os.killpg(pid, signal.SIGKILL)
os.tcsetpgrp(0, os.getpgrp())
with open(out, "w") as f:
    json.dump(res, f)
"""

# The stub Claude: answers the launcher's probes, else records what it sees, holds the terminal 2 s (time for a
# leftover to touch the terminal from the background) and exits 0.
STUB = r"""
import json, os, subprocess, sys, time
if "--help" in sys.argv or "--version" in sys.argv:
    print("2.1.0 (Claude Code)")
    sys.exit(0)
d = os.environ["T193_DIR"]
def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], stdout=subprocess.PIPE, universal_newlines=True).stdout.strip()
    return bool(st) and not st.startswith("Z")
try:
    orphan = int(open(os.path.join(d, "orphan.pid")).read().strip())
except (OSError, ValueError):
    orphan = None
with open(os.path.join(d, "stub.json"), "w") as f:
    json.dump({"pid": os.getpid(), "pgrp": os.getpgrp(), "fg": os.tcgetpgrp(0),
               "orphanAlive": alive(orphan) if orphan else None}, f)
time.sleep(2)
"""

# The helper launcher (the shell's job). Modes:
#   run     an orphan in its own group touches the terminal, then run_agent starts the stub Claude (F1, V3)
#   exec    an "old" launcher: a wrapper started without a group of its own leaves its grandchild (the inner Claude)
#           when only the handle is ended (the old end_child), then this process execs itself (the restart's exec)
#           and the new code's first start runs (F4)
#   after   the exec'd process of `exec`: run_agent at once
#   spare   end_leftovers with an ancestor (the shell), a live descendant and a stopped leftover (F2)
#   ignore  end_leftovers with a leftover that ignores SIGTERM (F3, H9); END_GRACE 1
#   tty     run_child of a child that turns mouse mode on and leaves a terminal reply unread (F6)
LAUNCHER = r"""
import fcntl, importlib.util, json, os, signal, struct, subprocess, sys, termios, time, types
pa_py, d, mode = sys.argv[1], sys.argv[2], sys.argv[3]
os.environ["HC_TERMINAL_ID"] = "t1"
os.environ["T193_DIR"] = d
spec = importlib.util.spec_from_file_location("pa", pa_py)
pa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pa)
pa.call = lambda argv, check=True, **k: ""  # never hc
result = {"mode": mode, "pid": os.getpid()}

def save():
    with open(os.path.join(d, "launcher.json.out"), "w") as f:
        json.dump(result, f)

def orphan(kind):
    # A process in this process's group, re-parented (its parent exits): what an old launch left on the terminal.
    r, w = os.pipe()
    x = os.fork()
    if x == 0:
        o = os.fork()
        if o == 0:
            os.close(r)
            for s in (signal.SIGTTOU, signal.SIGTTIN, signal.SIGTSTP):
                signal.signal(s, signal.SIG_DFL)
            if kind == "ignore":
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
            os.write(w, (str(os.getpid()) + "\n").encode())
            os.close(w)
            if kind == "stopped":
                os.kill(os.getpid(), signal.SIGSTOP)
            while True:
                try:
                    termios.tcsetattr(0, termios.TCSANOW, termios.tcgetattr(0))
                except termios.error:
                    pass
                time.sleep(0.2)
        os._exit(0)
    os.close(w)
    os.waitpid(x, 0)
    pid = int(os.read(r, 64).decode().strip())
    os.close(r)
    with open(os.path.join(d, "orphan.pid"), "w") as f:
        f.write(str(pid))
    return pid

class Done(Exception):
    pass

def run_agent():
    prof = os.path.join(pa.ROOT, "profiles", "test")
    os.makedirs(prof, exist_ok=True)
    with open(os.path.join(prof, "profile.json"), "w") as f:
        json.dump({"name": "test", "claude": [sys.executable, os.path.join(d, "stub.py")], "codex": ["true"]}, f)
    space = os.path.join(pa.ROOT, "spaces", "sp1")
    os.makedirs(space, exist_ok=True)
    agent = {"version": 1, "space": "sp1", "project": d, "profile": "test", "name": "proj", "terminal": "t1",
             "sessionId": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "sessionStarted": False}
    with open(os.path.join(space, "agent.json"), "w") as f:
        json.dump(agent, f)
    def stop(*a, **k):
        raise Done()
    pa.reload_after_exit = stop  # after the first exit: done
    args = types.SimpleNamespace(fresh=False)
    ctx = {"beat": False, "directory": space}
    try:
        pa.run_agent(args, "t1", d, pa.load_profile("test"), space, agent, ctx, time.time())
    except Done:
        pass
    with open(os.path.join(space, "session.log")) as f:
        result["log"] = f.read()
    result["foreground"] = os.tcgetpgrp(0) == os.getpgrp()

if mode == "run":
    result["orphan"] = orphan("touch")
    time.sleep(0.5)
    run_agent()
elif mode == "exec":
    script = ("import termios, time\n"
              "while True:\n"
              "    try:\n"
              "        termios.tcsetattr(0, termios.TCSANOW, termios.tcgetattr(0))\n"
              "    except termios.error:\n"
              "        pass\n"
              "    time.sleep(0.2)\n")
    wrapper = ("{} -c '{}' & echo $! > {}/orphan.pid; trap 'exit 255' TERM; wait".format(
        sys.executable, script.replace("'", "'\"'\"'"), d))
    old = subprocess.Popen(["/bin/sh", "-c", wrapper])  # the code from before t-184: no group of its own
    end = time.time() + 10
    while not os.path.exists(os.path.join(d, "orphan.pid")) and time.time() < end:
        time.sleep(0.05)
    time.sleep(0.5)
    old.send_signal(signal.SIGTERM)  # the old end_child: the handle only; the wrapper exits 255, its child stays
    old.wait()
    os.execv(sys.executable, [sys.executable, sys.argv[0], pa_py, d, "after"])
elif mode == "after":
    result["orphan"] = int(open(os.path.join(d, "orphan.pid")).read().strip())
    run_agent()
elif mode in ("spare", "ignore"):
    space = os.path.join(pa.ROOT, "spaces", "sp1")
    os.makedirs(space, exist_ok=True)
    if mode == "ignore":
        pa.END_GRACE = 1
    desc = subprocess.Popen(["sleep", "30"])  # the launcher's own live call: a descendant
    result["descendant"] = desc.pid
    result["orphan"] = orphan("stopped" if mode == "spare" else "ignore")
    time.sleep(0.5)
    began = time.time()
    pa.end_leftovers({"directory": space}, "t1")
    result["took"] = time.time() - began
    result["descendantAlive"] = desc.poll() is None
    result["shell"] = os.getppid()
    with open(os.path.join(space, "session.log")) as f:
        result["log"] = f.read()
    desc.kill()
    desc.wait()
elif mode == "tty":
    child = os.path.join(d, "mouse.py")
    with open(child, "w") as f:
        f.write("import os, sys, time, tty\n"
                "tty.setraw(0)\n"
                "os.write(1, b'\\x1b[?1000h\\x1b[?1006h\\x1b[?2004hREADY\\r\\n')\n"
                "time.sleep(1.5)\n")
    ctx = {"directory": d}
    result["code"] = pa.run_child(ctx, [sys.executable, child], d)
    buf = struct.pack("i", 0)
    result["pending"] = struct.unpack("i", fcntl.ioctl(0, termios.FIONREAD, buf))[0]
save()
"""


class PtyBase(unittest.TestCase):
    """Runs LAUNCHER in `mode` as the job of SHELL on a pty of the test's own; returns (shell result, launcher result,
    everything the pty showed)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=TMP)
        self.pids = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for name in ("orphan.pid",):
            try:
                with open(os.path.join(self.dir, name)) as f:
                    self.pids.append(int(f.read().strip()))
            except (OSError, ValueError):
                pass
        try:
            with open(os.path.join(self.dir, "stub.json")) as f:
                self.pids.append(json.load(f)["pid"])
        except (OSError, ValueError, KeyError):
            pass
        for pid in self.pids:  # only processes these tests started
            kill_quietly(pid)

    def run_job(self, mode, answer=None, timeout=60):
        for name, text in (("shell.py", SHELL), ("launcher.py", LAUNCHER), ("stub.py", STUB)):
            with open(os.path.join(self.dir, name), "w") as f:
                f.write(text)
        out = os.path.join(self.dir, "shell.json")
        err = open(os.path.join(self.dir, "shell.err"), "w")
        self.addCleanup(err.close)
        master, slave = pty.openpty()
        shown = b""
        try:
            proc = subprocess.Popen([sys.executable, os.path.join(self.dir, "shell.py"), out, sys.executable,
                                     os.path.join(self.dir, "launcher.py"), PA_PY, self.dir, mode],
                                    stdin=slave, stdout=slave, stderr=err, start_new_session=True, cwd=self.dir)
            os.close(slave)
            slave = None
            answered, end = False, time.time() + timeout
            while proc.poll() is None and time.time() < end:
                try:
                    if select.select([master], [], [], 0.1)[0]:
                        shown += os.read(master, 65536)
                except OSError:
                    break
                if answer and not answered and b"READY" in shown:
                    os.write(master, answer)
                    answered = True
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            while True:  # what is left in the pty
                try:
                    if not select.select([master], [], [], 0.1)[0]:
                        break
                    more = os.read(master, 65536)
                except OSError:
                    break
                if not more:
                    break
                shown += more
        finally:
            if slave is not None:
                os.close(slave)
            os.close(master)
        with open(os.path.join(self.dir, "shell.err")) as f:
            errors = f.read()
        self.assertTrue(os.path.exists(out), "the stand-in shell gave no result: " + errors[-3000:])
        with open(out) as f:
            shell = json.load(f)
        launcher = None
        if os.path.exists(os.path.join(self.dir, "launcher.json.out")):
            with open(os.path.join(self.dir, "launcher.json.out")) as f:
                launcher = json.load(f)
        self.errors = errors
        return shell, launcher, shown

    def stub(self):
        with open(os.path.join(self.dir, "stub.json")) as f:
            return json.load(f)


class F1_LauncherNotStopped(PtyBase):
    """F1 (R1, R2, R4; V3: stop_repro.py's `bug` mode against the real launcher): an orphan in the launcher's own group
    that redraws the terminal is ended before the stub Claude starts; the launcher is never stopped."""

    def test_orphan_in_the_launchers_group_is_ended_before_the_start(self):
        shell, launcher, shown = self.run_job("run")
        self.assertEqual(shell, {"state": "exited", "code": 0},
                         "the launcher's job was stopped (zsh takes the tab: a bare prompt): {} {}".format(
                             shell, self.errors[-2000:]))
        stub = self.stub()
        self.assertFalse(stub["orphanAlive"], "the leftover still ran when the stub Claude started")
        self.assertEqual(stub["fg"], stub["pgrp"], "the stub Claude did not own the terminal's foreground")
        self.assertIn("leftover ended pid={} tty=".format(launcher["orphan"]), launcher["log"])
        self.assertTrue(launcher["foreground"], "the launcher did not get the terminal back")
        self.assertFalse(alive(launcher["orphan"]))


class F4_ExecPathSweeps(PtyBase):
    """F4 (R1, R4): the exec path. An old end_child ended only the wrapper's handle; its inner Claude stayed in the
    launcher's group; the process execs (pid unchanged) and the new code's first start ends it."""

    def test_first_start_after_the_exec_ends_what_the_old_code_left(self):
        shell, launcher, shown = self.run_job("exec")
        self.assertEqual(shell, {"state": "exited", "code": 0}, "stopped after the exec: {} {}".format(
            shell, self.errors[-2000:]))
        stub = self.stub()
        self.assertFalse(stub["orphanAlive"], "the old Claude left by the handle-only end still ran")
        self.assertIn("leftover ended pid={} tty=".format(launcher["orphan"]), launcher["log"])


class F2_WhatTheSweepSpares(PtyBase):
    """F2 (R1.1, R1.2, H3, H4, H9): the sweep spares the tab's shell (an ancestor), the launcher's own live call
    (a descendant) and a process on another pty; a stopped leftover is gone within 2 s (SIGCONT), not END_GRACE."""

    def test_spares_ancestor_descendant_and_other_pty_ends_a_stopped_leftover_fast(self):
        other_master, other_slave = pty.openpty()  # another pty, a process of this uid in a session of its own there
        other = subprocess.Popen([sys.executable, "-c", "import fcntl, termios, time; fcntl.ioctl(0, termios.TIOCSCTTY, 0); time.sleep(60)"],
                                 stdin=other_slave, stdout=other_slave, stderr=subprocess.DEVNULL, start_new_session=True)
        os.close(other_slave)
        self.addCleanup(os.close, other_master)
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        shell, launcher, shown = self.run_job("spare")
        self.assertEqual(shell, {"state": "exited", "code": 0}, self.errors[-2000:])
        self.assertIsNotNone(launcher, self.errors[-2000:])
        self.assertTrue(launcher["descendantAlive"], "the launcher's own live call was signalled")
        self.assertFalse(alive(launcher["orphan"]), "the stopped leftover outlived the sweep")
        self.assertLess(launcher["took"], 2.0, "a stopped leftover took {:.1f} s: no SIGCONT".format(launcher["took"]))
        self.assertIn("leftover ended pid={} tty=".format(launcher["orphan"]), launcher["log"])
        self.assertIn("how=term", launcher["log"])
        self.assertIsNone(other.poll(), "a process on another pty was signalled")
        self.assertNotIn("pid={} ".format(launcher["shell"]), launcher["log"], "the tab's shell was a target")


class F3_TermIgnored(PtyBase):
    """F3 (R1.2, H9): a leftover that ignores SIGTERM is SIGKILLed after END_GRACE."""

    def test_sigterm_ignored_then_sigkill(self):
        shell, launcher, shown = self.run_job("ignore")
        self.assertEqual(shell, {"state": "exited", "code": 0}, self.errors[-2000:])
        self.assertFalse(alive(launcher["orphan"]))
        self.assertGreaterEqual(launcher["took"], 0.9, "SIGKILL before the grace")
        self.assertLess(launcher["took"], 1 + pa.KILL_WAIT + 2)
        self.assertIn("leftover ended pid={} tty=".format(launcher["orphan"]), launcher["log"])
        self.assertIn("how=kill", launcher["log"])


class F6_TerminalHygiene(PtyBase):
    """F6 (R3.1): after a child that turned mouse mode on and left a terminal reply unread, the pty shows the reset
    sequence and the terminal's input queue is empty before the next start."""

    def test_reset_sequence_and_input_flushed(self):
        shell, launcher, shown = self.run_job("tty", answer=b"\x1b[<0;62;23M\x1b[?62;22c")
        self.assertEqual(shell, {"state": "exited", "code": 0}, self.errors[-2000:])
        self.assertEqual(launcher["code"], 0)
        after = shown.split(b"READY", 1)[-1]
        self.assertIn(TTY_RESET.encode(), after, "no reset sequence after the child (got {!r})".format(after[-300:]))
        self.assertEqual(launcher["pending"], 0, "the terminal's reply was left for the shell to read")


# ---- in-process: the sweep's choice, its steps, its guards ----------------------------------------------------------

class Guarded(unittest.TestCase):
    """Fakes ps (pa.ps_rows) and the one signal call (pa.signal_pid): nothing real is signalled."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=TMP)
        self.kills, self.gone, self.eperm, self.gone_on = [], set(), set(), {}
        saved = {k: pa.__dict__.get(k) for k in ("ps_rows", "launcher_tty", "signal_pid")}

        def send(pid, sig):
            self.kills.append((pid, sig))
            if pid in self.eperm:
                raise PermissionError(errno.EPERM, "not permitted")
            if sig in self.gone_on.get(pid, ()):
                self.gone.add(pid)
        pa.signal_pid = send
        os.environ["HC_TERMINAL_ID"] = "t1"

        def undo():
            os.environ.pop("HC_TERMINAL_ID", None)
            for k, v in saved.items():
                if v is None:
                    pa.__dict__.pop(k, None)
                else:
                    setattr(pa, k, v)
        self.addCleanup(undo)

    def log(self):
        try:
            with open(os.path.join(self.dir, "session.log")) as f:
                return f.read()
        except OSError:
            return ""

    def rows_after_kill(self, rows):
        """ps as seen now: a pid marked gone by a signal is not listed."""
        return lambda *a, **k: [r for r in rows if r["pid"] not in self.gone]


class H1_H4_H8_Choice(unittest.TestCase):
    """leftover_targets(rows, me, tty, uid): (targets, other-uid rows). Ancestors, descendants, pid <= 1, the launcher
    itself and other ttys are never targets; a target's own descendants are, whatever their tty."""
    ME = 500

    def chain(self):
        return [row(1, ppid=0, tty="?"), row(80, ppid=1, tty="pts/1", stat="Ss"),  # a login shell above the tab's shell
                row(90, ppid=80, tty="pts/1", stat="S"),  # the tab's shell (zsh -lic ...)
                row(self.ME, ppid=90, tty="pts/1", stat="S+"),  # the launcher
                row(501, ppid=self.ME, tty="pts/1"),  # its live hc call (H4)
                row(502, ppid=501, tty="?")]  # and that call's child

    def pids(self, rows, tty="pts/1"):
        targets, foreign = pa.leftover_targets(rows, self.ME, tty, os.getuid())
        return sorted(r["pid"] for r in targets), sorted(r["pid"] for r in foreign)

    def test_h3_h4_ancestors_and_descendants_spared(self):
        rows = self.chain() + [row(600, ppid=1, tty="pts/1", stat="T")]
        self.assertEqual(self.pids(rows), ([600], []))

    def test_h1_tty_compared_exactly(self):
        rows = self.chain() + [row(601, tty="pts/10"), row(602, tty="pts/1 "), row(603, tty="pts/1")]
        self.assertEqual(self.pids(rows), ([603], []))
        rows = [row(self.ME, ppid=1, tty="ttys003"), row(604, tty="ttys0030"), row(605, tty="ttys003")]
        self.assertEqual(self.pids(rows, "ttys003"), ([605], []))

    def test_h2_no_tty_is_never_a_leftover(self):
        rows = self.chain() + [row(610 + i, tty=t) for i, t in enumerate(("?", "??", "-", ""))]
        self.assertEqual(self.pids(rows), ([], []))
        for tty in ("?", "??", "-", "", None):
            self.assertEqual(pa.leftover_targets(rows + [row(620, tty=tty or "")], self.ME, tty, os.getuid()), ([], []))

    def test_h8_pid_0_1_negative_and_self_never(self):
        rows = self.chain() + [row(0, tty="pts/1"), row(-1, tty="pts/1"), row(1, ppid=0, tty="pts/1"),
                               row(self.ME, ppid=90, tty="pts/1")]
        self.assertEqual(self.pids(rows), ([], []))

    def test_descendants_of_a_leftover_go_too_whatever_their_tty(self):
        rows = self.chain() + [row(700, tty="pts/1"), row(701, ppid=700, tty="?"), row(702, ppid=701, tty="pts/9")]
        self.assertEqual(self.pids(rows), ([700, 701, 702], []))

    def test_zombies_are_not_targets(self):
        rows = self.chain() + [row(710, tty="pts/1", stat="Z")]
        self.assertEqual(self.pids(rows), ([], []))

    def test_h6_other_uid_is_not_a_target_but_reported(self):
        rows = self.chain() + [row(720, tty="pts/1", uid=os.getuid() + 1)]
        self.assertEqual(self.pids(rows), ([], [720]))


class H5_H6_Steps(Guarded):
    """end_leftovers' steps (R1.2, R1.3): every pid re-read before every signal; EPERM logged as left; the start goes
    on."""

    def test_h5_recycled_pid_is_not_signalled_and_logged(self):
        first = [row(os.getpid(), ppid=1, tty="pts/7"), row(800, tty="pts/7", lstart="Fri Nov 20 17:31:53 2026")]
        second = [row(os.getpid(), ppid=1, tty="pts/7"), row(800, tty="pts/7", lstart="Fri Nov 20 18:00:00 2026")]
        reads = [first]
        pa.ps_rows = lambda *a, **k: reads.pop(0) if reads else second
        pa.launcher_tty = lambda: "pts/7"
        pa.end_leftovers({"directory": self.dir}, "t1")
        self.assertEqual([k for k in self.kills if k[0] == 800], [], "a recycled pid was signalled")
        self.assertIn("pid=800", self.log())
        self.assertIn("not signalled", self.log())

    def test_h6_eperm_logged_as_left(self):
        pa.END_GRACE, saved = 0.3, pa.END_GRACE
        pa.KILL_WAIT, saved_kw = 0.3, pa.KILL_WAIT
        self.addCleanup(setattr, pa, "END_GRACE", saved)
        self.addCleanup(setattr, pa, "KILL_WAIT", saved_kw)
        rows = [row(os.getpid(), ppid=1, tty="pts/7"), row(810, tty="pts/7")]
        pa.ps_rows = lambda *a, **k: list(rows)
        pa.launcher_tty = lambda: "pts/7"
        self.eperm = {810}
        pa.end_leftovers({"directory": self.dir}, "t1")
        self.assertIn("leftover left pid=810", self.log())

    def test_other_uid_on_the_same_tty_is_logged_not_signalled(self):
        rows = [row(os.getpid(), ppid=1, tty="pts/7"), row(820, tty="pts/7", uid=os.getuid() + 1)]
        pa.ps_rows = lambda *a, **k: list(rows)
        pa.launcher_tty = lambda: "pts/7"
        pa.end_leftovers({"directory": self.dir}, "t1")
        self.assertEqual([k for k in self.kills if k[0] == 820], [])
        self.assertIn("leftover left pid=820", self.log())

    def test_term_then_cont_then_ended_line(self):
        rows = [row(os.getpid(), ppid=1, tty="pts/7"), row(830, tty="pts/7", etime=100)]
        self.gone_on = {830: (signal.SIGCONT,)}
        pa.ps_rows = self.rows_after_kill(rows)
        pa.launcher_tty = lambda: "pts/7"
        pa.end_leftovers({"directory": self.dir}, "t1")
        sigs = [s for p, s in self.kills if p == 830]
        self.assertEqual(sigs[:2], [signal.SIGTERM, signal.SIGCONT])
        self.assertNotIn(signal.SIGKILL, sigs)
        line = [x for x in self.log().splitlines() if "leftover ended pid=830" in x]
        self.assertEqual(len(line), 1, self.log())
        self.assertRegex(line[0], r"leftover ended pid=830 tty=pts/7 started=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ how=term$")
        self.assertIn("started={}".format(pa.iso_time(1794000000.0 - 100)), line[0])


class H7_H10_NoSweep(Guarded):
    """H7, H10, R1.5: no ps, a failing ps, a ps past its time, no terminal, no Herdr tab: no signal; said once."""

    def assert_said_once(self):
        lines = [x for x in self.log().splitlines() if "not swept" in x]
        self.assertEqual(len(lines), 1, self.log())

    def run_twice(self):
        for _ in range(2):
            pa.end_leftovers({"directory": self.dir}, "t1")

    def test_h7_ps_missing_failing_or_slow(self):
        pa.launcher_tty = lambda: "pts/7"
        saved = (pa.SWEEP_PS, pa.PS_TIMEOUT)
        self.addCleanup(setattr, pa, "SWEEP_PS", saved[0])
        self.addCleanup(setattr, pa, "PS_TIMEOUT", saved[1])
        for argv in (["/nonexistent/ps"], [sys.executable, "-c", "import sys; print('1 1 1 1 pts/7 S 1:00 Fri Nov 20 17:31:53 2026'); sys.exit(3)"],
                     [sys.executable, "-c", "import time; time.sleep(5)"]):
            open(os.path.join(self.dir, "session.log"), "w").close()
            pa.SWEEP_PS, pa.PS_TIMEOUT = argv, 0.5
            self.assertIsNone(pa.ps_rows())
            ctx = {"directory": self.dir}
            pa.end_leftovers(ctx, "t1")
            pa.end_leftovers(ctx, "t1")
            self.assertEqual(self.kills, [], argv)
            self.assertEqual(len([x for x in self.log().splitlines() if "not swept" in x]), 1, (argv, self.log()))

    def test_h7_malformed_lines_are_skipped(self):
        saved = pa.SWEEP_PS
        self.addCleanup(setattr, pa, "SWEEP_PS", saved)
        text = ("  42     1    42  {uid} pts/7    S     01:02 Fri Nov 20 17:31:53 2026\n"
                "garbage\n"
                "  -1     1    -1  {uid} pts/7    S     01:02 Fri Nov 20 17:31:53 2026\n"
                "   0     0     0  {uid} pts/7    S     01:02 Fri Nov 20 17:31:53 2026\n"
                "  43     1    43  {uid} pts/7    S\n"
                "  44     x    44  {uid} pts/7    S     01:02 Fri Nov 20 17:31:53 2026\n"
                "  45     1    45  {uid} ??       Ss 1-02:03:04 Fri Nov 20 17:31:53 2026\n").format(uid=os.getuid())
        pa.SWEEP_PS = [sys.executable, "-c", "import sys; sys.stdout.write({!r})".format(text)]
        rows = pa.ps_rows()
        self.assertEqual([r["pid"] for r in rows], [42, 45])
        self.assertEqual(rows[0]["etime"], 62)
        self.assertEqual(rows[1]["etime"], 86400 + 2 * 3600 + 3 * 60 + 4)
        self.assertEqual(rows[0]["tty"], "pts/7")
        self.assertEqual(rows[0]["lstart"], "Fri Nov 20 17:31:53 2026")

    def test_ps_reads_on_this_machine_linux_and_macos(self):
        rows = pa.ps_rows()
        self.assertTrue(rows, "this machine's ps gave no rows")
        mine = [r for r in rows if r["pid"] == os.getpid()]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["ppid"], os.getppid())
        self.assertEqual(mine[0]["uid"], os.getuid())
        self.assertEqual(mine[0]["pgid"], os.getpgrp())

    def test_h10_fd0_not_a_tty(self):
        r, w = os.pipe()
        saved = os.dup(0)
        os.dup2(r, 0)

        def restore():
            os.dup2(saved, 0)
            for fd in (saved, r, w):
                os.close(fd)
        self.addCleanup(restore)
        self.assertIsNone(pa.launcher_tty())
        reads = []
        pa.ps_rows = lambda *a, **k: reads.append(1) or []
        self.run_twice()
        self.assertEqual(reads, [], "ps was read without a terminal")
        self.assertEqual(self.kills, [])
        self.assert_said_once()

    def test_not_a_herdr_tab_no_sweep(self):
        os.environ.pop("HC_TERMINAL_ID", None)
        pa.launcher_tty = lambda: "pts/7"
        reads = []
        pa.ps_rows = lambda *a, **k: reads.append(1) or []
        self.run_twice()
        self.assertEqual(reads, [])
        self.assertEqual(self.kills, [])
        self.assert_said_once()


class RunAgentSweepsEveryStart(unittest.TestCase):
    """R1.1: run_agent calls the sweep before every Claude start (also a retry), before its start line."""

    def test_every_start(self):
        d = os.path.join(pa.ROOT, "spaces", "sp9")
        os.makedirs(d, exist_ok=True)
        prof = os.path.join(pa.ROOT, "profiles", "test")
        os.makedirs(prof, exist_ok=True)
        with open(os.path.join(prof, "profile.json"), "w") as f:
            json.dump({"name": "test", "claude": ["true"], "codex": ["true"]}, f)
        agent = {"version": 1, "space": "sp9", "project": TMP, "profile": "test", "name": "p", "terminal": "t1",
                 "sessionId": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "sessionStarted": False}
        with open(os.path.join(d, "agent.json"), "w") as f:
            json.dump(agent, f)
        order, starts = [], []

        class Stop(Exception):
            pass

        def fake_call(argv, cwd=None):
            order.append("start")
            starts.append(argv)
            if len(starts) >= 2:
                raise Stop()
            return 0
        saved = {k: pa.__dict__.get(k) for k in ("call", "end_leftovers", "snapshot_flag", "cc_version")}
        saved_thread, saved_sleep = pa.threading.Thread, pa.time.sleep
        undo = child_fakes.patch(pa, fake_call)  # replaces end_leftovers with a no-op too: set ours after it
        pa.call = lambda argv, check=True: ""
        pa.end_leftovers = lambda ctx, address: order.append("sweep")
        pa.snapshot_flag, pa.cc_version = (lambda p, ctx=None: False), (lambda p, ctx=None: None)

        class NoThread:
            def __init__(self, target=None, daemon=None):
                pass

            def start(self):
                pass
        pa.threading.Thread = NoThread
        pa.time.sleep = lambda s: None
        try:
            with self.assertRaises(Stop):
                pa.run_agent(types.SimpleNamespace(fresh=False), "t1", TMP, pa.load_profile("test"), d, agent,
                             {"beat": False, "directory": d}, time.time())
        finally:
            undo()
            pa.threading.Thread, pa.time.sleep = saved_thread, saved_sleep
            for k, v in saved.items():
                if v is None:
                    pa.__dict__.pop(k, None)
                else:
                    setattr(pa, k, v)
        self.assertEqual(order, ["sweep", "start", "sweep", "start"])


# ---- R1.4: SIGCONT in end_child ------------------------------------------------------------------------------------

WRAPPER = """#!/bin/sh
{grandchild} &
echo "$!." > "$1"
trap 'exit 255' TERM
wait
"""


class R1_4_EndChildSigcont(unittest.TestCase):
    """R1.4: end_child sends SIGCONT to the group right after SIGTERM: a stopped member ends now, not after the
    grace."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=TMP)
        saved = pa.__dict__.get("foreground_tty")
        pa.foreground_tty = lambda: False
        self.addCleanup(setattr, pa, "foreground_tty", saved)
        grace = pa.END_GRACE
        pa.END_GRACE = 6
        self.addCleanup(setattr, pa, "END_GRACE", grace)

    def test_stopped_member_ends_within_a_few_seconds(self):
        path = os.path.join(self.dir, "wrapper.sh")
        with open(path, "w") as f:
            f.write(WRAPPER.format(grandchild="sh -c 'kill -STOP $$; exec sleep 300'"))
        pidfile = os.path.join(self.dir, "gc.pid")
        ctx = {"directory": self.dir}
        t = threading.Thread(target=lambda: pa.run_child(ctx, ["/bin/sh", path, pidfile], self.dir), daemon=True)
        t.start()
        end = time.time() + 10
        grandchild = None
        while time.time() < end:
            try:
                with open(pidfile) as f:
                    text = f.read().strip()
                if text.endswith("."):
                    grandchild = int(text[:-1])
                    break
            except OSError:
                pass
            time.sleep(0.05)
        self.assertIsNotNone(grandchild)
        self.addCleanup(kill_quietly, grandchild)
        end = time.time() + 5
        while time.time() < end:  # the member has stopped itself
            st = subprocess.run(["ps", "-o", "stat=", "-p", str(grandchild)], stdout=subprocess.PIPE,
                                universal_newlines=True).stdout.strip()
            if st.startswith("T"):
                break
            time.sleep(0.05)
        began = time.time()
        pa.end_child(ctx, "restart")
        took = time.time() - began
        self.assertFalse(alive(grandchild))
        self.assertLess(took, 3, "a stopped member took {:.1f} s: no SIGCONT".format(took))
        t.join(10)


# ---- R2: the launcher cannot be stopped by the terminal; its Claude can --------------------------------------------

class F5_JobControlSignals(unittest.TestCase):
    """F5 (R2.1, R2.2): run_session ignores SIGTTOU/SIGTTIN/SIGTSTP; the child gets SIG_DFL for all three."""

    def setUp(self):
        self.saved = {s: signal.getsignal(s) for s in JOB_SIGNALS + (signal.SIGINT,)}
        self.addCleanup(self.restore)

    def restore(self):
        for s, h in self.saved.items():
            signal.signal(s, h)

    def test_run_session_ignores_the_three(self):
        shutil.rmtree(pa.ROOT, ignore_errors=True)
        d = os.path.join(pa.ROOT, "spaces", "sp1")
        os.makedirs(d)
        project = os.path.join(TMP, "proj5")
        os.makedirs(project, exist_ok=True)
        prof = os.path.join(pa.ROOT, "profiles", "test")
        os.makedirs(prof, exist_ok=True)
        with open(os.path.join(prof, "profile.json"), "w") as f:
            json.dump({"name": "test", "claude": ["true"], "codex": ["true"]}, f)
        saved = {k: getattr(pa, k) for k in ("hc_get", "live_terminals", "call")}
        pa.hc_get, pa.live_terminals, pa.call = (lambda a: {}), (lambda: {}), (lambda argv, check=True: "")
        seen = {}

        class Stop(Exception):
            pass

        def fake_call(argv, cwd=None):
            seen.update({s: signal.getsignal(s) for s in JOB_SIGNALS})
            raise Stop()

        class NoThread:
            def __init__(self, target=None, daemon=None):
                pass

            def start(self):
                pass
        saved_thread = pa.threading.Thread
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
        self.assertEqual(seen, {s: signal.SIG_IGN for s in JOB_SIGNALS})

    def test_child_gets_the_defaults_back(self):
        d = tempfile.mkdtemp(dir=TMP)
        saved = pa.__dict__.get("foreground_tty")
        pa.foreground_tty = lambda: False
        self.addCleanup(setattr, pa, "foreground_tty", saved)
        for s in JOB_SIGNALS:
            signal.signal(s, signal.SIG_IGN)  # as the launcher has them
        out = os.path.join(d, "child.json")
        probe = ("import json, signal, sys; json.dump({str(int(s)): (signal.getsignal(s) == signal.SIG_DFL) for s in "
                 "(signal.SIGTTOU, signal.SIGTTIN, signal.SIGTSTP)}, open(sys.argv[1], 'w'))")
        child = pa.spawn([sys.executable, "-c", probe, out], d)
        child.wait()
        with open(out) as f:
            got = json.load(f)
        self.assertEqual(got, {str(int(s)): True for s in JOB_SIGNALS}, "the child kept the launcher's SIG_IGN")


class R2_3_ReadEio(unittest.TestCase):
    """R2.3: with SIGTTIN ignored, a read of fd 0 from the background gets EIO: wait_enter takes the terminal back
    and reads again once; on a second EIO it sleeps 3 s and loops; it does not exit."""

    def setUp(self):
        import builtins
        saved = (pa.__dict__.get("give_tty"), pa.time.sleep, builtins.input, pa.select.select)
        self.given, self.sleeps = [], []
        pa.give_tty = lambda group: self.given.append(group)

        def undo():
            pa.give_tty, pa.time.sleep, builtins.input, pa.select.select = saved
        self.addCleanup(undo)

    def test_no_ctx_eio_then_line(self):
        import builtins
        reads = [OSError(errno.EIO, "eio"), "yes"]

        def fake_input(*a):
            v = reads.pop(0)
            if isinstance(v, Exception):
                raise v
            return v
        builtins.input = fake_input
        self.assertTrue(pa.wait_enter())
        self.assertEqual(self.given, [os.getpgrp()])

    def test_no_ctx_two_eio_sleep_3_and_loop(self):
        import builtins
        reads = [OSError(errno.EIO, "eio"), OSError(errno.EIO, "eio"), "yes"]

        def fake_input(*a):
            v = reads.pop(0)
            if isinstance(v, Exception):
                raise v
            return v
        builtins.input = fake_input
        pa.time.sleep = lambda s: self.sleeps.append(s)
        self.assertTrue(pa.wait_enter())
        self.assertIn(3, self.sleeps)
        self.assertEqual(reads, [])

    def test_ctx_eio_then_line(self):
        reads = [OSError(errno.EIO, "eio"), "\n"]

        class In:
            def readline(self):
                v = reads.pop(0)
                if isinstance(v, Exception):
                    raise v
                return v

            def fileno(self):
                return 0
        saved = sys.stdin
        sys.stdin = In()
        self.addCleanup(setattr, sys, "stdin", saved)
        pa.select.select = lambda r, w, x, t=None: (r, [], [])
        self.assertTrue(pa.wait_enter({"launcher": {"instance": "i"}}))
        self.assertEqual(self.given, [os.getpgrp()])


class F6_ResetTtyGuards(unittest.TestCase):
    """R3.1: reset_tty writes nothing when fd 0 is not a tty, and never raises."""

    def test_not_a_tty(self):
        r, w = os.pipe()
        saved = os.dup(0)
        os.dup2(r, 0)

        def restore():
            os.dup2(saved, 0)
            for fd in (saved, r, w):
                os.close(fd)
        self.addCleanup(restore)
        writes, real = [], os.write
        pa.os.write = lambda fd, data: writes.append(fd) or real(fd, data)
        self.addCleanup(setattr, pa.os, "write", real)
        pa.reset_tty()  # no raise
        self.assertNotIn(0, writes)
        self.assertEqual(pa.TTY_RESET, TTY_RESET)


if __name__ == "__main__":
    unittest.main()
