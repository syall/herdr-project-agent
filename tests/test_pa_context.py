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
                # round 2 (REVIEW MF-J3-1): the shell-form line ends in `|| true`, so the line itself never exits 2
                self.assertEqual(words, [sys.executable, pa.PA_FILE, "context", sub, "--space", "sp1", "||", "true"])

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

    def run_line(self, sub):
        """One hook command line as Claude Code runs it (shell form), with the given PA_FILE in force."""
        line = pa.hook_command("sp1", sub)
        env = dict(os.environ, HERDR_PA_HOME=pa.ROOT, HERDR_APP_HOME=pa.APP, CLAUDE_CONFIG_DIR=os.path.join(TMP, "claude"),
                   HOME=os.path.join(TMP, "home"))
        return pa.subprocess.run(line, shell=True, env=env, input=b"{}", stdout=pa.subprocess.PIPE,
                                 stderr=pa.subprocess.PIPE, timeout=30)

    def test_p10_missing_pa_file_never_exits_2(self):  # REVIEW MF-J3-1 (a): CPython exits 2 on a missing script
        saved = pa.PA_FILE
        pa.PA_FILE = os.path.join(TMP, "gone", "pa.py")
        try:
            for sub in ("compact-instructions", "start-pack", "compacted"):
                r = self.run_line(sub)
                self.assertEqual(r.returncode, 0, (sub, r.stderr))
                self.assertIn(b"gone", r.stderr)  # the interpreter did fail: the line, not pa.py, gives the 0
        finally:
            pa.PA_FILE = saved

    def test_p11_pa_py_without_pa_context_never_exits_2(self):  # REVIEW MF-J3-1 (b): a pre-J3 pa.py exits 2
        folder = os.path.join(TMP, "pre-j3")
        os.makedirs(folder, exist_ok=True)
        old = os.path.join(folder, "pa.py")
        write(old, "import sys\nsys.stderr.write('pa: invoke as pa-session, pa-task, ...\\n')\nsys.exit(2)\n")
        saved = pa.PA_FILE
        pa.PA_FILE = old
        try:
            for sub in ("compact-instructions", "start-pack", "compacted"):
                r = self.run_line(sub)
                self.assertEqual(r.returncode, 0, (sub, r.stderr))
                self.assertIn(b"invoke as", r.stderr)
        finally:
            pa.PA_FILE = saved


# ---- t-041 J4: the fresh reset (R1-R4, R6, R10, R11, R13's resets part, R16 records, R17 script) ---------------

import signal  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402


