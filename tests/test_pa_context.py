"""t-041 J2: what must be on disk before a context is dropped. standing.md (seed, parser, cap), the ledger's
`context` checkpoint (checkpoint_problems), the handoff file (handoff_file), pa-doctor's notes on them
(context_notes), init_state creating standing.md, and the ledger writers keeping the new fields.
Spec R7, R8, R9 (AC7.1, AC8.1 helper, AC9.1 parser, AC9.2).

Run: python3 -m unittest -v tests/test_pa_context.py
Temp homes only: HERDR_PA_HOME, CLAUDE_CONFIG_DIR and HERDR_APP_HOME point at a temp folder while pa.py is
loaded; hc is never called (pa.call, hc_get and live_terminals are replaced).
"""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(HERE, "..", "runtime", "tools")
TMP = tempfile.mkdtemp(prefix="t041-context-")

_HOMES = {"HERDR_PA_HOME": os.path.join(TMP, "pa"), "CLAUDE_CONFIG_DIR": os.path.join(TMP, "claude"),
          "HERDR_APP_HOME": os.path.join(TMP, "app")}
_saved_env = {k: os.environ.get(k) for k in _HOMES}
_saved_mod = sys.modules.get("pa")
os.environ.update(_HOMES)
_spec = importlib.util.spec_from_file_location("pa", os.path.join(TOOLS, "pa.py"))
pa = importlib.util.module_from_spec(_spec)
sys.modules["pa"] = pa
_spec.loader.exec_module(pa)
_spec = importlib.util.spec_from_file_location("pa_hooks", os.path.join(TOOLS, "pa_hooks.py"))
pa_hooks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pa_hooks)
_spec = importlib.util.spec_from_file_location("pa_guard", os.path.join(TOOLS, "pa_guard.py"))
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)
if _saved_mod is None:
    del sys.modules["pa"]
else:
    sys.modules["pa"] = _saved_mod
for _k, _v in _saved_env.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v
assert pa.ROOT.startswith(TMP) and pa.APP.startswith(TMP), "pa.py must use the temp homes"

DROP_ENV = ("HC_TERMINAL_ID", "HC_SOCKET", "HC_TERMINAL_SOCKET", "HC_SCOPE", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
            "CODEX_SANDBOX", "CODEX_HOME_SESSION", "CLAUDE_CODE_SESSION_ID")
_module_env = {}

SEED = "# Standing words\n\n# Open asks\n\n# Hold\n\n# Working notes\n"
SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CHECKPOINT = {"pass": 12, "at": "2026-11-18T10:00:00Z", "session": SID, "clean": True, "boundary": "task", "why": None}


def setUpModule():
    for k in DROP_ENV + ("HERDR_PA_HOME",):
        _module_env[k] = os.environ.get(k)
    for k in DROP_ENV:
        os.environ.pop(k, None)
    os.environ["HERDR_PA_HOME"] = _HOMES["HERDR_PA_HOME"]


def tearDownModule():
    for k, v in _module_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    shutil.rmtree(TMP, ignore_errors=True)


def write(path, text, mode="w"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode) as f:
        f.write(text)


def dump(value, path):
    write(path, json.dumps(value, indent=2))


def load(path):
    with open(path) as f:
        return json.load(f)


