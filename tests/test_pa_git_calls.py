"""Unit tests for fewer git calls in the gate (t-094 J1: R1, R2, R4, R9, R10).

Run: python3 -m unittest -v tests/test_pa_git_calls.py
No hc calls and no real homes: HOME, HERDR_PA_HOME, CLAUDE_CONFIG_DIR and HERDR_APP_HOME point at a temp folder,
and git runs only on repositories made under that temp folder, with no global or system git config. Nothing here
pushes, and no repository has a reachable remote.

The pin's answers are compared with OLD_GATE_COMMITS, a copy of gate_commits as it was before t-094 (main 2a71d7e),
run on the same repository: R1 must give the same pin in fewer calls.
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(HERE, "..", "runtime", "tools")
TMP = os.path.realpath(tempfile.mkdtemp(prefix="t094-calls-"))

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


def OLD_GATE_COMMITS(worktree, runner=None):
    """gate_commits at main 2a71d7e (before t-094), the oracle for R1."""
    git = pa_hooks.git if runner is None else runner
    if not worktree or not os.path.isdir(worktree):
        return []
    root = os.path.realpath(worktree)

    def entry(path):
        code, top, _ = git(path, "rev-parse", "--show-toplevel")
        if code or not top or os.path.realpath(top) != path:
            return None
        code, common, _ = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if code or not common:
            code, common, _ = git(path, "rev-parse", "--git-common-dir")
            common = os.path.join(path, common) if not code and common else None
        _c, branch, _ = git(path, "symbolic-ref", "--short", "-q", "HEAD")
        code, commit, _ = git(path, "rev-parse", "--verify", "-q", "HEAD")
        _c, status, _ = git(path, "--no-optional-locks", "status", "--porcelain", "--untracked-files=no")
        changed = [l for l in status.splitlines() if l.strip() and not l.split(None, 1)[-1].strip('"').startswith(".claude/")]
        return {"repo": path, "common": os.path.realpath(common) if common else None, "branch": branch or None,
                "commit": commit if not code and commit else None, "uncommitted": len(changed)}

    one = entry(root)
    if one:
        return [one]
    src = os.path.join(root, "src")
    if not os.path.isdir(src):
        return []
    found = []
    for name in sorted(os.listdir(src)):
        path = os.path.join(src, name)
        if os.path.isdir(path) and not os.path.islink(path):
            e = entry(path)
            if e:
                found.append(e)
    return found


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def sh(cwd, *argv, ok=(0,)):
    r = subprocess.run(list(argv), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    if r.returncode not in ok:
        raise AssertionError("{} failed in {}: {}".format(" ".join(argv), cwd, r.stderr))
    return r.stdout.strip()


class Counting:
    """A runner with git()'s signature that records every argv it runs (after the path)."""

    def __init__(self, inner=None, before=None):
        self.inner = inner or pa_hooks.git
        self.before = before
        self.calls = []

    def __call__(self, path, *args, **kw):
        self.calls.append(list(args))
        if self.before:
            self.before(path, list(args))
        return self.inner(path, *args, **kw)

    def having(self, word):
        return [c for c in self.calls if word in c]


def gate_git_like(path, *args, timeout=None):
    """pa_push.Session.gate_git's argv rule (--ignore-submodules=all after status), on pa_hooks.git."""
    args = list(args)
    if "status" in args:
        args.insert(args.index("status") + 1, "--ignore-submodules=all")
    return pa_hooks.git(path, *args, timeout=timeout)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(dir=TMP))

    def repo(self, name="proj"):
        path = os.path.join(self.tmp, name)
        os.makedirs(path)
        sh(path, "git", "init", "-q", "-b", "main")
        write(os.path.join(path, "README"), "base\n")
        sh(path, "git", "add", "README")
        sh(path, "git", "commit", "-q", "-m", "base")
        return path

    def commit(self, path, file, text, message=None):
        write(os.path.join(path, file), text)
        sh(path, "git", "add", file)
        sh(path, "git", "commit", "-q", "-m", message or ("change " + file))
        return sh(path, "git", "rev-parse", "HEAD")

    def common(self, path):
        return sh(path, "git", "rev-parse", "--path-format=absolute", "--git-common-dir")

    def assertSamePin(self, folder, runner=None):
        old = OLD_GATE_COMMITS(folder, runner)
        new = pa_hooks.gate_commits(folder, runner)
        self.assertEqual(new, old)
        return new