def iso_ms(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".{:03d}Z".format(int((t % 1) * 1000))


def t_user(t, text="hello", **kw):
    return dict({"type": "user", "timestamp": iso_ms(t), "message": {"role": "user", "content": text}}, **kw)


def t_tool_result(t):
    return {"type": "user", "timestamp": iso_ms(t), "toolUseResult": {"ok": True},
            "message": {"role": "user", "content": [{"type": "tool_result", "content": "x"}]}}


def t_assistant(t, i=1234, cw=50000, cr=200000, stop="end_turn", **kw):
    return dict({"type": "assistant", "timestamp": iso_ms(t), "message": {
        "role": "assistant", "stop_reason": stop,
        "usage": {"input_tokens": i, "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr, "output_tokens": 9}}},
        **kw)


class FakeChild:
    def __init__(self, obeys_term=True):
        self.signals, self.obeys, self.returncode = [], obeys_term, None

    def send_signal(self, sig):
        self.signals.append(sig)

    def kill(self):
        self.send_signal(signal.SIGKILL)

    def terminate(self):
        self.send_signal(signal.SIGTERM)

    def wait(self, timeout=None):
        if signal.SIGKILL in self.signals:
            self.returncode = -9
        elif signal.SIGTERM in self.signals and self.obeys:
            self.returncode = 143
        elif timeout is not None:
            raise subprocess.TimeoutExpired("claude", timeout)
        return self.returncode

    def poll(self):
        return self.returncode


class J4Base(J3Base):
    """A space at a clean point: an idle PA whose last turn (a user entry, then the assistant's answer) ended 95 s
    ago, its clean checkpoint written in that turn, standing.md seeded, no card, no worker, no restart request."""

    def setUp(self):
        super().setUp()
        self.saved_j4 = {k: getattr(pa, k) for k in ("relay", "CONTEXT_COPIES_MAX", "END_GRACE")}
        self.project = os.path.join(TMP, "proj")
        os.makedirs(self.project, exist_ok=True)
        self.now = time.time()
        self.base = self.now - 100
        self.agent.update(project=self.project, sessionStarted=True)
        dump(self.agent, os.path.join(self.dir, "agent.json"))
        write(os.path.join(self.dir, "standing.md"), SEED)
        self.transcript([t_user(self.base), t_assistant(self.base + 5, i=0, cw=0, cr=90000)])  # below the caps
        self.checkpoint()
        self.terms["t0"] = {"state": "live", "status": {"state": "idle"}}
        dump({"version": 2, "accepts": ["restart", "context"], "instance": "i1"}, os.path.join(self.dir, "launcher.json"))
        pa.probe_version = lambda p: V300

    def tearDown(self):
        for k, v in self.saved_j4.items():
            setattr(pa, k, v)
        super().tearDown()

    def tpath(self, sid=SID):
        return os.path.join(pa.CLAUDE_HOME, "projects", pa.claude_slug(self.project), sid + ".jsonl")

    def transcript(self, entries, sid=SID):
        write(self.tpath(sid), "".join(json.dumps(e) + "\n" for e in entries))

    def checkpoint(self, **change):
        value = dict(CHECKPOINT, at=pa.iso_time(self.base + 10), session=SID, boundary="pass")
        value.update(change)
        self.ledger(dict(self.ledger(), context=value))

    def size(self):
        return pa.context_size(self.project, SID)

    def ctx(self, **kw):
        size = self.size()
        value = {"idleSince": self.now - 60, "idleKey": size["key"] if size else None, "project": self.project,
                 "directory": self.dir, "running": {"since": self.base - 50, "session": SID, "marked": True},
                 "child": FakeChild()}
        value.update(kw)
        return value

    def clean(self, ctx=None, size="read"):
        return pa.clean_point(ctx or self.ctx(), self.dir, load(os.path.join(self.dir, "agent.json")),
                              self.size() if size == "read" else size, self.now, "t0")

    def words(self, reasons):
        return " | ".join(text for _letter, text in reasons)

    def once(self, ctx, at=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            pa.context_once(ctx, self.dir, "t0", "sp1", at=self.now if at is None else at)
        return out.getvalue()

    def big(self, ctx_tokens=260000):
        self.transcript([t_user(self.base), t_assistant(self.base + 5, i=0, cw=0, cr=ctx_tokens)])

    def log(self):
        path = os.path.join(self.dir, "session.log")
        return read_text_or_empty(path)

    def request(self, **change):
        at = time.time()
        value = {"v": 1, "id": "req-1", "space": "sp1", "by": "person", "why": None, "at": pa.iso_time(at),
                 "expires": pa.iso_time(at + 7200)}
        value.update(change)
        dump(value, os.path.join(self.dir, "context-request.json"))
        return value


def read_text_or_empty(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


class J4Size(J4Base):  # R1, AC1.1
    def test_z1_last_assistant_usage(self):
        self.transcript([t_user(self.base), t_assistant(self.base + 5)])
        self.assertEqual(self.size()["ctx"], 251234)
        self.assertEqual(self.size()["source"], "transcript")

    def test_z2_sidechain_ignored(self):
        self.transcript([t_user(self.base), t_assistant(self.base + 5),
                         t_assistant(self.base + 6, i=0, cw=0, cr=900000, isSidechain=True)])
        self.assertEqual(self.size()["ctx"], 251234)

    def test_z3_missing_unreadable_empty_garbage(self):
        self.assertIsNone(pa.context_size(self.project, "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"))
        write(self.tpath(), "")
        self.assertIsNone(self.size())
        write(self.tpath(), "not json\n{\n[1,2]\n")
        self.assertIsNone(self.size())
        self.transcript([t_user(self.base), t_assistant(self.base + 5)])
        os.chmod(self.tpath(), 0)
        try:
            if os.access(self.tpath(), os.R_OK):
                self.skipTest("running as a user who reads mode-000 files")
            self.assertIsNone(self.size())
        finally:
            os.chmod(self.tpath(), 0o644)
        self.assertIsNone(pa.context_size(None, SID))
        self.assertIsNone(pa.context_size(self.project, "../x"))

    def test_z4_only_the_tail_is_read(self):
        pad = json.dumps(t_assistant(self.base, i=0, cw=0, cr=5)) + "\n"
        with open(self.tpath(), "w") as f:
            while f.tell() < 3 * (1 << 20):
                f.write(pad)
            f.write(json.dumps(t_user(self.base + 1)) + "\n" + json.dumps(t_assistant(self.base + 5)) + "\n")
        self.assertLessEqual(len(pa.read_tail(self.tpath(), pa.TAIL_MAX)), pa.TAIL_MAX)
        self.assertEqual(pa.TAIL_MAX, 1 << 20)
        self.assertEqual(self.size()["ctx"], 251234)

    def test_z5_turn_facts(self):
        s = self.size()
        self.assertAlmostEqual(s["turnStart"], self.base, delta=0.01)
        self.assertAlmostEqual(s["lastAssistant"], self.base + 5, delta=0.01)
        self.assertFalse(s["userAfter"])
        self.assertFalse(s["midTool"])
        self.transcript([t_user(self.base), t_assistant(self.base + 5, stop="tool_use"), t_tool_result(self.base + 6)])
        s = self.size()
        self.assertTrue(s["userAfter"])
        self.assertTrue(s["midTool"])
        self.assertAlmostEqual(s["turnStart"], self.base, delta=0.01)  # a tool result starts no turn
        self.transcript([t_user(self.base), t_assistant(self.base + 5), t_user(self.base + 7, isMeta=True)])
        self.assertTrue(self.size()["userAfter"])
        self.assertAlmostEqual(self.size()["turnStart"], self.base, delta=0.01)

    def test_z6_compact_boundary_after_the_last_assistant(self):
        self.transcript([t_user(self.base), t_assistant(self.base + 5),
                         {"type": "system", "subtype": "compact_boundary", "timestamp": iso_ms(self.base + 6)}])
        self.assertIsNone(self.size())

    def test_z7_statusline_fallback_and_no_reset_on_it(self):
        os.remove(self.tpath())
        dump({"v": 1, "session": SID, "ctx": 300000, "at": pa.now()}, os.path.join(self.dir, "costs", "cc", SID + ".json"))
        s = pa.current_size(self.dir, self.project, SID)
        self.assertEqual((s["ctx"], s["source"]), (300000, "statusline"))
        ok, reasons = self.clean(size=s)
        self.assertFalse(ok)
        self.assertEqual(reasons[0][0], "a")
        dump({"v": 1, "session": "other-session-id", "ctx": 300000}, os.path.join(self.dir, "costs", "cc", SID + ".json"))
        self.assertIsNone(pa.current_size(self.dir, self.project, SID))

    def test_parse_stamp(self):
        self.assertEqual(pa.parse_stamp("2026-11-18T10:00:00Z"), pa.parse_time("2026-11-18T10:00:00Z"))
        self.assertAlmostEqual(pa.parse_stamp("2026-11-18T10:00:00.250Z"), pa.parse_time("2026-11-18T10:00:00Z") + 0.25)
        for bad in (None, "", "2026-11-18", 5, "2026-11-18T10:00:00.x"):
            self.assertIsNone(pa.parse_stamp(bad))


class J4Clean(J4Base):  # R2, AC2.1, AC2.2, AC8.2
    def test_all_hold(self):
        self.assertEqual(self.clean(), (True, []))

    def test_c1_working(self):
        self.terms["t0"]["status"] = {"state": "working"}
        ok, reasons = self.clean()
        self.assertFalse(ok)
        self.assertIn("turn working", self.words(reasons))

    def test_c2_idle_too_short(self):
        ok, reasons = self.clean(self.ctx(idleSince=self.now - 10))
        self.assertFalse(ok)
        self.assertIn("idle 10s < 20s", self.words(reasons))
        ok, reasons = self.clean(self.ctx(idleSince=None))
        self.assertFalse(ok)

    def test_c3_not_clean(self):
        self.checkpoint(clean=False, boundary=None, why="waiting for the yes")
        ok, reasons = self.clean()
        self.assertEqual([r[0] for r in reasons], ["b"])
        self.assertIn("not clean (waiting for the yes)", self.words(reasons))

    def test_c4_another_session_accepted_by_its_time(self):  # t-041 J7-R2 (d-274 Q2): written in this turn
        self.checkpoint(session="ffffffff-eeee-dddd-cccc-bbbbbbbbbbbb")
        self.assertEqual(self.clean(), (True, []))

    def test_c4c_another_session_before_the_turn(self):  # J7-R2: still refused, both ids named
        self.checkpoint(session="ffffffff-eeee-dddd-cccc-bbbbbbbbbbbb", at=pa.iso_time(self.base - 30))
        ok, reasons = self.clean()
        self.assertEqual(([r[0] for r in reasons], ok), (["b"], False))
        self.assertIn("checkpoint of another session (ffffffff not aaaaaaaa)", self.words(reasons))

    def test_c4b_null_session_accepted_by_the_times(self):  # Q3
        self.checkpoint(session=None)
        self.assertEqual(self.clean(), (True, []))
        self.checkpoint(session=None, at=pa.iso_time(self.base - 80))  # before this session began
        ok, reasons = self.clean()
        self.assertFalse(ok)

    def test_c5_older_than_the_turn(self):
        self.checkpoint(at=pa.iso_time(self.base - 30))
        ok, reasons = self.clean()
        self.assertEqual([r[0] for r in reasons], ["b"])
        self.assertIn("older than the turn", self.words(reasons))
        self.ledger({"version": 1, "passCount": 12, "tasks": []})
        ok, reasons = self.clean()
        self.assertIn("checkpoint none", self.words(reasons))
        self.checkpoint(clean="yes")
        self.assertIn("checkpoint malformed", self.words(self.clean()[1]))

    def card(self, cid, answered):
        dump({"id": cid, "status": "answered", "question": "q", "options": ["a"], "answer": "a", "answered": answered},
             os.path.join(self.dir, "decisions", cid + ".json"))

    def test_c6_answer_not_acted_on(self):
        self.card("d-007", pa.iso_time(self.base - 20))
        ok, reasons = self.clean()
        self.assertEqual([r[0] for r in reasons], ["c"])
        self.assertIn("card d-007 answered, not acted on", self.words(reasons))
        write(os.path.join(self.dir, "journal.jsonl"), json.dumps({"at": pa.iso_time(self.base - 40), "why": "d-007"}) + "\n"
              + json.dumps({"at": pa.iso_time(self.base - 10), "why": "opened d-0071"}) + "\n")
        self.assertFalse(self.clean()[0])  # one line before the answer, one naming d-0071

    def test_c6b_acted_on(self):
        self.card("d-007", pa.iso_time(self.base - 20))
        write(os.path.join(self.dir, "journal.jsonl"),
              json.dumps({"at": pa.iso_time(self.base - 10), "why": "acting on d-007's answer"}) + "\n")
        self.assertEqual(self.clean(), (True, []))
        open_card = {"id": "d-008", "status": "open", "question": "q", "options": ["a"]}
        dump(open_card, os.path.join(self.dir, "decisions", "d-008.json"))
        self.assertEqual(self.clean(), (True, []))

    def test_c7_live_worker_without_a_handoff(self):  # AC8.2
        self.ledger(dict(self.ledger(), tasks=[{"id": "t-1", "workers": [
            {"address": "t5", "name": "t-1-build"}, {"address": "t6", "name": "t-1-verify", "finished": True},
            {"address": "t7", "name": "t-1-review"}]}]))
        self.terms.update(t5={"state": "live"}, t6={"state": "live"}, t7={"state": "finished"})
        ok, reasons = self.clean()
        self.assertEqual([r[0] for r in reasons], ["d"])
        self.assertIn("worker t5 (t-1 build) has no handoff file", self.words(reasons))
        write(os.path.join(self.dir, "evidence", "t-1", "handoff-build.md"), "Do the job.\n")
        self.assertEqual(self.clean(), (True, []))

    def test_c7c_terminal_list_unavailable(self):  # REVIEW J4 MF-J4-2: (d) refuses when hc ls fails
        self.ledger(dict(self.ledger(), tasks=[{"id": "t-1", "workers": [{"address": "t5", "name": "t-1-build"}]}]))
        self.terms["t5"] = {"state": "live"}
        pa.live_terminals = lambda: None
        ok, reasons = self.clean()
        self.assertFalse(ok)
        self.assertIn(("d", "terminal list unavailable"), reasons)
        self.ledger(dict(self.ledger(), tasks=[]))  # no worker at all: still unknown, still refused
        self.assertIn(("d", "terminal list unavailable"), self.clean()[1])

    def test_c7b_a_worker_whose_name_names_no_job(self):  # Q4: refused
        self.ledger(dict(self.ledger(), tasks=[{"id": "t-1", "workers": [{"address": "t5", "name": "helper"}]}]))
        self.terms["t5"] = {"state": "live"}
        ok, reasons = self.clean()
        self.assertEqual([r[0] for r in reasons], ["d"])

    def test_c8_standing(self):
        os.remove(os.path.join(self.dir, "standing.md"))
        self.assertEqual([r[0] for r in self.clean()[1]], ["e"])
        write(os.path.join(self.dir, "standing.md"), SEED + "- 2026-11-18 " + "x" * 6000 + "\n")
        self.assertEqual([r[0] for r in self.clean()[1]], ["e"])
        write(os.path.join(self.dir, "standing.md"), SEED.replace("# Hold\n", ""))
        self.assertIn("standing.md", self.words(self.clean()[1]))

    def test_c9_restart_pending_and_a_recent_reset(self):
        write(os.path.join(self.dir, pa.REQUEST), "{}")
        ok, reasons = self.clean()
        self.assertEqual([r[0] for r in reasons], ["f"])
        self.assertIn("restart request pending", self.words(reasons))
        os.remove(os.path.join(self.dir, pa.REQUEST))
        ok, reasons = self.clean(self.ctx(lastResetAt=self.now - 300))
        self.assertIn("reset 300s ago", self.words(reasons))
        self.assertEqual(self.clean(self.ctx(lastResetAt=self.now - 700)), (True, []))

    def test_c10_c12_never_mid_turn(self):  # AC2.2
        self.big(800000)
        for status in ("working", "blocked"):
            self.terms["t0"]["status"] = {"state": status}
            self.assertFalse(self.clean()[0])
        self.terms["t0"]["status"] = {"state": "idle"}
        self.transcript([t_user(self.base), t_assistant(self.base + 5, i=0, cw=0, cr=800000), t_user(self.base + 8, "more")])
        ok, reasons = self.clean()
        self.assertFalse(ok)
        self.assertIn("message waiting", self.words(reasons))

    def test_logged_once_per_idle_period(self):
        self.big()
        self.card("d-007", pa.iso_time(self.base - 20))
        ctx = self.ctx()
        self.once(ctx)
        self.once(ctx, self.now + 10)
        self.assertEqual(self.log().count("context not reset"), 1, self.log())
        self.assertIn("card d-007 answered, not acted on", self.log())
        self.assertIsNone(ctx.get("after"))


class J4Reason(J4Base):  # R3, AC3.1
    def test_g1_grid(self):
        settings = pa.read_context(self.dir)[0]
        for n in (149999, 150000, 249999, 250000, 260000):
            for boundary in ("task", "pass", None):
                with self.subTest(n=n, boundary=boundary):
                    want = ("task-cap" if n >= 150000 and boundary == "task" else "pass-cap" if n >= 250000 else None)
                    self.assertEqual(pa.reset_reason({"ctx": n}, {"boundary": boundary}, settings, None), want)
        self.assertIsNone(pa.reset_reason(None, {"boundary": "task"}, settings, None))
        self.assertEqual(pa.reset_reason({"ctx": 300000}, None, settings, None), "pass-cap")

    def test_g2_mode_and_caps(self):
        self.context(mode="off")
        settings = pa.read_context(self.dir)[0]
        self.assertIsNone(pa.reset_reason({"ctx": 800000}, {"boundary": "task"}, settings, None))
        self.assertIsNone(pa.reset_reason({"ctx": 800000}, {"boundary": "task"}, settings, {"by": "person"}))
        self.context(mode="fresh", taskCap=120000)
        settings = pa.read_context(self.dir)[0]
        self.assertEqual(pa.reset_reason({"ctx": 120000}, {"boundary": "task"}, settings, None), "task-cap")
        self.assertEqual(pa.reset_reason({"ctx": 90000}, {"boundary": "pass"}, settings, {"by": "person"}), "person")
        self.assertEqual(pa.reset_reason({"ctx": 90000}, {"boundary": "pass"}, settings, {"by": "panel"}), "person")
        self.assertEqual(pa.reset_reason(None, None, settings, {"by": "orchestrator"}), "orchestrator")
        self.context(mode="fresh", net=50000)  # invalid: the defaults
        self.assertEqual(pa.reset_reason({"ctx": 150000}, {"boundary": "task"}, pa.read_context(self.dir)[0], None), "task-cap")


class J4Nudge(J4Base):  # R2's one line
    def sends(self):
        return [c for c in self.calls if c[:2] == ["hc", "send"]]

    def test_n1_n2_only_b_failing_once_an_hour(self):
        self.big()
        self.checkpoint(clean=False, boundary=None, why="mid-card")
        ctx = self.ctx()
        self.once(ctx)
        self.assertEqual(self.sends(), [["hc", "send", "t0", "[launcher] context at 260k, over the cap: if this is a clean "
                                         "point, write the ledger's context checkpoint and end your turn"]])
        self.once(ctx, self.now + 1800)
        self.assertEqual(len(self.sends()), 1)
        self.once(ctx, self.now + 3700)
        self.assertEqual(len(self.sends()), 2)
        self.assertIn("context nudge ctx=260000 reason=pass-cap", self.log())

    def test_n3_another_reason_too(self):
        self.big()
        self.checkpoint(clean=False, boundary=None, why="mid-card")
        os.remove(os.path.join(self.dir, "standing.md"))
        self.once(self.ctx())
        self.assertEqual(self.sends(), [])

    def test_n4_a_request_waiting(self):
        self.request()
        self.checkpoint(clean=False, boundary=None, why="mid-card")
        ctx = self.ctx()
        self.once(ctx)
        self.assertEqual(len(self.sends()), 1)
        self.assertIn("a fresh context was asked for", self.sends()[0][3])
        self.assertTrue(self.sends()[0][3].startswith("[launcher] context"))

    def test_below_the_caps_nothing(self):
        self.big(100000)
        self.once(self.ctx())
        self.assertEqual(self.sends(), [])
        self.assertNotIn("context not reset", self.log())


class J4Reset(J4Base):  # R4, AC4.1, R16 records
    def test_f1_f7_the_reset_ends_only_its_own_child(self):
        self.big()
        child = FakeChild()
        ctx = self.ctx(child=child)
        out = self.once(ctx)
        self.assertEqual(child.signals, [signal.SIGTERM])
        self.assertEqual(ctx["after"]["kind"], "fresh")
        self.assertEqual((ctx["after"]["reason"], ctx["after"]["ctx"], ctx["after"]["from"], ctx["after"]["pass"]),
                         ("pass-cap", 260000, SID, 12))
        self.assertEqual(ctx["ended"], "fresh")
        self.assertIn("[Project Agent] Context reset (260k): continuing from the state folder. Previous session: " + SID, out)

    def test_f1b_sigkill_only_after_the_grace(self):
        self.big()
        pa.END_GRACE = 0.1
        child = FakeChild(obeys_term=False)
        self.once(self.ctx(child=child))
        self.assertEqual(child.signals, [signal.SIGTERM, signal.SIGKILL])

    def run_fresh(self, after=None, previous=None):
        agent = load(os.path.join(self.dir, "agent.json"))
        if previous is not None:
            agent["previousSessions"] = previous
        dump(agent, os.path.join(self.dir, "agent.json"))
        dump({"name": "test", "claude": ["true"], "codex": ["true"], "systemPromptSnapshot": "keep"},
             os.path.join(pa.ROOT, "profiles", "test", "profile.json"))
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
        ctx = {"beat": True, "previous": None, "launcher": {"version": 2, "accepts": ["restart", "context"], "instance": "i1"},
               "after": after or {"kind": "fresh", "reason": "pass-cap", "ctx": 260000, "pass": 12, "from": SID,
                                  "request": None, "by": None}}
        try:
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(Stop):
                pa.run_agent(argparse.Namespace(fresh=False), "t0", self.project, pa.load_profile("test"), self.dir,
                             load(os.path.join(self.dir, "agent.json")), ctx, time.time())
        finally:
            undo()
            pa.subprocess.call, pa.time.sleep = saved
        return calls[-1], ctx

    def test_f2_f6_the_fresh_start(self):
        older = ["old-{:02d}-aaaa-bbbb-cccc-dddddddddddd".format(i) for i in range(20)]
        argv, ctx = self.run_fresh(previous=older)
        agent = load(os.path.join(self.dir, "agent.json"))
        new = agent["sessionId"]
        self.assertNotEqual(new, SID)
        self.assertFalse(agent["sessionStarted"])
        self.assertEqual(agent["previousSessions"], [SID] + older[:19])  # F3: 21 -> 20, newest first
        self.assertIn("--session-id", argv)
        self.assertEqual(argv[argv.index("--session-id") + 1], new)
        self.assertNotIn("--resume", argv)
        self.assertEqual(argv[-2], "--settings")
        prompt = argv[-3]
        self.assertTrue(prompt.startswith("Fresh context (reset 1 today, reason pass-cap; previous session {}). Your "
                                          "conversation was reset on purpose. Your state is on disk.".format(SID)), prompt)
        log = self.log().splitlines()
        fresh = [i for i, l in enumerate(log) if l.split()[1:2] == ["fresh"]]
        starts = [i for i, l in enumerate(log) if l.split()[1:2] == ["start"]]
        self.assertEqual(len(fresh), 1, log)
        self.assertIn("fresh from={} to={} ctx=260000 reason=pass-cap checkpoint=12 by=- n=1".format(SID, new), log[fresh[0]])
        self.assertLess(fresh[0], starts[-1])  # Q6: the start line stays the last of a start
        self.assertIn("reason=context", log[starts[-1]])
        recs = self.records()
        reset = [r for r in recs if r.get("ev") == "reset"]
        self.assertEqual(len(reset), 1)
        self.assertEqual(sorted(reset[0]), sorted(["v", "ev", "at", "from", "to", "ctx", "reason", "pass", "terminal"]))
        self.assertEqual((reset[0]["from"], reset[0]["to"], reset[0]["ctx"], reset[0]["reason"], reset[0]["pass"],
                          reset[0]["terminal"]), (SID, new, 260000, "pass-cap", 12, "t0"))
        start = [r for r in recs if r.get("ev") == "start" and r.get("session") == new]
        self.assertEqual([r["resumed"] for r in start], [False])
        self.assertLess(recs.index(reset[0]), recs.index(start[0]))
        with open(os.path.join(self.dir, "costs", "context", new + ".txt")) as f:
            self.assertEqual(f.read(), prompt)  # F6
        costs = pa.costs_module()
        days = costs.pa_context([], recs, {}, {})
        self.assertEqual(sum(d["resets"]["fresh"] for d in days), 1)
        rec = load(os.path.join(self.dir, "launcher.json"))
        self.assertEqual((rec["lastReset"]["from"], rec["lastReset"]["to"], rec["lastReset"]["reason"]), (SID, new, "pass-cap"))
        self.assertIsNotNone(ctx.get("lastResetAt"))

    def test_f8_a_recent_reset_and_a_restart_first(self):
        self.big()
        ctx = self.ctx(lastResetAt=self.now - 300)
        self.once(ctx)
        self.assertIsNone(ctx.get("after"))
        write(os.path.join(self.dir, pa.REQUEST), "{}")
        ctx = self.ctx()
        self.once(ctx)
        self.assertIsNone(ctx.get("after"))
        for key in ("restarting", "waitingFor"):
            ctx = self.ctx(**{key: "r1"})
            os.remove(os.path.join(self.dir, pa.REQUEST)) if os.path.exists(os.path.join(self.dir, pa.REQUEST)) else None
            self.once(ctx)
            self.assertIsNone(ctx.get("after"), key)

    def test_no_child_nothing(self):
        self.big()
        ctx = self.ctx(child=None)
        self.once(ctx)
        self.assertIsNone(ctx.get("after"))

    def test_take_after_fresh(self):
        ctx = {"after": {"kind": "fresh", "reason": "person"}}
        pa.take_after(ctx, self.project, self.dir, "t0")
        self.assertEqual(ctx["freshNext"]["reason"], "person")
        self.assertNotIn("after", ctx)

    def test_launch_command_prompt(self):
        argv, _m = pa.launch_command(self.profile(), dict(self.agent), False, self.dir, version=V300, prompt="Fresh context x")
        self.assertEqual(argv[-3:-1], ["Fresh context x", "--settings"])
        argv, _m = pa.launch_command(self.profile(), dict(self.agent), True, self.dir, version=V300, prompt="Fresh context x")
        self.assertNotIn("Fresh context x", argv)  # a resume keeps its own prompt
        with self.assertRaises(pa.Problem):
            pa.launch_command(self.profile(), dict(self.agent), False, self.dir, version=V300, prompt="-x")
        argv, _m = pa.launch_command(self.profile(), dict(self.agent), False, self.dir, version=V300)
        self.assertEqual(argv[-4:-2], ["--autocompact", "400000"])  # no prompt without one


class J4Prompt(J4Base):  # R10, AC10.1
    def test_p1_size(self):
        write(os.path.join(self.dir, "standing.md"), (SEED + "- 2026-11-18 " + "y" * 6000)[:6000])
        self.assertEqual(len(load_text(os.path.join(self.dir, "standing.md"))), 6000)
        self.assertLessEqual(len(pa.fresh_prompt(self.dir, 3, "pass-cap", SID)), 9000)

    def test_p2_names_every_file_and_the_hold_rule(self):
        text = pa.fresh_prompt(self.dir, 1, "task-cap", SID)
        for word in ("`pa-doctor`", "ledger.json", "`standing.md`", "the last 30 journal lines", "the open decisions", "`hc ls`"):
            self.assertIn(word, text)
        hold = "If the last hold or resume line in the journal is a hold, start nothing"
        self.assertIn(hold, text)
        argv, _m = pa.launch_command(self.profile(), dict(self.agent), True, self.dir, version=V300)
        self.assertIn(hold, " ".join(argv))
        core = " ".join(load_text(os.path.join(HERE, "..", "runtime", "core", "orchestrator.md")).split())
        self.assertIn("is a hold, start nothing", core)
        self.assertIn("A fresh context counts as a restart: continue without asking exactly as after a restart", core)

    def test_p3_head(self):
        today = time.strftime("%Y-%m-%d", time.gmtime())
        write(os.path.join(self.dir, "session.log"), "2000-01-01T00:00:00Z fresh from=a to=b\n{}T00:00:00Z fresh from=b to=c\n"
              "{}T00:00:01Z start resume=False session=c\n".format(today, today))
        self.assertEqual(pa.resets_today(self.dir), 1)
        text = pa.fresh_prompt(self.dir, pa.resets_today(self.dir) + 1, "person", SID)
        self.assertTrue(text.startswith("Fresh context (reset 2 today, reason person; previous session {}). Your conversation "
                                        "was reset on purpose. ".format(SID)), text[:200])

    def test_p4_standing_verbatim(self):
        standing = SEED.replace("# Standing words\n", "# Standing words\n- 2026-11-18 \"keep replies short\" (all)\n")
        write(os.path.join(self.dir, "standing.md"), standing)
        self.assertIn(standing.rstrip("\n"), pa.fresh_prompt(self.dir, 1, "person", SID))


def load_text(path):
    with open(path) as f:
        return f.read()


class J4Request(J4Base):  # R6, AC6.1
    def req_file(self):
        path = os.path.join(self.dir, "context-request.json")
        return load(path) if os.path.exists(path) else None

    def test_q1_written(self):
        code, out, err = self.cli("fresh", "--space", "sp1")
        self.assertEqual(code, 0, err)
        req = self.req_file()
        self.assertEqual(sorted(req), sorted(["v", "id", "space", "by", "why", "at", "expires"]))
        self.assertEqual((req["v"], req["space"], req["by"], req["why"]), (1, "sp1", "person", None))
        self.assertEqual(pa.parse_time(req["expires"]) - pa.parse_time(req["at"]), 7200)
        got, why = pa.read_context_request(self.dir)
        self.assertEqual((got["id"], why), (req["id"], None))

    def test_q2_acted_on_at_a_clean_point_only_with_no_size_condition(self):
        self.request()
        self.terms["t0"]["status"] = {"state": "working"}
        ctx = self.ctx()
        self.once(ctx)
        self.assertIsNone(ctx.get("after"))
        self.terms["t0"]["status"] = {"state": "idle"}
        self.once(ctx)  # idle from now on: 20 s to wait
        self.assertIsNone(ctx.get("after"))
        self.once(ctx, self.now + 25)
        self.assertEqual((ctx["after"]["reason"], ctx["after"]["request"], ctx["after"]["ctx"]), ("person", "req-1", 90000))

    def test_q3_expired(self):
        self.request(at=pa.iso_time(time.time() - 8000), expires=pa.iso_time(time.time() - 800))
        ctx = self.ctx(launcher={"version": 2, "accepts": ["restart", "context"], "instance": "i1"})
        self.once(ctx)
        self.assertIsNone(self.req_file())
        self.assertIn("context request req-1 expired", self.log())
        self.assertIsNone(ctx.get("after"))
        self.assertEqual(load(os.path.join(self.dir, "launcher.json"))["context"]["outcome"], "expired")

    def test_q4_another_space_and_unreadable(self):
        self.request(space="sp2")
        ctx = self.ctx()
        self.once(ctx)
        self.assertIsNone(self.req_file())
        self.assertIn("context request req-1 refused: for space sp2", self.log())
        self.assertIsNone(ctx.get("after"))
        for bad in ({"by": "worker"}, {"id": "-x"}, {"why": "a\x07b"}, {"expires": pa.iso_time(time.time() + 9000)}):
            with self.subTest(bad=bad):
                self.request(**bad)
                self.assertEqual(pa.read_context_request(self.dir)[0], None)
                self.once(self.ctx())
                self.assertIsNone(self.req_file())
        write(os.path.join(self.dir, "context-request.json"), "not json")
        self.once(self.ctx())
        self.assertIsNone(self.req_file())

    def test_q5_inside_claude(self):
        os.environ["CLAUDECODE"] = "1"
        code, _o, err = self.cli("fresh", "--space", "sp1")
        self.assertEqual(code, 2)
        self.assertIn("--why", err)
        self.assertIsNone(self.req_file())
        code, _o, err = self.cli("fresh", "--space", "sp1", "--why", "the person: 'start fresh'")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.req_file()["by"], "orchestrator")
        lines = [json.loads(l) for l in load_text(os.path.join(self.dir, "journal.jsonl")).splitlines() if l.strip()]
        self.assertEqual((lines[-1]["trigger"], lines[-1]["request"]), ("conversation", self.req_file()["id"]))
        self.assertIn("start fresh", lines[-1]["why"])
        os.environ.pop("CLAUDECODE")
        ctx = self.ctx()
        self.once(ctx)
        self.assertEqual(ctx["after"]["reason"], "orchestrator")

    def test_q6_by_panel(self):
        os.environ["CLAUDECODE"] = "1"
        code, _o, err = self.cli("fresh", "--space", "sp1", "--by", "panel", "--why", "x")
        self.assertEqual(code, 2)
        self.assertIn("--by panel", err)
        os.environ.pop("CLAUDECODE")
        code, _o, err = self.cli("fresh", "--space", "sp1", "--by", "panel", "--why", "the panel's button")
        self.assertEqual((code, self.req_file()["by"]), (0, "panel"), err)
        ctx = self.ctx()
        self.once(ctx)
        self.assertEqual((ctx["after"]["reason"], ctx["after"]["by"]), ("person", "panel"))
        pa.fresh_records({"launcher": {"version": 2}}, self.dir, "t0", ctx["after"], "cccccccc-aaaa-bbbb-dddd-eeeeeeeeeeee", "p")
        self.assertIn("reason=person checkpoint=12 by=panel", self.log())

    def test_q7_mode_off_refused(self):
        self.context(mode="off")
        code, _o, err = self.cli("fresh", "--space", "sp1")
        self.assertEqual(code, 2)
        self.assertIn("pa-restart --fresh", err)
        self.assertIsNone(self.req_file())

    def test_q8_an_older_launcher(self):
        dump({"version": 2, "accepts": ["restart"], "instance": "i1"}, os.path.join(self.dir, "launcher.json"))
        code, _o, err = self.cli("fresh", "--space", "sp1")
        self.assertEqual(code, 2)
        self.assertIn("restart it once", err)
        self.assertIsNone(self.req_file())
        self.assertFalse(pa.context_capable({"version": 1, "accepts": ["restart", "context"]}))
        self.assertTrue(pa.context_capable({"version": 2, "accepts": ["restart", "context"]}))

    def test_q9_withdraw_dry_run_json(self):
        code, out, err = self.cli("fresh", "--space", "sp1", "--dry-run", "--json")
        self.assertEqual(code, 0, err)
        self.assertIsNone(self.req_file())
        report = json.loads(out.strip().splitlines()[-1])
        self.assertEqual((report["outcome"], report["ctx"], report["due"]), ("dry-run", 90000, None))
        self.assertIn("reasons", report)
        self.assertIn("90k", out)
        self.cli("fresh", "--space", "sp1")
        rid = self.req_file()["id"]
        code, out, err = self.cli("fresh", "--space", "sp1", "--dry-run", "--json")
        report = json.loads(out.strip().splitlines()[-1])
        self.assertEqual((report["due"], report["request"]), ("person", rid))
        code, out, err = self.cli("fresh", "--space", "sp1", "--withdraw", "--json")
        self.assertEqual(code, 0, err)
        self.assertIsNone(self.req_file())
        self.assertEqual(json.loads(out.strip().splitlines()[-1]), {"outcome": "withdrawn", "request": rid})
        code, out, err = self.cli("fresh", "--space", "sp1", "--json")
        self.assertEqual(json.loads(out.strip().splitlines()[-1])["outcome"], "requested")

    def test_q10_on_relays(self):
        calls = []
        pa.relay = lambda argv: calls.append(list(argv)) or 0
        code, _o, err = self.cli("fresh", "--on", "m2", "--space", "sp1", "--why", "it's long", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(calls[0][:4], ["hc", "exec", "--on", "m2"])
        import shlex
        words = shlex.split(calls[0][-1])
        self.assertEqual(words[1:3], ["context", "fresh"])
        self.assertEqual(words[words.index("--why") + 1], "it's long")
        self.assertIn("--json", words)
        self.assertNotIn("--on", words)
        code, _o, err = self.cli("fresh", "--on", "m2")
        self.assertEqual(code, 2)

    def test_q10b_on_inside_claude_checks_first(self):  # REVIEW J4 MF-J4-1
        calls = []
        pa.relay = lambda argv: calls.append(list(argv)) or 0
        os.environ["CLAUDECODE"] = "1"
        code, _o, err = self.cli("fresh", "--on", "m2", "--space", "sp1")
        self.assertEqual(code, 2)
        self.assertIn("--why", err)
        code, _o, err = self.cli("fresh", "--on", "m2", "--space", "sp1", "--by", "panel", "--why", "x")
        self.assertEqual(code, 2)
        self.assertIn("--by panel", err)
        self.assertEqual(calls, [])
        code, _o, err = self.cli("fresh", "--on", "m2", "--space", "sp1", "--why", "the person: 'start fresh'")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(calls), 1)


class J4Settings(J4Base):  # R13, resets part
    def test_k9_mode_off_while_running(self):
        self.big()
        self.context(mode="off")
        ctx = self.ctx()
        self.once(ctx)
        self.assertIsNone(ctx.get("after"))
        self.assertEqual([c for c in self.calls if c[:2] == ["hc", "send"]], [])

    def test_k10_by_panel(self):
        code, _o, err = self.cli("set", "--space", "sp1", "--mode", "off", "--by", "panel")
        self.assertEqual(code, 0, err)
        self.assertEqual(load(os.path.join(self.dir, "context.json"))["by"], "panel")
        os.environ["CLAUDECODE"] = "1"
        code, _o, _e = self.cli("set", "--space", "sp1", "--mode", "fresh", "--by", "panel")
        self.assertEqual(code, 2)
        self.assertEqual(load(os.path.join(self.dir, "context.json"))["mode"], "off")

    def test_k11_json(self):
        code, out, err = self.cli("set", "--space", "sp1", "--net", "450k", "--json")
        self.assertEqual(code, 0, err)
        value = json.loads(out.strip().splitlines()[-1])
        self.assertEqual((value["net"], value["mode"], value["by"]), (450000, "fresh", "person"))
        code, out, err = self.cli("set", "--space", "sp1", "--json")
        self.assertEqual(json.loads(out.strip().splitlines()[-1])["net"], 450000)

    def test_k12_on_relays(self):
        calls = []
        pa.relay = lambda argv: calls.append(list(argv)) or 0
        code, _o, err = self.cli("set", "--on", "m2", "--space", "sp1", "--mode", "off", "--by", "panel")
        self.assertEqual(code, 0, err)
        import shlex
        words = shlex.split(calls[0][-1])
        self.assertEqual(words[1:3], ["context", "set"])
        self.assertEqual(words[words.index("--mode") + 1], "off")
        self.assertEqual(words[words.index("--by") + 1], "panel")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "context.json")))

    def test_k12b_on_inside_claude_refused(self):  # REVIEW J4 MF-J4-1: set is the person's, also with --on
        calls = []
        pa.relay = lambda argv: calls.append(list(argv)) or 0
        for var in ("CLAUDECODE", "CODEX_SANDBOX"):
            os.environ[var] = "1"
            code, _o, err = self.cli("set", "--on", "m2", "--space", "sp1", "--mode", "off")
            self.assertEqual(code, 2)
            self.assertIn("person's command", err)
            os.environ.pop(var)
        self.assertEqual(calls, [])


class J4Records(J4Base):  # R16
    def test_r1_accepts(self):
        self.assertEqual(pa.ACCEPTS, ["restart", "context"])

    def test_r2_request_outcome_and_file_removed(self):
        self.request()
        ctx = {"launcher": {"version": 2, "accepts": list(pa.ACCEPTS), "instance": "i1"}}
        after = {"kind": "fresh", "reason": "person", "ctx": 90000, "pass": 12, "from": SID, "request": "req-1", "by": "person"}
        pa.fresh_records(ctx, self.dir, "t0", after, "dddddddd-aaaa-bbbb-cccc-eeeeeeeeeeee", "prompt")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "context-request.json")))
        rec = load(os.path.join(self.dir, "launcher.json"))
        self.assertEqual((rec["context"]["request"], rec["context"]["outcome"]), ("req-1", "ok"))
        self.assertEqual(rec["lastReset"]["to"], "dddddddd-aaaa-bbbb-cccc-eeeeeeeeeeee")

    def test_r3_copies_capped(self):
        pa.CONTEXT_COPIES_MAX = 2
        after = {"kind": "fresh", "reason": "pass-cap", "ctx": 260000, "pass": 12, "from": SID, "request": None, "by": None}
        for i in range(3):
            pa.fresh_records({}, self.dir, "t0", after, "eeeeeee{}-aaaa-bbbb-cccc-dddddddddddd".format(i), "p{}".format(i))
            os.utime(os.path.join(self.dir, "costs", "context", "eeeeeee{}-aaaa-bbbb-cccc-dddddddddddd.txt".format(i)),
                     (1000 + i, 1000 + i))
        names = sorted(os.listdir(os.path.join(self.dir, "costs", "context")))
        self.assertLessEqual(len(names), 2)
        self.assertIn("eeeeeee2-aaaa-bbbb-cccc-dddddddddddd.txt", names)


