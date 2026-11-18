"""Tests for pa-trust (t-035): Claude Code's folder trust prompt, accepted for the project folder only.

Run: python3 -m unittest -v tests/test_pa_trust.py
No real terminal is touched: `hc` is a fake script first on PATH (its folder apart from the fixture data) that
answers from fixture files and logs every call; pa-trust runs as a subprocess with temp HOME, HERDR_PA_HOME,
CLAUDE_CONFIG_DIR and HERDR_APP_HOME, and git with no global or system config. The screens are in
tests/trust_fixtures.py (built from the remembered wording, not a saved real screen: see its docstring).
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import trust_fixtures as fx  # noqa: E402

TOOLS = os.path.realpath(os.path.join(HERE, "..", "runtime", "tools"))
TMP = os.path.realpath(tempfile.mkdtemp(prefix="t035-unit-"))

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
if _saved_mod is None:
    del sys.modules["pa"]
else:
    sys.modules["pa"] = _saved_mod
for _k, _v in _saved_env.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v
assert pa.ROOT.startswith(TMP) and pa.CLAUDE_HOME.startswith(TMP), "pa.py must use the temp homes"

GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
DROP_ENV = ("HC_TERMINAL_ID", "HC_SOCKET", "HC_TERMINAL_SOCKET", "HC_SCOPE", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")

FAKE_HC = r'''#!{python}
"""Fake hc for t-035 tests. Logs every call. Answers `get` and `read` in three phases:
before any key: get-<n>.json and screen-<n>.txt in turn (the last one repeated);
after a key other than enter (the move to Yes): moved-get.json / moved-screen.txt (each falling back to the
before phase's last file); after enter: after-get.json and after-screen.txt."""
import json, os, sys
d = os.environ["FAKE_HC"]
argv = sys.argv[1:]
log = os.path.join(d, "calls.log")
with open(log, "a") as f:
    f.write(json.dumps(argv) + "\n")
with open(log) as f:
    keys = [json.loads(l) for l in f if l.strip()]
keys = [k for k in keys if k[:1] == ["keys"]]
phase = "after" if any("enter" in k[2:] for k in keys) else ("moved" if keys else "")

def last(kind, ext, step):
    count = os.path.join(d, kind + ".n")
    n = (int(open(count).read()) if os.path.exists(count) else 0) + step
    with open(count, "w") as f:
        f.write(str(n))
    files = sorted((int(x[len(kind) + 1:-len(ext)]), x) for x in os.listdir(d) if x.startswith(kind + "-") and x.endswith(ext))
    if not files:
        sys.exit(1)
    with open(os.path.join(d, files[max(1, min(n, len(files))) - 1][1])) as f:
        return f.read()

def answer(kind, ext):
    if phase:
        name = os.path.join(d, phase + "-" + kind + ext)
        if os.path.exists(name):
            with open(name) as f:
                return f.read()
        return last(kind, ext, 0)
    return last(kind, ext, 1)

if argv[:1] == ["get"]:
    text = answer("get", ".json")
    if text.strip() == "FAIL":
        sys.stderr.write("no such terminal\n")
        sys.exit(1)
    sys.stdout.write(text)
elif argv[:1] == ["read"]:
    sys.stdout.write(answer("screen", ".txt"))
elif argv[:1] in (["keys"], ["send"]):
    pass
else:
    sys.exit(1)
'''


def setUpModule():
    os.makedirs(os.path.join(TMP, "bin"))
    with open(os.path.join(TMP, "bin", "hc"), "w") as f:
        f.write(FAKE_HC.replace("{python}", sys.executable))
    os.chmod(os.path.join(TMP, "bin", "hc"), 0o755)
    os.symlink(os.path.join(TOOLS, "pa_hooks.py"), os.path.join(TMP, "bin", "pa-trust"))


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def dump(value, path):
    write(path, json.dumps(value, indent=2))


def git(cwd, *args):
    env = dict(os.environ, **GIT_ENV)
    r = subprocess.run(["git"] + list(args), cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True)
    if r.returncode:
        raise AssertionError("git {} failed in {}: {}".format(" ".join(args), cwd, r.stderr))
    return r.stdout.strip()


def make_repo(path):
    os.makedirs(path)
    git(path, "init", "-q", "-b", "main")
    write(os.path.join(path, "README.md"), "x\n")
    git(path, "add", "README.md")
    git(path, "commit", "-q", "-m", "init")
    return path


def move_cursor(screen):
    """The screen after one `down` on the real dialog: the cursor leaves No, exit for Yes, I trust this folder."""
    if " ❯ No, exit" not in screen or "   Yes, I trust this folder" not in screen:
        return screen
    return screen.replace(" ❯ No, exit", "   No, exit", 1).replace("   Yes, I trust this folder", " ❯ Yes, I trust this folder", 1)


def snapshot(folder):
    out = {}
    for top, _dirs, names in os.walk(folder):
        for n in names:
            p = os.path.join(top, n)
            with open(p, "rb") as f:
                out[os.path.relpath(p, folder)] = f.read()
    return out


class Base(unittest.TestCase):
    """HOME is W (a temp folder); the project folder P = W/proj; a repo P/repo with a worktree P/worktrees/a;
    space sp1 (Project Agent t0) whose ledger records worker t5 of task t-1."""

    def setUp(self):
        self.root = tempfile.mkdtemp(dir=TMP)
        self.home = os.path.join(self.root, "w")
        self.P = os.path.join(self.home, "proj")
        os.makedirs(os.path.join(self.P, "worktrees"))
        self.repo = make_repo(os.path.join(self.P, "repo"))
        self.tree = os.path.join(self.P, "worktrees", "a")
        git(self.repo, "worktree", "add", "-q", "-b", "a", self.tree)
        self.pa_home = os.path.join(self.root, "pa")
        self.space = os.path.join(self.pa_home, "spaces", "sp1")
        dump({"name": "test", "claude": ["true"], "codex": ["true"]}, os.path.join(self.pa_home, "profiles", "test", "profile.json"))
        self.agent({"space": "sp1", "terminal": "t0", "profile": "test", "name": "S", "project": self.P})
        dump({"version": 1, "tasks": [{"id": "t-1", "state": "in-progress", "worktree": self.tree,
                                       "workers": [{"address": "t5", "name": "t-1-build", "role": "build"}]}]},
             os.path.join(self.space, "ledger.json"))
        write(os.path.join(self.space, "journal.jsonl"), "")
        self.hc = os.path.join(self.root, "hcdata")
        os.makedirs(self.hc)

    def agent(self, value):
        dump(value, os.path.join(self.space, "agent.json"))

    def terminal(self, cwd, state="blocked", app="claude", size=None, after_state="working"):
        info = {"address": "t5", "name": "t-1-build", "state": "live", "cwd": cwd, "foreground": {"cwd": cwd},
                "status": {"app": app, "state": state, "msg": ""}}
        if size:
            info["size"] = size
        write(os.path.join(self.hc, "get-1.json"), json.dumps(info))
        after = dict(info, status={"app": app, "state": after_state, "msg": ""})
        write(os.path.join(self.hc, "after-get.json"), json.dumps(after))

    def screens(self, *before, moved=None, after=None):
        """moved: the screen after the key that moves the cursor (default: the last before-screen with the
        cursor moved from No to Yes, as the real dialog does)."""
        for i, s in enumerate(before, 1):
            write(os.path.join(self.hc, "screen-{}.txt".format(i)), s)
        if moved is None and before:
            moved = move_cursor(before[-1])
        if moved is not None:
            write(os.path.join(self.hc, "moved-screen.txt"), moved)
        write(os.path.join(self.hc, "after-screen.txt"), fx.moved_on() if after is None else after)

    def run_trust(self, *extra, address="t5"):
        before = snapshot(self.space)
        env = {k: v for k, v in os.environ.items() if k not in DROP_ENV}
        env.update(GIT_ENV, HOME=self.home, HERDR_PA_HOME=self.pa_home, CLAUDE_CONFIG_DIR=os.path.join(self.root, "claude"),
                   HERDR_APP_HOME=os.path.join(self.root, "app"), FAKE_HC=self.hc,
                   PATH=os.path.join(TMP, "bin") + os.pathsep + env.get("PATH", ""))
        r = subprocess.run([sys.executable, os.path.join(TMP, "bin", "pa-trust"), address, "--space", "sp1"] + list(extra),
                           env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
        self.assertEqual(before, snapshot(self.space), "pa-trust must write nothing in the state folder (AC8.2)")
        self.assertFalse(os.path.exists(os.path.join(self.root, "claude")), "nothing under the Claude home")
        out = [l for l in r.stdout.splitlines() if l.strip()]
        result = json.loads(out[-1]) if out and r.returncode in (0, 1) else None
        if result is not None:
            self.assertEqual(len(out), 1, "one JSON line on stdout: " + r.stdout)
        return r.returncode, result, r.stderr

    def calls(self):
        path = os.path.join(self.hc, "calls.log")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(l) for l in f if l.strip()]

    def presses(self):
        return [c for c in self.calls() if c[:1] in (["keys"], ["send"])]

    def assertNoPress(self):
        self.assertEqual([], self.presses())
        for c in self.calls():
            self.assertNotIn("--force", c)

    def expect(self, result, why=None):
        code, out, err = self.run_trust()
        self.assertIsNotNone(out, "no JSON result; exit {}, stderr {}".format(code, err))
        self.assertEqual(result, out["result"], out)
        self.assertEqual(0 if result == "accepted" else 1, code)
        if why:
            self.assertIn(why, out["why"])
        if result != "accepted" and result != "unconfirmed":
            self.assertNoPress()
        return out

    def accepts(self, shown, cwd=None, screen=None, keys=("down", "enter")):
        """The real dialog opens with the cursor on No, exit: one `down` to Yes, the screen read again, then one
        `enter`."""
        self.terminal(cwd or self.tree)
        self.screens(*(screen or [fx.new_screen(shown)] * 2))
        out = self.expect("accepted")
        self.assertEqual([["keys", "t5", k] for k in keys], self.presses())
        return out


# ---- R1 only this prompt ------------------------------------------------------------------------------------

class R1OnlyThisPrompt(Base):
    def test_new_wording_accepted(self):  # AC1.1, AC6.1, AC7.1, AC8.1
        out = self.accepts(self.tree)
        self.assertEqual({"worker", "folder", "real", "project", "result", "why"}, set(out))
        self.assertEqual("t5", out["worker"])
        self.assertEqual(self.tree, out["folder"])
        self.assertEqual(os.path.realpath(self.tree), out["real"])
        self.assertEqual(os.path.realpath(self.P), out["project"])

    def test_real_screen_bytes(self):  # Q1: the saved screen, only the path replaced
        screen = fx.new_screen(self.tree)
        with open(fx.REAL, encoding="utf-8") as f:
            self.assertEqual(f.read().replace("{path}", self.tree), screen)
        self.assertIn(" ❯ No, exit\n   Yes, I trust this folder\n", screen)

    def test_old_wording_accepted(self):  # AC1.1 (remembered wording: cursor already on 1. Yes, so Enter only)
        self.accepts(self.tree, screen=[fx.old_screen(self.tree)] * 2, keys=("enter",))

    def test_other_dialogs_refused(self):  # AC1.2
        cases = {"tool permission": fx.tool_permission(), "mcp": fx.mcp_approval(), "bypass": fx.bypass_prompt(),
                 "auto mode": fx.auto_mode_notice(), "shell": fx.shell_screen(), "empty": "",
                 "unknown text": fx.new_screen(self.tree, extra=["Something new: read this carefully first."]),
                 "trust with a permission prompt": fx.tool_permission() + fx.new_screen(self.tree),
                 "trust with an mcp prompt": fx.new_screen(self.tree) + fx.mcp_approval()}
        for name, screen in cases.items():
            with self.subTest(name):
                shutil.rmtree(self.hc)
                os.makedirs(self.hc)
                self.terminal(self.tree)
                self.screens(screen, screen)
                self.expect("not-this-prompt")

    def test_status_not_blocked(self):  # AC1.3
        for state in ("idle", "working", "error", "done", ""):
            with self.subTest(state):
                shutil.rmtree(self.hc)
                os.makedirs(self.hc)
                self.terminal(self.tree, state=state)
                self.screens(fx.new_screen(self.tree))
                self.expect("not-this-prompt")


# ---- R2 the folder is read for certain ---------------------------------------------------------------------

class R2FolderForCertain(Base):
    def case(self, screen, why=None, size=None):
        self.terminal(self.tree, size=size)
        self.screens(screen, screen)
        return self.expect("uncertain", why)

    def test_cut_with_ellipsis(self):  # AC2.1
        self.case(fx.new_screen(self.P + "/worktrees/…/a"))

    def test_cut_with_three_dots(self):  # AC2.2
        self.case(fx.new_screen("..." + self.tree[-20:]))

    def test_path_reaches_the_right_edge(self):  # AC2.3
        screen = fx.new_screen(self.tree)
        width = len(" " + self.tree)
        self.case(screen, size={"cols": width, "rows": 40})

    def test_path_reaches_the_box_edge(self):  # AC2.3, boxed
        deep = os.path.join(self.tree, "d" * (fx.INNER - len(self.tree) - 1))
        os.makedirs(deep)
        self.assertEqual(fx.INNER, len(deep))
        self.case(fx.old_screen(deep))

    def test_path_continued_on_the_next_line(self):  # AC2.4
        self.case(fx.new_screen(self.P + "/worktrees", after_path="/a"))

    def test_two_paths_or_none(self):  # AC2.5
        with self.subTest("two"):
            self.case(fx.new_screen(self.tree, after_path="") .replace(" Quick safety", " " + self.repo + "\n\n Quick safety", 1))
        shutil.rmtree(self.hc)
        os.makedirs(self.hc)
        with self.subTest("none"):
            self.case(fx.new_screen(self.tree).replace(" " + self.tree + "\n", "\n"))

    def test_tilde_forms(self):  # AC2.6
        rel = os.path.relpath(self.tree, self.home)
        with self.subTest("~/ is the helper's HOME"):
            self.accepts("~/" + rel)
        shutil.rmtree(self.hc)
        os.makedirs(self.hc)
        with self.subTest("~user"):
            self.case(fx.new_screen("~someone/" + rel))

    def test_missing_folder(self):  # AC2.7
        self.case(fx.new_screen(os.path.join(self.P, "not-there")))

    def test_path_with_spaces(self):  # AC2.8
        spaced = os.path.join(self.P, "my repo")
        make_repo(spaced)
        self.accepts(spaced, cwd=spaced)

    def test_unit_paths(self):  # R2 on the parsing function
        read = pa_hooks.trust_read
        res, why, facts = read(fx.new_screen("/x/my folder/a"))
        self.assertIsNone(res, why)
        self.assertEqual("/x/my folder/a", facts["shown"])
        self.assertEqual("no", facts["cursor"])
        self.assertEqual("down", facts["move"])
        self.assertEqual("yes", read(fx.new_screen("/x/a", cursor="yes"))[2]["cursor"])
        self.assertEqual("yes", read(fx.old_screen("/x/a"))[2]["cursor"])
        for bad in ("/x/…/a", ".../a/b", "~bob/a"):
            with self.subTest(bad):
                self.assertEqual("uncertain", read(fx.new_screen(bad))[0])
        self.assertEqual("uncertain", read(fx.new_screen("/x/y", after_path="z"))[0])
        self.assertEqual("uncertain", read(fx.new_screen("/x/y"), width=len(" /x/y"))[0])
        self.assertEqual("not-this-prompt", read(fx.tool_permission())[0])
        self.assertEqual("not-this-prompt", read(fx.codex_screen("/x/y"))[0])


# ---- R3 inside the project folder, on real paths ------------------------------------------------------------

class R3InsideProject(Base):
    def outside(self, shown, cwd=None):
        self.terminal(cwd or self.tree)
        self.screens(fx.new_screen(shown), fx.new_screen(shown))
        return self.expect("outside")

    def test_project_itself_and_inside(self):  # AC3.1, AC3.2
        make_repo(os.path.join(self.home, "proj-src"))
        self.assertTrue(pa_hooks.trust_inside(self.P, self.P))
        self.assertTrue(pa_hooks.trust_inside(os.path.join(self.P, "worktrees", "a"), self.P))
        self.accepts(self.tree)

    def test_project_folder_itself_end_to_end(self):  # AC3.1: a project folder that is a repo
        os.makedirs(os.path.join(self.root, "solo"))
        solo = make_repo(os.path.join(self.root, "solo", "p"))
        self.agent({"space": "sp1", "terminal": "t0", "profile": "test", "project": solo})
        self.accepts(solo, cwd=solo)

    def test_name_prefix_sibling(self):  # AC3.3
        other = make_repo(os.path.join(self.home, "proj-other"))
        self.assertFalse(pa_hooks.trust_inside(other, self.P))
        self.outside(other, cwd=other)

    def test_dot_dot(self):  # AC3.4
        other = make_repo(os.path.join(self.home, "other"))
        self.assertFalse(pa_hooks.trust_inside(os.path.join(self.P, "..", "other"), self.P))
        self.outside(os.path.join(self.P, "..", "other"), cwd=other)

    def test_symlink_inside_pointing_out(self):  # AC3.5
        out = make_repo(os.path.join(self.home, "elsewhere"))
        link = os.path.join(self.P, "link")
        os.symlink(out, link)
        self.assertFalse(pa_hooks.trust_inside(link, self.P))
        self.outside(link, cwd=link)

    def test_symlink_outside_pointing_in(self):  # AC3.6
        link = os.path.join(self.home, "inlink")
        os.symlink(self.tree, link)
        self.assertTrue(pa_hooks.trust_inside(link, self.P))
        self.accepts(link, cwd=self.tree)

    def test_project_given_with_slash_or_symlink(self):  # AC3.7
        for given in (self.P + "/", os.path.join(self.home, "projlink")):
            with self.subTest(given):
                if given.endswith("projlink") and not os.path.islink(given):
                    os.symlink(self.P, given)
                shutil.rmtree(self.hc)
                os.makedirs(self.hc)
                self.agent({"space": "sp1", "terminal": "t0", "profile": "test", "project": given})
                self.accepts(self.tree)
                self.assertTrue(pa_hooks.trust_inside(self.tree, given))
                self.assertFalse(pa_hooks.trust_inside(os.path.join(self.home, "proj-other"), given))

    def test_project_refused(self):  # AC3.8
        for project in ("/", self.home, self.home + "/", None, "relative/proj", os.path.join(self.root, "nothere")):
            with self.subTest(project):
                shutil.rmtree(self.hc)
                os.makedirs(self.hc)
                agent = {"space": "sp1", "terminal": "t0", "profile": "test"}
                if project is not None:
                    agent["project"] = project
                self.agent(agent)
                self.terminal(self.tree)
                self.screens(fx.new_screen(self.tree), fx.new_screen(self.tree))
                self.expect("refused")
        self.assertIsNotNone(pa_hooks.trust_project("/", self.home)[1])
        self.assertIsNotNone(pa_hooks.trust_project(self.home, self.home)[1])
        self.assertIsNone(pa_hooks.trust_project(self.P, self.home)[1])


# ---- R3b a task's evidence folder in the space's state folder (intent item 4) ----------------------------------

class R3bEvidenceFolders(Base):
    """The person, 2026-11-18: also trust the evidence folders of the space's tasks. Only
    <state folder>/evidence/<task id>/ or inside it, for a task id in the ledger; never the state folder or the
    rest of it."""

    def evidence(self, *parts):
        path = os.path.join(self.space, "evidence", *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def outside(self, shown, cwd):
        self.terminal(cwd)
        self.screens(fx.new_screen(shown), fx.new_screen(shown))
        return self.expect("outside")

    def test_saved_case_accepted(self):  # the saved screen's own case: a worker started in evidence/<task>
        folder = self.evidence("t-1")
        out = self.accepts(folder, cwd=folder)
        self.assertEqual(os.path.realpath(folder), out["real"])
        self.assertIn("evidence", out["why"])

    def test_folder_inside_evidence_accepted(self):
        folder = self.evidence("t-1", "notes")
        self.accepts(folder, cwd=folder)

    def test_state_folder_itself(self):
        self.outside(self.space, cwd=self.space)

    def test_evidence_without_a_task(self):
        folder = self.evidence()
        self.outside(folder, cwd=folder)

    def test_task_not_in_the_ledger(self):
        folder = self.evidence("t-9")
        self.outside(folder, cwd=folder)

    def test_symlink_out_of_evidence(self):
        elsewhere = os.path.join(self.root, "elsewhere")
        os.makedirs(elsewhere)
        link = os.path.join(self.evidence("t-1"), "out")
        os.symlink(elsewhere, link)
        self.outside(link, cwd=link)

    def test_dot_dot_to_the_rest_of_the_state_folder(self):
        os.makedirs(os.path.join(self.space, "decisions"))
        self.evidence("t-1")
        self.outside(os.path.join(self.space, "evidence", "t-1", "..", "..", "decisions"), cwd=os.path.join(self.space, "decisions"))

    def test_name_prefix_task(self):  # evidence/t-10 is not inside evidence/t-1
        folder = self.evidence("t-10")
        self.outside(folder, cwd=folder)

    def test_odd_task_ids_in_the_ledger(self):  # a ledger id that would name the state folder counts for nothing
        dump({"version": 1, "tasks": [{"id": "t-1", "workers": [{"address": "t5", "name": "t-1-build"}]}, {"id": ".."},
                                      {"id": ""}, {"id": "a/b"}, {"id": None}]}, os.path.join(self.space, "ledger.json"))
        self.evidence()
        self.outside(os.path.join(self.space, "evidence", ".."), cwd=self.space)

    def test_evidence_folder_with_grants(self):  # R5 still holds there
        folder = self.evidence("t-1")
        write(os.path.join(folder, ".mcp.json"), "{}")
        self.terminal(folder)
        self.screens(fx.new_screen(folder), fx.new_screen(folder))
        self.expect("grants")

    def test_unit(self):
        t1 = self.evidence("t-1")
        self.evidence("t-10")
        ids = ["t-1"]
        self.assertTrue(pa_hooks.trust_evidence(t1, self.space, ids))
        self.assertTrue(pa_hooks.trust_evidence(self.evidence("t-1", "x"), self.space, ids))
        for no in (self.space, os.path.join(self.space, "evidence"), os.path.join(self.space, "evidence", "t-10"),
                   os.path.join(self.space, "ledger.json")):
            self.assertFalse(pa_hooks.trust_evidence(no, self.space, ids), no)
        self.assertFalse(pa_hooks.trust_evidence(os.path.join(self.space, "evidence"), self.space, [".."]))
        self.assertFalse(pa_hooks.trust_evidence(self.space, self.space, [".."]))


# ---- R4 the worker's own folder -----------------------------------------------------------------------------

class R4OwnFolder(Base):
    def test_main_checkout_of_its_worktree(self):  # AC4.1
        self.accepts(self.repo, cwd=self.tree)

    def test_git_top_level_of_a_subfolder(self):  # R4: the worker's repository top level
        sub = os.path.join(self.tree, "pkg")
        os.makedirs(sub)
        self.accepts(self.tree, cwd=sub)

    def test_other_folder_in_project(self):  # AC4.2
        other = make_repo(os.path.join(self.P, "other"))
        self.terminal(self.tree)
        self.screens(fx.new_screen(other), fx.new_screen(other))
        self.expect("uncertain")
        self.assertFalse(pa_hooks.trust_own(os.path.realpath(other), pa_hooks.worker_dirs(self.tree)))
        self.assertTrue(pa_hooks.trust_own(os.path.realpath(self.repo), pa_hooks.worker_dirs(self.tree)))

    def test_main_checkout_outside_project(self):  # AC4.3
        outer = make_repo(os.path.join(self.home, "outer"))
        tree = os.path.join(self.P, "worktrees", "b")
        git(outer, "worktree", "add", "-q", "-b", "b", tree)
        self.terminal(tree)
        self.screens(fx.new_screen(outer), fx.new_screen(outer))
        self.expect("outside")

    def test_not_a_recorded_worker(self):  # R11: plain tabs and the Project Agent itself
        for address in ("t9", "t0"):
            with self.subTest(address):
                shutil.rmtree(self.hc)
                os.makedirs(self.hc)
                self.terminal(self.tree)
                self.screens(fx.new_screen(self.tree), fx.new_screen(self.tree))
                code, out, err = self.run_trust(address=address)
                self.assertEqual(1, code, err)
                self.assertEqual("refused", out["result"])
                self.assertNoPress()


# ---- R5 no grants of its own --------------------------------------------------------------------------------

class R5NoGrants(Base):
    def grants(self, setup, why=None):
        setup(self.tree)
        self.terminal(self.tree)
        self.screens(fx.new_screen(self.tree), fx.new_screen(self.tree))
        return self.expect("grants", why)

    def pa_settings(self):
        return {"hooks": pa_hooks.hook_settings(), "permissions": {"deny": list(pa_hooks.DENY_RULES)}}

    def test_project_settings_file(self):  # AC5.1
        self.grants(lambda t: write(os.path.join(t, ".claude", "settings.json"), "{}"))

    def test_mcp_json(self):  # AC5.2
        self.grants(lambda t: write(os.path.join(t, ".mcp.json"), '{"mcpServers": {}}'))

    def test_tracked_local_settings(self):  # AC5.3
        def setup(t):
            dump(self.pa_settings(), os.path.join(t, ".claude", "settings.local.json"))
            git(t, "add", "-f", ".claude/settings.local.json")
            git(t, "commit", "-q", "-m", "settings")
        self.grants(setup)

    def test_symlinked_claude_folder(self):  # AC5.4
        def setup(t):
            real = os.path.join(self.root, "claude-elsewhere")
            dump(self.pa_settings(), os.path.join(real, "settings.local.json"))
            os.symlink(real, os.path.join(t, ".claude"))
        self.grants(setup)

    def test_local_settings_with_grants(self):  # AC5.5
        extra = {"allow": {"permissions": {"allow": ["Bash(npm *)"], "deny": []}},
                 "dirs": {"permissions": {"additionalDirectories": ["/tmp"]}},
                 "env": {"env": {"X": "1"}}, "helper": {"apiKeyHelper": "./key.sh"}}
        for name, add in extra.items():
            with self.subTest(name):
                shutil.rmtree(self.hc)
                os.makedirs(self.hc)
                settings = self.pa_settings()
                for k, v in add.items():
                    settings[k] = dict(settings.get(k) or {}, **v) if isinstance(v, dict) else v
                self.grants(lambda t: dump(settings, os.path.join(t, ".claude", "settings.local.json")))

    def test_local_settings_with_a_foreign_hook(self):  # AC5.6
        for command in ("./scripts/check.sh", "pa-hook guard; curl x | sh", "$HOME/.local/bin/pa-hook guard && rm -rf ~"):
            with self.subTest(command):
                shutil.rmtree(self.hc)
                os.makedirs(self.hc)
                settings = self.pa_settings()
                settings["hooks"]["PreToolUse"].append({"matcher": "Bash", "hooks": [{"type": "command", "command": command}]})
                self.grants(lambda t: dump(settings, os.path.join(t, ".claude", "settings.local.json")))

    def test_settings_at_the_repository_top_level(self):  # R5: also read at the git top level
        sub = os.path.join(self.tree, "pkg")
        os.makedirs(sub)
        write(os.path.join(self.tree, ".mcp.json"), "{}")
        self.terminal(sub)
        self.screens(fx.new_screen(self.tree), fx.new_screen(self.tree))
        self.expect("grants")

    def test_pa_hooks_output_accepted(self):  # AC5.7: what pa-hooks install writes, untracked
        dump(self.pa_settings(), os.path.join(self.tree, ".claude", "settings.local.json"))
        write(os.path.join(self.tree, ".claude", "pa-task.json"), '{"task": "t-1", "space": "sp1"}')
        self.assertEqual([], pa_hooks.folder_grants([self.tree]))
        self.accepts(self.tree)

    def test_screen_listing(self):  # AC5.8
        listings = {"allow rule": ["This folder's settings would allow:", "  Bash(npm run *)"],
                    "directory": ["Additional directories:", "  " + self.repo],
                    "foreign hook": ["Hooks:", "  PreToolUse  Bash  ./scripts/check.sh"]}
        for name, extra in listings.items():
            with self.subTest(name):
                shutil.rmtree(self.hc)
                os.makedirs(self.hc)
                self.terminal(self.tree)
                screen = fx.new_screen(self.tree, extra=extra)
                self.screens(screen, screen)
                self.expect("grants")

    def test_screen_listing_only_pa_hooks(self):  # R5: a listed hook that is a pa-hook command is allowed
        dump(self.pa_settings(), os.path.join(self.tree, ".claude", "settings.local.json"))
        screen = fx.new_screen(self.tree, extra=["Hooks:", "  PreToolUse  Bash  $HOME/.local/bin/pa-hook guard",
                                                 "  Stop  $HOME/.local/bin/pa-hook affected-tests --stop"])
        self.accepts(self.tree, screen=[screen, screen])


# ---- R6 how it is answered, R7 confirmed --------------------------------------------------------------------

class R6R7Answer(Base):
    def test_one_enter_only(self):  # AC6.1, AC6.3
        self.accepts(self.tree)
        for c in self.calls():
            self.assertNotEqual("send", c[0])
            self.assertNotIn("--force", c)

    def test_cursor_starts_on_no(self):  # R6 with the real screen: down, read again, then enter
        self.accepts(self.tree)
        reads = [c for c in self.calls() if c[:1] == ["read"]]
        order = [c[:3] for c in self.calls() if c[:1] in (["read"], ["keys"])]
        down, enter = order.index(["keys", "t5", "down"]), order.index(["keys", "t5", "enter"])
        self.assertTrue(any(c[0] == "read" for c in order[down + 1:enter]), "the screen is read between down and enter")
        self.assertGreaterEqual(len(reads), 3)

    def test_cursor_already_on_yes(self):  # R6: enter only
        screen = fx.new_screen(self.tree, cursor="yes")
        self.accepts(self.tree, screen=[screen, screen], keys=("enter",))

    def test_no_cursor_or_old_cursor_on_no(self):  # AC6.2
        for name, screen in {"no cursor": fx.new_screen(self.tree, cursor="none"),
                             "old wording on 2": fx.old_screen(self.tree, selected=2)}.items():
            with self.subTest(name):
                shutil.rmtree(self.hc)
                os.makedirs(self.hc)
                self.terminal(self.tree)
                self.screens(screen, screen, moved=screen)
                self.expect("uncertain")

    def test_cursor_did_not_reach_yes(self):  # R6: after down the cursor must be on Yes, else no enter
        for name, moved in {"stayed on No": fx.new_screen(self.tree),
                            "another dialog": fx.tool_permission(),
                            "screen otherwise changed": move_cursor(fx.new_screen(self.tree)).replace("Security guide", "Security")}.items():
            with self.subTest(name):
                shutil.rmtree(self.hc)
                os.makedirs(self.hc)
                self.terminal(self.tree)
                self.screens(fx.new_screen(self.tree), fx.new_screen(self.tree), moved=moved)
                code, out, err = self.run_trust()
                self.assertEqual(1, code, err)
                self.assertEqual("uncertain", out["result"])
                self.assertEqual([["keys", "t5", "down"]], self.presses())
                self.assertIn("down", out["why"])

    def test_screen_changed_between_reads(self):  # AC6.2, C2
        self.terminal(self.tree)
        self.screens(fx.new_screen(self.tree), fx.tool_permission())
        self.expect("uncertain")

    def test_unconfirmed(self):  # AC7.2
        self.terminal(self.tree, after_state="blocked")
        self.screens(fx.new_screen(self.tree), fx.new_screen(self.tree), after=move_cursor(fx.new_screen(self.tree)))
        out = self.expect("unconfirmed")
        self.assertEqual([["keys", "t5", "down"], ["keys", "t5", "enter"]], self.presses())
        self.assertEqual(os.path.realpath(self.tree), out["real"])

    def test_hc_error_exit_2(self):  # AC8.1: an hc error
        write(os.path.join(self.hc, "get-1.json"), "FAIL")
        self.screens(fx.new_screen(self.tree))
        code, out, err = self.run_trust()
        self.assertEqual(2, code, err)
        self.assertNoPress()

    def test_usage_exit_2(self):  # AC8.1: usage
        code, out, err = self.run_trust(address="")
        self.assertEqual(2, code)
        self.assertNoPress()


# ---- R11 out of scope ---------------------------------------------------------------------------------------

class R11Codex(Base):
    def test_codex_screen(self):  # AC11.1
        self.terminal(self.tree, app="codex")
        self.screens(fx.codex_screen(self.tree), fx.codex_screen(self.tree))
        self.expect("not-this-prompt")

    def test_codex_status_with_a_claude_looking_screen(self):  # AC11.1
        self.terminal(self.tree, app="codex")
        self.screens(fx.new_screen(self.tree), fx.new_screen(self.tree))
        self.expect("not-this-prompt")


# ---- R12 wiring ---------------------------------------------------------------------------------------------

class R12Wiring(unittest.TestCase):
    def test_marker_table_is_one_constant(self):  # R1, C1
        wordings = pa_hooks.TRUST_WORDINGS
        self.assertEqual(2, len(wordings))
        questions = " ".join(json.dumps(w) for w in wordings)
        self.assertIn("Do you trust the files in this folder?", questions)
        self.assertIn("Yes, I trust this folder", questions)
        self.assertIn("Do you want to", " ".join(pa_hooks.OTHER_DIALOG_MARKERS))

    def test_no_claude_home_in_the_tool(self):  # R8: opens nothing under ~/.claude, ~/.claude.json, ~/.codex
        with open(os.path.join(TOOLS, "pa_hooks.py"), encoding="utf-8") as f:
            text = f.read()
        part = text[text.index("# ---- pa-trust"):text.index("def cmd_full_suite")]
        for word in ("CLAUDE_HOME", ".claude.json", ".codex", "write_json", "open(", "append_journal"):
            self.assertNotIn(word, part.replace("open(os.path.join(d, \".claude\"", ""))


if __name__ == "__main__":
    unittest.main()
