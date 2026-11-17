"""Unit tests for the worker command allowlist (t-007 part 7a): runtime/tools/pa_guard.py and its call
from `pa-hook guard`.

Run: python3 -m unittest -v tests/test_pa_guard.py
The command lines below are data: they are only handed to the check functions, never run. No hc calls
and no real homes: HERDR_PA_HOME, CLAUDE_CONFIG_DIR and HERDR_APP_HOME point at a temp folder while
pa.py and pa_hooks.py are loaded, and every space, profile and repository is made under that folder.
"""
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.join(HERE, "..")
TOOLS = os.path.join(REPO, "runtime", "tools")
TMP = os.path.realpath(tempfile.mkdtemp(prefix="t007a-guard-"))

# Load pa.py and pa_hooks.py with the temp homes, then give the environment back (as test_pa_worker.py does).
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

# The module under test, loaded by path. Before it exists every test fails on its own (setUp), not as one import error.
try:
    _spec = importlib.util.spec_from_file_location("pa_guard_under_test", os.path.join(TOOLS, "pa_guard.py"))
    guard = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(guard)
except (OSError, ImportError) as e:  # FileNotFoundError before the implementation
    guard, GUARD_ERROR = None, e

PA = "pa1"       # the Project Agent's address (agent.json "terminal")
OWN = "w1"       # this worker's own address (HC_TERMINAL_ID)
OFF_TABLE = ("Refused by the Project Agent's allowlist: {}. Workers run only the git, gh and hc commands on the "
             "allowlist. Stop and report this to the Project Agent with hc send; it handles publishing and "
             "repository settings. There is no override.")
NOT_PLAIN = ("Refused by the Project Agent's allowlist: this line uses git, gh or hc beyond plain commands joined by "
             "; && || |. Write the command plainly and run it again. If it cannot be written plainly, stop and "
             "report to the Project Agent with hc send. There is no override.")

