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
sys.path.insert(0, HERE)
from steer_fixtures import forge_parent  # noqa: E402 (t-052 J1 round 3)

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
        self.assertEqual(list(result), ["task", "phase", "tier", "passed", "at", "checks", "commits", "content"])
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


class D077ReviewerAddresses(Base):  # t-020 J1, card d-077 limit 2: only workers with an address count; roles in lower case
    def review_check(self, workers, tier="normal"):
        dump({"version": 1, "tasks": []}, os.path.join(self.dir, "ledger.json"))
        task = self.add_task("t-1", type="docs", tier=tier, workers=workers)
        self.evidence("t-1")
        result = pa_hooks.evaluate_gate(self.dir, task, "review")
        found = [c for c in result["checks"] if c["check"] == "reviewer is not the author"]
        self.assertEqual(len(found), 1, result["checks"])
        return found[0]

    def test_d077_a_reviewer_with_no_address_does_not_count(self):
        for reviewer in ({"role": "reviewer"}, {"address": "", "role": "reviewer"}, {"address": None, "role": "reviewer"}):
            with self.subTest(reviewer=reviewer):
                check = self.review_check([{"address": "t31", "role": "build"}, reviewer])
                self.assertFalse(check["ok"], check)
                self.assertEqual(check["why"], "the ledger lists no reviewer worker separate from the build workers "
                                               "(a check worker counts for low tier only)")
        check = self.review_check([{"address": "t31", "role": "build"}, {"role": "check"}], tier="low")
        self.assertFalse(check["ok"], check)

    def test_d077_an_author_recorded_as_build_in_capitals_is_still_an_author(self):
        for role in ("Build", "BUILD", "Design"):
            with self.subTest(role=role):
                check = self.review_check([{"address": "t31", "role": role}, {"address": "t31", "role": "reviewer"}])
                self.assertFalse(check["ok"], check)
                self.assertEqual(check["why"], "t31 is recorded as reviewer and as a build or design worker")
        check = self.review_check([{"address": "t31", "role": "build"}, {"address": "t31", "role": "Reviewer"}])
        self.assertFalse(check["ok"], check)
        self.assertEqual(check["why"], "t31 is recorded as Reviewer and as a build or design worker")

    def test_d077_a_reviewer_recorded_in_capitals_counts(self):
        self.assertTrue(self.review_check([{"address": "t31", "role": "build"}, {"address": "t32", "role": "Reviewer"}])["ok"])
        self.assertTrue(self.review_check([{"address": "t31", "role": "build"}, {"address": "t33", "role": "CHECK"}], tier="low")["ok"])

    def test_d077_an_author_with_no_address_is_no_author(self):
        # a build entry without an address cannot make an addressed reviewer its own author
        self.assertTrue(self.review_check([{"role": "build"}, {"address": "t32", "role": "reviewer"}])["ok"])


# ---- t-052 J1: checks valid by content (R4, R5, R7 AC7.1) ---------------------------------------

NINE = "".join("line {}\n".format(i) for i in range(1, 10))
SINCE = "2026-01-01T00:00:00Z"
AFTER, BEFORE = "2026-06-01T00:00:00Z", "2025-06-01T00:00:00Z"
CHECK = "the checks saw this content"


class ContentBase(Base):
    """main with a.txt (nine lines) and b.txt; the task branch t-1-x in a linked worktree changes line 2 of a.txt (f1)
    and adds c.txt (f2). The task's verifier started after the space's content stamp, so the check is required."""

    def setUp(self):
        super().setUp()
        self.proj = os.path.join(self.tmp, "proj")
        os.makedirs(self.proj)
        sh(self.proj, "git", "init", "-q", "-b", "main")
        write(os.path.join(self.proj, "a.txt"), NINE)
        write(os.path.join(self.proj, "b.txt"), "b\n")
        sh(self.proj, "git", "add", "a.txt", "b.txt")
        sh(self.proj, "git", "commit", "-q", "-m", "base")
        self.wt = self.linked(self.proj, "t-1-x", os.path.join(self.tmp, "wt"))
        self.f1 = self.commit(self.wt, "a.txt", NINE.replace("line 2\n", "line two\n"))
        self.f2 = self.commit(self.wt, "c.txt", "c\n")
        self.task = self.add_task("t-1", worktree=self.wt, type="feature", workers=WORKERS + [
            {"address": "t33", "role": "verifier", "started": AFTER}])
        self.evidence("t-1")
        self.stamp(SINCE)

    def stamp(self, at):
        dump({"version": 1, "at": at}, os.path.join(self.dir, "content", "since.json"))

    def checked(self, *shas, name="VERIFY.md"):
        write(os.path.join(self.dir, "evidence", "t-1", name),
              "text\n" + "".join("Checked: {}\n".format(s) for s in shas) + "Verdict: pass\n")

    def on_main(self, file, text, subject="main moves"):
        write(os.path.join(self.proj, file), text)
        sh(self.proj, "git", "add", file)
        sh(self.proj, "git", "commit", "-q", "-m", subject)

    def rebase(self):
        sh(self.wt, "git", "rebase", "-q", "main")
        return sh(self.wt, "git", "rev-parse", "HEAD")

    def head(self, path=None):
        return sh(path or self.wt, "git", "rev-parse", "HEAD")

    def content_line(self, out):
        lines = [l for l in out.splitlines() if CHECK in l]
        self.assertEqual(len(lines), 1, out)
        return lines[0]

    def assertContentOk(self, *note):
        code, out, err = self.run_gate("t-1")
        line = self.content_line(out)
        self.assertEqual(code, 0, out + err)
        self.assertTrue(line.startswith("ok    " + CHECK), line)
        for n in note:
            self.assertIn(n, line)
        return line

    def assertContentFails(self, *why):
        code, out, err = self.run_gate("t-1")
        line = self.content_line(out)
        self.assertEqual(code, 1, out + err)
        self.assertTrue(line.startswith("FAIL  " + CHECK), line)
        for w in why:
            self.assertIn(w, line)
        return line

    def records(self):
        return pa.read_json(os.path.join(self.dir, "content", "t-1.json"), {}) or {}


