"""Unit tests for the gate's commit pin (t-007 part 7a, R2): GATE.json records the exact commit(s) it passed.

Run: python3 -m unittest -v tests/test_pa_gate.py
No hc calls and no real homes: HOME, HERDR_PA_HOME, CLAUDE_CONFIG_DIR and HERDR_APP_HOME point at a temp
folder, hc is a stub, and git runs only on repositories made under that temp folder, with no global or
system git config. Nothing here pushes, and no repository has a remote.
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(HERE, "..", "runtime", "tools")
TMP = os.path.realpath(tempfile.mkdtemp(prefix="t007-gate-"))

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
assert pa.ROOT.startswith(TMP), "pa.py must use the temp homes"

TEST_ENV = {"HOME": os.path.join(TMP, "home"), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid", "HERDR_PA_HOME": _HOMES["HERDR_PA_HOME"]}
DROP_ENV = ("HC_TERMINAL_ID", "HC_SOCKET", "HC_TERMINAL_SOCKET", "HC_SCOPE", "CLAUDECODE", "GIT_DIR", "GIT_WORK_TREE")
_module_env = {}


def setUpModule():
    for k in list(TEST_ENV) + list(DROP_ENV):
        _module_env[k] = os.environ.get(k)
    for k in DROP_ENV:
        os.environ.pop(k, None)
    os.environ.update(TEST_ENV)
    os.makedirs(TEST_ENV["HOME"], exist_ok=True)


def tearDownModule():
    for k, v in _module_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    shutil.rmtree(TMP, ignore_errors=True)


def dump(value, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(value, f, indent=2)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def sh(cwd, *argv):
    r = subprocess.run(list(argv), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    if r.returncode:
        raise AssertionError("{} failed in {}: {}".format(" ".join(argv), cwd, r.stderr))
    return r.stdout.strip()


REVIEW_FILES = ["intent.md", "spec.md", "plan.md", "FINDINGS.md", "DECISIONS.md", "VERIFY.md", "REVIEW.md", "build.log"]
WORKERS = [{"address": "t31", "role": "build"}, {"address": "t32", "role": "reviewer"}]


class Base(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(pa.ROOT, ignore_errors=True)
        self.tmp = os.path.realpath(tempfile.mkdtemp(dir=TMP))
        self.dir = os.path.join(pa.ROOT, "spaces", "sp1")
        dump({"space": "sp1", "terminal": "t0", "terminals": ["t0"], "profile": "test", "name": "S"}, os.path.join(self.dir, "agent.json"))
        dump({"name": "test", "claude": ["true"], "codex": ["true"]}, os.path.join(pa.ROOT, "profiles", "test", "profile.json"))
        dump({"version": 1, "tasks": []}, os.path.join(self.dir, "ledger.json"))
        self.saved = {k: getattr(pa, k) for k in ("hc_get", "call")}
        pa.hc_get = lambda address: {}
        pa.call = self.no_call
        self.cwd = os.getcwd()
        os.chdir(self.tmp)  # no task record above this folder

    def tearDown(self):
        os.chdir(self.cwd)
        for k, v in self.saved.items():
            setattr(pa, k, v)

    def no_call(self, argv, check=True):
        raise AssertionError("no hc call expected: {}".format(argv))

    def add_task(self, tid, **fields):
        path = os.path.join(self.dir, "ledger.json")
        with open(path) as f:
            led = json.load(f)
        led["tasks"].append(dict({"id": tid, "title": "T", "state": "in-progress", "tier": "normal", "workers": WORKERS}, **fields))
        dump(led, path)
        return led["tasks"][-1]

    def evidence(self, tid, names=REVIEW_FILES):
        for name in names:
            write(os.path.join(self.dir, "evidence", tid, name), "text\n")

    def gate_file(self, tid):
        return os.path.join(self.dir, "evidence", tid, "GATE.json")

    def run_gate(self, tid, *extra):
        out, err, code = io.StringIO(), io.StringIO(), 0
        saved = sys.argv
        sys.argv = ["pa-gate", tid, "--space", "sp1"] + list(extra)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    pa_hooks.main()
                except SystemExit as e:
                    code = e.code if isinstance(e.code, int) else (0 if e.code is None else 2)
        finally:
            sys.argv = saved
        return code, out.getvalue(), err.getvalue()

    def gate_json(self, tid):
        with open(self.gate_file(tid)) as f:
            return json.load(f)

    # ---- git ----
    def repo(self, name="proj"):
        path = os.path.join(self.tmp, name)
        os.makedirs(path)
        sh(path, "git", "init", "-q", "-b", "main")
        write(os.path.join(path, "README"), "base\n")
        sh(path, "git", "add", "README")
        sh(path, "git", "commit", "-q", "-m", "base")
        return path

    def linked(self, repo, branch, path):
        sh(repo, "git", "worktree", "add", "-q", "-b", branch, path, "main")
        return path

    def commit(self, path, file, text):
        write(os.path.join(path, file), text)
        sh(path, "git", "add", file)
        sh(path, "git", "commit", "-q", "-m", "change " + file)
        return sh(path, "git", "rev-parse", "HEAD")


class G0Unchanged(Base):
    def test_g0_output_exit_code_and_keys_as_before_plus_empty_commits(self):
        self.add_task("t-1", type="docs")
        self.evidence("t-1")
        code, out, err = self.run_gate("t-1")
        self.assertEqual(code, 0, err)
        expected = ["ok    intent.md", "ok    spec.md", "ok    plan.md", "ok    FINDINGS.md", "ok    DECISIONS.md",
                    "ok    VERIFY.md", "ok    REVIEW.md", "ok    reviewer is not the author",
                    "pa-gate t-1 (review phase, normal tier): PASSED"]
        self.assertEqual(out.splitlines(), expected)
        result = self.gate_json("t-1")
        self.assertEqual(list(result), ["task", "phase", "tier", "passed", "at", "checks", "commits"])
        self.assertEqual(result["commits"], [])
        self.assertTrue(result["passed"])

    def test_g0b_a_failing_gate_still_fails_the_same_way(self):
        self.add_task("t-1", type="docs")
        self.evidence("t-1", ["intent.md"])
        code, out, _ = self.run_gate("t-1")
        self.assertEqual(code, 1)
        self.assertIn("FAIL  spec.md  (missing)", out)
        self.assertEqual(out.splitlines()[-1], "pa-gate t-1 (review phase, normal tier): FAILED. The task does not move.")
        result = self.gate_json("t-1")
        self.assertFalse(result["passed"])
        self.assertEqual(result["commits"], [])


class G1PlainWorktree(Base):
    def setUp(self):
        super().setUp()
        self.proj = self.repo()
        self.wt = self.linked(self.proj, "t-1-x", os.path.join(self.tmp, "wt"))
        self.head = self.commit(self.wt, "a.txt", "a\n")
        self.add_task("t-1", worktree=self.wt)
        self.evidence("t-1")

    def test_g1_one_entry_for_a_clean_linked_worktree(self):
        code, _out, err = self.run_gate("t-1")
        self.assertEqual(code, 0, err)
        commits = self.gate_json("t-1")["commits"]
        self.assertEqual(commits, [{"repo": os.path.realpath(self.wt),
                                    "common": os.path.realpath(os.path.join(self.proj, ".git")),
                                    "branch": "t-1-x", "commit": self.head, "uncommitted": 0}])
        self.assertRegex(commits[0]["commit"], r"^[0-9a-f]{40}([0-9a-f]{24})?$")

    def test_g2_uncommitted_is_recorded_not_failed(self):
        write(os.path.join(self.wt, "a.txt"), "changed\n")
        write(os.path.join(self.wt, "new.txt"), "untracked\n")
        code, _out, err = self.run_gate("t-1")
        self.assertEqual(code, 0, err)
        result = self.gate_json("t-1")
        self.assertTrue(result["passed"])
        self.assertEqual(result["commits"][0]["uncommitted"], 1)
        self.assertEqual(result["commits"][0]["commit"], self.head)

    def test_g6_a_commit_after_the_gate_gives_a_new_pin_on_the_next_run(self):
        self.run_gate("t-1")
        first = self.gate_json("t-1")["commits"][0]["commit"]
        later = self.commit(self.wt, "b.txt", "b\n")
        self.run_gate("t-1")
        second = self.gate_json("t-1")["commits"][0]["commit"]
        self.assertEqual(first, self.head)
        self.assertEqual(second, later)
        self.assertNotEqual(first, second)

    def test_g6b_any_later_phase_run_overwrites_the_pin(self):
        self.run_gate("t-1")
        self.run_gate("t-1", "--phase", "build")
        result = self.gate_json("t-1")
        self.assertEqual(result["phase"], "build")
        self.assertEqual(result["commits"][0]["commit"], self.head)

    def test_g7_claude_files_are_not_counted(self):
        # A tracked file under .claude/ (normally ignored) that changes: not counted.
        self.commit(self.wt, os.path.join(".claude", "notes.json"), "{}\n")
        write(os.path.join(self.wt, ".claude", "notes.json"), '{"x": 1}\n')
        write(os.path.join(self.wt, ".claude", "pa-task.json"), "{}\n")
        self.run_gate("t-1")
        self.assertEqual(self.gate_json("t-1")["commits"][0]["uncommitted"], 0)
        write(os.path.join(self.wt, "a.txt"), "changed\n")
        self.run_gate("t-1")
        self.assertEqual(self.gate_json("t-1")["commits"][0]["uncommitted"], 1)

    def test_g1b_a_subfolder_of_a_repository_is_not_a_top_level(self):
        sub = os.path.join(self.wt, "sub")
        os.makedirs(sub)
        self.add_task("t-2", worktree=sub)
        self.evidence("t-2")
        self.run_gate("t-2")
        self.assertEqual(self.gate_json("t-2")["commits"], [])


class G3PackageFolder(Base):
    def test_g3_a_folder_of_packages_gets_one_entry_per_package_sorted_by_path(self):
        a, b = self.repo("A-main"), self.repo("B-main")
        wt = os.path.join(self.tmp, "wt")
        os.makedirs(os.path.join(wt, "src"))
        write(os.path.join(wt, "workspace.txt"), "a folder of packages, not itself a repository\n")
        # made in reverse order, so sorting is seen
        self.linked(b, "t-1-x", os.path.join(wt, "src", "B"))
        self.linked(a, "t-1-x", os.path.join(wt, "src", "A"))
        os.makedirs(os.path.join(wt, "src", "C-not-git"))
        head_a = self.commit(os.path.join(wt, "src", "A"), "a.txt", "a\n")
        head_b = sh(os.path.join(wt, "src", "B"), "git", "rev-parse", "HEAD")
        self.add_task("t-1", worktree=wt)
        self.evidence("t-1")
        code, _out, err = self.run_gate("t-1")
        self.assertEqual(code, 0, err)
        commits = self.gate_json("t-1")["commits"]
        self.assertEqual([c["repo"] for c in commits], [os.path.join(wt, "src", "A"), os.path.join(wt, "src", "B")])
        self.assertEqual([c["commit"] for c in commits], [head_a, head_b])
        self.assertEqual([c["common"] for c in commits], [os.path.join(a, ".git"), os.path.join(b, ".git")])
        self.assertEqual({c["branch"] for c in commits}, {"t-1-x"})


class G4Missing(Base):
    def test_g4_a_missing_worktree_gives_no_entries_and_no_error(self):
        self.add_task("t-1", worktree=os.path.join(self.tmp, "gone"))
        self.evidence("t-1")
        code, out, err = self.run_gate("t-1")
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        self.assertTrue(out.splitlines()[-1].endswith("PASSED"))
        self.assertEqual(self.gate_json("t-1")["commits"], [])

    def test_g4b_a_folder_that_is_not_git_gives_no_entries(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        self.add_task("t-1", worktree=plain)
        self.evidence("t-1")
        code, _out, _err = self.run_gate("t-1")
        self.assertEqual(code, 0)
        self.assertEqual(self.gate_json("t-1")["commits"], [])


class G5NoSideEffects(Base):
    def test_g5_evaluate_gate_writes_nothing(self):
        proj = self.repo()
        wt = self.linked(proj, "t-1-x", os.path.join(self.tmp, "wt"))
        task = self.add_task("t-1", worktree=wt)
        self.evidence("t-1")
        before = {}
        for folder, _dirs, files in os.walk(pa.ROOT):
            for name in files:
                p = os.path.join(folder, name)
                before[p] = os.stat(p).st_mtime_ns
        time.sleep(0.01)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = pa_hooks.evaluate_gate(self.dir, task, "review")
        self.assertEqual(out.getvalue(), "")
        self.assertFalse(os.path.exists(self.gate_file("t-1")))
        after = {}
        for folder, _dirs, files in os.walk(pa.ROOT):
            for name in files:
                p = os.path.join(folder, name)
                after[p] = os.stat(p).st_mtime_ns
        self.assertEqual(after, before)
        self.assertTrue(result["passed"])
        self.assertEqual(result["task"], "t-1")
        self.assertEqual(result["commits"][0]["branch"], "t-1-x")

    def test_g5b_evaluate_gate_agrees_with_the_written_result(self):
        task = self.add_task("t-1", type="docs")
        self.evidence("t-1", ["intent.md", "spec.md"])
        result = pa_hooks.evaluate_gate(self.dir, task, "review")
        self.run_gate("t-1")
        written = self.gate_json("t-1")
        for key in ("task", "phase", "tier", "passed", "checks", "commits"):
            self.assertEqual(result[key], written[key], key)


class G6NoOptionalLocks(Base):
    def test_g6_gate_commits_runs_status_without_optional_locks(self):  # REVIEW-6 M6
        proj = self.repo()
        wt = self.linked(proj, "t-1-x", os.path.join(self.tmp, "wt"))
        calls = []
        real = pa_hooks.git

        def recording(path, *args, **kw):
            calls.append(args)
            return real(path, *args, **kw)

        pa_hooks.git = recording
        try:
            commits = pa_hooks.gate_commits(wt)
        finally:
            pa_hooks.git = real
        self.assertEqual(commits[0]["branch"], "t-1-x")
        status = [a for a in calls if "status" in a]
        self.assertTrue(status, calls)
        for args in status:
            self.assertEqual(args[0], "--no-optional-locks", args)


class J5CheckRole(Base):  # t-017 J5: R4 AC4.3, G3. The gate's tier is the task's ledger tier.
    BUILD = {"address": "t31", "role": "build"}
    REVIEWER = {"address": "t32", "role": "reviewer"}
    CHECK = {"address": "t33", "role": "check"}

    def review_check(self, tier, workers):
        dump({"version": 1, "tasks": []}, os.path.join(self.dir, "ledger.json"))
        task = self.add_task("t-1", type="docs", tier=tier, workers=workers)
        self.evidence("t-1")
        result = pa_hooks.evaluate_gate(self.dir, task, "review")
        found = [c for c in result["checks"] if c["check"] == "reviewer is not the author"]
        self.assertEqual(len(found), 1, result["checks"])
        return result, found[0]

    def test_j5_low_tier_check_worker_passes(self):
        result, check = self.review_check("low", [self.BUILD, self.CHECK])
        self.assertTrue(check["ok"], check)
        self.assertTrue(result["passed"], result["checks"])

    def test_j5_low_tier_check_worker_passes_through_the_command(self):
        self.add_task("t-1", type="docs", tier="low", workers=[self.BUILD, self.CHECK])
        self.evidence("t-1")
        code, out, err = self.run_gate("t-1")
        self.assertEqual(code, 0, out + err)
        self.assertIn("ok    reviewer is not the author", out)

    def test_j5_normal_and_high_tier_check_worker_alone_fails(self):
        for tier in ("normal", "high"):
            with self.subTest(tier=tier):
                result, check = self.review_check(tier, [self.BUILD, self.CHECK])
                self.assertFalse(check["ok"])
                self.assertFalse(result["passed"])
                self.assertEqual(check["why"], "the ledger lists no reviewer worker separate from the build "
                                               "workers (a check worker counts for low tier only)")

    def test_j5_check_worker_who_is_an_author_fails_at_every_tier(self):
        for tier, role in (("low", "build"), ("low", "design"), ("normal", "build"), ("high", "design")):
            for extra in ([], [self.REVIEWER]):
                with self.subTest(tier=tier, role=role, reviewer=bool(extra)):
                    workers = [{"address": "t31", "role": role}, {"address": "t31", "role": "check"}] + extra
                    result, check = self.review_check(tier, workers)
                    self.assertFalse(check["ok"], check)
                    self.assertFalse(result["passed"])
                    self.assertEqual(check["why"], "t31 is recorded as check and as a build or design worker")

    def test_j5_reviewer_who_is_an_author_names_the_role(self):
        workers = [self.BUILD, {"address": "t31", "role": "reviewer"}]
        _result, check = self.review_check("normal", workers)
        self.assertFalse(check["ok"])
        self.assertEqual(check["why"], "t31 is recorded as reviewer and as a build or design worker")

    def test_j5_interim_record_as_reviewer_still_passes(self):
        for tier in ("low", "normal", "high"):
            with self.subTest(tier=tier):
                result, check = self.review_check(tier, [self.BUILD, self.REVIEWER])
                self.assertTrue(check["ok"], check)
                self.assertTrue(result["passed"], result["checks"])

    def test_j5_low_tier_with_neither_names_both_roles(self):
        _result, check = self.review_check("low", [self.BUILD])
        self.assertFalse(check["ok"])
        self.assertEqual(check["why"], "the ledger lists no reviewer or check worker separate from the build workers")


if __name__ == "__main__":
    unittest.main()
