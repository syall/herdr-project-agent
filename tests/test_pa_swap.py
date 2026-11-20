"""t-195: a running launcher loads the installed code by itself, without touching Claude (the swap).

While its Claude child runs, the launcher execs the installed pa.py in the same process; execve keeps the pid, so
Claude stays its child and the new image adopts it (spec.md R1-R5, FINDINGS.md). Claude is never signalled, its
terminal never written to, its conversation unchanged.

Run: python3 -m unittest -v tests/test_pa_swap.py
Temp homes only: HERDR_PA_HOME, HERDR_APP_HOME and CLAUDE_CONFIG_DIR point at temp folders before pa.py is loaded, and
every launcher these tests start gets its own temp home and a stub `hc` first on PATH (it logs, it answers nothing).
Every process signalled or exec'd is one these tests started (stand-in shells, helper launchers, stub Claudes, sleep);
the pty tests run on ptys the tests open. No Claude, no real hc.
"""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import pty
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import child_fakes  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PA_PY = os.path.join(REPO, "runtime", "tools", "pa.py")
TMP = os.path.realpath(tempfile.mkdtemp(prefix="t195-swap-"))
os.environ["HERDR_PA_HOME"] = os.path.join(TMP, "pa")
os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(TMP, "claude")
os.environ["HERDR_APP_HOME"] = os.path.join(TMP, "app")
for k in ("HC_TERMINAL_ID", "CLAUDECODE"):
    os.environ.pop(k, None)
spec = importlib.util.spec_from_file_location("pa", PA_PY)
pa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pa)
assert pa.ROOT.startswith(TMP) and pa.CLAUDE_HOME.startswith(TMP), "pa.py must use the temp homes"
SPAWN = pa.spawn  # the real one, before any test replaces it

SP = "sp1"
SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
TTY_RESET = ("\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1005l\x1b[?1006l\x1b[?1015l\x1b[?1004l\x1b[?2004l\x1b[<u"
             "\x1b[?25h\x1b[?1049l")


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def load(path):
    with open(path) as f:
        return json.load(f)


def dump(value, path):
    write(path, json.dumps(value))


def read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def lines(path):
    out = []
    for line in read(path).splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        universal_newlines=True).stdout.strip()
    return bool(st) and not st.startswith("Z")


def kill_group(pgid):
    """SIGKILL to a group these tests started (never this process's own, never 0 or 1)."""
    if isinstance(pgid, int) and pgid > 1 and pgid != os.getpgrp():
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass


def kill_pid(pid):
    if isinstance(pid, int) and pid > 1 and pid != os.getpid():
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def wait_until(pred, timeout, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        value = pred()
        if value:
            return value
        time.sleep(step)
    return pred()


def install(home, comment=None, at=None):
    """The temp home's tools/ (the repo's tools, plus `comment` appended to pa.py), core/ and a test profile, and
    installed.json stamped for them with `at` (UTC ISO; default now). Returns the tools hash."""
    tools = os.path.join(home, "tools")
    os.makedirs(tools, exist_ok=True)
    for name in os.listdir(os.path.join(REPO, "runtime", "tools")):
        if name.endswith(".py"):
            shutil.copy2(os.path.join(REPO, "runtime", "tools", name), os.path.join(tools, name))
    if comment:
        with open(os.path.join(tools, "pa.py"), "a") as f:
            f.write("\n# {}\n".format(comment))
    write(os.path.join(home, "core", "orchestrator.md"), "# o\n")
    return stamp(home, at)


def stamp(home, at=None, **over):
    value = {"version": 1, "at": at or pa.now(), "by": "install.sh", "source": None,
             "tools": pa.tree_hash(os.path.join(home, "tools"), top_py_only=True),
             "core": pa.tree_hash(os.path.join(home, "core")), "mods": None, "syncedFrom": None}
    value.update(over)
    dump(value, os.path.join(home, "installed.json"))
    return value["tools"]


# ---- the pty harness ------------------------------------------------------------------------------------------------

# A stand-in job-control shell (zsh in a Herdr tab): the pty's session leader; runs the launcher as its foreground job.
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
with open(out + ".pid", "w") as f:
    f.write(str(pid))
res, end = {"state": "timeout"}, time.time() + 120
while time.time() < end:
    p, st = os.waitpid(pid, os.WUNTRACED | os.WNOHANG)
    if p == 0:
        time.sleep(0.05)
        continue
    if os.WIFSTOPPED(st):
        res = {"state": "stopped", "signal": os.WSTOPSIG(st)}
    elif os.WIFEXITED(st):
        res = {"state": "exited", "code": os.WEXITSTATUS(st)}
    else:
        res = {"state": "signalled", "signal": os.WTERMSIG(st)}
    break
with open(out, "w") as f:
    json.dump(res, f)
"""

# The stub Claude: answers the launcher's probes; else records its start and every catchable signal it gets, writes
# a tick to the terminal every 0.2 s, and exits STUB_EXIT after STUB_RUN s (its first start only; later starts wait).
STUB = r"""
import json, os, signal, sys, time
if sys.argv[1:2] in (["--help"], ["--version"]):
    print("2.1.300 (Claude Code)")
    sys.exit(0)
d = os.environ["T195_DIR"]
def log(name, value):
    with open(os.path.join(d, name), "a") as f:
        f.write(json.dumps(value) + "\n")
def on(sig, _frame):
    log("stub-signals.jsonl", {"pid": os.getpid(), "sig": signal.Signals(sig).name, "at": time.time()})
    if sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        os._exit(143)
for name in ("SIGTERM", "SIGINT", "SIGHUP", "SIGCONT", "SIGUSR1", "SIGUSR2", "SIGTTOU", "SIGTTIN", "SIGTSTP", "SIGQUIT",
             "SIGWINCH", "SIGALRM", "SIGPIPE"):
    try:
        signal.signal(getattr(signal, name), on)
    except (OSError, ValueError, AttributeError):
        pass
sid, prev = None, None
for a in sys.argv[1:]:
    if prev in ("--session-id", "--resume"):
        sid = a
    prev = a
try:
    n = len(open(os.path.join(d, "stub-starts.jsonl")).read().splitlines())
except OSError:
    n = 0
try:
    fg = os.tcgetpgrp(0)
except OSError:
    fg = None
log("stub-starts.jsonl", {"pid": os.getpid(), "pgrp": os.getpgrp(), "fg": fg, "argv": sys.argv[1:], "at": time.time(),
                          "n": n, "session": sid})
if sid and os.environ.get("CLAUDE_CONFIG_DIR"):  # the session exists: a transcript, so the next start resumes it
    import re
    folder = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects", re.sub(r"[^A-Za-z0-9]", "-", os.getcwd()))
    os.makedirs(folder, exist_ok=True)
    open(os.path.join(folder, sid + ".jsonl"), "a").close()
run = float(os.environ.get("STUB_RUN", "8")) if n == 0 else 3600
end, i = time.time() + run, 0
while time.time() < end:
    if n == 0 and os.path.exists(os.path.join(d, "exit-now")):
        log("stub-exits.jsonl", {"pid": os.getpid(), "code": 9, "at": time.time()})
        sys.exit(9)
    os.write(1, b"tick %d\n" % i)
    i += 1
    time.sleep(0.2)
log("stub-exits.jsonl", {"pid": os.getpid(), "code": int(os.environ.get("STUB_EXIT", "7")), "at": time.time()})
sys.exit(int(os.environ.get("STUB_EXIT", "7")))
"""

HC = """#!/bin/sh
printf '%s\\n' "$*" >> "$T195_DIR/hc.log"
case "$1" in
  get) echo '{}' ;;
  ls) echo '{"terminals": []}' ;;