class Base(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(pa.ROOT, ignore_errors=True)
        self.dir = os.path.join(pa.ROOT, "spaces", "sp1")
        dump({"version": 1, "space": "sp1", "terminal": "t0", "terminals": ["t0"], "profile": "test", "name": "S",
              "project": os.path.join(TMP, "proj")}, os.path.join(self.dir, "agent.json"))
        dump({"name": "test", "claude": ["true"], "codex": ["true"]}, os.path.join(pa.ROOT, "profiles", "test", "profile.json"))
        dump({"version": 1, "passCount": 12, "tasks": []}, os.path.join(self.dir, "ledger.json"))
        write(os.path.join(self.dir, "journal.jsonl"), "")
        self.terms = {}
        self.calls = []
        self.saved = {k: getattr(pa, k) for k in ("hc_get", "call", "live_terminals", "space_code_state")}
        self.saved_hooks = {k: getattr(pa_hooks, k) for k in ("by_an_agent",)}
        pa.hc_get = lambda a: dict(self.terms.get(a) or {})
        pa.call = self.fake_call
        pa.live_terminals = lambda: {a: dict(i) for a, i in self.terms.items() if i.get("state") == "live"}
        pa.space_code_state = lambda *a: ("unknown", "test")
        pa_hooks.by_an_agent = lambda: True
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(pa, k, v)
        for k, v in self.saved_hooks.items():
            setattr(pa_hooks, k, v)
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)

    def fake_call(self, argv, check=True):
        self.calls.append(list(argv))
        if argv[:2] == ["hc", "kill"] and argv[2] in self.terms:
            self.terms[argv[2]].update(state="finished", status=None)
        elif argv[:2] == ["hc", "rm"] and argv[2] in self.terms:
            del self.terms[argv[2]]
        elif argv[:2] == ["hc", "ls"]:
            return json.dumps({"terminals": [{"address": a, "state": i.get("state")} for a, i in self.terms.items()]})
        return ""

    def ledger(self, value=None):
        path = os.path.join(self.dir, "ledger.json")
        if value is not None:
            dump(value, path)
        return load(path)

    def doctor(self):
        out, err, code = io.StringIO(), io.StringIO(), 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                pa.cmd_doctor(argparse.Namespace(space="sp1"))
            except SystemExit as e:
                code = e.code or 0
        return code, out.getvalue()

    def run_cmd(self, kind, *argv, main=None):
        out, err, code = io.StringIO(), io.StringIO(), 0
        saved = sys.argv
        sys.argv = [kind] + list(argv)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    (main or pa_hooks.main)()
                except pa.Problem as e:
                    print("{}: {}".format(kind, e), file=sys.stderr)
                    code = 2
                except SystemExit as e:
                    code = e.code if isinstance(e.code, int) else (0 if e.code is None else 2)
        finally:
            sys.argv = saved
        return code, out.getvalue(), err.getvalue()


# ---- R9: standing.md -------------------------------------------------------------------------------------------

class Standing(unittest.TestCase):
    def test_s1_seed_parses(self):
        self.assertEqual(pa.standing_seed(), SEED)
        sections, problems, warnings = pa.parse_standing(SEED)
        self.assertEqual((problems, warnings), ([], []))
        self.assertEqual(sorted(sections), sorted(pa.STANDING_HEADINGS))
        self.assertEqual(pa.STANDING_CAP, 6000)

    def test_s2_each_heading_missing(self):
        for heading in pa.STANDING_HEADINGS:
            with self.subTest(heading=heading):
                text = SEED.replace(heading + "\n", "")
                problems = pa.parse_standing(text)[1]
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(heading, problems[0])

    def test_s3_heading_twice(self):
        problems = pa.parse_standing(SEED + "\n# Hold\n")[1]
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("# Hold", problems[0])

    def test_s4_unknown_heading_and_subheading(self):
        problems = pa.parse_standing(SEED + "\n# Other\n")[1]
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("# Other", problems[0])
        self.assertEqual(pa.parse_standing(SEED + "## Sub\n- 2026-11-18 note\n")[1], [])
        sections = pa.parse_standing("# Hold\n- 2026-11-18 hold\n" + SEED.replace("# Hold\n", ""))[0]
        self.assertEqual(sections["# Hold"], ["- 2026-11-18 hold"])  # order of the headings is free

    def test_s5_cap_in_characters(self):
        pad = lambda n, ch="x": SEED + "- 2026-11-18 " + ch * (n - len(SEED) - len("- 2026-11-18 ") - 1) + "\n"
        self.assertEqual(len(pad(6000)), 6000)
        self.assertEqual(pa.parse_standing(pad(6000))[1], [])
        problems = pa.parse_standing(pad(6001))[1]
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("6,000", problems[0])
        self.assertEqual(pa.parse_standing(pad(6000, "é"))[1], [])  # characters, not bytes

    def test_s6_warnings(self):
        text = "intro\n" + SEED.replace("# Hold\n", "# Hold\n- no date here\n")
        text = text.replace("# Working notes\n", "# Working notes\n" + "".join("- 2026-11-18 n{}\n".format(i) for i in range(11)))
        sections, problems, warnings = pa.parse_standing(text)
        self.assertEqual(problems, [])
        self.assertEqual(len(warnings), 3, warnings)
        joined = " | ".join(warnings)
        self.assertIn("no date here", joined)
        self.assertIn("before the first heading", joined)
        self.assertIn("11", joined)
        self.assertEqual(pa.parse_standing(SEED.replace("# Hold\n", "# Hold\n- 2026-11-18 ok\n  more of it\n"))[2], [])


