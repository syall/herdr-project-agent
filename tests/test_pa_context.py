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
sys.path.insert(0, HERE)  # child_fakes (t-037 J2's Popen stand-in), for J3's run_agent test

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
        dump({"version": 1, "rules": []}, os.path.join(self.dir, "rules.json"))
        dump({"everyMinutes": 5}, os.path.join(self.dir, "schedule.json"))

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



# ---- t-041 J3: settings and net (R5, R12, R13; AC5.1, AC5.2, AC9.1 second half, AC12.1, AC13.1) -------------------

V300, V250, V200 = (2, 1, 300), (2, 1, 250), (2, 1, 200)
EVENTS = ("PreCompact", "SessionStart", "PostCompact")


class J3Base(Base):
    def setUp(self):
        super().setUp()
        agent = load(os.path.join(self.dir, "agent.json"))
        agent.update(sessionId=SID, name="S")
        dump(agent, os.path.join(self.dir, "agent.json"))
        self.agent = agent
        self.saved_j3 = {k: getattr(pa, k) for k in ("probe_version", "VERSION_TIMEOUT", "profile_for_terminal",
                                                     "read_standing", "costs_module")}
        self.env_j3 = {k: os.environ.get(k) for k in ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "PA_NO_CC_STATUSLINE")}
        for k in self.env_j3:
            os.environ.pop(k, None)
        # pa_costs imports pa: as in a real run (pa.py is __main__ there), this module must be the one it gets
        self.mod_j3 = sys.modules.get("pa")
        sys.modules["pa"] = pa
        pa._COSTS.clear()

    def tearDown(self):
        for k, v in self.saved_j3.items():
            setattr(pa, k, v)
        pa._COSTS.clear()
        if self.mod_j3 is None:
            sys.modules.pop("pa", None)
        else:
            sys.modules["pa"] = self.mod_j3
        for k, v in self.env_j3.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for k in DROP_ENV:
            os.environ.pop(k, None)
        super().tearDown()

    def profile(self, status_line=False):
        value = {"name": "test", "claude": ["true"], "codex": ["true"]}
        if status_line:
            value["statusLine"] = {"command": ["true", "statusline"], "refreshInterval": 30}
        dump(value, os.path.join(pa.ROOT, "profiles", "test", "profile.json"))
        return pa.load_profile("test")

    def context(self, **value):
        dump(dict({"v": 1}, **value), os.path.join(self.dir, "context.json"))

    def launch(self, version=V300, resume=False, p=None):
        argv, _model = pa.launch_command(p or self.profile(), dict(self.agent), resume, self.dir, version=version)
        return argv

    def settings(self, argv):
        n = sum(1 for a in argv if a == "--settings" or a.startswith("--settings="))
        self.assertEqual(n, 1, argv)
        self.assertEqual(argv[-2], "--settings")
        return json.loads(argv[-1])

    def net(self, argv):
        at = [i for i, a in enumerate(argv) if a == "--autocompact"]
        self.assertLessEqual(len(at), 1, argv)
        return int(argv[at[0] + 1]) if at else None

    def cli(self, *argv, stdin=None):
        saved = sys.stdin
        if stdin is not None:
            sys.stdin = io.TextIOWrapper(io.BytesIO(stdin if isinstance(stdin, bytes) else stdin.encode()))
        try:
            return self.run_cmd("pa-context", *argv, main=pa.main)
        finally:
            sys.stdin = saved

    def records(self):
        path = os.path.join(self.dir, "costs", "sessions.jsonl")
        if not os.path.isfile(path):
            return []
        with open(path) as f:
            return [json.loads(l) for l in f if l.strip()]