# AC1: at least one allowed line per row of the table (spec section 3, plus the read-only rows the person added).
# Keyed by the row's command words; test_every_row_has_a_case pins that a new row needs a new case here.
ROW_CASES = {
    ("git", "status"): ["git status", "git status --short", "git --no-pager status"],
    ("git", "diff"): ["git diff", "git diff --stat HEAD~1", "git diff --cached -- runtime/tools/pa.py", "git --no-pager diff e21fe98"],
    ("git", "log"): ["git log --oneline -5", "git --no-pager log -p -1", "git log --oneline"],
    ("git", "show"): ["git show HEAD", "git show --stat e21fe98"],
    ("git", "blame"): ["git blame -L 1,10 runtime/tools/pa.py"],
    ("git", "ls-files"): ["git ls-files -m", "git ls-files"],
    ("git", "rev-parse"): ["git rev-parse --abbrev-ref HEAD", "git rev-parse --show-toplevel"],
    ("git", "branch"): ["git branch", "git branch --show-current", "git branch -a", "git branch -r", "git branch -vv",
                        "git branch --list 't-*'", "git branch --contains HEAD", "git branch --merged main",
                        "git branch --no-merged", "git branch new-x", "git branch new-x main"],
    ("git", "switch"): ["git switch main", "git switch -c new-x", "git switch -c new-x main"],
    ("git", "checkout"): ["git checkout some-branch", "git checkout -b new-x", "git checkout -b new-x main"],
    ("git", "add"): ["git add a.py b.py", "git add -A", "git add -u", "git add -- a.py", "git add -A a.py", "git add ."],
    ("git", "restore"): ["git restore a.py", "git restore --staged a.py", "git restore --staged -- a.py b.py", "git restore -- src/a.py"],
    ("git", "commit"): ["git commit -m 'Fix: one; two | three && four'", "git commit -F msg.txt", "git commit -a -m x",
                        "git commit --amend --no-edit", "git commit --allow-empty -s -m x", "git commit -am x",
                        'git commit -m "first line\n\nbody line"'],
    ("git", "stash"): ["git stash", "git stash push -m wip -- a.py", "git stash push", "git stash list", "git stash show",
                       "git stash show stash@{1}", "git stash apply", "git stash pop stash@{0}"],
    ("git", "fetch"): ["git fetch", "git fetch --prune", "git fetch origin", "git fetch origin main"],
    ("git", "pull"): ["git pull", "git pull --rebase", "git pull --ff-only origin main"],
    ("git", "merge"): ["git merge main", "git merge --no-ff -m 'Merge x' feature", "git merge --ff-only main",
                       "git merge --abort", "git merge --continue"],
    ("git", "rebase"): ["git rebase main", "git rebase --continue", "git rebase --abort", "git rebase --skip"],
    ("git", "worktree", "list"): ["git worktree list", "git worktree list --porcelain"],
    ("git", "config"): ["git config --get user.name", "git config --get-all remote.origin.fetch", "git config --list",
                        "git config -l", "git config user.email"],
    ("gh", "pr"): ["gh pr view 12", "gh pr list --state open", "gh pr diff 12", "gh pr checks 12", "gh pr status"],
    ("gh", "issue"): ["gh issue view 3 --comments", "gh issue list", "gh issue status"],
    ("gh", "run"): ["gh run view 1 --log", "gh run list --limit 5"],
    ("gh", "release"): ["gh release view v1.0", "gh release list"],
    ("gh", "repo", "view"): ["gh repo view", "gh repo view --json name"],
    ("hc", "send"): ["hc send pa1 'done: evidence/t-x/VERIFY.md'"],
    ("hc", "read"): ["hc read w1", "hc read w1 --lines 50"],
    ("hc", "get"): ["hc get w1"],
    # the read-only rows the person added
    ("git", "archive"): ["git archive --format=tar HEAD", "git archive HEAD runtime"],
    ("git", "merge-base"): ["git merge-base HEAD main", "git merge-base --is-ancestor e21fe98 HEAD"],
    ("git", "grep"): ["git grep -n foo", "git grep -e pattern -- '*.py'", "git grep --or -e a -e b"],
    ("git", "cat-file"): ["git cat-file -p HEAD", "git cat-file -t e21fe98"],
    ("git", "ls-tree"): ["git ls-tree -r HEAD", "git ls-tree --name-only HEAD runtime"],
    ("git", "for-each-ref"): ["git for-each-ref refs/heads", "git for-each-ref --format='%(refname)' refs/tags"],
    ("git", "describe"): ["git describe --tags", "git describe --always HEAD"],
    ("git", "reflog"): ["git reflog", "git reflog show HEAD", "git reflog -n 5", "git reflog list"],
    ("git", "diff-tree"): ["git diff-tree --no-commit-id --name-only -r HEAD"],
    ("git", "cherry"): ["git cherry -v main", "git cherry"],
    ("git", "tag"): ["git tag", "git tag -l 'v*'", "git tag --list", "git tag --list 'v1.*' 'v2.*'",
                     "git tag --contains HEAD", "git tag --merged main"],
}
# The last three rows of the table and the ordinary tools: allowed whenever no git, gh or hc is involved.
FREE_CASES = ["tests/run-all.sh", "tests/run-all.sh --affected", "pa-gate t-x --phase build", "pa-full-suite",
              "python3 -m unittest -v tests/test_pa_guard.py", "ls -la", "cat README.md", "make", "node tests/keeper/run.mjs",
              "grep -rn foo runtime", "sed -n 1,5p README.md", "find . -name '*.py'", "mkdir -p out", "echo 'use git push here'"]
