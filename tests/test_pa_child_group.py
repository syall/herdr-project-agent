"""t-184: a launch's Claude runs in a process group of its own, and the launcher ends that whole group.

The launcher's Claude can be a wrapper that exits at once on SIGTERM without ending the real Claude it
started; ending the handle alone left that Claude running on the terminal (spec.md R1-R8, FINDINGS.md).

Run: python3 -m unittest -v tests/test_pa_child_group.py
Temp homes only: HERDR_PA_HOME, HERDR_APP_HOME and CLAUDE_CONFIG_DIR point at a temp folder before pa.py is loaded.
Real child processes, all started by these tests (sh, sleep, python); no Claude, no hc. The in-process tests
replace pa.foreground_tty so the test runner's terminal is never handed over; T3 runs a helper launcher in a session
of its own on a pty the test opens.
"""
import importlib.util
import os
import pty
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import child_fakes  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PA_PY = os.path.join(REPO, "runtime", "tools", "pa.py")
TMP = tempfile.mkdtemp(prefix="t184-group-")
os.environ["HERDR_PA_HOME"] = os.path.join(TMP, "pa")
os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(TMP, "claude")
os.environ["HERDR_APP_HOME"] = os.path.join(TMP, "app")
for k in ("HC_TERMINAL_ID", "CLAUDECODE"):
    os.environ.pop(k, None)
spec = importlib.util.spec_from_file_location("pa", PA_PY)
pa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pa)


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