class J4Followup(J4Base):  # R17, AC17.1
    def fixture(self, before=1.0, after=0.7):
        self.context(mode="fresh", enabledAt="2026-11-18T00:00:00Z", by="person")
        dump({"paContext": [
            {"day": "2026-11-19", "calls": 300, "mean": 150000, "peak": 250000, "peakExact": True,
             "resets": {"fresh": 1, "compact": 0}, "firstCall": 70000, "money": after * 50, "passes": 50, "perPass": after},
            {"day": "2026-11-18", "calls": 300, "mean": 160000, "peak": 254000, "peakExact": True,
             "resets": {"fresh": 1, "compact": 0}, "firstCall": 72000, "money": after * 50, "passes": 50, "perPass": after},
            {"day": "2026-11-17", "calls": 300, "mean": 468000, "peak": 966000, "peakExact": False,
             "resets": {"fresh": 0, "compact": 1}, "firstCall": None, "money": before * 100, "passes": 100, "perPass": before},
        ]}, os.path.join(self.dir, "costs", "summary.json"))
        write(os.path.join(self.dir, "costs", "sessions.jsonl"),
              json.dumps({"v": 1, "ev": "reset", "at": "2026-11-18T05:00:00Z", "from": "a", "to": "b", "ctx": 260000,
                          "reason": "pass-cap", "pass": 20, "terminal": "t0"}) + "\n"
              + json.dumps({"v": 1, "ev": "reset", "at": "2026-11-19T05:00:00Z", "from": "b", "to": "c", "ctx": 155000,
                            "reason": "task-cap", "pass": 60, "terminal": "t0"}) + "\n")
        write(os.path.join(self.dir, "journal.jsonl"),
              json.dumps({"at": "2026-11-18T04:00:00Z", "pass": 19, "trigger": "message", "standing": "added",
                          "entry": "- 2026-11-18 \"keep replies short\""}) + "\n")

    def journal(self, *lines):
        with open(os.path.join(self.dir, "journal.jsonl"), "a") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")

    def test_v2_keep(self):
        self.fixture()
        r = pa.followup_report(self.dir, "2026-11-18T00:00:00Z")
        self.assertEqual(r["verdict"], "keep")
        self.assertAlmostEqual(r["fell"], 0.3, places=6)
        self.assertEqual((r["before"]["perPass"], r["after"]["perPass"]), (1.0, 0.7))
        self.assertEqual(r["resets"]["byReason"], {"pass-cap": 1, "task-cap": 1})
        self.assertEqual(r["resets"]["maxPerHour"], 1)
        self.assertEqual(r["compact"], 0)
        self.assertEqual(len(r["standing"]), 1)

    def test_v1_one_repeat_marker_tunes(self):
        self.fixture()
        self.journal({"at": "2026-11-18T09:00:00Z", "pass": 30, "trigger": "message", "repeat": "keep replies short"})
        r = pa.followup_report(self.dir, "2026-11-18T00:00:00Z")
        self.assertEqual(r["verdict"], "tune")
        self.assertEqual([(m["kind"], m["text"]) for m in r["markers"]], [("repeat", "keep replies short")])

    def test_v3_two_resets_in_an_hour_tune(self):
        self.fixture()
        with open(os.path.join(self.dir, "costs", "sessions.jsonl"), "a") as f:
            f.write(json.dumps({"v": 1, "ev": "reset", "at": "2026-11-18T05:30:00Z", "from": "b", "to": "b2", "ctx": 250000,
                                "reason": "pass-cap", "pass": 22, "terminal": "t0"}) + "\n")
        r = pa.followup_report(self.dir, "2026-11-18T00:00:00Z")
        self.assertEqual((r["resets"]["maxPerHour"], r["verdict"]), (2, "tune"))

    def test_v4_judgement_candidates(self):
        self.fixture()
        self.journal({"at": "2026-11-18T05:10:00Z", "pass": 21, "trigger": "report"},
                     {"at": "2026-11-18T05:20:00Z", "pass": 22, "trigger": "message", "reask": "ask-3"})
        r = pa.followup_report(self.dir, "2026-11-18T00:00:00Z")
        self.assertEqual(r["verdict"], "tune")
        self.assertTrue(any("2026-11-18T05:00:00Z" in j for j in r["judgement"]), r["judgement"])
        dump({"id": "d-009", "status": "answered", "question": "q", "options": ["a"], "answered": "2026-11-19T04:45:00Z"},
             os.path.join(self.dir, "decisions", "d-009.json"))
        r = pa.followup_report(self.dir, "2026-11-18T00:00:00Z")
        self.assertTrue(any("d-009" in j for j in r["judgement"]), r["judgement"])

    def test_v5_json_and_no_savings(self):
        self.fixture(before=1.0, after=0.9)
        code, out, err = self.cli("followup", "--space", "sp1", "--json")
        self.assertEqual(code, 0, err)
        r = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(r["since"], "2026-11-18T00:00:00Z")
        self.assertEqual(r["verdict"], "needs judgement")
        self.assertEqual(json.loads(json.dumps(r)), r)
        code, out, err = self.cli("followup", "--space", "sp1")
        self.assertIn("verdict", out)
        self.assertEqual(sorted(r), sorted(["since", "until", "days", "before", "after", "fell", "resets", "compact", "markers",
                                            "standing", "judgement", "verdict", "refusals", "acceptedByTime", "passes"]))