# Lines that reach a row but not its argument shape: refused with the off-table text.
SHAPE_REFUSALS = [
    "git diff --output=x.patch", "git diff --output x.patch", "git log --outp=x", "git show --output=x HEAD",
    "git branch -d x", "git branch -m a b", "git branch --set-upstream-to origin/x", "git branch a b c",
    "git switch --detach HEAD", "git switch -", "git checkout -b",
    "git add -f ignored.txt", "git add -p", "git add",
    "git restore", "git restore '*.py'", "git restore ./", "git restore --source HEAD a.py", "git restore :/a.py",
    "git commit --no-verify -m x", "git commit -m", "git commit a.py -m x", "git commit --fixup HEAD",
    "git stash show -p extra", "git stash push --keep-index", "git stash list --all", "git stash branch x",
    "git fetch origin +main:main", "git fetch origin main:main", "git fetch --all", "git fetch a b c",
    "git pull origin", "git pull --rebase --ff-only", "git pull --force",
    "git merge", "git merge a b", "git merge -s ours main", "git merge --no-ff --ff-only main",
    "git rebase -i main", "git rebase main topic", "git rebase --onto x y",
    "git worktree list --verbose", "git worktree add ../x", "git worktree prune",
    "git config user.name Someone", "git config --unset user.name", "git config --global --get user.name", "git config",
    "gh pr create --fill", "gh pr edit 3", "gh pr", "gh issue create", "gh run rerun 1", "gh release upload v1 a.zip",
    "gh repo edit", "gh repo create x",
    "hc send other 'text'", "hc send pa1", "hc send pa1 a b", "hc read other", "hc read w1 --lines x", "hc get other", "hc get",
    "git archive -o x.tar HEAD", "git archive --output=x.tar HEAD", "git archive -vo x.tar HEAD",
    "git grep -O vim foo", "git grep -Ovim foo", "git grep -nO foo", "git grep --open-files-in-pager foo",
    "git diff-tree --output=x HEAD", "git log --o=x",
    "git reflog expire --all", "git reflog delete HEAD@{1}", "git reflog list extra",
    "git tag v1", "git tag -d v1", "git tag -a v1 -m x", "git tag -f v1", "git tag v1 HEAD",
    "git -C /tmp/x status", "git -c core.pager=cat log", "git --git-dir=/x status", "git --no-pager",
    "FOO=1 git status", "/usr/bin/git status", "env git status", "sudo git status", "xargs git add", "! git status",
    "grep git notes.md",
]


def write_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(value, f)


class GuardTest(unittest.TestCase):
    def setUp(self):
        if guard is None:
            self.fail("runtime/tools/pa_guard.py does not load: {!r}".format(GUARD_ERROR))
        self.cwd = tempfile.mkdtemp(dir=TMP)

    def check(self, line, **kw):
        kw.setdefault("pa_address", PA)
        kw.setdefault("own_address", OWN)
        kw.setdefault("cwd", self.cwd)
        return guard.check_command(line, **kw)


class AC1EveryRowAllowed(GuardTest):
    def test_every_row_has_a_case(self):
        rows = {tuple(row[0]) for row in guard.ALLOWLIST}
        self.assertEqual(sorted(rows - set(ROW_CASES)), [], "rows without a positive test")
        self.assertEqual(sorted(set(ROW_CASES) - rows), [], "cases for rows that are not in ALLOWLIST")

    def test_each_row_allows_its_shapes(self):
        for command, lines in sorted(ROW_CASES.items()):
            for line in lines:
                with self.subTest(row=" ".join(command), line=line):
                    self.assertIsNone(self.check(line))

    def test_free_tools_and_last_three_rows(self):
        for line in FREE_CASES:
            with self.subTest(line=line):
                self.assertIsNone(self.check(line))

    def test_shapes_outside_a_row_are_refused(self):
        for line in SHAPE_REFUSALS:
            with self.subTest(line=line):
                message = self.check(line)
                self.assertIsNotNone(message, line)
                self.assertTrue(message.startswith("Refused by the Project Agent's allowlist: "), message)
                self.assertNotEqual(message, NOT_PLAIN)

    def test_checkout_of_an_existing_path_is_refused(self):
        os.makedirs(os.path.join(self.cwd, "sub"))
        open(os.path.join(self.cwd, "a.py"), "w").close()
        open(os.path.join(self.cwd, "sub", "b.py"), "w").close()
        self.assertIsNotNone(self.check("git checkout a.py"))
        self.assertIsNotNone(self.check("git checkout sub"))
        self.assertIsNotNone(self.check("cd sub && git checkout b.py"))
        self.assertIsNone(self.check("git checkout b.py"))  # no such path here: a branch name
        self.assertIsNotNone(self.check("cd - && git checkout x"))  # folder unknown: refused

    def test_hc_send_without_a_known_project_agent_allows_one_address(self):  # open question Q2, accepted
        self.assertIsNone(self.check("hc send anyone 'report'", pa_address=None))
        self.assertIsNotNone(self.check("hc send anyone two 'report'", pa_address=None))
        self.assertIsNotNone(self.check("hc send --all 'report'", pa_address=None))

    def test_hc_read_without_own_address_is_refused(self):
        self.assertIsNotNone(self.check("hc read w1", own_address=None))
        self.assertIsNotNone(self.check("hc get w1", own_address=None))