class C1SameCommit(ContentBase):
    def test_c1_checked_is_head(self):
        self.checked(self.f2)
        self.assertContentOk()
        content = self.gate_json("t-1")["content"]
        self.assertEqual(len(content), 1)
        self.assertIs(content[0]["same"], True)
        self.assertEqual(content[0]["checked"], self.f2)
        self.assertEqual(content[0]["onto"], "main")
        self.assertEqual(content[0]["repo"], os.path.realpath(self.wt))


class C2CleanRebase(ContentBase):
    def test_c2_one_and_three_clean_rebases_keep_the_content(self):
        self.checked(self.f2)
        self.assertContentOk()
        first = self.gate_json("t-1")["content"][0]
        for n in range(3):
            self.on_main("b.txt", "b {}\n".format(n))
            tip = self.rebase()
            self.assertNotEqual(tip, self.f2)
            line = self.assertContentOk("same content as the checked " + self.f2[:7])
            now = self.gate_json("t-1")["content"][0]
            self.assertEqual((now["change"], now["sequence"]), (first["change"], first["sequence"]), n)
            self.assertIs(now["same"], True)
            self.assertNotEqual(now["base"], first["base"])
            self.assertTrue(line)


class C3BaseTouchedAFile(ContentBase):
    def test_c3_main_changed_a_touched_file_outside_the_hunk(self):
        self.checked(self.f2)
        self.assertContentOk()
        self.on_main("a.txt", NINE.replace("line 8\n", "line eight\n"))
        self.rebase()  # no conflict: line 8 is outside f1's context
        self.assertContentFails("a.txt")
        content = self.gate_json("t-1")["content"][0]
        self.assertIs(content["same"], False)
        self.assertFalse(self.gate_json("t-1")["passed"])

    def test_c4_patch_id_stays_the_same_where_the_gate_fails(self):  # probe finding 3, as a regression
        def patch_id(sha):
            show = subprocess.run(["git", "show", sha], cwd=self.wt, stdout=subprocess.PIPE, check=True).stdout
            out = subprocess.run(["git", "patch-id", "--stable"], cwd=self.wt, input=show, stdout=subprocess.PIPE, check=True).stdout
            return out.split()[0]
        self.checked(self.f2)
        before = patch_id(self.f1)
        self.on_main("a.txt", NINE.replace("line 8\n", "line eight\n"))
        self.rebase()
        after = patch_id(sh(self.wt, "git", "rev-parse", "HEAD~1"))
        self.assertEqual(before, after)
        self.assertContentFails("a.txt")


class C5Sequence(ContentBase):
    def test_c5_an_empty_commit_after_the_check(self):
        self.checked(self.f2)
        sh(self.wt, "git", "commit", "-q", "--allow-empty", "-m", "later")
        line = self.assertContentFails("2 commits checked, 3 now")
        self.assertNotIn("a.txt", line)

    def test_c5b_a_reword(self):
        self.checked(self.f2)
        sh(self.wt, "git", "commit", "-q", "--amend", "-m", "reworded")
        self.assertContentFails("commits changed")

    def test_c5c_a_squash(self):
        self.checked(self.f2)
        sh(self.wt, "git", "reset", "-q", "--soft", "HEAD~2")
        sh(self.wt, "git", "commit", "-q", "-m", "both")
        self.assertContentFails("2 commits checked, 1 now")


