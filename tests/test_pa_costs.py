"""Unit tests for pa-costs, the launch records and the closed event (t-009 part A), and Claude Code's own
per-session cost beside est. (t-029: the statusline command, the launcher's --settings, the summary and report).

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
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(HERE, "..", "runtime", "tools")
sys.path.insert(0, HERE)
import costs_fixtures as fx  # noqa: E402
import child_fakes  # noqa: E402

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
    for k in list(_HOMES) + list(DROP_ENV) + ["TZ"]:
        _module_env[k] = os.environ.get(k)
    os.environ["TZ"] = "UTC"  # the report's days are the terminal's (t-036): every test sets its own zone, never the machine's
    time.tzset()
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
    time.tzset()
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


T0 = 1794873600  # 2026-11-17T00:00:00Z, a fixed day start
PRICES = {"v": 1, "currency": "USD", "unit": "per-1M-tokens", "source": "test table", "asOf": "2026-11-01",
          "models": {"model-a": [{"validFrom": "2026-01-01", "in": 1.0, "out": 10.0, "cw5m": 2.0, "cw1h": 4.0, "cr": 0.5}],
                     "model-b": [{"validFrom": "2026-01-01", "in": 2.0, "out": 20.0, "cw5m": 4.0, "cw1h": 8.0, "cr": 1.0}]},
          "aliases": [{"match": "^model-a-.*", "model": "model-a"}]}
OUTPUT_SEEN = []  # every stdout/stderr text a test captured, for V1
CANARY_HITS = []  # files under costs/ or billing/ that held the canary after a test, for V1


def scan_for_canary():
    for folder, _dirs, names in os.walk(pa.ROOT):
        if os.sep + "costs" not in folder + os.sep and not (folder + os.sep).startswith(os.path.join(pa.ROOT, "billing") + os.sep):
            continue
        for name in names:
            path = os.path.join(folder, name)
            if fx.CANARY.encode() in (raw(path) or b""):
                CANARY_HITS.append(os.path.relpath(path, pa.ROOT))


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
        scan_for_canary()

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


# ---- step 1: price table and money (R4) ------------------------------------------------------------

ZERO = {"in": 0, "out": 0, "cw5m": 0, "cw1h": 0, "cr": 0}


def toks(**kw):
    out = dict(ZERO)
    out.update(kw)
    return out


class CostsBase(Base):
    """Base for the tests of pa_costs itself: they fail (not skip) while pa_costs.py is missing."""

    def setUp(self):
        self.assertIsNotNone(pa_costs, COSTS_MISSING)
        super().setUp()


class P_Prices(CostsBase):
    def profile(self):
        return pa.load_profile("test")

    def table(self, value=None):
        self.prices(value)
        table, problem = pa_costs.load_prices(self.profile())
        self.assertIsNone(problem)
        return table

    def test_p1_valid_from_on_both_sides_of_a_change(self):
        t = json.loads(json.dumps(PRICES))
        t["models"]["model-a"] = [{"validFrom": "2026-11-10", "in": 3.0, "out": 30.0, "cw5m": 6.0, "cw1h": 12.0, "cr": 1.5},
                                  {"validFrom": "2026-01-01", "in": 1.0, "out": 10.0, "cw5m": 2.0, "cw1h": 4.0, "cr": 0.5}]
        table = self.table(t)
        self.assertEqual(pa_costs.price_for(table, "model-a", "2026-11-09T23:59:59Z")["out"], 10.0)
        self.assertEqual(pa_costs.price_for(table, "model-a", "2026-11-10T00:00:00Z")["out"], 30.0)
        self.assertEqual(pa_costs.price_for(table, "model-a", "2026-12-01T00:00:00Z")["out"], 30.0)
        self.assertIsNone(pa_costs.price_for(table, "model-a", "2025-12-31T00:00:00Z"))

    def test_p2_alias_regex(self):
        table = self.table()
        self.assertEqual(pa_costs.price_for(table, "model-a-2026", "2026-11-17T00:00:00Z")["in"], 1.0)
        self.assertIsNone(pa_costs.price_for(table, "model-c", "2026-11-17T00:00:00Z"))

    def test_p3_missing_model_gives_partial_money_and_unpriced_tokens(self):
        table = self.table()
        rows = [{"model": "model-a", "from": "2026-11-17T01:00:00Z", "tokens": toks(out=1000000), "long": dict(ZERO)},
                {"model": "model-c", "from": "2026-11-17T01:00:00Z", "tokens": toks(out=5, cr=7), "long": dict(ZERO)}]
        total = pa_costs.price_rows(rows, table)
        self.assertAlmostEqual(total["money"], 10.0)
        self.assertEqual(total["unpricedTokens"], 12)
        self.assertEqual(total["missing"], ["model-c"])

    def test_p4_invalid_table_gives_tokens_only_and_a_problem(self):
        for bad in ("not json", json.dumps(dict(PRICES, models={"model-a": [{"validFrom": "2026-01-01", "in": -1}]})),
                    json.dumps(dict(PRICES, models={"model-a": [{"validFrom": "soon", "in": 1}]})),
                    json.dumps(dict(PRICES, models={"model-a": [{"validFrom": "2026-01-01", "in": "cheap"}]}))):
            write(os.path.join(self.profile_dir, "prices.json"), bad)
            table, problem = pa_costs.load_prices(self.profile())
            self.assertIsNone(table, bad)
            self.assertIn("prices.json", problem)
        os.unlink(os.path.join(self.profile_dir, "prices.json"))
        self.assertEqual(pa_costs.load_prices(self.profile()), (None, None))
        # A null price (the profile has not filled it yet) is not an error: that model is unpriced.
        t = json.loads(json.dumps(PRICES))
        t["models"]["model-b"] = [{"validFrom": "2026-01-01", "in": None, "out": None, "cw5m": None, "cw1h": None, "cr": None}]
        table = self.table(t)
        total = pa_costs.price_rows([{"model": "model-b", "from": "2026-11-17T01:00:00Z", "tokens": toks(out=3), "long": dict(ZERO)}], table)
        self.assertIsNone(total["money"])
        self.assertEqual(total["unpricedTokens"], 3)

    def test_p5_long_context_priced_from_long(self):
        t = json.loads(json.dumps(PRICES))
        t["models"]["model-a"][0]["long"] = {"above": 200000, "in": 2.0, "out": 20.0, "cw5m": 4.0, "cw1h": 8.0, "cr": 1.0}
        table = self.table(t)
        price = pa_costs.price_for(table, "model-a", "2026-11-17T00:00:00Z")
        # 2M cache read in total, 1M of it from long calls: 1M x 0.5 + 1M x 1.0 = 1.5
        self.assertAlmostEqual(pa_costs.money(toks(cr=2000000), toks(cr=1000000), price), 1.5)
        # without a long block, long tokens are priced like the rest
        self.assertAlmostEqual(pa_costs.money(toks(cr=2000000), toks(cr=1000000), PRICES["models"]["model-a"][0]), 1.0)

    def test_p6_money_from_tokens_only(self):
        table = self.table()
        row = {"model": "model-a", "from": "2026-11-17T01:00:00Z", "tokens": toks(**{"in": 1000000, "out": 1000000, "cw5m": 1000000,
                                                                                    "cw1h": 1000000, "cr": 1000000}),
               "long": dict(ZERO), "money": 999.0, "calls": 4}
        self.assertAlmostEqual(pa_costs.row_money(row, table), 1.0 + 10.0 + 2.0 + 4.0 + 0.5)
        self.assertIsNone(pa_costs.row_money(dict(row, model="model-c"), table))


# ---- step 2: reading, dedupe, offsets, crash safety (R2, R5) -----------------------------------------

SID = "11111111-2222-4333-8444-5555abcd5555"
SID2 = "66666666-7777-4888-8999-aaaa0000aaaa"


class CountingFile:
    """A file proxy that counts the bytes read through it."""

    def __init__(self, f, counter):
        self.f, self.counter = f, counter

    def read(self, *a):
        data = self.f.read(*a)
        self.counter[0] += len(data)
        return data

    def __getattr__(self, name):
        return getattr(self.f, name)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.f.close()

    def __iter__(self):
        for line in self.f:
            self.counter[0] += len(line)
            yield line


class Collecting(CostsBase):
    """Helpers: a recorded Claude worker session SID in the project folder, collect at a fixture time."""

    def setUp(self):
        super().setUp()
        self.opened = []
        self.read_bytes = [0]
        real_open = open

        def counting_open(path, *a, **kw):
            self.opened.append(str(path))
            return CountingFile(real_open(path, *a, **kw), self.read_bytes)
        pa_costs.open = counting_open
        self.addCleanup(lambda: delattr(pa_costs, "open") if "open" in vars(pa_costs) else None)

    def transcript(self, sid=SID, cwd=None):
        return fx.claude_path(pa.CLAUDE_HOME, cwd or self.project, sid)

    def add(self, *chunks, sid=SID):
        out = []
        for c in chunks:
            out.extend(c if isinstance(c, list) else [c])
        fx.append(self.transcript(sid), out)

    def prompt(self, t, sid=SID):
        return fx.claude_user(isoms(t), sid, self.project)

    def call(self, t, mid, sid=SID, **kw):
        return fx.claude_call(isoms(t), sid, self.project, mid, **kw)

    def collect(self, now, **kw):
        result, out, err = self.run_cmd(pa_costs.collect, self.dir, now=now, **kw)
        return result

    def rows(self):
        """The ledger's rows that count: void lines drop the earlier rows of their session."""
        rows = lines(self.costs("turns.jsonl"))
        kept = []
        for r in rows:
            if r.get("ev") == "void":  # a subagent file's void ("sub") drops that file's rows only
                kept = [k for k in kept if not (k["session"] == r["session"] and k.get("sub") == r.get("sub") and k["seq"] <= r["upTo"])]
            else:
                kept.append(r)
        return kept

    def totals(self):
        t = dict(ZERO)
        for r in self.rows():
            for k in t:
                t[k] += r["tokens"][k]
        return t

    def costs_bytes(self):
        folder = os.path.join(self.dir, "costs")
        return {n: raw(os.path.join(folder, n)) for n in sorted(os.listdir(folder))}


class D_Dedupe(Collecting):
    def test_d1_three_lines_per_call_count_once_with_the_largest_output(self):
        self.record(session=SID, task="t-001", name="t-001-build", terminal="t5")
        self.add(self.prompt(T0 + 1), self.call(T0 + 2, "m1", lines=3, out=90, inp=5, cr=1000, cw5m=100),
                 self.call(T0 + 3, "m2", lines=2, out=40, inp=3, cr=2000))
        self.collect(T0 + 2000)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["calls"], 2)
        self.assertEqual(rows[0]["tokens"], {"in": 8, "out": 130, "cw5m": 100, "cw1h": 0, "cr": 3000})
        self.assertEqual(rows[0]["model"], "model-a")
        self.assertEqual(rows[0]["day"], "2026-11-17")

    def test_d2_a_calls_lines_split_across_two_collects(self):
        self.record(session=SID, task="t-001")
        lines3 = self.call(T0 + 2, "m1", lines=3, out=90)
        self.add(self.prompt(T0 + 1), lines3[:2])
        self.collect(T0 + 3)
        self.assertEqual(self.rows(), [])  # the turn is still open
        self.add(lines3[2:], self.prompt(T0 + 10), self.call(T0 + 11, "m2", lines=1, out=7))
        self.collect(T0 + 12)
        rows = self.rows()
        self.assertEqual([r["tokens"]["out"] for r in rows], [90])  # first turn settled by the next prompt
        self.collect(T0 + 2000)
        self.assertEqual([r["tokens"]["out"] for r in self.rows()], [90, 7])
        self.assertEqual(sum(r["calls"] for r in self.rows()), 2)

    def test_d3_id_fallback_request_id_then_uuid(self):
        self.record(session=SID, task="t-001")
        a = fx.claude_assistant(isoms(T0 + 2), SID, self.project, None, request_id="r1", out=5)
        b = fx.claude_assistant(isoms(T0 + 2), SID, self.project, None, request_id="r1", out=9)
        c = fx.claude_assistant(isoms(T0 + 3), SID, self.project, None, uuid="u9", out=4)
        self.add(self.prompt(T0 + 1), a, b, c)
        self.collect(T0 + 2000)
        rows = self.rows()
        self.assertEqual(rows[0]["calls"], 2)
        self.assertEqual(rows[0]["tokens"]["out"], 13)

    def test_d4_codex_cumulative_deltas_with_a_repeated_total(self):
        self.record(session=None, program="codex", bind="launch-window", at=iso(T0), task="t-002", cwd=self.project, terminal="t6")
        with open(self.costs("sessions.jsonl"), "a") as f:  # binding itself is step 5's; here the bind is given
            f.write(json.dumps({"v": 1, "ev": "bind", "at": iso(T0 + 30), "session": SID2, "program": "codex",
                                "for": iso(T0) + " t6", "match": "launch-window", "confidence": "high"}) + "\n")
        path = fx.codex_path(_HOMES["CODEX_HOME"], "2026-11-17", SID2)
        fx.append(path, [fx.codex_meta(isoms(T0 + 1), SID2, self.project), fx.codex_turn(isoms(T0 + 2), "model-b"),
                         fx.codex_user(isoms(T0 + 2)),
                         fx.codex_tokens(isoms(T0 + 3), inp=1000, cached=600, cw=100, out=50, reasoning=20),
                         fx.codex_tokens(isoms(T0 + 4), inp=1000, cached=600, cw=100, out=50, reasoning=20),  # repeated total
                         fx.codex_tokens(isoms(T0 + 5), inp=1500, cached=1000, cw=100, out=80)])
        self.collect(T0 + 2000)
        self.assertEqual(self.totals(), {"in": 400, "out": 80, "cw5m": 100, "cw1h": 0, "cr": 1000})
        rows = self.rows()
        self.assertEqual({r["model"] for r in rows}, {"model-b"})
        self.assertEqual({r["program"] for r in rows}, {"codex"})
        self.assertEqual(sum(r["calls"] for r in rows), 2)

    def test_d5_forked_record_skips_copied_older_calls(self):
        # A fork writes a new file that may hold copies of the old session's calls. A resume with the same id writes
        # into that session's own file, so its older calls are its own past: fix round 2, FR-M2.
        self.record(session=SID, fork=True, at=iso(T0 + 100), task="t-001")
        self.add(self.prompt(T0 + 1), self.call(T0 + 10, "old", lines=1, out=1000),
                 self.prompt(T0 + 150), self.call(T0 + 200, "new", lines=1, out=7))
        self.collect(T0 + 3000)
        self.assertEqual(self.totals()["out"], 7)

    def test_d6_turn_rules_and_day_split(self):
        self.record(session=SID, task="t-001")
        day_end = T0 + 86400
        self.add(self.prompt(day_end - 100), self.call(day_end - 50, "m1", lines=1, out=3),
                 fx.claude_user(isoms(day_end - 40), SID, self.project, text=False, tool_result=True),  # tool result: same turn
                 fx.claude_user(isoms(day_end - 30), SID, self.project, meta=True),  # meta: same turn
                 fx.claude_user(isoms(day_end - 20), SID, self.project, sidechain=True),  # sidechain: same turn
                 self.call(day_end + 10, "m2", lines=1, out=4))  # same turn, next UTC day: a second segment
        self.collect(day_end + 2000)
        rows = self.rows()
        self.assertEqual([(r["day"], r["tokens"]["out"]) for r in rows], [("2026-11-17", 3), ("2026-11-18", 4)])
        self.assertEqual(rows[0]["from"][:10], "2026-11-17")
        self.assertEqual(rows[0]["turn"], rows[1]["turn"])


class O_Offsets(Collecting):
    def base_session(self):
        self.record(session=SID, task="t-001")
        self.add(self.prompt(T0 + 1), self.call(T0 + 2, "m1", out=30), self.prompt(T0 + 5), self.call(T0 + 6, "m2", out=60))

    def test_o1_second_collect_reads_only_new_bytes(self):
        self.base_session()
        self.collect(T0 + 7)
        size = os.path.getsize(self.transcript())
        self.read_bytes[0] = 0
        extra = [self.prompt(T0 + 9)] + self.call(T0 + 10, "m3", out=9)
        self.add(extra)
        self.opened[:] = []
        self.collect(T0 + 11)
        grown = os.path.getsize(self.transcript()) - size
        transcript_reads = [p for p in self.opened if p == self.transcript()]
        self.assertEqual(len(transcript_reads), 1)
        # Bytes read: the new part of the transcript, plus the costs/ files and the space's small state files.
        others = sum(os.path.getsize(p) for p in set(self.opened) if p != self.transcript() and os.path.exists(p))
        self.assertLessEqual(self.read_bytes[0], grown + others)

    def test_o2_half_line_is_not_consumed(self):
        self.record(session=SID, task="t-001")
        line = self.call(T0 + 2, "m1", lines=1, out=30)[0]
        self.add(self.prompt(T0 + 1), line[:40])
        self.collect(T0 + 3)
        self.add(line[40:])
        self.collect(T0 + 2000)
        self.assertEqual(self.totals()["out"], 30)
        self.assertEqual(sum(r["calls"] for r in self.rows()), 1)

    def test_o3_replaced_file_voids_and_rereads_with_equal_totals(self):
        self.base_session()
        self.collect(T0 + 2000)
        before = self.totals()
        path = self.transcript()
        data = raw(path)
        tmp = path + ".new"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)  # same bytes, new inode
        self.collect(T0 + 2100)
        self.assertTrue(any(r.get("ev") == "void" for r in lines(self.costs("turns.jsonl"))))
        self.assertEqual(self.totals(), before)

    def test_o4_rerun_with_nothing_new_changes_no_byte(self):
        self.base_session()
        self.collect(T0 + 2000)
        before = self.costs_bytes()
        self.collect(T0 + 2000)
        self.collect(T0 + 2050)
        self.assertEqual(self.costs_bytes(), before)

    def test_o5_killed_between_ledger_append_and_state_write(self):
        self.base_session()
        real = pa.write_text
        fail = [True]

        def flaky(path, text):
            if fail[0] and path.endswith(os.path.join("costs", "state.json")):
                fail[0] = False
                raise OSError("killed here")
            return real(path, text)
        pa.write_text = flaky
        try:
            with self.assertRaises(OSError):
                pa_costs.collect(self.dir, now=T0 + 2000)
        finally:
            pa.write_text = real
        self.assertTrue(raw(self.costs("turns.jsonl")))  # the rows were appended before the kill
        self.collect(T0 + 2000)
        self.assertEqual(self.totals(), {"in": 4, "out": 90, "cw5m": 0, "cw1h": 0, "cr": 0})
        self.assertEqual(sum(r["calls"] for r in self.rows()), 2)
        seqs = [r["seq"] for r in lines(self.costs("turns.jsonl")) if "seq" in r]
        self.assertEqual(len(seqs), len(set(seqs)))

    def test_o6_second_collector_is_refused_while_one_runs(self):
        import fcntl
        self.base_session()
        os.makedirs(os.path.join(self.dir, "costs"), exist_ok=True)
        with open(self.costs(".collect.lock"), "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            result, out, _err = self.run_cmd(pa_costs.collect, self.dir, now=T0 + 2000, quiet=False)
        self.assertEqual(result.get("busy"), True)
        self.assertIn("already running", out)
        self.assertIsNone(raw(self.costs("turns.jsonl")))


class G_Gone(Collecting):
    def test_g1_deleted_file_marks_gone_and_keeps_rows(self):
        self.record(session=SID, task="t-001")
        self.record(session=SID2, task="t-002")
        self.add(self.prompt(T0 + 1), self.call(T0 + 2, "m1", out=30))
        self.collect(T0 + 2000)
        rows = self.rows()
        os.unlink(self.transcript())
        self.collect(T0 + 2100)
        self.collect(T0 + 2200)
        gone = [r for r in lines(self.costs("sessions.jsonl")) if r.get("ev") == "gone"]
        self.assertEqual([g["session"] for g in gone], [SID])  # a file that was seen and is deleted: gone at once, once
        self.assertEqual(self.rows(), rows)
        # SID2's file never appeared: it waits (fix round 2, FR-M1), and is gone only after GONE_UNSEEN.
        self.collect(T0 + pa_costs.GONE_UNSEEN + 1)
        self.collect(T0 + pa_costs.GONE_UNSEEN + 100)
        gone = [r for r in lines(self.costs("sessions.jsonl")) if r.get("ev") == "gone"]
        self.assertEqual(sorted(g["session"] for g in gone), sorted([SID, SID2]))  # one gone event each, not repeated
        self.assertEqual(self.rows(), rows)
        state = load(self.costs("state.json"))
        self.assertTrue(any("gone before" in p for p in state.get("problems", [])), state.get("problems"))


# ---- step 3: launch records (R1) --------------------------------------------------------------------

class StopLaunch(Exception):
    pass


class L_LaunchRecords(Base):
    """pa-claude / pa-codex under the Project Agent t0 of space sp1, in worker terminal t5 (t-010-build)."""

    def setUp(self):
        super().setUp()
        dump({"name": "test", "claude": ["true"], "codex": ["true"], "models": {"worker": "model-a"}},
             os.path.join(self.profile_dir, "profile.json"))
        self.ledger({"version": 1, "tasks": [{"id": "t-01", "state": "done"}, {"id": "t-010", "state": "in-progress", "workers": []}]})
        self.hc.terminals["t5"] = {"address": "t5", "name": "t-010-build", "parent": "t0", "state": "live", "labels": {"herdr.space": "sp1"}}
        self.hc.terminals["t9"] = {"address": "t9", "name": "plain", "state": "live", "labels": {"herdr.space": "other"}}
        os.environ["HC_TERMINAL_ID"] = "t5"
        os.environ["PA_PRINT"] = "1"
        self.addCleanup(os.environ.pop, "HC_TERMINAL_ID", None)
        self.addCleanup(os.environ.pop, "PA_PRINT", None)

    def launch(self, kind, *argv):
        result, out, err = self.run_cmd(pa.cmd_launch, kind, list(argv))
        OUTPUT_SEEN.pop()  # PA_PRINT echoes the launcher's own argv by design; V1 is about what pa-costs keeps and prints
        self.assertEqual(err, "")
        return json.loads(out.strip().splitlines()[-1])

    def records(self):
        return lines(self.costs("sessions.jsonl"))

    def test_l1_worker_gets_a_session_id_and_one_start_record(self):
        before = time.time()
        argv = self.launch("claude", "do the job")
        i = argv.index("--session-id")
        sid = argv[i + 1]
        self.assertRegex(sid, r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
        recs = self.records()
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual((r["v"], r["ev"], r["session"], r["program"], r["kind"], r["space"]), (1, "start", sid, "claude", "worker", "sp1"))
        self.assertEqual((r["task"], r["name"], r["terminal"], r["parent"], r["model"]), ("t-010", "t-010-build", "t5", "t0", "model-a"))
        self.assertEqual((r["resumed"], r["bind"], r["source"], r["role"]), (False, None, "recorded", None))
        self.assertEqual(r["cwd"], os.getcwd())
        self.assertGreaterEqual(pa_costs_time(r["at"]), int(before) - 1)
        self.assertNotIn("argv", r)

    def test_l2_given_ids_are_recorded_not_replaced(self):
        cases = [(["--resume", "abc-1"], "abc-1", True, None), (["-r", "abc-2"], "abc-2", True, None),
                 (["--session-id", "abc-3"], "abc-3", False, None), (["--session-id=abc-4"], "abc-4", False, None),
                 (["--continue"], None, True, "launch-window"), (["-c"], None, True, "launch-window"),
                 (["--resume", "abc-5", "--fork-session"], None, True, "launch-window")]
        for args, sid, resumed, bind in cases:
            os.unlink(self.costs("sessions.jsonl")) if os.path.exists(self.costs("sessions.jsonl")) else None
            argv = self.launch("claude", *args)
            self.assertEqual(argv.count("--session-id") + sum(a.startswith("--session-id=") for a in argv),
                             1 if any(a.startswith("--session-id") for a in args) else 0, args)
            r = self.records()
            self.assertEqual(len(r), 1, args)
            self.assertEqual((r[0]["session"], r[0]["resumed"], r[0]["bind"]), (sid, resumed, bind), args)
        self.assertTrue(r[0]["fork"])

    def test_l3_passthrough_and_plain_tabs_write_nothing(self):
        self.launch("claude", "--version")
        self.launch("claude", "mcp", "list")
        os.environ["HC_TERMINAL_ID"] = "t9"
        self.launch("claude", "hello")
        os.environ.pop("HC_TERMINAL_ID")
        self.launch("claude", "hello")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "costs")))

    def test_l4_record_holds_no_argument_text(self):
        argv = self.launch("claude", "--settings", "/x/{}/s.json".format(fx.CANARY), "the handoff {}".format(fx.CANARY))
        self.assertIn("--session-id", argv)
        for name in os.listdir(os.path.join(self.dir, "costs")):
            self.assertNotIn(fx.CANARY.encode(), raw(os.path.join(self.dir, "costs", name)) or b"", name)

    def test_l5_held_append_lock_does_not_hold_the_launch(self):
        import fcntl
        os.makedirs(os.path.join(self.dir, "costs"), exist_ok=True)
        with open(self.costs(".append.lock"), "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            began = time.time()
            argv = self.launch("claude", "job")
            took = time.time() - began
        self.assertIn("--session-id", argv)
        self.assertLess(took, 1.2)
        self.assertEqual(self.records(), [])

    def test_l6_codex_records_the_launch_only(self):
        argv = self.launch("codex", "--some-flag", "job")
        self.assertEqual(argv[1:], ["--some-flag", "job"])
        r = self.records()
        self.assertEqual(len(r), 1)
        self.assertEqual((r[0]["program"], r[0]["session"], r[0]["bind"], r[0]["task"]), ("codex", None, "launch-window", "t-010"))

    def test_l7_run_agent_records_each_start_and_resume(self):
        import argparse as ap
        import types
        directory = self.dir
        agent = load(os.path.join(directory, "agent.json"))
        p = pa.load_profile("test")
        calls = []

        def fake_call(argv, cwd=None):
            calls.append(argv)
            raise StopLaunch()
        saved = pa.subprocess
        pa.subprocess = types.SimpleNamespace(call=fake_call, run=saved.run, PIPE=saved.PIPE, DEVNULL=saved.DEVNULL)
        undo_child = child_fakes.patch(pa, fake_call)  # t-037 J2: the launcher starts Claude with pa.spawn
        try:
            for fresh in (True, False):
                agent = load(os.path.join(directory, "agent.json"))
                if not fresh:
                    agent["sessionStarted"] = True
                    dump(agent, os.path.join(directory, "agent.json"))
                with self.assertRaises(StopLaunch):
                    pa.run_agent(ap.Namespace(fresh=fresh), "t0", self.project, p, directory, agent, {"beat": True, "previous": None}, time.time())
        finally:
            pa.subprocess = saved
            undo_child()
        recs = self.records()
        self.assertEqual([(r["kind"], r["resumed"]) for r in recs], [("pa", False), ("pa", True)])
        sid = load(os.path.join(directory, "agent.json"))["sessionId"]
        self.assertEqual([r["session"] for r in recs], [sid, sid])
        self.assertEqual([r["cwd"] for r in recs], [self.project, self.project])
        self.assertEqual({(r["task"], r["name"], r["terminal"]) for r in recs}, {(None, None, "t0")})

    def test_l8_a_broken_pa_costs_never_stops_a_launch(self):
        broken = os.path.join(TMP, "broken_costs.py")
        write(broken, "raise RuntimeError('broken on purpose')\n")
        saved = pa.COSTS_FILE
        pa.COSTS_FILE = broken
        pa._COSTS.clear()
        try:
            argv = self.launch("claude", "job")
        finally:
            pa.COSTS_FILE = saved
            pa._COSTS.clear()
        self.assertEqual(argv[0], shutil.which("true"))


def pa_costs_time(text):
    return int(time.mktime(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone)


# ---- step 4: attribution (R3) ---------------------------------------------------------------------

class A_Attribution(Collecting):
    def pa_session(self):
        self.record(session=SID, kind="pa", terminal="t0", parent=None)

    def turn(self, t, mid, out=10, sid=SID, **kw):
        self.add(self.prompt(t, sid=sid), self.call(t + 2, mid, lines=1, out=out, sid=sid, **kw))

    def j(self, t, trigger="report", task=None, pass_=1, **kw):
        e = {"at": iso(t), "by": "agent", "trigger": trigger, "task": task, "pass": pass_}
        e.update(kw)
        self.journal(e)

    def by_turn(self):
        return {r["turn"]: r for r in self.rows()}

    def test_a1_pa_turn_by_majority_trigger_and_largest_pass(self):
        self.pa_session()
        self.turn(T0 + 10, "m1")
        self.j(T0 + 12, "report", "t-001", 4)
        self.j(T0 + 13, "report", "t-001", 5)
        self.j(T0 + 14, "message", "t-001", 5)
        self.collect(T0 + 2000)
        r = self.rows()[0]
        self.assertEqual((r["activity"], r["pass"], r["trigger"], r["task"], r["split"]), ("pass:report", 5, "report", "t-001", None))

    def test_a2_two_tasks_give_split_weights(self):
        self.pa_session()
        self.turn(T0 + 10, "m1")
        self.j(T0 + 12, "decision", "t-001")
        self.j(T0 + 13, "decision", "t-001")
        self.j(T0 + 14, "decision", "t-002")
        self.collect(T0 + 2000)
        r = self.rows()[0]
        self.assertIsNone(r["task"])
        self.assertEqual([s["task"] for s in r["split"]], ["t-001", "t-002"])
        self.assertAlmostEqual(sum(s["w"] for s in r["split"]), 1.0)
        self.assertAlmostEqual(r["split"][0]["w"], 2 / 3, places=3)

    def test_a3_no_journal_entry_is_unattributed_after_the_hold(self):
        self.pa_session()
        self.turn(T0 + 10, "m1")
        self.turn(T0 + 100, "m2")
        self.collect(T0 + 150)
        self.assertEqual(self.rows(), [])  # held: the agent journals at the end of a pass
        self.collect(T0 + 2000)
        r = self.by_turn()[isoms(T0 + 10)]
        self.assertEqual((r["activity"], r["reason"], r["task"]), ("unattributed", "pa-no-journal", None))

    def test_a4_late_journal_entry_within_the_hold_is_attributed(self):
        self.pa_session()
        self.turn(T0 + 10, "m1")
        self.turn(T0 + 100, "m2")
        self.collect(T0 + 105)
        self.j(T0 + 50, "message", "t-003", 7)  # written late, but during the first turn
        self.collect(T0 + 2000)
        r = self.by_turn()[isoms(T0 + 10)]
        self.assertEqual((r["activity"], r["task"], r["pass"]), ("pass:message", "t-003", 7))
        self.assertEqual(self.by_turn()[isoms(T0 + 100)]["reason"], "pa-no-journal")

    def test_a9_unknown_trigger_is_pass_other(self):
        self.pa_session()
        self.turn(T0 + 10, "m1")
        self.j(T0 + 12, "close", "t-001")
        self.collect(T0 + 2000)
        self.assertEqual(self.rows()[0]["activity"], "pass:other")

    def worker(self, name="t-001-build", role="build", task="t-001", terminal="t5"):
        self.record(session=SID, kind="worker", terminal=terminal, name=name, task=task)
        if role:
            self.ledger({"version": 1, "tasks": [{"id": task or "t-001", "state": "in-progress",
                                                  "workers": [{"address": terminal, "role": role, "name": name}]}]})

    def test_a5_a10_worker_role_from_the_ledger_is_pinned(self):
        self.worker()
        self.turn(T0 + 10, "m1")
        self.collect(T0 + 2000)
        self.assertEqual((self.rows()[0]["role"], self.rows()[0]["activity"]), ("build", "build"))
        pins = [r for r in lines(self.costs("sessions.jsonl")) if r.get("ev") == "role"]
        self.assertEqual([(p["session"], p["role"], p["from"]) for p in pins], [(SID, "build", "ledger")])
        self.ledger({"version": 1, "tasks": [{"id": "t-001", "state": "in-progress", "workers": []}]})  # pa-worker delete
        self.turn(T0 + 3000, "m2")
        self.collect(T0 + 6000)
        self.assertEqual([r["role"] for r in self.rows()], ["build", "build"])
        self.assertEqual(len([r for r in lines(self.costs("sessions.jsonl")) if r.get("ev") == "role"]), 1)

    def test_a6_reused_worker_split_by_journal_address_token(self):
        self.record(session=SID, kind="worker", terminal="t5", name="t-001-design", task="t-001")
        self.ledger({"version": 1, "tasks": [{"id": "t-001", "state": "done", "workers": [{"address": "t5", "role": "design", "name": "t-001-design"}]},
                                             {"id": "t-002", "state": "in-progress", "workers": [{"address": "t5", "role": "design", "name": "t-001-design"}]}]})
        self.j(T0 + 5, "report", "t-001", why="Started worker t5 (t-001-design)")
        self.turn(T0 + 10, "m1")
        self.j(T0 + 500, "report", "t-002", why="Sent t5 the design of t-002; not t55 or xt5")
        self.j(T0 + 600, "report", "t-003", why="Started t55 for t-003")
        self.turn(T0 + 503, "m2")
        self.turn(T0 + 700, "m3")
        self.collect(T0 + 4000)
        rows = self.by_turn()
        self.assertEqual(rows[isoms(T0 + 10)]["task"], "t-001")
        self.assertEqual((rows[isoms(T0 + 503)]["task"], rows[isoms(T0 + 503)]["match"], rows[isoms(T0 + 503)]["confidence"]),
                         ("t-002", "journal-handoff", "medium"))
        self.assertEqual(rows[isoms(T0 + 700)]["task"], "t-002")

    def test_a7_phase_from_the_journal(self):
        self.worker()
        self.j(T0 + 1, "report", "t-001", to="build/in-progress")
        self.j(T0 + 2, "report", "t-002", to="review/review")
        self.turn(T0 + 10, "m1")
        self.j(T0 + 20, "report", "t-001", to="test/review")
        self.turn(T0 + 30, "m2")
        self.collect(T0 + 2000)
        self.assertEqual([r["phase"] for r in self.rows()], ["build", "test"])

    def test_a8_activity_map(self):
        cases = [("design", "t-001-design", "intent/investigating", "investigate"), ("design", "t-001-design", "plan/in-progress", "plan"),
                 ("design", "t-001-design", "design/in-progress", "design"), ("verifier", "t-001-verify", "test/review", "verify"),
                 ("reviewer", "t-001-review", "review/review", "review"), (None, "t-001-investigate", None, "investigate"),
                 (None, "t-001-tests", None, "other")]
        for role, name, to, activity in cases:
            self.setUp()
            self.worker(name=name, role=role)
            if to:
                self.j(T0 + 1, "report", "t-001", to=to)
            self.turn(T0 + 10, "m1")
            self.collect(T0 + 2000)
            self.assertEqual(self.rows()[0]["activity"], activity, (role, name, to))
        self.setUp()
        self.record(session=SID, kind="worker", terminal="t5", name="scratch", task=None)
        self.turn(T0 + 10, "m1")
        self.collect(T0 + 2000)
        self.assertEqual((self.rows()[0]["activity"], self.rows()[0]["reason"]), ("unattributed", "worker-no-task"))

    def test_a11_unknown_kind_is_unattributed(self):
        self.record(session=SID, kind="mystery")
        self.turn(T0 + 10, "m1")
        self.collect(T0 + 2000)
        self.assertEqual((self.rows()[0]["activity"], self.rows()[0]["reason"]), ("unattributed", "unknown-session-kind"))


class C_CacheRebuild(Collecting):
    def rebuilds(self, gap, cw=30000, key="cw5m"):
        self.setUp()
        self.record(session=SID, task="t-001")
        self.add(self.prompt(T0 + 1), self.call(T0 + 2, "m1", lines=1, out=5, **{key: cw}),
                 self.call(T0 + 2 + gap, "m2", lines=1, out=5, **{key: cw}))
        self.collect(T0 + 10000)
        return [r["rebuild"] for r in self.rows()]

    def test_c1_idle_gap_over_the_cache_lifetime_is_a_rebuild(self):
        self.assertEqual(sum(r["calls"] for r in self.rebuilds(301)), 1)
        self.assertEqual(sum(r["cw5m"] for r in self.rebuilds(301)), 30000)
        self.assertEqual(sum(r["calls"] for r in self.rebuilds(3601, key="cw1h")), 1)

    def test_c2_shorter_gap_or_small_write_is_not(self):
        self.assertEqual(sum(r["calls"] for r in self.rebuilds(299)), 0)
        self.assertEqual(sum(r["calls"] for r in self.rebuilds(301, cw=19999)), 0)
        self.assertEqual(sum(r["calls"] for r in self.rebuilds(3599, key="cw1h")), 0)

    def test_c3_first_call_is_a_cold_start(self):
        self.setUp()
        self.record(session=SID, task="t-001")
        self.add(self.prompt(T0 + 1), self.call(T0 + 5000, "m1", lines=1, cw5m=90000))
        self.collect(T0 + 10000)
        self.assertEqual(self.rows()[0]["rebuild"]["calls"], 0)


# ---- step 5: binding Codex and --continue sessions; reconstruction (R7) ---------------------------------

SID3 = "77777777-8888-4999-8aaa-bbbb1111cccc"
SID4 = "99999999-aaaa-4bbb-8ccc-dddd2222eeee"


class B_Binding(Collecting):
    def rollout(self, sid, t, cwd=None, out=10):
        path = fx.codex_path(_HOMES["CODEX_HOME"], iso(t)[:10], sid)
        fx.append(path, [fx.codex_meta(isoms(t), sid, cwd or self.project), fx.codex_turn(isoms(t + 1)),
                         fx.codex_tokens(isoms(t + 2), inp=100, out=out)])
        return path

    def binds(self):
        return [r for r in lines(self.costs("sessions.jsonl")) if r.get("ev") == "bind"]

    def test_b1_codex_bound_in_the_launch_window(self):
        self.record(program="codex", bind="launch-window", at=iso(T0), terminal="t6", task="t-002")
        self.rollout(SID2, T0 + 3)
        self.rollout(SID3, T0 + 500)  # outside the window
        self.rollout(SID4, T0 + 3, cwd="/elsewhere")  # another folder
        self.collect(T0 + 2000)
        b = self.binds()
        self.assertEqual([(x["session"], x["match"], x["confidence"], x["for"]) for x in b], [(SID2, "launch-window", "high", iso(T0) + " t6")])
        self.assertEqual(self.totals()["out"], 10)
        self.assertEqual({r["task"] for r in self.rows()}, {"t-002"})
        self.collect(T0 + 2100)
        self.assertEqual(len(self.binds()), 1)

    def test_b2_two_candidates_in_start_order(self):
        self.record(program="codex", bind="launch-window", at=iso(T0), terminal="t6", task="t-002")
        self.record(program="codex", bind="launch-window", at=iso(T0 + 1), terminal="t7", task="t-003")
        self.rollout(SID3, T0 + 4)
        self.rollout(SID2, T0 + 2)
        self.collect(T0 + 2000)
        b = {x["for"]: x for x in self.binds()}
        self.assertEqual((b[iso(T0) + " t6"]["session"], b[iso(T0 + 1) + " t7"]["session"]), (SID2, SID3))
        self.assertEqual({x["confidence"] for x in b.values()}, {"medium"})

    def test_b3_unbound_after_an_hour_is_a_problem(self):
        self.record(program="codex", bind="launch-window", at=iso(T0), terminal="t6", task="t-002")
        self.collect(T0 + 600)
        self.assertFalse(any("t6" in p for p in load(self.costs("state.json")).get("problems", [])))
        self.collect(T0 + 3700)
        self.assertIn("codex session for t6 not found", load(self.costs("state.json"))["problems"])

    def test_b4_claude_continue_bound_by_folder_and_start(self):
        self.record(program="claude", bind="launch-window", resumed=True, at=iso(T0), terminal="t6", task="t-002")
        self.add(self.prompt(T0 + 3, sid=SID3), self.call(T0 + 4, "m1", sid=SID3, lines=1, out=11), sid=SID3)
        self.collect(T0 + 2000)
        self.assertEqual([(x["session"], x["program"]) for x in self.binds()], [(SID3, "claude")])
        self.assertEqual(self.totals()["out"], 11)


class R_Reconstruct(Collecting):
    def session(self, sid, first, last=None, cwd=None, out=10):
        cwd = cwd or self.project
        chunks = [fx.claude_user(isoms(first), sid, cwd)] + fx.claude_call(isoms(first + 1), sid, cwd, "m-" + sid[:4], lines=1, out=out)
        if last:
            chunks += [fx.claude_user(isoms(last - 1), sid, cwd)] + fx.claude_call(isoms(last), sid, cwd, "n-" + sid[:4], lines=1, out=out)
        fx.append(fx.claude_path(pa.CLAUDE_HOME, cwd, sid), chunks)

    def reconstruct(self, **kw):
        result, out, err = self.run_cmd(pa_costs.reconstruct, self.dir, now=T0 + 5000, **kw)
        return result, out

    def binds(self):
        return {r["session"]: r for r in lines(self.costs("sessions.jsonl")) if r.get("ev") == "bind"}

    def setUp(self):
        super().setUp()
        self.wt = os.path.join(TMP, "wt-t-002")
        self.ledger({"version": 1, "tasks": [{"id": "t-002", "state": "done", "worktree": self.wt, "workers": [{"address": "t5", "role": "build", "name": "t-002-build"}]},
                                             {"id": "t-003", "state": "done", "workers": []}]})

    def test_r1x_session_log_cwd_start_pairs_live_pid_and_unmatched(self):
        write(os.path.join(self.dir, "session.log"), "{} start resume=False session={} reason=new\n".format(iso(T0), SID))
        self.session(SID, T0 + 5, out=1)  # the Project Agent
        self.journal({"at": iso(T0 + 100), "trigger": "report", "task": "t-002", "why": "Started build worker t5 (t-002-build)"},
                     {"at": iso(T0 + 195), "trigger": "report", "task": "t-003", "why": "Started t7 (t-003-verify) and"},
                     {"at": iso(T0 + 196), "trigger": "report", "task": "t-003", "why": "Started t8 (t-003-review)"},
                     {"at": iso(T0 + 401), "trigger": "report", "task": "t-003", "why": "Read t8's report"},
                     {"at": iso(T0 + 901), "trigger": "report", "task": "t-003", "why": "Read t7's report"})
        self.session(SID2, T0 + 130, cwd=self.wt, out=2)  # t5 in the task's worktree
        self.session(SID3, T0 + 200, last=T0 + 890, out=3)  # t7 (ends near its report)
        self.session(SID4, T0 + 201, last=T0 + 395, out=4)  # t8
        lone = "abababab-cdcd-4efe-8fef-0101abab0202"
        self.session(lone, T0 + 3000, out=50)  # nothing names it
        old = "cdcdcdcd-abab-4efe-8fef-0101abab0303"
        self.session(old, T0 - 7200, out=500)  # before the space began
        live = "efefefef-abab-4efe-8fef-0101abab0404"
        self.session(live, T0 + 4000, out=5)
        dump({"pid": 4242, "sessionId": live, "cwd": self.project}, os.path.join(pa.CLAUDE_HOME, "sessions", "4242.json"))
        pa.live_terminals = lambda: {"t9": {"address": "t9", "pid": 4242, "name": "t-003-tests", "parent": "t0", "state": "live"}}
        before = raw(self.costs("sessions.jsonl"))
        _r, out = self.reconstruct(dry_run=True)
        self.assertEqual(raw(self.costs("sessions.jsonl")), before)
        self.assertFalse(os.path.exists(self.costs("turns.jsonl")))
        self.assertIn("session-log", out)
        self.reconstruct()
        b = self.binds()
        self.assertEqual((b[SID]["kind"], b[SID]["match"], b[SID]["confidence"]), ("pa", "session-log", "high"))
        self.assertEqual((b[SID2]["task"], b[SID2]["terminal"], b[SID2]["match"], b[SID2]["confidence"]), ("t-002", "t5", "cwd+start", "medium"))
        self.assertEqual((b[SID3]["terminal"], b[SID3]["match"], b[SID3]["confidence"]), ("t7", "end-time-pair", "low"))
        self.assertEqual((b[SID4]["terminal"], b[SID4]["match"]), ("t8", "end-time-pair"))
        self.assertEqual((b[live]["terminal"], b[live]["task"], b[live]["match"], b[live]["confidence"]), ("t9", "t-003", "live-pid", "high"))
        self.assertNotIn(lone, b)
        self.assertNotIn(old, b)
        self.assertEqual({r["source"] for r in b.values()}, {"reconstructed"})
        self.collect(T0 + 9000)
        self.assertEqual(self.totals()["out"], 1 + 2 + 3 * 2 + 4 * 2 + 5)  # unmatched not in the totals
        self.assertEqual({r["source"] for r in self.rows()}, {"reconstructed"})
        um = [r for r in lines(self.costs("sessions.jsonl")) if r.get("ev") == "unmatched"]
        self.assertEqual((um[-1]["sessions"], um[-1]["tokens"]), (1, 52))
        after = raw(self.costs("sessions.jsonl"))
        self.reconstruct()
        self.assertEqual(raw(self.costs("sessions.jsonl")), after)  # a second run adds nothing

    def test_r2x_recorded_sessions_are_not_reconstructed(self):
        self.record(session=SID2, task="t-002", terminal="t5", name="t-002-build", cwd=self.wt)
        self.journal({"at": iso(T0 + 100), "trigger": "report", "task": "t-002", "why": "Started build worker t5 (t-002-build)"})
        self.session(SID2, T0 + 130, cwd=self.wt)
        self.reconstruct()
        self.assertEqual(self.binds(), {})


# ---- step 6: summary and report (R9 data, R10, R11) ------------------------------------------------------

def money_of(entries):
    return round(sum(e["money"] or 0 for e in entries), 6)


def tokens_of(entries):
    return sum(e["tokens"] for e in entries)


class S_Summary(Collecting):
    def mixed(self):
        """A Project Agent with a split pass, a worker with a rebuild, a reconstructed session, an unpriced model."""
        self.prices()
        self.ledger({"version": 1, "tasks": [{"id": "t-001", "title": "One", "state": "in-progress", "workers": [{"address": "t5", "role": "build", "name": "t-001-build"}]},
                                             {"id": "t-002", "title": "Two", "state": "done", "workers": []}]})
        self.record(session=SID, kind="pa", terminal="t0", parent=None)
        self.add(self.prompt(T0 + 10), self.call(T0 + 12, "p1", lines=2, out=1000, cr=50000), self.prompt(T0 + 100),
                 self.call(T0 + 102, "p2", lines=1, out=500, cw5m=1000))
        self.journal({"at": iso(T0 + 20), "trigger": "report", "task": "t-001", "pass": 1},
                     {"at": iso(T0 + 21), "trigger": "report", "task": "t-002", "pass": 1})
        self.record(session=SID2, kind="worker", terminal="t5", name="t-001-build", task="t-001", host="h2")
        self.add(self.prompt(T0 + 30, sid=SID2), self.call(T0 + 31, "w1", sid=SID2, lines=3, out=3000, cr=100000, cw5m=40000),
                 self.call(T0 + 700, "w2", sid=SID2, lines=1, out=200, cw5m=60000),  # a rebuild after 669 s idle
                 fx.claude_assistant(isoms(T0 + 701), SID2, self.project, "w3", model="model-z", out=77), sid=SID2)
        with open(self.costs("sessions.jsonl"), "a") as f:
            f.write(json.dumps({"v": 1, "ev": "bind", "at": iso(T0), "session": SID3, "program": "claude", "kind": "worker",
                                "task": "t-002", "name": "t-002-build", "terminal": "t4", "cwd": self.project,
                                "source": "reconstructed", "match": "cwd+start", "confidence": "medium"}) + "\n")
        self.add(fx.claude_user(isoms(T0 - 3600), SID3, self.project), self.call(T0 - 3590, "r1", sid=SID3, lines=1, out=4000, model="model-b"), sid=SID3)
        self.collect(T0 + 5000)
        return load(self.costs("summary.json"))

    def test_s1_every_breakdown_sums_to_the_totals(self):
        s = self.mixed()
        total_tokens = sum(r["tokens"][k] for r in self.rows() for k in r["tokens"])
        self.assertEqual(s["totals"]["tokens"]["all"], total_tokens)
        money = s["totals"]["money"]
        self.assertGreater(money, 0)
        self.assertGreater(s["totals"]["unpricedTokens"], 0)
        self.assertEqual(s["prices"]["missing"], ["model-z"])
        groups = [s["byActivity"], s["byTask"], s["byPhase"], s["byDay"], s["sessions"]] + list(s["byKind"].values())
        for g in groups:
            self.assertAlmostEqual(money_of(g), money, places=4)
            self.assertEqual(tokens_of(g), total_tokens)
        self.assertAlmostEqual(round(sum(e["money"] or 0 for e in s["byDayModel"]), 6), money, places=4)
        acts = {e["key"]: e for e in s["byActivity"]}
        self.assertIn("cache-rebuild", acts)
        self.assertEqual(acts["cache-rebuild"]["tokens"], 60000)
        tasks = {e["key"]: e for e in s["byTask"]}
        self.assertEqual((tasks["t-001"]["title"], tasks["t-001"]["state"]), ("One", "in-progress"))
        self.assertTrue(tasks["t-001"]["split"])
        self.assertGreater(s["totals"]["reconstructedMoney"], 0)
        self.assertEqual({e["key"] for e in s["byKind"]["machine"]}, {"h1", "h2", "unknown"})
        self.assertEqual(s["passes"][0]["pass"], 1)
        self.assertEqual({x["state"] for x in s["sessions"]}, {"open"})
        self.assertEqual(s["account"], "default")
        self.assertEqual((s["prices"]["source"], s["prices"]["asOf"], s["prices"]["currency"]), ("test table", "2026-11-01", "USD"))
        self.assertLessEqual(len(raw(self.costs("summary.json"))), 65536)

    def test_s2_summary_stays_under_64_kb(self):
        rows, recs = [], []
        for i in range(1000):
            sid = "{:08x}-0000-4000-8000-{:012x}".format(i, i)
            day = T0 - (i % 120) * 86400
            recs.append({"v": 1, "ev": "start", "at": iso(day), "session": sid, "program": "claude", "kind": "worker",
                         "task": "t-{:03d}".format(i % 300), "name": "t-{:03d}-build".format(i % 300), "terminal": "t{}".format(i), "host": "h1"})
            rows.append({"v": 1, "seq": i + 1, "session": sid, "program": "claude", "kind": "worker", "model": "model-a",
                         "from": iso(day), "to": iso(day + 60), "day": iso(day)[:10], "calls": 3, "tokens": toks(out=1000 + i, cr=50000),
                         "long": dict(ZERO), "rebuild": {"calls": 0, "cw5m": 0, "cw1h": 0, "gapS": 0}, "turn": iso(day),
                         "task": "t-{:03d}".format(i % 300), "phase": "build", "activity": "build", "role": "build", "pass": None,
                         "trigger": None, "split": None, "reason": None, "source": "recorded", "match": "launch-record", "confidence": "high"})
        os.makedirs(os.path.join(self.dir, "costs"), exist_ok=True)
        write(self.costs("sessions.jsonl"), "".join(json.dumps(r) + "\n" for r in recs))
        write(self.costs("turns.jsonl"), "".join(json.dumps(r) + "\n" for r in rows))
        self.prices()
        pa_costs.write_summary(self.dir, now=T0)
        s = load(self.costs("summary.json"))
        self.assertLessEqual(len(raw(self.costs("summary.json"))), 65536)
        self.assertLessEqual(len(s["byDay"]), 93)
        self.assertLessEqual(len(s["sessions"]), 200)
        self.assertAlmostEqual(money_of(s["byDay"]), s["totals"]["money"], places=3)

    def test_rp1_report_text(self):
        self.mixed()
        _r, out, _e = self.run_cmd(pa_costs.report, self.dir, now=T0 + 5000)
        for want in ("est.", "test table", "2026-11-01", "USD", "By activity", "cache-rebuild", "By task", "t-001", "includes", "reconstructed",
                     "No price: model-z"):
            self.assertIn(want, out)
        _r, only, _e = self.run_cmd(pa_costs.report, self.dir, by="task", now=T0 + 5000)
        self.assertIn("t-002", only)
        self.assertNotIn("By activity", only)
        _r, later, _e = self.run_cmd(pa_costs.report, self.dir, by="day", since="2026-11-17", now=T0 + 5000)
        later = "\n".join(x for x in later.splitlines() if x.startswith("  "))  # the table, not the header's times
        self.assertIn("2026-11-17", later)
        self.assertNotIn("2026-11-16", later)
        _r, earlier, _e = self.run_cmd(pa_costs.report, self.dir, by="day", until="2026-11-16", now=T0 + 5000)
        earlier = "\n".join(x for x in earlier.splitlines() if x.startswith("  "))
        self.assertIn("2026-11-16", earlier)
        self.assertNotIn("2026-11-17", earlier)

    def test_rp2_report_json_is_the_summary_shape(self):
        s = self.mixed()
        _r, out, _e = self.run_cmd(pa_costs.report, self.dir, as_json=True, now=T0 + 5000)
        j = json.loads(out)
        self.assertEqual(sorted(j), sorted(s))
        self.assertAlmostEqual(j["totals"]["money"], s["totals"]["money"], places=4)

    def test_rp3_no_price_table_is_tokens_only(self):
        self.record(session=SID, task="t-001")
        self.add(self.prompt(T0 + 1), self.call(T0 + 2, "m1", out=30))
        self.collect(T0 + 2000)
        s = load(self.costs("summary.json"))
        self.assertIsNone(s["totals"]["money"])
        self.assertIsNone(s["prices"]["source"])
        _r, out, _e = self.run_cmd(pa_costs.report, self.dir, now=T0 + 2000)
        self.assertIn("No price table in profile test: tokens only", out)

    def test_aw1_account_wide_sums_spaces_with_the_same_account(self):
        self.mixed()
        other = os.path.join(pa.ROOT, "spaces", "sp2", "costs")
        mine = load(self.costs("summary.json"))
        dump(dict(mine, space="sp2"), os.path.join(other, "summary.json"))
        dump(dict(mine, space="sp3", account="elsewhere"), os.path.join(pa.ROOT, "spaces", "sp3", "costs", "summary.json"))
        _r, out, _e = self.run_cmd(pa_costs.report, self.dir, account_wide=True, now=T0 + 5000)
        j = json.loads(out)
        self.assertEqual((j["account"], sorted(j["spaces"])), ("default", ["sp1", "sp2"]))
        self.assertAlmostEqual(sum(e["money"] or 0 for e in j["byDayModel"]), 2 * money_of(mine["byDayModel"]), places=3)

    def test_t1_collect_with_nothing_new_is_cheap(self):
        for i in range(50):
            sid = "{:08x}-1111-4111-8111-1111abcd{:04x}".format(i, i)
            self.record(session=sid, task="t-001", terminal="t{}".format(i))
            self.add(self.prompt(T0 + 1, sid=sid), self.call(T0 + 2, "m{}".format(i), sid=sid, lines=2, out=5), sid=sid)
        self.collect(T0 + 2000)
        self.opened[:] = []
        best = None
        for _ in range(3):
            began = time.time()
            self.collect(T0 + 2010)
            took = time.time() - began
            best = took if best is None else min(best, took)
        self.assertLess(best, 0.2 * 5)
        self.assertEqual({os.path.basename(p) for p in self.opened}, {"state.json", "sessions.jsonl"})

    def test_rs1_resummarize_reprices_without_reading_session_files(self):
        self.mixed()
        before = load(self.costs("summary.json"))["totals"]["money"]
        t = json.loads(json.dumps(PRICES))
        for e in t["models"].values():
            for k in ("in", "out", "cw5m", "cw1h", "cr"):
                e[0][k] *= 2
        self.prices(t)
        self.opened[:] = []
        self.collect(T0 + 5001, resummarize=True)
        self.assertAlmostEqual(load(self.costs("summary.json"))["totals"]["money"], 2 * before, places=4)
        self.assertFalse([p for p in self.opened if p.startswith(pa.CLAUDE_HOME) or p.startswith(_HOMES["CODEX_HOME"])])

    def test_n1_no_network_module(self):
        import ast
        with open(os.path.join(TOOLS, "pa_costs.py")) as f:
            tree = ast.parse(f.read())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[0])
        self.assertFalse(names & {"socket", "urllib", "http", "ssl", "ftplib", "smtplib", "requests"}, names)


# ---- step 7: billed rows, import fallback, scheduled query (R13-R15; plan section 6) -------------------------

Q1 = r"""#!/bin/sh
# A stub billing query: records its arguments, prints what the test put beside it. No network, no account.
d=$(dirname "$0"); mode=$2
echo "$*" >> "$d/q1.calls"
[ -f "$d/q1.$mode.err" ] && cat "$d/q1.$mode.err" >&2
[ -f "$d/q1.$mode.sleep" ] && sleep "$(cat "$d/q1.$mode.sleep")"
[ -f "$d/q1.$mode.out" ] && cat "$d/q1.$mode.out"
exit "$(cat "$d/q1.$mode.code" 2>/dev/null || echo 0)"
"""
F1 = r"""#!/usr/bin/env python3
# A stub importer: reads JSON lines from the file it is given and prints them, then the done line.
import sys
path = sys.argv[sys.argv.index("--file") + 1]
for line in open(path):
    if line.strip():
        print(line.strip())
print('{"done": true}')
"""


TWELVE = "4321" * 3  # a 12-digit number, built so the public check does not see one in the source
TWELVE2 = "9876" * 3


def brow(day="2026-11-16", amount=1.0, **kw):
    r = {"account": "acct1", "from": day, "to": day, "label": "usage-a-out", "model": "model-a", "kind": "out",
         "quantity": None, "unit": None, "amount": amount, "currency": "USD", "final": True, "accountWide": True}
    r.update(kw)
    return r


class BillingBase(CostsBase):
    def setUp(self):
        super().setUp()
        self.prices()
        prof = {"name": "test", "claude": ["true"], "codex": ["true"], "billing": {
            "account": "acct1",
            "queries": {"q1": {"run": ["billing/q1.sh", "--mode", "{mode}", "--credential", "{credential}", "--from", "{from}",
                                       "--to", "{to}", "--max-pages", "{maxPages}"],
                               "readOnly": True, "calls": ["identity-read", "usage-read"], "feePerRequest": 0.01, "currency": "USD",
                               "label": "test query"}},
            "importers": {"f1": {"run": ["billing/f1.py", "--file", "{file}"], "label": "test file"}}}}
        dump(prof, os.path.join(self.profile_dir, "profile.json"))
        self.bdir = os.path.join(self.profile_dir, "billing")
        for name, text in (("q1.sh", Q1), ("f1.py", F1)):
            write(os.path.join(self.bdir, name), text)
            os.chmod(os.path.join(self.bdir, name), 0o755)
        self.check_ok()
        self.saved_tty = pa_costs.stdin_is_tty
        pa_costs.stdin_is_tty = lambda: True
        self.addCleanup(setattr, pa_costs, "stdin_is_tty", self.saved_tty)

    def stub(self, mode, out=None, code=None, err=None, sleep=None):
        for suffix, value in (("out", out), ("code", code), ("err", err), ("sleep", sleep)):
            path = os.path.join(self.bdir, "q1.{}.{}".format(mode, suffix))
            if value is None:
                if os.path.exists(path):
                    os.unlink(path)
            else:
                write(path, value if isinstance(value, str) else str(value))

    def check_ok(self, identity="ExampleReadOnly", read_only=True):
        self.stub("check", out=json.dumps({"identity": identity, "readOnly": read_only, "why": "test"}) + "\n")

    def query_out(self, rows, requests=1):
        self.stub("query", out="".join(json.dumps(r) + "\n" for r in rows) + json.dumps({"done": True, "requests": requests}) + "\n")

    def calls(self):
        data = raw(os.path.join(self.bdir, "q1.calls"))
        return data.decode().splitlines() if data else []

    def modes(self):
        return [c.split()[1] for c in self.calls()]

    def enable(self, credential="ExampleReadOnly", match="ReadOnly$", confirm=None, **kw):
        return self.run_cmd(pa_costs.billed_enable, "q1", credential, match, now=T0,
                            ask=lambda _prompt: credential if confirm is None else confirm, **kw)

    def fetch(self, mode="scheduled", now=T0, **kw):
        return self.run_cmd(pa_costs.billed_fetch, mode=mode, now=now, **kw)

    def billed(self):
        return lines(os.path.join(pa.ROOT, "billing", "billed.jsonl"))

    def qstate(self):
        return pa.read_json(os.path.join(pa.ROOT, "billing", "query-state.json"), {}) or {}

    def conf(self):
        return (load(os.path.join(pa.ROOT, "config.json")).get("costs") or {}).get("billing")


class BQ_Billing(BillingBase):
    def test_bq1_enable_refuses_inside_an_agent_and_without_a_tty(self):
        for marker in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_SANDBOX", "CODEX_HOME_SESSION"):
            os.environ[marker] = "1"
            try:
                with self.assertRaises(pa.Problem):
                    pa_costs.billed_enable("q1", "ExampleReadOnly", "ReadOnly$", now=T0, ask=lambda p: "ExampleReadOnly")
            finally:
                os.environ.pop(marker)
        pa_costs.stdin_is_tty = lambda: False
        with self.assertRaises(pa.Problem) as e:
            pa_costs.billed_enable("q1", "ExampleReadOnly", "ReadOnly$", now=T0, ask=lambda p: "ExampleReadOnly")
        self.assertIn("terminal", str(e.exception))
        self.assertEqual(self.calls(), [])
        self.assertIsNone(self.conf())

    def test_bq2_enable_refuses_wide_or_missing_credentials(self):
        for cred in ("", "default", "Admin", "TeamAdministrator", "PowerUserAccess", "x-FullAccess", "root", "AccountOwner"):
            with self.assertRaises(pa.Problem, msg=cred):
                pa_costs.billed_enable("q1", cred, ".*", now=T0, ask=lambda p: cred)
        self.assertEqual(self.calls(), [])
        with self.assertRaises(pa.Problem):  # the confirmation must be typed again exactly
            pa_costs.billed_enable("q1", "ExampleReadOnly", "ReadOnly$", now=T0, ask=lambda p: "examplereadonly")
        self.assertIsNone(self.conf())

    def test_bq3_enable_runs_check_once_and_refuses_not_read_only(self):
        self.check_ok(read_only=False)
        with self.assertRaises(pa.Problem) as e:
            pa_costs.billed_enable("q1", "ExampleReadOnly", "ReadOnly$", now=T0, ask=lambda p: "ExampleReadOnly")
        self.assertIn("read-only", str(e.exception))
        self.assertEqual(self.modes(), ["check"])
        self.assertIsNone(self.conf())
        self.check_ok()
        _r, out, _e = self.enable()
        self.assertEqual(self.modes(), ["check", "check"])
        c = self.conf()
        self.assertEqual((c["query"], c["credential"], c["readOnlyMatch"], c["enabled"], c["enabledBy"]),
                         ("q1", "ExampleReadOnly", "ReadOnly$", True, "terminal"))
        for want in ("q1", "ExampleReadOnly", "identity-read", "usage-read", "0.01", "USD", "1 per day"):
            self.assertIn(want, out)

    def test_bq4_scheduled_with_nothing_enabled_does_nothing(self):
        result, _o, _e = self.fetch()
        self.assertEqual(result["result"], "skipped")
        self.assertEqual(self.calls(), [])

    def test_bq5_scheduled_runs_at_most_once_a_day(self):
        self.enable()
        self.query_out([brow()])
        self.fetch(now=T0 + 60)
        self.fetch(now=T0 + 3600)
        self.fetch(now=T0 + 86000)
        self.assertEqual(self.modes().count("query"), 1)
        self.fetch(now=T0 + 60 + 86400)
        self.assertEqual(self.modes().count("query"), 2)

    def test_bq6_now_and_scheduled_at_most_three_a_day(self):
        self.enable()
        self.query_out([brow()])
        self.fetch(mode="now", now=T0 + 10)
        self.fetch(mode="now", now=T0 + 20)
        self.fetch(mode="now", now=T0 + 30)
        with self.assertRaises(pa.Problem):
            pa_costs.billed_fetch(mode="now", now=T0 + 40)
        self.assertEqual(self.fetch(now=T0 + 50)[0]["result"], "skipped")
        self.assertEqual(self.modes().count("query"), 3)
        self.assertEqual(self.qstate()["runsToday"], {"day": "2026-11-17", "n": 3})

    def test_bq7_identity_not_matching_is_refused_and_recorded(self):
        self.enable()
        self.check_ok(identity="SomeOtherRole")
        result, _o, _e = self.fetch()
        self.assertEqual(result["result"], "refused")
        self.assertNotIn("query", self.modes())
        self.assertEqual(self.qstate()["result"], "refused")
        self.check_ok(identity="AdminReadOnly")  # matches readOnlyMatch, but looks like an admin
        self.assertEqual(self.fetch(now=T0 + 86400 + 1)[0]["result"], "refused")
        self.assertNotIn("query", self.modes())

    def test_bq8_terminal_rules(self):
        self.enable()
        self.query_out([brow()])
        os.environ["HC_TERMINAL_ID"] = "t1"
        self.addCleanup(os.environ.pop, "HC_TERMINAL_ID", None)
        self.hc.terminals["t1"] = {"address": "t1", "name": "plain", "state": "live", "labels": {}}
        with self.assertRaises(pa.Problem):
            pa_costs.billed_fetch(mode="scheduled", now=T0)
        self.hc.terminals["t1"]["parent"] = "t0"  # a worker
        with self.assertRaises(pa.Problem):
            pa_costs.billed_fetch(mode="now", now=T0)
        del self.hc.terminals["t1"]["parent"]
        self.assertEqual(pa_costs.billed_fetch(mode="now", now=T0, quiet=True)["result"], "ok")  # the person's own tab
        self.assertEqual(self.modes().count("query"), 1)

    def test_bq9_query_entry_must_declare_read_only_and_calls(self):
        prof = load(os.path.join(self.profile_dir, "profile.json"))
        for change in ({"readOnly": False}, {"calls": []}, {"readOnly": None}):
            q = dict(prof["billing"]["queries"]["q1"], **change)
            p2 = json.loads(json.dumps(prof))
            p2["billing"]["queries"]["q1"] = {k: v for k, v in q.items() if v is not None}
            dump(p2, os.path.join(self.profile_dir, "profile.json"))
            with self.assertRaises(pa.Problem, msg=change):
                pa_costs.billed_enable("q1", "ExampleReadOnly", "ReadOnly$", now=T0, ask=lambda p: "ExampleReadOnly")
        self.assertEqual(self.calls(), [])

    def test_bq10_more_requests_than_the_cap_rejects_the_batch(self):
        self.enable()
        self.query_out([brow()], requests=4)
        result, _o, _e = self.fetch()
        self.assertEqual(result["result"], "failed")
        self.assertEqual(self.billed(), [])
        self.assertIn("--max-pages 3", self.calls()[-1])

    def test_bq11_a_twelve_digit_number_rejects_the_batch(self):
        self.enable()
        for bad in (brow(label="usage-{}-x".format(TWELVE)), brow(account=TWELVE), brow(note="acct " + TWELVE2)):
            self.query_out([brow(day="2026-11-15"), bad])
            result, _o, _e = self.fetch(mode="now", now=T0 + len(self.calls()))
            self.assertEqual(result["result"], "failed")
        self.assertEqual(self.billed(), [])

    def test_bq12_rows_appended_and_refetch_replaces_by_id(self):
        self.enable()
        self.query_out([brow(day="2026-11-15", amount=1.0), brow(day="2026-11-16", amount=2.0, final=False)])
        self.assertEqual(self.fetch(mode="now", now=T0 + 10)[0]["result"], "ok")
        self.query_out([brow(day="2026-11-16", amount=2.5)])
        self.fetch(mode="now", now=T0 + 20)
        rows = pa_costs.effective_billed()
        self.assertEqual(sorted((r["from"], r["amount"]) for r in rows), [("2026-11-15", 1.0), ("2026-11-16", 2.5)])
        self.assertEqual({r["source"] for r in rows}, {"query:q1"})
        self.assertEqual(len({r["id"] for r in rows}), 2)
        call = self.calls()[-1].split()
        self.assertEqual(call[call.index("--from") + 1], "2026-10-13")  # the last 35 days
        self.assertEqual(call[call.index("--to") + 1], "2026-11-17")
        self.assertEqual(call[call.index("--credential") + 1], "ExampleReadOnly")
        qs = self.qstate()
        self.assertEqual((qs["result"], qs["requests"], qs["identity"]), ("ok", 1, "ExampleReadOnly"))

    def test_bq13_failures_are_plain_and_wait_for_tomorrow(self):
        self.enable()
        for code, words in ((4, "permission denied"), (5, "not enabled for this account")):
            self.stub("query", out="", code=code)
            result, _o, _e = self.fetch(mode="now", now=T0 + code)
            self.assertEqual(result["result"], "failed")
            self.assertIn(words, self.qstate()["lastError"])
        saved = pa_costs.SCRIPT_TIMEOUT
        pa_costs.SCRIPT_TIMEOUT = 1
        try:
            self.stub("query", out="", code=0, sleep=3)
            result, _o, _e = self.fetch(mode="now", now=T0 + 10)
        finally:
            pa_costs.SCRIPT_TIMEOUT = saved
        self.assertEqual(result["result"], "failed")
        self.assertIn("timed out", self.qstate()["lastError"])
        self.assertEqual(self.billed(), [])
        self.stub("query", sleep=None)
        self.query_out([brow()])
        self.assertEqual(self.fetch(now=T0 + 3600)[0]["result"], "skipped")  # the next scheduled run is tomorrow

    def test_bq14_disable_stops_the_schedule_and_keeps_rows(self):
        self.enable()
        self.query_out([brow()])
        self.fetch()
        self.run_cmd(pa_costs.billed_disable)
        self.assertFalse(self.conf()["enabled"])
        self.assertEqual(self.fetch(now=T0 + 2 * 86400)[0]["result"], "skipped")
        self.assertEqual(len(pa_costs.effective_billed()), 1)
        _r, out, _e = self.run_cmd(pa_costs.billed_status, now=T0 + 2 * 86400)
        self.assertIn("off", out)

    def test_bq15_import_through_the_profile_importer(self):
        src = os.path.join(TMP, "export.jsonl")
        write(src, json.dumps(brow(day="2026-11-10", amount=3.0)) + "\n" + json.dumps(brow(day="2026-11-11", amount=4.0)) + "\n")
        result, out, _e = self.run_cmd(pa_costs.billed_import, src, "f1", now=T0)
        rows = pa_costs.effective_billed()
        self.assertEqual(sorted(r["amount"] for r in rows), [3.0, 4.0])
        self.assertEqual({r["source"] for r in rows}, {"file:f1"})
        self.run_cmd(pa_costs.billed_import, src, "f1", now=T0 + 5)  # importing the same file again: no duplicates
        self.assertEqual(len(pa_costs.effective_billed()), 2)
        write(src, json.dumps(brow(day="2026-11-12")) + "\n" + json.dumps(brow(day="11/13/2026")) + "\n")
        with self.assertRaises(pa.Problem):
            pa_costs.billed_import(src, "f1", now=T0)
        self.assertEqual(len(pa_costs.effective_billed()), 2)
        os.environ["CLAUDECODE"] = "1"
        try:
            with self.assertRaises(pa.Problem):
                pa_costs.billed_import(src, "f1", now=T0)
        finally:
            os.environ.pop("CLAUDECODE")
        rid = rows[0]["id"]
        self.run_cmd(pa_costs.billed_remove, rid)
        self.assertEqual([r["id"] for r in pa_costs.effective_billed()], [r["id"] for r in rows[1:]])
        _r, listed, _e = self.run_cmd(pa_costs.billed_list)
        self.assertIn("2026-11-11", listed)
        self.assertNotIn("2026-11-10", listed)

    def test_bq20_stderr_is_scrubbed(self):
        self.enable()
        self.stub("query", out="", code=1, err="line1\nrole arn with {} in it\nline3\n".format(TWELVE))
        self.fetch(mode="now", now=T0 + 10)
        text = json.dumps(self.qstate())
        self.assertNotIn(TWELVE, text)
        self.assertIn("line3", text)

    def test_ba1_there_is_no_billed_add(self):
        for argv in (["billed", "add"], ["billed", "add", "--amount", "1"]):
            result, _o, err = self.run_cmd(pa_costs.main, argv)
            self.assertEqual(result, 2)  # argparse: invalid choice
            self.assertIn("invalid choice", err)

    def test_bq_status_line(self):
        self.enable()
        self.query_out([brow()], requests=2)
        self.fetch(now=T0 + 60)
        _r, out, _e = self.run_cmd(pa_costs.billed_status, now=T0 + 120)
        for want in ("Billing query: last run", "ok", "2 requests", "0.02 USD", "next after", "cap 1/day", "3 requests", "Turn off"):
            self.assertIn(want, out)


class BC_Comparison(BillingBase):
    def summary(self, rows):
        """A space summary and an all-spaces summary with the given byDayModel (day, model, money)."""
        bdm = [{"day": d, "model": m, "tokens": toks(out=1), "money": v} for d, m, v in rows]
        return {"account": "acct1", "updated": "2026-11-17T09:00:00Z", "byDayModel": bdm, "prices": {"currency": "USD"}}

    def compare(self, billed, space_rows, all_rows=None):
        table, _p = pa_costs.load_prices(pa.load_profile("test"))
        rows = [pa_costs.validate_row(dict(r), "query:q1", "acct1", T0) for r in billed]
        return pa_costs.billing_comparison(rows, self.summary(space_rows), self.summary(all_rows or space_rows), table)

    def test_bq16_price_factor_from_quantities(self):
        days = ["2026-11-1{}".format(i) for i in range(3, 6)]
        billed = [brow(day=d, amount=12.5, quantity=1000000, unit="tokens") for d in days]  # 1M out tokens at 10 = 10.00
        c = self.compare(billed, [(d, "model-a", 8.0) for d in days])
        m = c["models"][0]
        self.assertEqual((m["model"], m["factor"]["kind"]), ("model-a", "price"))
        self.assertAlmostEqual(m["factor"]["value"], 1.25)
        self.assertTrue(m["factor"]["applied"])
        self.assertFalse(m["factor"]["few"])
        self.assertAlmostEqual(c["calibrated"], 30.0)
        self.assertTrue(c["accountWide"])

    def test_bq17_scope_ratio_shown_not_applied(self):
        days = ["2026-11-1{}".format(i) for i in range(3, 6)]
        c = self.compare([brow(day=d, amount=1.0) for d in days], [(d, "model-a", 1.0) for d in days], [(d, "model-a", 2.0) for d in days])
        f = c["models"][0]["factor"]
        self.assertEqual((f["kind"], f["applied"]), ("scope", False))
        self.assertAlmostEqual(f["value"], 0.5)
        self.assertIn("over-prices", f["note"])
        self.assertAlmostEqual(c["calibrated"], 3.0)  # nothing applied

    def test_bq18_partial_coverage_is_named(self):
        c = self.compare([brow(day="2026-11-14"), brow(day="2026-11-16", final=False)],
                         [("2026-11-14", "model-a", 1.0), ("2026-11-15", "model-a", 1.0), ("2026-11-16", "model-a", 1.0)])
        self.assertEqual(c["covered"]["missing"], ["2026-11-15", "2026-11-17"])
        self.assertTrue(c["models"][0]["factor"]["few"])
        self.assertTrue(c["models"][0]["notFinal"])
        self.assertAlmostEqual(c["models"][0]["thisSpace"], 2.0)  # the uncovered day is excluded

    def test_bq19_other_currency_and_other_account(self):
        c = self.compare([brow(day="2026-11-14", currency="EUR", amount=9.0), brow(day="2026-11-14", account="acct2", amount=7.0),
                          brow(day="2026-11-14", amount=1.0)], [("2026-11-14", "model-a", 1.0)])
        self.assertEqual([(x["currency"], x["amount"]) for x in c["separate"]], [("EUR", 9.0)])
        self.assertAlmostEqual(c["total"]["billed"], 1.0)


# ---- step 8: the closed event and the guard (R5, R15) ------------------------------------------------------

class W_CloseAndGuard(CostsBase):
    def close_one(self):
        ledger = {"version": 1, "tasks": [{"id": "t-001", "state": "in-progress",
                                           "workers": [{"address": "t5", "role": "build", "name": "t-001-build"}]}]}
        self.ledger(ledger)
        task = ledger["tasks"][0]
        return pa_hooks.end_worker(self.dir, ledger, task, task["workers"][0], "t5", {}, "evidence/t-001/REPORT.md", "agent", "close")

    def test_w1_close_appends_one_closed_event(self):
        line = self.close_one()
        self.assertIn("Closed worker t5", line)
        recs = lines(self.costs("sessions.jsonl"))
        self.assertEqual([(r["ev"], r["terminal"], r["by"]) for r in recs], [("closed", "t5", "pa-worker close")])
        self.assertRegex(recs[0]["at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_w1b_a_failing_append_does_not_change_close(self):
        write(os.path.join(self.dir, "costs"), "a file where the costs folder should be\n")
        line = self.close_one()
        self.assertIn("Closed worker t5", line)
        self.assertTrue(load(os.path.join(self.dir, "ledger.json"))["tasks"][0]["workers"][0]["finished"])
        saved = pa.COSTS_FILE
        pa.COSTS_FILE = os.path.join(TMP, "no-such-costs.py")
        pa._COSTS.clear()
        try:
            os.unlink(os.path.join(self.dir, "costs"))
            self.assertIn("Closed worker t5", self.close_one())
        finally:
            pa.COSTS_FILE = saved
            pa._COSTS.clear()
        self.assertFalse(os.path.exists(self.costs("sessions.jsonl")))

    def test_w1c_collector_marks_the_session_closed(self):
        self.record(session=SID, task="t-001", terminal="t5", name="t-001-build", at=iso(T0))
        fx.append(fx.claude_path(pa.CLAUDE_HOME, self.project, SID), [fx.claude_user(isoms(T0 + 1), SID, self.project)]
                  + fx.claude_call(isoms(T0 + 2), SID, self.project, "m1", lines=1, out=5))
        self.close_one()
        pa_costs.collect(self.dir, now=T0 + 2000)
        s = load(self.costs("summary.json"))
        self.assertEqual([x["state"] for x in s["sessions"]], ["closed"])
        pa_costs.collect(self.dir, now=T0 + 2000 + 86400 + 1)
        self.assertTrue(load(self.costs("state.json"))["files"][SID]["done"])

    def guard(self, command):
        err = io.StringIO()
        data = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": self.project}
        with contextlib.redirect_stderr(err):
            try:
                code = pa_hooks.hook_guard(data, [])
            except SystemExit as e:
                code = e.code
        return code, err.getvalue()

    def test_w2_guard_blocks_the_persons_billing_commands(self):
        tool = "pa-" + "costs"
        for command in (tool + " billed fetch --now", tool + " billed fetch --scheduled --quiet",
                        tool + " billed enable --query q1 --credential X --read-only-match X", tool + " billed import f --via f1",
                        tool + " 'billed' fetch --now", tool + ' billed "enable"', "cd /tmp && " + tool + " billed\\ fetch --now",
                        "~/.local/bin/" + tool + " billed fetch --now"):
            code, err = self.guard(command)
            self.assertEqual(code, 2, command)
            self.assertIn("billing command", err, command)
        for command in (tool + " report", tool + " report --billed", tool + " billed status", tool + " billed list",
                        tool + " billed disable", tool + " collect --space sp1 --quiet"):
            self.assertEqual(self.guard(command)[0], 0, command)


# ---- fix round 2: review M1-M3, verify M1-M2 ---------------------------------------------------------------

class FR_FixRound2(Collecting):
    def pa_turn(self, t, mid, sid=SID, inp=2, out=10):
        self.add(self.prompt(t, sid=sid), self.call(t + 2, mid, lines=1, out=out, inp=inp, sid=sid), sid=sid)

    def test_fr_m1_transcript_written_late_is_still_counted(self):
        # Review M1: the program writes its transcript at the first message, which can come hours after the start.
        self.record(session=SID, kind="pa", terminal="t0", parent=None, at=iso(T0))
        self.collect(T0 + 700)
        self.collect(T0 + 3 * 3600)
        self.assertFalse([r for r in lines(self.costs("sessions.jsonl")) if r.get("ev") == "gone"])
        self.pa_turn(T0 + 4 * 3600, "m1", out=650)
        self.collect(T0 + 5 * 3600)
        self.assertEqual(self.totals()["out"], 650)
        self.assertFalse(any("gone before" in p for p in load(self.costs("state.json")).get("problems", [])))

    def test_fr_m1_a_later_start_record_brings_a_gone_session_back(self):
        self.record(session=SID, kind="pa", terminal="t0", parent=None, at=iso(T0))
        later = T0 + pa_costs.GONE_UNSEEN + 10
        self.collect(later)
        self.assertEqual([r["session"] for r in lines(self.costs("sessions.jsonl")) if r.get("ev") == "gone"], [SID])
        self.record(session=SID, kind="pa", terminal="t0", parent=None, at=iso(later + 100), resumed=True)
        self.pa_turn(later + 200, "m1", out=40)
        self.collect(later + 3000)
        self.assertEqual(self.totals()["out"], 40)
        s = load(self.costs("summary.json"))
        self.assertEqual([x["state"] for x in s["sessions"]], ["open"])

    def test_fr_m2_resumed_after_install_keeps_its_earlier_spend(self):
        # Review M2: the Project Agent was running before install; its next start is a --resume of the same id.
        for i in range(4):
            self.pa_turn(T0 - 3000 + i * 100, "old{}".format(i), inp=100, out=95)
        self.record(session=SID, kind="pa", terminal="t0", parent=None, at=iso(T0), resumed=True)
        for i in range(2):
            self.pa_turn(T0 + 100 + i * 100, "new{}".format(i), inp=100, out=95)
        self.collect(T0 + 2000)
        self.run_cmd(pa_costs.reconstruct, self.dir, now=T0 + 2100)
        self.collect(T0 + 4000)
        self.assertEqual(self.totals()["in"], 600)
        self.assertEqual(sum(r["calls"] for r in self.rows()), 6)
        early = [r for r in self.rows() if r["from"] < iso(T0)]
        self.assertEqual(sum(r["calls"] for r in early), 4)
        self.assertEqual({r["source"] for r in early}, {"reconstructed"})  # shown as best effort, R7
        self.assertEqual({r["source"] for r in self.rows() if r["from"] >= iso(T0)}, {"recorded"})
        self.assertFalse([r for r in lines(self.costs("sessions.jsonl")) if r.get("ev") == "bind"])

    def m3_space(self):
        self.record(session=SID, task="t-001")
        self.record(session=SID2, task="t-002")
        self.add(self.prompt(T0 + 1), self.call(T0 + 2, "m1", out=30, inp=500))
        self.add(self.prompt(T0 + 1, sid=SID2), self.call(T0 + 2, "m2", out=7, inp=5, sid=SID2), sid=SID2)
        self.collect(T0 + 2000)
        before = self.totals()
        os.unlink(self.transcript(SID2))  # swept: its rows are the only record left
        self.collect(T0 + 2100)
        self.assertEqual(self.totals(), before)
        return before

    def test_fr_m3_missing_state_json_rebuilds_and_keeps_every_row(self):
        before = self.m3_space()
        os.unlink(self.costs("state.json"))
        self.collect(T0 + 2200)
        self.assertEqual(self.totals(), before)
        state = load(self.costs("state.json"))
        self.assertTrue(any("rebuilt from turns.jsonl" in p for p in state["problems"]), state["problems"])
        self.collect(T0 + 2300)
        self.assertEqual(self.totals(), before)
        seqs = [r["seq"] for r in lines(self.costs("turns.jsonl")) if "seq" in r]
        self.assertEqual(len(seqs), len(set(seqs)))

    def test_fr_m3_unreadable_state_json_rebuilds_and_keeps_the_bad_copy(self):
        before = self.m3_space()
        write(self.costs("state.json"), "{not json")
        self.collect(T0 + 2200)
        self.assertEqual(self.totals(), before)
        kept = [n for n in os.listdir(os.path.join(self.dir, "costs")) if n.startswith("state.json.bad-")]
        self.assertEqual(len(kept), 1)
        self.assertEqual(raw(os.path.join(self.dir, "costs", kept[0])), b"{not json")

    def test_fr_m3_unknown_state_version_is_refused_and_nothing_changes(self):
        self.m3_space()
        state = load(self.costs("state.json"))
        state["v"] = 2
        dump(state, self.costs("state.json"))
        before = self.costs_bytes()
        with self.assertRaises(pa.Problem) as e:
            pa_costs.collect(self.dir, now=T0 + 2200)
        self.assertIn("state.json", str(e.exception))
        self.assertEqual(self.costs_bytes(), before)

    def test_fr_vm1_reconstruct_does_not_bind_to_a_recorded_worker(self):
        # Verify M1: the journal's start entry of t5 belongs to t5's own recorded session.
        self.record(session=SID2, task="t-001", terminal="t5", name="t-001-build", at=iso(T0 + 99))
        self.add(self.prompt(T0 + 100, sid=SID2), self.call(T0 + 101, "w1", out=20, sid=SID2), sid=SID2)
        self.journal({"at": iso(T0 + 100), "trigger": "report", "task": "t-001", "why": "Started build worker t5 (t-001-build)"})
        self.add(self.prompt(T0 + 130, sid=SID3), self.call(T0 + 131, "h1", out=10000, sid=SID3), sid=SID3)  # run by hand
        self.run_cmd(pa_costs.reconstruct, self.dir, now=T0 + 5000)
        self.assertFalse([r for r in lines(self.costs("sessions.jsonl")) if r.get("ev") == "bind"])
        um = [r for r in lines(self.costs("sessions.jsonl")) if r.get("ev") == "unmatched"]
        self.assertEqual(um[-1]["sessions"], 1)
        self.assertEqual(self.totals()["out"], 20)

    def test_fr_vm1_codex_rollout_is_left_to_its_launch_record(self):
        self.record(program="codex", bind="launch-window", at=iso(T0 + 100), terminal="t6", task="t-003", name="t-003-build")
        self.journal({"at": iso(T0 + 101), "trigger": "report", "task": "t-003", "why": "Started t6 (t-003-build)"})
        path = fx.codex_path(_HOMES["CODEX_HOME"], iso(T0)[:10], SID4)
        fx.append(path, [fx.codex_meta(isoms(T0 + 102), SID4, self.project), fx.codex_turn(isoms(T0 + 103)),
                         fx.codex_tokens(isoms(T0 + 104), inp=100, out=10)])
        self.run_cmd(pa_costs.reconstruct, self.dir, now=T0 + 5000)
        binds = [r for r in lines(self.costs("sessions.jsonl")) if r.get("ev") == "bind"]
        self.assertEqual([(b["session"], b["match"]) for b in binds], [(SID4, "launch-window")])
        um = [r for r in lines(self.costs("sessions.jsonl")) if r.get("ev") == "unmatched"]
        self.assertFalse(um and um[-1]["sessions"])
        self.assertEqual(self.totals()["out"], 10)

    def test_fr_vm2_held_project_agent_turn_is_not_counted_twice_on_a_void(self):
        # Verify M2: the first turn is held for its journal entries when the file is replaced.
        self.record(session=SID, kind="pa", terminal="t0", parent=None, at=iso(T0))
        self.pa_turn(T0 + 10, "m1", inp=100, out=1)
        self.pa_turn(T0 + 100, "m2", inp=100, out=1)
        self.collect(T0 + 150)
        self.assertEqual(len(load(self.costs("state.json"))["held"]), 1)
        path = self.transcript()
        data = raw(path)
        with open(path + ".new", "wb") as f:
            f.write(data)
        os.replace(path + ".new", path)  # same bytes, new inode
        self.collect(T0 + 2000)
        self.assertEqual(self.totals()["in"], 200)
        self.collect(T0 + 4000)
        self.assertEqual(self.totals()["in"], 200)
        self.assertEqual(load(self.costs("state.json"))["held"], [])


# ---- fix round 2, P-c: subagent transcripts in separate files --------------------------------------------------

class PC_Subagents(Collecting):
    def sub_lines(self, t, mid, out, sid=SID, **kw):
        return [fx.claude_user(isoms(t), sid, self.project, sidechain=True)] + [
            fx.claude_assistant(isoms(t + 1), sid, self.project, mid, out=out, sidechain=True, **kw)]

    def nested(self, agent, sid=SID):
        return fx.claude_subagent_path(pa.CLAUDE_HOME, self.project, sid, agent)

    def beside(self, agent):
        return fx.claude_agent_path(pa.CLAUDE_HOME, self.project, agent)

    def worker(self):
        self.record(session=SID, kind="worker", terminal="t5", name="t-001-build", task="t-001")
        self.ledger({"version": 1, "tasks": [{"id": "t-001", "state": "in-progress",
                                              "workers": [{"address": "t5", "role": "build", "name": "t-001-build"}]}]})

    def test_pc1_both_layouts_are_read_and_attributed_to_the_parent(self):
        self.worker()
        self.add(self.prompt(T0 + 1), self.call(T0 + 2, "p1", lines=1, out=10))
        fx.append(self.nested("agent-a1"), self.sub_lines(T0 + 10, "s1", 100) + self.sub_lines(T0 + 20, "s2", 200))
        fx.append(self.beside("agent-b2"), self.sub_lines(T0 + 30, "s3", 1000))
        fx.append(self.beside("agent-c3"), self.sub_lines(T0 + 40, "s4", 5000, sid=SID2))  # another session's
        self.collect(T0 + 2000)
        self.assertEqual(self.totals()["out"], 1310)
        rows = self.rows()
        self.assertEqual({(r["session"], r["task"], r["role"], r["activity"]) for r in rows}, {(SID, "t-001", "build", "build")})
        self.assertEqual(sorted(r.get("sub") or "" for r in rows), ["", "agent-a1", "agent-b2"])
        self.collect(T0 + 2100)
        self.assertEqual(self.totals()["out"], 1310)  # counted once
        fx.append(self.nested("agent-a1"), self.sub_lines(T0 + 2200, "s5", 7))
        self.collect(T0 + 5000)
        self.assertEqual(self.totals()["out"], 1317)
        s = load(self.costs("summary.json"))
        self.assertEqual([(x["task"], x["tokens"]) for x in s["sessions"]], [("t-001", self.totals()["in"] + 1317)])

    def test_pc2_a_call_in_both_the_parent_and_a_subagent_file_counts_once(self):
        self.worker()
        self.add(self.prompt(T0 + 1), self.call(T0 + 2, "p1", lines=1, out=10),
                 fx.claude_assistant(isoms(T0 + 11), SID, self.project, "s1", out=100, sidechain=True))
        fx.append(self.nested("agent-a1"), self.sub_lines(T0 + 10, "s1", 100))
        self.collect(T0 + 2000)
        self.assertEqual(self.totals()["out"], 110)

    def test_pc3_replaced_subagent_file_voids_its_own_rows_only(self):
        self.worker()
        self.add(self.prompt(T0 + 1), self.call(T0 + 2, "p1", lines=1, out=10))
        fx.append(self.nested("agent-a1"), self.sub_lines(T0 + 10, "s1", 100))
        self.collect(T0 + 2000)
        before = self.totals()
        path = self.nested("agent-a1")
        data = raw(path)
        with open(path + ".new", "wb") as f:
            f.write(data)
        os.replace(path + ".new", path)
        self.collect(T0 + 2100)
        voids = [r for r in lines(self.costs("turns.jsonl")) if r.get("ev") == "void"]
        self.assertEqual([(v["session"], v.get("sub")) for v in voids], [(SID, "agent-a1")])
        self.assertEqual(self.totals(), before)

    def test_pc4_project_agent_subagent_is_attributed_like_its_turn(self):
        self.record(session=SID, kind="pa", terminal="t0", parent=None)
        self.add(self.prompt(T0 + 1), self.call(T0 + 2, "p1", lines=1, out=10))
        fx.append(self.nested("agent-a1"), self.sub_lines(T0 + 10, "s1", 100))
        self.journal({"at": iso(T0 + 30), "by": "agent", "trigger": "report", "task": "t-001", "pass": 3})
        self.collect(T0 + 2000)
        rows = self.rows()
        self.assertEqual({(r["kind"], r["activity"], r["task"], r["pass"]) for r in rows}, {("pa", "pass:report", "t-001", 3)})
        self.assertEqual(self.totals()["out"], 110)

    def test_pc5_reconstruct_does_not_take_subagent_files_for_sessions(self):
        self.worker()
        self.add(self.prompt(T0 + 1), self.call(T0 + 2, "p1", lines=1, out=10))
        fx.append(self.nested("agent-a1"), self.sub_lines(T0 + 10, "s1", 100))
        fx.append(self.beside("agent-b2"), self.sub_lines(T0 + 30, "s3", 1000))
        self.run_cmd(pa_costs.reconstruct, self.dir, now=T0 + 5000)
        recs = lines(self.costs("sessions.jsonl"))
        self.assertFalse([r for r in recs if r.get("ev") == "bind"])
        self.assertFalse([r for r in recs if r.get("ev") == "unmatched" and r["sessions"]])
        self.assertEqual(self.totals()["out"], 1110)

    def test_pc6_missing_state_json_rebuild_keeps_subagent_rows(self):
        self.worker()
        self.add(self.prompt(T0 + 1), self.call(T0 + 2, "p1", lines=1, out=10))
        fx.append(self.nested("agent-a1"), self.sub_lines(T0 + 10, "s1", 100))
        self.collect(T0 + 2000)
        before = self.totals()
        os.unlink(self.costs("state.json"))
        self.collect(T0 + 2100)
        self.assertEqual(self.totals(), before)


# ---- fix round 2, P-d: long.above in the price table ------------------------------------------------------------

class PD_LongAbove(Collecting):
    def with_long(self, above):
        t = json.loads(json.dumps(PRICES))
        block = {"in": 2.0, "out": 20.0, "cw5m": 4.0, "cw1h": 8.0, "cr": 1.0}
        if above is not ABSENT:
            block["above"] = above
        t["models"]["model-a"][0]["long"] = block
        self.prices(t)
        return pa_costs.load_prices(pa.load_profile("test"))

    def test_pd1_a_bad_long_above_refuses_the_table(self):
        for bad in (0, -5, 1.5, "200000", True, None, [200000]):
            table, problem = self.with_long(bad)
            self.assertIsNone(table, bad)
            self.assertIn("long.above must be a positive whole number of prompt tokens", problem or "", bad)
            self.assertIn("model-a", problem, bad)

    def test_pd2_absent_or_a_positive_integer_is_accepted(self):
        for good in (ABSENT, 200000):
            table, problem = self.with_long(good)
            self.assertIsNone(problem, good)
            self.assertEqual(table.get("warnings"), [], good)

    def test_pd3_another_threshold_is_named_not_silently_ignored(self):
        table, problem = self.with_long(100000)
        self.assertIsNone(problem)
        self.assertEqual(len(table["warnings"]), 1)
        self.assertIn("model-a", table["warnings"][0])
        self.assertIn("200000", table["warnings"][0])
        self.record(session=SID, task="t-001")
        self.add(self.prompt(T0 + 1), self.call(T0 + 2, "m1", lines=1, out=10))
        self.collect(T0 + 2000)
        self.assertIn(table["warnings"][0], load(self.costs("summary.json"))["problems"])
        _r, out, _e = self.run_cmd(pa_costs.cmd_prices, self.dir)
        self.assertIn(table["warnings"][0], out)


class FX_FixRound3(Collecting):
    """Fix round 3: REVIEW-2 MF1-MF3 (MF3 is in FX_BillingRound3) and VERIFY-2 M-1."""

    def turn(self, t, mid, out=10, sid=SID):
        self.add(self.prompt(t, sid=sid), self.call(t + 2, mid, lines=1, out=out, sid=sid), sid=sid)

    def reused_t5(self, new_in_ledger):
        # t5 did t-001 (closed, journaled before); a new worker t-002-design starts on the same address at T0.
        workers_t2 = [{"address": "t5", "role": "design", "name": "t-002-design"}] if new_in_ledger else []
        self.ledger({"version": 1, "tasks": [
            {"id": "t-001", "state": "done", "workers": [{"address": "t5", "role": "build", "name": "t-001-build"}]},
            {"id": "t-002", "state": "in-progress", "workers": workers_t2}]})
        self.journal({"at": iso(T0 - 600), "by": "agent", "trigger": "report", "task": "t-001", "pass": 3,
                      "why": "closed worker t5 (t-001-build)"})
        self.record(session=SID, kind="worker", terminal="t5", name="t-002-design", task="t-002", at=iso(T0))
        self.turn(T0 + 2, "m1", out=1800)
        self.journal({"at": iso(T0 + 30), "by": "agent", "trigger": "report", "task": "t-002", "pass": 4,
                      "why": "Started t5 (t-002-design)"})

    def test_fx_mf1_a_new_worker_on_a_reused_address_keeps_its_own_task(self):
        self.reused_t5(new_in_ledger=True)
        self.collect(T0 + 4000)
        r = self.rows()[0]
        self.assertEqual((r["task"], r["role"], r["activity"]), ("t-002", "design", "design"))
        self.assertNotEqual(r.get("match"), "journal-handoff")

    def test_fx_mf1_the_old_workers_role_is_not_pinned_before_the_ledger_lists_the_new_one(self):
        self.reused_t5(new_in_ledger=False)
        self.collect(T0 + 4000)
        r = self.rows()[0]
        self.assertEqual((r["task"], r["role"], r["activity"]), ("t-002", "design", "design"))
        pins = [x for x in lines(self.costs("sessions.jsonl")) if x.get("ev") == "role"]
        self.assertEqual(pins, [])

    def test_fx_mf2_problems_from_state_reach_the_summary_without_a_new_row(self):
        self.record(session=SID, kind="worker", terminal="t5", name="t-001-build", task="t-001", at=iso(T0))
        self.turn(T0 + 2, "m1")
        self.record(program="codex", bind="launch-window", at=iso(T0), terminal="t7", task="t-002")
        self.record(session=SID2, kind="worker", terminal="t8", name="t-003-build", task="t-003", at=iso(T0))  # never written
        self.collect(T0 + 2000)
        self.assertEqual(load(self.costs("summary.json"))["problems"], [])
        self.collect(T0 + 8000)
        self.assertIn("codex session for t7 not found", load(self.costs("summary.json"))["problems"])
        self.collect(T0 + 8 * 86400)
        self.assertIn("1 sessions' files were gone before they were read", load(self.costs("summary.json"))["problems"])
        before = self.costs_bytes()
        self.collect(T0 + 8 * 86400 + 100)
        self.assertEqual(self.costs_bytes(), before)  # a rerun with nothing new changes no byte

    def test_fx_m1_an_unknown_subagent_layout_counts_nothing_and_says_so(self):
        self.record(session=SID, kind="worker", terminal="t5", name="t-001-build", task="t-001")
        self.turn(T0 + 1, "p1", out=10)
        base = os.path.dirname(self.transcript())
        sub = [fx.claude_assistant(isoms(T0 + 5), SID, self.project, "u1", out=600, sidechain=True)]
        fx.append(os.path.join(base, SID, "subagents", "deeper", "agent-x.jsonl"), sub)
        fx.append(os.path.join(base, SID, "agents", "agent-y.jsonl"), sub)
        noid = json.loads(sub[0])
        noid.pop("sessionId")
        fx.append(os.path.join(base, "agent-noid.jsonl"), [json.dumps(noid) + "\n"])
        fx.append(fx.claude_subagent_path(pa.CLAUDE_HOME, self.project, SID, "agent-ok"), sub)  # the known layout
        self.collect(T0 + 2000)
        self.assertEqual(self.totals()["out"], 610)  # the parent and the known file only
        problems = load(self.costs("summary.json"))["problems"]
        said = [p for p in problems if "unknown layout" in p]
        self.assertEqual(len(said), 2, problems)
        mine = [p for p in said if SID[:8] in p]
        self.assertEqual(len(mine), 1, problems)
        self.assertIn("agents/agent-y.jsonl", mine[0])
        self.assertIn("subagents/deeper/agent-x.jsonl", mine[0])
        self.assertNotIn("agent-ok", mine[0])
        self.assertTrue(any("agent-noid.jsonl" in p for p in said), problems)
        before = self.costs_bytes()
        self.collect(T0 + 2100)
        self.assertEqual(self.costs_bytes(), before)


class FX_BillingRound3(BillingBase):
    def test_fx_mf3_a_twelve_digit_number_in_a_dropped_field_rejects_the_batch(self):
        self.enable()
        for bad in (brow(accountId=TWELVE), brow(extra={"ids": [int(TWELVE)]})):
            self.query_out([brow(day="2026-11-15"), bad])
            result, _o, _e = self.fetch(mode="now", now=T0 + len(self.calls()))
            self.assertEqual(result["result"], "failed")
        self.assertEqual(self.billed(), [])
        self.query_out([brow(amount=0.1234567890123)])  # a long fraction is not an id: the stored amount is rounded
        result, _o, _e = self.fetch(mode="now", now=T0 + len(self.calls()))
        self.assertNotEqual(result["result"], "failed")


class FR4_FixRound4(Collecting):
    """Fix round 4: REVIEW-3 MF2 (no price shows as no price) and VERIFY-3 MF-R3-1 (reconstructed handoff bound)."""

    def turn(self, t, mid, out=10, sid=SID):
        self.add(self.prompt(t, sid=sid), self.call(t + 2, mid, lines=1, out=out, sid=sid), sid=sid)

    def j(self, t, task, why):
        self.journal({"at": iso(t), "by": "agent", "trigger": "report", "task": task, "pass": 1, "why": why})

    def test_fr4_mfr31_a_reconstructed_reused_worker_takes_the_journal_handoff(self):
        # A6, but the session is bound by reconstruct (run long after its turns), not by a launch record.
        self.ledger({"version": 1, "tasks": [{"id": "t-001", "state": "done", "workers": [{"address": "t5", "role": "design", "name": "t-001-design"}]},
                                             {"id": "t-002", "state": "in-progress", "workers": [{"address": "t5", "role": "design", "name": "t-001-design"}]}]})
        os.makedirs(os.path.dirname(self.costs("sessions.jsonl")), exist_ok=True)
        with open(self.costs("sessions.jsonl"), "a") as f:
            f.write(json.dumps({"v": 1, "ev": "bind", "at": iso(T0 + 3000), "session": SID, "program": "claude", "kind": "worker",
                                "task": "t-001", "name": "t-001-design", "role": None, "terminal": "t5", "cwd": self.project,
                                "startedAt": iso(T0 + 8), "source": "reconstructed", "match": "cwd+start", "confidence": "medium"}) + "\n")
        self.j(T0 + 5, "t-001", "Started worker t5 (t-001-design)")
        self.turn(T0 + 8, "m1")
        self.j(T0 + 500, "t-002", "Sent t5 the design of t-002")
        self.turn(T0 + 503, "m2")
        self.collect(T0 + 4000)
        rows = {r["turn"]: r for r in self.rows()}
        self.assertEqual(rows[isoms(T0 + 8)]["task"], "t-001")
        self.assertEqual((rows[isoms(T0 + 503)]["task"], rows[isoms(T0 + 503)]["match"]), ("t-002", "journal-handoff"))

    def pa_pass(self):
        self.record(session=SID, kind="pa", terminal="t0", parent=None)
        self.add(self.prompt(T0 + 10), self.call(T0 + 12, "p1", lines=1, out=1000))
        self.journal({"at": iso(T0 + 20), "trigger": "report", "task": "t-001", "pass": 7})
        self.collect(T0 + 2000)
        return load(self.costs("summary.json"))

    def test_fr4_mf2_with_no_price_table_passes_and_today_say_no_price(self):
        s = self.pa_pass()
        self.assertIsNone(s["totals"]["money"])
        self.assertEqual(s["passes"][0]["pass"], 7)
        self.assertIsNone(s["passes"][0]["money"])
        self.assertIsNone(s["totals"]["reconstructedMoney"])
        _r, out, _e = self.run_cmd(pa_costs.report, self.dir, now=T0 + 2000)
        self.assertIn("Today     — est.", out)
        self.assertNotIn("0.00", out)
        _r, out, _e = self.run_cmd(pa_costs.report, self.dir, by="pass", now=T0 + 2000)
        line = [x for x in out.splitlines() if "pass 7" in x][0]
        self.assertIn("—", line)
        self.assertNotIn("0.00", line)

    def test_fr4_mf2_a_real_zero_still_shows_as_zero(self):
        t = json.loads(json.dumps(PRICES))
        t["models"]["model-a"][0].update({"in": 0.0, "out": 0.0, "cw5m": 0.0, "cw1h": 0.0, "cr": 0.0})
        self.prices(t)
        s = self.pa_pass()
        self.assertEqual(s["passes"][0]["money"], 0.0)
        self.assertEqual(s["totals"]["reconstructedMoney"], 0.0)
        _r, out, _e = self.run_cmd(pa_costs.report, self.dir, now=T0 + 2000)
        self.assertIn("Today     0.00 est.", out)
        _r, out, _e = self.run_cmd(pa_costs.report, self.dir, by="pass", now=T0 + 2000)
        self.assertIn("0.00", [x for x in out.splitlines() if "pass 7" in x][0])
        self.collect(T0 + 86400 + 2000)  # a table, no row today: zero
        _r, out, _e = self.run_cmd(pa_costs.report, self.dir, now=T0 + 86400 + 2000)
        self.assertIn("Today     0.00 est.", out)

    def test_fr4_mf2_with_no_table_and_no_row_today_today_is_no_price(self):
        self.pa_pass()
        self.collect(T0 + 86400 + 2000)
        _r, out, _e = self.run_cmd(pa_costs.report, self.dir, now=T0 + 86400 + 2000)
        self.assertIn("Today     — est.", out)


class FR4_BillingRound4(BillingBase):
    """Fix round 4: REVIEW-3 MF1, the code's own row id never trips the 12-digit check."""

    def generated(self):
        labels = ["usage-{}-{}".format(m, k) for m in "abcdefgh" for k in ("in", "out", "cw5m")]
        days = ["2026-{:02d}-{:02d}".format(1 + d // 28, 1 + d % 28) for d in range(0, 336, 1)][:240]
        for acct in ("acct1", "acct2"):
            for label in labels[:12]:
                for day in days:
                    yield brow(day=day, account=acct, label=label, kind=label.rsplit("-", 1)[-1])

    def test_fr4_mf1_no_generated_row_is_rejected_for_its_own_id(self):
        rows = list(self.generated())
        self.assertGreaterEqual(len(rows), 5000)
        out = [pa_costs.validate_row(dict(r), "query:q1", "acct1", T0) for r in rows]
        self.assertEqual(len(out), len(rows))
        self.assertTrue(any(re.search(r"\d{12}", o["id"]) for o in out))  # the case the review found is in the set
        review = pa_costs.validate_row(brow(day="2026-01-08", label="usage-a-in", kind=None), "query:q1", "acct1", T0)
        self.assertRegex(review["id"], r"\d{12}")  # REVIEW-3's row: its own id holds 12 digits in a row

    def test_fr4_mf1_a_twelve_digit_number_in_any_incoming_field_is_still_rejected(self):
        base = brow(model="model-a", note="n", quantity=5, unit="tokens")
        for key in list(base) + ["id", "fetchedAt", "source", "v", "accountId", "extra"]:
            for value in (TWELVE, "x" + TWELVE + "y", int(TWELVE)):
                bad = dict(base, **{key: value})
                with self.assertRaises(ValueError, msg=(key, value)):
                    pa_costs.validate_row(bad, "query:q1", "acct1", T0)

    def test_fr4_mf1_a_real_sized_batch_is_fetched(self):
        self.enable()
        rows = [brow(day="2026-11-{:02d}".format(1 + d % 15), label="usage-{}-in".format(m), kind="in") for d in range(15) for m in "abcdefghijklmnopqrst"]
        rows.append(brow(day="2026-01-08", label="usage-a-in", kind=None))
        self.query_out(rows)
        result, _o, _e = self.fetch(mode="now", now=T0)
        self.assertNotEqual(result["result"], "failed", result)
        self.assertEqual(len(self.billed()), len(rows))


class FR5_BillingRound5(BillingBase):
    """Fix round 5: VERIFY-4 F4-1 / REVIEW-4 MF4-1, the billed comparison never shows "no price" as 0.00 (AC4.4, R14)."""

    def summary(self, rows):
        s = BC_Comparison.summary(self, rows)
        s["prices"]["profile"] = "test"
        return s

    def compare(self, billed, space_rows, all_rows=None):
        return BC_Comparison.compare(self, billed, space_rows, all_rows)

    def text(self, billed, space_rows, all_rows=None):
        rows = [pa_costs.validate_row(dict(r), "query:q1", "acct1", T0) for r in billed]
        here = self.summary(all_rows or space_rows)
        saved = (pa_costs.effective_billed, pa_costs.account_wide_summary)
        pa_costs.effective_billed, pa_costs.account_wide_summary = (lambda: rows), (lambda _a, _n: here)
        try:
            return pa_costs.billed_lines(self.summary(space_rows), T0)
        finally:
            pa_costs.effective_billed, pa_costs.account_wide_summary = saved

    def line(self, out, start):
        return [x for x in out if x.strip().startswith(start)][0]

    def empty_table(self):
        t = json.loads(json.dumps(PRICES))
        t["models"]["model-a"][0].update({k: None for k in ("in", "out", "cw5m", "cw1h", "cr")})
        self.prices(t)

    def test_fr5_no_price_table_no_estimate_reads_zero(self):
        os.unlink(os.path.join(self.profile_dir, "prices.json"))
        billed = [brow(amount=1.5), brow(amount=1.5, model=None, label="other-label")]
        c = pa_costs.billing_comparison([pa_costs.validate_row(dict(r), "query:q1", "acct1", T0) for r in billed],
                                        self.summary([("2026-11-16", "model-a", None)]), self.summary([("2026-11-16", "model-a", None)]), None)
        self.assertIsNone(c["total"]["thisSpace"])
        self.assertIsNone(c["total"]["allSpaces"])
        self.assertIsNone(c["calibrated"])
        self.assertEqual(c["noPrice"], ["model-a"])
        for m in c["models"]:
            self.assertIsNone(m["thisSpace"])
            self.assertIsNone(m["allSpaces"])
        out = self.text(billed, [("2026-11-16", "model-a", None)])
        self.assertNotIn("0.00", "\n".join(out))
        self.assertIn("—", self.line(out, "total"))
        self.assertIn("—", self.line(out, "calibrated"))
        self.assertIn("no price for model-a", "\n".join(out))

    def test_fr5_every_price_empty_no_estimate_reads_zero(self):
        self.empty_table()
        billed = [brow(amount=1.5), brow(amount=1.5, model=None, label="other-label")]
        use = [("2026-11-16", "model-a", None)]
        c = self.compare(billed, use)
        m = c["models"][0]
        self.assertEqual(m["model"], "model-a")
        self.assertIsNone(m["thisSpace"])
        self.assertIsNone(m["allSpaces"])
        self.assertIsNone(c["total"]["thisSpace"])
        self.assertIsNone(c["total"]["allSpaces"])
        self.assertIsNone(c["calibrated"])
        self.assertEqual(c["noPrice"], ["model-a"])
        out = self.text(billed, use)
        self.assertNotIn("0.00", "\n".join(out))
        self.assertIn("—", self.line(out, "model-a"))
        self.assertIn("—", self.line(out, "total"))
        self.assertIn("—", self.line(out, "calibrated"))
        self.assertIn("no price for model-a", "\n".join(out))

    def test_fr5_some_models_priced_the_estimate_says_partial(self):
        billed = [brow(amount=1.5), brow(amount=1.5, model="model-b", label="usage-b-out"),
                  brow(day="2026-11-17", amount=1.5, model="model-b", label="usage-b-out"), brow(amount=1.5, model=None, label="other-label")]
        use = [("2026-11-16", "model-a", 2.0), ("2026-11-16", "model-b", 1.0), ("2026-11-17", "model-b", None), ("2026-11-16", "model-z", None)]
        c = self.compare(billed, use)
        ms = {m["model"]: m for m in c["models"]}
        self.assertAlmostEqual(ms["model-a"]["thisSpace"], 2.0)
        self.assertFalse(ms["model-a"]["thisSpacePartial"])
        self.assertAlmostEqual(ms["model-b"]["thisSpace"], 1.0)
        self.assertTrue(ms["model-b"]["thisSpacePartial"])
        self.assertTrue(ms["model-b"]["allSpacesPartial"])
        self.assertAlmostEqual(c["total"]["thisSpace"], 3.0)
        self.assertTrue(c["total"]["thisSpacePartial"])
        self.assertTrue(c["total"]["allSpacesPartial"])
        self.assertAlmostEqual(c["calibrated"], 3.0)
        self.assertTrue(c["calibratedPartial"])
        self.assertEqual(c["noPrice"], ["model-b", "model-z"])
        out = self.text(billed, use)
        self.assertIn("+", self.line(out, "model-b"))
        self.assertNotIn("+", self.line(out, "model-a"))
        self.assertIn("3.00+", self.line(out, "total"))
        self.assertIn("3.00 + unpriced", self.line(out, "calibrated"))
        self.assertIn("no price for model-b, model-z", "\n".join(out))

    def test_fr5_fully_priced_real_zero_still_reads_zero(self):
        t = json.loads(json.dumps(PRICES))
        t["models"]["model-a"][0].update({k: 0.0 for k in ("in", "out", "cw5m", "cw1h", "cr")})
        self.prices(t)
        billed = [brow(amount=1.5), brow(amount=1.5, model="model-b", label="usage-b-out")]  # model-b: no use in this space
        use = [("2026-11-16", "model-a", 0.0)]
        c = self.compare(billed, use)
        ms = {m["model"]: m for m in c["models"]}
        for key in ("model-a", "model-b"):
            self.assertEqual((ms[key]["thisSpace"], ms[key]["allSpaces"]), (0.0, 0.0))
            self.assertFalse(ms[key]["thisSpacePartial"])
        self.assertEqual((c["total"]["thisSpace"], c["total"]["allSpaces"]), (0.0, 0.0))
        self.assertFalse(c["total"]["thisSpacePartial"])
        self.assertEqual(c["calibrated"], 0.0)
        self.assertFalse(c["calibratedPartial"])
        self.assertEqual(c["noPrice"], [])
        out = self.text(billed, use)
        self.assertIn("0.00", self.line(out, "model-a"))
        self.assertIn("0.00", self.line(out, "total"))
        self.assertTrue(self.line(out, "calibrated").endswith("this space 0.00"))
        self.assertNotIn("no price", "\n".join(out))


# ---- t-021: follow-ups to the cost collector ---------------------------------------------------------------

class F1_ContinueBind(Collecting):
    """Item 1 (REVIEW L4): a --continue / --fork-session file that starts with copied old history is still bound."""

    def binds(self):
        return [r for r in lines(self.costs("sessions.jsonl")) if r.get("ev") == "bind"]

    def copied(self, sid, at, old_out=500, new_out=11):
        self.add(self.prompt(T0 + 10, sid=sid), self.call(T0 + 11, "old-" + sid[:4], sid=sid, lines=1, out=old_out),
                 self.prompt(at + 3, sid=sid), self.call(at + 4, "new-" + sid[:4], sid=sid, lines=1, out=new_out), sid=sid)
        os.utime(self.transcript(sid), (at + 4, at + 4))

    def old_session(self):
        # the session that was continued: unrecorded here, last written before the launch
        self.add(self.prompt(T0 + 10), self.call(T0 + 11, "old-" + SID[:4], lines=1, out=500))
        os.utime(self.transcript(SID), (T0 + 20, T0 + 20))

    def test_f1_continue_with_copied_history_is_bound_and_counts_new_calls_only(self):
        at = T0 + 1000
        self.old_session()
        self.record(program="claude", bind="launch-window", resumed=True, at=iso(at), terminal="t6", task="t-002")
        self.copied(SID3, at)
        self.collect(T0 + 3000)
        self.assertEqual([(x["session"], x["confidence"], x["match"]) for x in self.binds()],
                         [(SID3, "medium", "launch-window-copied")])
        self.assertEqual(self.totals()["out"], 11)
        self.assertEqual({r["task"] for r in self.rows()}, {"t-002"})

    def test_f1_fork_session_with_copied_history_is_bound(self):
        at = T0 + 1000
        self.record(program="claude", bind="launch-window", resumed=True, fork=True, at=iso(at), terminal="t6", task="t-002")
        self.copied(SID3, at)
        self.collect(T0 + 3000)
        self.assertEqual([x["session"] for x in self.binds()], [SID3])
        self.assertEqual(self.totals()["out"], 11)

    def test_f1_a_plain_launch_window_record_keeps_the_first_time_rule(self):
        at = T0 + 1000
        self.record(program="claude", bind="launch-window", resumed=False, fork=False, at=iso(at), terminal="t6", task="t-002")
        self.copied(SID3, at)
        self.collect(T0 + 3000)
        self.assertEqual(self.binds(), [])

    def test_f1_a_file_not_written_since_before_the_launch_is_not_bound(self):
        at = T0 + 1000
        self.old_session()
        self.record(program="claude", bind="launch-window", resumed=True, at=iso(at), terminal="t6", task="t-002")
        self.collect(T0 + 3000)
        self.assertEqual(self.binds(), [])
        self.collect(at + 3700)
        self.assertIn("claude session for t6 not found", load(self.costs("state.json"))["problems"])

    def test_f1_a_file_starting_in_the_window_is_still_picked_first(self):
        at = T0 + 1000
        self.record(program="claude", bind="launch-window", resumed=True, at=iso(at), terminal="t6", task="t-002")
        self.copied(SID3, at)
        self.add(self.prompt(at + 2, sid=SID4), self.call(at + 3, "m4", sid=SID4, lines=1, out=7), sid=SID4)
        self.collect(T0 + 3000)
        self.assertEqual([(x["session"], x["match"]) for x in self.binds()], [(SID4, "launch-window")])

    def test_f1_reconstruct_leaves_the_copied_file_to_its_launch_record(self):
        at = T0 + 1000
        self.record(program="claude", bind="launch-window", resumed=True, at=iso(at), terminal="t6", task="t-002")
        self.copied(SID3, at)
        result = pa_costs.reconstruct(self.dir, dry_run=True, now=at + 60, quiet=True)
        self.assertNotIn(SID3, [m["session"] for m in result["matches"]])
        self.assertNotIn(SID3[:8], result["unmatched"]["ids"])


class F2_SharedFolder(Collecting):
    """Item 2 (VERIFY R-d): another space on the same project folder; its recorded sessions are not this space's."""
    def setUp(self):
        super().setUp()
        self.wt = os.path.join(TMP, "wt-t-002")
        self.ledger({"version": 1, "tasks": [{"id": "t-002", "state": "done", "worktree": self.wt,
                                              "workers": [{"address": "t5", "role": "build", "name": "t-002-build"}]}]})

    session, reconstruct, binds = R_Reconstruct.session, R_Reconstruct.reconstruct, R_Reconstruct.binds

    def other_space(self, worker_sid, pa_sid):
        other = os.path.join(pa.ROOT, "spaces", "sp2")
        rec = {"v": 1, "ev": "start", "at": iso(T0 + 129), "session": worker_sid, "program": "claude", "kind": "worker",
               "space": "sp2", "terminal": "t5", "cwd": self.project, "bind": None, "source": "recorded"}
        write(os.path.join(other, "costs", "sessions.jsonl"), json.dumps(rec) + "\n")
        write(os.path.join(other, "session.log"), "{} start resume=False session={} reason=new\n".format(iso(T0), pa_sid))
        dump({"space": "sp2", "terminal": "t1", "project": self.project, "created": iso(T0 - 3600)}, os.path.join(other, "agent.json"))

    def test_f2_reconstruct_skips_sessions_another_space_records(self):
        self.other_space(SID2, SID3)
        self.journal({"at": iso(T0 + 100), "trigger": "report", "task": "t-002", "why": "Started build worker t5 (t-002-build)"})
        self.session(SID2, T0 + 130, out=2)  # sp2's recorded worker, in the shared project folder
        self.session(SID3, T0 + 200, out=3)  # sp2's Project Agent (its session.log)
        self.session(SID4, T0 + 3000, out=50)  # nobody's: still sp1's unmatched
        _r, _out = self.reconstruct()
        self.assertEqual(self.binds(), {})
        um = [r for r in lines(self.costs("sessions.jsonl")) if r.get("ev") == "unmatched"][-1]
        self.assertEqual((um["sessions"], um["tokens"], um["ids"]), (1, 52, [SID4[:8]]))

    def test_f2_collect_does_not_bind_another_spaces_recorded_session(self):
        self.other_space(SID2, SID3)
        at = T0 + 1000
        self.record(program="claude", bind="launch-window", resumed=True, at=iso(at), terminal="t6", task="t-002")
        self.session(SID2, at + 3, out=2)  # recorded by sp2, starts in sp1's launch window
        self.collect(at + 3700)
        self.assertEqual(self.binds(), {})
        self.assertIn("claude session for t6 not found", load(self.costs("state.json"))["problems"])


class F3_SubagentTurn(Collecting):
    """Item 3 (REVIEW-4 RL4-1): a Project Agent's subagent rows are attributed by the parent turn open at their first call."""
    sub_lines, nested = PC_Subagents.sub_lines, PC_Subagents.nested

    def j(self, t, trigger, task, pass_):
        self.journal({"at": iso(t), "by": "agent", "trigger": trigger, "task": task, "pass": pass_})

    def sub_rows(self):
        return [r for r in self.rows() if r.get("sub")]

    def test_f3_pass_journaled_long_after_the_subagent_ended(self):
        self.record(session=SID, kind="pa", terminal="t0", parent=None)
        self.add(self.prompt(T0 + 1), self.call(T0 + 2, "p1", lines=1, out=10), self.call(T0 + 500, "p2", lines=1, out=10))
        fx.append(self.nested("agent-a1"), self.sub_lines(T0 + 10, "s1", 100))
        self.collect(T0 + 700)  # the subagent has been idle 10 min; the parent's turn goes on
        self.assertEqual(self.sub_rows(), [])
        self.add(self.call(T0 + 1000, "p3", lines=1, out=10))
        self.j(T0 + 1800, "report", "t-001", 3)  # the pass journals 30 min after the subagent ended
        self.add(self.prompt(T0 + 2400), self.call(T0 + 2402, "p4", lines=1, out=10))
        self.j(T0 + 2410, "message", "t-002", 4)
        self.collect(T0 + 2500)
        self.collect(T0 + 2700)
        self.collect(T0 + 4000)
        subs = self.sub_rows()
        self.assertEqual([(r["activity"], r["task"], r["pass"], r["turn"]) for r in subs], [("pass:report", "t-001", 3, isoms(T0 + 1))])
        self.assertEqual(self.totals()["out"], 140)
        parent = {r["turn"]: r for r in self.rows() if not r.get("sub")}
        self.assertEqual((parent[isoms(T0 + 1)]["activity"], parent[isoms(T0 + 2400)]["activity"]), ("pass:report", "pass:message"))

    def test_f3_a_subagent_in_the_second_turn_takes_that_turn(self):
        self.record(session=SID, kind="pa", terminal="t0", parent=None)
        self.add(self.prompt(T0 + 1), self.call(T0 + 2, "p1", lines=1, out=10))
        self.j(T0 + 5, "report", "t-001", 3)
        self.add(self.prompt(T0 + 100), self.call(T0 + 102, "p2", lines=1, out=10))
        fx.append(self.nested("agent-a1"), self.sub_lines(T0 + 110, "s1", 100))
        self.j(T0 + 1500, "decision", "t-002", 4)
        self.add(self.prompt(T0 + 1600), self.call(T0 + 1602, "p3", lines=1, out=10))
        self.collect(T0 + 5000)
        self.collect(T0 + 5200)
        self.assertEqual([(r["activity"], r["task"], r["pass"]) for r in self.sub_rows()], [("pass:decision", "t-002", 4)])


class F4_Partial(Collecting):
    """Item 4 (REVIEW-3 RL3-1, VERIFY-5 RL5-2): an entry that mixes priced and unpriced use is marked partial."""

    def mixed_space(self):
        self.prices()
        self.ledger({"version": 1, "tasks": [{"id": "t-001", "state": "in-progress",
                                              "workers": [{"address": "t5", "role": "build", "name": "t-001-build"}]}]})
        self.record(session=SID, task="t-001", name="t-001-build", terminal="t5")
        self.add(self.prompt(T0 + 1), self.call(T0 + 2, "m1", lines=1, out=300),
                 self.call(T0 + 3, "m2", lines=1, out=2999998, model="model-c"))
        self.record(session=SID2, kind="pa", terminal="t0", parent=None)
        self.add(self.prompt(T0 + 10, sid=SID2), self.call(T0 + 12, "q1", sid=SID2, lines=1, out=8),
                 self.call(T0 + 13, "q2", sid=SID2, lines=1, out=98, model="model-c"), sid=SID2)
        self.journal({"at": iso(T0 + 14), "by": "agent", "trigger": "report", "task": "t-001", "pass": 3})
        self.collect(T0 + 2000)
        return load(self.costs("summary.json"))

    def test_f4_every_mixed_breakdown_entry_carries_its_unpriced_tokens(self):
        s = self.mixed_space()
        self.assertEqual(s["totals"]["unpricedTokens"], 3000000 + 100)
        build = [e for e in s["byActivity"] if e["key"] == "build"][0]
        self.assertIsNotNone(build["money"])
        self.assertEqual(build["unpricedTokens"], 3000000)
        task = [e for e in s["byTask"] if e["key"] == "t-001"][0]
        self.assertEqual(task["unpricedTokens"], 3000100)
        self.assertEqual([p.get("unpricedTokens") for p in task["phases"]], [3000100])
        self.assertEqual([e.get("unpricedTokens") for e in s["byPhase"]], [3000100])
        for dim in ("kind", "role", "program", "machine"):
            self.assertTrue(all(e.get("unpricedTokens") for e in s["byKind"][dim]), dim)
        models = {e["key"]: e for e in s["byKind"]["model"]}
        self.assertNotIn("unpricedTokens", models["model-a"])  # fully priced
        self.assertNotIn("unpricedTokens", models["model-c"])  # fully unpriced: money is null
        self.assertIsNone(models["model-c"]["money"])
        self.assertEqual(s["byDay"][0]["unpricedTokens"], 3000100)
        self.assertEqual(s["hours"][0][1]["unpricedTokens"], 3000100)
        self.assertEqual(s["passes"][0]["unpricedTokens"], 100)
        self.assertEqual(sorted(e.get("unpricedTokens") for e in s["sessions"]), [100, 3000000])
        self.assertTrue(all("unpricedTokens" not in e for e in s["byDayModel"]))  # one model a day: priced or not

    def test_f4_the_report_marks_a_partial_entry(self):
        self.mixed_space()
        _r, out, _e = self.run_cmd(pa_costs.report, self.dir, now=T0 + 2000)
        build = [x for x in out.splitlines() if x.strip().startswith("build")][0]
        self.assertRegex(build, r"\d\+")
        self.assertIn("+ marks an estimate of the priced part only", out)
        _r, out, _e = self.run_cmd(pa_costs.report, self.dir, by="pass", now=T0 + 2000)
        self.assertRegex([x for x in out.splitlines() if "pass 3" in x][0], r"\d\+")

    def test_f4_a_fully_priced_space_has_no_mark(self):
        self.prices()
        self.record(session=SID, task="t-001", name="t-001-build", terminal="t5")
        self.add(self.prompt(T0 + 1), self.call(T0 + 2, "m1", lines=1, out=300))
        self.collect(T0 + 2000)
        self.assertNotIn("unpricedTokens\": 1", raw(self.costs("summary.json")).decode())
        _r, out, _e = self.run_cmd(pa_costs.report, self.dir, now=T0 + 2000)
        self.assertNotIn("+ marks", out)

    def test_f4_folded_tasks_keep_the_unpriced_part(self):
        entries = [{"key": "a", "tokens": 10, "money": 1.0, "calls": 1, "phases": [], "sessions": []},
                   {"key": "b", "tokens": 10, "money": 1.0, "calls": 1, "unpricedTokens": 4, "phases": [], "sessions": []},
                   {"key": "c", "tokens": 6, "money": None, "calls": 1, "phases": [], "sessions": []}]
        other = pa_costs.fold_tasks(entries, 1)[-1]
        self.assertEqual((other["money"], other["unpricedTokens"]), (1.0, 10))

    def test_f4_all_spaces_here_sum_the_unpriced_part(self):
        def summary(space, money, extra=None):
            e = {"day": "2026-11-16", "model": "model-a", "tokens": toks(out=1000), "money": money}
            e.update(extra or {})
            dump({"account": "acct1", "updated": "2026-11-17T09:00:00Z", "byDayModel": [e]},
                 os.path.join(pa.ROOT, "spaces", space, "costs", "summary.json"))
        summary("sp1", 3.0)
        summary("sp2", None)
        summary("sp3", 1.0, {"unpricedTokens": 5})
        here = pa_costs.account_wide_summary("acct1", T0)
        self.assertEqual([(e["money"], e["unpricedTokens"]) for e in here["byDayModel"]], [(4.0, 1005)])
        summary("sp2", 2.0)
        summary("sp3", 1.0)
        here = pa_costs.account_wide_summary("acct1", T0)
        self.assertNotIn("unpricedTokens", here["byDayModel"][0])
        summary("sp1", None)
        summary("sp2", None)
        summary("sp3", None)
        here = pa_costs.account_wide_summary("acct1", T0)
        self.assertEqual((here["byDayModel"][0]["money"], "unpricedTokens" in here["byDayModel"][0]), (None, False))

    def test_f4_billed_comparison_sees_a_mixed_all_spaces_entry(self):
        # VERIFY-5 RL5-2: one space priced vm-one, another did not; the all-spaces figure is partial.
        self.prices()
        row = pa_costs.validate_row(dict(brow(), model="model-a"), "query:q1", "acct1", T0)
        space = {"account": "acct1", "updated": "2026-11-17T09:00:00Z", "prices": {"currency": "USD"},
                 "byDayModel": [{"day": "2026-11-16", "model": "model-a", "tokens": toks(out=1000), "money": None}]}
        here = {"byDayModel": [{"day": "2026-11-16", "model": "model-a", "tokens": toks(out=2000), "money": 3.0, "unpricedTokens": 1000}]}
        table, _p = pa_costs.load_prices(pa.load_profile("test"))
        c = pa_costs.billing_comparison([row], space, here, table)
        m = c["models"][0]
        self.assertEqual((m["allSpaces"], m["allSpacesPartial"]), (3.0, True))
        self.assertTrue(c["total"]["allSpacesPartial"])


class F5_Comparison(FR5_BillingRound5):
    """Item 5 (VERIFY-5 RL5-1, RL5-3; REVIEW-4 RL4-2): the billed comparison's calibrated line, scope ratio and currency."""
    test_fr5_no_price_table_no_estimate_reads_zero = test_fr5_every_price_empty_no_estimate_reads_zero = None
    test_fr5_some_models_priced_the_estimate_says_partial = test_fr5_fully_priced_real_zero_still_reads_zero = None

    def test_f5_unbilled_unpriced_use_is_not_a_bare_zero(self):
        # case 10: this space used only model-c (no price, not billed); the account was billed only for model-b
        billed = [brow(amount=1.5, model="model-b", label="usage-b-out")]
        use = [("2026-11-16", "model-c", None)]
        c = self.compare(billed, use)
        self.assertEqual((c["calibrated"], c["calibratedPartial"]), (0.0, True))
        out = self.text(billed, use)
        cal = self.line(out, "calibrated")
        self.assertFalse(cal.endswith("this space 0.00"), cal)
        self.assertIn("+ unpriced", cal)
        self.assertIn("no price for model-c", "\n".join(out))

    def test_f5_a_scope_ratio_from_a_partial_estimate_is_marked(self):
        days = ["2026-11-1{}".format(i) for i in range(3, 6)]
        billed = [brow(day=d, amount=9.0) for d in days]
        use = [(d, "model-a", 1.0) for d in days]
        mixed = use + [("2026-11-14", "model-a-x", None)]
        c = self.compare(billed, use, mixed)
        f = c["models"][0]["factor"]
        self.assertEqual((f["kind"], f["applied"], f["partial"]), ("scope", False, True))
        self.assertIn("(from a partial estimate)", self.line(self.text(billed, use, mixed), "model-a"))
        whole = self.compare(billed, use)["models"][0]["factor"]
        self.assertFalse(whole.get("partial"))
        self.assertNotIn("partial", self.line(self.text(billed, use), "model-a"))

    def test_f5_rows_only_in_another_currency_are_named(self):
        billed = [brow(amount=9.0, currency="EUR")]
        out = self.text(billed, [("2026-11-16", "model-a", 1.0)])
        self.assertNotIn("  no billed rows for account acct1", out)
        self.assertIn("  no billed rows in USD for account acct1", out)
        self.assertIn("in EUR (not compared)", "\n".join(out))

    def test_f5_rows_with_no_price_table_are_named(self):
        os.unlink(os.path.join(self.profile_dir, "prices.json"))
        billed = [brow(amount=9.0)]
        rows = [pa_costs.validate_row(dict(r), "query:q1", "acct1", T0) for r in billed]
        space = dict(self.summary([("2026-11-16", "model-a", None)]), prices={"profile": "test", "currency": None})
        saved = (pa_costs.effective_billed, pa_costs.account_wide_summary)
        pa_costs.effective_billed, pa_costs.account_wide_summary = (lambda: rows), (lambda _a, _n: space)
        try:
            out = pa_costs.billed_lines(space, T0)
        finally:
            pa_costs.effective_billed, pa_costs.account_wide_summary = saved
        self.assertNotIn("  no billed rows for account acct1", out)
        self.assertTrue(any("no price table" in x and "acct1" in x for x in out), out)

    def test_f5_no_rows_at_all_reads_as_before(self):
        out = self.text([], [("2026-11-16", "model-a", 1.0)])
        self.assertIn("  no billed rows for account acct1", out)


class F6_CollectFailure(Collecting):
    """Item 6 (REVIEW-2 RL3): when collect refuses or fails, summary.json says why, and the next good collect clears it."""

    def space(self):
        self.prices()
        self.record(session=SID, task="t-001")
        self.add(self.prompt(T0 + 1), self.call(T0 + 2, "m1", out=30, inp=500))
        self.collect(T0 + 2000)
        return load(self.costs("summary.json"))

    def set_version(self, v):
        state = load(self.costs("state.json"))
        state["v"] = v
        dump(state, self.costs("state.json"))

    def main_collect(self):
        return self.run_cmd(pa_costs.main, ["collect", "--space", "sp1", "--quiet"])

    def test_f6_a_refused_collect_is_in_the_summary_and_cleared_after(self):
        before = self.space()
        self.set_version(2)
        turns = raw(self.costs("turns.jsonl"))
        with self.assertRaises(pa.Problem):
            self.main_collect()
        s = load(self.costs("summary.json"))
        self.assertEqual(s["collect"]["ok"], False)
        self.assertIn("state.json has version 2", s["collect"]["why"])
        self.assertTrue(s["problems"][0].startswith("collect failed: "))
        self.assertEqual(s["totals"], before["totals"])  # the last good figures stay
        self.assertEqual(raw(self.costs("turns.jsonl")), turns)
        self.set_version(1)
        self.main_collect()
        s = load(self.costs("summary.json"))
        self.assertNotIn("collect", s)
        self.assertFalse(any(p.startswith("collect failed") for p in s["problems"]))
        self.assertEqual(s["totals"], before["totals"])

    def test_f6_an_unexpected_error_names_its_type_only(self):
        self.space()
        saved = pa_costs._collect

        def boom(*a, **kw):
            raise ValueError("{} some text".format(fx.CANARY))
        pa_costs._collect = boom
        try:
            with self.assertRaises(ValueError):
                self.main_collect()
        finally:
            pa_costs._collect = saved
        s = load(self.costs("summary.json"))
        self.assertEqual(s["collect"]["why"], "ValueError")
        self.assertNotIn(fx.CANARY, raw(self.costs("summary.json")).decode())

    def test_f6_with_no_summary_yet_a_short_one_without_totals(self):
        self.space()
        os.unlink(self.costs("summary.json"))
        self.set_version(2)
        with self.assertRaises(pa.Problem):
            self.main_collect()
        s = load(self.costs("summary.json"))
        self.assertEqual((s["v"], s["space"], s["collect"]["ok"]), (1, "sp1", False))
        self.assertNotIn("totals", s)
        self.assertTrue(s["problems"][0].startswith("collect failed: "))


ABSENT = object()


# ---- t-009 part A, second half, step 9a: what the Costs view reads (T-a billed compare --json, T-b drill fields) ----

class T9_BilledCompare(BillingBase):
    """T-a (plan-panel D9): `pa-costs billed compare --json` prints the comparison, the query status and the row count."""

    def space_summary(self):
        days = ["2026-11-1{}".format(i) for i in range(3, 6)]
        bdm = [{"day": d, "model": "model-a", "tokens": toks(out=1000000), "money": 8.0} for d in days]
        return {"v": 1, "space": "sp1", "updated": "2026-11-17T09:00:00Z", "account": "acct1",
                "prices": {"profile": "test", "source": "test table", "asOf": "2026-11-01", "currency": "USD", "missing": []},
                "byDayModel": bdm}

    def setUp(self):
        super().setUp()
        self.s = self.space_summary()
        dump(self.s, self.costs("summary.json"))
        days = ["2026-11-1{}".format(i) for i in range(3, 6)]
        rows = [pa_costs.validate_row(brow(day=d, amount=12.5, quantity=1000000, unit="tokens"), "query:q1", "acct1", T0) for d in days]
        rows.append(pa_costs.validate_row(brow(day="2026-11-14", amount=7.0, account="acct2"), "query:q1", "acct2", T0))
        pa_costs.append_billed(rows)

    def compare_json(self, *argv):
        result, out, err = self.run_cmd(pa_costs.main, ["billed", "compare", "--json"] + list(argv))
        self.assertEqual(result, 0, err)
        return json.loads(out)

    def expected(self):
        table, _p = pa_costs.load_prices(pa.load_profile("test"))
        return pa_costs.billing_comparison(pa_costs.effective_billed(), self.s, pa_costs.account_wide_summary("acct1", T0), table)

    def test_cmp1_local_space_gives_the_comparison(self):
        j = self.compare_json("--space", "sp1")
        self.assertEqual(j["comparison"], self.expected())
        m = j["comparison"]["models"][0]
        self.assertEqual((m["model"], m["factor"]["kind"]), ("model-a", "price"))
        self.assertAlmostEqual(m["factor"]["value"], 1.25)
        self.assertTrue(j["comparison"]["accountWide"])

    def test_cmp2_summary_and_here_as_arguments_give_the_same(self):
        here = pa_costs.account_wide_summary("acct1", T0)
        j = self.compare_json("--summary-json", json.dumps(self.s), "--here-json", json.dumps(here))
        self.assertEqual(j["comparison"], self.expected())
        big = json.dumps(dict(self.s, pad="x" * (100 * 1024 + 1)))
        with self.assertRaises(pa.Problem) as e:
            pa_costs.main(["billed", "compare", "--json", "--summary-json", big])
        self.assertIn("100 KB", str(e.exception))
        with self.assertRaises(pa.Problem) as e:
            pa_costs.main(["billed", "compare", "--json", "--summary-json", "{not json"])
        self.assertIn("not JSON", str(e.exception))

    def test_cmp3_status_fields_and_rows_of_this_account_only(self):
        pa_costs.set_billing_config({"query": "q1", "credential": "ExampleReadOnly", "readOnlyMatch": "ReadOnly$", "enabled": True})
        j = self.compare_json("--space", "sp1")
        st = j["status"]
        self.assertEqual((st["feePerRequest"], st["feeCurrency"], st["maxPages"], st["runsPerDay"]), (0.01, "USD", 3, 3))
        self.assertTrue(st["config"]["enabled"])
        self.assertIn("Billing query:", st["line"])
        self.assertIn("state", st)
        self.assertEqual(j["rows"], 3)

    def test_cmp4_read_only_and_allowed_inside_an_agent(self):
        os.environ["CLAUDECODE"] = "1"
        try:
            j = self.compare_json("--space", "sp1")
        finally:
            os.environ.pop("CLAUDECODE")
        self.assertEqual(j["rows"], 3)
        tool = "pa-" + "costs"
        self.assertEqual(W_CloseAndGuard.guard(self, tool + " billed compare --json --space sp1")[0], 0)
        self.assertEqual(W_CloseAndGuard.guard(self, tool + " billed fetch --now")[0], 2)
        self.assertFalse(os.path.exists(os.path.join(pa.ROOT, "billing", "query-state.json")))

    def test_cmp5_no_billed_file(self):
        os.unlink(os.path.join(pa.ROOT, "billing", "billed.jsonl"))
        j = self.compare_json("--space", "sp1")
        self.assertEqual(j["rows"], 0)
        self.assertIsNone(j["comparison"]["covered"])
        self.assertIsNone(j["status"]["config"])


class T9_SummaryDrill(Collecting):
    """T-b (plan-panel D10): byActivity entries carry their top tasks; unattributed carries its reasons."""
    mixed = S_Summary.mixed

    def test_sumx1_activity_tasks_and_unattributed_reasons(self):
        s = self.mixed()
        for a in s["byActivity"]:
            self.assertIn("tasks", a, a["key"])
            self.assertLessEqual(len(a["tasks"]), 8)
            for t in a["tasks"]:
                self.assertEqual(sorted(k for k in t if k in ("key", "tokens", "money")), ["key", "money", "tokens"])
                self.assertLessEqual(t["tokens"], a["tokens"])
                if a["money"] is not None and t["money"] is not None:
                    self.assertLessEqual(t["money"], a["money"] + 1e-6)
            if len(a["tasks"]) < 8:
                self.assertEqual(sum(t["tokens"] for t in a["tasks"]), a["tokens"], a["key"])
        acts = {a["key"]: a for a in s["byActivity"]}
        self.assertIn("t-001", [t["key"] for t in acts["cache-rebuild"]["tasks"]])
        un = acts.get("unattributed")
        self.assertIsNotNone(un, [a["key"] for a in s["byActivity"]])
        self.assertTrue(un["reasons"])
        self.assertEqual(sum(r["tokens"] for r in un["reasons"]), un["tokens"])
        for a in s["byActivity"]:
            if a["key"] != "unattributed":
                self.assertNotIn("reasons", a)

    def test_sumx2_still_under_64_kb_and_sums_hold(self):
        S_Summary.test_s2_summary_stays_under_64_kb(self)
        s = load(self.costs("summary.json"))
        self.assertTrue(all("tasks" in a for a in s["byActivity"]))

    def test_sumx2b_s1_still_holds_with_the_drill_fields(self):
        S_Summary.test_s1_every_breakdown_sums_to_the_totals(self)


# ---- t-009 part A, second half, step 11: the doctor line and the docs ----

class T9_DoctorAndDocs(Base):
    def doctor(self):
        import argparse
        result, out, err = self.run_cmd(pa.cmd_doctor, argparse.Namespace(space="sp1"))
        return result, out

    def test_dr1_doctor_notes_the_costs_folder(self):
        code0, out = self.doctor()
        self.assertIn("  costs/: none yet", out)
        os.makedirs(os.path.join(self.dir, "costs"), exist_ok=True)
        write(os.path.join(self.dir, "costs", "turns.jsonl"), "x" * 3000)
        dump({"v": 1, "updated": "2026-11-17T09:00:00Z"}, os.path.join(self.dir, "costs", "summary.json"))
        code1, out = self.doctor()
        self.assertRegex(out, r"  costs/: \d+(\.\d)? KB \(summary updated 2026-11-17T09:00:00Z\)")
        self.assertEqual(code0, code1)
        os.unlink(os.path.join(self.dir, "costs", "summary.json"))
        _c, out = self.doctor()
        self.assertRegex(out, r"  costs/: \d+(\.\d)? KB \(no summary yet\)")
        self.assertIn("pa-costs", pa.__doc__)

    def test_doc1_readme_and_recipe(self):
        def read(name):
            with open(os.path.join(HERE, "..", name)) as f:
                return f.read()
        readme = read("README.md")
        tool = "pa-" + "costs"
        for want in (tool + " collect", tool + " report", tool + " reconstruct", tool + " billed compare", tool + " billed enable",
                     tool + " billed disable", "est.", "account-wide", "once a day", "Turn off", "Costs view"):
            self.assertIn(want, readme)
        recipe = read("RECIPE.md")
        for head in ("## What you see", "## How it behaves", "## Watch out"):
            start = recipe.index(head)
            nxt = recipe.find("\n## ", start + 1)
            body = recipe[start:nxt if nxt > 0 else None]
            self.assertIn("Costs", body, head)
        self.assertIn("120 s", recipe)
        self.assertIn("30 min", recipe)
        self.assertIn("estimate", recipe)


class I1_Install(unittest.TestCase):
    def test_i1_install_links_pa_costs(self):
        with open(os.path.join(HERE, "..", "install.sh")) as f:
            text = f.read()
        self.assertRegex(text, r'for n in pa-costs; do ln -sfn "\$PA/tools/pa_costs.py" "\$HOME/.local/bin/\$n"; done')
        self.assertIn("chmod +x \"$PA\"/tools/*.py", text)  # pa_costs.py is made runnable with the others


# ---- t-029 Job 1: Claude Code's own per-session cost (CC) beside est. (spec R1-R6, R8; tests T1-T8) ----------

CC_SID = "cc0a1b2c-3d4e-4f50-8a6b-7c8d9e0f1a2b"
CC_STUBS = os.path.join(TMP, "cc-stubs")
CC_SCRIPTS = {
    "ok": "#!/bin/sh\ncat > /dev/null\nprintf 'STATUS-LINE ok'\n",
    "copy": "#!/bin/sh\ncat > \"$1\"\nprintf copied\n",
    "slow": "#!/bin/sh\ncat > /dev/null\nsleep 5\nprintf late\n",
    "fail": "#!/bin/sh\ncat > /dev/null\nprintf 'partial line'\necho oops >&2\nexit 1\n",
    "big": "#!/bin/sh\ncat > /dev/null\nhead -c 2097152 /dev/zero | tr '\\000' x\n",
    "noread": "#!/bin/sh\nprintf 'no-read'\n",
    "my-status": "#!/bin/sh\ncat > /dev/null\nprintf 'MY-STATUS %s' \"$*\"\n",
}


def cc_stub(name):
    path = os.path.join(CC_STUBS, name)
    if not os.path.exists(path):
        write(path, CC_SCRIPTS[name])
        os.chmod(path, 0o755)
    return path


def cc_input(sid=CC_SID, cost=1.25, dur=60000, api=30000, model="model-a", version="2.1.999", **over):
    """A status line input with every field EXPERIMENT.md lists (and two later ones); every text field holds the canary."""
    c = fx.CANARY
    obj = {"session_id": sid, "transcript_path": "/home/{0}/.claude/projects/{0}/{1}.jsonl".format(c, sid), "cwd": "/work/" + c,
           "prompt_id": c, "effort": {"level": c}, "session_name": c + " session title",
           "model": {"id": model, "display_name": c},
           "workspace": {"current_dir": "/w/" + c, "project_dir": "/w/" + c, "git_worktree": c, "added_dirs": ["/a/" + c],
                         "repo": {"host": c, "owner": c, "name": c}},
           "version": version, "output_style": {"name": c},
           "cost": {"total_cost_usd": cost, "total_duration_ms": dur, "total_api_duration_ms": api,
                    "total_lines_added": 12, "total_lines_removed": 3},
           "context_window": {"total_input_tokens": 1000, "total_output_tokens": 200, "context_window_size": 200000,
                              "current_usage": {"input_tokens": 10, "output_tokens": 20, "cache_creation_input_tokens": 30,
                                                "cache_read_input_tokens": 40},
                              "used_percentage": 5, "remaining_percentage": 95},
           "exceeds_200k_tokens": False,
           "prompt_cache": {"warm": True, "caching_observed": True, "ttl": c, "expires_at": 1, "requests": 2, "misses": 0,
                            "expected_rebuilds": 0, "hit_ratio": 0.5, "cache_write_tokens": 1, "miss_recache_tokens": 0,
                            "last_miss_at": None, "last_miss_cause": None, "miss_causes": {c: 1}, "recache_tokens_if_cold": 0},
           "fast_mode": False, "thinking": {"enabled": True}, "agent": {"name": c}, "a_later_field": c}
    obj.update(over)
    return obj


def cc_bytes(**kw):
    return json.dumps(cc_input(**kw)).encode()


def all_files(root):
    out = set()
    for folder, _dirs, names in os.walk(root):
        for n in names:
            out.add(os.path.relpath(os.path.join(folder, n), root))
    return out


class CCBase(CostsBase):
    def setUp(self):
        super().setUp()
        os.environ["HC_TERMINAL_ID"] = "t5"
        self.addCleanup(os.environ.pop, "HC_TERMINAL_ID", None)
        self.addCleanup(os.environ.pop, "PA_NO_CC_STATUSLINE", None)

    def cc_path(self, sid=CC_SID):
        return os.path.join(self.dir, "costs", "cc", sid + ".json")

    def cc_record(self, sid=CC_SID):
        data = raw(self.cc_path(sid))
        return json.loads(data.decode()) if data else None

    def run_sl(self, data, then=("ok",), space="sp1", now=T0, extra=()):
        argv = (["--space", space] if space is not None else []) + list(extra)
        if then:
            argv += ["--then", cc_stub(then[0]) if then[0] in CC_SCRIPTS else then[0]] + list(then[1:])
        result, out, err = self.run_cmd(pa_costs.statusline, argv, data, now=now)
        self.assertEqual(out, "")  # it returns the bytes; the script writes them
        self.assertEqual(err, "")
        return result


class CC_StatusLine(CCBase):
    """R1, R2, R8 in process: pa_costs.statusline(argv, stdin bytes, now) returns the bytes to print."""

    def test_cc1_record_has_the_allowlisted_fields_only(self):
        out = self.run_sl(cc_bytes())
        self.assertEqual(out, b"STATUS-LINE ok")
        self.assertEqual(self.cc_record(), {"v": 1, "session": CC_SID, "cost": 1.25, "base": 0, "segments": 1, "durationMs": 60000,
                                            "apiDurationMs": 30000, "model": "model-a", "version": "2.1.999", "terminal": "t5",
                                            "first": iso(T0), "at": iso(T0), "ctx": 1000})
        names = sorted(n for n in os.listdir(os.path.join(self.dir, "costs", "cc")) if not n.startswith("."))
        self.assertEqual(names, [CC_SID + ".json"])  # the temp file was replaced into place

    def test_cc2_privacy_no_text_field_reaches_a_file_or_the_output(self):
        before = all_files(pa.ROOT)
        out = self.run_sl(cc_bytes())
        self.assertEqual(out, b"STATUS-LINE ok")
        new = all_files(pa.ROOT) - before
        self.assertTrue(new)
        self.assertFalse([n for n in new if not n.startswith(os.path.join("spaces", "sp1", "costs", "cc") + os.sep)], new)
        for n in all_files(pa.ROOT):
            self.assertNotIn(fx.CANARY.encode(), raw(os.path.join(pa.ROOT, n)) or b"", n)
        # allowlisted fields with canary-shaped invalid values are dropped, not kept
        self.run_sl(cc_bytes(model="{} ../x".format(fx.CANARY), version="1 " + fx.CANARY), now=T0 + 400)
        rec = self.cc_record()
        self.assertIsNone(rec["model"])
        self.assertIsNone(rec["version"])
        self.assertNotIn(fx.CANARY.encode(), raw(self.cc_path()))
        # a session id that is not an id: nothing is written anywhere
        before = all_files(pa.ROOT)
        out = self.run_sl(cc_bytes(sid="../../" + fx.CANARY))
        self.assertEqual(out, b"STATUS-LINE ok")
        self.assertFalse([n for n in all_files(pa.ROOT) - before if not n.endswith(".lock")])
        for bad in (b"[1, 2]", ("\"" + fx.CANARY + "\"").encode(), b"\xff\xfe" + fx.CANARY.encode(), b"{\"session_id\": " + fx.CANARY.encode()):
            self.assertEqual(self.run_sl(bad), b"STATUS-LINE ok")
        for n in all_files(pa.ROOT):
            self.assertNotIn(fx.CANARY.encode(), raw(os.path.join(pa.ROOT, n)) or b"", n)

    def test_cc3_each_field_is_validated(self):
        for i, bad in enumerate((-1, "1.0", True, 1e6, float("nan"), float("inf"), None, [1])):
            sid = "dddd000{}-0000-4000-8000-abcdef000000".format(i)
            self.run_sl(cc_bytes(sid=sid, cost=bad))
            self.assertIsNone(self.cc_record(sid)["cost"], bad)
        self.run_sl(cc_bytes(sid="eeee0000-0000-4000-8000-abcd00000001", cost=2.5, dur=-5, api="x"))
        rec = self.cc_record("eeee0000-0000-4000-8000-abcd00000001")
        self.assertEqual((rec["cost"], rec["durationMs"], rec["apiDurationMs"]), (2.5, None, None))
        os.environ["HC_TERMINAL_ID"] = "t5; rm -rf /"
        self.run_sl(cc_bytes(sid="eeee0000-0000-4000-8000-abcd00000002", model={"id": "x" * 81}))
        rec = self.cc_record("eeee0000-0000-4000-8000-abcd00000002")
        self.assertEqual((rec["terminal"], rec["model"]), (None, None))
        for sid in ("short", "x" * 65, "a/b-0000-0000", "abc_def_ghi"):
            self.run_sl(cc_bytes(sid=sid))
        self.assertEqual(len([n for n in os.listdir(os.path.join(self.dir, "costs", "cc")) if n.endswith(".json")]), 10)
        self.run_sl(cc_bytes(sid="eeee0000-0000-4000-8000-abcd00000001", cost="bad", dur=10, api=10), now=T0 + 400)
        self.assertEqual(self.cc_record("eeee0000-0000-4000-8000-abcd00000001")["cost"], 2.5)  # a figure never disappears

    def test_cc4_writes_only_on_change_or_every_300_s(self):
        self.run_sl(cc_bytes(), now=T0)
        first = raw(self.cc_path())
        self.run_sl(cc_bytes(dur=61000), now=T0 + 100)
        self.assertEqual(raw(self.cc_path()), first)  # same cost, model, segment: no write
        self.run_sl(cc_bytes(dur=62000), now=T0 + 300)
        rec = self.cc_record()
        self.assertEqual((rec["at"], rec["first"], rec["durationMs"]), (iso(T0 + 300), iso(T0), 62000))
        self.run_sl(cc_bytes(cost=1.5, dur=63000), now=T0 + 310)
        self.assertEqual((self.cc_record()["cost"], self.cc_record()["at"]), (1.5, iso(T0 + 310)))
        self.run_sl(cc_bytes(cost=1.5, dur=63000, model="model-b"), now=T0 + 320)
        self.assertEqual((self.cc_record()["model"], self.cc_record()["at"]), ("model-b", iso(T0 + 320)))
        self.run_sl(cc_bytes(cost=1.5, dur=64000, model="model-b"), now=T0 + 330)
        self.assertEqual(self.cc_record()["at"], iso(T0 + 320))

    def test_cc5_segments_on_a_drop_with_a_lower_duration(self):
        self.run_sl(cc_bytes(cost=5.0, dur=100000), now=T0)
        self.run_sl(cc_bytes(cost=5.0, dur=90000), now=T0 + 10)  # a restore: equal is a continuation
        self.assertEqual((self.cc_record()["base"], self.cc_record()["segments"]), (0, 1))
        self.run_sl(cc_bytes(cost=4.995, dur=80000), now=T0 + 20)  # a drop of 0.01 or less is not a new segment
        self.assertEqual((self.cc_record()["base"], self.cc_record()["segments"]), (0, 1))
        self.run_sl(cc_bytes(cost=5.0, dur=100000), now=T0 + 25)
        self.run_sl(cc_bytes(cost=0.4, dur=200000), now=T0 + 30)  # lower cost, higher duration: a continuation
        self.assertEqual((self.cc_record()["base"], self.cc_record()["segments"]), (0, 1))
        self.run_sl(cc_bytes(cost=5.0, dur=200000), now=T0 + 35)
        self.run_sl(cc_bytes(cost=0.4, dur=1000), now=T0 + 40)  # a resume after a crash
        rec = self.cc_record()
        self.assertEqual((rec["base"], rec["cost"], rec["segments"]), (5.0, 0.4, 2))
        self.assertAlmostEqual(rec["base"] + rec["cost"], 5.4)
        self.run_sl(cc_bytes(cost=0.9, dur=2000), now=T0 + 50)
        rec = self.cc_record()
        self.assertEqual((rec["base"], rec["cost"], rec["segments"]), (5.0, 0.9, 2))

    def test_cc6_busy_lock_records_nothing_and_still_prints(self):
        import fcntl
        os.makedirs(os.path.join(self.dir, "costs", "cc"), exist_ok=True)
        with open(os.path.join(self.dir, "costs", "cc", ".lock"), "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            began = time.time()
            out = self.run_sl(cc_bytes())
            took = time.time() - began
        self.assertEqual(out, b"STATUS-LINE ok")
        self.assertIsNone(self.cc_record())
        self.assertLess(took, 1.0)

    def test_cc7_no_then_missing_then_bad_space_and_bad_input(self):
        self.assertEqual(self.run_sl(cc_bytes(), then=()), b"")
        self.assertEqual(self.cc_record()["cost"], 1.25)  # recorded anyway
        sid2 = "cc000000-0000-4000-8000-abcd00000002"
        self.assertEqual(self.run_sl(cc_bytes(sid=sid2), then=("/nonexistent/status-cmd",)), b"")
        self.assertIsNotNone(self.cc_record(sid2))
        plain = os.path.join(CC_STUBS, "not-executable")
        write(plain, "#!/bin/sh\necho hi\n")
        sid3 = "cc000000-0000-4000-8000-abcd00000003"
        self.assertEqual(self.run_sl(cc_bytes(sid=sid3), then=(plain,)), b"")
        self.assertIsNotNone(self.cc_record(sid3))
        before = all_files(pa.ROOT)
        for space in ("../sp1", "nosuch", "", None):
            self.assertEqual(self.run_sl(cc_bytes(sid="cc000000-0000-4000-8000-abcd00000004"), space=space), b"STATUS-LINE ok")
        self.assertEqual(all_files(pa.ROOT), before)
        self.assertFalse(os.path.exists(os.path.join(pa.ROOT, "spaces", "nosuch")))
        for data in (b"", b"not json{", b"null", b"{}", b"{\"session_id\": 5}"):
            self.assertEqual(self.run_sl(data), b"STATUS-LINE ok")
        self.assertEqual(all_files(pa.ROOT), before)
        self.assertEqual(self.run_sl(cc_bytes(), extra=["--refresh", "30", "--unknown", "x"]), b"STATUS-LINE ok")

    def test_cc8_inner_gets_the_stdin_bytes_and_more_than_1_mb_is_cut(self):
        copy = os.path.join(TMP, "cc-copy.bin")
        data = cc_bytes()
        self.assertEqual(self.run_sl(data, then=("copy", copy)), b"copied")
        self.assertEqual(raw(copy), data)
        sid = "cc000000-0000-4000-8000-abcd000000bb"
        big = cc_bytes(sid=sid, pad="x" * (1 << 20))
        self.assertEqual(self.run_sl(big, then=("copy", copy)), b"copied")
        self.assertEqual(raw(copy), big[:1 << 20])
        self.assertIsNone(self.cc_record(sid))  # cut: nothing recorded

    def test_cc9_inner_failing_slow_and_large(self):
        self.assertEqual(self.run_sl(cc_bytes(), then=("fail",)), b"partial line")
        self.assertEqual(len(self.run_sl(cc_bytes(), then=("big",))), 2097152)
        self.assertEqual(self.run_sl(cc_bytes(pad="y" * 900000), then=("noread",)), b"no-read")
        sid = "cc000000-0000-4000-8000-abcd000000aa"
        began = time.time()
        self.assertEqual(self.run_sl(cc_bytes(sid=sid), then=("slow",)), b"")
        self.assertLess(time.time() - began, 2.5)
        self.assertIsNotNone(self.cc_record(sid))  # recorded while the inner command ran
        self.assertEqual((pa_costs.INNER_TIMEOUT, pa_costs.RECORD_BUDGET), (2.0, 0.3))


class CC_Script(CCBase):
    """R2 as a real process: `python3 pa_costs.py statusline ...`, as Claude Code runs it."""

    def script(self, tail, data):
        env = dict(os.environ)
        env.update(_HOMES)
        env["HC_TERMINAL_ID"] = "t5"
        began = time.time()
        r = subprocess.run([sys.executable, os.path.join(TOOLS, "pa_costs.py"), "statusline"] + list(tail), input=data,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=30)
        took = time.time() - began
        OUTPUT_SEEN.append(r.stdout.decode(errors="replace")[:4096] + r.stderr.decode(errors="replace"))
        self.assertEqual((r.returncode, r.stderr), (0, b""), tail)
        return r.stdout, took

    def test_cs1_prints_the_inner_bytes_and_records(self):
        out, _t = self.script(["--space", "sp1", "--then", cc_stub("ok")], cc_bytes())
        self.assertEqual(out, b"STATUS-LINE ok")
        self.assertEqual(self.cc_record()["cost"], 1.25)
        self.assertEqual(self.cc_record()["terminal"], "t5")

    def test_cs2_slow_inner_is_killed_at_2_s_and_nothing_printed(self):
        sid = "cc000000-0000-4000-8000-abcd000000cc"
        out, took = self.script(["--space", "sp1", "--then", cc_stub("slow")], cc_bytes(sid=sid))
        self.assertEqual(out, b"")
        self.assertLess(took, 2.5)
        self.assertGreaterEqual(took, 1.9)
        self.assertIsNotNone(self.cc_record(sid))

    def test_cs3_missing_failing_large_and_no_reader(self):
        self.assertEqual(self.script(["--space", "sp1", "--then", "/nonexistent/x"], cc_bytes())[0], b"")
        self.assertEqual(self.script(["--space", "sp1", "--then", cc_stub("fail")], cc_bytes())[0], b"partial line")
        self.assertEqual(len(self.script(["--space", "sp1", "--then", cc_stub("big")], cc_bytes())[0]), 2097152)
        self.assertEqual(self.script(["--space", "sp1", "--then", cc_stub("noread")], cc_bytes(pad="z" * 900000))[0], b"no-read")
        self.assertEqual(self.script([], b"garbage")[0], b"")
        self.assertEqual(self.script(["--space"], b"")[0], b"")
        self.assertEqual(self.script(["--space", "sp1", "--then"], cc_bytes())[0], b"")

    def test_cs4_overhead_is_at_most_150_ms(self):
        # t-063: a wall-clock bound alone failed under machine load (0.18 s at load 13 on 14 cores). Load barely moves
        # the child's CPU time, so that is the main bound; wall time is bounded as overhead over a bare python3 that
        # runs the same inner command in the same rounds.
        import resource
        noop = shutil.which("true")
        env = dict(os.environ)
        env.update(_HOMES)
        env["HC_TERMINAL_ID"] = "t5"
        statusline = [sys.executable, os.path.join(TOOLS, "pa_costs.py"), "statusline", "--space", "sp1", "--then", noop]
        baseline = [sys.executable, "-c", "import subprocess, sys; sys.stdin.buffer.read(); subprocess.run([sys.argv[1]])",
                    noop]

        def timed(cmd):
            before = resource.getrusage(resource.RUSAGE_CHILDREN)
            began = time.monotonic()
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
            self.addCleanup(lambda: p.poll() is None and p.kill())
            _out, err = p.communicate(cc_bytes(), timeout=30)
            took = time.monotonic() - began
            after = resource.getrusage(resource.RUSAGE_CHILDREN)  # reaped by communicate: now counted
            self.assertEqual((p.returncode, err), (0, b""), cmd)
            return took, (after.ru_utime + after.ru_stime) - (before.ru_utime + before.ru_stime)

        # A best time only falls toward the unloaded cost, never below it, so more rounds cannot hide a real slowdown
        # (a sleep or a lock wait is in every round); a busy machine only needs more rounds to see one quiet moment.
        rounds = []
        for _batch in range(3):
            for _ in range(7):
                base_took, _c = timed(baseline)
                took, cpu_took = timed(statusline)
                rounds.append((base_took, took, cpu_took))
            cpu = min(c for _b, _t, c in rounds)
            overhead = min(t for _b, t, _c in rounds) - min(b for b, _t, _c in rounds)
            if cpu <= 0.15 and overhead <= 0.15:
                break
        self.assertEqual(self.cc_record()["cost"], 1.25)  # the timed runs did the real work
        self.assertLessEqual(cpu, 0.15, "CPU seconds of the statusline process and its inner command")
        self.assertLessEqual(overhead, 0.15, "wall seconds over a bare python3 running the same inner command")


HANG_SCRIPT = "#!/bin/sh\ncat > /dev/null\nsleep 30 &\necho $$ $! > \"$1.part\"\nmv \"$1.part\" \"$1\"\nwait\n"


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class CC_Cancel(CCBase):
    """REVIEW M1: Claude Code cancels a run still in flight; the inner command and its children go with it."""

    def cancel(self, sig):
        stub = os.path.join(CC_STUBS, "hang")
        if not os.path.exists(stub):
            write(stub, HANG_SCRIPT)
            os.chmod(stub, 0o755)
        pids_file = os.path.join(TMP, "hang-pids-{}".format(int(sig)))
        if os.path.exists(pids_file):
            os.unlink(pids_file)
        env = dict(os.environ)
        env.update(_HOMES)
        env["HC_TERMINAL_ID"] = "t5"
        p = subprocess.Popen([sys.executable, os.path.join(TOOLS, "pa_costs.py"), "statusline", "--space", "sp1", "--then",
                              stub, pids_file], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        p.stdin.write(cc_bytes())
        p.stdin.close()
        deadline = time.monotonic() + 1.8  # well inside the 2 s self-timeout, so only the signal can stop it
        pids = []
        while time.monotonic() < deadline:
            if os.path.exists(pids_file):
                pids = [int(x) for x in open(pids_file).read().split()]
                break
            time.sleep(0.01)
        self.assertEqual(len(pids), 2, "the stub started its child")
        for pid in pids:
            self.addCleanup(lambda pid=pid: alive(pid) and os.kill(pid, 9))
        p.send_signal(sig)
        try:
            code = p.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            code = None
        out, err = p.stdout.read(), p.stderr.read()
        p.stdout.close()
        p.stderr.close()
        OUTPUT_SEEN.append(out.decode(errors="replace") + err.decode(errors="replace"))
        gone_by = time.monotonic() + 1.0
        while time.monotonic() < gone_by and any(alive(pid) for pid in pids):
            time.sleep(0.02)
        left = [pid for pid in pids if alive(pid)]
        self.assertEqual((code, err, out), (0, b"", b""), sig)
        self.assertEqual(left, [], "stub and its child are gone within a second")
        folder = os.path.join(self.dir, "costs", "cc")
        names = sorted(os.listdir(folder)) if os.path.isdir(folder) else []
        self.assertEqual([n for n in names if n not in (".lock", CC_SID + ".json")], [], names)
        if CC_SID + ".json" in names:
            self.assertEqual(self.cc_record()["cost"], 1.25)  # whole, never partial

    def test_cx1_sigterm(self):
        import signal
        self.cancel(signal.SIGTERM)

    def test_cx2_sighup(self):
        import signal
        self.cancel(signal.SIGHUP)

    def test_cx3_sigint(self):
        import signal
        self.cancel(signal.SIGINT)


class CC_Launch(Base):
    """R3, R4: pa-claude and pa-session add one --settings with the recording status line, last, or nothing."""

    def setUp(self):
        super().setUp()
        self.bin = os.path.join(TMP, "cc-bin")
        os.makedirs(self.bin, exist_ok=True)
        self.inner = os.path.join(self.bin, "my-status")
        write(self.inner, CC_SCRIPTS["my-status"])
        os.chmod(self.inner, 0o755)
        self.set_profile({"command": ["my-status", "--flag", "a b"], "refreshInterval": 15})
        self.ledger({"version": 1, "tasks": [{"id": "t-010", "state": "in-progress", "workers": []}]})
        self.hc.terminals["t5"] = {"address": "t5", "name": "t-010-build", "parent": "t0", "state": "live", "labels": {"herdr.space": "sp1"}}
        self.hc.terminals["t9"] = {"address": "t9", "name": "plain", "state": "live", "labels": {"herdr.space": "other"}}
        os.environ["HC_TERMINAL_ID"] = "t5"
        os.environ.pop("PA_PRINT", None)
        self.addCleanup(os.environ.pop, "HC_TERMINAL_ID", None)
        self.addCleanup(os.environ.pop, "PA_NO_CC_STATUSLINE", None)
        self.addCleanup(os.environ.pop, "PA_PRINT", None)

    def set_profile(self, status=ABSENT):
        p = {"name": "test", "claude": ["true"], "codex": ["true"], "models": {"worker": "model-a"}, "path": [self.bin]}
        if status is not ABSENT:
            p["statusLine"] = status
        dump(p, os.path.join(self.profile_dir, "profile.json"))

    def launch(self, kind, *argv):
        seen = []

        def fake_execv(path, args):
            seen.append(list(args))
            raise StopLaunch()
        saved = os.execv
        os.execv = fake_execv
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    pa.cmd_launch(kind, list(argv))
                except StopLaunch:
                    pass
        finally:
            os.execv = saved
        if os.environ.get("PA_PRINT"):
            return json.loads(out.getvalue().strip().splitlines()[-1])
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(len(seen), 1)
        return seen[0]

    def settings_count(self, cmd):
        return sum(1 for a in cmd if a == "--settings" or a.startswith("--settings="))

    def ours(self, cmd, extra=()):
        self.assertEqual(cmd[-2], "--settings")
        js = json.loads(cmd[-1])
        self.assertEqual(sorted(js), sorted(["statusLine"] + list(extra)))  # t-040: a Project Agent's adds promptCacheTtl
        sl = js["statusLine"]
        self.assertEqual(sorted(sl), ["command", "refreshInterval", "type"])
        self.assertEqual(sl["type"], "command")
        return sl

    def mask(self, cmd):
        return [re.sub(r"^[0-9a-f]{8}-[0-9a-f-]{27}$", "<sid>", a) for a in cmd]

    def test_cl1_worker_gets_the_recording_status_line_last(self):
        cmd = self.launch("claude", "do the job")
        sl = self.ours(cmd)
        self.assertEqual(sl["refreshInterval"], 15)
        self.assertEqual(shlex.split(sl["command"]), [sys.executable, pa.COSTS_FILE, "statusline", "--space", "sp1", "--then",
                                                    self.inner, "--flag", "a b"])
        self.assertTrue(os.path.isabs(pa.COSTS_FILE) and os.path.isfile(pa.COSTS_FILE))
        self.assertEqual(self.settings_count(cmd), 1)
        self.assertIn("--session-id", cmd[:-2])
        self.set_profile()
        plain = self.launch("claude", "do the job")
        self.assertEqual(self.mask(cmd[:-2]), self.mask(plain))  # everything else as without the key

    def test_cl2_default_refresh_is_30(self):
        self.set_profile({"command": ["my-status"]})
        self.assertEqual(self.ours(self.launch("claude", "job"))["refreshInterval"], 30)

    def test_cl3_no_key_malformed_key_or_inner_not_found_add_nothing(self):
        cases = [ABSENT, None, "my-status", [], {"command": []}, {"command": "my-status"}, {"command": ["my-status", 3]},
                 {"command": [""]}, {"command": ["my-status"], "refreshInterval": 0}, {"command": ["my-status"], "refreshInterval": 301},
                 {"command": ["my-status"], "refreshInterval": True}, {"command": ["my-status"], "refreshInterval": "30"},
                 {"command": ["no-such-status-command"]}]
        for status in cases:
            self.set_profile(status)
            cmd = self.launch("claude", "job")
            self.assertEqual(self.settings_count(cmd), 0, status)
            self.assertEqual(cmd[0], shutil.which("true"), status)

    def test_cl4_caller_settings_off_switch_passthrough_plain_tab_and_codex(self):
        for args in (["--settings", "/x/s.json", "job"], ["--settings=/x/s.json", "job"]):
            cmd = self.launch("claude", *args)
            self.assertEqual(self.settings_count(cmd), 1, args)
            self.assertNotIn("statusline", " ".join(cmd))
        os.environ["PA_NO_CC_STATUSLINE"] = "1"
        self.assertEqual(self.settings_count(self.launch("claude", "job")), 0)
        os.environ["PA_NO_CC_STATUSLINE"] = "0"
        self.assertEqual(self.settings_count(self.launch("claude", "job")), 1)
        os.environ.pop("PA_NO_CC_STATUSLINE")
        self.assertEqual(self.settings_count(self.launch("claude", "mcp", "list")), 0)
        self.assertEqual(self.settings_count(self.launch("codex", "job")), 0)
        os.environ["HC_TERMINAL_ID"] = "t9"
        self.assertEqual(self.settings_count(self.launch("claude", "hello")), 0)
        os.environ.pop("HC_TERMINAL_ID")
        self.assertEqual(self.settings_count(self.launch("claude", "hello")), 0)

    def test_cl5_pa_print_shows_it(self):
        os.environ["PA_PRINT"] = "1"
        shown = self.launch("claude", "job")
        self.assertEqual(shown[-2], "--settings")
        self.assertTrue(shown[-1].startswith('{"statusLine"'))

    def test_cl6_the_command_runs_end_to_end(self):
        sl = self.ours(self.launch("claude", "job"))
        env = dict(os.environ)
        env.update(_HOMES)
        env["HC_TERMINAL_ID"] = "t5"
        r = subprocess.run(["sh", "-c", sl["command"]], input=cc_bytes(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=30)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"MY-STATUS --flag a b", b""))
        rec = load(os.path.join(self.dir, "costs", "cc", CC_SID + ".json"))
        self.assertEqual((rec["session"], rec["cost"], rec["terminal"]), (CC_SID, 1.25, "t5"))

    def test_cl7_a_failure_building_it_never_stops_a_launch(self):
        saved = pa.cc_statusline

        def broken(p):
            raise RuntimeError("broken on purpose")
        pa.cc_statusline = broken
        try:
            cmd = self.launch("claude", "job")
        finally:
            pa.cc_statusline = saved
        self.assertEqual(self.settings_count(cmd), 0)
        self.assertEqual(cmd[0], shutil.which("true"))

    def test_cl8_run_agent_adds_it_on_start_and_resume(self):
        import argparse as ap
        import types
        directory = self.dir
        p = pa.load_profile("test")
        calls = []

        def fake_call(argv, cwd=None):
            calls.append(argv)
            raise StopLaunch()
        saved = pa.subprocess
        pa.subprocess = types.SimpleNamespace(call=fake_call, run=saved.run, PIPE=saved.PIPE, DEVNULL=saved.DEVNULL)
        undo_child = child_fakes.patch(pa, fake_call)  # t-037 J2: the launcher starts Claude with pa.spawn
        try:
            for fresh in (True, False, True):
                if len(calls) == 2:
                    os.environ["PA_NO_CC_STATUSLINE"] = "1"
                agent = load(os.path.join(directory, "agent.json"))
                if not fresh:
                    agent["sessionStarted"] = True
                    dump(agent, os.path.join(directory, "agent.json"))
                with self.assertRaises(StopLaunch):
                    pa.run_agent(ap.Namespace(fresh=fresh), "t0", self.project, p, directory, agent, {"beat": True, "previous": None}, time.time())
        finally:
            pa.subprocess = saved
            undo_child()
        self.assertIn("--session-id", calls[0])
        self.assertIn("--resume", calls[1])
        for argv in calls[:2]:
            sl = self.ours(argv, extra=("promptCacheTtl", "hooks"))  # t-041 J3: and the compaction hooks
            self.assertEqual(shlex.split(sl["command"])[2:6], ["statusline", "--space", "sp1", "--then"])
            self.assertEqual(self.settings_count(argv), 1)
        # t-040: PA_NO_CC_STATUSLINE drops the status line only; the Project Agent keeps its cache setting
        self.assertEqual(self.settings_count(calls[2]), 1)
        js = json.loads(calls[2][-1])
        self.assertIn("hooks", js)  # t-041 J3: the compaction hooks, in the same --settings
        js.pop("hooks")
        self.assertEqual(js, {"promptCacheTtl": "1h"})

    def test_cl10_a_worker_gets_no_cache_setting(self):
        # t-040 W2: the profile key is for the Project Agent's own session; a worker's command is as without it
        base = self.launch("claude", "job")
        for value in ("1h", "5m", "default"):
            p = load(os.path.join(self.profile_dir, "profile.json"))
            p["orchestratorPromptCacheTtl"] = value
            dump(p, os.path.join(self.profile_dir, "profile.json"))
            cmd = self.launch("claude", "job")
            self.ours(cmd)
            self.assertNotIn("promptCacheTtl", " ".join(cmd))
            self.assertEqual(self.mask(cmd), self.mask(base), value)

    def test_cl9_exec_plain_adds_nothing(self):
        seen = []

        def fake_execv(path, args):
            seen.append(list(args))
            raise StopLaunch()
        saved = os.execv
        os.execv = fake_execv
        try:
            with self.assertRaises(StopLaunch):
                pa.exec_plain(pa.load_profile("test"))
        finally:
            os.execv = saved
        self.assertEqual(self.settings_count(seen[0]), 0)


class CC_Doctor(Base):
    def doctor(self):
        import argparse
        return self.run_cmd(pa.cmd_doctor, argparse.Namespace(space="sp1"))

    def test_cd1_a_malformed_key_is_a_note(self):
        code0, out0, _e = self.doctor()
        self.assertNotIn("statusLine", out0)
        dump({"name": "test", "claude": ["true"], "codex": ["true"], "statusLine": {"command": "not a list"}},
             os.path.join(self.profile_dir, "profile.json"))
        code1, out1, _e = self.doctor()
        self.assertEqual(code0, code1)
        note = [l for l in out1.splitlines() if "statusLine" in l]
        self.assertEqual(len(note), 1, out1)
        self.assertNotIn("PROBLEM", note[0])
        dump({"name": "test", "claude": ["true"], "codex": ["true"], "statusLine": {"command": ["x"], "refreshInterval": 30}},
             os.path.join(self.profile_dir, "profile.json"))
        _c, out2, _e = self.doctor()
        self.assertNotIn("malformed", out2)


def cc_sid(n):
    return "{:08x}-0000-4000-8000-abcd{:08x}".format(n, n)


class CCFixture(CostsBase):
    """Ledger rows, start records and costs/cc/ records written directly (no session files)."""

    SA, SB, SC, SD, SE, SF, SG = (cc_sid(0xa1), cc_sid(0xb2), cc_sid(0xc3), cc_sid(0xd4), cc_sid(0xe5), cc_sid(0xf6), cc_sid(0x97))

    def row(self, seq, sid, task, day=T0, split=None, program="claude", model="model-a", out=1000):
        return {"v": 1, "seq": seq, "session": sid, "program": program, "kind": "worker", "model": model, "from": iso(day),
                "to": iso(day + 60), "day": iso(day)[:10], "calls": 1, "tokens": toks(out=out, cr=1000), "long": dict(ZERO),
                "rebuild": {"calls": 0, "cw5m": 0, "cw1h": 0, "gapS": 0}, "turn": iso(day), "task": task, "phase": "build",
                "activity": "build", "role": "build", "pass": None, "trigger": None, "split": split, "reason": None,
                "source": "recorded", "match": "launch-record", "confidence": "high"}

    def start(self, sid, terminal, at=T0, program="claude", task=None):
        return {"v": 1, "ev": "start", "at": iso(at), "session": sid, "program": program, "kind": "worker", "task": task,
                "terminal": terminal, "host": "h1"}

    def cc(self, sid, cost, base=0, segments=1, terminal="t5", first=T0, at=None, **over):
        rec = {"v": 1, "session": sid, "cost": cost, "base": base, "segments": segments, "durationMs": 1000, "apiDurationMs": 500,
               "model": "model-a", "version": "2.1.999", "terminal": terminal, "first": iso(first), "at": iso(at or first + 60)}
        rec.update(over)
        dump(rec, os.path.join(self.dir, "costs", "cc", sid + ".json"))
        return rec

    def fixture(self, with_cc=True, prices=True):
        if prices:
            self.prices()
        split = [{"task": "t-001", "w": 0.5}, {"task": "t-002", "w": 0.5}]
        rows = [self.row(1, self.SA, "t-001"), self.row(2, self.SA, "t-001", day=T0 + 100),
                self.row(3, self.SB, "t-001", split=split), self.row(4, self.SC, "t-002", day=T0 - 3 * 86400),
                self.row(5, self.SF, "t-003", program="codex")]
        recs = [self.start(self.SA, "t5"), self.start(self.SB, "t6"), self.start(self.SC, "t7", at=T0 - 3 * 86400),
                self.start(self.SF, "t8", program="codex"), self.start(self.SG, "t9")]
        os.makedirs(os.path.join(self.dir, "costs"), exist_ok=True)
        write(self.costs("sessions.jsonl"), "".join(json.dumps(r) + "\n" for r in recs))
        write(self.costs("turns.jsonl"), "".join(json.dumps(r) + "\n" for r in rows))
        if with_cc:
            self.cc(self.SA, 1.5)
            self.cc(self.SB, 2.0, terminal="t6")
            self.cc(self.SC, 0.5, base=3.0, segments=2, terminal="t7", first=T0 - 3 * 86400)
            self.cc(self.SD, 0.25, terminal="t5", first=T0 + 600, at=T0 + 900)  # a /clear in t5: SA's continuation
            self.cc(self.SE, 0.75, terminal="t77")  # no session, no terminal: unmatched
            self.cc(self.SG, 9.0, terminal="t9")  # a recorded session with no rows yet: not shown, not unmatched
        pa_costs.write_summary(self.dir, now=T0 + 5000)
        return load(self.costs("summary.json"))



class CC_Summary(CCFixture):
    """R5: collect and the summary carry CC per session, per single-task session's task, and in totals.cc."""

    def test_cs1_per_session_task_and_totals(self):
        s = self.fixture()
        by = {e["key"]: e for e in s["sessions"]}
        a, b, c, f = by[self.SA[:8]], by[self.SB[:8]], by[self.SC[:8]], by[self.SF[:8]]
        self.assertAlmostEqual(a["cc"], 1.75)
        self.assertEqual(a["ccAt"], iso(T0 + 900))
        self.assertEqual(a.get("ccCleared"), 1)
        self.assertNotIn("ccSegments", a)
        self.assertAlmostEqual(b["cc"], 2.0)
        self.assertAlmostEqual(c["cc"], 3.5)
        self.assertEqual(c["ccSegments"], 2)
        self.assertNotIn("cc", f)
        tc = s["totals"]["cc"]
        self.assertAlmostEqual(tc.pop("money"), 7.25)
        self.assertAlmostEqual(tc["unmatched"].pop("money"), 0.75)
        self.assertEqual(tc, {"sessions": 3, "claudeSessions": 3, "allSessions": 4, "unmatched": {"sessions": 1}, "ccCurrency": "USD"})
        tasks = {e["key"]: e for e in s["byTask"]}
        self.assertAlmostEqual(tasks["t-001"]["cc"], 1.75)  # SA alone; SB is split
        self.assertEqual((tasks["t-001"]["ccSessions"], tasks["t-001"]["ccOf"]), (1, 2))
        self.assertAlmostEqual(tasks["t-002"]["cc"], 3.5)
        self.assertEqual((tasks["t-002"]["ccSessions"], tasks["t-002"]["ccOf"]), (1, 2))
        self.assertNotIn("cc", tasks["t-003"])
        for key in ("byActivity", "byPhase", "byKind", "byDay", "byDayModel", "passes", "hours", "prices", "unmatched"):
            self.assertNotIn('"cc', json.dumps(s[key]), key)

    def test_cs2_est_is_unchanged_and_no_cc_means_no_cc_field(self):
        with_cc = self.fixture()
        shutil.rmtree(os.path.join(self.dir, "costs", "cc"))
        without = self.fixture(with_cc=False)
        self.assertEqual(with_cc["totals"]["money"], without["totals"]["money"])
        self.assertEqual([(e["key"], e["money"]) for e in with_cc["byTask"]], [(e["key"], e["money"]) for e in without["byTask"]])
        self.assertEqual([e["key"] for e in with_cc["sessions"]], [e["key"] for e in without["sessions"]])
        self.assertNotIn('"cc', raw(self.costs("summary.json")).decode())
        os.makedirs(os.path.join(self.dir, "costs", "cc"))
        write(os.path.join(self.dir, "costs", "cc", ".lock"), "")
        pa_costs.write_summary(self.dir, now=T0 + 5000)
        self.assertNotIn('"cc', raw(self.costs("summary.json")).decode())

    def test_cs3_no_cc_figure_money_is_null(self):
        self.fixture(with_cc=False)
        self.cc(self.SE, 0.75, terminal="t77")
        pa_costs.write_summary(self.dir, now=T0 + 5000)
        tc = load(self.costs("summary.json"))["totals"]["cc"]
        self.assertEqual((tc["money"], tc["sessions"], tc["claudeSessions"], tc["unmatched"]["sessions"]), (None, 0, 3, 1))

    def test_cs4_bad_files_are_skipped_and_counted(self):
        self.fixture()
        folder = os.path.join(self.dir, "costs", "cc")
        write(os.path.join(folder, "junk.json"), "not json")
        dump({"v": 1, "session": self.SB, "cost": 1.0}, os.path.join(folder, self.SA[:-1] + "0.json"))  # name and id differ
        dump({"v": 2, "session": cc_sid(0x55), "cost": 1.0}, os.path.join(folder, cc_sid(0x55) + ".json"))
        dump([1, 2], os.path.join(folder, cc_sid(0x56) + ".json"))
        write(os.path.join(folder, "." + cc_sid(0x57) + ".json.tmp"), "half")  # a temp file is not a record
        pa_costs.write_summary(self.dir, now=T0 + 5000)
        s = load(self.costs("summary.json"))
        self.assertAlmostEqual(s["totals"]["cc"]["money"], 7.25)
        self.assertTrue([p for p in s["problems"] if "costs/cc" in p and "4" in p], s["problems"])

    def test_cs5_bad_values_in_a_stored_record_are_dropped(self):
        self.fixture()
        self.cc(self.SB, "2.0", terminal="t6")  # not a number: no figure
        self.cc(self.SC, 0.5, base=-3, segments=2, terminal="t7", first=T0 - 3 * 86400)  # a bad base: no figure
        pa_costs.write_summary(self.dir, now=T0 + 5000)
        s = load(self.costs("summary.json"))
        by = {e["key"]: e for e in s["sessions"]}
        self.assertNotIn("cc", by[self.SB[:8]])
        self.assertNotIn("cc", by[self.SC[:8]])
        self.assertAlmostEqual(s["totals"]["cc"]["money"], 1.75)

    def test_cs6_at_most_cc_max_files_newest_first(self):
        self.fixture()
        folder = os.path.join(self.dir, "costs", "cc")
        for i, sid in enumerate(sorted(os.listdir(folder))):
            os.utime(os.path.join(folder, sid), (T0 + i, T0 + i))
        os.utime(os.path.join(folder, self.SA + ".json"), (T0 + 100, T0 + 100))
        os.utime(os.path.join(folder, self.SB + ".json"), (T0 + 99, T0 + 99))
        saved = pa_costs.CC_MAX_FILES
        pa_costs.CC_MAX_FILES = 2
        try:
            pa_costs.write_summary(self.dir, now=T0 + 5000)
        finally:
            pa_costs.CC_MAX_FILES = saved
        s = load(self.costs("summary.json"))
        self.assertAlmostEqual(s["totals"]["cc"]["money"], 3.5)  # SA 1.5 and SB 2.0 only
        self.assertTrue([p for p in s["problems"] if "costs/cc" in p and "4" in p], s["problems"])
        self.assertEqual(saved, 5000)

    def test_cs7_summary_stays_under_64_kb_with_totals_cc_intact(self):
        rows, recs = [], []
        os.makedirs(os.path.join(self.dir, "costs", "cc"), exist_ok=True)
        for i in range(1000):
            sid = cc_sid(i + 1)
            day = T0 - (i % 120) * 86400
            recs.append(self.start(sid, "t{}".format(i), at=day, task="t-{:03d}".format(i % 300)))
            rows.append(self.row(i + 1, sid, "t-{:03d}".format(i % 300), day=day, out=1000 + i))
            self.cc(sid, 0.5, terminal="t{}".format(i), first=day)
        write(self.costs("sessions.jsonl"), "".join(json.dumps(r) + "\n" for r in recs))
        write(self.costs("turns.jsonl"), "".join(json.dumps(r) + "\n" for r in rows))
        self.prices()
        pa_costs.write_summary(self.dir, now=T0)
        s = load(self.costs("summary.json"))
        self.assertLessEqual(len(raw(self.costs("summary.json"))), 65536)
        self.assertTrue(s.get("truncated"))
        self.assertAlmostEqual(s["totals"]["cc"]["money"], 500.0)
        self.assertEqual((s["totals"]["cc"]["sessions"], s["totals"]["cc"]["allSessions"]), (1000, 1000))

    def test_cs8_folded_tasks_keep_their_cc(self):
        entries = [{"key": "t-{}".format(i), "tokens": 10, "money": 1.0, "calls": 1, "phases": [], "sessions": [],
                    "cc": 0.5, "ccSessions": 1, "ccOf": 1} for i in range(5)]
        folded = pa_costs.fold_tasks(entries, 2)
        other = folded[-1]
        self.assertEqual(other["key"], "(other tasks)")
        self.assertAlmostEqual(other["cc"], 1.5)
        self.assertEqual((other["ccSessions"], other["ccOf"]), (3, 3))


class CC_Collect(Collecting):
    def test_cc_collect_rewrites_the_summary_when_only_cc_changed(self):
        self.record(session=SID, task="t-001", terminal="t5")
        self.add(self.prompt(T0 + 1), self.call(T0 + 2, "m1", out=30))
        self.collect(T0 + 2000)
        self.assertNotIn("cc", load(self.costs("summary.json"))["totals"])
        self.collect(T0 + 2005)
        dump({"v": 1, "session": SID, "cost": 0.42, "base": 0, "segments": 1, "durationMs": 1, "apiDurationMs": 1, "model": "model-a",
              "version": "2.1.999", "terminal": "t5", "first": iso(T0), "at": iso(T0 + 60)}, os.path.join(self.dir, "costs", "cc", SID + ".json"))
        self.collect(T0 + 2010)
        s = load(self.costs("summary.json"))
        self.assertAlmostEqual(s["totals"]["cc"]["money"], 0.42)
        self.assertAlmostEqual(s["sessions"][0]["cc"], 0.42)
        before = raw(self.costs("summary.json"))
        self.collect(T0 + 2020)
        self.assertEqual(raw(self.costs("summary.json")), before)  # nothing new: not rewritten


class CC_Resume(CCBase):
    """t-043: a resumed session's Claude Code figure is counted once. On a resume Claude Code's first update comes before
    it restores the saved totals (cost and duration near 0), then it reports the restored total; a real restart (totals
    from zero) still counts as a new segment."""

    def readings(self, steps):
        for now, cost, dur in steps:
            self.run_sl(cc_bytes(cost=cost, dur=dur), now=now)
        return self.cc_record()

    def test_r1_a_resume_like_the_project_agents_is_counted_once(self):
        rec = self.readings([(T0, 524.0, 100000000), (T0 + 300, 530.0, 100300000),
                             (T0 + 600, 0.0, 2000),  # the resumed process's first update
                             (T0 + 610, 530.5, 100305000),  # its restored totals
                             (T0 + 900, 538.0, 100600000), (T0 + 1200, 0.0, 1500), (T0 + 1210, 589.0, 101000000)])
        self.assertEqual((rec["base"], rec["cost"], rec["segments"]), (0, 589.0, 1))
        self.assertAlmostEqual(pa_costs.cc_figure(pa_costs.cc_clean(rec)), 589.0)
        self.assertNotIn("heldCost", rec)
        self.assertNotIn("heldMs", rec)

    def test_r2_a_real_restart_keeps_its_segment(self):
        rec = self.readings([(T0, 5.0, 200000), (T0 + 40, 0.4, 1000)])  # a drop: held until it is seen a restore or not
        self.assertEqual((rec["base"], rec["cost"], rec["segments"], rec["heldCost"], rec["heldMs"]), (5.0, 0.4, 2, 5.0, 200000))
        self.assertAlmostEqual(pa_costs.cc_figure(pa_costs.cc_clean(rec)), 5.4)
        rec = self.readings([(T0 + 400, 2.0, 361000), (T0 + 800, 6.0, 761000)])  # grows at the clock's speed past both
        self.assertEqual((rec["base"], rec["cost"], rec["segments"]), (5.0, 6.0, 2))
        self.assertAlmostEqual(pa_costs.cc_figure(pa_costs.cc_clean(rec)), 11.0)

    def test_r3_duration_back_but_not_faster_than_the_clock_is_no_restore(self):
        rec = self.readings([(T0, 5.0, 100000), (T0 + 10, 0.4, 1000),
                             (T0 + 200, 5.5, 150000)])  # 149 s more in 190 s: back above both, but at the clock's speed
        self.assertEqual((rec["base"], rec["segments"]), (5.0, 2))
        rec = self.readings([(T0 + 210, 0.2, 500), (T0 + 220, 5.5, 155000)])  # a new drop replaces the hold; then restored
        self.assertEqual((rec["base"], rec["cost"], rec["segments"]), (5.0, 5.5, 2))  # only the second drop is undone
        self.assertNotIn("heldCost", rec)

    def test_r4_a_restore_with_a_lower_cost_than_held_is_no_restore(self):
        rec = self.readings([(T0, 5.0, 100000), (T0 + 10, 0.4, 1000), (T0 + 20, 1.0, 120000)])
        self.assertEqual((rec["base"], rec["segments"], rec["heldCost"]), (5.0, 2, 5.0))

    def test_r5_a_written_record_with_restored_totals_reads_once(self):
        sid = "e6885e5e-8522-4200-8df9-5bb232342b0a"
        stored = {"v": 1, "session": sid, "cost": 589.2501290000005, "base": 1070.2945344000002, "segments": 3,
                  "durationMs": 133402117, "apiDurationMs": 27521511, "model": None, "version": None, "terminal": None,
                  "first": "2026-11-18T02:25:51Z", "at": "2026-11-18T07:36:51Z"}  # the space's PA record (t-043 data copy)
        rec = pa_costs.cc_clean(stored, sid)
        self.assertEqual((rec["base"], rec["segments"], rec["corrected"]), (0, 1, True))
        self.assertAlmostEqual(pa_costs.cc_figure(rec), 589.250129)
        span_ok = dict(stored, durationMs=18660000 + 300000)  # within the span plus slack: a real restart, kept
        rec = pa_costs.cc_clean(span_ok, sid)
        self.assertEqual((rec["base"], rec["segments"]), (1070.2945344000002, 3))
        self.assertNotIn("corrected", rec)
        held = dict(stored, heldCost=500.0, heldMs=133000000)  # a hold in progress is the recorder's: not corrected
        rec = pa_costs.cc_clean(held, sid)
        self.assertEqual((rec["base"], rec["segments"], rec["heldCost"], rec["heldMs"]), (1070.2945344000002, 3, 500.0, 133000000))
        bad = pa_costs.cc_clean(dict(stored, heldCost=500.0, heldMs="x"), sid)  # half a hold: neither is kept
        self.assertNotIn("heldCost", bad)
        self.assertNotIn("heldMs", bad)

    def test_r6_the_next_reading_rewrites_a_corrected_record(self):
        dump({"v": 1, "session": CC_SID, "cost": 10.0, "base": 20.0, "segments": 3, "durationMs": 90000000, "apiDurationMs": 1,
              "model": "model-a", "version": "2.1.999", "terminal": "t5", "first": iso(T0), "at": iso(T0 + 3600)}, self.cc_path())
        rec = self.readings([(T0 + 3660, 10.5, 90060000)])
        self.assertEqual((rec["base"], rec["cost"], rec["segments"], rec["first"]), (0, 10.5, 1, iso(T0)))

    def test_r7_a_backward_clock_step_as_a_real_restart_crosses_held_keeps_its_segment(self):
        # t-051 (t-043 check, s7): the restarted process crosses the held duration at the clock's speed in the same
        # reading as the clock steps back 60 s; growing at the clock's speed then looks faster than the clock
        rec = self.readings([(T0, 5.0, 600000), (T0 + 30, 0.0, 1000), (T0 + 620, 4.9, 591000),
                             (T0 + 560, 5.2, 621000)])
        self.assertEqual((rec["base"], rec["cost"], rec["segments"]), (5.0, 5.2, 2))
        self.assertAlmostEqual(pa_costs.cc_figure(pa_costs.cc_clean(rec)), 10.2)

    def test_r7b_a_real_restart_past_held_is_never_undone_even_by_an_unseen_clock_step(self):
        # t-051 (t-043 REVIEW RL-A): the restarted process has outlived the old one; then the clock steps back 80 s
        # inside a 100 s gap, so the clock still reads forward (20 s) and 100 s of growth looks faster than the clock
        rec = self.readings([(T0, 5.0, 600000), (T0 + 30, 0.0, 1000), (T0 + 640, 5.1, 611000),
                             (T0 + 660, 5.3, 711000)])
        self.assertEqual((rec["base"], rec["cost"], rec["segments"]), (5.0, 5.3, 2))
        self.assertAlmostEqual(pa_costs.cc_figure(pa_costs.cc_clean(rec)), 10.3)

    def test_r8_a_resume_with_several_low_readings_is_counted_once(self):
        # t-051: the restore still undoes the drop: it jumps across the held duration in one reading
        rec = self.readings([(T0, 30.0, 28800000), (T0 + 100, 0.0, 500), (T0 + 110, 0.05, 10500), (T0 + 120, 0.1, 20500),
                             (T0 + 130, 30.1, 28805000)])
        self.assertEqual((rec["base"], rec["cost"], rec["segments"]), (0, 30.1, 1))
        self.assertAlmostEqual(pa_costs.cc_figure(pa_costs.cc_clean(rec)), 30.1)
        self.assertNotIn("heldCost", rec)

    def test_r9_a_restore_while_the_clock_reads_backward_is_still_a_restore(self):
        # t-051 round 2 (check x1, REVIEW RL-1): the clock steps back 70 s between the low reading and the restore; the
        # elapsed time counts as 0, the restored jump is still faster than the clock, so the resume is counted once
        rec = self.readings([(T0, 30.0, 28800000), (T0 + 100, 0.0, 500), (T0 + 30, 30.1, 28805000)])
        self.assertEqual((rec["base"], rec["cost"], rec["segments"]), (0, 30.1, 1))
        self.assertAlmostEqual(pa_costs.cc_figure(pa_costs.cc_clean(rec)), 30.1)
        self.assertNotIn("heldCost", rec)

    def test_r10_a_crash_resume_with_a_small_backward_step_is_counted_once(self):
        # t-051 round 2 (check x6): a crash, then a resume whose restore comes with the clock 5 s back
        rec = self.readings([(T0, 20.0, 14400000), (T0 + 100, 0.0, 800), (T0 + 95, 20.0, 14410000),
                             (T0 + 400, 22.0, 14700000)])
        self.assertEqual((rec["base"], rec["cost"], rec["segments"]), (0, 22.0, 1))
        self.assertAlmostEqual(pa_costs.cc_figure(pa_costs.cc_clean(rec)), 22.0)
        self.assertNotIn("heldCost", rec)


class CC_ResumeSummary(CCFixture):
    """t-043: the summary reads a written record with restored totals as counted once, and marks it."""

    def test_rs1_corrected_session_is_marked(self):
        self.fixture()
        self.cc(self.SA, 1.5, base=4.0, segments=3, durationMs=7200000, at=T0 + 600)  # 2 h of totals in a 10 min record
        pa_costs.write_summary(self.dir, now=T0 + 5000)
        s = load(self.costs("summary.json"))
        by = {e["key"]: e for e in s["sessions"]}
        a = by[self.SA[:8]]
        self.assertAlmostEqual(a["cc"], 1.75)  # 1.5 alone, plus the /clear continuation SD's 0.25
        self.assertEqual(a["ccCorrected"], 1)
        self.assertNotIn("ccSegments", a)
        c = by[self.SC[:8]]  # a real restart (1 s of totals in a 60 s record): unchanged
        self.assertAlmostEqual(c["cc"], 3.5)
        self.assertEqual(c["ccSegments"], 2)
        self.assertNotIn("ccCorrected", c)
        self.assertAlmostEqual(s["totals"]["cc"]["money"], 7.25)


class CC_Report(CCFixture):
    """R6: report shows CC beside est., est. first; CC never in shares, sorting or est."""

    def text(self, **kw):
        _r, out, _e = self.run_cmd(pa_costs.report, self.dir, now=T0 + 5000, **kw)
        return out

    def total_line(self, out):
        return next(l for l in out.splitlines() if l.startswith("Total"))

    def test_cr1_total_line_with_prices(self):
        self.fixture()
        line = self.total_line(self.text())
        self.assertIn("Claude Code 7.25 USD list price (3 of 3 Claude sessions)", line)
        self.assertLess(line.index("est."), line.index("Claude Code"))
        self.assertRegex(line, r"^Total\s+\d+\.\d\d est\.")

    def test_cr2_total_line_without_prices(self):
        self.fixture(prices=False)
        line = self.total_line(self.text())
        self.assertRegex(line, r"^Total\s+— est\. \(no price\)")
        self.assertIn("Claude Code 7.25 USD list price (3 of 3 Claude sessions)", line)

    def test_cr3_session_and_task_tables_get_a_cc_column_and_a_legend(self):
        self.fixture()
        out = self.text(by="session")
        head = next(l for l in out.splitlines() if "est." in l and "tokens" in l)
        self.assertLess(head.index("est."), head.index(" CC "))
        self.assertIn("CC/est.", head)
        rows = {l.split()[0]: l for l in out.splitlines() if l.startswith("  ") and l.split()[0] in (self.SA[:8], self.SF[:8])}
        self.assertIn("1.75", rows[self.SA[:8]])
        self.assertIn("—", rows[self.SF[:8]])
        self.assertIn("CC = Claude Code's own list-price estimate (USD), per session; not the bill", out)
        out = self.text(by="task")
        head = next(l for l in out.splitlines() if "est." in l and "tokens" in l)
        self.assertIn(" CC ", head)
        self.assertNotIn("CC/est.", head)
        t1 = next(l for l in out.splitlines() if l.startswith("  t-001"))
        self.assertIn("1.75", t1)
        self.assertIn("CC = Claude Code's own list-price estimate (USD), per session; not the bill", out)

    def test_cr4_no_ratio_without_a_usd_price_and_unmatched_line(self):
        t = json.loads(json.dumps(PRICES))
        t["currency"] = "EUR"
        self.prices(t)
        self.fixture(prices=False)
        out = self.text(by="session")
        self.assertNotIn("CC/est.", out)
        out = self.text()
        line = [l for l in out.splitlines() if "matching no session" in l]
        self.assertEqual(len(line), 1, out)
        self.assertIn("1", line[0])
        self.assertIn("0.75", line[0])

    def test_cr5_json_and_since(self):
        self.fixture()
        _r, out, _e = self.run_cmd(pa_costs.report, self.dir, as_json=True, now=T0 + 5000)
        j = json.loads(out)
        self.assertAlmostEqual(j["totals"]["cc"]["money"], 7.25)
        _r, out, _e = self.run_cmd(pa_costs.report, self.dir, as_json=True, since=iso(T0)[:10], now=T0 + 5000)
        j = json.loads(out)
        self.assertAlmostEqual(j["totals"]["cc"]["money"], 3.75)  # SC's rows are before --since
        self.assertEqual(j["totals"]["cc"]["allSessions"], 3)

    def test_cr6_without_cc_the_report_is_as_before(self):
        self.fixture(with_cc=False)
        out = self.text()
        self.assertNotIn("Claude Code", out)
        self.assertNotIn("(no price)", out)
        out = self.text(by="session")
        self.assertNotIn(" CC", out)


class CC_Docs(unittest.TestCase):
    def test_cdoc1_readme_and_docstring(self):
        with open(os.path.join(HERE, "..", "README.md")) as f:
            readme = f.read()
        for want in ("statusLine", "pa-" + "costs statusline", "PA_NO_CC_STATUSLINE", "list price", "refreshInterval", "Codex"):
            self.assertIn(want, readme)
        self.assertIn("statusline", pa_costs.__doc__)


class CC_Limits(CCBase):
    """t-029 Job 2 (card d-090): REVIEW-R2 R2-L1 (no kill by the pid of a reaped inner command, the Python 3.7/3.8
    fallback included) and REVIEW L1 (the id checks refuse a trailing newline)."""

    class FakeProc:
        """A Popen stand-in whose kill() signals nothing and does not poll first, like Popen.kill on 3.7 and 3.8."""

        def __init__(self, returncode):
            self.pid, self.returncode, self.kills = 999999, returncode, 0

        def kill(self):
            self.kills += 1

    def kill_calls(self, proc, killpg_fails=False):
        calls = []

        def fake_killpg(pid, sig):
            calls.append((pid, sig))
            if killpg_fails:
                raise OSError(3, "no such process group")
        real = pa_costs.os.killpg
        pa_costs.os.killpg = fake_killpg
        try:
            pa_costs.statusline_kill(proc)
        finally:
            pa_costs.os.killpg = real
        return calls

    def test_cl1_a_reaped_process_gets_no_kill(self):
        for code in (0, 1, -9):
            proc = self.FakeProc(code)
            self.assertEqual((self.kill_calls(proc), proc.kills), ([], 0), code)
            proc = self.FakeProc(code)
            self.assertEqual((self.kill_calls(proc, killpg_fails=True), proc.kills), ([], 0), "fallback, {}".format(code))

    def test_cl2_a_running_process_is_still_killed(self):
        proc = self.FakeProc(None)
        self.assertEqual((self.kill_calls(proc), proc.kills), ([(999999, 9)], 0))
        proc = self.FakeProc(None)  # killpg fails: the fallback kills the process alone, while it is still ours
        self.assertEqual((self.kill_calls(proc, killpg_fails=True), proc.kills), ([(999999, 9)], 1))

    def test_cl3_after_a_normal_end_and_after_the_timeout_nothing_is_killed(self):
        for stub in ("ok", "slow"):
            out = self.run_sl(cc_bytes(), then=(stub,), now=T0 + (0 if stub == "ok" else 400))
            self.assertEqual(out, b"STATUS-LINE ok" if stub == "ok" else b"")
            proc = pa_costs.CC_STOP["proc"]
            self.assertIsNotNone(proc)
            self.assertIsNotNone(proc.returncode, "{}: reaped".format(stub))
            kills = []
            real_kill = proc.kill
            proc.kill = lambda: kills.append(1)
            try:
                self.assertEqual(self.kill_calls(proc, killpg_fails=True), [], stub)
            finally:
                proc.kill = real_kill
            self.assertEqual(kills, [], stub)

    def test_cl4_a_cancel_after_the_inner_command_ended_kills_nothing(self):
        out = self.run_sl(cc_bytes())
        self.assertEqual(out, b"STATUS-LINE ok")
        proc = pa_costs.CC_STOP["proc"]
        self.assertIsNotNone(proc.returncode)

        class Exited(Exception):
            pass

        def fake_exit(code):
            raise Exited(code)
        calls = []
        real_killpg, real_exit, real_kill = pa_costs.os.killpg, pa_costs.os._exit, proc.kill
        pa_costs.os.killpg, pa_costs.os._exit = (lambda pid, sig: calls.append((pid, sig))), fake_exit
        proc.kill = lambda: calls.append("kill")
        try:
            with self.assertRaises(Exited) as done:
                pa_costs.statusline_stop()
        finally:
            pa_costs.os.killpg, pa_costs.os._exit, proc.kill = real_killpg, real_exit, real_kill
            pa_costs.CC_STOP["stopping"] = False
            if pa_costs.CC_STOP["write"].locked():
                pa_costs.CC_STOP["write"].release()
        self.assertEqual(done.exception.args, (0,))
        self.assertEqual(calls, [])

    def test_cl5_a_trailing_newline_is_refused_in_every_id(self):
        sid = "abcdefgh"
        for bad in (sid + "\n", CC_SID + "\n"):
            self.assertIsNone(pa_costs.cc_fields(cc_input(sid=bad), "t5", iso(T0)), repr(bad))
            before = all_files(pa.ROOT)
            self.assertEqual(self.run_sl(cc_bytes(sid=bad)), b"STATUS-LINE ok")
            self.assertEqual([n for n in all_files(pa.ROOT) - before if not n.endswith(".lock")], [], repr(bad))
        self.assertEqual(pa_costs.cc_fields(cc_input(sid=sid), "t5", iso(T0))["session"], sid)  # the id itself is fine
        f = pa_costs.cc_fields(cc_input(version="2.1.0\n", model="model-a\n"), "t5\n", iso(T0))
        self.assertEqual((f["session"], f["version"], f["model"], f["terminal"]), (CC_SID, None, None, None))
        f = pa_costs.cc_fields(cc_input(version="2.1.0", model="model-a"), "t5", iso(T0))
        self.assertEqual((f["version"], f["model"], f["terminal"]), ("2.1.0", "model-a", "t5"))
        os.environ["HC_TERMINAL_ID"] = "t5\n"
        self.run_sl(cc_bytes())
        self.assertIsNone(self.cc_record()["terminal"])

    def test_cl6_a_space_id_with_a_trailing_newline_gets_nothing(self):
        other = os.path.join(pa.ROOT, "spaces", "sp1\n")
        os.makedirs(other)
        self.addCleanup(shutil.rmtree, other, True)
        self.assertEqual(self.run_sl(cc_bytes(), space="sp1\n"), b"STATUS-LINE ok")
        self.assertEqual(all_files(other), set())
        self.assertIsNone(self.cc_record())

    def test_cl7_a_stored_record_with_a_trailing_newline_is_skipped(self):
        rec = {"v": 1, "session": CC_SID, "cost": 1.0, "base": 0, "segments": 1, "durationMs": 1, "apiDurationMs": 1,
               "model": "model-a", "version": "2.1.0", "terminal": "t5", "first": iso(T0), "at": iso(T0)}
        self.assertEqual(pa_costs.cc_clean(dict(rec))["session"], CC_SID)
        self.assertIsNone(pa_costs.cc_clean(dict(rec, session=CC_SID + "\n")))
        got = pa_costs.cc_clean(dict(rec, model="model-a\n", version="2.1.0\n", terminal="t5\n"))
        self.assertEqual((got["model"], got["version"], got["terminal"]), (None, None, None))
        folder = os.path.join(self.dir, "costs", "cc")
        os.makedirs(folder, exist_ok=True)
        write(os.path.join(folder, CC_SID + "\n.json"), json.dumps(dict(rec, session=CC_SID + "\n")))
        records, problems = pa_costs.read_cc(self.dir)
        self.assertEqual(records, [])
        self.assertTrue(any("skipped" in p for p in problems), problems)


class T28_DayActivities(Collecting):
    """t-028 C1 (t-009 REVIEW-PANEL RL-R1): byDay[].activities lists each activity of the day with its tokens, money
    and unpricedTokens (a partly unpriced one is marked, a wholly unpriced one is listed); byActivity stays as it was."""
    mixed_space = F4_Partial.mixed_space

    def test_c1_partly_unpriced_activity_is_marked(self):
        s = self.mixed_space()
        day = s["byDay"][0]
        acts = {a["key"]: a for a in day["activities"]}
        self.assertIsNotNone(acts["build"]["money"])
        self.assertEqual(acts["build"]["unpricedTokens"], 3000000)
        self.assertEqual(sum(a["tokens"] for a in day["activities"]), day["tokens"])
        for a in day["activities"]:
            self.assertEqual(sorted(k for k in a if k in ("key", "tokens", "money")), ["key", "money", "tokens"])
        self.assertIsInstance(day["byActivity"]["build"], float)  # the old field: priced money only, as before

    def test_c1_wholly_unpriced_activity_is_listed(self):
        self.prices()
        self.ledger({"version": 1, "tasks": [{"id": "t-001", "state": "in-progress",
                                              "workers": [{"address": "t5", "role": "build", "name": "t-001-build"}]}]})
        self.record(session=SID, task="t-001", name="t-001-build", terminal="t5")
        self.add(self.prompt(T0 + 1), self.call(T0 + 2, "m1", lines=1, out=300, model="model-c"))
        self.collect(T0 + 2000)
        s = load(self.costs("summary.json"))
        day = s["byDay"][0]
        acts = {a["key"]: a for a in day["activities"]}
        self.assertTrue(acts, day)
        for a in acts.values():
            self.assertIsNone(a["money"], a)
            self.assertGreater(a["tokens"], 0, a)
        self.assertEqual(sum(a["tokens"] for a in acts.values()), day["tokens"])
        self.assertEqual(day["byActivity"], {})  # the old field leaves it out (no price)


# ---- t-036: days in the person's own timezone ----------------------------------------------------------------------

# America/Los_Angeles from 2026 on: minutes east of UTC in force from each epoch on (the panel's --zone-offsets form).
LA = [[0, -480], [1772964000, -420], [1793523600, -480], [1805018400, -420]]
LA_ZONE = {"zone": "America/Los_Angeles", "offsets": LA}
LA_ARG = ",".join("{}:{}".format(e, m) for e, m in LA)


@contextlib.contextmanager
def tz(name):
    """The process's own timezone (the report's terminal), set by the test and put back after it."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        os.environ["TZ"] = old if old is not None else "UTC"
        time.tzset()


class T36_LocalDays(Collecting):
    """The summary's days cut at the person's local midnight (W1, W2), on disk UTC (W3), the report local (W4)."""

    def row(self, at, seq, tokens=100, activity="build"):
        return {"seq": seq, "session": SID, "from": iso(at), "to": iso(at + 60), "day": iso(at)[:10], "calls": 1,
                "tokens": toks(**{"in": tokens}), "long": dict(ZERO), "model": "model-a", "activity": activity,
                "kind": "worker"}

    def summ(self, rows, zone=None, now=T0 + 86400):
        return pa_costs.summarize(self.dir, rows, [], None, None, "test", now, cc=[], zone=zone)

    def keys(self, s):
        return [d["key"] for d in s["byDay"]]

    def test_w1_a_row_before_local_midnight_is_the_local_day_before(self):
        at = T0 + 6 * 3600 + 1800  # 2026-11-17T06:30Z = 2026-11-16 22:30 PST
        rows = [self.row(at, 1, 100, "build"), self.row(T0 + 9 * 3600, 2, 50, "review")]  # 09:00Z = 01:00 PST on the 17th
        s = self.summ(rows, LA_ZONE)
        self.assertEqual(self.keys(s), ["2026-11-17", "2026-11-16"])
        by = {d["key"]: d for d in s["byDay"]}
        self.assertEqual([a["key"] for a in by["2026-11-16"]["activities"]], ["build"])
        self.assertEqual([a["key"] for a in by["2026-11-17"]["activities"]], ["review"])
        self.assertEqual(s["days"], {"zone": "America/Los_Angeles", "offsets": LA})
        # W3: what is not a day bucket stays UTC
        self.assertEqual([e["day"] for e in s["byDayModel"]], ["2026-11-17"])
        self.assertEqual([h[0] for h in s["hours"]], ["2026-11-17T06", "2026-11-17T09"])
        self.assertTrue(s["updated"].endswith("Z"))

    def test_w1_no_zone_is_utc_days_named_utc(self):
        s = self.summ([self.row(T0 + 6 * 3600 + 1800, 1)])
        self.assertEqual(self.keys(s), ["2026-11-17"])
        self.assertEqual(s["days"], {"zone": "UTC", "offsets": [[0, 0]]})

    def test_w2_daylight_saving_changes_where_local_midnight_falls(self):
        before = 1772964000 - 3600 * 2 - 1800  # 2026-03-08T07:30Z, PST: 23:30 on 03-07
        after = 1772964000 + 3600 * 21 + 1800  # 2026-03-09T07:30Z, PDT: 00:30 on 03-09 (PST would be 23:30 on 03-08)
        s = self.summ([self.row(before, 1), self.row(after, 2)], LA_ZONE, now=after + 3600)
        self.assertEqual(self.keys(s), ["2026-03-09", "2026-03-07"])
        nov = 1793523600 + 3600 * 15 + 1800  # 2026-11-02T00:30Z, PST again: 16:30 on 11-01
        self.assertEqual(self.keys(self.summ([self.row(nov, 1)], LA_ZONE, now=nov + 60)), ["2026-11-01"])

    def test_w2_a_half_hour_zone_cuts_at_its_own_midnight(self):
        india = {"zone": "Asia/Kolkata", "offsets": [[0, 330]]}
        s = self.summ([self.row(T0 + 18 * 3600 + 900, 1), self.row(T0 + 18 * 3600 + 2700, 2)], india)  # 18:15Z, 18:45Z
        self.assertEqual(self.keys(s), ["2026-11-18", "2026-11-17"])  # 23:45 and 00:15 local

    def test_w2_collect_keeps_the_panels_zone_and_rewrites_on_a_change(self):
        at = T0 + 6 * 3600 + 1800
        self.record(session=SID, task="t-001", terminal="t5")
        self.add(self.prompt(at), self.call(at + 1, "m1", lines=1, out=10))
        self.collect(at + 2000, zone=LA_ZONE)
        self.assertEqual(load(self.costs("zone.json")), LA_ZONE)
        s = load(self.costs("summary.json"))
        self.assertEqual(self.keys(s), ["2026-11-16"])
        self.assertEqual(s["days"]["zone"], "America/Los_Angeles")
        self.collect(at + 2100)  # no zone given (the Project Agent's own collect): the saved one
        self.assertEqual(self.keys(load(self.costs("summary.json"))), ["2026-11-16"])
        self.collect(at + 2200, zone={"zone": "UTC", "offsets": [[0, 0]]})  # no new rows, another zone: rewritten
        s = load(self.costs("summary.json"))
        self.assertEqual(self.keys(s), ["2026-11-17"])
        self.assertEqual(s["days"]["zone"], "UTC")
        # W3: the ledger's rows keep their UTC day and time
        self.assertEqual([(r["day"], r["from"][:19]) for r in self.rows()], [("2026-11-17", iso(at + 1)[:19])])

    def test_w2_no_saved_zone_is_utc_days(self):
        at = T0 + 6 * 3600 + 1800
        self.record(session=SID, task="t-001", terminal="t5")
        self.add(self.prompt(at), self.call(at + 1, "m1", lines=1, out=10))
        with tz("America/Los_Angeles"):  # the space's machine's own zone is not the person's: not used
            self.collect(at + 2000)
        s = load(self.costs("summary.json"))
        self.assertEqual(self.keys(s), ["2026-11-17"])
        self.assertEqual(s["days"]["zone"], "UTC")
        self.assertFalse(os.path.exists(self.costs("zone.json")))

    def test_w2_the_cli_takes_the_panels_zone_and_refuses_a_bad_one(self):
        os.makedirs(os.path.join(self.dir, "costs"), exist_ok=True)
        self.record(session=SID, task="t-001", terminal="t5")
        r, _out, _err = self.run_cmd(pa_costs.main, ["collect", "--space", "sp1", "--quiet", "--zone-offsets", LA_ARG,
                                                     "--zone-name", "America/Los_Angeles"])
        self.assertEqual(r, 0)
        self.assertEqual(load(self.costs("zone.json")), LA_ZONE)
        for bad in ("x", "0:-480,5:-420,3:-480", "0:9999", "", ",".join("{}:0".format(i * 100) for i in range(70))):
            with self.assertRaises(pa.Problem, msg=bad):
                pa_costs.main(["collect", "--space", "sp1", "--quiet", "--zone-offsets", bad])
        with self.assertRaises(pa.Problem):
            pa_costs.main(["collect", "--space", "sp1", "--quiet", "--zone-offsets", "0:60", "--zone-name", "a b;c"])
        self.assertEqual(load(self.costs("zone.json")), LA_ZONE)  # a refused zone changes nothing

    def test_w2_a_row_is_priced_by_its_utc_day_whatever_its_local_day(self):
        # t-032 review: the price entry is picked by the row's UTC day (validFrom), never by the day it is shown under.
        t = json.loads(json.dumps(PRICES))
        t["models"]["model-a"] = [{"validFrom": "2026-11-17", "in": 0.0, "out": 1000.0, "cw5m": 0.0, "cw1h": 0.0, "cr": 0.0},
                                  {"validFrom": "2026-01-01", "in": 0.0, "out": 10.0, "cw5m": 0.0, "cw1h": 0.0, "cr": 0.0}]
        self.prices(t)
        early, late = T0 - 1800, T0 + 6 * 3600 + 1800  # 2026-11-16T23:30Z and 2026-11-17T06:30Z: both 2026-11-16 in PST
        self.record(session=SID, task="t-001", terminal="t5")
        self.add(self.prompt(early), self.call(early + 1, "m1", lines=1, out=1000000))
        self.collect(early + 2000, zone=LA_ZONE)
        self.add(self.prompt(late), self.call(late + 1, "m2", lines=1, out=1000000))
        self.collect(late + 2000, zone=LA_ZONE)
        s = load(self.costs("summary.json"))
        self.assertEqual(self.keys(s), ["2026-11-16"])
        self.assertAlmostEqual(s["byDay"][0]["money"], 10.0 + 1000.0)  # the old price for 23:30Z, the new one for 06:30Z
        self.assertEqual({e["day"]: e["money"] for e in s["byDayModel"]}, {"2026-11-16": 10.0, "2026-11-17": 1000.0})

    def report_world(self):
        at = T0 + 6 * 3600 + 1800  # 2026-11-16 22:30 PST
        self.prices()
        self.record(session=SID, task="t-001", terminal="t5")
        self.add(self.prompt(at), self.call(at + 1, "m1", lines=1, out=10))
        self.collect(at + 2000, zone=LA_ZONE)
        return at + 2000  # 2026-11-17T07:03:20Z = 2026-11-16 23:03 PST

    def test_w4_report_days_are_the_terminals_local_days(self):
        now = self.report_world()
        with tz("America/Los_Angeles"):
            _r, out, _e = self.run_cmd(pa_costs.report, self.dir, by="day", now=now)
            self.assertIn("By day (PST)", out)
            self.assertIn("  2026-11-16", out)
            self.assertNotIn("  2026-11-17", out)
            _r, out, _e = self.run_cmd(pa_costs.report, self.dir, now=now)
            self.assertIn("updated 2026-11-16 23:03 PST", out)
            self.assertRegex(out, r"Today     \S+ est\. \(local day, PST\)")
            _r, out, _e = self.run_cmd(pa_costs.report, self.dir, by="day", since="2026-11-17", now=now)
            self.assertNotIn("  2026-11-16", out)
            _r, out, _e = self.run_cmd(pa_costs.report, self.dir, by="day", until="2026-11-16", now=now)
            self.assertIn("  2026-11-16", out)
        with tz("UTC"):  # the saved zone is the panel's, not the terminal's
            _r, out, _e = self.run_cmd(pa_costs.report, self.dir, by="day", now=now)
            self.assertIn("  2026-11-17", out)
            self.assertNotIn("  2026-11-16", out)

    def test_w4_report_utc_asks_for_utc_days(self):
        now = self.report_world()
        with tz("America/Los_Angeles"):
            _r, out, _e = self.run_cmd(pa_costs.report, self.dir, by="day", now=now, utc=True)
            self.assertIn("By day (UTC)", out)
            self.assertIn("  2026-11-17", out)
            self.assertNotIn("  2026-11-16", out)
            _r, out, _e = self.run_cmd(pa_costs.report, self.dir, now=now, utc=True)
            self.assertIn("updated 2026-11-17 07:03 UTC", out)
            self.assertRegex(out, r"Today     \S+ est\. \(UTC day\)")
            _r, out, _e = self.run_cmd(pa_costs.report, self.dir, by="day", since="2026-11-17", now=now, utc=True)
            self.assertIn("  2026-11-17", out)
            r, out, _e = self.run_cmd(pa_costs.main, ["report", "--space", "sp1", "--by", "day", "--utc"])
            self.assertEqual(r, 0)
            self.assertIn("By day (UTC)", out)
            _r, out, _e = self.run_cmd(pa_costs.report, self.dir, as_json=True, now=now)
            self.assertEqual(json.loads(out)["days"]["offsets"][0][1], -420)  # Aug 2026 (now - 100 days): PDT

    def test_w4_machine_zone_finds_the_daylight_saving_changes(self):
        with tz("America/Los_Angeles"):
            z = pa_costs.machine_zone(T0)
            self.assertEqual(z, {"zone": "PST", "offsets": [[0, -420], [1793523600, -480]]})
            z = pa_costs.machine_zone(T0 + 120 * 86400)  # 2027-03-17: back to 2026-12-07, ahead to 2027-04-21
            self.assertEqual(z["offsets"], [[0, -480], [1805018400, -420]])
            self.assertEqual(z["zone"], "PDT")
        with tz("Asia/Kolkata"):
            self.assertEqual(pa_costs.machine_zone(T0)["offsets"], [[0, 330]])
        with tz("UTC"):
            self.assertEqual(pa_costs.machine_zone(T0), {"zone": "UTC", "offsets": [[0, 0]]})


    # ---- round 2 (REVIEW MF1): every day cut locally falls inside the zone table; before it, UTC days, labelled ----
    def test_mf1_a_row_before_the_panels_table_is_a_utc_day_labelled(self):
        # The reviewer's repro: the LA table a panel passed on 2026-11-17 that reaches back 100 days only.
        old = pa_costs.parse_ts("2026-02-10T07:30:00Z")  # 23:30 PST on 2026-02-09
        rows = [self.row(old, 1), self.row(T0 - 1800, 2)]  # and 2026-11-16 15:30 PST
        short = pa_costs.parse_zone("0:-420,1793523600:-480", "America/Los_Angeles", known_from=T0 - 100 * 86400)
        s = self.summ(rows, short, now=T0)
        self.assertEqual(self.keys(s), ["2026-11-16", "2026-02-10"])  # 02-10: its UTC day, never a guessed local one
        by = {d["key"]: d for d in s["byDay"]}
        self.assertIs(by["2026-02-10"].get("utc"), True)
        self.assertNotIn("utc", by["2026-11-16"])
        self.assertEqual(s["days"], {"zone": "America/Los_Angeles", "offsets": [[0, -420], [1793523600, -480]], "from": T0 - 100 * 86400})
        # A table that reaches back to it: the local day, not labelled.
        full = pa_costs.parse_zone(LA_ARG, "America/Los_Angeles", known_from=T0 - 3653 * 86400)
        s = self.summ(rows, full, now=T0)
        self.assertEqual(self.keys(s), ["2026-11-16", "2026-02-09"])
        self.assertFalse([d for d in s["byDay"] if "utc" in d])

    def test_mf1_the_cli_takes_where_the_panels_table_starts(self):
        os.makedirs(os.path.join(self.dir, "costs"), exist_ok=True)
        self.record(session=SID, task="t-001", terminal="t5")
        args = ["collect", "--space", "sp1", "--quiet", "--zone-offsets", LA_ARG, "--zone-name", "America/Los_Angeles"]
        r, _out, _err = self.run_cmd(pa_costs.main, args + ["--zone-from", "1478000000"])
        self.assertEqual(r, 0)
        self.assertEqual(load(self.costs("zone.json")), dict(LA_ZONE, **{"from": 1478000000}))
        for bad in ("x", "1.5", "1234567890123"):
            with self.assertRaises(pa.Problem, msg=bad):
                pa_costs.main(args + ["--zone-from", bad])
        # The start moves on every day the panel runs: kept, but not a new zone (no rewrite of the summary for it).
        self.assertFalse(pa_costs.save_zone(self.dir, dict(LA_ZONE, **{"from": 1478086400})))
        self.assertEqual(pa_costs.load_zone(self.dir)["from"], 1478086400)
        self.assertTrue(pa_costs.save_zone(self.dir, {"zone": "UTC", "offsets": [[0, 0]], "from": 1478086400}))

    def test_mf1_report_since_and_until_before_100_days_are_local_days(self):
        old = pa_costs.parse_ts("2026-02-10T07:30:00Z")  # 23:30 PST on 2026-02-09
        self.prices()
        self.record(session=SID, task="t-001", terminal="t5")
        self.add(self.prompt(old - 1), self.call(old, "m1", lines=1, out=10))
        self.collect(old + 2000)
        self.add(self.prompt(T0 - 1800), self.call(T0 - 1799, "m2", lines=1, out=10))
        self.collect(T0 - 1000)
        with tz("America/Los_Angeles"):
            _r, out, _e = self.run_cmd(pa_costs.report, self.dir, by="day", since="2026-02-09", until="2026-02-09", now=T0)
            self.assertIn("  2026-02-09", out)
            self.assertNotIn("  2026-02-10", out)
            self.assertNotIn("  2026-11-16", out)
            _r, out, _e = self.run_cmd(pa_costs.report, self.dir, by="day", until="2026-02-09", now=T0)
            self.assertIn("  2026-02-09", out)
            _r, out, _e = self.run_cmd(pa_costs.report, self.dir, by="day", now=T0)
            self.assertIn("  2026-02-09", out)
            self.assertIn("  2026-11-16", out)
            self.assertNotIn("(UTC)", out)

    def test_mf1_machine_zone_reaches_back_to_the_oldest_row(self):
        old = pa_costs.parse_ts("2026-02-10T07:30:00Z")
        with tz("America/Los_Angeles"):
            z = pa_costs.machine_zone(T0, oldest=old)
            self.assertEqual(z, {"zone": "PST", "offsets": [[0, -480], [1772964000, -420], [1793523600, -480]]})
            # Further back than ZONE_MAX entries hold: the newest kept, "from" where they start; before it UTC days.
            z = pa_costs.machine_zone(T0, oldest=T0 - 40 * 365 * 86400)
            self.assertLessEqual(len(z["offsets"]), pa_costs.ZONE_MAX)
            self.assertEqual(z["offsets"][0][0], 0)
            self.assertEqual(z["offsets"][-1], [1793523600, -480])
            self.assertGreater(z["from"], T0 - 40 * 365 * 86400)
            before = z["from"] - z["from"] % 86400 - 86400 + 3600  # 01:00Z before it: the day before in LA
            self.assertEqual(pa_costs.day_in(z, pa_costs.iso(before)), pa_costs.iso(before)[:10])
            self.assertEqual(pa_costs.zone_of({"days": z}), z)  # it round-trips through the summary


# ---- t-032 J1a: price scopes and the lookup (R-P1, R-P2, R-A4, A-P1, A-P2, A-P3) ---------------------------------

SAMPLES = os.path.join(HERE, "samples")
sys.path.insert(0, SAMPLES)
import old_pricing  # noqa: E402  (frozen copy of the 99e7d8b price code: the reference for unchanged figures)


def scoped_fixture():
    return load(os.path.join(SAMPLES, "prices-scoped-neutral.json"))


class J1A_Scopes(CostsBase):
    """A-P2 precedence, fall-through and refusals, on tests/samples/prices-scoped-neutral.json (neutral names)."""

    def table(self, value=None, pricing=None):
        self.prices(scoped_fixture() if value is None else value)
        if pricing is not None:
            prof = load(os.path.join(self.profile_dir, "profile.json"))
            prof["pricing"] = pricing
            dump(prof, os.path.join(self.profile_dir, "profile.json"))
        table, problem = pa_costs.load_prices(pa.load_profile("test"))
        self.assertIsNone(problem)
        return table

    def refused(self, value):
        self.prices(value)
        table, problem = pa_costs.load_prices(pa.load_profile("test"))
        self.assertIsNone(table)
        self.assertIn("not usable", problem)
        return problem

    def look(self, table, model, facts, day="2026-11-17"):
        entry, priced = pa_costs.lookup(table, model, day + "T01:00:00Z", facts)
        return (entry or {}).get("in"), (priced or {}).get("scope")

    def test_j1a1_no_facts_is_the_default_scope(self):
        t = self.table()
        self.assertEqual(t["defaultScope"], ("provider-a", "global", None, None))
        self.assertEqual(self.look(t, "model-a", None), (1.0, ["provider-a", "global", "*", "*"]))
        self.assertEqual(self.look(t, "model-a", (None, None, None, None)), (1.0, ["provider-a", "global", "*", "*"]))
        self.assertEqual(pa_costs.price_for(t, "model-a", "2026-11-17T01:00:00Z")["in"], 1.0)
        self.assertEqual(self.look(t, "model-a-2026", None)[0], 1.0)  # aliases shared by every scope

    def test_j1a2_regional_row_from_its_region_never_from_global(self):
        t = self.table()
        self.assertEqual(self.look(t, "model-a", ("provider-a", "regional", "region-a", None)),
                         (1.1, ["provider-a", "regional", "region-a", "*"]))
        self.assertEqual(self.look(t, "model-a", ("provider-a", "regional", "region-b", "acct-a"))[0], 1.2)
        self.assertEqual(self.look(t, "model-a", ("provider-a", "regional", "region-c", None)), (None, None))
        self.assertEqual(self.look(t, "model-a", ("provider-a", "regional", None, None)), (None, None))  # unknown region
        self.assertEqual(self.look(t, "model-b", ("provider-a", "regional", "region-a", None)), (None, None))

    def test_j1a3_unknown_endpoint_takes_the_default_endpoint(self):
        t = self.table()
        self.assertEqual(self.look(t, "model-a", ("provider-a", None, None, None))[0], 1.0)
        self.assertEqual(self.look(t, "model-a", ("provider-a", None, "region-b", None)), (0.9, ["provider-a", "global", "region-b", "*"]))
        self.assertEqual(self.look(t, "model-a", ("provider-a", "global", "region-c", None))[0], 1.0)  # unknown region: region-null scopes

    def test_j1a4_account_beats_region_region_beats_endpoint(self):
        t = self.table()
        self.assertEqual(self.look(t, "model-a", ("provider-a", "global", "region-b", "acct-b"), "2026-07-01"),
                         (0.5, ["provider-a", "*", "*", "acct-b"]))
        # before the account row's validFrom: the next matching scope (region-b global), not nothing
        self.assertEqual(self.look(t, "model-a", ("provider-a", "global", "region-b", "acct-b"), "2026-03-01")[0], 0.9)
        self.assertEqual(self.look(t, "model-a", ("provider-a", "geo", "region-a", None)), (2.0, ["provider-a", "*", "region-a", "*"]))
        self.assertEqual(self.look(t, "model-a", ("provider-a", "geo", "region-c", None))[0], 3.0)
        # a model the account scope lacks falls through to the default
        self.assertEqual(self.look(t, "model-b", ("provider-a", "global", None, "acct-b"), "2026-07-01")[0], 2.0)

    def test_j1a5_no_crossing_of_providers(self):
        t = self.table()
        self.assertEqual(self.look(t, "model-a", ("provider-b", None, None, None)), (None, None))
        self.assertEqual(self.look(t, "model-a", ("provider-z", "global", None, None)), (None, None))
        self.assertEqual(self.look(t, "model-c", ("provider-b", None, None, None)), (7.0, ["provider-b", "*", "*", "*"]))
        self.assertEqual(self.look(t, "model-c-2026", ("provider-b", "global", "region-a", "acct-a"))[0], 7.0)  # scopedAliases
        self.assertEqual(self.look(t, "model-c", ("provider-a", None, None, None)), (None, None))
        self.assertEqual(self.look(t, "model-c", None), (None, None))

    def test_j1a6_endpoint_price_as(self):
        t = self.table(pricing={"endpointPriceAs": {"geo": "regional"}})
        self.assertEqual(self.look(t, "model-a", ("provider-a", "geo", "region-a", None)), (1.1, ["provider-a", "regional", "region-a", "*"]))
        self.assertEqual(self.look(t, "model-a", ("provider-a", "geo", "region-c", None)), (None, None))

    def test_j1a7_no_default_provider_known_provider_is_unpriced(self):
        t = self.table(dict(PRICES))  # A-P3: no defaultScope, no pricing.defaultProvider
        self.assertEqual(t["defaultScope"], (None, None, None, None))
        self.assertEqual(self.look(t, "model-a", None)[0], 1.0)
        self.assertEqual(self.look(t, "model-a", (None, "regional", "region-a", "acct-a"))[0], 1.0)
        self.assertEqual(self.look(t, "model-a", ("provider-a", None, None, None)), (None, None))

    def test_j1a8_default_provider_forms(self):
        legacy = dict(PRICES, defaultProvider="provider-a")
        self.assertEqual(self.table(legacy)["defaultScope"], ("provider-a", None, None, None))
        t = self.table(dict(PRICES), pricing={"defaultProvider": "provider-a"})
        self.assertEqual(t["defaultScope"], ("provider-a", None, None, None))
        self.assertEqual(self.look(t, "model-a", ("provider-a", "regional", "region-q", None))[0], 1.0)
        self.assertEqual(self.look(t, "model-a", ("provider-b", None, None, None)), (None, None))
        t = self.table(scoped_fixture(), pricing={"defaultProvider": "provider-z"})
        self.assertEqual(t["defaultScope"][0], "provider-a")  # the file wins
        self.assertTrue(any("provider-z" in w and "provider-a" in w for w in t["warnings"]), t["warnings"])

    def test_j1a9_row_money_uses_the_row_facts(self):
        t = self.table()
        row = {"model": "model-a", "from": "2026-11-17T01:00:00Z", "tokens": toks(**{"in": 1000000}), "long": dict(ZERO)}
        self.assertAlmostEqual(pa_costs.row_money(row, t), 1.0)
        self.assertAlmostEqual(pa_costs.row_money(dict(row, provider="provider-a", endpoint="regional", region="region-a"), t), 1.1)
        self.assertIsNone(pa_costs.row_money(dict(row, provider="provider-b"), t))
        c = dict(row, model="model-c", provider="provider-b", tokens=toks(cw5m=5))
        self.assertIsNone(pa_costs.row_money(c, t))  # a null price with tokens: no price, never 0

    def test_j1a10_refusals(self):
        base = scoped_fixture()

        def with_scoped(*entries):
            return dict(base, scoped=list(entries))
        m = {"model-a": [{"validFrom": "2026-01-01", "in": 1.0}]}
        self.refused(with_scoped({"provider": None, "endpoint": None, "region": None, "account": None, "models": m}))
        self.refused(with_scoped({"provider": "provider-a", "account": "acct-b", "models": m},
                                 {"provider": "provider-a", "account": "acct-b", "endpoint": None, "models": m}))
        self.refused(with_scoped({"provider": "provider-a", "endpoint": "global", "models": m}))  # equals defaultScope
        self.refused(with_scoped({"provider": "provider-a", "account": "12345" + "6789012", "models": m}))
        self.refused(with_scoped({"provider": "provider-a", "region": "Region A", "models": m}))
        self.refused(with_scoped({"provider": "provider-a", "endpoint": "Global!", "models": m}))
        self.refused(with_scoped({"provider": "provider-a", "region": "region-a", "models": {"model-a": [{"validFrom": "x"}]}}))
        self.refused(with_scoped({"provider": "provider-a", "region": "region-a"}))
        self.refused(dict(base, scoped={"provider": "provider-a"}))
        self.refused(dict(base, scopedAliases=[{"match": "^x", "model": "model-q"}]))
        self.refused(dict(base, defaultScope={"provider": "provider-a", "endpoint": "glo bal"}))
        self.refused(dict(base, defaultScope="provider-a"))

    def test_j1a11_row_scope_mismatch_is_a_warning(self):
        f = scoped_fixture()
        f["models"]["model-b"][0]["endpoint"] = "regional"
        t = self.table(f)
        self.assertTrue(any("model-b" in w and "endpoint" in w for w in t["warnings"]), t["warnings"])
        legacy = json.loads(json.dumps(PRICES))
        legacy["models"]["model-a"][0]["provider"] = "provider-q"  # files without defaultScope are not checked
        self.assertEqual(self.table(legacy)["warnings"], [])

    def test_j1a12_endpoint_beats_provider(self):
        # A-P2 step 2 (fix round 2): a provider-null scope with an endpoint beside a provider-only scope; a row both match
        f = scoped_fixture()
        f["scoped"] += [
            {"provider": None, "endpoint": "edge", "region": None, "account": None,
             "models": {"model-a": [{"validFrom": "2026-01-01", "in": 4.0, "out": 40.0, "cw5m": 8.0, "cw1h": 16.0, "cr": 2.0}]}},
            {"provider": "provider-a", "endpoint": None, "region": None, "account": None,
             "models": {"model-a": [{"validFrom": "2026-01-01", "in": 6.0, "out": 60.0, "cw5m": 12.0, "cw1h": 24.0, "cr": 3.0}]}}]
        t = self.table(f)
        self.assertEqual(self.look(t, "model-a", ("provider-a", "edge", None, None)), (4.0, ["*", "edge", "*", "*"]))
        self.assertEqual(self.look(t, "model-a", ("provider-a", "edge", "region-c", None)), (4.0, ["*", "edge", "*", "*"]))
        # the provider-only scope still prices what the endpoint scope does not match
        self.assertEqual(self.look(t, "model-a", ("provider-a", "other", None, None)), (6.0, ["provider-a", "*", "*", "*"]))


class J1A_Parity(CostsBase):
    """R-P1/A-P1: the 99e7d8b code (tests/samples/old_pricing.py) reads the scoped file as its top-level models,
    and the new code prices rows without facts exactly as the old one."""

    DAYS = ("2025-12-31", "2026-01-01", "2026-06-01", "2026-11-17", "2027-01-01")
    MODELS = ("model-a", "model-a-2026", "model-b", "model-c", "model-c-2026", "model-z", None)

    def both(self, value):
        self.prices(value)
        prof = pa.load_profile("test")
        return old_pricing.load_prices(prof), pa_costs.load_prices(prof)

    def same_prices(self, old, new):
        for model in self.MODELS:
            for day in self.DAYS:
                when = day + "T10:00:00Z"
                o, n = old_pricing.price_for(old, model, when), pa_costs.price_for(new, model, when)
                self.assertEqual(o, n, (model, day))
                tokens = toks(**{"in": 1234, "out": 5678, "cw5m": 91011, "cw1h": 1213, "cr": 141516})
                self.assertEqual(old_pricing.money(tokens, {}, o), pa_costs.money(tokens, {}, n), (model, day))

    def test_j1p1_old_code_reads_the_scoped_file_as_its_models(self):
        (old, old_problem), (new, new_problem) = self.both(scoped_fixture())
        self.assertIsNone(old_problem)
        self.assertIsNone(new_problem)
        self.assertEqual(old["warnings"], [])
        self.assertEqual(sorted(old["models"]), ["model-a", "model-b"])
        self.assertEqual(old["models"], new["models"])
        for model in ("model-a", "model-b", "model-a-2026"):
            for day in self.DAYS:
                when = day + "T10:00:00Z"
                self.assertEqual(old_pricing.price_for(old, model, when), pa_costs.price_for(new, model, when))

    def test_j1p2_files_without_the_new_keys_price_as_before(self):
        installed = json.loads(json.dumps(PRICES))  # the installed shape: informational keys on rows, no scopes
        for rows in installed["models"].values():
            for r in rows:
                r.update(provider="provider-q", source="made up", refs={"in": {"sku": "S"}})
        installed["unverified"] = ["a note"]
        for value in (PRICES, installed):
            (old, _p1), (new, _p2) = self.both(value)
            self.assertEqual(new["scoped"], [])
            self.same_prices(old, new)
            self.assertEqual(old["warnings"], new["warnings"])
            for facts in (None, (None, None, None, None), (None, "global", None, "acct-a"), (None, "regional", "region-a", None)):
                for model in self.MODELS:
                    self.assertEqual(pa_costs.lookup(new, model, "2026-11-17T10:00:00Z", facts)[0],
                                     old_pricing.price_for(old, model, "2026-11-17T10:00:00Z"), (facts, model))


class J1A_Snapshot(Collecting):
    """The pre-J1 summary (tests/samples/summary-pre-j1.json, written by the 99e7d8b code for S_Summary.mixed at
    commit 'J1a tests'): every key it has is unchanged under the J1 code (added keys left out of the compare)."""

    mixed = S_Summary.mixed

    def restrict(self, new, old, path="$"):
        if isinstance(old, dict):
            self.assertIsInstance(new, dict, path)
            for k in old:
                self.assertIn(k, new, "{}.{}".format(path, k))
            return {k: self.restrict(new[k], old[k], "{}.{}".format(path, k)) for k in old}
        if isinstance(old, list):
            self.assertIsInstance(new, list, path)
            self.assertEqual(len(new), len(old), path)
            return [self.restrict(n, o, "{}[{}]".format(path, i)) for i, (n, o) in enumerate(zip(new, old))]
        return new

    def test_j1s1_pre_j1_summary_keys_unchanged(self):
        old = load(os.path.join(SAMPLES, "summary-pre-j1.json"))
        new = json.loads(json.dumps(self.mixed()))
        self.assertEqual(self.restrict(new, old), old)


# ---- t-032 J1b: facts at launch, in rows and for old rows; one account chain (R-P3, R-A1, R-A2, R-A6, A-R1-A-R3) ----

J1_PRICING = {"defaultProvider": "provider-a",
              "providers": [{"program": "claude", "model": None, "provider": "provider-a"},
                            {"program": "codex", "model": "^model-x", "provider": "provider-x"}],
              "endpoints": [{"model": r"^global\.", "endpoint": "global"}, {"model": r"^(us|eu)\.", "endpoint": "geo"},
                            {"model": r"^model-", "endpoint": "regional"}],
              "region": {"env": ["J1_REGION_A", "J1_REGION_B"], "match": "^region-[a-z]$"}}
J1_ENV = ("PA_COST_ACCOUNT", "PA_COST_REGION", "J1_REGION_A", "J1_REGION_B", "J1_PROFILE", "J1_REGION_SECRET", "J1_TOKEN", "J1_ENDPOINT")
FACT_KEYS = ("provider", "providerBy", "endpoint", "endpointBy", "region", "regionBy", "account", "accountBy")


def every_file_bytes(root):
    out = b""
    for folder, _dirs, names in os.walk(root):
        for name in names:
            out += raw(os.path.join(folder, name)) or b""
    return out


class J1B_Launch(Base):
    """Launch records of pa-claude / pa-codex workers and the Project Agent carry the facts (labels only)."""

    launch = L_LaunchRecords.launch
    records = L_LaunchRecords.records

    def setUp(self):
        super().setUp()
        self.assertIsNotNone(pa_costs, COSTS_MISSING)
        for k in J1_ENV:
            self.addCleanup(os.environ.pop, k, None)
            os.environ.pop(k, None)
        self.set_profile(J1_PRICING)
        self.ledger({"version": 1, "tasks": [{"id": "t-010", "state": "in-progress", "workers": []}]})
        self.hc.terminals["t5"] = {"address": "t5", "name": "t-010-build", "parent": "t0", "state": "live", "labels": {"herdr.space": "sp1"}}
        os.environ["HC_TERMINAL_ID"] = "t5"
        os.environ["PA_PRINT"] = "1"
        self.addCleanup(os.environ.pop, "HC_TERMINAL_ID", None)
        self.addCleanup(os.environ.pop, "PA_PRINT", None)

    def set_profile(self, pricing, **extra):
        prof = {"name": "test", "claude": ["true"], "codex": ["true"], "models": {"worker": "global.model-a"}}
        if pricing is not None:
            prof["pricing"] = pricing
        prof.update(extra)
        dump(prof, os.path.join(self.profile_dir, "profile.json"))

    def machine(self, **costs):
        dump({"version": 1, "defaultProfile": "test", "costs": costs}, os.path.join(pa.ROOT, "config.json"))

    def last(self, kind="claude", *argv):
        if os.path.exists(self.costs("sessions.jsonl")):
            os.unlink(self.costs("sessions.jsonl"))
        self.launch(kind, *(argv or ("do the job",)))
        recs = self.records()
        self.assertEqual(len(recs), 1)
        return recs[0]

    def facts(self, rec):
        return {k: rec.get(k) for k in FACT_KEYS}

    def test_j1b1_provider_and_endpoint_from_the_model_id_form(self):
        r = self.last()
        self.assertEqual(r["model"], "global.model-a")
        self.assertEqual((r["provider"], r["providerBy"], r["endpoint"], r["endpointBy"]), ("provider-a", "launch", "global", "rule"))
        self.assertEqual((self.last("claude", "--model", "us.model-a", "x")["endpoint"]), "geo")
        self.assertEqual((self.last("claude", "--model", "model-a", "x")["endpoint"]), "regional")
        r = self.last("claude", "--model", "other-1", "x")
        self.assertEqual((r["endpoint"], r["endpointBy"]), (None, "default"))
        r = self.last("codex", "go")  # a Codex worker with no --model: no codex rule matches
        self.assertEqual((r["provider"], r["providerBy"]), (None, "unknown"))
        r = self.last("codex", "--model", "model-x1", "go")
        self.assertEqual((r["provider"], r["providerBy"], r["endpoint"]), ("provider-x", "launch", "regional"))

    def test_j1b2_region_from_the_declared_variables_in_order(self):
        r = self.last()
        self.assertEqual((r["region"], r["regionBy"]), (None, "unknown"))
        os.environ["J1_REGION_B"] = "region-b"
        self.assertEqual((self.last()["region"], self.last()["regionBy"]), ("region-b", "env:J1_REGION_B"))
        os.environ["J1_REGION_A"] = "region-a"
        self.assertEqual((self.last()["region"], self.last()["regionBy"]), ("region-a", "env:J1_REGION_A"))
        os.environ["PA_COST_REGION"] = "region-q"
        self.assertEqual((self.last()["region"], self.last()["regionBy"]), ("region-q", "env:PA_COST_REGION"))
        os.environ.pop("PA_COST_REGION")
        os.environ.pop("J1_REGION_A")
        os.environ.pop("J1_REGION_B")
        self.machine(region="region-m")
        self.assertEqual((self.last()["region"], self.last()["regionBy"]), ("region-m", "machine"))

    def test_j1b3_a_value_that_is_not_a_region_is_never_stored(self):
        secret = "secret-profile-" + fx.CANARY
        os.environ["J1_REGION_A"] = secret
        os.environ["J1_REGION_B"] = "region-b"
        r = self.last()
        self.assertEqual((r["region"], r["regionBy"]), (None, "refused"))
        self.assertIn("J1_REGION_A", r.get("refused") or [])
        os.environ["PA_COST_REGION"] = "12345" + "6789012"
        r = self.last()
        self.assertNotEqual(r["region"], os.environ["PA_COST_REGION"])
        self.assertIn("PA_COST_REGION", r.get("refused") or [])
        self.assertNotIn(secret.encode(), every_file_bytes(pa.ROOT))
        self.assertNotIn(b"6789012", every_file_bytes(pa.ROOT))

    def test_j1b4_declared_names_that_look_like_credentials_are_refused(self):
        bad = dict(J1_PRICING, region={"env": ["J1_REGION_SECRET"], "match": ".*"},
                   endpoints=[{"env": "J1_TOKEN", "match": ".*", "endpoint": "global"}])
        self.set_profile(bad)
        os.environ["J1_REGION_SECRET"] = "region-a"
        os.environ["J1_TOKEN"] = "x"
        r = self.last()
        self.assertEqual((r["region"], r["regionBy"]), (None, "unknown"))
        self.assertEqual(r["endpointBy"], "default")
        _rules, problems = pa_costs.pricing_rules(pa.load_profile("test"))
        self.assertTrue(any("J1_REGION_SECRET" in p for p in problems), problems)
        self.assertTrue(any("J1_TOKEN" in p for p in problems), problems)
        self.machine(accountRules=[{"env": "J1_TOKEN", "match": ".*", "account": "acct-t"}])
        self.assertNotEqual(self.last()["account"], "acct-t")

    def test_j1b5_endpoint_env_rule(self):
        self.set_profile(dict(J1_PRICING, endpoints=[{"env": "J1_ENDPOINT", "match": "^1$", "endpoint": "edge-a"}] + J1_PRICING["endpoints"]))
        os.environ["J1_ENDPOINT"] = "1"
        r = self.last()
        self.assertEqual((r["endpoint"], r["endpointBy"]), ("edge-a", "rule"))
        os.environ["J1_ENDPOINT"] = "0"
        self.assertEqual(self.last()["endpoint"], "global")

    def test_j1b6_account_from_env_then_rules_then_the_chain(self):
        r = self.last()
        self.assertEqual((r["account"], r["accountBy"]), ("default", "default"))
        self.set_profile(J1_PRICING, billing={"account": "acct-p"})
        self.assertEqual((self.last()["account"], self.last()["accountBy"]), ("acct-p", "profile"))
        self.machine(account="acct-m")
        self.assertEqual((self.last()["account"], self.last()["accountBy"]), ("acct-m", "machine"))
        agent = load(os.path.join(self.dir, "agent.json"))
        dump(dict(agent, costs={"account": "acct-s"}), os.path.join(self.dir, "agent.json"))
        self.assertEqual((self.last()["account"], self.last()["accountBy"]), ("acct-s", "space"))
        secret = "secret-profile-" + fx.CANARY
        self.machine(account="acct-m", accountRules=[{"env": "J1_PROFILE", "match": "^secret-profile-", "account": "acct-b"}])
        os.environ["J1_PROFILE"] = secret
        self.assertEqual((self.last()["account"], self.last()["accountBy"]), ("acct-b", "rule"))
        os.environ["PA_COST_ACCOUNT"] = "acct-a"
        self.assertEqual((self.last()["account"], self.last()["accountBy"]), ("acct-a", "env"))
        os.environ["PA_COST_ACCOUNT"] = "12345" + "6789012"
        r = self.last()
        self.assertEqual((r["account"], r["accountBy"]), ("acct-b", "rule"))
        self.assertIn("PA_COST_ACCOUNT", r.get("refused") or [])
        self.assertNotIn(secret.encode(), every_file_bytes(pa.ROOT))
        self.assertNotIn(b"6789012", every_file_bytes(pa.ROOT))

    def test_j1b7_two_workers_with_different_accounts(self):
        os.environ["PA_COST_ACCOUNT"] = "acct-a"
        self.launch("claude", "one")
        os.environ["PA_COST_ACCOUNT"] = "acct-b"
        self.launch("claude", "two")
        self.assertEqual([r["account"] for r in self.records()], ["acct-a", "acct-b"])

    def test_j1b8_malformed_rules_never_stop_a_launch(self):
        self.set_profile({"providers": "x", "endpoints": [{"model": "(", "endpoint": "global"}], "region": {"env": "J1_REGION_A", "match": "("},
                          "defaultProvider": 7})
        self.machine(accountRules="nope", region=["x"], account={"a": 1})
        os.environ["J1_REGION_A"] = "region-a"
        argv = self.launch("claude", "go")
        self.assertIn("--session-id", argv)
        r = self.records()[-1]
        self.assertEqual((r["provider"], r["endpointBy"], r["regionBy"], r["account"]), (None, "default", "unknown", "default"))
        _rules, problems = pa_costs.pricing_rules(pa.load_profile("test"))
        self.assertTrue(problems)

    def test_j1b9_facts_function_never_raises(self):
        costs = pa.costs_module()  # the copy the launcher loads beside pa.py
        saved = costs.account_chain

        def boom(directory=None):
            raise RuntimeError("x")
        costs.account_chain = boom
        self.addCleanup(setattr, costs, "account_chain", saved)
        argv = self.launch("claude", "go")
        self.assertIn("--session-id", argv)
        r = self.records()[-1]
        self.assertEqual((r["providerBy"], r["account"], r["accountBy"]), ("unknown", "default", "default"))

    def test_j1b10_project_agent_start_record(self):
        prof = pa.load_profile("test")
        pa_costs.record_agent_start(self.dir, {"sessionId": "s-pa-1", "space": "sp1"}, "t0", self.project, "us.model-a", False, profile=prof)
        r = self.records()[-1]
        self.assertEqual((r["kind"], r["provider"], r["endpoint"], r["account"]), ("pa", "provider-a", "geo", "default"))
        pa_costs.record_agent_start(self.dir, {"sessionId": "s-pa-2", "space": "sp1"}, "t0", self.project, "us.model-a", False)
        r = self.records()[-1]  # an older caller without the profile: facts unknown, the record still written
        self.assertEqual((r["session"], r["providerBy"]), ("s-pa-2", "unknown"))


class J1B_Facts(Collecting):
    """make_row copies the facts of the session record; rows without them get them at summarize time."""

    def test_j1b11_rows_carry_the_record_facts(self):
        self.prices()
        self.record(session=SID, model="global.model-a", provider="provider-a", providerBy="launch", endpoint="global",
                    endpointBy="rule", region="region-a", regionBy="env:J1_REGION_A", account="acct-a", accountBy="env")
        self.add(self.prompt(T0 + 10), self.call(T0 + 12, "m1", lines=1, out=100))
        self.collect(T0 + 5000)
        rows = self.rows()
        self.assertTrue(rows)
        for r in rows:
            self.assertEqual((r["provider"], r["endpoint"], r["region"], r["account"], r["regionBy"]),
                             ("provider-a", "global", "region-a", "acct-a", "env:J1_REGION_A"))

    def test_j1b12_rows_of_records_without_facts_have_no_fact_keys(self):
        self.prices()
        self.record(session=SID)
        self.add(self.prompt(T0 + 10), self.call(T0 + 12, "m1", lines=1, out=100))
        self.collect(T0 + 5000)
        for r in self.rows():
            for k in FACT_KEYS:
                self.assertNotIn(k, r)

    def test_j1b13_old_rows_get_facts_from_the_rules(self):
        prof = {"name": "test", "claude": ["true"], "codex": ["true"], "pricing": J1_PRICING}
        dump(prof, os.path.join(self.profile_dir, "profile.json"))
        rules, problems = pa_costs.pricing_rules(pa.load_profile("test"))
        self.assertEqual(problems, [])
        chain = ("acct-c", "machine")
        row = {"session": SID, "model": "model-a", "program": "claude"}
        f = pa_costs.row_facts(row, {"program": "claude", "model": "global.model-a"}, rules, chain)
        self.assertEqual(f, {"provider": "provider-a", "providerBy": "rule", "endpoint": "global", "endpointBy": "rule",
                             "region": None, "regionBy": "unknown", "account": "acct-c", "accountBy": "machine"})
        f = pa_costs.row_facts(row, {}, rules, chain)  # no record model: the default provider and endpoint
        self.assertEqual((f["provider"], f["providerBy"], f["endpoint"], f["endpointBy"]), ("provider-a", "rule", None, "default"))
        f = pa_costs.row_facts(dict(row, program="codex"), {"program": "codex"}, rules, chain)
        self.assertEqual((f["provider"], f["providerBy"]), ("provider-a", "default"))
        launched = dict(row, provider="provider-b", providerBy="launch", endpoint="geo", endpointBy="rule", region="region-a",
                        regionBy="machine", account="acct-a", accountBy="env")
        f = pa_costs.row_facts(launched, {"model": "global.model-a"}, rules, chain)
        self.assertEqual({k: f[k] for k in FACT_KEYS}, {k: launched[k] for k in FACT_KEYS})
        f = pa_costs.row_facts(dict(row), {"model": "global.model-a"}, None, ("default", "default"))
        self.assertEqual((f["provider"], f["providerBy"], f["endpoint"], f["endpointBy"]), (None, "default", None, "default"))


class J1B_Account(CostsBase):
    """R-A1/R-A6: one chain for estimates and billed rows."""

    def test_j1b14_one_chain_on_a_home_with_only_billing_account(self):
        dump({"name": "test", "claude": ["true"], "codex": ["true"], "billing": {"account": "acct-a"}}, os.path.join(self.profile_dir, "profile.json"))
        self.assertEqual(pa_costs.costs_account(), "acct-a")
        self.assertEqual(pa_costs.default_account(), "acct-a")
        self.assertEqual(pa_costs.account_chain(self.dir), ("acct-a", "profile"))
        dump({"version": 1, "defaultProfile": "test", "costs": {"account": "acct-m"}}, os.path.join(pa.ROOT, "config.json"))
        self.assertEqual((pa_costs.costs_account(), pa_costs.default_account()), ("acct-m", "acct-m"))
        agent = load(os.path.join(self.dir, "agent.json"))
        dump(dict(agent, costs={"account": "acct-s"}), os.path.join(self.dir, "agent.json"))
        self.assertEqual(pa_costs.account_chain(self.dir), ("acct-s", "space"))
        self.assertEqual(pa_costs.account_chain(), ("acct-m", "machine"))
        dump(dict(agent, costs={"account": "12345" + "6789012"}), os.path.join(self.dir, "agent.json"))
        self.assertEqual(pa_costs.account_chain(self.dir), ("acct-m", "machine"))

    def test_j1b15_summary_account_follows_the_chain(self):
        dump({"name": "test", "claude": ["true"], "codex": ["true"], "billing": {"account": "acct-a"}}, os.path.join(self.profile_dir, "profile.json"))
        s = pa_costs.summarize(self.dir, [], [], None, None, "test", T0, cc=[])
        self.assertEqual((s["account"], s["accountBy"]), ("acct-a", "profile"))


class J1B_Doctor(Base):
    def test_j1b16_doctor_notes_refused_rules_and_records(self):
        import argparse
        dump({"name": "test", "claude": ["true"], "codex": ["true"], "pricing": {"region": {"env": ["J1_REGION_SECRET"], "match": ".*"}}},
             os.path.join(self.profile_dir, "profile.json"))
        os.makedirs(os.path.join(self.dir, "costs"), exist_ok=True)
        write(os.path.join(self.dir, "costs", "sessions.jsonl"),
              json.dumps({"v": 1, "ev": "start", "session": "s1", "regionBy": "refused", "refused": ["J1_REGION_A"]}) + "\n")
        _r, out, _e = self.run_cmd(pa.cmd_doctor, argparse.Namespace(space="sp1"))
        self.assertIn("J1_REGION_SECRET", out)
        self.assertRegex(out, r"1 launch record\(s\) with a refused .*J1_REGION_A")
        self.assertFalse([x for x in out.splitlines() if "PROBLEM" in x and ("cost" in x or "J1_" in x)], out)  # notes, never problems


# ---- t-032 J1c: summary, report, prices, billed compare and account-wide by facts and scope (R-P4, R-A3, R-A5, A-V1, A-V2) ----

class J1C_Summary(Collecting):
    """Two sessions under two scopes (global and regional, two accounts); one old session without facts."""

    def two_scopes(self, pricing=None):
        self.prices(scoped_fixture())
        prof = {"name": "test", "claude": ["true"], "codex": ["true"]}
        if pricing is not None:
            prof["pricing"] = pricing
        dump(prof, os.path.join(self.profile_dir, "profile.json"))
        self.ledger({"version": 1, "tasks": [{"id": "t-001", "state": "in-progress", "workers": []}]})
        self.record(session=SID, task="t-001", terminal="t5", name="t-001-build", model="global.model-a", provider="provider-a",
                    providerBy="launch", endpoint="global", endpointBy="rule", region=None, regionBy="unknown",
                    account="acct-a", accountBy="env")
        self.add(self.prompt(T0 + 10), self.call(T0 + 12, "g1", lines=1, out=1000000))
        self.record(session=SID2, task="t-001", terminal="t6", name="t-001-check", model="model-a", provider="provider-a",
                    providerBy="launch", endpoint="regional", endpointBy="rule", region="region-a", regionBy="machine",
                    account="acct-c", accountBy="env")
        self.add(self.prompt(T0 + 20, sid=SID2), self.call(T0 + 22, "r1", sid=SID2, lines=1, out=1000000), sid=SID2)
        self.collect(T0 + 5000)
        return load(self.costs("summary.json"))

    def test_j1c1_scopes_and_facts_in_the_summary(self):
        s = self.two_scopes()
        self.assertAlmostEqual(s["totals"]["money"], 10.0 + 11.0, places=4)
        self.assertEqual(s["scopes"], 2)
        scopes = {e["key"]: e for e in s["byScope"]}
        self.assertEqual(set(scopes), {"provider-a · global · ? · acct-a", "provider-a · regional · region-a · acct-c"})
        g = scopes["provider-a · global · ? · acct-a"]
        self.assertAlmostEqual(g["money"], 10.0, places=4)
        self.assertEqual(g["scope"], ["provider-a", "global", None, "acct-a"])
        self.assertEqual(g["pricedAs"], [{"scope": ["provider-a", "global", "*", "*"], "validFrom": "2026-01-01"}])
        r = scopes["provider-a · regional · region-a · acct-c"]
        self.assertEqual(r["pricedAs"], [{"scope": ["provider-a", "regional", "region-a", "*"], "validFrom": "2026-01-01"}])
        self.assertAlmostEqual(money_of(s["byScope"]), s["totals"]["money"], places=4)
        for key in ("byProvider", "byEndpoint", "byRegion", "byAccount"):
            self.assertAlmostEqual(money_of(s[key]), s["totals"]["money"], places=4, msg=key)
        self.assertEqual({e["key"] for e in s["byAccount"]}, {"acct-a", "acct-c"})
        self.assertEqual({e["key"] for e in s["byEndpoint"]}, {"global", "regional"})
        dm = {(e["endpoint"], e["account"]): e for e in s["byDayModel"]}
        self.assertEqual(set(dm), {("global", "acct-a"), ("regional", "acct-c")})
        self.assertEqual(dm[("regional", "acct-c")]["pricedAs"]["scope"], ["provider-a", "regional", "region-a", "*"])
        self.assertEqual(dm[("regional", "acct-c")]["region"], "region-a")
        sess = {e["key"]: e for e in s["sessions"]}
        self.assertEqual((sess[SID[:8]]["endpoint"], sess[SID2[:8]]["account"]), ("global", "acct-c"))

    def test_j1c2_unknown_region_and_unpriced_scope(self):
        self.prices(scoped_fixture())
        self.record(session=SID, model="model-a", provider="provider-a", providerBy="launch", endpoint="regional",
                    endpointBy="rule", region=None, regionBy="unknown", account="acct-a", accountBy="env")
        self.add(self.prompt(T0 + 10), self.call(T0 + 12, "u1", lines=1, out=1000))
        self.collect(T0 + 5000)
        s = load(self.costs("summary.json"))
        self.assertIsNone(s["totals"]["money"])
        e = s["byScope"][0]
        self.assertEqual((e["key"], e["scope"], e["pricedAs"], e["money"]), ("provider-a · regional · ? · acct-a", ["provider-a", "regional", None, "acct-a"], [], None))
        self.assertEqual(e["by"]["regionBy"], "unknown")
        self.assertIsNone(s["byDayModel"][0]["pricedAs"])

    def test_j1c3_old_rows_priced_by_profile_rules(self):
        pricing = {"defaultProvider": "provider-a", "providers": [{"program": "claude", "provider": "provider-a"}],
                   "endpoints": [{"model": "^global\\.", "endpoint": "global"}, {"model": "^model-", "endpoint": "regional"}]}
        self.prices(scoped_fixture())
        dump({"name": "test", "claude": ["true"], "codex": ["true"], "pricing": pricing}, os.path.join(self.profile_dir, "profile.json"))
        self.record(session=SID, model="global.model-a")  # a record from before J1: no facts
        self.add(self.prompt(T0 + 10), self.call(T0 + 12, "o1", lines=1, out=1000000))
        self.collect(T0 + 5000)
        s = load(self.costs("summary.json"))
        e = s["byScope"][0]
        self.assertEqual(e["scope"], ["provider-a", "global", None, "default"])
        self.assertEqual(e["by"], {"providerBy": "rule", "endpointBy": "rule", "regionBy": "unknown", "accountBy": "default"})
        self.assertAlmostEqual(s["totals"]["money"], 10.0, places=4)

    def test_j1c4_default_provider_rows_take_the_files_default_scope(self):
        # the profile names another default provider than the file: rows with providerBy default still price from the file's
        self.prices(scoped_fixture())
        dump({"name": "test", "claude": ["true"], "codex": ["true"], "pricing": {"defaultProvider": "provider-z"}},
             os.path.join(self.profile_dir, "profile.json"))
        self.record(session=SID, model="model-a")
        self.add(self.prompt(T0 + 10), self.call(T0 + 12, "d1", lines=1, out=1000000))
        self.collect(T0 + 5000)
        s = load(self.costs("summary.json"))
        self.assertAlmostEqual(s["totals"]["money"], 10.0, places=4)


class J1C_Report(J1C_Summary):
    def report(self, *args, **kw):
        _r, out, _e = self.run_cmd(pa_costs.report, self.dir, *args, now=T0 + 5000, **kw)
        return out

    def test_j1c5_report_by_each_fact_and_scope(self):
        self.two_scopes()
        for by, want in (("provider", "provider-a"), ("endpoint", "regional"), ("region", "region-a"), ("account", "acct-c"),
                         ("scope", "provider-a · regional · region-a · acct-c")):
            out = self.report(by=by)
            self.assertIn(want[:26], out, by)
        out = self.report(by="scope")
        self.assertIn("priced as provider-a · global · * · *", out)
        out = self.report()
        self.assertIn("By scope", out)  # more than one scope: shown in the default report

    def test_j1c6_one_scope_report_has_no_scope_section(self):
        self.prices()
        self.record(session=SID)
        self.add(self.prompt(T0 + 10), self.call(T0 + 12, "m1", lines=1, out=100))
        self.collect(T0 + 5000)
        self.assertNotIn("By scope", self.report())

    def test_j1c7_account_wide_per_entry_label(self):
        self.two_scopes()
        other = os.path.join(pa.ROOT, "spaces", "sp2", "costs", "summary.json")
        dump({"account": "acct-c", "updated": "2026-11-17T09:00:00Z",
              "byDayModel": [{"day": "2026-11-17", "model": "model-a", "tokens": toks(out=5), "money": 2.0}]}, other)
        here = pa_costs.account_wide_summary("acct-c", T0)
        self.assertEqual(here["spaces"], ["sp1", "sp2"])
        self.assertAlmostEqual(sum(e["money"] for e in here["byDayModel"]), 11.0 + 2.0, places=4)
        here = pa_costs.account_wide_summary("acct-a", T0)
        self.assertEqual(here["spaces"], ["sp1"])
        self.assertAlmostEqual(sum(e["money"] for e in here["byDayModel"]), 10.0, places=4)
        _r, out, _e = self.run_cmd(pa_costs.main, ["report", "--account-wide", "--account", "acct-c"])
        self.assertEqual(json.loads(out)["account"], "acct-c")
        with self.assertRaises(pa.Problem):  # the command line turns it into exit 2
            pa_costs.main(["report", "--account-wide", "--account", "12345" + "6789012"])


class J1C_Billed(J1C_Summary):
    def test_j1c8_billed_compare_per_account(self):
        s = self.two_scopes()
        table, _p = pa_costs.load_prices(pa.load_profile("test"))
        billed = [{"account": "acct-c", "from": "2026-11-17", "to": "2026-11-17", "model": "model-a", "amount": 12.0,
                   "currency": "USD", "final": True, "source": "q"}]
        here = pa_costs.account_wide_summary("acct-c", T0)
        c = pa_costs.billing_comparison(billed, dict(s, account="acct-c"), here, table)
        self.assertAlmostEqual(c["total"]["thisSpace"], 11.0, places=4)
        self.assertAlmostEqual(c["total"]["allSpaces"], 11.0, places=4)


class J1C_Prices(Collecting):
    def test_j1c9_prices_shows_scopes_notes_and_unpriced_per_facts(self):
        self.prices(scoped_fixture())
        self.record(session=SID, model="model-a", provider="provider-a", providerBy="launch", endpoint="regional",
                    endpointBy="rule", region="region-c", regionBy="machine", account="acct-a", accountBy="env")
        self.add(self.prompt(T0 + 10), self.call(T0 + 12, "p1", lines=1, out=10))
        self.collect(T0 + 5000)
        _r, out, _e = self.run_cmd(pa_costs.cmd_prices, self.dir)
        self.assertIn("default scope: provider provider-a · endpoint global · region * · account *", out)
        self.assertIn("provider provider-a · endpoint regional · region region-a · account *", out)
        self.assertIn("provider provider-b · endpoint * · region * · account *", out)
        self.assertIn("ignored by pa-costs from before t-032", out)
        self.assertIn("source made up", out)
        self.assertIn("Models seen without a price: model-a", out)
        self.assertIn("under provider-a · regional · region-c · acct-a: model-a", out)

    def test_j1c10_prices_of_a_file_without_scopes(self):
        self.prices()
        _r, out, _e = self.run_cmd(pa_costs.cmd_prices, self.dir)
        self.assertIn("default scope: provider ? · any endpoint, any region, any account (the file has no defaultScope)", out)
        self.assertNotIn("t-032", out)
        self.assertIn("Models seen without a price: (none)", out)


class J1C_AccountScope(Collecting):
    """R-A4 at the summary and in pa-costs prices (fix round 2): a session on acct-b after the account scope's validFrom
    is priced at the account's rate, not the general one."""

    def acct_b(self, fixture=None, model="model-a", out=1000000):
        self.prices(scoped_fixture() if fixture is None else fixture)
        self.record(session=SID, model=model, provider="provider-a", providerBy="launch", endpoint="global",
                    endpointBy="rule", region=None, regionBy="unknown", account="acct-b", accountBy="env")
        self.add(self.prompt(T0 + 10), self.call(T0 + 12, "b1", lines=1, out=out, model=model))
        self.collect(T0 + 5000)
        return load(self.costs("summary.json"))

    def test_j1c12_summary_prices_an_account_scope(self):
        s = self.acct_b()
        self.assertAlmostEqual(s["totals"]["money"], 5.0, places=4)  # the account's 5.0 per 1M out, not the default's 10.0
        e = {x["key"]: x for x in s["byScope"]}["provider-a · global · ? · acct-b"]
        self.assertAlmostEqual(e["money"], 5.0, places=4)
        self.assertEqual(e["pricedAs"], [{"scope": ["provider-a", "*", "*", "acct-b"], "validFrom": "2026-06-01"}])
        self.assertEqual(s["byDayModel"][0]["pricedAs"]["scope"], ["provider-a", "*", "*", "acct-b"])
        self.assertEqual({x["key"]: x for x in s["sessions"]}[SID[:8]]["pricedAs"]["scope"], ["provider-a", "*", "*", "acct-b"])
        self.assertAlmostEqual({x["key"]: x["money"] for x in s["byAccount"]}["acct-b"], 5.0, places=4)

    def test_j1c13_prices_finds_a_model_priced_only_in_an_account_scope(self):
        f = scoped_fixture()
        for e in f["scoped"]:
            if e.get("account") == "acct-b":
                e["models"]["model-d"] = [{"validFrom": "2026-06-01", "in": 0.5, "out": 5.0, "cw5m": 1.0, "cw1h": 2.0, "cr": 0.25}]
        s = self.acct_b(f, model="model-d")
        self.assertAlmostEqual(s["totals"]["money"], 5.0, places=4)
        self.assertEqual([r["model"] for r in self.rows()], ["model-d"])
        _r, out, _e = self.run_cmd(pa_costs.cmd_prices, self.dir)
        self.assertIn("Models seen without a price: (none)", out)


class J1R3_StartInEffect(Collecting):
    """Fix round 3 (verify Must fix 1, review RL-6): a row takes the facts of the start record in effect when it
    happened: the session's latest start record at or before the row's time, the first one when the row is earlier
    than all of them."""

    FACTS_A = {"provider": "provider-a", "providerBy": "launch", "endpoint": "global", "endpointBy": "rule", "region": None,
               "regionBy": "unknown", "account": "acct-a", "accountBy": "env"}
    FACTS_R = {"provider": "provider-a", "providerBy": "launch", "endpoint": "regional", "endpointBy": "rule",
               "region": "region-a", "regionBy": "machine", "account": "acct-c", "accountBy": "env"}

    def test_j1r1_fallback_rows_take_the_start_that_ran(self):
        # t-033's fallback: the refused attempt's record, then the record of the profile that started, same second
        self.prices(scoped_fixture())
        self.record(session=SID, model="model-a", **dict(self.FACTS_A, provider="provider-new", account="acct-new"))
        self.record(session=SID, model="model-a", **self.FACTS_A)
        self.add(self.prompt(T0 + 10), self.call(T0 + 12, "f1", lines=1, out=1000000))
        self.collect(T0 + 5000)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual({k: rows[0].get(k) for k in FACT_KEYS}, self.FACTS_A)
        s = load(self.costs("summary.json"))
        self.assertAlmostEqual(s["totals"]["money"], 10.0, places=4)  # priced, under provider-a's global scope
        self.assertEqual([e["key"] for e in s["byProvider"]], ["provider-a"])
        self.assertEqual([e["key"] for e in s["byAccount"]], ["acct-a"])
        self.assertEqual([e["key"] for e in s["byScope"]], ["provider-a · global · ? · acct-a"])

    def test_j1r2_resume_after_a_profile_edit(self):
        # rows before the resume under the first facts, rows after under the new; a resumed session's past (before
        # its first record) under the first
        self.prices(scoped_fixture())
        self.record(session=SID, at=iso(T0 + 100), model="model-a", resumed=True, **self.FACTS_A)
        self.add(self.prompt(T0 + 10), self.call(T0 + 12, "p1", lines=1, out=1000000))
        self.add(self.prompt(T0 + 200), self.call(T0 + 202, "a1", lines=1, out=1000000))
        self.record(session=SID, at=iso(T0 + 1000), model="model-a", resumed=True, **self.FACTS_R)
        self.add(self.prompt(T0 + 1010), self.call(T0 + 1012, "b1", lines=1, out=1000000))
        self.collect(T0 + 5000)
        rows = sorted(self.rows(), key=lambda r: r["from"])
        self.assertEqual(len(rows), 3)
        self.assertEqual([r["source"] for r in rows], ["reconstructed", "recorded", "recorded"])
        self.assertEqual([{k: r.get(k) for k in FACT_KEYS} for r in rows], [self.FACTS_A, self.FACTS_A, self.FACTS_R])
        s = load(self.costs("summary.json"))
        self.assertAlmostEqual(s["totals"]["money"], 10.0 + 10.0 + 11.0, places=4)
        scopes = {e["key"]: e["money"] for e in s["byScope"]}
        self.assertAlmostEqual(scopes["provider-a · global · ? · acct-a"], 20.0, places=4)
        self.assertAlmostEqual(scopes["provider-a · regional · region-a · acct-c"], 11.0, places=4)

    def test_j1r3_make_row_picks_the_start_in_effect(self):
        a = dict(self.FACTS_A, ev="start", session=SID, at=iso(T0 + 5))
        b = dict(self.FACTS_R, ev="start", session=SID, at=iso(T0 + 50))
        c = dict(self.FACTS_A, ev="start", session=SID, at=iso(T0 + 50), account="acct-b")  # same second, later line
        sess = pa_costs.sessions_of([a, b, c])[SID]
        def facts(t):
            seg = pa_costs.new_segment(isoms(t), isoms(t), "model-a")
            row = pa_costs.make_row(sess, seg)
            return {k: row.get(k) for k in FACT_KEYS}
        self.assertEqual(facts(T0 + 1), self.FACTS_A)  # earlier than all: the first
        self.assertEqual(facts(T0 + 5), self.FACTS_A)  # at the record's second (a call's time has milliseconds)
        self.assertEqual(facts(T0 + 49.9), self.FACTS_A)
        self.assertEqual(facts(T0 + 50.2), dict(self.FACTS_A, account="acct-b"))  # the later of two in one second
        self.assertEqual(facts(T0 + 9000), dict(self.FACTS_A, account="acct-b"))
        one = pa_costs.sessions_of([b])[SID]  # old data with one start record: its facts for every row
        for t in (T0, T0 + 60, T0 + 9000):
            row = pa_costs.make_row(one, pa_costs.new_segment(isoms(t), isoms(t), "model-a"))
            self.assertEqual({k: row.get(k) for k in FACT_KEYS}, self.FACTS_R)
        bare = pa_costs.sessions_of([{"ev": "start", "session": SID, "at": iso(T0)},
                                     dict(self.FACTS_R, ev="start", session=SID, at=iso(T0 + 50))])[SID]
        row = pa_costs.make_row(bare, pa_costs.new_segment(isoms(T0 + 10), isoms(T0 + 10), "model-a"))
        for k in FACT_KEYS:  # a start record without facts gives a row without fact keys, as before
            self.assertNotIn(k, row)


class J1D_Limits(Collecting):
    """t-032 J1d, the d-117 limits fixed with the Costs view: 2 (one account label per row), 7 (the rule fallback reads
    the model of the start record in effect), 8 (a segment open across a later start record is split there), 9 (a
    session's entry shows its largest scope)."""

    FACTS_A = J1R3_StartInEffect.FACTS_A
    FACTS_R = J1R3_StartInEffect.FACTS_R

    def machine_account(self, label):
        dump({"version": 1, "defaultProfile": "test", "costs": {"account": label}}, os.path.join(pa.ROOT, "config.json"))

    def test_j1d1_one_label_per_row(self):
        # limit 2: a row whose account came from the chain at launch follows today's chain everywhere: the account
        # bucket, the scope, the account-scoped price, the day drill, byDayModel and the account-wide sum
        self.prices(scoped_fixture())
        self.record(session=SID, model="model-a", **dict(self.FACTS_A, account="acct-old", accountBy="machine"))
        self.record(session=SID2, model="model-a", **dict(self.FACTS_A, account="acct-e", accountBy="env"))
        self.add(self.prompt(T0 + 10), self.call(T0 + 12, "d1", lines=1, out=1000000))
        self.add(self.prompt(T0 + 20, sid=SID2), self.call(T0 + 22, "e1", sid=SID2, lines=1, out=1000000), sid=SID2)
        self.machine_account("acct-b")  # changed after the launch
        self.collect(T0 + 5000)
        s = load(self.costs("summary.json"))
        self.assertEqual(s["account"], "acct-b")
        acc = {e["key"]: e["money"] for e in s["byAccount"]}
        self.assertEqual(set(acc), {"acct-b", "acct-e"})
        self.assertAlmostEqual(acc["acct-b"], 5.0, places=4)  # the acct-b scope's 5.0 per 1M out
        self.assertAlmostEqual(acc["acct-e"], 10.0, places=4)  # a launch-recorded label (env) keeps its own
        scopes = {e["key"]: e["money"] for e in s["byScope"]}
        self.assertAlmostEqual(scopes["provider-a · global · ? · acct-b"], 5.0, places=4)
        self.assertNotIn("provider-a · global · ? · acct-old", scopes)
        self.assertAlmostEqual(s["totals"]["money"], 15.0, places=4)
        day = s["byDay"][0]
        self.assertAlmostEqual(sum(a["money"] for a in day["activities"]), 15.0, places=4)
        dm = {(e["account"], e["accountBy"]): e["money"] for e in s["byDayModel"]}
        self.assertEqual(set(dm), {("acct-b", "machine"), ("acct-e", "env")})
        self.assertAlmostEqual(dm[("acct-b", "machine")], 5.0, places=4)
        _r, out, _e = self.run_cmd(pa_costs.report, self.dir, account_wide=True, account="acct-b", now=T0 + 5000)
        wide = json.loads(out)
        self.assertAlmostEqual(sum(e["money"] for e in wide["byDayModel"]), 5.0, places=4)

    def test_j1d1b_row_facts_account_rule(self):
        chain = ("acct-now", "machine")
        base = {"session": SID, "model": "model-a", "program": "claude", "provider": "provider-a", "providerBy": "launch"}
        for by in ("space", "machine", "profile", "default", None):
            f = pa_costs.row_facts(dict(base, account="acct-then", accountBy=by), {}, None, chain)
            self.assertEqual((f["account"], f["accountBy"]), chain, by)
        for by in ("env", "rule"):
            f = pa_costs.row_facts(dict(base, account="acct-then", accountBy=by), {}, None, chain)
            self.assertEqual((f["account"], f["accountBy"]), ("acct-then", by))

    def test_j1d2_rule_fallback_reads_the_start_in_effect(self):
        # limit 7 (review-j1-r4/probe.py): after a fallback the started record (m-held) has no provider or endpoint;
        # the current rules match only the refused launch's model (m-fresh): the row gets the defaults, not them
        rules, problems = pa_costs.pricing_rules({"pricing": {"providers": [{"program": "claude", "model": "m-fresh", "provider": "provider-fresh"}],
                                                              "endpoints": [{"model": "m-fresh", "endpoint": "edge"}]}})
        self.assertEqual(problems, [])
        none = {"provider": None, "providerBy": "unknown", "endpoint": None, "endpointBy": "default", "region": None,
                "regionBy": "unknown", "account": "default", "accountBy": "default"}
        a = dict(none, ev="start", session=SID, at=iso(T0), model="m-fresh", program="claude")
        b = dict(none, ev="start", session=SID, at=iso(T0), model="m-held", program="claude")
        sess = pa_costs.sessions_of([a, b])[SID]
        row = pa_costs.make_row(sess, pa_costs.new_segment(isoms(T0 + 5), isoms(T0 + 5), "m-held"))
        f = pa_costs.row_facts(row, sess, rules, ("default", "default"))
        self.assertEqual((f["provider"], f["providerBy"], f["endpoint"], f["endpointBy"]), (None, "default", None, "default"))
        # and the other way: the record in effect's model matches, the first's does not
        sess = pa_costs.sessions_of([b, dict(a, at=iso(T0 + 100))])[SID]
        row = pa_costs.make_row(sess, pa_costs.new_segment(isoms(T0 + 200), isoms(T0 + 200), "m-fresh"))
        f = pa_costs.row_facts(row, sess, rules, ("default", "default"))
        self.assertEqual((f["provider"], f["providerBy"], f["endpoint"], f["endpointBy"]), ("provider-fresh", "rule", "edge", "rule"))

    def facts_of(self, rows):
        return [{k: r.get(k) for k in FACT_KEYS} for r in sorted(rows, key=lambda r: r["from"])]

    def test_j1d3_segment_split_at_a_later_start(self):
        # limit 8: no prompt between the calls, a start record between them: two rows, one under each record
        self.prices(scoped_fixture())
        self.record(session=SID, model="model-a", **self.FACTS_A)
        self.record(session=SID, at=iso(T0 + 15), model="model-a", resumed=True, **self.FACTS_R)
        self.add(self.prompt(T0 + 10), self.call(T0 + 12, "s1", lines=1, out=1000000), self.call(T0 + 20, "s2", lines=1, out=1000000))
        self.collect(T0 + 5000)
        rows = self.rows()
        self.assertEqual(self.facts_of(rows), [self.FACTS_A, self.FACTS_R])
        self.assertEqual(sum(r["tokens"]["out"] for r in rows), 2000000)
        s = load(self.costs("summary.json"))
        self.assertAlmostEqual(s["totals"]["money"], 10.0 + 11.0, places=4)

    def test_j1d3b_segment_open_across_collects(self):
        # the same, with the segment still open in state.json when the second record is written
        self.prices(scoped_fixture())
        self.record(session=SID, model="model-a", **self.FACTS_A)
        self.add(self.prompt(T0 + 10), self.call(T0 + 12, "o1", lines=1, out=1000000))
        self.collect(T0 + 13)
        self.assertEqual(self.rows(), [])  # still open
        self.record(session=SID, at=iso(T0 + 15), model="model-a", resumed=True, **self.FACTS_R)
        self.add(self.call(T0 + 20, "o2", lines=1, out=1000000))
        self.collect(T0 + 5000)
        rows = self.rows()
        self.assertEqual(self.facts_of(rows), [self.FACTS_A, self.FACTS_R])
        self.assertEqual(sum(r["tokens"]["out"] for r in rows), 2000000)

    def test_j1d4_session_entry_shows_its_largest_scope(self):
        # limit 9: rows in two scopes, the larger first: the entry names the larger and says how many
        self.prices(scoped_fixture())
        self.record(session=SID, model="model-a", **self.FACTS_A)
        self.add(self.prompt(T0 + 10), self.call(T0 + 12, "l1", lines=1, out=3000000))
        self.record(session=SID, at=iso(T0 + 1000), model="model-a", resumed=True, **self.FACTS_R)
        self.add(self.prompt(T0 + 1010), self.call(T0 + 1012, "l2", lines=1, out=1000000))
        self.record(session=SID2, model="model-a", **self.FACTS_A)
        self.add(self.prompt(T0 + 20, sid=SID2), self.call(T0 + 22, "l3", sid=SID2, lines=1, out=1000000), sid=SID2)
        self.collect(T0 + 5000)
        s = load(self.costs("summary.json"))
        e = {x["key"]: x for x in s["sessions"]}
        one, two = e[SID[:8]], e[SID2[:8]]
        self.assertEqual([one[k] for k in ("provider", "endpoint", "region", "account")], ["provider-a", "global", None, "acct-a"])
        self.assertEqual(one["pricedAs"]["scope"], ["provider-a", "global", "*", "*"])
        self.assertEqual(one["scopes"], 2)
        self.assertAlmostEqual(one["money"], 30.0 + 11.0, places=4)
        self.assertNotIn("scopes", two)


class J1C_Docs(unittest.TestCase):
    def test_j1c11_readme_names_the_scope_format_and_facts(self):
        with open(os.path.join(HERE, "..", "README.md")) as f:
            readme = f.read()
        for want in ("defaultScope", "scoped", "scopedAliases", "PA_COST_ACCOUNT", "PA_COST_REGION", "costs.accountRules",
                     "costs.region", "pricing.providers", "pricing.endpoints", "--by scope", "--account", "billing.account"):
            self.assertIn(want, readme)


D1 = "2026-11-17"  # T0's UTC day
D0 = "2026-11-16"


class PX_PaContext(Collecting):
    """t-041 J1 (R15, R16 readers): the Project Agent's context per day: segment peak and first call, summary
    paContext, the PA context line under Today, --by context, the reset and compact records read."""

    def put_rows(self, *rows):
        """Rows written straight to turns.jsonl (as the collector would), each with a seq."""
        os.makedirs(os.path.join(self.dir, "costs"), exist_ok=True)
        with open(self.costs("turns.jsonl"), "a") as f:
            for i, r in enumerate(rows):
                base = {"v": 1, "seq": i + 1, "session": SID, "program": "claude", "kind": "pa", "model": "model-a",
                        "from": D1 + "T01:00:00Z", "to": D1 + "T01:01:00Z", "day": D1, "calls": 1, "tokens": toks(),
                        "long": toks(), "rebuild": {"calls": 0, "cw5m": 0, "cw1h": 0, "gapS": 0}, "turn": D1 + "T01:00:00Z",
                        "task": None, "pass": None, "trigger": None, "source": "recorded"}
                base.update(r)
                f.write(json.dumps(base) + "\n")

    def put_record(self, rec):
        os.makedirs(os.path.join(self.dir, "costs"), exist_ok=True)
        with open(self.costs("sessions.jsonl"), "a") as f:
            f.write(json.dumps(rec) + "\n")

    def summary(self, now=T0 + 5000):
        return pa_costs.write_summary(self.dir, now=now)

    def day(self, s, day=D1):
        found = [e for e in s["paContext"] if e["day"] == day]
        self.assertEqual(len(found), 1, s["paContext"])
        return found[0]

    def test_px1_peak_first_and_mean_of_one_segment(self):
        self.record(session=SID, kind="pa", terminal="t0", parent=None)
        self.add(self.prompt(T0 + 10), self.call(T0 + 12, "p1", lines=1, inp=0, cr=100000),
                 self.call(T0 + 14, "p2", lines=1, inp=0, cr=300000), self.call(T0 + 16, "p3", lines=1, inp=0, cr=200000))
        self.collect(T0 + 5000)
        rows = self.rows()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual((rows[0]["peak"], rows[0]["first"]), (300000, 100000))
        e = self.day(load(self.costs("summary.json")))
        self.assertEqual((e["calls"], e["mean"], e["peak"], e["peakExact"]), (3, 200000, 300000, True))
        self.assertEqual((e["firstCall"], e["firstCalls"]), (100000, 1))  # a start record with resumed false: fresh

    def test_px2_old_row_without_peak_is_a_lower_bound(self):
        self.put_rows({"calls": 2, "tokens": toks(cr=400000)}, {"calls": 1, "tokens": toks(cr=250000), "peak": 250000})
        s = self.summary()
        e = self.day(s)
        self.assertEqual((e["calls"], e["mean"], e["peak"], e["peakExact"]), (3, 216667, 250000, False))
        line = [x for x in pa_costs.report_lines(s) if x.startswith("PA context")]
        self.assertEqual(len(line), 1)
        self.assertIn("peak ≥250k", line[0])
        self.assertIn("mean 217k", line[0])

    def test_px3_reset_and_compact_records_are_counted(self):
        self.record(session=SID2, kind="worker", terminal="t5")
        self.put_rows({"calls": 1, "tokens": toks(cr=100000), "peak": 100000})
        for rec in (fx.reset_record(D1 + "T02:00:00Z", SID, SID3, reason="pass-cap"),
                    fx.reset_record(D1 + "T03:00:00Z", SID3, SID4, reason="task-cap"),
                    fx.reset_record("not a time", SID4, "x", reason="person"),
                    fx.compact_record(D1 + "T04:00:00Z", SID4),
                    fx.compact_record(D1 + "T04:30:00Z", SID2),  # a worker session's: not the PA's
                    fx.compact_record(D0 + "T04:00:00Z", SID)):
            self.put_record(rec)
        s = self.summary()
        e = self.day(s)
        self.assertEqual(e["resets"], {"fresh": 2, "compact": 1})
        self.assertEqual(e["reasons"], {"pass-cap": 1, "task-cap": 1})
        self.assertEqual(self.day(s, D0)["resets"], {"fresh": 0, "compact": 1})  # a day with records only
        line = [x for x in pa_costs.report_lines(s) if x.startswith("PA context")][0]
        self.assertIn("resets 3 (fresh 2, compact 1)", line)
        self.assertIn("peak 100k", line)
        self.assertNotIn("≥", line)

    def test_px4_only_the_pa_main_conversation_counts(self):
        self.put_rows({"calls": 1, "tokens": toks(cr=100000), "peak": 100000},
                      {"calls": 1, "tokens": toks(cr=900000), "peak": 900000, "sub": "agent-x"},
                      {"calls": 1, "tokens": toks(cr=800000), "peak": 800000, "kind": "worker", "session": SID2},
                      {"calls": 0, "tokens": toks(out=5)})
        e = self.day(self.summary())
        self.assertEqual((e["calls"], e["mean"], e["peak"]), (1, 100000, 100000))

    def test_px5_since_and_until_drop_rows_and_records_outside(self):
        self.put_rows({"calls": 1, "tokens": toks(cr=100000), "peak": 100000},
                      {"calls": 1, "tokens": toks(cr=500000), "peak": 500000, "day": D0, "from": D0 + "T01:00:00Z", "to": D0 + "T01:01:00Z"})
        self.put_record(fx.reset_record(D0 + "T02:00:00Z", SID, SID3))
        self.put_record(fx.reset_record(D1 + "T02:00:00Z", SID3, SID4))
        _r, out, _e = self.run_cmd(pa_costs.report, self.dir, since=D1, as_json=True, now=T0 + 5000)
        pc = json.loads(out)["paContext"]
        self.assertEqual([e["day"] for e in pc], [D1])
        self.assertEqual(pc[0]["resets"]["fresh"], 1)
        _r, out, _e = self.run_cmd(pa_costs.report, self.dir, until=D0, as_json=True, now=T0 + 5000)
        pc = json.loads(out)["paContext"]
        self.assertEqual([(e["day"], e["mean"], e["resets"]["fresh"]) for e in pc], [(D0, 500000, 1)])

    def test_px6_first_call_of_fresh_sessions_only(self):
        self.record(session=SID, kind="pa", terminal="t0", parent=None, resumed=False)
        self.record(session=SID2, kind="pa", terminal="t0", parent=None, resumed=True)
        self.put_record(fx.reset_record(D1 + "T05:00:00Z", SID2, SID3))
        self.put_rows({"session": SID, "first": 100000, "peak": 100000, "tokens": toks(cr=100000)},
                      {"session": SID, "first": 999, "peak": 999, "tokens": toks(cr=999), "from": D1 + "T02:00:00Z"},
                      {"session": SID2, "first": 500000, "peak": 500000, "tokens": toks(cr=500000)},
                      {"session": SID3, "first": 80000, "peak": 80000, "tokens": toks(cr=80000), "from": D1 + "T05:01:00Z"},
                      {"session": SID4, "tokens": toks(cr=70000), "day": D0, "from": D0 + "T05:00:00Z", "to": D0 + "T05:01:00Z"})
        self.record(session=SID4, kind="pa", terminal="t0", parent=None, resumed=False)
        s = self.summary()
        e = self.day(s)
        self.assertEqual((e["firstCall"], e["firstCalls"]), (90000, 2))
        e0 = self.day(s, D0)  # a fresh session whose rows predate `first`: no guess
        self.assertEqual((e0["firstCall"], e0["firstCalls"]), (None, 0))

    def test_px7_money_and_per_pass(self):
        self.prices()
        self.put_rows({"pass": 1, "tokens": toks(cr=100000, out=1000), "peak": 100000},
                      {"pass": 2, "tokens": toks(cr=200000, out=2000), "peak": 200000},
                      {"pass": 2, "tokens": toks(cr=200000, out=2000), "peak": 200000})
        s = self.summary()
        e = self.day(s)
        pa_money = [k for k in s["byKind"]["kind"] if k["key"] == "pa"][0]["money"]
        self.assertGreater(pa_money, 0)
        self.assertAlmostEqual(e["money"], pa_money, places=5)
        self.assertEqual(e["passes"], 2)
        self.assertAlmostEqual(e["perPass"], pa_money / 2, places=5)
        self.ledger({"version": 1, "tasks": []})
        os.remove(os.path.join(self.profile_dir, "prices.json"))
        e = self.day(self.summary())
        self.assertEqual((e["money"], e["perPass"]), (None, None))

    def test_px8_by_context_report(self):
        self.assertIn("context", pa_costs.REPORT_BY)
        self.prices()
        self.put_rows({"pass": 1, "calls": 2, "tokens": toks(cr=400000), "peak": 300000},
                      {"pass": 1, "calls": 1, "tokens": toks(cr=500000), "day": D0, "from": D0 + "T01:00:00Z", "to": D0 + "T01:01:00Z"})
        self.put_record(fx.reset_record(D1 + "T02:00:00Z", SID, SID3))
        s = self.summary()
        out = pa_costs.report_lines(s, "context")
        head = [x for x in out if "first call" in x]
        self.assertEqual(len(head), 1, out)
        self.assertIn("(UTC)", head[0])
        for want in ("calls", "mean", "peak", "resets", "est.", "est./pass"):
            self.assertIn(want, head[0])
        d1 = [x for x in out if x.strip().startswith(D1)][0]
        self.assertEqual(d1.split()[:5], [D1, "2", "200k", "300k", "1"])
        d0 = [x for x in out if x.strip().startswith(D0)][0]
        self.assertIn("≥500k", d0)
        self.assertTrue(any("≥ marks a lower bound" in x for x in out), out)
        self.assertFalse(any(x.startswith("By activity") for x in out))
        self.assertFalse([x for x in pa_costs.report_lines(s, "day") if "first call" in x])
        _r, out, _e = self.run_cmd(pa_costs.report, self.dir, by="context", now=T0 + 5000)
        self.assertIn("first call", out)

    def test_px9_no_pa_calls_today(self):
        s = self.summary()
        self.assertEqual(s["paContext"], [])
        line = [x for x in pa_costs.report_lines(s) if x.startswith("PA context")]
        self.assertEqual(line, ["PA context  — (no Project Agent calls today)"])

    def test_px10_json_round_trip_and_shrink(self):
        self.put_rows({"calls": 1, "tokens": toks(cr=100000), "peak": 100000})
        s = self.summary()
        self.assertEqual(json.loads(pa_costs.compact(s))["paContext"], s["paContext"])
        self.assertEqual(load(self.costs("summary.json"))["paContext"], s["paContext"])

    @staticmethod
    def pc_days(n, pad):
        return [{"day": "d{:05d}".format(9999 - i), "calls": 1, "mean": 1, "peak": 1, "peakExact": True,
                 "resets": {"fresh": 0, "compact": 0}, "firstCall": None, "firstCalls": 0, "money": None,
                 "passes": 0, "perPass": None, "pad": "x" * pad} for i in range(n)]

    def test_px10b_summary_keeps_14_days_cut_before_any_other_list(self):
        """t-041 J1 round 2 (REVIEW MF-2): summary.json keeps at most 14 days of paContext, cut before shrink touches
        the Sessions list, the per-task sessions, the passes, byTask or byDayModel."""
        self.assertEqual(pa_costs.PA_CONTEXT_SUMMARY_DAYS, 14)
        sessions = [{"session": "s{:04d}".format(i), "pad": "y" * 150} for i in range(150)]
        passes = [{"pass": i, "pad": "z" * 150} for i in range(20)]
        by_task = [{"key": "t-{:03d}".format(i), "tokens": 1, "money": None, "calls": 1, "sessions": [{"session": "s"}]}
                   for i in range(5)]
        big = {"sessions": list(sessions), "byTask": json.loads(json.dumps(by_task)), "passes": list(passes),
               "byDayModel": [{"day": "d", "pad": "w" * 100}], "paContext": self.pc_days(92, 300)}
        self.assertGreater(len(pa_costs.compact(big).encode()), pa_costs.SUMMARY_MAX)  # over the cap with 92 days
        newest = big["paContext"][:14]
        text = pa_costs.fit_summary(big)
        self.assertLessEqual(len(text.encode()), pa_costs.SUMMARY_MAX)
        self.assertEqual(json.loads(text), big)
        self.assertEqual(big["paContext"], newest)
        self.assertEqual((big["sessions"], big["passes"], big["byTask"]), (sessions, passes, by_task))
        self.assertNotIn("truncated", big)  # the 14-day cut is the summary's form, not a truncation
        small = {"sessions": [], "byTask": [], "passes": [], "byDayModel": [], "paContext": self.pc_days(30, 10)}
        pa_costs.fit_summary(small)
        self.assertEqual(len(small["paContext"]), 14)  # under the cap too: summary.json keeps at most 14 days

    @staticmethod
    def shrink_only(s):
        """The summary without paContext, fitted by shrink alone: cf0de90's write_summary loop."""
        s.pop("paContext", None)
        text = pa_costs.compact(s)
        while len(text.encode()) > pa_costs.SUMMARY_MAX and pa_costs.shrink(s):
            text = pa_costs.compact(s)
        return text

    def assert_newest_days_that_fit(self, s, days):
        """s["paContext"] is the newest days of `days` that fit in the room the rest left: all of them, or one more
        would go over the cap."""
        kept = s["paContext"]
        self.assertEqual(kept, days[:len(kept)])
        self.assertLessEqual(len(pa_costs.compact(s).encode()), pa_costs.SUMMARY_MAX)
        if len(kept) < len(days):
            self.assertGreater(len(pa_costs.compact(dict(s, paContext=days[:len(kept) + 1])).encode()), pa_costs.SUMMARY_MAX)

    def test_px10c_days_that_do_not_fit_cause_no_shrink_step(self):
        """t-041 J1 round 4 (REVIEW RL-4; replaces round 3's px10c, which pinned the cut to one day): when the 14 days
        do not fit, paContext is set aside, the rest is fitted as without it, and as many of the newest days as fit
        in the room left are put back. Here the rest fits as it is: no shrink step, no truncated, several days kept."""
        sessions = [{"session": "s{:04d}".format(i), "pad": "y" * 100} for i in range(10)]
        days = self.pc_days(14, 6000)
        big = {"sessions": list(sessions), "byTask": [], "passes": [{"pass": 1}], "byDayModel": [],
               "paContext": list(days)}
        text = pa_costs.fit_summary(big)
        self.assertEqual(json.loads(text), big)
        self.assertEqual((big["sessions"], big["passes"]), (sessions, [{"pass": 1}]))  # no shrink step ran
        self.assertNotIn("truncated", big)
        self.assertGreater(len(big["paContext"]), 1)  # round 3 kept one day here
        self.assert_newest_days_that_fit(big, days)

    def test_px10d_no_day_fits_and_paContext_is_empty(self):
        """t-041 J1 round 4 (replaces round 3's px10d, where the existing steps ran before the last day went): a day
        that does not fit causes no shrink step; paContext is [] and nothing is marked truncated."""
        sessions = [{"session": "s", "pad": "y" * 100}]
        big = {"sessions": list(sessions), "byTask": [], "passes": [{"pass": 1}], "byDayModel": [],
               "paContext": self.pc_days(14, 70000)}
        text = pa_costs.fit_summary(big)
        self.assertLessEqual(len(text.encode()), pa_costs.SUMMARY_MAX)
        self.assertEqual((big["sessions"], big["passes"]), (sessions, [{"pass": 1}]))
        self.assertEqual(big["paContext"], [])
        self.assertNotIn("truncated", big)
        fits = {"sessions": [], "byTask": [], "passes": [], "byDayModel": [], "paContext": self.pc_days(14, 60000)}
        pa_costs.fit_summary(fits)
        self.assertEqual(len(fits["paContext"]), 1)  # one day that fits is kept

    def test_px10e_the_rest_is_shrunk_as_without_paContext(self):
        """t-041 J1 round 4: when the rest is over the cap by itself, shrink runs on it exactly as without paContext
        (same steps, same truncated), then the newest days that fit in the room left go back, in paContext's place."""
        for n_sessions, pad, day_pad in ((400, 200, 1500), (330, 200, 1500), (300, 210, 150)):  # 8, 5, 14 days go back
            sessions = [{"session": "s{:04d}".format(i), "pad": "y" * pad} for i in range(n_sessions)]
            days = self.pc_days(14, day_pad)
            big = {"sessions": list(sessions), "byTask": [], "passes": [{"pass": 1}], "byDayModel": [],
                   "paContext": list(days), "unmatched": {}}
            ref = json.loads(json.dumps(big))
            self.shrink_only(ref)
            text = pa_costs.fit_summary(big)
            self.assertEqual({k: v for k, v in big.items() if k != "paContext"}, ref, n_sessions)
            self.assertEqual(json.loads(text), big)
            self.assertEqual(list(big), ["sessions", "byTask", "passes", "byDayModel", "paContext", "unmatched"]
                             + (["truncated"] if "truncated" in big else []))  # paContext keeps its place
            self.assert_newest_days_that_fit(big, days)

    def test_px10f_not_even_an_empty_list_fits(self):
        """t-041 J1 round 4: when the rest, fitted as without paContext, leaves less room than an empty paContext
        needs, the key is left out: summary.json is byte for byte the shrink-only fit (as a summary before J1)."""
        base = {"sessions": [], "byTask": [], "passes": [], "byDayModel": [], "pad": ""}
        room = pa_costs.SUMMARY_MAX - len(pa_costs.compact(base).encode())
        base["pad"] = "x" * (room - 5)  # 5 bytes left: ',"paContext":[]' is 15
        big = dict(base, paContext=self.pc_days(14, 10))
        ref = dict(base)
        ref_text = self.shrink_only(ref)
        text = pa_costs.fit_summary(big)
        self.assertEqual(text, ref_text)
        self.assertNotIn("paContext", big)
        self.assertNotIn("truncated", big)

    def test_px11_days_are_capped(self):
        rows = []
        for i in range(100):
            d = time.strftime("%Y-%m-%d", time.gmtime(T0 - i * 86400))
            rows.append({"day": d, "from": d + "T01:00:00Z", "to": d + "T01:01:00Z", "tokens": toks(cr=1000), "peak": 1000})
        self.put_rows(*rows)
        pc = self.summary()["paContext"]
        self.assertEqual(len(pc), pa_costs.PA_CONTEXT_SUMMARY_DAYS)  # summary.json: 14 days (round 2, MF-2)
        self.assertEqual(pc[0]["day"], D1)  # newest first
        self.assertEqual(load(self.costs("summary.json"))["paContext"], pc)
        _r, out, _e = self.run_cmd(pa_costs.report, self.dir, as_json=True, now=T0 + 5000)
        pc = json.loads(out)["paContext"]
        self.assertEqual(len(pc), pa_costs.DAYS)  # report summarizes without the summary's cut
        self.assertEqual(pc[0]["day"], D1)

    def test_px14_first_call_is_the_sessions_first_row_of_all(self):
        """t-041 J1 round 2 (REVIEW MF-1, review_j1_probe P1): a fresh session that starts before --since has its
        first row cut; its first row inside the window is a mid-session prompt and is not its first call."""
        self.put_record(fx.reset_record(D0 + "T23:50:00Z", SID, SID3))
        self.put_record(fx.reset_record(D1 + "T05:00:00Z", SID3, SID4))
        self.put_rows({"session": SID3, "calls": 2, "tokens": toks(cr=160000), "peak": 80000, "first": 80000, "day": D0,
                       "from": D0 + "T23:51:00Z", "to": D0 + "T23:59:00Z"},
                      {"session": SID3, "calls": 3, "tokens": toks(cr=900000), "peak": 310000, "first": 290000,
                       "from": D1 + "T00:00:10Z", "to": D1 + "T00:30:00Z"},
                      {"session": SID4, "calls": 1, "tokens": toks(cr=70000), "peak": 70000, "first": 70000,
                       "from": D1 + "T05:01:00Z", "to": D1 + "T05:02:00Z"})
        s = self.summary()
        self.assertEqual((self.day(s, D0)["firstCall"], self.day(s, D0)["firstCalls"]), (80000, 1))
        self.assertEqual((self.day(s)["firstCall"], self.day(s)["firstCalls"]), (70000, 1))
        for kw in ({"since": D1}, {"since": D1, "until": D1}):
            _r, out, _e = self.run_cmd(pa_costs.report, self.dir, as_json=True, now=T0 + 5000, **kw)
            pc = json.loads(out)["paContext"]
            self.assertEqual([(e["day"], e["firstCall"], e["firstCalls"]) for e in pc], [(D1, 70000, 1)], kw)
        _r, out, _e = self.run_cmd(pa_costs.report, self.dir, until=D0, as_json=True, now=T0 + 5000)
        pc = json.loads(out)["paContext"]
        self.assertEqual([(e["day"], e["firstCall"], e["firstCalls"]) for e in pc], [(D0, 80000, 1)])
        _r, out, _e = self.run_cmd(pa_costs.report, self.dir, by="context", since=D1, now=T0 + 5000)
        self.assertNotIn("290k", out)

    def test_px15_a_record_with_a_non_string_field_is_skipped(self):
        """t-041 J1 round 2 (VERIFY recorded limit 1): a reset or compact record whose session, at, to or from is not a
        string is skipped, like one with a bad at; the summary is still written."""
        self.put_rows({"calls": 1, "tokens": toks(cr=100000), "peak": 100000})
        good = [fx.reset_record(D1 + "T02:00:00Z", SID, SID3), fx.compact_record(D1 + "T04:00:00Z", SID3)]
        bad = [fx.compact_record(D1 + "T04:00:00Z", ["a", "b"]), fx.compact_record(D1 + "T04:00:00Z", {"x": 1}),
               fx.compact_record(D1 + "T04:00:00Z", 7), fx.compact_record(12345, SID3),
               fx.reset_record(D1 + "T02:00:00Z", SID, ["l"]), fx.reset_record(D1 + "T02:00:00Z", {"o": 1}, SID4),
               fx.reset_record(D1 + "T02:00:00Z", 3, SID4), fx.reset_record(["t"], SID, SID4),
               dict(fx.reset_record(D1 + "T02:00:00Z", SID, SID4), session=["s"])]
        for rec in bad + good:
            self.put_record(rec)
        s = self.summary()
        e = self.day(s)
        self.assertEqual(e["resets"], {"fresh": 1, "compact": 1})
        self.assertEqual(load(self.costs("summary.json"))["paContext"], s["paContext"])
        _r, out, _e = self.run_cmd(pa_costs.report, self.dir, by="context", now=T0 + 5000)
        self.assertIn(D1, out)

    def test_px12_old_open_segment_without_peak(self):
        seg = pa_costs.new_segment(D1 + "T01:00:00Z", D1 + "T01:00:00Z", "model-a")
        self.assertEqual((seg["peak"], seg["first"]), (0, None))
        for k in ("peak", "first"):
            seg.pop(k)
        seg["calls"] = 1
        row = pa_costs.make_row({"session": SID, "kind": "pa"}, seg)
        self.assertNotIn("peak", row)
        self.assertNotIn("first", row)

    def test_px16_near_the_cap_the_rest_of_the_summary_is_as_before_j1(self):
        """t-041 J1 rounds 3 and 4 (REVIEW MF-3, RL-4; review_j1r2_alt_order.sh, review_j1r3_probe.sh's sweep): K worker
        sessions plus N Project Agent days. Every part of summary.json other than paContext, truncated included,
        equals what cf0de90 wrote: the same summary without paContext fitted by shrink alone (cf0de90's
        write_summary loop; shrink is cf0de90's). The grid: K = 150, 165, 175, 200 at N = 20, 40, 50, 60, 80, and the
        reviewer's sweep K = 100..259 step 3 at N = 60 and 80, which holds K = 154 and 244..256 at N = 80 (round 3 cut
        the Sessions list there: 155 -> 116 sessions and truncated). paContext is the newest days that fit in the
        room left, or left out where not even [] fits (K = 154, N = 80); 'pa-costs report' keeps all N days."""
        cases = [(k, n) for k in (150, 165, 175, 200) for n in (20, 40, 50, 60, 80)]
        cases += [(k, n) for n in (60, 80) for k in range(100, 261, 3)]
        self.assertIn((154, 80), cases)
        self.assertTrue(all((k, 80) in cases for k in range(244, 257, 3)))
        for k, n in cases:
            shutil.rmtree(os.path.join(self.dir, "costs"), ignore_errors=True)
            rows = []
            for i in range(k):
                d = time.strftime("%Y-%m-%d", time.gmtime(T0 - (i % 20) * 86400))
                rows.append({"session": "{:08d}-0000-4000-8000-{:012d}".format(i, i), "kind": "worker",
                             "task": "t-{:03d}".format(i % 30), "day": d, "from": d + "T02:00:00Z",
                             "to": d + "T02:10:00Z", "tokens": toks(cr=1000 + i), "peak": 1000})
            for i in range(n):
                d = time.strftime("%Y-%m-%d", time.gmtime(T0 - (i + 1) * 86400))
                rows.append({"day": d, "from": d + "T12:00:00Z", "to": d + "T12:01:00Z", "tokens": toks(cr=300000),
                             "peak": 300000, "first": 200000})
            self.put_rows(*rows)
            with mock.patch.object(pa_costs, "fit_summary", self.shrink_only):
                self.summary()
            old = load(self.costs("summary.json"))
            self.summary()
            new = load(self.costs("summary.json"))
            self.assertEqual({key: v for key, v in new.items() if key != "paContext"}, old, (k, n))
            _r, out, _e = self.run_cmd(pa_costs.report, self.dir, as_json=True, now=T0 + 5000)
            every = json.loads(out)["paContext"]
            self.assertEqual(len(every), n, (k, n))  # report keeps all days
            self.assertEqual(every[0]["day"], D0, (k, n))
            if "paContext" in new:
                self.assert_newest_days_that_fit(new, every[:pa_costs.PA_CONTEXT_SUMMARY_DAYS])
            else:  # not even [] fits in the room left (K = 154, N = 80: cf0de90 wrote 65,532 bytes)
                self.assertGreater(len(pa_costs.compact(dict(new, paContext=[])).encode()), pa_costs.SUMMARY_MAX, (k, n))

    def ctx_report(self, **kw):
        _r, out, _e = self.run_cmd(pa_costs.report, self.dir, **kw)
        return out

    def la_reset_case(self):
        """REVIEW MF-4's probe A: SID reset at 11-17T03:00Z (LA 11-16 19:00); its first row then, a later row at 10:00Z."""
        self.put_record({"v": 1, "ev": "reset", "at": D1 + "T03:00:00Z", "session": "old", "from": "old", "to": SID,
                         "reason": "probe"})
        self.put_rows({"from": D1 + "T03:00:30Z", "to": D1 + "T03:05:00Z", "tokens": toks(cr=80000), "peak": 80000,
                       "first": 80000},
                      {"from": D1 + "T10:00:00Z", "to": D1 + "T10:05:00Z", "tokens": toks(cr=290000), "peak": 290000,
                       "first": 290000})

    def test_px17_report_window_is_the_reports_local_days(self):
        """t-041 J1 round 5 (REVIEW MF-4, review_j1r5_probe A): 'pa-costs report --since/--until' are local days
        (t-036); pa_context takes the same window, so a fresh session's first call counts only when its start is
        known and inside that window, a reset counts when its local day is inside it, and rows of the window's last
        local evening (after UTC midnight) are kept. paContext's days stay UTC days."""
        self.la_reset_case()
        now = T0 + 86400 + 3600

        def entry(**kw):
            with tz("America/Los_Angeles"):
                s = json.loads(self.ctx_report(as_json=True, now=now, **kw))
            found = [e for e in s["paContext"] if e["day"] == D1]
            self.assertEqual(len(found), 1, (kw, s["paContext"]))
            e = found[0]
            return {k: e[k] for k in ("calls", "firstCall", "firstCalls", "resets")}

        # LA --since 11-17: the reset (LA 11-16 19:00) and the first row are before the window: no first call is known
        self.assertEqual(entry(since=D1), {"calls": 1, "firstCall": None, "firstCalls": 0,
                                           "resets": {"fresh": 0, "compact": 0}})
        # LA --since 11-16: the start is inside the window: the true first call
        self.assertEqual(entry(since=D0), {"calls": 2, "firstCall": 80000, "firstCalls": 1,
                                           "resets": {"fresh": 1, "compact": 0}})
        # LA --until 11-16: the row of LA 11-16's evening (UTC 11-17) is kept, on its UTC day
        self.assertEqual(entry(since=D0, until=D0), {"calls": 1, "firstCall": 80000, "firstCalls": 1,
                                                     "resets": {"fresh": 1, "compact": 0}})
        # --utc --since 11-17 and no window: as before
        for kw in (dict(utc=True, since=D1), dict()):
            self.assertEqual(entry(**kw), {"calls": 2, "firstCall": 80000, "firstCalls": 1,
                                           "resets": {"fresh": 1, "compact": 0}}, kw)

    def test_px18_money_is_priced_as_summarize_prices_the_row(self):
        """t-041 J1 round 5 (REVIEW MF-5, review_j1r5_probe C): paContext's money and perPass are the rows' money
        as summarize prices them (t-032's row_facts, the account chain and the lookup), so they agree with byKind
        pa, passes and byAccount under a scoped price file."""
        self.prices(scoped_fixture())
        agent = load(os.path.join(self.dir, "agent.json"))
        agent["costs"] = {"account": "acct-b"}
        dump(agent, os.path.join(self.dir, "agent.json"))
        self.put_rows({"tokens": toks(**{"in": 1000000}), "pass": "p1", "trigger": "report"})
        s = self.summary()
        pa_kind = [e for e in s["byKind"]["kind"] if e["key"] == "pa"]
        self.assertEqual(pa_kind[0]["money"], 0.5)
        self.assertEqual([p["money"] for p in s["passes"]], [0.5])
        e = self.day(s)
        self.assertEqual((e["money"], e["perPass"]), (0.5, 0.5))
        with tz("UTC"):
            out = self.ctx_report(by="context", now=T0 + 5000)
        row = [x for x in out.splitlines() if x.strip().startswith(D1)]
        self.assertEqual(len(row), 1, out)
        self.assertEqual(row[0].split()[-2:], ["0.50", "0.50"])

    def test_px19_the_pa_context_line_names_its_utc_day_under_a_local_today(self):
        """t-041 J1 round 5 (REVIEW MF-6, review_j1r5_probe B): under 'Today (local day, <zone>)' the PA context line
        says plainly that it is the UTC day, and which; under a UTC Today it is as before. paContext stays on UTC days."""
        self.put_record({"v": 1, "ev": "reset", "at": D0 + "T21:00:00Z", "session": "old", "from": "old", "to": SID})
        self.put_rows({"from": D0 + "T20:00:00Z", "to": D0 + "T20:05:00Z", "day": D0, "calls": 4,
                       "tokens": toks(cr=400000), "peak": 120000, "first": 90000},
                      {"from": D1 + "T01:00:00Z", "to": D1 + "T01:05:00Z", "calls": 1, "tokens": toks(cr=250000),
                       "peak": 250000, "first": 250000})
        now = T0 + 3 * 3600  # LA 11-16 19:00 PST
        with tz("America/Los_Angeles"):
            out = self.ctx_report(now=now).splitlines()
        today = [x for x in out if x.startswith("Today")]
        self.assertIn("(local day, ", today[0])
        line = [x for x in out if x.startswith("PA context")]
        self.assertEqual(line, ["PA context (UTC day {})  mean 250k  peak 250k  resets 0 (fresh 0, compact 0)".format(D1)])
        with tz("America/Los_Angeles"):
            out = self.ctx_report(now=now, utc=True).splitlines()
        self.assertEqual([x for x in out if x.startswith("PA context")],
                         ["PA context  mean 250k  peak 250k  resets 0 (fresh 0, compact 0)"])
        # no PA call on that UTC day: the label stays, and the message names that day, not today
        with tz("America/Los_Angeles"):
            out = self.ctx_report(now=T0 + 2 * 86400 + 3 * 3600).splitlines()
        self.assertEqual([x for x in out if x.startswith("PA context")],
                         ["PA context (UTC day 2026-11-19)  — (no Project Agent calls that day)"])

    def test_px13_readme_names_the_report(self):
        with open(os.path.join(HERE, "..", "README.md")) as f:
            readme = f.read()
        self.assertIn("--by context", readme)
        self.assertIn("PA context", readme)


class PX_StatusLineCtx(CCBase):
    """t-041 J1 (R15): the status line record keeps context_window.total_input_tokens as ctx, a number only."""

    def test_px20_ctx_recorded(self):
        self.run_sl(cc_bytes(context_window={"total_input_tokens": 251234, "context_window_size": fx.CANARY}))
        rec = self.cc_record()
        self.assertEqual(rec["ctx"], 251234)
        self.assertNotIn(fx.CANARY.encode(), raw(self.cc_path()))
        self.assertEqual(sorted(rec), sorted(pa_costs.CC_KEYS))

    def test_px21_bad_ctx_is_null(self):
        for i, bad in enumerate(("251234", True, -1, 1e9, float("nan"), None, [1])):
            sid = "ffff000{}-0000-4000-8000-abcdef000000".format(i)
            self.run_sl(cc_bytes(sid=sid, context_window={"total_input_tokens": bad}))
            self.assertIsNone(self.cc_record(sid)["ctx"], bad)
        sid = "ffff0010-0000-4000-8000-abcdef000000"
        self.run_sl(cc_bytes(sid=sid, context_window=fx.CANARY))
        self.assertIsNone(self.cc_record(sid)["ctx"])
        inp = cc_input(sid="ffff0011-0000-4000-8000-abcdef000000")
        del inp["context_window"]
        self.run_sl(json.dumps(inp).encode())
        self.assertIsNone(self.cc_record("ffff0011-0000-4000-8000-abcdef000000")["ctx"])

    def test_px22_kept_and_rewritten(self):
        self.run_sl(cc_bytes(context_window={"total_input_tokens": 1000}), now=T0)
        self.run_sl(cc_bytes(context_window={"total_input_tokens": 2000}), now=T0 + 5)  # only ctx changed: rewritten
        self.assertEqual(self.cc_record()["ctx"], 2000)
        inp = cc_input(cost=1.5)
        del inp["context_window"]
        self.run_sl(json.dumps(inp).encode(), now=T0 + 10)  # a reading without ctx keeps the stored one
        rec = self.cc_record()
        self.assertEqual((rec["cost"], rec["ctx"]), (1.5, 2000))

    def test_px23_old_record_reads_with_null_ctx(self):
        old = {"v": 1, "session": CC_SID, "cost": 1.0, "base": 0, "segments": 1, "durationMs": 1, "apiDurationMs": 1,
               "model": "model-a", "version": "2.1.0", "terminal": "t5", "first": iso(T0), "at": iso(T0)}
        self.assertIsNone(pa_costs.cc_clean(old)["ctx"])
        self.assertEqual(pa_costs.cc_clean(dict(old, ctx=5))["ctx"], 5)
        self.assertIsNone(pa_costs.cc_clean(dict(old, ctx="5"))["ctx"])


# ---- t-032 J2a: d-117 limits 1 (key per scope) and 4 (unknown scoped keys) ------------------------------------------

def keys_fixture():
    """RL-1 probe 1: top-level model-a with the alias ^model-a; a provider-b scope with the exact key model-a-x."""
    return {"v": 1, "currency": "USD", "defaultScope": {"provider": "provider-a", "endpoint": None, "region": None, "account": None},
            "models": {"model-a": [{"validFrom": "2026-01-01", "in": 1.0, "out": 10.0, "cw5m": 2.0, "cw1h": 4.0, "cr": 0.5}]},
            "aliases": [{"match": "^model-a", "model": "model-a"}],
            "scoped": [{"provider": "provider-b", "models": {"model-a-x": [{"validFrom": "2026-01-01", "in": 7.0, "out": 70.0,
                                                                                "cw5m": 1.0, "cw1h": 1.0, "cr": 0.7}]}}]}


class J2A_Keys(CostsBase):
    """Limit 1: a model's key is resolved per candidate scope. Limit 4: an unknown key in a scoped entry skips it."""

    table = J1A_Scopes.table
    look = J1A_Scopes.look

    def test_j2k1_scoped_exact_key_does_not_hide_a_top_level_alias(self):
        t = self.table(keys_fixture())
        self.assertEqual(self.look(t, "model-a-x", None), (1.0, ["provider-a", "*", "*", "*"]))
        self.assertEqual(self.look(t, "model-a-x", ("provider-a", None, None, None)), (1.0, ["provider-a", "*", "*", "*"]))
        self.assertEqual(self.look(t, "model-a-x", (None, None, None, None))[0], 1.0)

    def test_j2k2_the_scope_with_the_exact_key_uses_it(self):
        t = self.table(keys_fixture())
        self.assertEqual(self.look(t, "model-a-x", ("provider-b", None, None, None)), (7.0, ["provider-b", "*", "*", "*"]))
        self.assertEqual(self.look(t, "model-a", ("provider-b", None, None, None)), (None, None))  # no crossing of providers

    def test_j2k3_a_scoped_alias_reaches_only_scopes_that_have_its_target(self):
        value = keys_fixture()
        value["scoped"].append({"provider": "provider-a", "region": "region-a",
                                "models": {"model-c": [{"validFrom": "2026-01-01", "in": 3.0, "out": 3.0, "cw5m": 3.0, "cw1h": 3.0, "cr": 3.0}]}})
        value["aliases"] = [{"match": "^model-a", "model": "model-a"}]
        value["scopedAliases"] = [{"match": "^model-", "model": "model-c"}]
        t = self.table(value)
        # region-a has model-c only: the top-level alias ^model-a (target model-a) is skipped there, the scoped one applies
        self.assertEqual(self.look(t, "model-a-y", ("provider-a", None, "region-a", None)), (3.0, ["provider-a", "*", "region-a", "*"]))
        # the default scope has model-a: its alias applies
        self.assertEqual(self.look(t, "model-a-y", ("provider-a", None, "region-z", None)), (1.0, ["provider-a", "*", "*", "*"]))
        # a model only the scoped alias names: priced in region-a, nowhere else
        self.assertEqual(self.look(t, "model-q", ("provider-a", None, "region-a", None))[0], 3.0)
        self.assertEqual(self.look(t, "model-q", ("provider-a", None, None, None)), (None, None))

    def test_j2k4_files_without_scoped_resolve_as_before(self):
        for value in (PRICES, scoped_fixture()):
            self.prices(value)
            prof = pa.load_profile("test")
            old, new = old_pricing.load_prices(prof)[0], pa_costs.load_prices(prof)[0]
            for model in J1A_Parity.MODELS:
                for day in J1A_Parity.DAYS:
                    when = day + "T10:00:00Z"
                    self.assertEqual(pa_costs.price_for(new, model, when), old_pricing.price_for(old, model, when), (model, day))

    def test_j2k5_an_unknown_key_skips_the_scoped_entry_with_a_warning(self):
        value = keys_fixture()
        value["scoped"].insert(0, {"provider": "provider-a", "tier": "x",
                                   "models": {"model-a": [{"validFrom": "2026-01-01", "in": 9.0, "out": 9.0, "cw5m": 9.0, "cw1h": 9.0, "cr": 9.0}]}})
        t = self.table(value)
        self.assertEqual([e["scope"] for e in t["scoped"]], [("provider-b", None, None, None)])
        self.assertTrue(any("scoped[0]" in w and "tier" in w for w in t["warnings"]), t["warnings"])
        self.assertEqual(t["skippedScoped"], [0])
        self.assertEqual(self.look(t, "model-a", ("provider-a", None, None, None))[0], 1.0)

    def test_j2k6_a_skipped_entry_is_not_a_duplicate(self):
        value = keys_fixture()
        value["scoped"].append({"provider": "provider-b", "tier": "x", "models": {}})
        t = self.table(value)
        self.assertEqual(len(t["scoped"]), 1)
        self.assertTrue(any("scoped[1]" in w for w in t["warnings"]), t["warnings"])

    def test_j2k7_unknown_key_in_default_scope_is_a_warning(self):
        value = keys_fixture()
        value["defaultScope"]["tier"] = "x"
        t = self.table(value)
        self.assertTrue(any("defaultScope" in w and "tier" in w for w in t["warnings"]), t["warnings"])
        self.assertEqual(self.look(t, "model-a", None)[0], 1.0)


# ---- t-032 J2b: pa-costs prices fetch (R-F1-F4, R-S1-S5, R-T1, A-F1-F4, A-S); no network: an injected opener --------

FETCH_SAMPLES = os.path.join(SAMPLES, "prices-fetch")
URL_A = "https://prices.example.test/list/region-a/index.json"
URL_B = "https://prices.example.test/list/region-b/index.json"
FETCH_NOW = T0  # 2026-11-17 (UTC)


def fetch_sample(name):
    return load(os.path.join(FETCH_SAMPLES, name))


def source_decl(**over):
    decl = {"label": "Neutral list", "providers": ["provider-a"],
            "defaultScope": {"provider": "provider-a", "endpoint": "global", "region": None, "account": None},
            "urls": {"region-a": {"url": URL_A, "region": "region-a", "maxBytes": 100000},
                     "region-b": {"url": URL_B, "region": "region-b", "maxBytes": 100000}},
            "defaultRegion": "region-a", "regionFree": ["global"],
            "read": ["prices/reader_stub.py", "--file", "{file}", "--region", "{region}"], "currency": "USD"}
    decl.update(over)
    return decl


class FakeResponse:
    def __init__(self, status, headers, body, chunks=None, on_read=None):
        self.status, self.headers, self.body, self.pos = status, headers, body, 0
        self.chunks, self.on_read, self.closed = chunks, on_read, False

    def getcode(self):
        return self.status

    def read(self, n=-1):
        if self.on_read:
            self.on_read()
        size = self.chunks or n
        if n is not None and n >= 0:
            size = min(size, n)
        part = self.body[self.pos:self.pos + size] if size and size > 0 else self.body[self.pos:]
        self.pos += len(part)
        return part

    def close(self):
        self.closed = True


class FakeHeaders(dict):
    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


class J2Base(CostsBase):
    """A profile `test` declaring source src-a (two neutral regions), the installed-shape price file, the stub reader
    copied into the profile folder, an opener serving tests/samples/prices-fetch/, and no socket anywhere."""

    def setUp(self):
        super().setUp()
        for target in ("socket.socket", "socket.create_connection", "urllib.request.urlopen"):
            p = mock.patch(target, side_effect=AssertionError("no network in tests: " + target))
            p.start()
            self.addCleanup(p.stop)
        os.makedirs(os.path.join(self.profile_dir, "prices"), exist_ok=True)
        stub = os.path.join(self.profile_dir, "prices", "reader_stub.py")
        shutil.copy(os.path.join(FETCH_SAMPLES, "reader_stub.py"), stub)
        os.chmod(stub, 0o755)
        self.declare(source_decl())
        self.prices(fetch_sample("prices-before.json"))
        self.served = {URL_A: fetch_sample("list-region-a.json"), URL_B: fetch_sample("list-region-b.json")}
        self.requests, self.responses = [], {}
        saved_tty = pa_costs.stdin_is_tty
        pa_costs.stdin_is_tty = lambda: True
        self.addCleanup(setattr, pa_costs, "stdin_is_tty", saved_tty)
        self.tmp_before = set(os.listdir(tempfile.gettempdir()))

    def declare(self, *decls, **named):
        prof = load(os.path.join(self.profile_dir, "profile.json"))
        sources = dict(named)
        if decls:
            sources["src-a"] = decls[0]
        prof["prices"] = {"sources": sources}
        dump(prof, os.path.join(self.profile_dir, "profile.json"))

    def target(self):
        return os.path.join(self.profile_dir, "prices.json")

    def opener(self, request, timeout):
        self.requests.append((request, timeout))
        url = request.get_full_url()
        if url in self.responses:
            r = self.responses[url]
            return r() if callable(r) else r
        value = self.served[url]
        body = value if isinstance(value, bytes) else json.dumps(value).encode()
        return FakeResponse(200, FakeHeaders({"Content-Length": str(len(body))}), body)

    def fetch(self, answer="y", **kw):
        kw.setdefault("opener", self.opener)
        kw.setdefault("now", FETCH_NOW)
        asked = []

        def ask(prompt):
            asked.append(prompt)
            return answer
        kw.setdefault("ask", ask)
        code, out, err = self.run_cmd(pa_costs.prices_fetch, **kw)
        self.asked = asked
        return code, out, err

    def snapshot(self):
        path = self.target()
        folder = os.path.dirname(path)
        return raw(path), os.stat(path).st_mtime_ns, sorted(os.listdir(folder))

    def assert_untouched(self, before):
        self.assertEqual(self.snapshot(), before)
        self.assertEqual([x for x in os.listdir(os.path.dirname(self.target())) if x.endswith(".bak")], [])
        self.assert_no_temp_left()

    def assert_no_temp_left(self):
        left = [x for x in set(os.listdir(tempfile.gettempdir())) - self.tmp_before if x.startswith("pa-prices-")]
        self.assertEqual(left, [])

    def written(self):
        return load(self.target())

    def scoped(self, value, scope):
        for e in value.get("scoped") or []:
            if tuple(e.get(f) for f in pa_costs.SCOPE_FIELDS) == scope:
                return e["models"]
        return None


class J2S_Decl(J2Base):
    def test_j2d1_a_good_declaration(self):
        sources, problems = pa_costs.price_sources(pa.load_profile("test"))
        self.assertEqual(problems, [])
        self.assertEqual(sorted(sources), ["src-a"])
        self.assertEqual(sources["src-a"]["defaultRegion"], "region-a")

    def test_j2d2_each_bad_declaration_is_refused_and_nothing_is_downloaded(self):
        url = lambda u: {"region-a": {"url": u, "region": "region-a", "maxBytes": 1000}}  # noqa: E731
        bad = {"unknown key": {"color": "x"}, "http": {"urls": url("http://prices.example.test/a.json"), "defaultRegion": "region-a"},
               "ip": {"urls": url("https://192.0.2.1/a.json"), "defaultRegion": "region-a"},
               "ipv6": {"urls": url("https://[2001:db8::1]/a.json"), "defaultRegion": "region-a"},
               "localhost": {"urls": url("https://localhost/a.json"), "defaultRegion": "region-a"},
               "port": {"urls": url("https://prices.example.test:8443/a.json"), "defaultRegion": "region-a"},
               "userinfo": {"urls": url("https://u:p@prices.example.test/a.json"), "defaultRegion": "region-a"},
               "query": {"urls": url("https://prices.example.test/a.json?x=1"), "defaultRegion": "region-a"},
               "no dot": {"urls": url("https://prices/a.json"), "defaultRegion": "region-a"},
               "too big": {"urls": {"region-a": {"url": URL_A, "region": "region-a", "maxBytes": 60 * 1024 * 1024}}, "defaultRegion": "region-a"},
               "no maxBytes": {"urls": {"region-a": {"url": URL_A, "region": "region-a"}}, "defaultRegion": "region-a"},
               "read outside": {"read": ["../outside.py", "--file", "{file}"]},
               "read empty": {"read": []},
               "defaultRegion": {"defaultRegion": "region-z"},
               "bad provider": {"providers": ["Bad Label!"]}, "no providers": {"providers": []},
               "twelve": {"label": "acct " + "12345" + "6789012"},
               "bad regex": {"models": {"include": ["("]}},
               "bad currency": {"currency": "usd"}}
        for why, over in sorted(bad.items()):
            with self.subTest(why=why):
                self.declare(source_decl(**over))
                sources, problems = pa_costs.price_sources(pa.load_profile("test"))
                self.assertEqual(sources, {}, why)
                self.assertTrue(problems and "src-a" in problems[0], (why, problems))
                before = self.snapshot()
                code, _out, err = self.fetch(source="src-a")
                self.assertIn(code, (1, 3), (why, err))
                self.assertEqual(self.requests, [], why)
                self.assert_untouched(before)

    def test_j2d3_prices_lists_the_declared_sources(self):
        _code, out, _err = self.run_cmd(pa_costs.cmd_prices, self.dir)
        self.assertIn("Sources:", out)
        self.assertIn("src-a (Neutral list): 2 files", out)
        self.declare(source_decl(color="x"))
        _code, out, _err = self.run_cmd(pa_costs.cmd_prices, self.dir)
        self.assertIn("src-a", out)
        self.assertIn("color", out)

    def test_j2d4_check_url_table(self):
        self.assertIsNone(pa_costs.check_url(URL_A))
        for u in ("http://a.example.test/x", "https://a.example.test:443/x", "https://127.0.0.1/x", "https://a.localhost/x",
                  "https://a.example.test/x#f", "ftp://a.example.test/x", "file:///etc/passwd", "https://a_b.example.test/x",
                  "https://a.example.test/" + "12345" + "6789012", "https://a.example.test/" + "x" * 3000, ""):
            self.assertIsNotNone(pa_costs.check_url(u), u)


class J2S_Net(J2Base):
    def test_j2n1_a_redirect_is_a_failure_naming_its_target(self):
        for status in (301, 302, 307, 308):
            self.responses[URL_A] = FakeResponse(status, FakeHeaders({"Location": "https://elsewhere.example.test/x"}), b"")
            before = self.snapshot()
            code, _out, err = self.fetch(source="src-a")
            self.assertEqual(code, 1, err)
            self.assertIn("elsewhere.example.test", err)
            self.assertIn("redirect", err)
            self.assert_untouched(before)
        handler = pa_costs.NoRedirect()
        import urllib.request
        req = urllib.request.Request(URL_A)
        with self.assertRaises(pa_costs.FetchFailed) as e:
            handler.redirect_request(req, None, 302, "Found", {"Location": "https://elsewhere.example.test/y"},
                                     "https://elsewhere.example.test/y")
        self.assertIn("elsewhere.example.test/y", str(e.exception))

    def test_j2n2_the_real_opener_has_only_the_handlers_it_needs(self):
        import urllib.request
        os.environ["HTTPS_PROXY"] = "https://proxy.example.test"  # ProxyHandler registers only when a proxy is set (Q5)
        self.addCleanup(os.environ.pop, "HTTPS_PROXY", None)
        opener = pa_costs.http_opener()
        names = sorted(type(h).__name__ for h in opener.handlers)
        self.assertIn("HTTPSHandler", names)
        self.assertIn("NoRedirect", names)
        self.assertIn("ProxyHandler", names)
        for forbidden in ("HTTPHandler", "FileHandler", "FTPHandler", "DataHandler", "HTTPCookieProcessor", "HTTPRedirectHandler",
                          "HTTPBasicAuthHandler", "ProxyBasicAuthHandler", "HTTPDigestAuthHandler", "UnknownHandler"):
            self.assertNotIn(forbidden, names)
        https = [h for h in opener.handlers if isinstance(h, urllib.request.HTTPSHandler)][0]
        ctx = getattr(https, "_context", None)
        if ctx is not None:
            import ssl
            self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(ctx.check_hostname)

    def test_j2n4_the_request_carries_no_credentials(self):
        self.fetch(source="src-a", dry_run=True)
        self.assertEqual(len(self.requests), 2)
        for request, timeout in self.requests:
            headers = {k.lower(): v for k, v in request.header_items()}
            self.assertEqual(headers, {"user-agent": "herdr-pa-costs", "accept-encoding": "identity"})
            self.assertLessEqual(timeout, 20)
            self.assertIsNone(request.data)
            self.assertEqual(request.get_method(), "GET")

    def test_j2n5_a_compressed_response_is_refused(self):
        body = json.dumps(self.served[URL_A]).encode()
        self.responses[URL_A] = FakeResponse(200, FakeHeaders({"Content-Encoding": "gzip"}), body)
        before = self.snapshot()
        code, _out, err = self.fetch(source="src-a")
        self.assertEqual(code, 1, err)
        self.assertIn("gzip", err)
        self.assert_untouched(before)

    def test_j2n6_the_reader_gets_a_minimal_environment(self):
        os.environ["PA_TEST_READER_CANARY"] = "x"
        self.addCleanup(os.environ.pop, "PA_TEST_READER_CANARY", None)
        code, out, err = self.fetch(source="src-a", dry_run=True)
        self.assertEqual(code, 0, err)
        env_lines = [x for x in (out + err).splitlines() if "env: " in x]
        self.assertTrue(env_lines, out + err)
        for line in env_lines:
            keys = set(line.split("env: ", 1)[1].split(","))
            self.assertNotIn("PA_TEST_READER_CANARY", keys)
            self.assertNotIn("HERDR_PA_HOME", keys)
            self.assertTrue(keys <= {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "HOME", "SYSTEMROOT", "__CF_USER_TEXT_ENCODING"}, keys)

    def test_j2n7_content_length_over_max_is_refused_before_reading(self):
        read = []
        self.responses[URL_A] = FakeResponse(200, FakeHeaders({"Content-Length": "100001"}), b"x" * 10, on_read=lambda: read.append(1))
        before = self.snapshot()
        code, _out, err = self.fetch(source="src-a")
        self.assertEqual(code, 1, err)
        self.assertIn("100000", err)
        self.assertEqual(read, [])
        self.assert_untouched(before)

    def test_j2n8_a_stream_over_max_is_refused_at_the_chunk_that_passes_it(self):
        resp = FakeResponse(200, FakeHeaders({}), b"x" * 300000, chunks=65536)
        self.responses[URL_A] = resp
        before = self.snapshot()
        code, _out, err = self.fetch(source="src-a")
        self.assertEqual(code, 1, err)
        self.assertIn("more than", err)
        self.assertLessEqual(resp.pos, 100000 + 65536)
        self.assert_untouched(before)

    def test_j2n9_past_the_whole_fetch_budget_is_refused(self):
        ticks = {"t": 0.0}

        def clock():
            return ticks["t"]

        def slow():
            ticks["t"] += 50.0
        body = json.dumps(self.served[URL_A]).encode()
        self.responses[URL_A] = lambda: FakeResponse(200, FakeHeaders({}), body, chunks=512, on_read=slow)
        before = self.snapshot()
        code, _out, err = self.fetch(source="src-a", clock=clock)
        self.assertEqual(code, 3, err)
        self.assertIn("120", err)
        self.assert_untouched(before)

    def test_j2n10_the_temp_folder_is_gone_after_success_and_failure(self):
        self.assertEqual(self.fetch(source="src-a", yes=True)[0], 0)
        self.assert_no_temp_left()
        self.served[URL_B] = {"raw": "not json\n", "exit": 0}
        self.assertEqual(self.fetch(source="src-a", yes=True)[0], 4)
        self.assert_no_temp_left()

    def test_j2n11_a_status_other_than_200_fails(self):
        self.responses[URL_B] = FakeResponse(404, FakeHeaders({}), b"no")
        before = self.snapshot()
        code, _out, err = self.fetch(source="src-a")
        self.assertEqual(code, 1, err)
        self.assertIn("404", err)
        self.assert_untouched(before)

    def test_j2n12_no_socket_is_opened(self):
        import socket
        with self.assertRaises(AssertionError):
            socket.socket()
        self.assertEqual(self.fetch(source="src-a", yes=True)[0], 0)


class J2S_Person(J2Base):
    def test_j2p1_refused_inside_an_agent_before_anything(self):
        for marker in pa_costs.AGENT_MARKERS:
            for dry in (False, True):
                os.environ[marker] = "1"
                try:
                    before = self.snapshot()
                    code, _out, err = self.fetch(source="src-a", dry_run=dry)
                finally:
                    os.environ.pop(marker)
                self.assertEqual(code, 3, (marker, err))
                self.assertIn("person", err)
                self.assertEqual(self.requests, [])
                self.assert_untouched(before)

    def test_j2p2_refused_in_a_worker_terminal(self):
        self.hc.terminals["w9"] = {"address": "w9", "labels": {"herdr.pa": "sp1"}}
        os.environ["HC_TERMINAL_ID"] = "w9"
        self.addCleanup(os.environ.pop, "HC_TERMINAL_ID", None)
        before = self.snapshot()
        code, _out, err = self.fetch(source="src-a", dry_run=True)
        self.assertEqual(code, 3, err)
        self.assertEqual(self.requests, [])
        self.assert_untouched(before)

    def guard(self, command):
        return W_CloseAndGuard.guard(self, command)

    def test_j2p3_the_guard_refuses_the_fetch_and_allows_the_listing(self):
        tool = "pa-" + "costs"
        for command in (tool + " prices fetch", tool + " prices fetch --dry-run", tool + " prices --space sp1 fetch --dry-run",
                        tool + " 'prices' fetch", tool + ' prices "fetch" --yes', "cd /tmp && " + tool + " prices\\ fetch",
                        "~/.local/bin/" + tool + " prices fetch --source s --into /x/prices.json"):
            code, err = self.guard(command)
            self.assertEqual(code, 2, command)
            self.assertIn("price fetch", err, command)
        for command in (tool + " prices", tool + " prices --space sp1", tool + " report", tool + " collect --space sp1 --quiet"):
            self.assertEqual(self.guard(command)[0], 0, command)


def reader_lines(*lines, done=True, url=URL_A):
    out = "".join(json.dumps(x) + "\n" for x in lines)
    if done:
        out += json.dumps({"done": True, "source": {"url": url, "version": "v1", "publicationDate": "2026-11-01T12:00:00Z"}}) + "\n"
    return out


def rline(**over):
    line = {"provider": "provider-a", "account": None, "endpoint": "global", "region": None, "model": "model-a", "kind": "in",
            "price": 1.5, "effectiveDate": "2026-11-01", "ref": {"sku": "S1", "usagetype": "u", "rateCode": "r", "raw": "1.5"}}
    line.update(over)
    return line


class J2R_Reader(J2Base):
    def run_raw(self, text, code=0):
        self.served[URL_A] = {"raw": text, "exit": code}
        before = self.snapshot()
        result = self.fetch(source="src-a", yes=True)
        return result, before

    def test_j2r1_a_good_batch_is_read(self):
        code, out, err = self.fetch(source="src-a", dry_run=True)
        self.assertEqual(code, 0, err)
        self.assertIn(URL_A, out)
        self.assertIn("v-2026-11-01", out)

    def test_j2r2_each_breach_refuses_the_whole_batch(self):
        no_kind = rline()
        del no_kind["kind"]
        null_no_why = rline(price=None)
        cases = {"not json": "{oops\n" + reader_lines(), "missing field": reader_lines(no_kind),
                 "unknown field": reader_lines(rline(color="x")), "bad kind": reader_lines(rline(kind="cw2h")),
                 "nan": reader_lines(rline()).replace("1.5,", "NaN,", 1), "inf": reader_lines(rline()).replace("1.5,", "Infinity,", 1),
                 "negative": reader_lines(rline(price=-1)), "bool": reader_lines(rline(price=True)),
                 "null without why": reader_lines(null_no_why),
                 "repeat": reader_lines(rline(), rline()), "bad model": reader_lines(rline(model="model a")),
                 "twelve": reader_lines(rline(ref={"sku": "12345" + "6789012"})), "no done": reader_lines(rline(), done=False),
                 "done url": reader_lines(rline(), url="https://other.example.test/x.json"),
                 "region": reader_lines(rline(endpoint="regional", region="region-b")),
                 "provider": reader_lines(rline(provider="provider-z")), "bad date": reader_lines(rline(effectiveDate="2026-11")),
                 "done twice": reader_lines(rline()) + reader_lines(), "line after done": reader_lines(rline()) + json.dumps(rline(kind="out")) + "\n"}
        for why, text in sorted(cases.items()):
            with self.subTest(why=why):
                (code, _out, err), before = self.run_raw(text)
                self.assertEqual(code, 4, (why, err))
                self.assert_untouched(before)

    def test_j2r3_currency_differs_from_the_file(self):
        self.declare(source_decl(currency="EUR"))
        before = self.snapshot()
        code, _out, err = self.fetch(source="src-a", yes=True)
        self.assertEqual(code, 1, err)
        self.assertIn("EUR", err)
        self.assertEqual(self.requests, [])
        self.assert_untouched(before)

    def test_j2r15_reader_exit_6_is_exit_4(self):
        self.served[URL_A] = {"format": "something-else"}
        before = self.snapshot()
        code, _out, err = self.fetch(source="src-a", yes=True)
        self.assertEqual(code, 4, err)
        self.assertIn("format", err)
        self.assert_untouched(before)

    def test_j2r16_reader_failure_timeout_or_too_much_is_exit_1(self):
        (code, _out, err), before = self.run_raw(reader_lines(rline()), code=1)
        self.assertEqual(code, 1, err)
        self.assert_untouched(before)
        saved = pa_costs.run_script
        for outcome in ("timeout", "toolarge"):
            pa_costs.run_script = lambda *a, **k: (outcome, "", [])
            try:
                before = self.snapshot()
                code, _out, err = self.fetch(source="src-a", yes=True)
            finally:
                pa_costs.run_script = saved
            self.assertEqual(code, 1, (outcome, err))
            self.assert_untouched(before)

    def test_j2r17_a_timestamp_version_is_not_an_account_number(self):
        self.served[URL_A] = dict(self.served[URL_A], version="20261101120000")
        code, _out, err = self.fetch(source="src-a", yes=True)
        self.assertEqual(code, 0, err)
        rows = self.written()["models"]["model-b"]
        self.assertEqual(rows[-1]["fetched"]["version"], "20261101120000")


class J2C_Rules(J2Base):
    def change_a_global(self, prices, effective="2026-11-01", regions=("region-a", "region-b")):
        for url in (URL_A, URL_B):
            value = json.loads(json.dumps(self.served[url]))
            for r in value["rows"]:
                if r["model"] == "model-a" and r["endpoint"] == "global":
                    r["price"] = prices.get(r["kind"], r["price"])
                    r["effective"] = effective
            self.served[url] = value

    def old_rows_kept(self, before, after):
        """Every row the file had is in the new file, unchanged and at its place (R-F4: never edited)."""
        for model, rows in before["models"].items():
            self.assertEqual(after["models"][model][:len(rows)], rows, model)
        for e in before.get("scoped") or []:
            scope = tuple(e.get(f) for f in pa_costs.SCOPE_FIELDS)
            for model, rows in e["models"].items():
                self.assertEqual(self.scoped(after, scope)[model][:len(rows)], rows, (scope, model))
        for k, v in before.items():
            if k not in ("models", "scoped", "defaultScope", "source", "asOf"):
                self.assertEqual(after[k], v, k)

    def test_j2c1_no_rows_yet_adds_one_from_the_effective_date(self):
        before = self.written()
        code, out, err = self.fetch(source="src-a", yes=True)
        self.assertEqual(code, 0, err)
        after = self.written()
        b = after["models"]["model-b"]
        self.assertEqual(len(b), 1)
        self.assertEqual(b[0]["validFrom"], "2026-11-01")
        self.assertEqual((b[0]["in"], b[0]["out"], b[0]["cw5m"], b[0]["cw1h"], b[0]["cr"]), (2.0, 20.0, 4.0, None, 1.0))
        self.assertIn("cw1h", b[0]["why"])
        self.assertEqual(b[0]["provider"], "provider-a")
        self.assertEqual((b[0]["endpoint"], b[0]["region"]), ("global", None))
        self.assertEqual(b[0]["source"], "Neutral list")
        self.assertEqual(b[0]["fetched"]["url"], URL_A)
        self.assertEqual(b[0]["fetched"]["publicationDate"], "2026-11-01T12:00:00Z")
        self.assertEqual(len(b[0]["fetched"]["sha256"]), 64)
        self.assertEqual(b[0]["refs"]["in"]["sku"], "SKU-model-b")
        self.old_rows_kept(before, after)
        table, problem = pa_costs.load_prices(pa.load_profile("test"))
        self.assertIsNone(problem)
        self.assertEqual(table["warnings"], [])

    def test_j2c2_same_prices_change_nothing(self):
        self.fetch(source="src-a", yes=True)
        after = self.written()
        self.assertEqual(len(after["models"]["model-a"]), 1)  # the global model-a equals the file's: no row added
        self.assertEqual(after["models"]["model-a"][0]["source"], "by hand")  # provenance not rewritten

    def test_j2c3_a_change_after_the_latest_row_is_added_at_the_effective_date(self):
        before = self.written()
        self.change_a_global({"in": 1.5})
        code, out, err = self.fetch(source="src-a", yes=True)
        self.assertEqual(code, 0, err)
        rows = self.written()["models"]["model-a"]
        self.assertEqual([r["validFrom"] for r in rows], ["2026-01-01", "2026-11-01"])
        self.assertEqual((rows[1]["in"], rows[1]["out"]), (1.5, 10.0))
        self.assertIn("model-a / in: 1.0 → 1.5 (validFrom 2026-11-01)", out)
        self.old_rows_kept(before, self.written())

    def test_j2c4_an_earlier_effective_date_is_added_from_today(self):
        self.change_a_global({"in": 1.5}, effective="2025-06-01")
        code, out, _err = self.fetch(source="src-a", yes=True)
        self.assertEqual(code, 0)
        rows = self.written()["models"]["model-a"]
        self.assertEqual(rows[-1]["validFrom"], "2026-11-17")
        self.assertIn("source effective 2025-06-01 is before your row of 2026-01-01: added from today", out)

    def test_j2c5_a_model_not_in_the_source_is_kept_and_listed(self):
        code, out, _err = self.fetch(source="src-a", yes=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.written()["models"]["model-old"], fetch_sample("prices-before.json")["models"]["model-old"])
        self.assertIn("not in source", out)
        self.assertIn("model-old", out)

    def test_j2c6_a_null_never_removes_a_price(self):
        self.change_a_global({"cw1h": None})
        code, out, _err = self.fetch(source="src-a", yes=True)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.written()["models"]["model-a"]), 1)
        self.assertIn("model-a / cw1h: kept 4.0 (source has no price: no row)", out)  # the stub's default why

    def test_j2c7_a_null_beside_a_change_carries_the_files_number(self):
        self.change_a_global({"cw1h": None, "in": 1.5})
        self.fetch(source="src-a", yes=True)
        row = self.written()["models"]["model-a"][-1]
        self.assertEqual((row["in"], row["cw1h"]), (1.5, 4.0))

    def test_j2c9_a_second_identical_fetch_writes_nothing(self):
        self.assertEqual(self.fetch(source="src-a", yes=True)[0], 0)
        before = self.snapshot()
        code, out, _err = self.fetch(source="src-a", yes=True)
        self.assertEqual(code, 0)
        self.assertIn("no change", out)
        self.assertEqual(self.asked, [])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len([x for x in before[2] if x.endswith(".bak")]), 1)

    def test_j2c10_source_and_as_of_only_when_the_default_scope_changed(self):
        value = fetch_sample("prices-before.json")
        value["defaultScope"] = {"provider": "provider-a", "endpoint": "global", "region": None, "account": None}
        value["models"]["model-b"] = [{"validFrom": "2026-01-01", "in": 2.0, "out": 20.0, "cw5m": 4.0, "cw1h": None, "cr": 1.0}]
        self.prices(value)
        self.fetch(source="src-a", yes=True)
        after = self.written()
        self.assertEqual((after["source"], after["asOf"]), ("entered by hand", "2026-01-01"))
        self.assertIsNotNone(self.scoped(after, ("provider-a", "regional", "region-a", None)))
        self.change_a_global({"in": 1.5})
        self.fetch(source="src-a", yes=True)
        after = self.written()
        self.assertIn(URL_A, after["source"])
        self.assertEqual(after["asOf"], "2026-11-01")

    def test_j2c11_a_row_later_than_today_is_never_shadowed(self):
        value = fetch_sample("prices-before.json")
        value["models"]["model-a"].append({"validFrom": "2026-12-01", "in": 1.0, "out": 10.0, "cw5m": 2.0, "cw1h": 4.0, "cr": 0.5})
        self.prices(value)
        self.change_a_global({"in": 1.5}, effective="2026-11-01")
        code, out, _err = self.fetch(source="src-a", yes=True)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.written()["models"]["model-a"]), 2)
        self.assertIn("your row of 2026-12-01 is later than today", out)

    def test_j2c12_two_regions_one_region_free_scope_and_two_regional(self):
        code, out, err = self.fetch(source="src-a", yes=True)
        self.assertEqual(code, 0, err)
        after = self.written()
        self.assertEqual(after["defaultScope"], {"provider": "provider-a", "endpoint": "global", "region": None, "account": None})
        b = after["models"]["model-b"][0]
        self.assertEqual([x["region"] for x in b["refs"]["in"]["sameIn"]], ["region-b"])
        ra = self.scoped(after, ("provider-a", "regional", "region-a", None))
        rb = self.scoped(after, ("provider-a", "regional", "region-b", None))
        self.assertEqual(ra["model-a"][0]["in"], 1.1)
        self.assertEqual(rb["model-a"][0]["in"], 1.2)
        self.assertEqual((rb["model-a"][0]["endpoint"], rb["model-a"][0]["region"]), ("regional", "region-b"))
        self.assertEqual(rb["model-a"][0]["fetched"]["url"], URL_B)
        self.assertIsNone(self.scoped(after, ("provider-a", "global", "region-b", None)))
        table, problem = pa_costs.load_prices(pa.load_profile("test"))
        self.assertIsNone(problem)
        when = "2026-11-17T10:00:00Z"
        self.assertEqual(pa_costs.price_for(table, "model-a", when, ("provider-a", "regional", "region-b", None))["in"], 1.2)
        self.assertIsNone(pa_costs.price_for(table, "model-a", when, ("provider-a", "regional", "region-c", None)))
        self.assertEqual(pa_costs.price_for(table, "model-a", when, ("provider-a", "global", "region-b", None))["in"], 1.0)

    def test_j2c13_a_region_whose_global_row_differs_gets_its_own_scope(self):
        self.served[URL_B] = fetch_sample("list-region-b-global-differs.json")
        code, out, _err = self.fetch(source="src-a", yes=True)
        self.assertEqual(code, 0)
        after = self.written()
        gb = self.scoped(after, ("provider-a", "global", "region-b", None))
        self.assertEqual(gb["model-a"][0]["in"], 0.9)
        self.assertNotIn("model-b", gb)
        self.assertIn("differs in region-b", out)

    def test_j2c14_a_model_only_in_another_regions_free_rows_gets_that_region(self):
        self.served[URL_B] = fetch_sample("list-region-b-third-model.json")
        self.fetch(source="src-a", yes=True)
        after = self.written()
        self.assertNotIn("model-c", after["models"])
        self.assertEqual(self.scoped(after, ("provider-a", "global", "region-b", None))["model-c"][0]["in"], 5.0)

    def test_j2c15_identical_multi_region_fetch_writes_nothing_the_second_time(self):
        self.served[URL_B] = fetch_sample("list-region-b-global-differs.json")
        self.fetch(source="src-a", yes=True)
        before = self.snapshot()
        code, out, _err = self.fetch(source="src-a", yes=True)
        self.assertEqual(code, 0)
        self.assertIn("no change", out)
        self.assertEqual(self.snapshot(), before)

    def test_j2c16_model_limits_leave_models_out_and_never_remove(self):
        self.served[URL_B] = fetch_sample("list-region-b-third-model.json")
        self.declare(source_decl(models={"include": ["^model-"], "exclude": ["^model-b$"], "onlyWithEndpoint": "regional"}))
        code, out, _err = self.fetch(source="src-a", yes=True)
        self.assertEqual(code, 0)
        after = self.written()
        self.assertNotIn("model-b", after["models"])
        self.assertIsNone((self.scoped(after, ("provider-a", "global", "region-b", None)) or {}).get("model-c"))
        self.assertIn("excluded by the profile (left as they are): model-b, model-c", out)
        self.assertEqual(self.scoped(after, ("provider-a", "regional", "region-a", None))["model-a"][0]["in"], 1.1)

    def test_j2c17_the_first_fetch_writes_the_declared_default_scope(self):
        code, out, _err = self.fetch(source="src-a", yes=True)
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[[i for i, x in enumerate(out.splitlines()) if "default scope" in x][0]].split(":")[0], "default scope")
        self.assertIn("default scope: none → provider-a · global · * · *", out)
        self.assertEqual(self.written()["defaultScope"]["endpoint"], "global")

    def test_j2c18_a_file_with_another_default_scope_keeps_it(self):
        value = fetch_sample("prices-before.json")
        value["defaultScope"] = {"provider": "provider-a", "endpoint": None, "region": None, "account": None}
        self.prices(value)
        self.fetch(source="src-a", yes=True)
        after = self.written()
        self.assertEqual(after["defaultScope"], value["defaultScope"])
        self.assertEqual(after["models"], value["models"])
        self.assertEqual(self.scoped(after, ("provider-a", "global", None, None))["model-b"][0]["in"], 2.0)


class J2F_Flow(J2Base):
    def test_j2f1_dry_run_prints_and_writes_nothing(self):
        before = self.snapshot()
        code, out, err = self.fetch(source="src-a", dry_run=True)
        self.assertEqual(code, 0, err)
        self.assertIn("model-b", out)
        self.assertIn("dry run", out)
        self.assertEqual(self.asked, [])
        self.assert_untouched(before)

    def test_j2f2_yes_at_the_question_writes_with_a_backup(self):
        old = raw(self.target())
        code, out, err = self.fetch(source="src-a")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.asked), 1)
        self.assertIn("[y/N]", self.asked[0])
        self.assertIn(self.target(), self.asked[0])
        baks = [x for x in os.listdir(self.profile_dir) if x.endswith(".bak")]
        self.assertEqual(baks, ["prices.json.20261117T000000Z.bak"])
        self.assertEqual(raw(os.path.join(self.profile_dir, baks[0])), old)
        self.assertIn("installed profile", out + err)  # the warning: the next install overwrites it

    def test_j2f3_no_at_the_question_is_exit_5(self):
        before = self.snapshot()
        code, _out, _err = self.fetch(source="src-a", answer="n")
        self.assertEqual(code, 5)
        self.assert_untouched(before)

    def test_j2f4_yes_flag_skips_the_question(self):
        code, _out, _err = self.fetch(source="src-a", yes=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.asked, [])
        self.assertIn("model-b", self.written()["models"])

    def test_j2f5_not_on_a_terminal_without_yes_is_refused(self):
        pa_costs.stdin_is_tty = lambda: False
        before = self.snapshot()
        code, _out, err = self.fetch(source="src-a")
        self.assertEqual(code, 3, err)
        self.assertEqual(self.requests, [])
        self.assert_untouched(before)
        self.assertEqual(self.fetch(source="src-a", dry_run=True)[0], 0)

    def test_j2f6_two_sources_need_source(self):
        self.declare(source_decl(), **{"src-b": source_decl(label="Other")})
        before = self.snapshot()
        code, _out, err = self.fetch()
        self.assertEqual(code, 2, err)
        self.assertIn("--source", err)
        self.assert_untouched(before)
        self.declare(source_decl())
        self.assertEqual(self.fetch(dry_run=True)[0], 0)  # one source: no --source needed
        self.assertEqual(self.fetch(source="src-z", dry_run=True)[0], 2)

    def test_j2f7_no_source_declared(self):
        prof = load(os.path.join(self.profile_dir, "profile.json"))
        del prof["prices"]
        dump(prof, os.path.join(self.profile_dir, "profile.json"))
        code, _out, err = self.fetch()
        self.assertEqual(code, 1)
        self.assertIn("declares no price source", err)

    def test_j2f8_five_backups_are_kept(self):
        for i in range(7):
            self.change_in(1.0 + i / 10.0)
            self.assertEqual(self.fetch(source="src-a", yes=True, now=FETCH_NOW + i * 86400)[0], 0)
        baks = sorted(x for x in os.listdir(self.profile_dir) if x.endswith(".bak"))
        self.assertEqual(len(baks), 5)
        self.assertEqual(baks[0], "prices.json.20261119T000000Z.bak")
        self.assertEqual(baks[-1], "prices.json.20261123T000000Z.bak")

    def change_in(self, price):
        for url in (URL_A, URL_B):
            value = json.loads(json.dumps(self.served[url]))
            for r in value["rows"]:
                if r["model"] == "model-b" and r["kind"] == "in":
                    r["price"] = price
            self.served[url] = value

    def test_j2f9_a_failed_replace_leaves_the_old_bytes(self):
        before = raw(self.target())
        saved = os.replace

        def boom(a, b):
            raise OSError("injected")
        with mock.patch.object(pa_costs.os, "replace", side_effect=boom):
            code, _out, err = self.fetch(source="src-a", yes=True)
        self.assertIs(os.replace, saved)
        self.assertEqual(code, 1, err)
        self.assertEqual(raw(self.target()), before)
        self.assertEqual([x for x in os.listdir(self.profile_dir) if ".tmp-" in x], [])

    def test_j2f10_a_file_changed_meanwhile_is_not_overwritten(self):
        other = dict(fetch_sample("prices-before.json"), note="written by someone else")

        def ask(prompt):
            dump(other, self.target())
            return "y"
        code, _out, err = self.fetch(source="src-a", ask=ask)
        self.assertEqual(code, 1, err)
        self.assertIn("changed", err)
        self.assertEqual(self.written()["note"], "written by someone else")

    def test_j2f11_a_symlink_target_is_refused(self):
        real = os.path.join(TMP, "real-prices.json")
        shutil.copy(self.target(), real)
        link = os.path.join(TMP, "link-prices.json")
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink(real, link)
        code, _out, err = self.fetch(source="src-a", yes=True, into=link)
        self.assertEqual(code, 1, err)
        self.assertIn("link", err)
        self.assertTrue(os.path.islink(link))
        self.assertEqual(self.requests, [])

    def test_j2f12_a_file_with_a_skipped_scoped_entry_is_refused(self):
        value = fetch_sample("prices-before.json")
        value["scoped"] = [{"provider": "provider-a", "tier": "x", "models": {}}]
        self.prices(value)
        before = self.snapshot()
        code, _out, err = self.fetch(source="src-a", yes=True)
        self.assertEqual(code, 1, err)
        self.assertIn("scoped[0]", err)
        self.assertEqual(self.requests, [])
        self.assert_untouched(before)

    def test_j2f13_into_another_file(self):
        folder = os.path.join(TMP, "checkout", "profiles", "test")
        os.makedirs(folder, exist_ok=True)
        into = os.path.join(folder, "prices.json")
        dump(fetch_sample("prices-before.json"), into)
        installed = raw(self.target())
        code, out, err = self.fetch(source="src-a", yes=True, into=into)
        self.assertEqual(code, 0, err)
        self.assertIn("model-b", load(into)["models"])
        self.assertEqual(raw(self.target()), installed)
        self.assertNotIn("installed profile", out + err)

    def test_j2f14_the_cli(self):
        self.assertEqual(self.run_cmd(pa_costs.main, ["prices", "fetch", "--bogus"])[0], 2)
        self.assertEqual(self.run_cmd(pa_costs.main, ["prices", "--yes"])[0], 2)  # fetch options without fetch
        fake = mock.Mock()
        fake.open = lambda request, timeout: self.opener(request, timeout)
        with mock.patch.object(pa_costs, "http_opener", return_value=fake):
            code, out, err = self.run_cmd(pa_costs.main, ["prices", "fetch", "--source", "src-a", "--dry-run"])
        self.assertEqual(code, 0, err)
        self.assertIn("dry run", out)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.run_cmd(pa_costs.main, ["prices", "--space", "sp1"])[0], 0)

    def test_j2f15_readme_names_the_fetch(self):
        with open(os.path.join(HERE, "..", "README.md")) as f:
            readme = f.read()
        for want in ("pa-costs prices fetch", "prices.sources", "--into", "--dry-run", "maxBytes", "regionFree", "defaultRegion",
                     "effectiveDate"):
            self.assertIn(want, readme)


class ZZ_V1_Privacy(unittest.TestCase):
    """Run last: the canary of every fixture reached no file under costs/ or billing/ and no captured output."""

    def test_v1_canary_nowhere(self):
        self.assertTrue(OUTPUT_SEEN, "the tests captured some output")
        self.assertEqual(CANARY_HITS, [])
        self.assertFalse([o for o in OUTPUT_SEEN if fx.CANARY in o])