class AC2OffTable(GuardTest):
    def test_one_off_table_subcommand_each(self):
        for line in ["git push origin t-x", "gh api repos/x/y", "hc kill w2"]:
            with self.subTest(line=line):
                self.assertEqual(self.check(line), OFF_TABLE.format(line))

    def test_more_off_table(self):
        for line in ["git remote add origin x", "git remote -v", "git gc", "git submodule update", "gh auth status",
                     "gh secret list", "hc run sh", "hc exec ls", "git", "gh", "hc"]:
            with self.subTest(line=line):
                self.assertEqual(self.check(line), OFF_TABLE.format(line))


class AC3NotPlainWithGit(GuardTest):
    def test_not_plain_lines_that_involve_git_gh_or_hc(self):
        for line in ["git diff > out.patch", "git log 2>&1", "git status &", "echo $(git rev-parse HEAD)",
                     "echo `git rev-parse HEAD`", "git commit -F - <<EOF\nmsg\nEOF", "(git status)", "{ git status; }",
                     "git status\ngit push", "git status |& cat", "g=gi''t; $g push", "cat <(gh pr view)",
                     "hc send pa1 \"$(cat report.md)\"", "git add *.py", "git status \\\n --short", "=git push",
                     "git log -1 # comment", "git add {a,b}.py", "git status;; ls", "git status && ", "| git status",
                     "git commit -m 'unterminated", "/usr/bin/git push > /dev/null"]:
            with self.subTest(line=line):
                self.assertEqual(self.check(line), NOT_PLAIN)


class AC4NotPlainWithoutGit(GuardTest):
    def test_not_plain_lines_without_git_gh_or_hc(self):
        for line in ["make test > build.log 2>&1", "for f in *.py; do echo $f; done", "echo $HOME",
                     "python3 -c 'print(1)' | tee out.txt", "cat <<EOF > notes.txt\nplain notes\nEOF",
                     "(cd sub && make)", "ls *.py", "echo github.com/x/y > url.txt", "cat .git/HEAD > /tmp/head"]:
            with self.subTest(line=line):
                self.assertIsNone(self.check(line))


class AC5JoinedLines(GuardTest):
    def test_joined_allowed_commands(self):
        for line in ["cd x && git status && git diff | head", "git add a.py && git commit -m 'x; y' && git log -1",
                     "git status; git log --oneline -3 || true", "git status;", "ls && pa-full-suite"]:
            with self.subTest(line=line):
                self.assertIsNone(self.check(line))

    def test_one_off_table_part_refuses_the_line(self):
        self.assertEqual(self.check("git status && git push origin t-x"), OFF_TABLE.format("git push origin t-x"))
        self.assertEqual(self.check("ls; git remote -v"), OFF_TABLE.format("git remote -v"))
        self.assertEqual(self.check("git log | grep git"), OFF_TABLE.format("grep git"))

    def test_split_plain_keeps_quoted_operators_in_one_word(self):
        self.assertEqual(guard.split_plain("git commit -m 'a; b | c && d' && git log"),
                         [["git", "commit", "-m", "a; b | c && d"], ["git", "log"]])
        self.assertEqual(guard.split_plain('echo "x \\"y\\" z" | cat'), [["echo", 'x "y" z'], ["cat"]])
        self.assertEqual(guard.split_plain("git show stash@{0}"), [["git", "show", "stash@{0}"]])
        self.assertEqual(guard.split_plain(""), [])
        self.assertIsNone(guard.split_plain("a > b"))

    def test_involves(self):
        self.assertTrue(guard.involves(["/usr/bin/git", "status"]))
        self.assertTrue(guard.involves(["xargs", "gh"]))
        self.assertFalse(guard.involves(["cat", ".git/HEAD"]))
        self.assertFalse(guard.involves(["git-lfs", "ls-files"]))
        self.assertFalse(guard.involves(["echo", "see github.com"]))