# ---- R1: the pin -------------------------------------------------------------------------------------------------

class R1PinCalls(Base):
    def test_ac1_1_one_pin_on_a_plain_worktree_is_two_git_calls(self):
        path = self.repo()
        runner = Counting()
        pins = pa_hooks.gate_commits(path, runner)
        self.assertEqual(len(pins), 1)
        self.assertEqual(len(runner.calls), 2, runner.calls)

    def test_ac1_1_the_status_call_keeps_no_optional_locks_first(self):
        path = self.repo()
        runner = Counting()
        pa_hooks.gate_commits(path, runner)
        status = runner.having("status")
        self.assertEqual(len(status), 1, runner.calls)
        self.assertEqual(status[0][0], "--no-optional-locks")

    def test_ac1_1_a_linked_worktree_is_two_calls_too(self):
        path = self.repo()
        wt = os.path.join(self.tmp, "wt")
        sh(path, "git", "worktree", "add", "-q", "-b", "t-1", wt, "main")
        runner = Counting()
        pins = self.assertSamePin(wt)
        self.assertEqual(pins[0]["branch"], "t-1")
        self.assertEqual(pins[0]["common"], os.path.realpath(self.common(path)))
        pa_hooks.gate_commits(wt, runner)
        self.assertEqual(len(runner.calls), 2, runner.calls)


