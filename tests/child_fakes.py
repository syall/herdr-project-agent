"""t-037 J2: the launcher starts Claude through pa.spawn (a Popen handle it keeps). Tests written for
subprocess.call hand their fake call to popen_of(), which gives a Popen-shaped stand-in: the fake runs when the
"process" is created (so an exception it raises comes from the start, as Popen's would), and wait() returns its
result. patch(pa, fake_call) installs it and returns the function that undoes it."""
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


def patch(pa, fake_call):
    saved = pa.__dict__.get("spawn")
    fake = popen_of(fake_call)
    pa.spawn = lambda argv, cwd=None: fake(argv, cwd=cwd)

    def undo():
        if saved is None:
            pa.__dict__.pop("spawn", None)
        else:
            pa.spawn = saved
    return undo