class Version(J3Base):
    def test_v1_parse(self):
        self.assertEqual(pa.parse_cc_version("2.1.300 (Claude Code)\n"), V300)
        self.assertEqual(pa.parse_cc_version("my-wrapper 4.2.0\n2.1.250 (Claude Code)\n"), V250)
        self.assertEqual(pa.parse_cc_version("2.1.221\n"), (2, 1, 221))
        for text in ("", "Claude Code", "v2", "my-wrapper 4.2.0\n", "error: 2.1.300 not found\n", None, b"2.1.300"):
            self.assertIsNone(pa.parse_cc_version(text), text)

    def test_v2_grid(self):  # AC12.1
        for version, net, idle in ((V200, None, False), (V250, 400000, False), (V300, 400000, True), (None, None, False)):
            with self.subTest(version=version):
                argv = self.launch(version)
                self.assertEqual(self.net(argv), net)
                js = self.settings(argv)
                self.assertEqual("idleCompaction" in js, idle)
                if idle:
                    self.assertIs(js["idleCompaction"], False)
                self.assertEqual(sorted(js["hooks"]), sorted(EVENTS))  # hooks need no version
                self.assertEqual(js["promptCacheTtl"], "1h")

    def test_v3_probe_never_raises(self):
        self.assertIsNone(pa.probe_version(self.profile()))  # `true --version`: no output
        pa.VERSION_TIMEOUT = 0.5
        slow = dict(self.profile(), claude=["sh", "-c", "sleep 5; echo '2.1.300 (Claude Code)'", "x"])
        began = pa.time.time()
        self.assertIsNone(pa.probe_version(slow))
        self.assertLess(pa.time.time() - began, 4)
        self.assertIsNone(pa.probe_version(dict(self.profile(), claude=[os.path.join(TMP, "no-such-claude")])))
        ok = dict(self.profile(), claude=["sh", "-c", "echo '2.1.290 (Claude Code)'", "x"])
        self.assertEqual(pa.probe_version(ok), (2, 1, 290))

    def test_v4_probed_once_per_command(self):
        seen = []
        pa.probe_version = lambda p: seen.append(tuple(p["claude"])) or V300
        ctx, p = {}, self.profile()
        self.assertEqual(pa.cc_version(p, ctx), V300)
        self.assertEqual(pa.cc_version(p, ctx), V300)
        self.assertEqual(pa.cc_version(dict(p, claude=["other"]), ctx), V300)
        self.assertEqual(seen, [("true",), ("other",)])

    def test_v5_boundaries(self):
        self.assertTrue(pa.version_at_least((2, 1, 221), pa.AUTOCOMPACT_SINCE))
        self.assertFalse(pa.version_at_least((2, 1, 220), pa.AUTOCOMPACT_SINCE))
        self.assertTrue(pa.version_at_least((2, 1, 290), pa.IDLE_COMPACTION_SINCE))
        self.assertFalse(pa.version_at_least((2, 1, 289), pa.IDLE_COMPACTION_SINCE))
        self.assertTrue(pa.version_at_least((3, 0, 0), pa.IDLE_COMPACTION_SINCE))
        self.assertFalse(pa.version_at_least(None, pa.AUTOCOMPACT_SINCE))
        self.assertEqual(pa.VERSION_TIMEOUT, 10)