class C6Files(ContentBase):
    ODD = ("sp ace.txt", "new\nline.txt", 'qu"ote.txt')

    def test_c6_rename_is_a_delete_and_an_add(self):
        sh(self.wt, "git", "mv", "c.txt", "d.txt")
        sh(self.wt, "git", "commit", "-q", "-m", "rename")
        self.checked(self.head())
        self.assertContentOk()
        head = [r for r in self.records()["records"] if r["source"] == "head"][-1]
        statuses = {(f["path"], f["status"]) for f in head["files"]}
        self.assertIn(("a.txt", "M"), statuses)
        self.assertIn(("d.txt", "A"), statuses)  # c.txt was added on the branch, so only d.txt is in base..tip
        sh(self.wt, "git", "mv", "b.txt", "e.txt")
        sh(self.wt, "git", "commit", "-q", "-m", "rename a base file")
        self.checked(self.head())
        self.assertContentOk()
        head = [r for r in self.records()["records"] if r["source"] == "head"][-1]
        statuses = {(f["path"], f["status"]) for f in head["files"]}
        self.assertIn(("b.txt", "D"), statuses)
        self.assertIn(("e.txt", "A"), statuses)
        self.assertFalse(any(s.startswith("R") for _p, s in statuses))

    def test_c6b_a_mode_change_is_caught(self):
        self.checked(self.f2)
        os.chmod(os.path.join(self.wt, "c.txt"), 0o755)
        sh(self.wt, "git", "commit", "-q", "-am", "mode")
        sh(self.wt, "git", "reset", "-q", "--soft", "HEAD~1")
        sh(self.wt, "git", "commit", "-q", "--amend", "--no-edit")  # same sequence of messages, other mode
        self.assertContentFails("c.txt")

    def test_c6c_binary_file(self):
        with open(os.path.join(self.wt, "bin.dat"), "wb") as f:
            f.write(b"\x00\x01\x02binary\xff")
        sh(self.wt, "git", "add", "bin.dat")
        sh(self.wt, "git", "commit", "-q", "-m", "binary")
        self.checked(self.head())
        self.on_main("b.txt", "b moved\n")
        self.rebase()
        self.assertContentOk()
        with open(os.path.join(self.wt, "bin.dat"), "wb") as f:
            f.write(b"\x00\x01\x02binarY\xff")
        sh(self.wt, "git", "commit", "-q", "--amend", "-a", "--no-edit")
        self.assertContentFails("bin.dat")

    def test_c6d_paths_with_spaces_newlines_and_quotes(self):
        for name in self.ODD:
            write(os.path.join(self.wt, name), name + "\n")
            sh(self.wt, "git", "add", "--", name)
        sh(self.wt, "git", "commit", "-q", "-m", "odd names")
        self.checked(self.head())
        self.assertContentOk()
        head = [r for r in self.records()["records"] if r["source"] == "head"][-1]
        self.assertTrue(set(self.ODD) <= {f["path"] for f in head["files"]}, head["files"])
        self.on_main("b.txt", "b moved\n")
        self.rebase()
        self.assertContentOk()
        write(os.path.join(self.wt, "new\nline.txt"), "changed\n")
        sh(self.wt, "git", "commit", "-q", "--amend", "-a", "--no-edit")
        line = self.assertContentFails(pa_hooks.clean("new\nline.txt"))
        self.assertNotIn("sp ace.txt", line)


class C7Transition(ContentBase):
    def test_c7_no_checked_line_in_flight_without_a_stamp(self):
        os.remove(os.path.join(self.dir, "content", "since.json"))
        self.assertContentOk("content not recorded")
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "content", "since.json")))  # written by this first run
        self.assertContentOk("content not recorded")  # the stamp is now, after this verifier's start
        content = self.gate_json("t-1")["content"][0]
        self.assertIsNone(content["checked"])
        self.assertIsNone(content["same"])
        self.assertTrue(content["change"])

    def test_c7b_no_checked_line_verifier_started_before_the_stamp(self):
        self.ledger_workers([{"address": "t33", "role": "verifier", "started": BEFORE}])
        self.assertContentOk("content not recorded")

    def test_c7c_no_checked_line_verifier_with_no_start(self):
        self.ledger_workers([{"address": "t33", "role": "verifier"}])
        self.assertContentOk("content not recorded")

    def test_c7d_no_checked_line_required_fails(self):
        self.assertContentFails("VERIFY.md has no Checked: line")
        self.ledger_workers([{"address": "t33", "role": "verifier", "started": BEFORE},
                             {"address": "t34", "role": "Check", "started": AFTER}])
        self.assertContentFails("VERIFY.md has no Checked: line")

    def test_c7e_short_id_and_decorated_lines_do_not_count(self):
        write(os.path.join(self.dir, "evidence", "t-1", "VERIFY.md"),
              "Checked: {}\n**Checked: {}**\n  Checked: {}\nChecked: `{}`\nVerdict: pass\n".format(
                  self.f2[:12], self.f2, self.f2, self.f2))
        self.assertContentFails("VERIFY.md has no Checked: line")

    def test_c7f_checked_not_a_commit(self):
        missing = "0123456789abcdef" * 2 + "01234567"
        self.checked(missing)
        self.assertContentFails("not a commit")

    def test_c7g_checked_not_a_commit_but_stored(self):
        self.checked(self.f2)
        self.assertContentOk()
        data = self.records()
        missing = "0123456789abcdef" * 2 + "01234567"
        verify = [r for r in data["records"] if r["source"] == "verify"][-1]
        data["records"].append(dict(verify, commit=missing))
        dump(data, os.path.join(self.dir, "content", "t-1.json"))
        self.checked(missing)
        self.assertContentOk()

    def test_c7h_computed_and_stored_differ_fails(self):  # the orchestrator's addition to point 4
        self.checked(self.f2)
        self.assertContentOk()
        data = self.records()
        mine = [r for r in data["records"] if r["source"] == "verify" and r["commit"] == self.f2]
        self.assertTrue(mine, data)
        self.on_main("b.txt", "b moved\n")
        self.rebase()
        self.assertContentOk("same content")  # computed now and stored agree
        for r in mine:
            r["change"] = "0" * 64
        dump(data, os.path.join(self.dir, "content", "t-1.json"))
        self.assertContentFails("differs from the record stored")

    def test_c7i_in_flight_with_a_checked_line_is_compared(self):
        self.ledger_workers([{"address": "t33", "role": "verifier", "started": BEFORE}])
        self.checked(self.f1)
        self.assertContentFails("c.txt", "1 commit checked, 2 now")

    def ledger_workers(self, extra):
        path = os.path.join(self.dir, "ledger.json")
        led = pa.read_json(path)
        led["tasks"][0]["workers"] = WORKERS + extra
        dump(led, path)