class J7Unacted(J4Base):  # t-041 J7-R1: R2 (c) looks only at this conversation's window
    def card(self, cid, answered):
        dump({"id": cid, "status": "answered", "question": "q", "options": ["a"], "answer": "a", "answered": answered},
             os.path.join(self.dir, "decisions", cid + ".json"))

    def filler(self, size, start, step=1, name="x"):
        """Journal lines of about 300 bytes, stamped from `start` on, naming no card, `size` bytes in all."""
        lines, n, t = [], 0, start
        while n < size:
            line = json.dumps({"at": pa.iso_time(t), "pass": 1, "trigger": "report", "why": name * 280}) + "\n"
            lines.append(line)
            n += len(line)
            t += step
        return "".join(lines)

    def test_c6c_old_answered_cards_do_not_count(self):  # the J6 case: 90 of 90 refusals
        old = self.base - 2 * 86400
        for i in range(200):
            self.card("d-{}".format(100 + i), pa.iso_time(old + i))
        write(os.path.join(self.dir, "journal.jsonl"), self.filler(1500000, old - 86400, step=1))
        self.assertEqual(self.clean(), (True, []))

    def test_c6d_answered_in_this_conversation(self):
        self.card("d-007", pa.iso_time(self.base - 40))  # running.since is base - 50
        ok, reasons = self.clean()
        self.assertEqual(([r[0] for r in reasons], ok), (["c"], False))

    def test_c6e_answered_in_the_slack_before_it(self):
        self.card("d-007", pa.iso_time(self.base - 50 - 1200))  # 20 min before this conversation began
        self.assertIn("card d-007 answered, not acted on", self.words(self.clean()[1]))

    def test_c6f_naming_line_beyond_256_kb(self):
        self.card("d-007", pa.iso_time(self.base - 20))
        write(os.path.join(self.dir, "journal.jsonl"),
              json.dumps({"at": pa.iso_time(self.base - 10), "why": "acting on d-007"}) + "\n"
              + self.filler(600000, self.base - 9, step=0))
        self.assertEqual(self.clean(), (True, []))

    def test_c6g_no_running_record_a_day_back(self):
        ctx = self.ctx()
        del ctx["running"]
        self.checkpoint(session=SID)
        self.card("d-007", pa.iso_time(self.now - 25 * 3600))
        self.assertEqual(self.clean(ctx), (True, []))
        self.card("d-008", pa.iso_time(self.now - 2 * 3600))
        ok, reasons = self.clean(ctx)
        self.assertEqual(([r[0] for r in reasons], ok), (["c"], False))
        self.assertIn("card d-008 answered, not acted on", self.words(reasons))

    def test_c6h_journal_not_read_back_that_far(self):
        self.card("d-007", pa.iso_time(self.base - 1800))  # inside W (running.since - 30 min)
        write(os.path.join(self.dir, "journal.jsonl"), self.filler(6 << 20, self.base - 1700, step=0))
        reads = []
        real_open = open

        class Counting:
            def __init__(self, f):
                self.f = f

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.f.close()

            def read(self, n=-1):
                data = self.f.read(n)
                reads.append(len(data))
                return data

            def __getattr__(self, name):
                return getattr(self.f, name)

        def counting_open(path, mode="r", *a, **kw):
            f = real_open(path, mode, *a, **kw)
            return Counting(f) if str(path).endswith("journal.jsonl") else f
        pa.open = counting_open
        try:
            ok, reasons = self.clean()
        finally:
            del pa.open
        self.assertFalse(ok)
        self.assertIn("card d-007 answered, journal not read back that far", self.words(reasons))
        self.assertLessEqual(sum(reads), (4 << 20) + (256 << 10))


    def test_c6i_answered_hours_before_this_conversation(self):  # inside the day, outside since - 30 min
        self.card("d-007", pa.iso_time(self.base - 50 - 3 * 3600))  # running.since is base - 50
        self.assertEqual(self.clean(), (True, []))

    def edge(self, since, answered):
        """clean_point at a whole-second `at` with running.since `since`, one card d-007 answered at `answered`."""
        at = int(self.now)
        self.card("d-007", pa.iso_time(answered))
        ctx = self.ctx(running={"since": since, "session": SID, "marked": True})
        return pa.clean_point(ctx, self.dir, load(os.path.join(self.dir, "agent.json")), self.size(), at, "t0")

    def test_c6j_window_edge_at_since_less_the_slack(self):
        since = int(self.now) - 150
        self.assertEqual(self.edge(since, since - 1801), (True, []))
        ok, reasons = self.edge(since, since - 1800)
        self.assertEqual(([r[0] for r in reasons], ok), (["c"], False))

    def test_c6k_day_cap_in_a_conversation_older_than_a_day(self):
        at = int(self.now)
        since = at - 3 * 86400
        self.assertEqual(self.edge(since, at - 86401), (True, []))
        ok, reasons = self.edge(since, at - 86400)
        self.assertEqual(([r[0] for r in reasons], ok), (["c"], False))
        self.assertIn("card d-007 answered, not acted on", self.words(reasons))