class Launch(J3Base):
    def test_l1_one_settings_with_the_hooks_and_the_net(self):  # AC5.1
        for status_line in (False, True):
            argv = self.launch(V300, p=self.profile(status_line))
            self.assertEqual(self.net(argv), 400000)
            js = self.settings(argv)
            self.assertEqual(sorted(js), sorted(["promptCacheTtl", "idleCompaction", "hooks"] + (["statusLine"] if status_line else [])))
            hooks = js["hooks"]
            self.assertEqual(sorted(hooks), sorted(EVENTS))
            self.assertNotIn("matcher", hooks["PreCompact"][0])  # manual and auto
            self.assertEqual(hooks["SessionStart"][0]["matcher"], "compact")
            self.assertNotIn("matcher", hooks["PostCompact"][0])
            for event, sub in zip(EVENTS, ("compact-instructions", "start-pack", "compacted")):
                (entry,) = hooks[event]
                (h,) = entry["hooks"]
                self.assertEqual(h["type"], "command")
                self.assertEqual(h["timeout"], 10)
                words = pa.shlex.split(h["command"])
                self.assertEqual(words, [sys.executable, pa.PA_FILE, "context", sub, "--space", "sp1"])

    def test_l2_new_session_and_resume_alike(self):
        for resume in (False, True):
            argv = self.launch(V300, resume=resume)
            self.assertEqual(self.net(argv), 400000)
            i = argv.index("--autocompact")
            if resume:  # the flag comes before the resume prompt (the last positional before --settings)
                self.assertLess(i, len(argv) - 3)
                self.assertTrue(argv[-3].startswith("Your session was restarted."))
            self.assertIn("hooks", self.settings(argv))

    def test_l3_net_from_context_json(self):
        self.context(mode="fresh", net=500000)
        self.assertEqual(self.net(self.launch(V300)), 500000)

    def test_l4_a_worker_gets_nothing_of_it(self):
        self.context(mode="fresh")
        seen = []

        class Stop(Exception):
            pass

        def fake_execv(path, args):
            seen.append(list(args))
            raise Stop()
        p = self.profile(True)
        pa.profile_for_terminal = lambda explicit=None, walked=None: (p, "sp1")
        saved = os.execv
        os.execv = fake_execv
        try:
            with self.assertRaises(Stop), contextlib.redirect_stdout(io.StringIO()):
                pa.cmd_launch("claude", ["job"])
        finally:
            os.execv = saved
        text = " ".join(seen[0])
        for word in ("--autocompact", "hooks", "idleCompaction", "compact-instructions", "promptCacheTtl"):
            self.assertNotIn(word, text)

    def test_l5_no_environment_variable(self):
        self.launch(V300)
        for k in os.environ:
            self.assertFalse(k.startswith("CLAUDE_CODE_AUTO_COMPACT"), k)
        self.assertNotIn("CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS", os.environ)

    def test_l6_the_hook_commands_run(self):
        write(os.path.join(self.dir, "standing.md"), SEED)
        hooks = self.settings(self.launch(V300))["hooks"]
        env = dict(os.environ, HERDR_PA_HOME=pa.ROOT, HERDR_APP_HOME=pa.APP, CLAUDE_CONFIG_DIR=os.path.join(TMP, "claude"),
                   HOME=os.path.join(TMP, "home"))
        stdin = {"PreCompact": {"session_id": SID, "hook_event_name": "PreCompact", "trigger": "auto", "custom_instructions": None},
                 "SessionStart": {"session_id": SID, "hook_event_name": "SessionStart", "source": "compact"},
                 "PostCompact": {"session_id": SID, "hook_event_name": "PostCompact", "trigger": "auto", "compact_summary": "S"}}
        out = {}
        for event in EVENTS:
            r = pa.subprocess.run(hooks[event][0]["hooks"][0]["command"], shell=True, env=env, input=json.dumps(stdin[event]).encode(),
                                  stdout=pa.subprocess.PIPE, stderr=pa.subprocess.PIPE, timeout=30)
            self.assertEqual(r.returncode, 0, (event, r.stderr))
            out[event] = r.stdout.decode()
        self.assertIn("the next step per task", out["PreCompact"])
        self.assertIn(SEED, out["SessionStart"])
        self.assertEqual([(r["ev"], r["trigger"], r["session"]) for r in self.records()], [("compact", "auto", SID)])

    def test_l7_session_log_says_what_the_start_asked_for(self):
        self.context(mode="fresh", net=999)  # invalid: the defaults, and one line saying so
        pa.probe_version = lambda p: None
        calls = []

        class Stop(Exception):
            pass

        def fake_call(argv, cwd=None):
            calls.append(list(argv))
            raise Stop()
        import child_fakes  # noqa: E402
        saved = (pa.subprocess.call, pa.time.sleep)
        pa.subprocess.call, pa.time.sleep = fake_call, (lambda s: None)
        undo = child_fakes.patch(pa, fake_call)
        ctx = {"beat": True, "previous": None}
        try:
            for _ in range(2):
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(Stop):
                    pa.run_agent(argparse.Namespace(fresh=True), "t0", os.path.join(TMP, "proj"), self.profile(), self.dir,
                                 load(os.path.join(self.dir, "agent.json")), ctx, pa.time.time())
        finally:
            undo()
            pa.subprocess.call, pa.time.sleep = saved
        with open(os.path.join(self.dir, "session.log")) as f:
            log = f.read()
        start = [l for l in log.splitlines() if " start " in l][-1].split()
        self.assertIn("net=none", start)  # version unknown: no --autocompact
        self.assertIn("idle=kept", start)
        self.assertEqual(log.count("context.json ignored"), 1, log)  # once per launcher process
        self.assertEqual(log.count("claude --version not known"), 1, log)
        self.assertNotIn("--autocompact", calls[0])
        self.assertIn("hooks", json.loads(calls[0][-1]))