class C8Records(ContentBase):
    def test_c8_gate_json_keys_and_old_fields(self):
        self.checked(self.f2)
        self.assertContentOk()
        result = self.gate_json("t-1")
        self.assertEqual(list(result), ["task", "phase", "tier", "passed", "at", "checks", "commits", "content"])
        self.assertEqual(result["commits"][0]["commit"], self.f2)
        self.assertEqual(set(result["content"][0]), {"repo", "common", "onto", "base", "change", "sequence", "checked", "review", "same"})
        self.assertRegex(result["content"][0]["change"], "^[0-9a-f]{64}$")

    def test_c8b_records_are_stored_once(self):
        self.checked(self.f1)
        self.commit(self.wt, "d.txt", "d\n")
        self.run_gate("t-1")
        self.run_gate("t-1")
        data = self.records()
        self.assertEqual(data["version"], 1)
        keys = [(r["commit"], r["source"]) for r in data["records"]]
        self.assertEqual(sorted(keys), sorted([(self.f1, "verify"), (self.head(), "head")]))
        head = [r for r in data["records"] if r["source"] == "head"][0]
        self.assertEqual([f["path"] for f in head["files"]], ["a.txt", "c.txt", "d.txt"])
        self.assertEqual(len(head["commits"]), 3)
        for k in ("repo", "common", "commit", "tree", "onto", "base", "files", "commits", "change", "sequence", "at", "by", "source"):
            self.assertIn(k, head)
        self.assertEqual(head["by"], "pa-gate")

    def test_c8c_review_checked_is_recorded_never_failed(self):
        self.checked(self.f2)
        self.checked(self.f1, name="REVIEW.md")
        self.assertContentOk()
        self.assertEqual(self.gate_json("t-1")["content"][0]["review"], self.f1)

    def test_c8d_stamp_written_once(self):
        os.remove(os.path.join(self.dir, "content", "since.json"))
        self.run_gate("t-1", "--phase", "build")
        first = pa.read_json(os.path.join(self.dir, "content", "since.json"))
        self.assertRegex(first["at"], r"^\d{4}-\d\d-\d\dT")
        self.stamp(SINCE)
        self.run_gate("t-1")
        self.assertEqual(pa.read_json(os.path.join(self.dir, "content", "since.json"))["at"], SINCE)

    def test_c8e_other_phases_carry_no_content_check(self):
        code, out, _ = self.run_gate("t-1", "--phase", "build")
        self.assertNotIn(CHECK, out)
        self.assertEqual(self.gate_json("t-1")["content"], [])

    def test_c8f_evaluate_gate_writes_nothing(self):
        self.checked(self.f1)
        before = {os.path.join(f, n): os.stat(os.path.join(f, n)).st_mtime_ns for f, _d, ns in os.walk(pa.ROOT) for n in ns}
        result = pa_hooks.evaluate_gate(self.dir, self.task, "review")
        after = {os.path.join(f, n): os.stat(os.path.join(f, n)).st_mtime_ns for f, _d, ns in os.walk(pa.ROOT) for n in ns}
        self.assertEqual(before, after)
        self.assertIn(CHECK, [c["check"] for c in result["checks"]])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "content", "t-1.json")))


class C9Packages(Base):
    def test_c9_one_checked_line_per_package(self):
        wt = os.path.join(self.tmp, "ws")
        heads = {}
        for name in ("A", "B"):
            path = os.path.join(wt, "src", name)
            os.makedirs(path)
            sh(path, "git", "init", "-q", "-b", "main")
            self.commit(path, "base.txt", "base\n")
            sh(path, "git", "checkout", "-q", "-b", "t-1-x")
            heads[name] = self.commit(path, name.lower() + ".txt", name + "\n")
        self.add_task("t-1", worktree=wt, type="feature", workers=WORKERS + [{"address": "t33", "role": "verifier", "started": AFTER}])
        self.evidence("t-1")
        dump({"version": 1, "at": SINCE}, os.path.join(self.dir, "content", "since.json"))
        write(os.path.join(self.dir, "evidence", "t-1", "VERIFY.md"), "Checked: {}\nChecked: {}\nVerdict: pass\n".format(heads["A"], heads["B"]))
        code, out, err = self.run_gate("t-1")
        self.assertEqual(code, 0, out + err)
        self.assertEqual([c["checked"] for c in self.gate_json("t-1")["content"]], [heads["A"], heads["B"]])
        self.commit(os.path.join(wt, "src", "B"), "b.txt", "B changed\n")
        code, out, err = self.run_gate("t-1")
        self.assertEqual(code, 1, out)
        line = [l for l in out.splitlines() if CHECK in l][0]
        self.assertIn("B: ", line)
        self.assertIn("b.txt", line)
        self.assertNotIn("A: ", line)