class R1PinAnswers(Base):
    """The pin is today's for every status line kind and HEAD state (R1, R5)."""

    def test_plain_clean_and_dirty(self):
        path = self.repo()
        self.assertEqual(self.assertSamePin(path)[0]["uncommitted"], 0)
        write(os.path.join(path, "README"), "changed\n")
        self.assertEqual(self.assertSamePin(path)[0]["uncommitted"], 1)
        write(os.path.join(path, "new"), "x\n")
        sh(path, "git", "add", "new")
        self.assertEqual(self.assertSamePin(path)[0]["uncommitted"], 2)

    def test_claude_files_are_not_counted(self):
        path = self.repo()
        self.commit(path, ".claude/settings.json", "{}\n")
        write(os.path.join(path, ".claude", "settings.json"), '{"a": 1}\n')
        self.assertEqual(self.assertSamePin(path)[0]["uncommitted"], 0)

    def test_rename_out_of_claude_counts_as_today(self):
        path = self.repo()
        self.commit(path, ".claude/notes.md", "notes\n")
        sh(path, "git", "mv", ".claude/notes.md", "notes.md")
        self.assertIn("R  .claude/notes.md -> notes.md", sh(path, "git", "status", "--porcelain", "-uno"))
        self.assertSamePin(path)

    def test_rename_into_claude_counts_as_today(self):
        path = self.repo()
        self.commit(path, "notes.md", "notes\n")
        os.makedirs(os.path.join(path, ".claude"))
        sh(path, "git", "mv", "notes.md", ".claude/notes.md")
        self.assertSamePin(path)

    def test_quoted_paths_count_as_today(self):
        path = self.repo()
        self.commit(path, ".claude/ä b.md", "x\n")
        self.commit(path, "q \"x\".md", "x\n")
        write(os.path.join(path, ".claude", "ä b.md"), "y\n")
        write(os.path.join(path, "q \"x\".md"), "y\n")
        self.assertEqual(self.assertSamePin(path)[0]["uncommitted"], 1)
        sh(path, "git", "mv", ".claude/ä b.md", "ä c.md")
        self.assertSamePin(path)

    def test_unmerged_path_counts_as_today(self):
        path = self.repo()
        sh(path, "git", "checkout", "-q", "-b", "side")
        self.commit(path, "README", "side\n")
        sh(path, "git", "checkout", "-q", "main")
        self.commit(path, "README", "main\n")
        sh(path, "git", "merge", "-q", "side", ok=(0, 1))
        self.assertIn("UU README", sh(path, "git", "status", "--porcelain", "-uno"))
        self.assertEqual(self.assertSamePin(path)[0]["uncommitted"], 1)

    def test_detached_head(self):
        path = self.repo()
        sh(path, "git", "checkout", "-q", "--detach")
        pin = self.assertSamePin(path)[0]
        self.assertIsNone(pin["branch"])
        self.assertTrue(pin["commit"])

    def test_unborn_head(self):
        path = os.path.join(self.tmp, "empty")
        os.makedirs(path)
        sh(path, "git", "init", "-q", "-b", "main")
        pin = self.assertSamePin(path)[0]
        self.assertEqual(pin["branch"], "main")
        self.assertIsNone(pin["commit"])

    def test_not_a_repository_and_a_subfolder(self):
        self.assertEqual(self.assertSamePin(self.tmp), [])
        path = self.repo()
        os.makedirs(os.path.join(path, "sub"))
        self.assertEqual(self.assertSamePin(os.path.join(path, "sub")), [])

    def test_a_folder_of_packages(self):
        os.makedirs(os.path.join(self.tmp, "ws", "src"))
        for name in ("B", "A"):
            path = os.path.join(self.tmp, "ws", "src", name)
            os.makedirs(path)
            sh(path, "git", "init", "-q", "-b", "main")
            self.commit(path, "f", name + "\n")
        os.makedirs(os.path.join(self.tmp, "ws", "src", "plain"))
        pins = self.assertSamePin(os.path.join(self.tmp, "ws"))
        self.assertEqual([os.path.basename(p["repo"]) for p in pins], ["A", "B"])

    def test_the_push_gate_runner(self):
        path = self.repo()
        write(os.path.join(path, "README"), "changed\n")
        self.assertSamePin(path, gate_git_like)


class R1AmbiguousNames(Base):
    """J1-D1 (option A, t1dr): the branch is symbolic-ref --short's, also where status v2's name differs."""

    def test_a_branch_named_like_a_tag(self):
        path = self.repo()
        sh(path, "git", "tag", "x")
        sh(path, "git", "checkout", "-q", "-b", "x")
        self.assertEqual(self.assertSamePin(path)[0]["branch"], "heads/x")

    def test_a_branch_named_stash_with_a_stash(self):
        path = self.repo()
        sh(path, "git", "checkout", "-q", "-b", "stash")
        write(os.path.join(path, "README"), "changed\n")
        sh(path, "git", "stash", "-q")
        self.assertEqual(self.assertSamePin(path)[0]["branch"], "heads/stash")

    def test_head_on_a_remote_tracking_ref(self):
        path = self.repo()
        sh(path, "git", "update-ref", "refs/remotes/origin/foo", "HEAD")
        sh(path, "git", "symbolic-ref", "HEAD", "refs/remotes/origin/foo")
        pin = self.assertSamePin(path)[0]
        self.assertEqual(pin["branch"], "origin/foo")
        self.assertTrue(pin["commit"])

    def test_head_on_a_missing_non_branch_ref(self):
        path = self.repo()
        sh(path, "git", "symbolic-ref", "HEAD", "refs/remotes/origin/gone")
        self.assertEqual(self.assertSamePin(path)[0]["branch"], "origin/gone")


# ---- R2, R9, R10: the content path's steering check -----------------------------------------------------------------