def read_pid(path, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with open(path) as f:
                text = f.read().strip()
            if text.endswith("."):
                return int(text[:-1])
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    raise AssertionError("no pid in " + path)


# A wrapper like a vendor launcher: starts a grandchild, and on SIGTERM exits 255 at once without passing it on.
WRAPPER = """#!/bin/sh
{grandchild} &
echo "$!." > "$1"
trap 'exit 255' TERM
{tail}
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=TMP)
        self.pids = []
        saved = pa.__dict__.get("foreground_tty")
        pa.foreground_tty = lambda: False  # never hand the test runner's terminal to a child
        self.addCleanup(self.cleanup, saved)

    def cleanup(self, saved):
        if saved is None:
            pa.__dict__.pop("foreground_tty", None)
        else:
            pa.foreground_tty = saved
        for pid in self.pids:  # only processes these tests started
            if alive(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass

    def wrapper(self, grandchild="sleep 300", tail="wait"):
        path = os.path.join(self.dir, "wrapper.sh")
        with open(path, "w") as f:
            f.write(WRAPPER.format(grandchild=grandchild, tail=tail))
        pidfile = os.path.join(self.dir, "grandchild.pid")
        return ["/bin/sh", path, pidfile], pidfile

    def start(self, argv):
        ctx = {"directory": self.dir}
        result = {}

        def run():
            try:
                result["code"] = pa.run_child(ctx, argv, self.dir)
            except BaseException as e:  # noqa: BLE001 - reported by the test
                result["error"] = e
        t = threading.Thread(target=run, daemon=True)
        t.start()
        end = time.time() + 10
        while ctx.get("child") is None and t.is_alive() and time.time() < end:
            time.sleep(0.02)
        return ctx, t, result

    def log(self):
        try:
            with open(os.path.join(self.dir, "session.log")) as f:
                return f.read()
        except OSError:
            return ""


class T1_WholeTreeEnds(Base):
    """R1, R2: end_child ends the wrapper's grandchild too, before it returns."""

    def test_grandchild_of_a_wrapper_that_exits_on_term_is_gone_when_end_child_returns(self):
        argv, pidfile = self.wrapper()
        ctx, t, result = self.start(argv)
        grandchild = read_pid(pidfile)
        self.pids.append(grandchild)
        self.assertTrue(alive(grandchild))
        child = ctx["child"]
        self.pids.append(child.pid)
        self.assertEqual(pa.END_GRACE, 20)
        began = time.time()
        pa.end_child(ctx, "restart")
        took = time.time() - began
        self.assertFalse(alive(grandchild), "the wrapper's grandchild outlived end_child (orphaned Claude)")
        # verify r1 RL1: the SIGTERM goes to the group, so a grandchild that ends on TERM is gone long before the
        # 20 s grace (TERM to the handle only: the grace runs out and the group gets SIGKILL)
        self.assertLess(took, 5, "end_child took {:.1f} s: the group got no SIGTERM".format(took))
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertEqual(result.get("code"), 255, result)
        self.assertEqual(ctx.get("ended"), "restart")

    def test_run_child_returns_only_when_the_group_is_gone(self):
        # verify r1 RL2: a grandchild that takes ~1.5 s to end on TERM (like a Claude shutting down); run_child
        # must wait for end_child, not return as soon as the wrapper's handle exits
        argv, pidfile = self.wrapper(grandchild="(trap 'sleep 1.5; exit 0' TERM; while :; do sleep 0.1; done)")
        ctx, t, result = self.start(argv)
        grandchild = read_pid(pidfile)
        self.pids.append(grandchild)
        ender = threading.Thread(target=pa.end_child, args=(ctx, "fresh"), daemon=True)
        ender.start()
        t.join(30)
        self.assertFalse(t.is_alive())
        self.assertFalse(alive(grandchild), "run_child returned while the launch's group still ran")
        ender.join(30)


class T6_CallStdin(unittest.TestCase):
    """review r1 RL1: call() never gives its command the launcher's terminal as stdin (pa.py is a background group
    while Claude has the foreground; a command reading the terminal there would be stopped)."""

    def test_call_gives_its_command_dev_null_as_stdin(self):
        r, w = os.pipe()
        saved = os.dup(0)
        os.dup2(r, 0)  # this process's stdin: a pipe, standing in for the terminal

        def restore():
            os.dup2(saved, 0)
            for fd in (saved, r, w):
                os.close(fd)
        self.addCleanup(restore)
        probe = ("import os, sys; a, b = os.fstat(0), os.stat(os.devnull); "
                 "print('devnull' if (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino) else 'other')")
        self.assertEqual(pa.call([sys.executable, "-c", probe]).strip(), "devnull")


class T2_KillAfterGrace(Base):
    """R1: what ignores SIGTERM gets SIGKILL after END_GRACE."""

    def setUp(self):
        super().setUp()
        saved = pa.END_GRACE
        pa.END_GRACE = 1
        self.addCleanup(setattr, pa, "END_GRACE", saved)

    def test_grandchild_ignoring_term_is_killed_after_the_grace(self):
        argv, pidfile = self.wrapper(grandchild="(trap '' TERM; exec sleep 300)")
        ctx, t, result = self.start(argv)
        grandchild = read_pid(pidfile)
        self.pids.append(grandchild)
        time.sleep(0.2)  # the subshell has set its trap and exec'd sleep
        began = time.time()
        pa.end_child(ctx, "restart")
        took = time.time() - began
        self.assertFalse(alive(grandchild), "a grandchild ignoring SIGTERM outlived end_child")
        self.assertGreaterEqual(took, 0.9, "SIGKILL before the grace")
        self.assertLess(took, 1 + 5 + 1)
        t.join(10)
        self.assertFalse(t.is_alive())


class T4_NothingElseSignalled(Base):
    """R6: only the launch's own group is signalled; a handle without a group gets its own signal only."""

    def test_a_process_in_the_launchers_own_group_survives(self):
        sibling = subprocess.Popen(["sleep", "300"])  # same group as this test process (the launcher's)
        self.addCleanup(sibling.wait)
        self.addCleanup(sibling.kill)  # runs first (cleanups run last-in first-out)
        calls = []
        real = os.killpg

        def killpg(g, sig):
            calls.append((g, sig))
            return real(g, sig)
        pa.os.killpg = killpg
        self.addCleanup(setattr, pa.os, "killpg", real)
        argv, pidfile = self.wrapper()
        ctx, t, result = self.start(argv)
        self.pids.append(read_pid(pidfile))
        child = ctx["child"]
        pa.end_child(ctx, "restart")
        t.join(10)
        self.assertTrue(alive(sibling.pid), "a process in the launcher's own group was signalled")
        own = os.getpgrp()
        self.assertTrue(all(g == child.pid and g != own for g, _sig in calls), calls)

    def test_a_handle_without_a_group_gets_only_its_own_signal(self):
        signals = []
        fake = child_fakes.popen_of(lambda argv, cwd=None: 0, signals=signals)(["claude"], cwd=self.dir)
        calls = []
        real = os.killpg
        pa.os.killpg = lambda g, sig: calls.append((g, sig))
        self.addCleanup(setattr, pa.os, "killpg", real)
        ctx = {"directory": self.dir, "child": fake}
        self.assertEqual(pa.end_child(ctx, "restart"), 0)
        self.assertEqual(signals, [signal.SIGTERM])
        self.assertEqual(calls, [])


class T5_CleanExitLeavesAMember(Base):
    """R5: a child that exits by itself keeps its exit code; a member it left in its group is ended and logged."""

    def test_exit_0_leaving_a_member(self):
        argv, pidfile = self.wrapper(tail="exit 0")
        ctx, t, result = self.start(argv)
        t.join(30)
        self.assertFalse(t.is_alive())
        grandchild = read_pid(pidfile)
        self.pids.append(grandchild)
        self.assertEqual(result.get("code"), 0, result)
        self.assertIsNone(ctx.get("ended"))
        self.assertFalse(alive(grandchild), "a member left by a clean exit outlived run_child")
        self.assertIn("exit left 1 in group: ended", self.log())

    def test_clean_exit_alone_adds_no_wait_and_no_line(self):
        ctx, t, result = self.start(["/bin/sh", "-c", "exit 3"])
        began = time.time()
        t.join(10)
        self.assertEqual(result.get("code"), 3, result)
        self.assertLess(time.time() - began, 2)
        self.assertNotIn("exit left", self.log())


HELPER = r"""
import fcntl, importlib.util, json, os, sys, termios
fcntl.ioctl(0, termios.TIOCSCTTY, 0)  # this helper's session (start_new_session) takes the pty as its terminal
spec = importlib.util.spec_from_file_location("pa", sys.argv[1])
pa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pa)
out, child_out = sys.argv[2], sys.argv[3]
before = {"pgrp": os.getpgrp(), "fg": os.tcgetpgrp(0), "foreground_tty": os.tcgetpgrp(0) == os.getpgrp()}
probe = ("import json, os, sys; json.dump({'pid': os.getpid(), 'pgrp': os.getpgrp(), 'fg': os.tcgetpgrp(0)}, "
         "open(sys.argv[1], 'w'))")
ctx = {"directory": os.path.dirname(out)}
code = pa.run_child(ctx, [sys.executable, "-c", probe, child_out], os.path.dirname(out))
after = {"pgrp": os.getpgrp(), "fg": os.tcgetpgrp(0), "code": code}
json.dump({"before": before, "after": after}, open(out, "w"))
"""


class T3_OwnGroupOwnsTheTerminal(unittest.TestCase):
    """R3, R8: the child runs in a group of its own; with a terminal, that group is its foreground group while the
    child runs, and the launcher's again after run_child returns; without one, no terminal handover."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=TMP)

    def test_on_a_pty(self):
        import json
        helper = os.path.join(self.dir, "helper.py")
        with open(helper, "w") as f:
            f.write(HELPER)
        out, child_out = os.path.join(self.dir, "out.json"), os.path.join(self.dir, "child.json")
        err = open(os.path.join(self.dir, "helper.err"), "w")
        self.addCleanup(err.close)
        master, slave = pty.openpty()
        try:
            proc = subprocess.Popen([sys.executable, helper, PA_PY, out, child_out], stdin=slave, stdout=slave,
                                    stderr=err, start_new_session=True, cwd=self.dir)
            os.close(slave)
            slave = None
            end = time.time() + 30
            while proc.poll() is None and time.time() < end:
                try:
                    import select
                    if select.select([master], [], [], 0.1)[0]:
                        os.read(master, 4096)
                except OSError:
                    break
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        finally:
            if slave is not None:
                os.close(slave)
            os.close(master)
        with open(os.path.join(self.dir, "helper.err")) as f:
            self.assertTrue(os.path.exists(out), "the helper launcher failed: " + f.read()[-2000:])
        with open(out) as f:
            got = json.load(f)
        with open(child_out) as f:
            child = json.load(f)
        self.assertTrue(got["before"]["foreground_tty"], got)
        self.assertEqual(child["pgrp"], child["pid"], "the child is not in a group of its own")
        self.assertNotEqual(child["pgrp"], got["before"]["pgrp"])
        self.assertEqual(child["fg"], child["pgrp"], "the child's group is not the terminal's foreground group")
        self.assertEqual(got["after"]["fg"], got["after"]["pgrp"], "the launcher did not take the terminal back")
        self.assertEqual(got["after"]["code"], 0)

    def test_without_a_terminal(self):
        script = os.path.join(self.dir, "nt.py")
        with open(script, "w") as f:
            f.write(r"""
import importlib.util, json, os, sys
spec = importlib.util.spec_from_file_location("pa", sys.argv[1])
pa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pa)
probe = "import json, os, sys; json.dump({'pid': os.getpid(), 'pgrp': os.getpgrp()}, open(sys.argv[1], 'w'))"
child_out = sys.argv[2]
ctx = {"directory": os.path.dirname(child_out)}
code = pa.run_child(ctx, [sys.executable, "-c", probe, child_out], os.path.dirname(child_out))
json.dump({"code": code, "tty": getattr(pa, "foreground_tty", lambda: False)(), "pgrp": os.getpgrp()}, open(child_out + ".launcher", "w"))
""")
        child_out = os.path.join(self.dir, "child.json")
        run = subprocess.run([sys.executable, script, PA_PY, child_out], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, universal_newlines=True, timeout=30,
                             cwd=self.dir)
        self.assertEqual(run.returncode, 0, run.stderr[-2000:])
        import json
        with open(child_out) as f:
            child = json.load(f)
        with open(child_out + ".launcher") as f:
            launcher = json.load(f)
        self.assertFalse(launcher["tty"])
        self.assertEqual(launcher["code"], 0)
        self.assertEqual(child["pgrp"], child["pid"], "no terminal: still a group of its own (R1/R2)")
        self.assertNotEqual(child["pgrp"], launcher["pgrp"])


if __name__ == "__main__":
    unittest.main()