class C10Refusals(ContentBase):
    def test_c10_a_merge_commit(self):
        sh(self.wt, "git", "checkout", "-q", "-b", "side", self.f1)
        self.commit(self.wt, "s.txt", "s\n")
        sh(self.wt, "git", "checkout", "-q", "t-1-x")
        sh(self.wt, "git", "merge", "-q", "--no-ff", "-m", "merge side", "side")
        self.checked(self.f2)
        self.assertContentFails("merge commit")

    def test_c10b_no_base_branch(self):
        self.checked(self.f1)
        sh(self.proj, "git", "branch", "-m", "main", "elsewhere")
        self.assertContentFails("no base branch")

    def test_c10c_profile_main_branch(self):
        dump({"name": "test", "claude": ["true"], "codex": ["true"], "mainBranches": ["elsewhere"]},
             os.path.join(pa.ROOT, "profiles", "test", "profile.json"))
        self.checked(self.f2)
        sh(self.proj, "git", "branch", "-m", "main", "elsewhere")
        self.on_main("b.txt", "b moved\n")
        self.rebase_onto("elsewhere")
        self.assertContentOk("same content")
        self.assertEqual(self.gate_json("t-1")["content"][0]["onto"], "elsewhere")

    def rebase_onto(self, branch):
        sh(self.wt, "git", "rebase", "-q", branch)


class C11Position(ContentBase):
    """t-052 J1 round 2 (review M1, L1; verify MF-2): each record is computed against the base it was made on, not
    today's onto. Local main at or past the tip (a hand merge, a push card's ff) neither passes unseen commits nor
    fails a clean carry-over."""

    def ff_main(self, sha=None):
        sh(self.proj, "git", "merge", "-q", "--ff-only", sha or self.head())

    def test_c11_main_at_head_with_an_unreviewed_commit_no_records(self):  # review probe a, A2
        self.checked(self.f2)
        self.commit(self.wt, "evil.txt", "unreviewed\n")
        self.ff_main()
        line = self.assertContentFails("already on main")
        self.assertNotIn("same content", line)
        self.assertIs(self.gate_json("t-1")["content"][0]["same"], None)

    def test_c11b_main_at_head_with_an_unreviewed_commit_after_records(self):
        self.checked(self.f2)
        self.assertContentOk()  # X == HEAD: X's record (and HEAD's) stored with the old base
        self.commit(self.wt, "evil.txt", "unreviewed\n")
        self.assertContentFails("evil.txt")
        self.ff_main()
        line = self.assertContentFails("evil.txt")
        self.assertNotIn("same content", line)

    def test_c11c_main_fast_forwarded_to_a_cleanly_rebased_tip(self):  # verify MF-2, review probe b B2
        self.checked(self.f2)
        self.assertContentOk()
        first = self.gate_json("t-1")["content"][0]
        self.on_main("b.txt", "b moved\n")
        tip = self.rebase()
        self.assertContentOk("same content as the checked " + self.f2[:7])
        self.ff_main(tip)
        self.assertContentOk("same content as the checked " + self.f2[:7])
        now = self.gate_json("t-1")["content"][0]
        self.assertEqual((now["change"], now["sequence"]), (first["change"], first["sequence"]))
        self.assertIs(now["same"], True)
        self.assertNotEqual(now["base"], tip)

    def test_c11d_main_at_a_rebased_tip_whose_base_was_never_stored(self):  # review probe b B3
        self.checked(self.f2)
        self.assertContentOk()
        self.on_main("b.txt", "b moved\n")
        self.ff_main(self.rebase())  # no gate run between the rebase and the ff: HEAD's base is not known
        self.assertContentFails("already on main")

    def test_c11e_checked_commit_already_on_main_no_record(self):
        self.checked(self.f1)
        self.ff_main(self.f1)  # X is on main, HEAD (f2) one commit past it; nothing stored
        line = self.assertContentFails("already on main")
        self.assertIn(self.f1[:7], line)

    def test_c11f_checked_commit_on_main_with_a_stored_record(self):
        self.checked(self.f1)
        self.assertContentFails("c.txt")  # stores X's record against the old base
        self.ff_main(self.f1)
        self.assertContentFails("c.txt")  # X's change stays f1's, so HEAD still adds c.txt

    def test_c11g_a_stored_base_that_is_not_an_ancestor_fails(self):
        self.checked(self.f1)
        self.commit(self.wt, "d.txt", "d\n")
        self.assertContentFails()
        data = self.records()
        self.on_main("m.txt", "m\n")
        other = sh(self.proj, "git", "rev-parse", "HEAD")
        for r in data["records"]:
            if r["commit"] == self.f1:
                r["base"] = other
        dump(data, os.path.join(self.dir, "content", "t-1.json"))
        self.assertContentFails("not an ancestor")