class AC6EditorRule(GuardTest):
    def setUp(self):
        super().setUp()
        self.spaces = os.path.join(TMP, "spaces-ac6")
        self.state = os.path.join(self.spaces, "sp1")
        self.repo = os.path.join(TMP, "repo-ac6")
        for d in [os.path.join(self.state, "decisions"), os.path.join(self.state, "evidence", "t-x"),
                  os.path.join(self.repo, ".git", "hooks"), os.path.join(self.repo, "src")]:
            os.makedirs(d, exist_ok=True)

    def edit(self, path):
        return guard.check_edit(path, self.spaces, cwd=self.repo)

    def test_refused(self):
        for path in [os.path.join(self.repo, ".git", "config"), os.path.join(self.repo, ".git", "hooks", "pre-commit"),
                     os.path.join(self.repo, ".GIT", "config"), os.path.join(self.repo, "sub", ".git"),
                     ".git/info/exclude", os.path.join(self.state, "ledger.json"),
                     os.path.join(self.state, "decisions", "d-001.json"), os.path.join(self.state, "journal.jsonl"),
                     os.path.join(self.state, "rules.json"), os.path.join(self.state, "agent.json")]:
            with self.subTest(path=path):
                self.assertEqual(self.edit(path), OFF_TABLE.format(path))

    def test_links_are_resolved(self):
        link = os.path.join(self.repo, "src", "innocent.txt")
        if not os.path.lexists(link):
            os.symlink(os.path.join(self.repo, ".git", "config"), link)
        self.assertIsNotNone(self.edit(link))
        state_link = os.path.join(self.repo, "state")
        if not os.path.lexists(state_link):
            os.symlink(self.state, state_link)
        self.assertIsNotNone(self.edit(os.path.join(state_link, "ledger.json")))

    def test_allowed(self):
        for path in [os.path.join(self.state, "evidence", "t-x", "x.md"), os.path.join(self.repo, "src", "a.py"),
                     os.path.join(self.repo, ".github", "workflows", "ci.yml"), "src/b.py", "",
                     os.path.join(self.state, "evidence", "t-x", "ledger.json")]:
            with self.subTest(path=path):
                self.assertIsNone(self.edit(path))


class AC7Messages(GuardTest):
    def test_message_contents(self):
        for message, item in [(self.check("git push origin t-x"), "git push origin t-x"),
                              (guard.check_edit(os.path.join(TMP, "r", ".git", "config"), os.path.join(TMP, "none")),
                               os.path.join(TMP, "r", ".git", "config"))]:
            for words in [item, "report", "Project Agent", "There is no override"]:
                self.assertIn(words, message)
        message = self.check("git diff > x")
        for words in ["report", "Project Agent", "There is no override", "plainly"]:
            self.assertIn(words, message)

    def test_exact_texts(self):
        self.assertEqual(guard.OFF_TABLE, OFF_TABLE)
        self.assertEqual(guard.NOT_PLAIN, NOT_PLAIN)

    def test_quoted_words_are_shown_quoted(self):
        self.assertEqual(self.check("git push origin 'a b'"), OFF_TABLE.format("git push origin 'a b'"))


class AC8ProfileRows(GuardTest):
    def rows(self, allow):
        err = io.StringIO()
        rows = guard.profile_rows({"name": "p", "guardAllow": allow}, err=err)
        return rows, err.getvalue()

    def test_added_rows_widen(self):
        rows, note = self.rows([{"command": ["git", "lfs", "pull"], "args": "any"}, {"command": ["git", "gc"], "args": "none"}])
        self.assertEqual(note, "")
        self.assertIsNotNone(self.check("git lfs pull"))
        self.assertIsNone(self.check("git lfs pull --include x", extra_rows=rows))
        self.assertIsNone(self.check("git gc", extra_rows=rows))
        self.assertIsNotNone(self.check("git gc --aggressive", extra_rows=rows))

    def test_never_add_rows_are_ignored_with_a_note(self):
        for command, line in [(["git", "push"], "git push origin x"), (["git"], "git push origin x"),
                              (["gh", "pr"], "gh pr create"), (["gh", "pr", "merge"], "gh pr merge 1"),
                              (["git", "remote", "add"], "git remote add x y"), (["git", "config"], "git config a b"),
                              (["gh", "api"], "gh api x"), (["hc", "send"], "hc send other x")]:
            with self.subTest(command=command):
                rows, note = self.rows([{"command": command, "args": "any"}])
                self.assertEqual(rows, [])
                self.assertIn(" ".join(command), note)
                self.assertIsNotNone(self.check(line, extra_rows=rows))

    def test_malformed_rows_are_ignored(self):
        rows, note = self.rows([{"command": "git gc", "args": "any"}, {"command": ["git", "gc"], "args": "some"},
                                {"command": ["make"], "args": "any"}, "x"])
        self.assertEqual(rows, [])
        self.assertTrue(note)
        self.assertEqual(self.rows(None)[0], [])
        self.assertEqual(guard.profile_rows(None), [])