class ReadStanding(Base):
    def path(self):
        return os.path.join(self.dir, "standing.md")

    def test_s7_missing(self):
        text, _sections, problems, _w = pa.read_standing(self.dir)
        self.assertIsNone(text)
        self.assertEqual(len(problems), 1)
        self.assertIn("missing", problems[0])

    def test_s8_not_utf8(self):
        write(self.path(), SEED.encode() + b"\xff\xfe\n", "wb")
        text, _s, problems, _w = pa.read_standing(self.dir)
        self.assertIsNone(text)
        self.assertEqual(len(problems), 1, problems)

    def test_s9_large_file_not_read(self):
        write(self.path(), SEED + "x" * 30000)
        opened = []
        real_open = open

        def spy(path, *a, **k):
            opened.append(path)
            return real_open(path, *a, **k)
        import builtins
        builtins.open, saved = spy, builtins.open
        try:
            text, _s, problems, _w = pa.read_standing(self.dir)
        finally:
            builtins.open = saved
        self.assertNotIn(self.path(), opened)
        self.assertIsNone(text)
        self.assertEqual(len(problems), 1)
        self.assertIn("6,000", problems[0])

    def test_reads_a_good_file(self):
        write(self.path(), SEED)
        text, sections, problems, warnings = pa.read_standing(self.dir)
        self.assertEqual((text, problems, warnings), (SEED, [], []))


# ---- AC9.2: init_state creates it ------------------------------------------------------------------------------

class InitState(Base):
    def p(self):
        return {"name": "test", "_folder": os.path.join(pa.ROOT, "profiles", "test")}

    def test_i1_new_space(self):
        shutil.rmtree(pa.ROOT, ignore_errors=True)
        directory, _agent = pa.init_state("sp2", os.path.join(TMP, "proj2"), self.p(), adopt=False)
        with open(os.path.join(directory, "standing.md")) as f:
            self.assertEqual(f.read(), SEED)

    def test_i2_existing_file_kept(self):
        mine = SEED + "- 2026-11-18 \"keep replies short\"\n"
        write(os.path.join(self.dir, "standing.md"), mine)
        pa.init_state("sp1", os.path.join(TMP, "proj"), self.p(), adopt=False)
        with open(os.path.join(self.dir, "standing.md")) as f:
            self.assertEqual(f.read(), mine)

    def test_i3_existing_space_gets_one(self):
        self.assertFalse(os.path.exists(os.path.join(self.dir, "standing.md")))
        before = self.ledger()
        pa.init_state("sp1", os.path.join(TMP, "proj"), self.p(), adopt=False)
        with open(os.path.join(self.dir, "standing.md")) as f:
            self.assertEqual(f.read(), SEED)
        self.assertEqual(self.ledger(), before)


# ---- R7: the checkpoint ----------------------------------------------------------------------------------------

class Checkpoint(unittest.TestCase):
    def test_c1_valid(self):
        self.assertEqual(pa.checkpoint_problems(CHECKPOINT), [])
        self.assertEqual(pa.checkpoint_problems(dict(CHECKPOINT, session=None, boundary="pass")), [])
        self.assertEqual(pa.checkpoint_problems(dict(CHECKPOINT, clean=False, boundary=None, why="waiting for a yes")), [])

    def test_c1_each_field_wrong(self):
        cases = {"not an object": [1],
                 "pass": dict(CHECKPOINT, **{"pass": -1}),
                 "pass bool": dict(CHECKPOINT, **{"pass": True}),
                 "at": dict(CHECKPOINT, at="yesterday"),
                 "session": dict(CHECKPOINT, session=12),
                 "clean": dict(CHECKPOINT, clean="yes"),
                 "boundary": dict(CHECKPOINT, boundary="phase"),
                 "why": dict(CHECKPOINT, clean=False, boundary=None, why=""),
                 "clean without boundary": dict(CHECKPOINT, boundary=None)}
        for name, value in cases.items():
            with self.subTest(case=name):
                self.assertEqual(len(pa.checkpoint_problems(value)), 1, pa.checkpoint_problems(value))