class C12SteeringRefs(ContentBase):
    """t-052 J1 round 2 (verify MF-1, review L2): refs a repository can set do not change the answer."""

    def test_c12_a_replace_ref_does_not_make_unchecked_content_look_checked(self):  # verify s11
        self.checked(self.f2)
        unchecked = self.commit(self.wt, "d.txt", "unchecked\n")
        sh(self.wt, "git", "replace", unchecked, self.f2)
        line = self.assertContentFails("d.txt", "2 commits checked, 3 now")
        self.assertNotIn("same content", line)
        result = pa_hooks.evaluate_gate(self.dir, self.task, "review")
        check = [c for c in result["checks"] if c["check"] == CHECK][0]
        self.assertFalse(check["ok"], check)

    def test_c12c_origin_head_does_not_pick_the_base_branch(self):
        self.checked(self.f1)
        unchecked = self.head()
        sh(self.proj, "git", "branch", "steer", unchecked)
        sh(self.proj, "git", "update-ref", "refs/remotes/origin/steer", unchecked)
        sh(self.proj, "git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/steer")
        line = self.assertContentFails("c.txt")
        self.assertNotIn("same content", line)
        self.assertEqual(self.gate_json("t-1")["content"][0]["onto"], "main")



class C13SteeringFiles(ContentBase):
    """t-052 J1 round 3 (verify MF-R2-1): files a repository can write under its git directory (info/grafts, shallow,
    the commit-graph) do not change what the content check says a commit's parents are."""

    def sibling_base(self):
        """Verify n13: E, a root commit with main's tree plus an unreviewed evil.txt (not a child of main's M); the
        branch reset to E + f1 + f2. Returns (M, E, the common git directory)."""
        m = sh(self.proj, "git", "rev-parse", "main")
        write(os.path.join(self.proj, "evil.txt"), "evil\n")
        sh(self.proj, "git", "add", "evil.txt")
        tree = sh(self.proj, "git", "write-tree")
        sh(self.proj, "git", "rm", "-q", "--cached", "evil.txt")
        os.remove(os.path.join(self.proj, "evil.txt"))
        e = sh(self.proj, "git", "commit-tree", tree, "-m", "base")
        sh(self.wt, "git", "reset", "-q", "--hard", e)
        sh(self.wt, "git", "cherry-pick", self.f1)
        sh(self.wt, "git", "cherry-pick", self.f2)
        common = sh(self.wt, "git", "rev-parse", "--path-format=absolute", "--git-common-dir")
        return m, e, common

    def assertNotSame(self, *why):
        line = self.assertContentFails(*why)
        self.assertNotIn("same content", line)
        result = pa_hooks.evaluate_gate(self.dir, self.task, "review")
        check = [c for c in result["checks"] if c["check"] == CHECK][0]
        self.assertFalse(check["ok"], check)
        return line

    def test_c13_a_graft_does_not_make_unchecked_content_look_checked(self):  # verify n13
        self.checked(self.f2)
        self.assertContentOk()
        m, e, common = self.sibling_base()
        self.assertNotSame("no merge base")
        write(os.path.join(common, "info", "grafts"), "{} {}\n".format(m, e))
        self.assertEqual(sh(self.wt, "git", "merge-base", "HEAD", "main"), e)  # plain git honours the graft
        self.assertEqual(sh(self.wt, "git", "--no-replace-objects", "merge-base", "HEAD", "main"), e)
        self.assertNotSame("grafts")

    def test_c13b_a_graft_file_named_by_git_graft_file(self):
        self.checked(self.f2)
        m, e, _common = self.sibling_base()
        graft = os.path.join(self.tmp, "elsewhere-grafts")
        write(graft, "{} {}\n".format(m, e))
        saved = os.environ.get("GIT_GRAFT_FILE")
        os.environ["GIT_GRAFT_FILE"] = graft
        try:
            self.assertEqual(sh(self.wt, "git", "merge-base", "HEAD", "main"), e)
            self.assertNotSame("grafts")
        finally:
            if saved is None:
                os.environ.pop("GIT_GRAFT_FILE", None)
            else:
                os.environ["GIT_GRAFT_FILE"] = saved

    def test_c13c_a_shallow_file_fails(self):
        self.checked(self.f1)
        self.assertContentFails("c.txt")
        common = sh(self.wt, "git", "rev-parse", "--path-format=absolute", "--git-common-dir")
        write(os.path.join(common, "shallow"), self.f1 + "\n")  # f1 reads as a root (this fails safe; the reason must say shallow)
        self.assertNotSame("shallow")

    def test_c13d_a_forged_commit_graph_does_not_steer_parents(self):
        self.checked(self.f2)
        self.assertContentOk()
        m, e, _common = self.sibling_base()
        forge_parent(self.wt, m, e)
        self.assertEqual(sh(self.wt, "git", "rev-parse", m + "^"), e)  # plain git believes the graph
        self.assertEqual(sh(self.wt, "git", "--no-replace-objects", "merge-base", "HEAD", "main"), e)
        line = self.assertNotSame("no merge base")
        self.assertNotIn("grafts", line)

    def test_c13e_an_empty_graft_file_still_fails(self):  # existence, not content: no parsing of git's format
        self.checked(self.f2)
        common = sh(self.wt, "git", "rev-parse", "--path-format=absolute", "--git-common-dir")
        write(os.path.join(common, "info", "grafts"), "")
        self.assertNotSame("grafts")
        os.remove(os.path.join(common, "info", "grafts"))
        self.assertContentOk("the checked commit is HEAD")