class Settings(J3Base):
    def test_k1_off_drops_everything_j3_adds(self):  # AC13.1, the back-out
        base = {}
        for status_line in (False, True):
            self.context(mode="off")
            for version in (V200, V250, V300, None):
                argv = self.launch(version, p=self.profile(status_line))
                self.assertIsNone(self.net(argv))
                js = self.settings(argv)
                self.assertEqual(sorted(js), sorted(["promptCacheTtl"] + (["statusLine"] if status_line else [])))
                base.setdefault(status_line, argv)
                self.assertEqual(argv, base[status_line])

    def test_k2_missing_file_is_the_defaults(self):
        settings, problem = pa.read_context(self.dir)
        self.assertIsNone(problem)
        self.assertEqual({k: settings[k] for k in ("mode", "taskCap", "passCap", "net")},
                         {"mode": "fresh", "taskCap": 150000, "passCap": 250000, "net": 400000})
        self.assertEqual(pa.read_context(None)[0]["mode"], "fresh")

    def test_k3_invalid_file_is_ignored(self):
        cases = ["{not json", json.dumps({"v": 1, "mode": "x"}), json.dumps({"v": 1, "net": 50000}),
                 json.dumps({"v": 1, "taskCap": 300000, "passCap": 250000}), json.dumps([1]), json.dumps({"v": 2}),
                 json.dumps({"v": 1, "net": True}), json.dumps({"v": 1, "by": "worker"}), json.dumps({"v": 1, "enabledAt": "soon"})]
        for text in cases:
            with self.subTest(text=text):
                write(os.path.join(self.dir, "context.json"), text)
                settings, problem = pa.read_context(self.dir)
                self.assertTrue(problem)
                self.assertEqual(settings["net"], 400000)
                self.assertEqual(settings["mode"], "fresh")
                self.assertEqual(self.net(self.launch(V300)), 400000)
        self.assertEqual(pa.context_problems({"v": 1, "mode": "off", "taskCap": 100000, "passCap": 100000, "net": 900000,
                                              "enabledAt": None, "by": "panel"}), [])

    def test_k4_refused_values(self):
        self.context(mode="fresh", net=450000)
        before = open(os.path.join(self.dir, "context.json")).read()
        for argv in (["--net", "50000"], ["--net", "950k"], ["--task-cap", "300k", "--pass-cap", "250k"],
                     ["--net", "4e5"], ["--net", "-400000"], ["--mode", "on"], ["--net", "200k"]):
            with self.subTest(argv=argv):
                code, out, err = self.cli("set", "--space", "sp1", *argv)
                self.assertEqual(code, 2, (out, err))
                self.assertEqual(open(os.path.join(self.dir, "context.json")).read(), before)

    def test_k5_enabled_at_and_by(self):
        code, out, err = self.cli("set", "--space", "sp1", "--mode", "off")
        self.assertEqual(code, 0, err)
        js = load(os.path.join(self.dir, "context.json"))
        self.assertEqual((js["mode"], js["by"], js["v"]), ("off", "person", 1))
        self.assertIsNone(js.get("enabledAt"))
        self.cli("set", "--space", "sp1", "--mode", "fresh")
        js = load(os.path.join(self.dir, "context.json"))
        self.assertEqual(js["mode"], "fresh")
        self.assertIsNotNone(pa.parse_time(js["enabledAt"]))
        js["enabledAt"] = "2026-11-18T00:00:00Z"
        dump(js, os.path.join(self.dir, "context.json"))
        code, out, err = self.cli("set", "--space", "sp1", "--net", "450k")
        self.assertEqual(code, 0, err)
        js = load(os.path.join(self.dir, "context.json"))
        self.assertEqual((js["enabledAt"], js["net"], js["mode"]), ("2026-11-18T00:00:00Z", 450000, "fresh"))
        self.assertIn("next start", out)
        os.remove(os.path.join(self.dir, "context.json"))
        self.cli("set", "--space", "sp1", "--net", "500k")  # from no file: the mode becomes fresh now
        self.assertIsNotNone(pa.parse_time(load(os.path.join(self.dir, "context.json"))["enabledAt"]))

    def test_k6_refused_inside_an_agent(self):
        for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_SANDBOX", "CODEX_HOME_SESSION"):
            with self.subTest(k=k):
                os.environ[k] = "1"
                try:
                    code, out, err = self.cli("set", "--space", "sp1", "--mode", "off")
                finally:
                    os.environ.pop(k)
                self.assertEqual(code, 2)
                self.assertIn("person", err)
                self.assertFalse(os.path.exists(os.path.join(self.dir, "context.json")))

    def test_k7_no_option_shows_and_writes_nothing(self):
        code, out, err = self.cli("set", "--space", "sp1")
        self.assertEqual(code, 0, err)
        self.assertIn("400,000", out)
        self.assertIn("fresh", out)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "context.json")))

    def test_k8_k_suffix(self):
        self.assertEqual(pa.parse_amount("400k"), 400000)
        self.assertEqual(pa.parse_amount("400K"), 400000)
        self.assertEqual(pa.parse_amount("400000"), 400000)
        for bad in ("400 k", "4e5", "", "k", "1.5k", "-1"):
            with self.assertRaises(pa.Problem):
                pa.parse_amount(bad)