class J7Checkpoint(J4Base):  # t-041 J7-R2: the accepted-by-time note
    def test_c4d_note_once_per_idle_period(self):
        self.big()
        self.checkpoint(session="ffffffff-eeee-dddd-cccc-bbbbbbbbbbbb")
        os.remove(os.path.join(self.dir, "standing.md"))  # another reason, so no reset is taken
        ctx = self.ctx()
        self.once(ctx)
        self.once(ctx, self.now + 10)
        note = "checkpoint names session ffffffff, this session aaaaaaaa: accepted by its time"
        self.assertEqual(self.log().count(note), 1, self.log())


class J7Texts(J4Base):  # t-041 J7-R3, R4 (c, d), R5 (b)
    def test_k13_the_net_fires_below_its_window(self):
        settings, _p = pa.read_context(self.dir)
        self.assertIn("net 400,000 (compacts at about 366k)", pa.context_summary(settings, False))
        self.assertIn("(compacts at about 416k)", pa.context_summary(dict(settings, net=450000), True))

    def rules(self, value):
        path = os.path.join(self.dir, "rules.json")
        write(path, value if isinstance(value, str) else json.dumps(value))

    def test_p5_active_rule_ids(self):
        self.rules({"version": 1, "rules": [{"id": "r-a", "status": "active"}, {"id": "r-p", "status": "proposed"},
                                            {"id": "r-b", "status": "active"}]})
        text = pa.fresh_prompt(self.dir, 1, "pass-cap", SID)
        self.assertIn("Active rules (rules.json, highest order first): r-a; r-b. Read each one's text before your next "
                      "action.", text)  # t-190 R1: no title, the id alone
        for bad in ("{not json", json.dumps({"version": 1}), json.dumps([1]), json.dumps({"rules": "x"})):
            self.rules(bad)
            self.assertNotIn("Active rules", pa.fresh_prompt(self.dir, 1, "pass-cap", SID))
        os.remove(os.path.join(self.dir, "rules.json"))
        self.assertNotIn("Active rules", pa.fresh_prompt(self.dir, 1, "pass-cap", SID))
        self.rules({"rules": [{"id": "r-rule-number-{:02d}".format(i), "status": "active"} for i in range(60)]})
        line = [x for x in pa.fresh_prompt(self.dir, 1, "pass-cap", SID).split("\n") if x.startswith("Active rules")][0]
        self.assertLessEqual(len(line), 1600)  # t-190 R1: titles need room
        write(os.path.join(self.dir, "standing.md"), (SEED + "- 2026-11-18 " + "y" * 6000)[:6000])
        self.assertLessEqual(len(pa.fresh_prompt(self.dir, 3, "pass-cap", SID)), 9000)

    def test_p6_next_pass(self):
        self.assertIn("Your next pass is 13.", pa.fresh_prompt(self.dir, 1, "pass-cap", SID))
        os.remove(os.path.join(self.dir, "ledger.json"))
        self.assertNotIn("next pass", pa.fresh_prompt(self.dir, 1, "pass-cap", SID))

    def test_compact_instructions_name_rules_json(self):
        self.assertIn("rules.json", pa.compact_instructions())