class S1FullSuiteRecord(Base):
    def test_s1_each_run_is_recorded(self):
        proj = self.repo()
        wt = self.linked(proj, "t-1-x", os.path.join(self.tmp, "wt"))
        head = self.commit(wt, "a.txt", "a\n")
        self.add_task("t-1", worktree=wt)
        write(os.path.join(wt, ".claude", "pa-task.json"), json.dumps({"task": "t-1", "space": "sp1"}))
        agent = pa.read_json(os.path.join(self.dir, "agent.json"))
        saved_cwd = os.getcwd()
        os.chdir(wt)
        try:
            for command, exit_code in (("true", 0), ("exit 3", 3)):
                agent["commands"] = {"full": command}
                dump(agent, os.path.join(self.dir, "agent.json"))
                out, err = io.StringIO(), io.StringIO()
                saved, sys.argv = sys.argv, ["pa-full-suite"]
                os.environ["HC_TERMINAL_ID"] = "t77"
                try:
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        with self.assertRaises(SystemExit) as e:
                            pa_hooks.main()
                finally:
                    sys.argv = saved
                    os.environ.pop("HC_TERMINAL_ID", None)
                self.assertEqual(e.exception.code, exit_code)
        finally:
            os.chdir(saved_cwd)
        with open(os.path.join(self.dir, "content", "suites.jsonl")) as f:
            lines = [json.loads(l) for l in f if l.strip()]
        self.assertEqual([l["exit"] for l in lines], [0, 3])
        tree = sh(wt, "git", "rev-parse", "HEAD^{tree}")
        for l in lines:
            self.assertEqual((l["task"], l["worker"], l["repo"], l["commit"], l["tree"], l["uncommitted"]),
                             ("t-1", "t77", os.path.realpath(wt), head, tree, 0))
            for k in ("at", "seconds", "command"):
                self.assertIn(k, l)
        self.assertIn("full suite: `exit 3`", pa.read_text(os.path.join(self.dir, "evidence", "t-1", "build.log")))


# ---- t-192 R2: a task with jobs in two repositories gates each job on its own check files --------------------------