class AC9Fast(GuardTest):
    def test_no_subprocess_and_fast(self):
        line = ""
        while len(line) < 2000:
            line += "git status && git diff --stat HEAD~1 | head -n 20; git commit -m 'one; two | three' && "
        line = line + "git log --oneline -5"
        self.assertGreaterEqual(len(line), 2000)
        forbidden = mock.Mock(side_effect=AssertionError("a subprocess was started"))
        with mock.patch.object(subprocess, "run", forbidden), mock.patch.object(subprocess, "Popen", forbidden), \
                mock.patch.object(os, "system", forbidden):
            self.assertIsNone(self.check(line))
            guard.check_edit(os.path.join(TMP, "x", ".git", "config"), TMP)
            times = []
            for _ in range(100):
                start = time.perf_counter()
                self.check(line)
                times.append(time.perf_counter() - start)
        self.assertLess(statistics.median(times), 0.010)


class HookGuard(unittest.TestCase):
    """Through pa-hook guard (pa_hooks.hook_guard): exit 0, or SystemExit(2) with the message on stderr."""

    @classmethod
    def setUpClass(cls):
        cls.space = "spguard"
        cls.state = pa.space_dir(cls.space)
        write_json(os.path.join(cls.state, "agent.json"), {"terminal": PA, "profile": "guardp", "space": cls.space})
        write_json(os.path.join(cls.state, "ledger.json"), {"tasks": []})
        write_json(os.path.join(pa.ROOT, "profiles", "guardp", "profile.json"),
                   {"name": "guardp", "claude": ["true"], "codex": ["true"],
                    "guardAllow": [{"command": ["git", "lfs", "pull"], "args": "any"}, {"command": ["git", "push"], "args": "any"}]})
        cls.wt = os.path.join(TMP, "wt-hook")
        write_json(os.path.join(cls.wt, ".claude", "pa-task.json"), {"task": "t-x", "space": cls.space, "fix": False})
        cls.bare = os.path.join(TMP, "no-record")
        os.makedirs(cls.bare, exist_ok=True)

    def run_hook(self, tool, tool_input, cwd=None):
        err = io.StringIO()
        data = {"tool_name": tool, "tool_input": tool_input, "cwd": cwd or self.wt}
        with mock.patch.dict(os.environ, {"HC_TERMINAL_ID": OWN}), contextlib.redirect_stderr(err):
            try:
                code = pa_hooks.hook_guard(data, [])
            except SystemExit as e:
                code = e.code
        return code, err.getvalue()

    def bash(self, command, cwd=None):
        return self.run_hook("Bash", {"command": command}, cwd)

    def test_allowed_and_refused(self):
        self.assertEqual(self.bash("git status && git diff | head")[0], 0)
        code, err = self.bash("git push origin t-x")
        self.assertEqual(code, 2)
        self.assertIn(OFF_TABLE.format("git push origin t-x"), err)
        code, err = self.bash("git diff > x.patch")
        self.assertEqual((code, err.strip()), (2, NOT_PLAIN))

    def test_hc_send_goes_to_the_project_agent_from_agent_json(self):
        self.assertEqual(self.bash("hc send pa1 'done'")[0], 0)
        self.assertEqual(self.bash("hc send w9 'done'")[0], 2)
        self.assertEqual(self.bash("hc read w1 --lines 20")[0], 0)

    def test_no_task_record_still_applies_the_allowlist(self):  # Q2, accepted
        self.assertEqual(self.bash("hc send anyone 'done'", cwd=self.bare)[0], 0)
        self.assertEqual(self.bash("git push origin t-x", cwd=self.bare)[0], 2)
        self.assertEqual(self.bash("git status", cwd=self.bare)[0], 0)

    def test_profile_rows_through_the_hook(self):  # AC8 through the hook
        self.assertEqual(self.bash("git lfs pull")[0], 0)
        code, err = self.bash("git push origin t-x")
        self.assertEqual(code, 2)
        self.assertIn("git push", err)

    def test_editor_tools(self):  # AC6 through the hook
        for tool, key in [("Edit", "file_path"), ("Write", "file_path"), ("MultiEdit", "file_path"), ("NotebookEdit", "notebook_path")]:
            with self.subTest(tool=tool):
                self.assertEqual(self.run_hook(tool, {key: os.path.join(self.state, "ledger.json")})[0], 2)
                self.assertEqual(self.run_hook(tool, {key: os.path.join(self.state, "decisions", "d-1.json")})[0], 2)
                self.assertEqual(self.run_hook(tool, {key: os.path.join(self.wt, ".git", "config")})[0], 2)
                self.assertEqual(self.run_hook(tool, {key: os.path.join(self.state, "evidence", "t-x", "x.md")})[0], 0)
                self.assertEqual(self.run_hook(tool, {key: os.path.join(self.wt, "src", "a.py")})[0], 0)
        self.assertEqual(self.run_hook("Edit", {"file_path": os.path.join(self.state, "ledger.json")}, cwd=self.bare)[0], 2)
        self.assertEqual(self.run_hook("Read", {"file_path": os.path.join(self.state, "ledger.json")})[0], 0)
        self.assertEqual(self.run_hook("Read", {"file_path": os.path.join(self.wt, ".git", "config")})[0], 0)