esac
exit 0
"""

# The old image: pa.py loaded from the temp home's tools/ with a short SWAP_EVERY, then `pa.py session` as pa-session
# runs it. Modes: plain | exit (the stub exits right before the exec: SWAP_HOOK) | request (a restart request for
# this instance is written right before the exec).
OLD = r"""
import importlib.util, json, os, subprocess, sys, time
pa_py, mode, project = sys.argv[1], sys.argv[2], sys.argv[3]
d = os.environ["T195_DIR"]
spec = importlib.util.spec_from_file_location("pa", pa_py)
pa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pa)
pa.SWAP_EVERY = 0.3

def hook_exit(ctx, child):
    open(os.path.join(d, "exit-now"), "w").close()
    end = time.time() + 5
    while time.time() < end:
        st = subprocess.run(["ps", "-o", "stat=", "-p", str(child.pid)], stdout=subprocess.PIPE,
                            universal_newlines=True).stdout.strip()
        if not st or st.startswith("Z"):
            break
        time.sleep(0.05)

def hook_request(ctx, child):
    at = time.time()
    req = {"version": 1, "id": "r-window", "space": "sp1", "instance": ctx["launcher"]["instance"], "terminal": "t1",
           "at": pa.iso_time(at), "expires": pa.iso_time(at + 600), "by": "pa-restart", "why": "test", "now": True,
           "fresh": False}
    with open(os.path.join(pa.ROOT, "spaces", "sp1", "restart-request.json"), "w") as f:
        json.dump(req, f)

if mode == "exit":
    pa.SWAP_HOOK = hook_exit
elif mode == "request":
    pa.SWAP_HOOK = hook_request
sys.argv = [pa_py, "session", "--space=sp1", "--project=" + project, "--profile=test", "--name=proj"]
try:
    pa.main()
except pa.Problem as e:
    print("pa-session: {}".format(e), file=sys.stderr)
    sys.exit(2)
"""


class Pty:
    """One launcher on a pty of the test's own, under the stand-in shell, in its own temp home."""

    def __init__(self, test, mode="plain", stub_run=8, stub_exit=7):
        self.test, self.mode = test, mode
        self.base = os.path.realpath(tempfile.mkdtemp(dir=TMP))
        self.home = os.path.join(self.base, "pa")
        self.project = os.path.join(self.base, "proj")
        os.makedirs(self.project)
        self.space = os.path.join(self.home, "spaces", SP)
        self.old_tools = install(self.home)
        for name, text in (("shell.py", SHELL), ("stub.py", STUB), ("old.py", OLD), ("bin/hc", HC)):
            write(os.path.join(self.base, name), text)
        os.chmod(os.path.join(self.base, "bin", "hc"), 0o755)
        write(os.path.join(self.home, "profiles", "test", "profile.json"), json.dumps(
            {"name": "test", "claude": [sys.executable, os.path.join(self.base, "stub.py")], "codex": ["true"]}))
        write(os.path.join(self.home, "profiles", "test", "context.md"), "ctx\n")
        os.makedirs(self.space, exist_ok=True)
        dump({"version": 1, "space": SP, "project": self.project, "profile": "test", "name": "proj", "terminal": "t1",
              "terminals": ["t1"], "sessionId": SID, "sessionStarted": False}, os.path.join(self.space, "agent.json"))
        self.env = {k: v for k, v in os.environ.items() if k not in ("HC_TERMINAL_ID", "CLAUDECODE")}
        self.env.update(HERDR_PA_HOME=self.home, HERDR_APP_HOME=os.path.join(self.base, "app"),
                        CLAUDE_CONFIG_DIR=os.path.join(self.base, "claude"), HC_TERMINAL_ID="t1", T195_DIR=self.base,
                        PATH=os.path.join(self.base, "bin") + os.pathsep + os.environ.get("PATH", ""),
                        STUB_RUN=str(stub_run), STUB_EXIT=str(stub_exit))
        self.shown, self.lock, self.proc, self.master = b"", threading.Lock(), None, None
        test.addCleanup(self.stop)

    def start(self):
        master, slave = pty.openpty()
        self.master = master
        out = os.path.join(self.base, "shell.json")
        self.err = open(os.path.join(self.base, "shell.err"), "w")
        self.proc = subprocess.Popen([sys.executable, os.path.join(self.base, "shell.py"), out, sys.executable,
                                      os.path.join(self.base, "old.py"), os.path.join(self.home, "tools", "pa.py"),
                                      self.mode, self.project],
                                     stdin=slave, stdout=slave, stderr=self.err, start_new_session=True, cwd=self.project,
                                     env=self.env)
        os.close(slave)
        self.reader = threading.Thread(target=self.read_loop, daemon=True)
        self.reader.start()
        return self

    def read_loop(self):
        while True:
            try:
                if not select.select([self.master], [], [], 0.1)[0]:
                    if self.proc.poll() is not None and self.master is None:
                        return
                    continue
                chunk = os.read(self.master, 65536)
            except (OSError, ValueError, TypeError):
                return
            if not chunk:
                return
            with self.lock:
                self.shown += chunk

    def text(self):
        with self.lock:
            return self.shown

    def launcher_pid(self):
        try:
            return int(read(os.path.join(self.base, "shell.json.pid")).strip())
        except ValueError:
            return None

    def record(self):
        try:
            return load(os.path.join(self.space, "launcher.json"))
        except (OSError, ValueError):
            return {}

    def log(self):
        return read(os.path.join(self.space, "session.log"))

    def starts(self):
        return lines(os.path.join(self.base, "stub-starts.jsonl"))

    def signals(self):
        return lines(os.path.join(self.base, "stub-signals.jsonl"))

    def hc(self):
        return read(os.path.join(self.base, "hc.log"))

    def install_new(self, comment="t-195 swap test", age=120):
        """The installed tools change (pa.py + a comment), stamped `age` s ago."""
        with open(os.path.join(self.home, "tools", "pa.py"), "a") as f:
            f.write("\n# {}\n".format(comment))
        self.new_tools = stamp(self.home, pa.iso_time(time.time() - age))
        return self.new_tools

    def request(self, rid, now=False):
        at = time.time()
        rec = self.record()
        dump({"version": 1, "id": rid, "space": SP, "instance": rec["instance"], "terminal": "t1", "at": pa.iso_time(at),
              "expires": pa.iso_time(at + 600), "by": "pa-restart", "why": "test", "now": now, "fresh": False},
             os.path.join(self.space, "restart-request.json"))

    def stop(self):
        for s in self.starts():
            kill_group(s.get("pgrp"))
            kill_pid(s.get("pid"))
        launcher = self.launcher_pid()
        kill_group(launcher)
        kill_pid(launcher)
        if self.proc is not None:
            if self.proc.poll() is None:
                kill_group(self.proc.pid)
                self.proc.kill()
            self.proc.wait()
        master, self.master = self.master, None
        if master is not None:
            try:
                os.close(master)
            except OSError:
                pass
        if getattr(self, "err", None):
            self.err.close()

    def explain(self):
        return "\nsession.log:\n{}\nshell.err:\n{}\nhc:\n{}\npty:\n{!r}".format(
            self.log()[-3000:], read(os.path.join(self.base, "shell.err"))[-3000:], self.hc()[-1500:], self.text()[-1500:])