class T192JobGate(Base):
    """t-1: its main worktree in repo proj, job j2's in repo other (a second repository); the task's evidence has
    VERIFY.md and REVIEW.md for the main job and VERIFY-j2.md, REVIEW-j2.md for j2."""

    def setUp(self):
        super().setUp()
        self.wt = self.linked(self.repo("proj"), "t-1-x", os.path.join(self.tmp, "wt"))
        self.f1 = self.commit(self.wt, "a.txt", "a\n")
        self.wt2 = self.linked(self.repo("other"), "t-1-j2", os.path.join(self.tmp, "wt2"))
        self.j2 = self.commit(self.wt2, "j.txt", "j\n")
        self.add_task("t-1", worktree=self.wt, type="feature",
                      worktrees=[{"path": self.wt, "job": "main"}, {"path": self.wt2, "job": "j2"}])
        self.evidence("t-1")
        self.evidence("t-1", ["VERIFY-j2.md", "REVIEW-j2.md"])

    def ev(self, name):
        return os.path.join(self.dir, "evidence", "t-1", name)

    def test_vp3_the_job_gate_reads_its_files_and_pins_its_worktree(self):
        code, out, err = self.run_gate("t-1", "--job", "j2")
        self.assertEqual(code, 0, out + err)
        self.assertIn("ok    VERIFY-j2.md", out)
        self.assertIn("ok    REVIEW-j2.md", out)
        self.assertNotIn("VERIFY.md", out)
        self.assertNotIn("REVIEW.md", out)
        self.assertEqual(out.splitlines()[-1], "pa-gate t-1 job j2 (review phase, normal tier): PASSED")
        self.assertFalse(os.path.exists(self.gate_file("t-1")), "GATE.json is the main job's")
        with open(self.ev("GATE-j2.json")) as f:
            result = json.load(f)
        self.assertEqual(result["job"], "j2")
        self.assertEqual([(c["repo"], c["commit"]) for c in result["commits"]], [(self.wt2, self.j2)])
        self.assertTrue(pa_hooks.tool_file("GATE-j2.json"))

    def test_vp3_with_no_job_the_gate_is_todays(self):
        expected = None
        for extra in ((), ("--job", "main")):
            with self.subTest(extra=extra):
                code, out, err = self.run_gate("t-1", *extra)
                self.assertEqual(code, 0, out + err)
                self.assertIn("ok    VERIFY.md", out)
                self.assertNotIn("j2", out)
                self.assertEqual(out.splitlines()[-1], "pa-gate t-1 (review phase, normal tier): PASSED")
                result = self.gate_json("t-1")
                self.assertNotIn("job", result)
                self.assertEqual(list(result), ["task", "phase", "tier", "passed", "at", "checks", "commits", "content"])
                self.assertEqual([c["repo"] for c in result["commits"]], [self.wt])
                self.assertFalse(os.path.exists(self.ev("GATE-main.json")))
                expected = expected or out
                self.assertEqual(out, expected, "--job main is byte for byte no --job")

    def test_h2_5_a_missing_job_file_never_falls_back_to_the_task_file(self):
        os.remove(self.ev("VERIFY-j2.md"))
        code, out, _ = self.run_gate("t-1", "--job", "j2")
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL  VERIFY-j2.md  (missing)", out)
        self.assertTrue(os.path.isfile(self.ev("VERIFY.md")))

    def test_h2_6_a_job_file_linked_out_of_the_evidence_folder(self):
        outside = os.path.join(self.tmp, "elsewhere.md")
        write(outside, "text\n")
        os.remove(self.ev("REVIEW-j2.md"))
        os.symlink(outside, self.ev("REVIEW-j2.md"))
        code, out, _ = self.run_gate("t-1", "--job", "j2")
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL  REVIEW-j2.md  (outside the evidence folder)", out)

    def refused(self, *argv):
        before = sorted(os.listdir(os.path.join(self.dir, "evidence", "t-1")))
        with self.assertRaises(pa.Problem) as cm:
            self.run_gate("t-1", *argv)
        self.assertEqual(sorted(os.listdir(os.path.join(self.dir, "evidence", "t-1"))), before, "nothing is written")
        return str(cm.exception)

    def test_h2_1_h2_2_words_that_are_not_a_recorded_job(self):
        for word in ("", "-x", "../x", "a/b", "/abs", "x.md", "MAIN", "Main", "J2", "j2 ", "j2\n", "j3", "j2/../j2"):
            with self.subTest(word=word):
                self.assertIn("is not a recorded job of t-1", self.refused("--job=" + word))

    def test_h2_3_a_job_recorded_twice_is_ambiguous(self):
        led = pa.read_json(os.path.join(self.dir, "ledger.json"))
        wt3 = self.linked(os.path.join(self.tmp, "other"), "t-1-j2b", os.path.join(self.tmp, "wt3"))
        led["tasks"][0]["worktrees"].append({"path": wt3, "job": "j2"})
        dump(led, os.path.join(self.dir, "ledger.json"))
        why = self.refused("--job", "j2")
        self.assertIn("recorded 2 times", why)
        self.assertIn(self.wt2, why)
        self.assertIn(wt3, why)

    def test_h2_4_a_removed_job(self):
        led = pa.read_json(os.path.join(self.dir, "ledger.json"))
        led["tasks"][0]["worktrees"][1]["removed"] = "2026-11-20T10:00:00Z"
        dump(led, os.path.join(self.dir, "ledger.json"))
        self.assertIn("job j2 of t-1 was removed at 2026-11-20T10:00:00Z", self.refused("--job", "j2"))

    def test_vp3_the_content_check_reads_the_jobs_checked_line(self):
        dump({"version": 1, "at": SINCE}, os.path.join(self.dir, "content", "since.json"))
        led = pa.read_json(os.path.join(self.dir, "ledger.json"))
        led["tasks"][0]["workers"] = WORKERS + [{"address": "t33", "role": "verifier", "started": AFTER}]
        dump(led, os.path.join(self.dir, "ledger.json"))
        write(self.ev("VERIFY.md"), "text\nChecked: {}\nVerdict: pass\n".format(self.f1))
        write(self.ev("VERIFY-j2.md"), "text\nChecked: {}\nVerdict: pass\n".format(self.j2))
        code, out, err = self.run_gate("t-1", "--job", "j2")
        self.assertEqual(code, 0, out + err)
        self.assertIn("ok    the checks saw this content  (the checked commit is HEAD)", out)
        write(self.ev("VERIFY-j2.md"), "text\nChecked: {}\nVerdict: pass\n".format(self.f1))  # the main job's commit
        code, out, _ = self.run_gate("t-1", "--job", "j2")
        self.assertEqual(code, 1, out)
        self.assertIn("VERIFY-j2.md's Checked commit {} is not a commit in this repository".format(self.f1[:7]), out)
        code, out, err = self.run_gate("t-1")  # the main job reads VERIFY.md's line, untouched
        self.assertEqual(code, 0, out + err)
        self.assertIn("(the checked commit is HEAD)", out)

    def test_a_job_on_the_task_worktree_is_the_main_job(self):  # DECISIONS D-R2-main
        led = pa.read_json(os.path.join(self.dir, "ledger.json"))
        led["tasks"][0]["worktrees"][0]["job"] = "build"
        dump(led, os.path.join(self.dir, "ledger.json"))
        code, out, err = self.run_gate("t-1", "--job", "build")
        self.assertEqual(code, 0, out + err)
        self.assertIn("ok    VERIFY.md", out)
        self.assertTrue(os.path.isfile(self.gate_file("t-1")))
        self.assertEqual(pa_hooks.check_files(pa_hooks.files_job(led["tasks"][0], "build")), ("VERIFY.md", "REVIEW.md", "GATE.json"))


if __name__ == "__main__":
    unittest.main()