class AC10Unchanged(unittest.TestCase):
    """The existing refusals run first with their own messages (R5), and the tool is wired in."""

    run_hook = HookGuard.run_hook
    bash = HookGuard.bash

    @classmethod
    def setUpClass(cls):
        HookGuard.setUpClass.__func__(cls)

    def test_existing_refusals_come_first(self):
        for command, text in [("git push --force origin t-x", "Blocked by the Project Agent safety hook: a force push."),
                              ("git push origin main", "Blocked by the Project Agent safety hook: a push to the main branch."),
                              ("git reset --hard", "Blocked by the Project Agent safety hook: discarding uncommitted work (git reset --hard)."),
                              ("git stash drop", "Blocked by the Project Agent safety hook: deleting stashed work."),
                              ("cat .env", "Blocked by the Project Agent safety hook: reading secrets.")]:
            with self.subTest(command=command):
                code, err = self.bash(command)
                self.assertEqual(code, 2)
                self.assertTrue(err.startswith(text), err)
                self.assertNotIn("allowlist", err)
        code, err = self.run_hook("Read", {"file_path": os.path.join(self.wt, ".env")})
        self.assertEqual(code, 2)
        self.assertIn("an environment file with secrets", err)

    def test_wired(self):
        settings = pa_hooks.hook_settings()["PreToolUse"]
        guard_entry = [e for e in settings if any("pa-hook guard" in h["command"] for h in e["hooks"])]
        self.assertEqual(len(guard_entry), 1)
        self.assertEqual(set(guard_entry[0]["matcher"].split("|")), {"Bash", "Read", "Edit", "Write", "MultiEdit", "NotebookEdit"})
        with open(os.path.join(REPO, "tests", "run-all.sh")) as f:
            self.assertIn("runtime/tools/pa_guard.py", f.read())
        with open(os.path.join(TOOLS, "pa_guard.py")) as f:
            import ast
            ast.parse(f.read(), feature_version=(3, 7))

    def test_worker_rule_line(self):
        with open(os.path.join(REPO, "runtime", "core", "worker.md")) as f:
            text = f.read()
        rules = text.split("## Rules of the job", 1)[1].split("\n## ", 1)[0]
        flat = re.sub(r"\s+", " ", rules)
        self.assertIn("Commands that involve git, gh or hc are limited to the allowlist in `pa_guard.py`; anything "
                      "else is refused: report it to the Project Agent.", flat)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