# ---- R8: the handoff file --------------------------------------------------------------------------------------

class Handoff(Base):
    def ev(self, name):
        path = os.path.join(self.dir, "evidence", "t-041", name)
        write(path, "job\n")
        return path

    def test_h1_found(self):
        path = self.ev("handoff-plan-j2.md")
        self.assertEqual(pa.handoff_file(self.dir, "t-041", {"name": "t-041-plan-j2"}), (path, path))

    def test_h2_second_try_uses_the_jobs_file(self):
        path = self.ev("handoff-fix.md")
        found, expected = pa.handoff_file(self.dir, "t-041", {"name": "t-041-fix-2"})
        self.assertEqual(found, path)
        self.assertEqual(expected, os.path.join(self.dir, "evidence", "t-041", "handoff-fix-2.md"))

    def test_h3_missing(self):
        self.assertEqual(pa.handoff_file(self.dir, "t-041", {"name": "t-041-tests"}),
                         (None, os.path.join(self.dir, "evidence", "t-041", "handoff-tests.md")))

    def test_h4_cannot_check(self):
        self.assertEqual(pa.handoff_file(self.dir, "t-041", {}), (None, None))
        self.assertEqual(pa.handoff_file(self.dir, "t-041", {"name": "other-job"}), (None, None))
        _found, expected = pa.handoff_file(self.dir, "t-041", {"name": "t-041-../../x"})
        self.assertEqual(os.path.dirname(expected), os.path.join(self.dir, "evidence", "t-041"))


# ---- pa-doctor's notes -----------------------------------------------------------------------------------------

class Doctor(Base):
    def setUp(self):
        super().setUp()
        write(os.path.join(self.dir, "standing.md"), SEED)

    def test_d1_clean_checkpoint(self):
        self.ledger(dict(self.ledger(), context=CHECKPOINT))
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("checkpoint: pass 12 at 2026-11-18T10:00:00Z, clean, boundary task, session aaaaaaaa", out)
        self.ledger(dict(self.ledger(), context=dict(CHECKPOINT, clean=False, boundary=None, why="waiting for a yes on the push")))
        self.assertIn("checkpoint: pass 12 at 2026-11-18T10:00:00Z, not clean (waiting for a yes on the push)", self.doctor()[1])
        self.ledger({"version": 1, "tasks": []})
        self.assertIn("checkpoint: none yet", self.doctor()[1])

    def test_d2_malformed_checkpoint_is_a_note(self):
        self.ledger(dict(self.ledger(), context=dict(CHECKPOINT, boundary="phase")))
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("checkpoint malformed:", out)
        self.assertNotIn("PROBLEM", out)

    def test_d3_standing_size(self):
        self.assertIn("standing.md: {} of 6,000 characters".format(len(SEED)), self.doctor()[1])

    def test_d4_standing_problems_are_notes(self):
        os.remove(os.path.join(self.dir, "standing.md"))
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("standing.md: missing", out)
        write(os.path.join(self.dir, "standing.md"), SEED + "x" * 7000)
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("standing.md: over the cap", out)
        self.assertNotIn("PROBLEM", out)

    def test_d5_due(self):
        self.ledger({"version": 1, "tasks": [
            {"id": "t-041", "state": "in-progress", "next": "follow-up report", "due": "2020-01-01T00:00:00Z"},
            {"id": "t-042", "state": "done", "next": "x", "due": "2020-01-01T00:00:00Z"},
            {"id": "t-043", "state": "queued", "next": "later", "due": "2999-01-01T00:00:00Z"},
            {"id": "t-044", "state": "queued", "next": "y", "due": "soon"}]})
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("t-041 due 2020-01-01T00:00:00Z (passed): follow-up report", out)
        self.assertNotIn("t-042 due", out)
        self.assertNotIn("t-043 due", out)
        self.assertIn("t-044 due 'soon' is not a UTC time", out)

    def test_d6_handoff_notes(self):
        write(os.path.join(self.dir, "evidence", "t-041", "handoff-plan-j2.md"), "job\n")
        self.ledger({"version": 1, "tasks": [{"id": "t-041", "state": "in-progress", "workers": [
            {"address": "t10", "name": "t-041-plan-j2"},
            {"address": "t11", "name": "t-041-fix"},
            {"address": "t12", "name": "t-041-old", "finished": True}]}]})
        out = self.doctor()[1]
        self.assertIn("worker t11 (t-041 fix) has no handoff file evidence/t-041/handoff-fix.md", out)
        self.assertNotIn("worker t10", out)
        self.assertNotIn("worker t12", out)

    def test_d7_this_session(self):
        self.assertNotIn("this session:", self.doctor()[1])
        os.environ["CLAUDE_CODE_SESSION_ID"] = SID
        self.assertIn("this session: " + SID, self.doctor()[1])
        os.environ["CLAUDE_CODE_SESSION_ID"] = "has spaces in it"
        self.assertNotIn("this session:", self.doctor()[1])