class J7Followup(J4Base):  # t-041 J7-R5 (c), R7; J4Followup's fixture, not its tests again
    fixture = J4Followup.fixture
    journal = J4Followup.journal

    def test_v6_refusals_by_reason(self):
        self.fixture()
        write(os.path.join(self.dir, "session.log"),
              "2026-11-17T10:00:00Z context not reset (ctx=260000 reason=pass-cap): card d-9 answered, not acted on\n"
              "2026-11-18T10:00:00Z context not reset (ctx=260000 reason=pass-cap): card d-1 answered, not acted on\n"
              "2026-11-18T10:01:00Z context not reset (ctx=261000 reason=pass-cap): card d-2 answered, not acted on\n"
              "2026-11-18T10:02:00Z context not reset (ctx=262000 reason=task-cap): checkpoint of another session "
              "(ffffffff not aaaaaaaa)\n"
              "2026-11-17T11:00:00Z checkpoint names session ffffffff, this session aaaaaaaa: accepted by its time\n"
              "2026-11-18T11:00:00Z checkpoint names session ffffffff, this session aaaaaaaa: accepted by its time\n")
        r = pa.followup_report(self.dir, "2026-11-18T00:00:00Z")
        self.assertEqual(r["refusals"], {"total": 3, "byReason": {"card answered, not acted on": 2,
                                                                  "checkpoint of another session": 1}})
        self.assertEqual(r["acceptedByTime"], 1)
        code, out, err = self.cli("followup", "--space", "sp1")
        self.assertIn("refusals 3", out)

    def test_v7_long_passes(self):
        self.fixture()
        self.journal(*[{"at": pa.iso_time(pa.parse_time("2026-11-18T06:00:00Z") + i * 100), "pass": 7, "trigger": "message"}
                       for i in range(25)])
        self.journal({"at": "2026-11-18T08:00:00Z", "pass": 8, "trigger": "report"})
        r = pa.followup_report(self.dir, "2026-11-18T00:00:00Z")
        self.assertEqual(r["passes"]["maxLines"], 25)
        self.assertEqual(r["passes"]["ids"], 3)  # 7, 8 and the fixture's 19
        self.assertEqual([(p["pass"], p["lines"], p["from"], p["to"]) for p in r["passes"]["long"]],
                         [(7, 25, "2026-11-18T06:00:00Z", "2026-11-18T06:40:00Z")])
        self.assertEqual(json.loads(json.dumps(r)), r)


