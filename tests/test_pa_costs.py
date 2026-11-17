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


T0 = 1794873600  # 2026-11-17T00:00:00Z, a fixed day start
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
            if r.get("ev") == "void":
                kept = [k for k in kept if not (k["session"] == r["session"] and k["seq"] <= r["upTo"])]
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

    def test_d5_forked_or_resumed_record_skips_copied_older_calls(self):
        self.record(session=SID, resumed=True, at=iso(T0 + 100), task="t-001")
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