def first_stub_window(shown):
    """What the pty showed from the first stub tick to its last one, the ticks taken out."""
    first = shown.find(b"tick 0")
    if first < 0:
        return None
    ticks = list(re.finditer(rb"tick (\d+)\r?\n", shown[first:]))
    last, n = first, -1
    for m in ticks:
        if int(m.group(1)) != n + 1:  # a later start's ticks begin at 0 again
            break
        n = int(m.group(1))
        last = first + m.end()
    return re.sub(rb"tick \d+\r?\n", b"", shown[first:last])


class S1_S2_SwapWhileClaudeRuns(unittest.TestCase):
    """S1 (R2, R5, R1): an install while Claude runs: the launcher loads it in place; Claude is not touched.
    S2 (R2.5.5): when the adopted Claude exits, the new image handles the exit from its real start and resumes it."""

    def setUp(self):
        r = self.run_ = Pty(self, stub_run=8, stub_exit=7).start()
        # installed 2 s into the run: an exit measured from the swap instead of the real start would read <= 6 s
        self.assertTrue(wait_until(lambda: len(r.starts()) == 1 and b"tick 10" in r.text(), 20), r.explain())
        self.before = r.record()
        self.agent_before = load(os.path.join(r.space, "agent.json"))
        self.stub = r.starts()[0]
        r.install_new()

    def test_s1_s2(self):
        r = self.run_
        swapped = wait_until(lambda: r.record().get("code", {}).get("tools") == r.new_tools, 6)
        self.assertTrue(swapped, "the launcher did not load the installed tools while Claude ran" + r.explain())
        after = r.record()
        self.assertEqual(after["pid"], self.before["pid"], "the same process (exec in place)")
        self.assertEqual(after["pid"], r.launcher_pid())
        self.assertEqual(after["instance"], self.before["instance"], "the instance is kept")
        self.assertEqual(after["started"], self.before["started"])
        self.assertEqual(after["session"], self.before["session"], "the session record is carried unchanged")
        self.assertIn("swap", after["accepts"])
        self.assertEqual(after["swap"]["count"], 1)
        self.assertEqual(after["swap"]["fromTools"], r.old_tools)
        self.assertIsNone(after["swapFailed"])
        self.assertEqual(after["code"]["installed"], r.new_tools)
        wait_until(lambda: "swap adopted" in r.log(), 5)  # logged right after launcher.json is written
        self.assertIn("swap adopted pid={} pgid={} from={} to={}".format(
            self.stub["pid"], self.stub["pgrp"], r.old_tools[:12], r.new_tools[:12]), r.log())
        self.assertEqual(len(r.starts()), 1, "no second Claude")
        self.assertTrue(alive(self.stub["pid"]), "Claude still runs")
        self.assertFalse(os.path.exists(os.path.join(r.space, "launcher-swap.json")), "the state file is removed")
        # S2: the stub exits 7 at 8 s; the new image logs it from the real start and resumes the same session
        self.assertTrue(wait_until(lambda: "exit code=7" in r.log(), 15), r.explain())
        m = re.search(r"exit code=7 after=(\d+)s", r.log())
        self.assertTrue(m, r.log())
        self.assertGreaterEqual(int(m.group(1)), 8, "lasted is measured from the real start, not the swap")
        self.assertTrue(wait_until(lambda: len(r.starts()) == 2, 15), r.explain())
        second = r.starts()[1]
        self.assertIn("--resume", second["argv"])
        self.assertEqual(second["session"], self.agent_before["sessionId"])
        # R5.1: no signal, agent.json's conversation fields unchanged across the swap
        self.assertEqual(r.signals(), [], "Claude got a signal")
        agent = load(os.path.join(r.space, "agent.json"))
        for key in ("sessionId", "previousSessions", "prompt"):
            self.assertEqual(agent.get(key), self.agent_before.get(key), key)
        # no "starting" label from the new image (only the old image's first start)
        self.assertEqual(r.hc().count("herdr.pa.starting=" + SP), 1, r.hc())
        # nothing but the stub's own bytes reached the terminal while it ran
        window = first_stub_window(r.text())
        self.assertEqual(window, b"", "the launcher wrote to Claude's terminal: {!r}".format(window))
        self.assertEqual(self.stub["fg"], self.stub["pgrp"], "Claude had the terminal")


class S3_ExitInTheWindow(unittest.TestCase):
    """S3 (R2.5.2): Claude exits right before the exec: the new image adopts it as a zombie and handles its exit."""

    def test_zombie_adopted(self):
        r = Pty(self, mode="exit", stub_run=30).start()
        self.assertTrue(wait_until(lambda: len(r.starts()) == 1 and b"tick 1" in r.text(), 20), r.explain())
        stub = r.starts()[0]
        r.install_new()
        self.assertTrue(wait_until(lambda: "exit code=9" in r.log(), 10), "no exit handled" + r.explain())
        log = r.log()
        self.assertIn("swap adopted pid={}".format(stub["pid"]), log)
        self.assertLess(log.index("swap adopted"), log.index("exit code=9"))
        self.assertEqual(r.record().get("code", {}).get("tools"), r.new_tools)
        self.assertEqual(r.signals(), [])
        self.assertTrue(wait_until(lambda: len(r.starts()) == 2, 15), "the next start runs" + r.explain())


class S8_RequestInTheWindow(unittest.TestCase):
    """S8 (R5.2): a restart request for the instance written between the swap's last check and the exec is the new
    image's own: it ends the adopted Claude by its group and restarts."""

    def test_taken_by_the_new_image(self):
        r = Pty(self, mode="request", stub_run=60).start()
        self.assertTrue(wait_until(lambda: len(r.starts()) == 1 and b"tick 1" in r.text(), 20), r.explain())
        stub, before = r.starts()[0], r.record()
        r.install_new()
        self.assertTrue(wait_until(lambda: (r.record().get("restart") or {}).get("request") == "r-window"
                                   and r.record()["restart"].get("outcome") == "ok", 25), r.explain())
        wait_until(lambda: "restarted request=r-window" in r.log(), 5)  # logged right after launcher.json
        log = r.log()
        self.assertIn("swap adopted", log)
        self.assertLess(log.index("swap adopted"), log.index("restart taken request=r-window"))
        self.assertIn("restarted request=r-window from={}".format(before["instance"]), log)
        self.assertEqual(r.record()["pid"], before["pid"])
        self.assertIn("SIGTERM", [s["sig"] for s in r.signals()])
        self.assertFalse(alive(stub["pid"]))
        self.assertTrue(wait_until(lambda: len(r.starts()) == 2, 15), r.explain())


class S9_RestartAfterASwap(unittest.TestCase):
    """S9 (R2.7): Restart and Restart now after a swap: end_child on the adopted Claude ends its group (TERM, then
    CONT), collects its code; the terminal is given back and reset; the restart runs."""

    def run_restart(self, now):
        r = Pty(self, stub_run=60).start()
        self.assertTrue(wait_until(lambda: len(r.starts()) == 1 and b"tick 1" in r.text(), 20), r.explain())
        stub = r.starts()[0]
        r.install_new()
        self.assertTrue(wait_until(lambda: (r.record().get("swap") or {}).get("count") == 1, 8), r.explain())
        r.request("r-after", now=now)
        self.assertTrue(wait_until(lambda: (r.record().get("restart") or {}).get("request") == "r-after"
                                   and r.record()["restart"].get("outcome") == "ok", 25), r.explain())
        sigs = [s["sig"] for s in r.signals()]
        self.assertIn("SIGTERM", sigs)  # with t-193's SIGCONT; macOS may deliver the CONT first
        self.assertLessEqual(set(sigs), {"SIGTERM", "SIGCONT"}, sigs)
        self.assertFalse(alive(stub["pid"]), "the adopted Claude was ended")
        log = r.log()
        self.assertRegex(log, r"exit code=143 after=\d+s ended=restart")
        self.assertNotIn("processes still in group", log)
        self.assertTrue(wait_until(lambda: len(r.starts()) == 2, 15), r.explain())
        shown = r.text()
        self.assertGreater(shown.find(TTY_RESET.encode(), shown.find(b"tick 0")), 0, "t-193's reset after the adopted Claude")

    def test_restart(self):
        self.run_restart(False)

    def test_restart_now(self):
        self.run_restart(True)


