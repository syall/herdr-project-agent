"""Unit tests for pa-costs, the launch records and the closed event (t-009 part A).

Run: python3 -m unittest -v tests/test_pa_costs.py
No hc calls, no network, no real homes: HERDR_PA_HOME, CLAUDE_CONFIG_DIR, CODEX_HOME and HERDR_APP_HOME point at a
temp folder while pa.py, pa_hooks.py and pa_costs.py are loaded; hc is a dict-backed stub. Every session line is
synthetic (tests/costs_fixtures.py) and every content field holds the canary, which must reach no file under
costs/ or billing/, no output and no exception text (V1, run last).
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(HERE, "..", "runtime", "tools")
sys.path.insert(0, HERE)
import costs_fixtures as fx  # noqa: E402

TMP = tempfile.mkdtemp(prefix="t009-unit-")
_HOMES = {"HERDR_PA_HOME": os.path.join(TMP, "pa"), "CLAUDE_CONFIG_DIR": os.path.join(TMP, "claude"),
          "CODEX_HOME": os.path.join(TMP, "codex"), "HERDR_APP_HOME": os.path.join(TMP, "app")}
DROP_ENV = ("HC_TERMINAL_ID", "HC_SOCKET", "HC_TERMINAL_SOCKET", "HC_SCOPE", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
            "CODEX_SANDBOX", "CODEX_HOME_SESSION", "PA_PRINT")
_saved_env = {k: os.environ.get(k) for k in list(_HOMES) + list(DROP_ENV)}
_saved_mod = sys.modules.get("pa")
os.environ.update(_HOMES)
for _k in DROP_ENV:
    os.environ.pop(_k, None)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(TOOLS, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pa = _load("pa")
sys.modules["pa"] = pa
pa_hooks = _load("pa_hooks")
try:
    pa_costs = _load("pa_costs")
    COSTS_MISSING = None
except FileNotFoundError as e:  # before step 1 the file does not exist yet
    pa_costs, COSTS_MISSING = None, "runtime/tools/pa_costs.py does not exist yet ({})".format(e)
if _saved_mod is None:
    del sys.modules["pa"]
else:
    sys.modules["pa"] = _saved_mod
for _k, _v in _saved_env.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v

_module_env = {}
_module_pa = []


def setUpModule():
    for k in list(_HOMES) + list(DROP_ENV):
        _module_env[k] = os.environ.get(k)
    for k in DROP_ENV:
        os.environ.pop(k, None)
    os.environ.update(_HOMES)
    _module_pa.append(sys.modules.get("pa"))
    sys.modules["pa"] = pa  # pa.py loads pa_costs.py beside it; that one imports this pa


def tearDownModule():
    for k, v in _module_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    if _module_pa and _module_pa[0] is not None:
        sys.modules["pa"] = _module_pa[0]
    else:
        sys.modules.pop("pa", None)
    shutil.rmtree(TMP, ignore_errors=True)


def need_costs(test):
    return unittest.skipIf(pa_costs is None, COSTS_MISSING or "")(test)


def load(path):
    with open(path) as f:
        return json.load(f)


def dump(value, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(value, f, indent=2)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def raw(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def lines(path):
    data = raw(path)
    return [json.loads(x) for x in data.decode().splitlines() if x.strip()] if data else []


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def isoms(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + ".{:03d}Z".format(int(round((epoch % 1) * 1000)) % 1000)


T0 = 1794830400  # 2026-11-17T00:00:00Z, a fixed day start
PRICES = {"v": 1, "currency": "USD", "unit": "per-1M-tokens", "source": "test table", "asOf": "2026-11-01",
          "models": {"model-a": [{"validFrom": "2026-01-01", "in": 1.0, "out": 10.0, "cw5m": 2.0, "cw1h": 4.0, "cr": 0.5}],
                     "model-b": [{"validFrom": "2026-01-01", "in": 2.0, "out": 20.0, "cw5m": 4.0, "cw1h": 8.0, "cr": 1.0}]},
          "aliases": [{"match": "^model-a-.*", "model": "model-a"}]}
OUTPUT_SEEN = []  # every stdout/stderr text a test captured, for V1


class HcStub:
    """hc as a dict of terminals: hc_get answers from it; call() refuses anything else."""

    def __init__(self, terminals=None):
        self.terminals = terminals or {}
        self.calls = []

    def get(self, address):
        self.calls.append(address)
        return dict(self.terminals.get(address) or {})


class Base(unittest.TestCase):
    """A space sp1 (Project Agent t0, profile test, project folder), its ledger, journal and session.log."""

    def setUp(self):
        for k in DROP_ENV:
            os.environ.pop(k, None)
        os.environ.update(_HOMES)
        for home in (pa.ROOT, pa.CLAUDE_HOME, _HOMES["CODEX_HOME"]):
            shutil.rmtree(home, ignore_errors=True)
        self.saved = {k: getattr(pa, k) for k in ("hc_get", "call", "live_terminals")}
        self.hc = HcStub({"t0": {"address": "t0", "name": "PA: S", "state": "live",
                                 "labels": {"herdr.pa": "sp1", "herdr.pa.profile": "test", "herdr.space": "sp1"}}})
        pa.hc_get = self.hc.get

        def no_call(argv, check=True):
            raise pa.Problem("no external command in unit tests: " + " ".join(argv[:3]))
        pa.call = no_call
        pa.live_terminals = lambda: {}
        self.dir = os.path.join(pa.ROOT, "spaces", "sp1")
        self.project = os.path.join(TMP, "proj")
        os.makedirs(self.project, exist_ok=True)
        dump({"space": "sp1", "terminal": "t0", "terminals": ["t0"], "profile": "test", "name": "S",
              "project": self.project, "created": iso(T0 - 3600)}, os.path.join(self.dir, "agent.json"))
        self.profile_dir = os.path.join(pa.ROOT, "profiles", "test")
        dump({"name": "test", "claude": ["true"], "codex": ["true"]}, os.path.join(self.profile_dir, "profile.json"))
        dump({"version": 1, "defaultProfile": "test"}, os.path.join(pa.ROOT, "config.json"))
        self.ledger({"version": 1, "tasks": []})
        write(os.path.join(self.dir, "journal.jsonl"), "")
        write(os.path.join(self.dir, "session.log"), "")

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(pa, k, v)

    # ---- state helpers
    def ledger(self, value):
        dump(value, os.path.join(self.dir, "ledger.json"))

    def journal(self, *entries):
        with open(os.path.join(self.dir, "journal.jsonl"), "a") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def prices(self, table=None):
        dump(PRICES if table is None else table, os.path.join(self.profile_dir, "prices.json"))

    def costs(self, name):
        return os.path.join(self.dir, "costs", name)

    def record(self, **fields):
        """Append one session record (as the launchers write them)."""
        rec = {"v": 1, "ev": "start", "at": iso(T0), "session": None, "program": "claude", "kind": "worker", "space": "sp1",
               "task": None, "name": None, "role": None, "terminal": None, "parent": "t0", "model": None, "host": "h1",
               "cwd": self.project, "pid": 1, "resumed": False, "bind": None, "source": "recorded"}
        rec.update(fields)
        os.makedirs(os.path.join(self.dir, "costs"), exist_ok=True)
        with open(self.costs("sessions.jsonl"), "a") as f:
            f.write(json.dumps(rec) + "\n")
        return rec

    def run_cmd(self, fn, *args, **kw):
        """Run fn capturing stdout and stderr; returns (result or SystemExit code, out, err)."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                result = fn(*args, **kw)
            except SystemExit as e:
                result = e.code
        OUTPUT_SEEN.append(out.getvalue() + err.getvalue())
        return result, out.getvalue(), err.getvalue()


class H1_Harness(Base):
    def test_h1_temp_homes(self):
        self.assertTrue(pa.ROOT.startswith(TMP) and pa.CLAUDE_HOME.startswith(TMP) and pa.APP.startswith(TMP))
        self.assertTrue(os.environ["CODEX_HOME"].startswith(TMP))
        for k in ("HC_TERMINAL_ID", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_SANDBOX", "CODEX_HOME_SESSION"):
            self.assertNotIn(k, os.environ)
        self.assertIs(pa_hooks.pa, pa)
        if pa_costs is not None:
            self.assertIs(pa_costs.pa, pa)
            self.assertTrue(pa_costs.CODEX_HOME.startswith(TMP))
            self.assertTrue(pa_costs.CLAUDE_HOME.startswith(TMP))