class R2Paths(Base):
    def test_one_paths_call_per_folder_per_run(self):
        path = self.repo()
        tip = self.commit(path, "a", "a\n")
        runner = Counting()
        run = pa_hooks.content_git(runner)
        rec = pa_hooks.content_record(run, path, tip, "main", sh(path, "git", "rev-parse", "HEAD^"))
        self.assertNotIn("error", rec)
        self.assertEqual(rec["common"], os.path.realpath(self.common(path)))
        asked = [c for c in runner.calls if "--git-path" in c or "--git-common-dir" in c]
        self.assertEqual(len(asked), 1, asked)
        self.assertEqual(asked[0][-6:], ["--path-format=absolute", "--git-common-dir", "--git-path", "info/grafts",
                                         "--git-path", "shallow"])
        self.assertEqual(asked[0][:len(pa_hooks.CONTENT_GIT)], list(pa_hooks.CONTENT_GIT))

    def test_a_relative_answer_cannot_tell_where(self):
        path = self.repo()

        def relative(p, *args, **kw):
            if "--git-path" in args:
                return 0, ".git\n.git/info/grafts\n.git/shallow", ""
            return pa_hooks.git(p, *args, **kw)
        run = pa_hooks.content_git(relative)
        with self.assertRaises(pa_hooks.ContentError) as e:
            run(path, "cat-file", "-e", "HEAD")
        self.assertIn("cannot tell where", str(e.exception))

    def test_union_only_reads_the_same_paths_answer(self):
        path = self.repo()
        tip = self.commit(path, "a", "a\n")
        runner = Counting()
        run = pa_hooks.content_git(runner)
        rec = pa_hooks.content_record(run, path, tip, "main", sh(path, "git", "rev-parse", "HEAD^"))
        other = dict(rec, files=[dict(f, baseBlob="0" * 40) for f in rec["files"]])
        pa_hooks.union_only(run, pa_hooks.git, path, rec, other)
        asked = [c for c in runner.calls if "--git-path" in c or "--git-common-dir" in c]
        self.assertEqual(len(asked), 1, asked)


class R9Shallow(Base):
    def test_ac9_1_the_shallow_refusal_names_its_fix(self):
        path = self.repo()
        write(os.path.join(self.common(path), "shallow"), sh(path, "git", "rev-parse", "HEAD") + "\n")
        run = pa_hooks.content_git(pa_hooks.git)
        with self.assertRaises(pa_hooks.ContentError) as e:
            run(path, "cat-file", "-e", "HEAD")
        why = str(e.exception)
        self.assertIn("a shallow file", why)
        self.assertIn("the content cannot be compared (fix: git fetch --unshallow in {})".format(path), why)

    def test_the_grafts_refusal_is_unchanged(self):
        path = self.repo()
        grafts = os.path.join(self.common(path), "info", "grafts")
        write(grafts, "")
        run = pa_hooks.content_git(pa_hooks.git)
        with self.assertRaises(pa_hooks.ContentError) as e:
            run(path, "cat-file", "-e", "HEAD")
        self.assertEqual(str(e.exception), "the repository has a grafts file ({}), which changes what git says a "
                                           "commit's parents are; the content cannot be compared".format(grafts))