class Hooks(J3Base):
    def test_p1_compact_instructions(self):
        code, out, err = self.cli("compact-instructions", "--space", "sp1", stdin="{}")
        self.assertEqual(code, 0, err)
        for words in ("open asks", "the person's own words", "address", "the next step per task", "hold"):
            self.assertIn(words, out)
        self.assertLess(len(out), 10000)

    def test_p2_compacted_records_ids_only(self):
        code, out, err = self.cli("compacted", "--space", "sp1",
                                  stdin=json.dumps({"session_id": "s1", "trigger": "auto", "compact_summary": "SECRET SUMMARY"}))
        self.assertEqual(code, 0, err)
        (rec,) = self.records()
        self.assertEqual(sorted(rec), ["at", "ev", "session", "trigger", "v"])
        self.assertEqual((rec["v"], rec["ev"], rec["trigger"], rec["session"]), (1, "compact", "auto", "s1"))
        self.assertIsNotNone(pa.parse_time(rec["at"]))
        with open(os.path.join(self.dir, "costs", "sessions.jsonl")) as f:
            self.assertNotIn("SECRET", f.read())

    def test_p3_compacted_with_bad_stdin(self):
        for stdin in ("not json", "", json.dumps({"session_id": 5, "trigger": "sometimes"}), b"\xff\xfe"):
            code, out, err = self.cli("compacted", "--space", "sp1", stdin=stdin)
            self.assertEqual(code, 0, err)
        recs = self.records()
        self.assertEqual(len(recs), 4)
        for rec in recs:
            self.assertEqual((rec["trigger"], rec["session"]), (None, None))

    def test_p4_start_pack_inlines_standing_verbatim(self):  # AC9.1 second half
        text = SEED.replace("# Standing words\n", '# Standing words\n- 2026-11-18 "keep replies short" (all; until said)\n')
        write(os.path.join(self.dir, "standing.md"), text)
        self.ledger(dict(self.ledger(), context=CHECKPOINT))
        code, out, err = self.cli("start-pack", "--space", "sp1", stdin="{}")
        self.assertEqual(code, 0, err)
        self.assertIn(text, out)
        self.assertIn("checkpoint: pass 12 at 2026-11-18T10:00:00Z, clean, boundary task, session aaaaaaaa", out)
        self.assertTrue(out.startswith("Your context was compacted."))
        self.assertIn("After a restart or a compaction", out)
        self.assertIn("If the last hold or resume line in the journal is a hold, start nothing.", out)
        self.assertEqual(pa.start_pack(self.dir, "HEAD."), "HEAD." + out[len("Your context was compacted."):].rstrip("\n"))

    def test_p5_full_size_standing_fits(self):
        entry = '- 2026-11-18 "' + "x" * 80 + '"\n'
        text = SEED
        while len(text) + len(entry) <= pa.STANDING_CAP:
            text = text.replace("# Hold\n", entry + "# Hold\n", 1)
        self.assertLessEqual(len(text), pa.STANDING_CAP)
        self.assertGreater(len(text), pa.STANDING_CAP - 100)
        write(os.path.join(self.dir, "standing.md"), text)
        self.ledger(dict(self.ledger(), context=dict(CHECKPOINT, clean=False, boundary=None, why="y" * 500)))
        pack = pa.start_pack(self.dir, "Your context was compacted.")
        self.assertIn(text, pack)
        self.assertLessEqual(len(pack), pa.START_PACK_MAX)

    def test_p6_standing_missing_or_over_the_cap(self):
        code, out, err = self.cli("start-pack", "--space", "sp1")
        self.assertEqual(code, 0, err)
        self.assertIn("standing.md: missing", out)
        self.assertIn("checkpoint: none yet", out)
        write(os.path.join(self.dir, "standing.md"), SEED + "- 2026-11-18 " + "z" * 7000 + "\n")
        pack = pa.start_pack(self.dir, "H.")
        self.assertIn("(cut: over the cap)", pack)
        self.assertIn("standing.md: over the cap", pack)
        self.assertLessEqual(len(pack), pa.START_PACK_MAX)
        write(os.path.join(self.dir, "standing.md"), "z" * 30000)
        pack = pa.start_pack(self.dir, "H.")
        self.assertIn("standing.md: over the cap", pack)
        self.assertLessEqual(len(pack), pa.START_PACK_MAX)

    def test_p7_unreadable_or_unknown_state_folder(self):
        for space in ("nope", "../x", "-x"):
            for sub in ("compact-instructions", "start-pack", "compacted"):
                with self.subTest(space=space, sub=sub):
                    code, out, err = self.cli(sub, "--space=" + space, stdin="{}")
                    self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(os.path.join(pa.ROOT, "spaces", "nope")))
        write(os.path.join(self.dir, "standing.md"), SEED)
        os.chmod(self.dir, 0)
        try:
            for sub in ("compact-instructions", "start-pack", "compacted"):
                code, out, err = self.cli(sub, "--space", "sp1", stdin="{}")
                self.assertEqual(code, 0, (sub, err))
                if sub == "start-pack":
                    self.assertIn("Your state is on disk.", out)
        finally:
            os.chmod(self.dir, 0o755)

    def test_p8_doctor_checkpoint_line_unchanged(self):
        for ledger, line in (({"context": CHECKPOINT}, "checkpoint: pass 12 at 2026-11-18T10:00:00Z, clean, boundary task, session aaaaaaaa"),
                             ({}, "checkpoint: none yet"), (None, "checkpoint: none yet"),
                             ({"context": dict(CHECKPOINT, boundary="phase")}, "checkpoint malformed: boundary must be task, pass or null")):
            self.assertEqual(pa.checkpoint_line(ledger), line)
            self.assertIn(line, pa.context_notes(self.dir, ledger, environ={}))

    def test_p9_never_exits_2(self):
        def boom(*a, **k):
            raise RuntimeError("broken on purpose")
        pa.read_standing = boom
        pa.costs_module = boom
        for sub in ("compact-instructions", "start-pack", "compacted"):
            code, out, err = self.cli(sub, "--space", "sp1", stdin="{}")
            self.assertEqual(code, 0, (sub, err))
        code, out, err = self.cli("start-pack", "--space", "sp1", stdin="{}")
        self.assertIn("Your state is on disk.", out)


if __name__ == "__main__":
    unittest.main()