# ---- R7: the ledger writers keep the fields (pinning: these pass before J2 too) ---------------------------------

TASK_FIELDS = {"next": "the gate", "after": ["t-040"], "why": "the cap depends on the TTL", "due": "2026-11-19T10:00:00Z"}


class WritersKeep(Base):
    def setUp(self):
        super().setUp()
        self.ledger({"version": 1, "passCount": 12, "context": CHECKPOINT, "tasks": [
            dict({"id": "t-003", "title": "T", "state": "in-progress", "worktree": None, "evidence": "evidence/t-003",
                  "workers": [{"address": "t31", "role": "build", "name": "t-003-tests"}]}, **TASK_FIELDS)]})
        write(os.path.join(self.dir, "evidence", "t-003", "FINDINGS.md"), "report\n")
        self.terms["t31"] = {"address": "t31", "name": "t-003-tests", "state": "live", "status": "idle", "parent": "t0"}

    def kept(self):
        ledger = self.ledger()
        self.assertEqual(ledger.get("context"), CHECKPOINT)
        task = ledger["tasks"][0]
        self.assertEqual({k: task.get(k) for k in TASK_FIELDS}, TASK_FIELDS)

    def test_k1_close(self):
        code, _out, err = self.run_cmd("pa-worker", "close", "t31", "--evidence", "FINDINGS.md", "--space", "sp1")
        self.assertEqual(code, 0, err)
        self.assertTrue(self.ledger()["tasks"][0]["workers"][0].get("finished"))
        self.kept()

    def test_k2_keep(self):
        code, _out, err = self.run_cmd("pa-worker", "keep", "t31", "--quote", "keep t31", "--space", "sp1")
        self.assertEqual(code, 0, err)
        self.assertTrue(self.ledger()["tasks"][0]["workers"][0].get("keep"))
        self.kept()

    def test_k3_leftovers(self):
        code, _out, err = self.run_cmd("pa-worker", "close", "--leftovers", "--space", "sp1")
        self.assertEqual(code, 0, err)
        self.assertIn("workerPolicy", self.ledger())
        self.kept()

    def test_k4_task_park(self):
        os.environ.pop("CLAUDECODE", None)
        code, _out, err = self.run_cmd("pa-task", "park", "t-003", "--space", "sp1", main=pa.main)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ledger()["tasks"][0]["state"], "parked")
        self.kept()


# ---- the worker guard refuses standing.md (pinning) ------------------------------------------------------------

class Guard(unittest.TestCase):
    def test_g1_worker_cannot_write_standing(self):
        spaces = os.path.join(TMP, "g", "spaces")
        state = os.path.join(spaces, "sp1")
        os.makedirs(os.path.join(state, "evidence", "t-x"), exist_ok=True)
        self.assertIsNotNone(guard.check_edit(os.path.join(state, "standing.md"), spaces, cwd=TMP, home=os.path.join(TMP, "g")))
        self.assertIsNone(guard.check_edit(os.path.join(state, "evidence", "t-x", "a.md"), spaces, cwd=TMP, home=os.path.join(TMP, "g")))


if __name__ == "__main__":
    unittest.main()