if __name__ == "__main__":
    unittest.main()


# ---- t-190 R1 and T1: the start pack and the resume prompt name each active rule with its title ------------------

def ago(seconds):
    return pa.iso_time(time.time() - seconds)


class T190StartPack(J4Base):
    def rules(self, rules):
        dump({"version": 1, "rules": rules}, os.path.join(self.dir, "rules.json"))

    def line(self, text=None):
        text = pa.fresh_prompt(self.dir, 1, "pass-cap", SID) if text is None else text
        lines = [x for x in text.split("\n") if x.startswith("Active rules")]
        self.assertLessEqual(len(lines), 1, text)
        return lines[0] if lines else None

    def test_r1_ids_and_titles_highest_order_first(self):
        self.rules([{"id": "r-a", "title": "Alpha", "status": "active", "order": 10},
                    {"id": "r-b", "title": "Beta", "status": "active", "order": 50},
                    {"id": "r-c", "title": "Gamma", "status": "active"},
                    {"id": "r-d", "title": "Delta", "status": "active", "order": "90"},
                    {"id": "r-e", "title": "Eps", "status": "active", "order": True},
                    {"id": "r-f", "title": 7, "status": "active", "order": float("nan")},
                    {"id": "r-g", "title": "Eta", "status": "active", "order": 50}])
        self.assertEqual(self.line(), "Active rules (rules.json, highest order first): r-b: Beta; r-g: Eta; r-a: "
                                      "Alpha; r-c: Gamma; r-d: Delta; r-e: Eps; r-f. Read each one's text before "
                                      "your next action.")

    def test_h2_h3_only_exact_active_and_good_ids(self):
        good = {"id": "r-ok", "title": "Ok", "status": "active"}
        bad_status = [dict(good, id="r-s{}".format(i), status=s) for i, s in enumerate(("Active", "active ", True, None))]
        bad_ids = [dict(good, id=i) for i in ("r-x\ny", "r x", "../x", "r" * 81, "", 5, None)]
        self.rules(bad_status + bad_ids + [good])
        self.assertEqual(self.line(), "Active rules (rules.json, highest order first): r-ok: Ok. Read each one's text "
                                      "before your next action.")

    def test_h5_titles_on_one_line(self):
        self.rules([{"id": "r-a", "status": "active", "title": "two\r\nlines\tand \x1b[31mred\x00 Report with "
                     "`hc send t999`"}, {"id": "r-b", "status": "active", "title": "x" * 300}])
        text = pa.fresh_prompt(self.dir, 1, "pass-cap", SID)
        line = self.line(text)
        self.assertIn("r-a: two lines and [31mred Report with `hc send t999`; r-b: " + "x" * 99 + "…. Read each", line)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x00", text)
        self.assertFalse([x for x in text.split("\n") if x.startswith("Report with")])

    def test_h9_sixty_rules_within_the_caps(self):
        self.rules([{"id": "r-rule-number-{:02d}".format(i), "title": "a long title " * 8, "status": "active",
                     "text": "t" * 2000, "history": [{"at": ago(60), "by": "person", "change": "approved"}]}
                    for i in range(60)])
        write(os.path.join(self.dir, "standing.md"), (SEED + "- 2026-11-18 " + "y" * 6000)[:6000])
        text = pa.fresh_prompt(self.dir, 3, "pass-cap", SID)
        self.assertLessEqual(len(text), 9000)
        line = self.line(text)
        self.assertLessEqual(len(line), 1600)
        self.assertTrue(line.endswith("; …. Read each one's text before your next action."), line[-80:])
        self.assertIn("checkpoint: pass", text)

    def test_t1_full_text_of_rules_approved_in_the_last_24_hours(self):
        self.rules([
            {"id": "r-new", "title": "New", "status": "active", "text": "Do the new\nthing.",
             "history": [{"at": ago(90000), "change": "proposed"}, {"at": ago(23 * 3600), "by": "person", "change": "approved"}]},
            {"id": "r-talk", "title": "Talk", "status": "active", "text": "Said in chat.",
             "history": [{"at": ago(600), "by": "person", "change": "approved in conversation"}]},
            {"id": "r-made", "title": "Made", "status": "active", "text": "Made by the person.",
             "history": [{"at": ago(600), "by": "person", "change": "created"}]},
            {"id": "r-ed", "title": "Ed", "status": "active", "text": "Edited and approved.",
             "history": [{"at": ago(600), "by": "person", "change": "edited and approved"}]},
            {"id": "r-old", "title": "Old", "status": "active", "text": "Approved long ago.",
             "history": [{"at": ago(25 * 3600), "by": "person", "change": "approved"}]},
            {"id": "r-toggled", "title": "Toggled", "status": "active", "text": "Only turned off and on.",
             "history": [{"at": ago(30 * 3600), "change": "approved"}, {"at": ago(60), "change": "turned off"},
                         {"at": ago(30), "change": "turned on"}]},
            {"id": "r-off", "title": "Off", "status": "off", "text": "Not active.",
             "history": [{"at": ago(60), "change": "approved"}]},
            {"id": "r-bad", "title": "Bad", "status": "active", "text": "Bad times.",
             "history": [{"at": "yesterday", "change": "approved"}, "x", {"change": "approved"}]}])
        text = pa.fresh_prompt(self.dir, 1, "pass-cap", SID)
        self.assertIn("Rules approved in the last 24 h (their full text; read it before your next action):", text)
        for rid, words in (("r-new", "Do the new thing."), ("r-talk", "Said in chat."), ("r-made", "Made by the person."),
                           ("r-ed", "Edited and approved.")):
            self.assertIn("- {}: {}".format(rid, words), text)
        for words in ("Approved long ago.", "Only turned off and on.", "Not active.", "Bad times."):
            self.assertNotIn(words, text)
        self.rules([{"id": "r-old", "title": "Old", "status": "active", "text": "Approved long ago.",
                     "history": [{"at": ago(25 * 3600), "change": "approved"}]}])
        self.assertNotIn("Rules approved in the last 24 h", pa.fresh_prompt(self.dir, 1, "pass-cap", SID))

    def test_t1_text_cut_to_the_room_left(self):
        self.rules([{"id": "r-new", "title": "New", "status": "active", "text": "z" * 5000,
                     "history": [{"at": ago(60), "change": "approved"}]}])
        write(os.path.join(self.dir, "standing.md"), (SEED + "- 2026-11-18 " + "y" * 6000)[:6000])
        text = pa.fresh_prompt(self.dir, 1, "pass-cap", SID)
        self.assertLessEqual(len(text), 9000)
        self.assertIn("… (read the rest in rules.json)", text)
        self.assertIn("checkpoint: pass", text)
        self.assertIn("Active rules (rules.json, highest order first): r-new: New.", text)

    def test_r1_texts_name_rules_json_and_titles(self):
        self.assertIn("run `pa-doctor`, read ledger.json, rules.json (every active rule's text), `standing.md`,",
                      pa.START_PACK_BODY)
        self.assertIn("the ids and titles of the active rules in rules.json", pa.compact_instructions())

    def test_r1_resume_prompt_names_the_rules(self):
        self.rules([{"id": "r-a", "title": "Alpha", "status": "active", "order": 1}])
        argv = self.launch(V300, resume=True)
        self.assertTrue(argv[-3].startswith("Your session was restarted."), argv[-3])
        self.assertTrue(argv[-3].endswith(" Active rules (rules.json, highest order first): r-a: Alpha. Read each "
                                          "one's text before your next action."), argv[-3][-200:])
        os.remove(os.path.join(self.dir, "rules.json"))
        argv = self.launch(V300, resume=True)
        self.assertTrue(argv[-3].endswith("it is not a hold."), argv[-3][-80:])
