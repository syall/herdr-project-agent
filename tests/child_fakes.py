"""t-037 J2: the launcher starts Claude through pa.spawn (a Popen handle it keeps). Tests written for
subprocess.call hand their fake call to popen_of(), which gives a Popen-shaped stand-in: the fake runs when the
"process" is created (so an exception it raises comes from the start, as Popen's would), and wait() returns its
result. patch(pa, fake_call) installs it and returns the function that undoes it.
t-193: patch() and unfaked() also replace pa.end_leftovers (the sweep before each start, which signals what an
earlier launch left on the launcher's terminal) with a no-op: a test that fakes the start never sweeps the test
runner's own terminal. test_pa_leftovers tests the sweep itself."""
import signal


def popen_of(fake_call, signals=None):
    class FakePopen:
        def __init__(self, argv, cwd=None, **_kw):
            self.args, self.cwd, self.returncode, self.pid = list(argv), cwd, None, 4242
            self._code = fake_call(argv, cwd=cwd)

        def wait(self, timeout=None):
            self.returncode = self._code
            return self._code

        def poll(self):
            return self.returncode

        def send_signal(self, sig):
            if signals is not None:
                signals.append(sig)

        def terminate(self):
            self.send_signal(signal.SIGTERM)

        def kill(self):
            self.send_signal(signal.SIGKILL)
    return FakePopen


def no_sweep(pa):
    """pa.end_leftovers replaced by a no-op; returns its undo."""
    saved = pa.__dict__.get("end_leftovers")
    pa.end_leftovers = lambda ctx, address=None: None

    def undo():
        if saved is None:
            pa.__dict__.pop("end_leftovers", None)
        else:
            pa.end_leftovers = saved
    return undo


def patch(pa, fake_call):
    saved = pa.__dict__.get("spawn")
    fake = popen_of(fake_call)
    pa.spawn = lambda argv, cwd=None: fake(argv, cwd=cwd)
    undo_sweep = no_sweep(pa)

    def undo():
        undo_sweep()
        if saved is None:
            pa.__dict__.pop("spawn", None)
        else:
            pa.spawn = saved
    return undo


class SpawnNotFaked(AssertionError):
    pass


def unfaked(pa):
    """t-049 (t-059, t-037 J2 REVIEW RL8): pa.spawn raises until a test fakes it (patch() above, or its own). A test
    that fakes only subprocess.call then fails at once instead of starting the profile's real program (how c6 hung
    twice). Installed by the test bases of test_pa_session, test_pa_costs and test_pa_restart; returns its undo."""
    saved = pa.__dict__.get("spawn")

    def spawn(argv, cwd=None):
        raise SpawnNotFaked("spawn not faked: this test would start {!r} (fake it with child_fakes.patch)".format(
            list(argv)[:1]))
    pa.spawn = spawn
    undo_sweep = no_sweep(pa)

    def undo():
        undo_sweep()
        if saved is None:
            pa.__dict__.pop("spawn", None)
        else:
            pa.spawn = saved
    return undo