# ---- in-process: the swap's checks (S4) ---------------------------------------------------------------------------

MISSING = object()


class Execd(BaseException):  # not an Exception: swap_once's own guard must not swallow the recorded exec
    pass


class Running:
    """A Claude handle that runs (poll None) until told otherwise; never signalled here."""

    def __init__(self, pid=4242, code=None):
        self.pid, self.pa_group, self.pa_tty, self.returncode, self.signals = pid, pid, False, code, []

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None and timeout is not None:
            raise subprocess.TimeoutExpired("claude", timeout)
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)

    def kill(self):
        self.send_signal(signal.SIGKILL)


class Base(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(pa.ROOT, ignore_errors=True)
        shutil.rmtree(pa.APP, ignore_errors=True)
        self.old_tools = install(pa.ROOT)
        write(os.path.join(pa.ROOT, "profiles", "test", "profile.json"),
              json.dumps({"name": "test", "claude": ["true"], "codex": ["true"]}))
        write(os.path.join(pa.ROOT, "profiles", "test", "context.md"), "ctx\n")
        self.dir = os.path.join(pa.ROOT, "spaces", SP)
        os.makedirs(self.dir, exist_ok=True)
        self.project = os.path.join(TMP, "proj")
        os.makedirs(self.project, exist_ok=True)
        dump({"version": 1, "space": SP, "project": self.project, "profile": "test", "name": "proj", "terminal": "t1",
              "terminals": ["t1"], "sessionId": SID, "sessionStarted": True}, os.path.join(self.dir, "agent.json"))
        self.saved = {k: pa.__dict__.get(k, MISSING) for k in ("call", "hc_get", "live_terminals", "__file__",
                                                                "restart_precheck", "SWAP_RETRY", "SWAP_SETTLE",
                                                                "SWAP_EVERY", "SWAP_HOOK")}
        self.calls = []
        pa.call = lambda argv, check=True: self.calls.append(list(argv)) or ""
        pa.hc_get = lambda address: {}
        pa.live_terminals = lambda: {}
        self.addCleanup(child_fakes.unfaked(pa))
        self.saved_exec = os.execv
        self.execs = []

        def recorder(path, argv):
            self.execs.append((path, list(argv)))
            raise Execd()
        os.execv = recorder
        self.addCleanup(self.restore)

    def restore(self):
        os.execv = self.saved_exec
        for k, v in self.saved.items():
            if v is MISSING:
                pa.__dict__.pop(k, None)
            else:
                setattr(pa, k, v)

    def ctx(self, child=None):
        ctx = {}
        pa.__file__ = os.path.join(pa.ROOT, "tools", "pa.py")
        with contextlib.redirect_stdout(io.StringIO()):
            pa.launcher_start(ctx, "t1", SP, self.dir, pa.load_profile("test"))
        ctx.update(beat=True, previous=None, directory=self.dir, project=self.project,
                   child=child or Running(), childGroup=(child or Running()).pid,
                   running={"since": time.time() - 30, "session": SID, "marked": True})
        ctx["childGroup"] = ctx["child"].pid
        return ctx

    def new_install(self, age=120, at=None):
        with open(os.path.join(pa.ROOT, "tools", "pa.py"), "a") as f:
            f.write("\n# new {}\n".format(uuid.uuid4()))
        self.new_tools = stamp(pa.ROOT, at if at is not None else pa.iso_time(time.time() - age))
        return self.new_tools

    def swap(self, ctx):
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                pa.swap_once(ctx, ctx["child"])
        except Execd:
            return True
        return False

    def counting_precheck(self, why=None):
        seen = []

        def precheck(ctx, space, profile, argv=None):
            seen.append(list(argv or []))
            if why:
                return None, why
            return {"tools": pa.hash_or_none(os.path.join(pa.ROOT, "tools"), True),
                    "core": pa.hash_or_none(os.path.join(pa.ROOT, "core")), "argv": list(argv)}, None
        pa.restart_precheck = precheck
        return seen

    def log(self):
        return read(os.path.join(self.dir, "session.log"))

    def record(self):
        return load(os.path.join(self.dir, "launcher.json"))


class S4_WhenItSwaps(Base):
    """S4 (R2.2): every condition, each miss means no exec and the wait goes on."""

    def test_swaps_with_the_real_self_check(self):
        ctx = self.ctx()
        self.new_install()
        self.assertTrue(self.swap(ctx), self.log())
        path, argv = self.execs[0]
        self.assertEqual(path, sys.executable)
        self.assertEqual(argv[:3], [sys.executable, os.path.join(pa.ROOT, "tools", "pa.py"), "session"])
        self.assertIn("--swap-child=4242:4242", argv)
        ids = [a for a in argv if a.startswith("--swap-id=")]
        self.assertEqual(len(ids), 1)
        self.assertFalse(any(a.startswith("--restarted") or a == "--fresh" for a in argv), argv)
        self.assertTrue(pa.pa_session_argv(argv, SP), "t-193 --recover still finds it (S11)")
        # the state file is there for the new image (the recorded exec did not return), SIGINT is restored here
        self.assertEqual(load(os.path.join(self.dir, pa.SWAP_FILE))["id"], ids[0].split("=", 1)[1])
        self.assertIs(signal.getsignal(signal.SIGINT), signal.default_int_handler)

    def test_state_file_written_before_the_exec(self):
        ctx = self.ctx()
        ctx["child"].pa_tty = True
        self.new_install()
        self.counting_precheck()
        seen = {}

        def hook(c, child):
            seen["state"] = load(os.path.join(self.dir, pa.SWAP_FILE))
            seen["mode"] = stat.S_IMODE(os.stat(os.path.join(self.dir, pa.SWAP_FILE)).st_mode)
            seen["sigint"] = signal.getsignal(signal.SIGINT)
        pa.SWAP_HOOK = hook
        self.assertTrue(self.swap(ctx))
        state = seen["state"]
        sid = [a for a in self.execs[0][1] if a.startswith("--swap-id=")][0].split("=", 1)[1]
        self.assertEqual(state["version"], 1)
        self.assertEqual(state["id"], sid)
        self.assertEqual((state["pid"], state["pgid"], state["tty"]), (4242, 4242, True))
        self.assertEqual(state["instance"], ctx["launcher"]["instance"])
        self.assertEqual(state["fromTools"], self.old_tools)
        self.assertEqual(state["running"]["since"], ctx["running"]["since"])
        self.assertEqual(state["session"], ctx["launcher"]["session"])
        self.assertEqual(state["swapCount"], 0)
        self.assertEqual(seen["mode"], 0o600)
        self.assertEqual(seen["sigint"], signal.SIG_IGN)

    def test_no_swap_when_installed_is_mine(self):
        ctx = self.ctx()
        seen = self.counting_precheck()
        self.assertFalse(self.swap(ctx))
        self.assertEqual(seen, [])

    def test_no_swap_before_it_settled(self):
        ctx = self.ctx()
        self.new_install(age=10)
        seen = self.counting_precheck()
        self.assertFalse(self.swap(ctx))
        self.assertEqual(seen, [])

    def test_garbled_at_waits_for_the_mtime(self):
        ctx = self.ctx()
        self.new_install(at="garbled")
        seen = self.counting_precheck()
        self.assertFalse(self.swap(ctx), "a fresh mtime: not settled")
        self.assertEqual(seen, [])
        old = time.time() - 120
        os.utime(os.path.join(pa.ROOT, "installed.json"), (old, old))
        self.assertTrue(self.swap(ctx), "an old mtime: settled")

    def test_future_at_waits_for_the_mtime(self):
        ctx = self.ctx()
        self.new_install(at=pa.iso_time(time.time() + 600))
        self.counting_precheck()
        self.assertFalse(self.swap(ctx))
        old = time.time() - 120
        os.utime(os.path.join(pa.ROOT, "installed.json"), (old, old))
        self.assertTrue(self.swap(ctx))

    def test_no_swap_with_a_request_file(self):
        for name, text in (("restart-request.json", "{not json"), ("context-request.json", "{}")):
            ctx = self.ctx()
            self.new_install()
            seen = self.counting_precheck()
            write(os.path.join(self.dir, name), text)
            self.assertFalse(self.swap(ctx), name)
            self.assertEqual(seen, [], name)
            os.remove(os.path.join(self.dir, name))

    def test_no_swap_while_the_launcher_is_busy(self):
        for key, value in (("after", {"kind": "exec"}), ("restarting", "r1"), ("waitingFor", "r1"), ("ended", "restart"),
                           ("freshNext", {"reason": "x"})):
            ctx = self.ctx()
            self.new_install()
            seen = self.counting_precheck()
            ctx[key] = value
            self.assertFalse(self.swap(ctx), key)
            self.assertEqual(seen, [], key)

    def test_no_swap_while_an_install_is_half_done(self):
        ctx = self.ctx()
        self.new_install()
        with open(os.path.join(pa.ROOT, "tools", "pa.py"), "a") as f:
            f.write("# more\n")  # the files no longer match installed.json
        self.assertFalse(self.swap(ctx))
        self.assertIsNone(self.record()["swapFailed"])
        self.assertNotIn("swap", self.log().replace("swap=", ""))

    def test_failed_self_check_is_said_once_and_retried_after_swap_retry(self):
        ctx = self.ctx()
        new = self.new_install()
        seen = self.counting_precheck("the installed launcher's self-check failed (exit 1): boom")
        pa.SWAP_RETRY = 0.6
        for _ in range(3):
            self.assertFalse(self.swap(ctx))
        self.assertEqual(len(seen), 1, "tried once within SWAP_RETRY")
        failed = self.record()["swapFailed"]
        self.assertEqual(failed["installed"], new)
        self.assertIn("self-check failed (exit 1): boom", failed["why"])
        self.assertTrue(pa.parse_time(failed["at"]))
        self.assertEqual(self.log().count("swap not done"), 1, self.log())
        time.sleep(0.7)
        self.assertFalse(self.swap(ctx))
        self.assertEqual(len(seen), 2, "retried after SWAP_RETRY")
        self.assertEqual(self.log().count("swap not done"), 1, "said once per installed hash")
        # installed equal to mine again: swapFailed cleared
        stamp(pa.ROOT, pa.iso_time(time.time() - 120), tools=self.old_tools)
        self.assertFalse(self.swap(ctx))
        self.assertIsNone(self.record()["swapFailed"])

    def test_exec_raising_keeps_waiting(self):
        ctx = self.ctx()
        self.new_install()
        self.counting_precheck()

        def broken(path, argv):
            raise OSError(8, "Exec format error")
        os.execv = broken
        self.assertFalse(self.swap(ctx))
        self.assertIs(signal.getsignal(signal.SIGINT), signal.default_int_handler, "SIGINT restored")
        self.assertFalse(os.path.exists(os.path.join(self.dir, pa.SWAP_FILE)), "state file removed")
        failed = self.record()["swapFailed"]
        self.assertIn("Exec format error", failed["why"])
        self.assertEqual(ctx["child"].signals, [])

    def test_no_swap_when_claude_exited_meanwhile(self):  # mutation: drop the child.poll() re-check
        ctx = self.ctx(child=Running(code=0))
        self.new_install()
        self.counting_precheck()
        self.assertFalse(self.swap(ctx))
        self.assertFalse(os.path.exists(os.path.join(self.dir, pa.SWAP_FILE)))

    def test_no_swap_when_the_tools_change_after_the_check(self):
        ctx = self.ctx()
        self.new_install()
        real = self.counting_precheck()

        def precheck(c, space, profile, argv=None):
            got = pa.__dict__["_t195_inner"](c, space, profile, argv)
            with open(os.path.join(pa.ROOT, "tools", "pa.py"), "a") as f:
                f.write("# changed after the check\n")
            return got
        pa._t195_inner = pa.restart_precheck
        pa.restart_precheck = precheck
        self.addCleanup(pa.__dict__.pop, "_t195_inner", None)
        self.assertFalse(self.swap(ctx))
        self.assertEqual(len(real), 1)

    def test_never_raises(self):
        ctx = self.ctx()
        self.new_install()

        def boom(*a, **k):
            raise RuntimeError("boom")
        pa.restart_precheck = boom
        self.assertFalse(self.swap(ctx))

    def test_run_child_waits_in_slices(self):  # R2.1: run_child calls swap_once at each timeout, same return value
        calls = []
        pa.SWAP_EVERY = 0.05
        saved = pa.swap_once
        pa.swap_once = lambda ctx, child: calls.append(child.pid)
        self.addCleanup(setattr, pa, "swap_once", saved)
        saved_fg = pa.foreground_tty
        pa.foreground_tty = lambda: False
        self.addCleanup(setattr, pa, "foreground_tty", saved_fg)
        pa.spawn = SPAWN
        ctx = {"directory": self.dir}
        code = pa.run_child(ctx, [sys.executable, "-c", "import time; time.sleep(0.4); raise SystemExit(5)"], self.dir)
        self.assertEqual(code, 5)
        self.assertGreaterEqual(len(calls), 3)


# ---- the argv (S5) ------------------------------------------------------------------------------------------------

HOSTILE_CHILD = ["abc", "", "12", "12:", ":12", "-5:5", "5:-5", "0:0", "1:1", "+12:12", " 12:12", "12:12 ", "12 :12",
                 "012:12", "١٢:١٢", "1١:1١", "12:1٢", "12:12:12", "99999999999:1", "2147483648:1", "1:2147483648",
                 "12:12\n"]
HOSTILE_ID = ["-x", "../a", "", "a" * 81, "a b"]


class S5_HostileArgv(Base):
    """S5: every malformed --swap-child / --swap-id is refused by the installed launcher's --self-check."""

    def self_check(self, *flags):
        argv = [sys.executable, os.path.join(pa.ROOT, "tools", "pa.py"), "session", "--space=" + SP,
                "--project=" + self.project, "--profile=test", "--name=proj"] + list(flags) + ["--self-check"]
        env = dict(os.environ, HERDR_PA_HOME=pa.ROOT, HERDR_APP_HOME=pa.APP, PATH="/usr/bin:/bin")
        return subprocess.run(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                              timeout=60)

    def test_a_valid_swap_argv_passes(self):
        for value in ("12:12", "2147483647:2147483647", "4242:99"):
            r = self.self_check("--swap-child=" + value, "--swap-id=" + str(uuid.uuid4()))
            self.assertEqual(r.returncode, 0, (value, r.stderr[-1500:]))
            self.assertTrue(json.loads(r.stdout.strip().splitlines()[-1])["ok"])

    def test_hostile_swap_child_refused(self):
        for value in HOSTILE_CHILD + ["{}:{}".format(os.getpid(), os.getpgrp())]:  # the self-check's parent
            r = self.self_check("--swap-child=" + value, "--swap-id=" + str(uuid.uuid4()))
            self.assertNotEqual(r.returncode, 0, repr(value))

    def test_hostile_swap_id_refused(self):
        for value in HOSTILE_ID:
            r = self.self_check("--swap-child=12:12", "--swap-id=" + value)
            self.assertNotEqual(r.returncode, 0, repr(value))

    def test_shapes_refused(self):
        sid = "--swap-id=" + str(uuid.uuid4())
        for flags in (["--swap-child=12:12", "--swap-child=13:13", sid],  # given twice
                      ["--swap-child", "--space=" + SP, sid],  # an option-like value
                      ["--swap-child=12:12"],  # no id
                      [sid],  # no child
                      ["--swap-child=12:12", sid, "--swap-id=" + str(uuid.uuid4())]):
            r = self.self_check(*flags)
            self.assertNotEqual(r.returncode, 0, flags)

    def test_parse_swap_args(self):
        sid = str(uuid.uuid4())
        self.assertEqual(pa.parse_swap_args("12:34", sid)[0], (12, 34, sid))
        for value in HOSTILE_CHILD + [None, "{}:{}".format(os.getpid(), os.getpgrp()), "{}:1".format(os.getppid())]:
            got, why = pa.parse_swap_args(value, sid)
            self.assertIsNone(got, repr(value))
            self.assertTrue(why)
        for value in HOSTILE_ID + [None]:
            self.assertIsNone(pa.parse_swap_args("12:12", value)[0], repr(value))


# The new image started with a bad --swap-child by a process that has a real child of its own (what an exec leaves):
# it adopts nothing, reaps that child first, and only then starts its Claude.
REAPER = r"""
import os, subprocess, sys, time
d, pa_py, project, value = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
child = subprocess.Popen([sys.executable, "-c", "import sys, time; time.sleep(2); "
                          "open(sys.argv[1], 'w').write(repr(time.time()))", os.path.join(d, "child-end")])
value = value.replace("OWN", "{}:{}".format(os.getpid(), os.getpgrp())).replace("KID", "{0}:{0}".format(child.pid))
open(os.path.join(d, "child.pid"), "w").write(str(child.pid))
os.execv(sys.executable, [sys.executable, pa_py, "session", "--space=sp1", "--project=" + project, "--profile=test",
                          "--name=proj", "--swap-child=" + value, "--swap-id=0123456789abcdef"])
"""

QUIET_STUB = r"""
import json, os, sys, time
if sys.argv[1:2] in (["--help"], ["--version"]):
    print("2.1.300 (Claude Code)")
    sys.exit(0)
with open(os.path.join(os.environ["T195_DIR"], "stub-starts.jsonl"), "a") as f:
    f.write(json.dumps({"pid": os.getpid(), "pgrp": os.getpgrp(), "at": time.time()}) + "\n")
time.sleep(3600)
"""


class S5_RunTimeNotAdopted(unittest.TestCase):
    """S5 / R2.6 at run time: no adoption; every child of its own is reaped before any Claude starts."""

    def run_case(self, value):
        base = os.path.realpath(tempfile.mkdtemp(dir=TMP))
        home, project = os.path.join(base, "pa"), os.path.join(base, "proj")
        os.makedirs(project)
        install(home)
        for name, text in (("reaper.py", REAPER), ("stub.py", QUIET_STUB), ("bin/hc", HC)):
            write(os.path.join(base, name), text)
        os.chmod(os.path.join(base, "bin", "hc"), 0o755)
        write(os.path.join(home, "profiles", "test", "profile.json"), json.dumps(
            {"name": "test", "claude": [sys.executable, os.path.join(base, "stub.py")], "codex": ["true"]}))
        space = os.path.join(home, "spaces", SP)
        dump({"version": 1, "space": SP, "project": project, "profile": "test", "name": "proj", "terminal": "t1",
              "terminals": ["t1"], "sessionId": SID, "sessionStarted": False}, os.path.join(space, "agent.json"))
        env = {k: v for k, v in os.environ.items() if k not in ("HC_TERMINAL_ID", "CLAUDECODE")}
        env.update(HERDR_PA_HOME=home, HERDR_APP_HOME=os.path.join(base, "app"), HC_TERMINAL_ID="t1", T195_DIR=base,
                   CLAUDE_CONFIG_DIR=os.path.join(base, "claude"),
                   PATH=os.path.join(base, "bin") + os.pathsep + os.environ.get("PATH", ""))
        other = subprocess.Popen(["sleep", "30"])  # a live process that is not the launcher's child
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        value = value.replace("OTHER", "{0}:{0}".format(other.pid))
        proc = subprocess.Popen([sys.executable, os.path.join(base, "reaper.py"), base, os.path.join(home, "tools", "pa.py"),
                                 project, value], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=open(os.path.join(base, "err"), "w"), env=env, start_new_session=True, cwd=project)

        def stop():
            for s in lines(os.path.join(base, "stub-starts.jsonl")):
                kill_group(s.get("pgrp"))
            kill_group(proc.pid)
            proc.kill()
            proc.wait()
        self.addCleanup(stop)
        started = wait_until(lambda: lines(os.path.join(base, "stub-starts.jsonl")), 20)
        log = read(os.path.join(space, "session.log"))
        self.assertTrue(started, "no Claude start: " + log[-2000:] + read(os.path.join(base, "err"))[-2000:])
        ended = float(read(os.path.join(base, "child-end")) or "inf")
        self.assertGreaterEqual(started[0]["at"], ended, "a Claude started while a child of its own lived")
        self.assertIn("swap not adopted", log)
        kid = int(read(os.path.join(base, "child.pid")))
        self.assertIn("swap: reaped pid={} code=0".format(kid), log)
        self.assertEqual(started[0]["pgrp"], started[0]["pid"])
        self.assertTrue(alive(other.pid), "the other process is not touched")

    def test_malformed(self):
        self.run_case("abc")

    def test_arabic_indic_digits(self):  # mutation: drop re.ASCII
        self.run_case("١٢:١٢")

    def test_own_pid(self):
        self.run_case("OWN")

    def test_not_its_child(self):
        self.run_case("OTHER")


# ---- adoption (R2.5.2, R2.6, R2.7) and the state file (S6) -----------------------------------------------------------

def fork_child(own_group=True, code=0, run=30.0):
    """A real child of this test process (python), in a group of its own unless own_group is False."""
    pid = os.fork()
    if pid == 0:
        try:
            if own_group:
                os.setpgid(0, 0)
            time.sleep(run)
        finally:
            os._exit(code)
    if own_group:
        try:
            os.setpgid(pid, pid)
        except OSError:
            pass
    return pid


class Adoption(Base):
    def setUp(self):
        super().setUp()
        self.kids = []
        self.addCleanup(self.reap)

    def reap(self):
        for pid in self.kids:
            kill_pid(pid)
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass

    def kid(self, **kw):
        pid = fork_child(**kw)
        self.kids.append(pid)
        return pid

    def take(self, value, sid="0123456789abcdef"):
        args = types.SimpleNamespace(space=SP, project=self.project, profile="test", name="proj", swap_child=value,
                                     swap_id=sid, fresh=False, auto=False, ask=False, restarted=None)
        return pa.take_swap(args, quiet=False)

    def test_own_child_in_its_group(self):
        pid = self.kid()
        ctx = self.take("{0}:{0}".format(pid))
        child = ctx["adopt"]["child"]
        self.assertIsInstance(child, pa.AdoptedChild)
        self.assertEqual((child.pid, child.pa_group), (pid, pid))
        self.assertIsNone(child.poll())
        with self.assertRaises(subprocess.TimeoutExpired):
            child.wait(timeout=0.1)

    def test_wrong_pgid_adopts_without_a_group(self):
        pid = self.kid()
        ctx = self.take("{}:{}".format(pid, pid + 1 if pid + 1 != os.getpgrp() else pid + 2))
        self.assertIsNone(ctx["adopt"]["child"].pa_group)

    def test_launchers_own_group_adopts_without_a_group(self):
        pid = self.kid(own_group=False)
        ctx = self.take("{}:{}".format(pid, os.getpgrp()))
        self.assertIsNone(ctx["adopt"]["child"].pa_group, "end_child must never signal the launcher's group")

    def test_exited_child_adopted_with_its_status(self):
        pid = self.kid(code=9, run=0)
        wait_until(lambda: subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], stdout=subprocess.PIPE,
                                          universal_newlines=True).stdout.strip().startswith("Z"), 5)
        ctx = self.take("{0}:{0}".format(pid))
        child = ctx["adopt"]["child"]
        self.assertEqual(child.poll(), 9)
        self.assertEqual(child.wait(), 9)
        self.kids.remove(pid)

    def test_not_its_child(self):
        # a process these tests started that is not this process's child: a shell's background sleep, the shell gone
        out = subprocess.run(["/bin/sh", "-c", "sleep 30 >/dev/null 2>&1 & echo $!"], stdout=subprocess.PIPE,
                             universal_newlines=True).stdout.strip()
        orphan = int(out)
        self.addCleanup(kill_pid, orphan)
        ctx = self.take("{0}:{0}".format(orphan))
        self.assertNotIn("adopt", ctx)
        self.assertTrue(ctx.get("reapFirst"))
        self.assertTrue(any("swap not adopted" in n for n in ctx["swapNotes"]))

    def test_malformed_is_not_adopted(self):
        for value in ("abc", None, "1:1"):
            ctx = self.take(value)
            self.assertNotIn("adopt", ctx, value)
            self.assertTrue(ctx.get("reapFirst"), value)

    def test_end_child_on_the_adopted_child(self):  # S9 (R2.7) in-process: TERM to the group, the code, nothing left
        pid = self.kid()
        ctx = self.take("{0}:{0}".format(pid))
        child = ctx["adopt"]["child"]
        ctx.update(child=child, childGroup=child.pa_group, childDone=threading.Event(), directory=self.dir)
        code = pa.end_child(ctx, "restart")
        self.assertEqual(code, -signal.SIGTERM)
        self.kids.remove(pid)
        self.assertEqual(child.returncode, -signal.SIGTERM)
        self.assertTrue(ctx["childDone"].is_set())
        with self.assertRaises(ProcessLookupError):
            os.killpg(pid, 0)

    def test_a_reaped_child_is_never_signalled(self):
        pid = self.kid(code=3, run=0)
        ctx = self.take("{0}:{0}".format(pid))
        child = ctx["adopt"]["child"]
        self.assertEqual(child.wait(), 3)
        self.kids.remove(pid)
        sent = []
        real = os.kill
        os.kill = lambda p, s: sent.append((p, s))
        try:
            child.send_signal(signal.SIGTERM)
            child.kill()
        finally:
            os.kill = real
        self.assertEqual(sent, [])

    def test_two_waiters_get_the_same_code(self):
        pid = self.kid(code=4, run=0.5)
        ctx = self.take("{0}:{0}".format(pid))
        child = ctx["adopt"]["child"]
        got = []
        threads = [threading.Thread(target=lambda: got.append(child.wait())) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.kids.remove(pid)
        self.assertEqual(got, [4, 4])

    # S6: the state file's hostile shapes. Adoption happens from argv in every case.
    def state(self, value=None, sid="0123456789abcdef", raw=None):
        path = os.path.join(self.dir, pa.SWAP_FILE)
        if raw is not None:
            write(path, raw)
        elif value is not None:
            full = {"version": 1, "id": sid, "instance": "inst-1", "startedAt": "2026-11-20T10:00:00Z",
                    "started": 1794000000.0, "previous": [1794000000, "t1"],
                    "running": {"since": 1794000100.0, "session": SID, "marked": True}, "began": 1794000100.0,
                    "resume": True, "why": "recorded", "quick": 1, "quickResumeFails": 0,
                    "session": {"id": SID, "core": "C" * 64}, "restartedFrom": None, "restart": None,
                    "context": None, "lastReset": None, "formerInstances": ["inst-0"], "swapCount": 2,
                    "fromTools": "F" * 64, "at": "2026-11-20T10:05:00Z", "pid": 1, "pgid": 1, "tty": True}
            full.update(value)
            dump(full, path)
        return path

    def test_state_carried(self):
        pid = self.kid()
        self.state({})
        ctx = self.take("{0}:{0}".format(pid))
        self.assertEqual(ctx["instance"], "inst-1")
        self.assertEqual(ctx["startedAt"], "2026-11-20T10:00:00Z")
        self.assertEqual(ctx["started"], 1794000000.0)
        self.assertEqual(list(ctx["previous"]), [1794000000, "t1"])
        self.assertEqual(ctx["formerInstances"], {"inst-0"})
        adopt = ctx["adopt"]
        self.assertEqual((adopt["began"], adopt["resume"], adopt["quick"]), (1794000100.0, True, 1))
        self.assertEqual(adopt["running"]["since"], 1794000100.0)
        self.assertEqual(adopt["carry"]["session"], {"id": SID, "core": "C" * 64})
        self.assertEqual((adopt["fromTools"], adopt["count"]), ("F" * 64, 2))
        self.assertTrue(ctx["restartNext"], "restart-next.json is not taken again")

    def assert_ignored(self, path, kept=True):
        pid = self.kid()
        ctx = self.take("{0}:{0}".format(pid))
        self.assertIn("adopt", ctx, "adopted from argv")
        self.assertEqual(ctx["adopt"]["child"].pid, pid)
        self.assertNotEqual(ctx.get("instance"), "inst-1", "nothing read from it")
        if kept:
            self.assertTrue(os.path.lexists(path))
        return ctx

    def test_state_missing(self):
        ctx = self.assert_ignored(os.path.join(self.dir, pa.SWAP_FILE), kept=False)
        self.assertTrue(ctx["adopt"]["resume"])
        self.assertEqual(ctx["adopt"]["quick"], 0)

    def test_state_symlink_ignored(self):
        real = self.state({})
        os.rename(real, real + ".real")
        os.symlink(real + ".real", real)
        self.assert_ignored(real)

    def test_state_fifo_ignored(self):
        path = os.path.join(self.dir, pa.SWAP_FILE)
        os.mkfifo(path)
        began = time.time()
        self.assert_ignored(path)
        self.assertLess(time.time() - began, 3, "a FIFO never blocks")

    def test_state_other_uid_ignored(self):
        path = self.state({})
        real = os.getuid
        os.getuid = lambda: real() + 1
        try:
            self.assert_ignored(path)
        finally:
            os.getuid = real

    def test_state_too_big_ignored(self):
        self.assert_ignored(self.state({"pad": "x" * (70 * 1024)}))

    def test_state_not_json_or_not_an_object(self):
        for raw in ("{not json", "[1, 2]", '"x"'):
            self.assert_ignored(self.state(raw=raw))

    def test_state_wrong_id_ignored_and_kept(self):
        path = self.state({}, sid="another-id")
        self.assert_ignored(path)
        self.assertFalse(pa.remove_swap_state(self.dir, "0123456789abcdef"))
        self.assertTrue(os.path.exists(path))
        self.assertTrue(pa.remove_swap_state(self.dir, "another-id"))
        self.assertFalse(os.path.exists(path))

    def test_state_wrong_types_take_defaults(self):
        pid = self.kid()
        self.state({"instance": 5, "startedAt": ["x"], "started": "x", "previous": "x", "running": [], "began": "x",
                    "resume": "yes", "quick": True, "quickResumeFails": -1, "session": "x", "formerInstances": "x",
                    "swapCount": "2", "fromTools": 7, "restart": 1, "context": "x", "lastReset": []})
        ctx = self.take("{0}:{0}".format(pid))
        adopt = ctx["adopt"]
        self.assertNotIn("instance", ctx)
        self.assertNotIn("startedAt", ctx)
        self.assertNotIn("started", ctx)
        self.assertIsNone(ctx.get("previous"))
        self.assertEqual(adopt["resume"], True)
        self.assertEqual(adopt["quick"], 0)
        self.assertEqual(adopt["quickResumeFails"], 0)
        self.assertEqual(adopt["count"], 0)
        self.assertIsNone(adopt["fromTools"])
        self.assertIsNone(adopt["carry"]["session"])
        self.assertIsNone(adopt["carry"]["restart"])
        self.assertTrue(isinstance(adopt["began"], float) and abs(adopt["began"] - time.time()) < 5)


# ---- R1.3, R3, S7, S11 ----------------------------------------------------------------------------------------------

class S7_S11_Records(Base):
    def test_accepts_swap(self):
        self.assertEqual(pa.ACCEPTS, ["restart", "context", "swap"])
        ctx = self.ctx()
        rec = self.record()
        self.assertIn("swap", rec["accepts"])
        self.assertIsNone(rec["swap"])
        self.assertIsNone(rec["swapFailed"])
        self.assertTrue(pa.restart_capable(rec))
        del ctx

    def swapped_record(self, carry=True):
        session = {"id": SID, "resume": True, "at": "2026-11-20T10:00:00Z", "core": self.core, "context": None,
                   "prompt": "new", "promptCore": self.core, "promptContext": None}
        ctx = {"instance": "inst-1", "startedAt": "2026-11-20T09:00:00Z",
               "adopt": {"fromTools": "F" * 64, "count": 0,
                         "carry": {"session": session if carry else None, "restart": None, "restartedFrom": "inst-0",
                                   "context": None, "lastReset": None}}}
        pa.__file__ = os.path.join(pa.ROOT, "tools", "pa.py")
        with contextlib.redirect_stdout(io.StringIO()):
            pa.launcher_start(ctx, "t1", SP, self.dir, pa.load_profile("test"))
        return self.record(), session

    def test_s7_swapped_launcher_reads_current(self):
        self.core = pa.tree_hash(os.path.join(pa.ROOT, "core"))
        rec, session = self.swapped_record()
        self.assertEqual(rec["instance"], "inst-1")
        self.assertEqual(rec["started"], "2026-11-20T09:00:00Z")
        self.assertEqual(rec["session"], session)
        self.assertEqual(rec["restartedFrom"], "inst-0")
        self.assertEqual(rec["swap"]["fromTools"], "F" * 64)
        self.assertEqual(rec["swap"]["count"], 1)
        self.assertTrue(pa.parse_time(rec["swap"]["at"]))
        self.assertIsNone(rec["swapFailed"])
        self.assertEqual(pa.code_state(rec, pa.installed_stamp(), "t1")[0], "current")
        rec2, _ = self.swapped_record(carry=False)  # the mutation: without the carry
        self.assertEqual(pa.code_state(rec2, pa.installed_stamp(), "t1"),
                         ("unknown", "launcher.json has no core hash"))

    def test_s11_old_records_read_as_before(self):
        rows = load(os.path.join(HERE, "samples", "code-state-cases.json"))
        names = [r["name"] for r in rows]
        self.assertIn("swapped launcher", names)
        for row in rows:
            self.assertEqual(pa.code_state(row["launcher"], row["installed"], row["terminal"])[0], row["word"], row["name"])

    def test_doctor_lines(self):
        self.assertEqual(pa.swap_notes({"version": 2, "accepts": ["restart", "context"]}), [])
        self.assertEqual(pa.swap_notes(None), [])
        self.assertEqual(pa.swap_notes({"swap": {"at": "2026-11-20T10:05:00Z", "fromTools": "F" * 64, "count": 2},
                                        "swapFailed": None}),
                         ["loaded the installed code in place at 2026-11-20T10:05:00Z (2×)"])
        self.assertEqual(pa.swap_notes({"swap": None, "swapFailed": {"at": "2026-11-20T10:06:00Z", "installed": "T" * 64,
                                                                     "why": "the installed launcher's self-check failed"}}),
                         ["could not load the installed code in place at 2026-11-20T10:06:00Z: the installed launcher's "
                          "self-check failed"])
        self.assertEqual(pa.swap_notes({"swap": "x", "swapFailed": 5}), [])

    def test_doctor_prints_them(self):
        rec = {"version": 2, "accepts": ["restart", "context", "swap"], "instance": "i1", "terminal": "t1",
               "code": {"tools": self.old_tools}, "session": {"core": pa.tree_hash(os.path.join(pa.ROOT, "core"))},
               "swap": {"at": "2026-11-20T10:05:00Z", "fromTools": "F" * 64, "count": 1}, "swapFailed": None}
        dump(rec, os.path.join(self.dir, "launcher.json"))
        dump({"version": 1, "tasks": []}, os.path.join(self.dir, "ledger.json"))
        pa.hc_get = lambda a: {"state": "live", "labels": {"herdr.pa": SP}} if a == "t1" else {}
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                pa.cmd_doctor(argparse.Namespace(space=SP))
            except SystemExit:
                pass
        self.assertIn("loaded the installed code in place at 2026-11-20T10:05:00Z (1×)", out.getvalue())

    def test_swap_argv_matches_restart_argv_base(self):
        ctx = self.ctx()
        child = ctx["child"]
        argv = pa.swap_argv(ctx, "sid-1", self.project, self.dir, child)
        restart = pa.restart_argv(ctx, "r1", "pa-restart", False, self.project, self.dir)
        self.assertEqual(argv[:-2], restart[:restart.index("--restarted=r1")])
        self.assertEqual(argv[-2:], ["--swap-child=4242:4242", "--swap-id=sid-1"])
        self.assertTrue(pa.pa_session_argv(argv, SP))


if __name__ == "__main__":
    unittest.main()