class R10After(Base):
    def made_during(self, path, name, word):
        target = os.path.join(self.common(path), name)

        def before(_p, args):
            if word in args:
                write(target, "")
        return target, Counting(before=before)

    def test_ac10_1_grafts_made_during_the_call_refuse_it(self):
        path = self.repo()
        target, runner = self.made_during(path, "info/grafts", "cat-file")
        run = pa_hooks.content_git(runner)
        with self.assertRaises(pa_hooks.ContentError) as e:
            run(path, "cat-file", "-e", sh(path, "git", "rev-parse", "HEAD") + "^{commit}")
        self.assertIn("a grafts file ({})".format(target), str(e.exception))
        self.assertEqual(len(runner.having("cat-file")), 1, "the call ran; its answer was discarded")

    def test_ac10_2_a_shallow_file_made_during_the_call_refuses_it(self):
        path = self.repo()
        target, runner = self.made_during(path, "shallow", "cat-file")
        run = pa_hooks.content_git(runner)
        with self.assertRaises(pa_hooks.ContentError) as e:
            run(path, "cat-file", "-e", sh(path, "git", "rev-parse", "HEAD") + "^{commit}")
        self.assertIn("a shallow file ({})".format(target), str(e.exception))
        self.assertIn("git fetch --unshallow", str(e.exception))

    def test_ac10_a_file_made_during_the_paths_call_refuses(self):
        path = self.repo()
        _target, runner = self.made_during(path, "info/grafts", "--git-path")
        run = pa_hooks.content_git(runner)
        with self.assertRaises(pa_hooks.ContentError):
            run(path, "cat-file", "-e", "HEAD")
        self.assertEqual(runner.having("cat-file"), [], "no content call runs after the paths call refused")

    def test_a_record_refuses_when_the_file_comes_mid_record(self):
        path = self.repo()
        base = sh(path, "git", "rev-parse", "HEAD")
        tip = self.commit(path, "a", "a\n")
        _target, runner = self.made_during(path, "info/grafts", "rev-list")
        rec = pa_hooks.content_record(pa_hooks.content_git(runner), path, tip, "main", base)
        self.assertIn("a grafts file", rec.get("error", ""))


# ---- R4: one rev-list per record ----------------------------------------------------------------------------------

class R4RevList(Base):
    def test_ac4_1_one_rev_list_and_the_same_commits(self):
        path = self.repo()
        base = sh(path, "git", "rev-parse", "HEAD")
        a = self.commit(path, "a", "a\n")
        b = self.commit(path, "b", "b\n")
        runner = Counting()
        rec = pa_hooks.content_record(pa_hooks.content_git(runner), path, b, "main", base)
        self.assertEqual([c["sha"] for c in rec["commits"]], [a, b])
        self.assertEqual(len(runner.having("rev-list")), 1, runner.having("rev-list"))

    def test_ac4_1_the_merge_message_names_the_newest_merge(self):
        path = self.repo()
        base = sh(path, "git", "rev-parse", "HEAD")
        sh(path, "git", "checkout", "-q", "-b", "side")
        self.commit(path, "s1", "s\n")
        sh(path, "git", "checkout", "-q", "main")
        self.commit(path, "m1", "m\n")
        sh(path, "git", "merge", "-q", "--no-edit", "side")
        first = sh(path, "git", "rev-parse", "HEAD")
        sh(path, "git", "checkout", "-q", "side")
        self.commit(path, "s2", "s\n")
        sh(path, "git", "checkout", "-q", "main")
        self.commit(path, "m2", "m\n")
        sh(path, "git", "merge", "-q", "--no-edit", "side")
        newest = sh(path, "git", "rev-parse", "HEAD")
        self.assertNotEqual(first, newest)
        today = sh(path, "git", "rev-list", "--min-parents=2", base + ".." + newest).split()[0]
        self.assertEqual(today, newest)
        runner = Counting()
        rec = pa_hooks.content_record(pa_hooks.content_git(runner), path, newest, "main", base)
        self.assertEqual(rec, {"error": "the branch has a merge commit ({}); the content check takes linear "
                                        "branches".format(newest[:7])})
        self.assertEqual(len(runner.having("rev-list")), 1, runner.having("rev-list"))

    def test_rev_list_failure_reads_as_today(self):
        path = self.repo()
        tip = self.commit(path, "a", "a\n")

        def failing(p, *args, **kw):
            if "rev-list" in args:
                return 128, "", "fatal: boom"
            return pa_hooks.git(p, *args, **kw)
        rec = pa_hooks.content_record(pa_hooks.content_git(failing), path, tip, "main", tip)
        self.assertEqual(rec, {"error": "git rev-list failed (fatal: boom)"})


if __name__ == "__main__":
    unittest.main()
