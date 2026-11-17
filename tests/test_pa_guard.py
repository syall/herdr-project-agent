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
        self.assertTrue(guard.involves(["git-lfs", "ls-files"]))  # fix round 2: a dashed git program is git
        self.assertFalse(guard.involves(["cat", "docs/git-guide.md"]))
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


# ---- Allowlist guard fix round: each case makes the guard stricter -------------------------------------
# Finding ids: reviewer 1 #n (REVIEW.md), reviewer 2 In/Mn (REVIEW-2.md), verifier Fn (VERIFY.md).

GUARD_UNAVAILABLE_WORDS = ["guard unavailable", "report", "Project Agent", "There is no override",
                           "hc send is refused too", "last message"]  # fix round 2: REVIEW-4 M1


def load_hooks_copy(folder):
    """pa_hooks.py copied into folder (with whatever pa_guard.py the test put there), loaded with the temp pa."""
    shutil.copy(os.path.join(TOOLS, "pa_hooks.py"), os.path.join(folder, "pa_hooks.py"))
    saved = sys.modules.get("pa")
    sys.modules["pa"] = pa
    try:
        spec = importlib.util.spec_from_file_location("pa_hooks_copy", os.path.join(folder, "pa_hooks.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if saved is None:
            del sys.modules["pa"]
        else:
            sys.modules["pa"] = saved


def call_hook(hooks, function, tool, tool_input, cwd):
    err = io.StringIO()
    data = {"tool_name": tool, "tool_input": tool_input, "cwd": cwd}
    with mock.patch.dict(os.environ, {"HC_TERMINAL_ID": OWN}), contextlib.redirect_stderr(err):
        try:
            code = function(data, [])
        except SystemExit as e:
            code = e.code
    return code, err.getvalue()


class FixA_FailClosed(unittest.TestCase):
    """(a) card d-038 'Block'; reviewer 1 #1, reviewer 2 I2: a guard that cannot load or that raises refuses the call."""

    @classmethod
    def setUpClass(cls):
        HookGuard.setUpClass.__func__(cls)

    def copy(self, guard_text=None):
        folder = tempfile.mkdtemp(dir=TMP)
        if guard_text is not None:
            with open(os.path.join(folder, "pa_guard.py"), "w") as f:
                f.write(guard_text)
        try:
            return load_hooks_copy(folder)
        except Exception as e:  # noqa: BLE001 - before the fix the import itself dies
            self.fail("pa_hooks.py must load without a working pa_guard.py (the other hooks keep working): {!r}".format(e))

    def assert_unavailable(self, code, err):
        self.assertEqual(code, 2, err)
        for words in GUARD_UNAVAILABLE_WORDS:
            self.assertIn(words, err)

    def test_missing_guard_blocks_bash_and_edits_and_other_hooks_work(self):
        hooks = self.copy()
        self.assert_unavailable(*call_hook(hooks, hooks.hook_guard, "Bash", {"command": "git status"}, self.wt))
        self.assert_unavailable(*call_hook(hooks, hooks.hook_guard, "Bash", {"command": "ls"}, self.bare))
        self.assert_unavailable(*call_hook(hooks, hooks.hook_guard, "Edit", {"file_path": os.path.join(self.wt, "a.py")}, self.wt))
        self.assertEqual(call_hook(hooks, hooks.hook_guard, "Read", {"file_path": os.path.join(self.wt, "a.py")}, self.wt)[0], 0)
        self.assertEqual(call_hook(hooks, hooks.hook_test_lock, "Edit", {"file_path": os.path.join(self.wt, "a.py")}, self.wt)[0], 0)
        # the older refusals still come first, with their own message
        code, err = call_hook(hooks, hooks.hook_guard, "Bash", {"command": "git stash drop"}, self.wt)
        self.assertEqual(code, 2)
        self.assertIn("Blocked by the Project Agent safety hook", err)

    def test_broken_guard_blocks(self):
        hooks = self.copy("raise RuntimeError('half-installed')\n")
        self.assert_unavailable(*call_hook(hooks, hooks.hook_guard, "Bash", {"command": "git status"}, self.wt))
        self.assertEqual(call_hook(hooks, hooks.hook_test_lock, "Edit", {"file_path": os.path.join(self.wt, "a.py")}, self.wt)[0], 0)

    def test_a_check_that_raises_blocks(self):
        def boom(*_a, **_k):
            raise RuntimeError("boom")
        broken = mock.Mock(check_command=boom, check_edit=boom, profile_rows=boom)
        with mock.patch.object(pa_hooks, "_load_guard", return_value=broken):
            self.assert_unavailable(*call_hook(pa_hooks, pa_hooks.hook_guard, "Bash", {"command": "git status"}, self.wt))
            self.assert_unavailable(*call_hook(pa_hooks, pa_hooks.hook_guard, "Write", {"file_path": os.path.join(self.wt, "a.py")}, self.wt))
        with mock.patch.object(pa_hooks, "hook_context", side_effect=OSError("gone")):
            self.assert_unavailable(*call_hook(pa_hooks, pa_hooks.hook_guard, "Bash", {"command": "git status"}, self.wt))

    def test_a_malformed_profile_does_not_open_the_table(self):  # reviewer 2 I2
        for n, allow in enumerate([5, [{"command": ["git", "lfs"], "args": ["any"]}], "rows", [{"command": ["git", "push"], "args": {}}]]):
            with self.subTest(allow=allow):
                space = "spbad{}".format(n)
                state = pa.space_dir(space)
                write_json(os.path.join(state, "agent.json"), {"terminal": PA, "profile": "badp{}".format(n), "space": space})
                write_json(os.path.join(pa.ROOT, "profiles", "badp{}".format(n), "profile.json"),
                           {"name": "badp{}".format(n), "claude": ["true"], "codex": ["true"], "guardAllow": allow})
                wt = os.path.join(TMP, "wt-bad{}".format(n))
                write_json(os.path.join(wt, ".claude", "pa-task.json"), {"task": "t-x", "space": space, "fix": False})
                code, err = call_hook(pa_hooks, pa_hooks.hook_guard, "Bash", {"command": "git push origin t-x"}, wt)
                self.assertEqual(code, 2)
                self.assertIn(OFF_TABLE.format("git push origin t-x"), err)
                self.assertEqual(call_hook(pa_hooks, pa_hooks.hook_guard, "Bash", {"command": "git status"}, wt)[0], 0)


class FixB_CheckoutBranchOnly(GuardTest):
    """(b) reviewer 1 #2, reviewer 2 I1, verifier F1: git checkout takes a branch name only."""

    def test_pathspec_words_are_refused(self):
        for line in ["git checkout '*.py'", "git checkout ':/'", "git checkout ':(glob)**'", "git checkout 'a?b'",
                     "git checkout '[ab]c'", "git checkout ':!x'", "git checkout -b x '*.py'", "git checkout -b ':x'",
                     "git checkout -b x ':/'", "git checkout -b 'n*'", "git --no-pager checkout '*.md'"]:
            with self.subTest(line=line):
                self.assertTrue((self.check(line) or "").startswith("Refused by the Project Agent's allowlist: git"), line)

    def test_branch_words_still_allowed(self):
        for line in ["git checkout some-branch", "git checkout HEAD~1", "git checkout -b new-x", "git checkout -b new-x main",
                     "git checkout feature/x"]:
            with self.subTest(line=line):
                self.assertIsNone(self.check(line))

    def test_a_cd_that_may_not_run_leaves_the_folder_unknown(self):  # reviewer 2 I1, the cd blind spot
        os.makedirs(os.path.join(self.cwd, "sub"))
        open(os.path.join(self.cwd, "a.py"), "w").close()
        open(os.path.join(self.cwd, "sub", "b.py"), "w").close()
        for line in ["cd /nonexistent || true; git checkout a.py", "cd sub; git checkout a.py", "true || cd sub && git checkout a.py",
                     "ls | cd sub && git checkout a.py", "pushd sub && git checkout a.py", "popd && git checkout x",
                     "cd sub || true && git checkout a.py"]:
            with self.subTest(line=line):
                self.assertIsNotNone(self.check(line))
        for line in ["cd sub && git checkout a.py", "git status; cd sub && git checkout x", "cd sub && cd .. && git checkout x"]:
            with self.subTest(line=line):
                self.assertIsNone(self.check(line))


class FixC_ConfigReadOnly(GuardTest):
    """(c) reviewer 1 #3, reviewer 2 I4: git config reads only (a key containing a dot, list, get <key>)."""

    def test_refused(self):
        for line in ["git config edit", "git config set", "git config unset", "git config rename-section",
                     "git config remove-section", "git config foo", "git config get", "git config get a.b c",
                     "git config get edit", "git config --get edit", "git config --get-all x", "git config list x",
                     "git config set user.name x", "git config get --all a.b", "git config .", "git config -e"]:
            with self.subTest(line=line):
                self.assertIsNotNone(self.check(line))

    def test_allowed(self):
        for line in ["git config user.email", "git config list", "git config get user.name", "git config --get user.name",
                     "git config --get-all remote.origin.fetch", "git config --list", "git config -l", "git config branch.x.remote"]:
            with self.subTest(line=line):
                self.assertIsNone(self.check(line))


class FixD_ProfileOptionRows(AC8ProfileRows):
    """(d) reviewer 1 #4, verifier F4: guardAllow rows whose words after the program start with - are ignored, with a note."""

    def test_option_rows_are_ignored(self):
        for command, line in [(["git", "-c"], "git -c a=b push origin x"), (["git", "-C"], "git -C . push origin x"),
                              (["git", "--no-pager"], "git --no-pager --no-pager push origin x"),
                              (["gh", "--repo"], "gh --repo x pr create"), (["git", "-c", "a=b", "push"], "git -c a=b push origin x"),
                              (["git", "lfs", "--x"], "git lfs --x"), (["hc", "-v"], "hc -v kill w2")]:
            with self.subTest(command=command):
                rows, note = self.rows([{"command": command, "args": "any"}])
                self.assertEqual(rows, [])
                self.assertIn(" ".join(command), note)
                self.assertIsNotNone(self.check(line, extra_rows=rows))

    def test_profile_rows_are_total(self):  # reviewer 2 I2
        for allow in [5, "rows", {"command": ["git", "gc"]}, [{"command": ["git", "gc"], "args": ["any"]}],
                      [{"command": ["git", "gc"], "args": {"x": 1}}], [{"command": ["git", 5], "args": "any"}], [None]]:
            with self.subTest(allow=allow):
                rows, note = self.rows(allow)
                self.assertEqual(rows, [])
                self.assertTrue(note)
        for profile in ["x", 5, ["guardAllow"]]:
            with self.subTest(profile=profile):
                self.assertEqual(guard.profile_rows(profile, err=io.StringIO()), [])


class FixE_ShellWrappers(GuardTest):
    """(e) reviewer 2 I3: a shell -c wrapper or eval whose text names git, gh or hc as a whole word involves them."""

    def test_refused(self):
        for line in ['sh -c "git push origin x"', 'bash -c "gh pr create"', 'zsh -c "hc send x y"', "dash -c 'git push'",
                     "ksh -c 'git push'", "eval 'git push'", 'eval "gh api x"', "bash -lc 'git push'", "/bin/sh -c 'git push'",
                     "env bash -c 'git push'", "echo 'git push' | sh", "sh -c 'GIT push'", "bash -c 'git status'",
                     "xargs sh -c 'git push' _", "sh -c 'cd x; git push'", "BASH -c 'git push'", "sh -c 'x=1 /usr/bin/git push'"]:
            with self.subTest(line=line):
                message = self.check(line)
                self.assertIsNotNone(message, line)
                self.assertTrue(message.startswith("Refused by the Project Agent's allowlist: "), message)

    def test_ordinary_uses_stay_allowed(self):
        for line in ["sh -c 'make test'", "bash tests/run-all.sh", "sh -c 'echo github.com'", "grep 'git push' notes.md",
                     "echo 'use git push here'", "echo 'git'' notes'", "eval 'make'", "bash -c 'ls .git'", "sh -c 'cat x.git'"]:
            with self.subTest(line=line):
                self.assertIsNone(self.check(line))


class FixF_ProgramCase(GuardTest):
    """(f) verifier F2: git, gh and hc are recognised without regard to case; only the exact lowercase word matches a row."""

    def test_case_variants_are_refused(self):
        for line in ["GIT push origin x", "Gh pr create", "HC send x y", "GIT status", "Git log", "/usr/bin/GIT push",
                     "xargs GIT push", "gIt diff", "HC read w1"]:
            with self.subTest(line=line):
                self.assertEqual(self.check(line), OFF_TABLE.format(line))
        for line in ["GIT diff > x", "echo $(Git rev-parse HEAD)"]:
            with self.subTest(line=line):
                self.assertEqual(self.check(line), NOT_PLAIN)

    def test_other_words_stay_allowed(self):
        for line in ["echo GITHUB", "ls Git.log", "cat .GIT/HEAD", "make > GIT.log", "grep -rn gitignore ."]:
            with self.subTest(line=line):
                self.assertIsNone(self.check(line))


class FixG_ArchiveRemote(GuardTest):
    """(g) reviewer 1 #6, reviewer 2 M5, verifier F3: git archive refuses --remote and --exec and their abbreviations."""

    def test_refused(self):
        for line in ["git archive --remote=. HEAD", "git archive --remote . HEAD", "git archive --exec=x --remote=. HEAD",
                     "git archive --rem=. HEAD", "git archive --r=. HEAD", "git archive --e=x HEAD", "git archive --ex x HEAD",
                     "git archive --format=tar --exec=./x --remote=. HEAD", "git archive HEAD --remote=."]:
            with self.subTest(line=line):
                self.assertIsNotNone(self.check(line))

    def test_allowed(self):
        for line in ["git archive --format=tar HEAD", "git archive HEAD runtime", "git archive --prefix=x/ HEAD"]:
            with self.subTest(line=line):
                self.assertIsNone(self.check(line))


class FixH_NeverAdd(AC8ProfileRows):
    """(h) reviewer 1 #5, reviewer 2 M1: NEVER_ADD covers git lfs push and the missing gh groups."""

    def test_more_rows_are_ignored(self):
        for command in [["git", "lfs"], ["git", "lfs", "push"], ["git", "svn"], ["git", "p4"], ["git", "daemon"],
                        ["git", "instaweb"], ["git", "imap-send"], ["git", "http-push"], ["git", "send-pack"],
                        ["gh", "run"], ["gh", "run", "rerun"], ["gh", "run", "cancel"], ["gh", "run", "delete"],
                        ["gh", "issue", "delete"], ["gh", "issue", "transfer"], ["gh", "issue", "reopen"],
                        ["gh", "issue", "develop"], ["gh", "issue", "lock"], ["gh", "issue", "pin"],
                        ["gh", "pr", "update-branch"], ["gh", "pr", "revert"], ["gh", "pr", "lock"], ["gh", "label"],
                        ["gh", "cache"], ["gh", "project"], ["gh", "ssh-key"], ["gh", "gpg-key"], ["gh", "codespace"],
                        ["gh", "org"]]:
            with self.subTest(command=command):
                rows, note = self.rows([{"command": command, "args": "any"}])
                self.assertEqual(rows, [])
                self.assertIn(" ".join(command), note)

    def test_lfs_pull_still_added(self):
        rows, note = self.rows([{"command": ["git", "lfs", "pull"], "args": "any"}])
        self.assertEqual((len(rows), note), (1, ""))


class FixH_Restore(GuardTest):
    """(h) reviewer 1 #9, reviewer 2 M4: git restore names files, not folders."""

    def test_folders_are_refused(self):
        os.makedirs(os.path.join(self.cwd, "sub", "deeper"))
        for line in ["git restore ../..", "git restore ..//..", "git restore sub", "git restore sub/", "git restore sub/deeper/..",
                     "git restore " + self.cwd, "git restore /", "git restore --staged sub", "git restore -- sub",
                     "cd - && git restore a.py", "cd /nonexistent; git restore a.py", "git restore '~'"]:
            with self.subTest(line=line):
                self.assertIsNotNone(self.check(line))

    def test_files_still_allowed(self):
        os.makedirs(os.path.join(self.cwd, "sub"))
        for line in ["git restore a.py", "git restore --staged sub/a.py", "git restore ../a.py", "cd sub && git restore a.py",
                     "git restore " + os.path.join(self.cwd, "a.py")]:
            with self.subTest(line=line):
                self.assertIsNone(self.check(line))


class FixH_EditorRule(GuardTest):
    """(h) reviewer 1 #7, verifier F4 (profiles folder), reviewer 2 M2 (.git reached through a link)."""

    run_hook = HookGuard.run_hook

    @classmethod
    def setUpClass(cls):
        HookGuard.setUpClass.__func__(cls)

    def setUp(self):
        super().setUp()
        self.home = os.path.join(TMP, "home-fix")
        self.spaces = os.path.join(self.home, "spaces")
        self.state = os.path.join(self.spaces, "sp1")
        self.repo = os.path.join(TMP, "repo-fix")
        for d in [os.path.join(self.state, "evidence", "t-x"), os.path.join(self.home, "profiles", "p"),
                  os.path.join(self.home, "core"), os.path.join(self.repo, "sub"), os.path.join(self.repo, "realgit")]:
            os.makedirs(d, exist_ok=True)
        link = os.path.join(self.repo, "sub", ".git")
        if not os.path.lexists(link):
            os.symlink(os.path.join(self.repo, "realgit"), link)

    def edit(self, path):
        return guard.check_edit(path, self.spaces, cwd=self.repo, home=self.home)

    def test_refused(self):
        for name in ["pass-request.json", "schedule.json", "ledger.last-good.json", "agent.json.bak-t1", "heartbeat",
                     "session.log", ".ledger.lock", os.path.join("other", "x.json")]:
            with self.subTest(name=name):
                self.assertIsNotNone(self.edit(os.path.join(self.state, name)))
        for path in [os.path.join(self.spaces, "registry.json"), os.path.join(self.home, "profiles", "p", "profile.json"),
                     os.path.join(self.home, "core", "worker.md"), os.path.join(self.home, "config.json"),
                     os.path.join(self.repo, "sub", ".git", "config"), "sub/.git/hooks/pre-commit"]:
            with self.subTest(path=path):
                self.assertEqual(self.edit(path), OFF_TABLE.format(path))

    def test_allowed(self):
        for path in [os.path.join(self.state, "evidence", "t-x", "x.md"), os.path.join(self.state, "evidence", "t-y", "build.log"),
                     os.path.join(self.repo, "realgit", "notes.txt"), os.path.join(self.repo, "sub", "a.py")]:
            with self.subTest(path=path):
                self.assertIsNone(self.edit(path))

    def test_through_the_hook(self):
        profile = os.path.join(pa.ROOT, "profiles", "guardp", "profile.json")
        code, err = self.run_hook("Edit", {"file_path": profile})
        self.assertEqual(code, 2)
        self.assertIn(profile, err)
        self.assertEqual(self.run_hook("Write", {"file_path": os.path.join(pa.space_dir("spguard"), "schedule.json")})[0], 2)
        self.assertEqual(self.run_hook("Write", {"file_path": os.path.join(pa.space_dir("spguard"), "evidence", "t-x", "a.md")})[0], 0)


class FixH_HcSendText(GuardTest):
    """(h) reviewer 2 nit: the hc send text is a word, not an option."""

    def test_refused(self):
        for line in ["hc send pa1 --force", "hc send pa1 -", "hc send pa1 -x", "hc send --force pa1"]:
            with self.subTest(line=line):
                self.assertIsNotNone(self.check(line))
        self.assertIsNone(self.check("hc send pa1 'done - see evidence'"))


# ---- Fix round 2 -----------------------------------------------------------------------------------
# A user home (HOME is patched to it) holding the Project Agent's home at ~/.config/herdr-pa, and a worktree beside it.

class Round2(GuardTest):
    def setUp(self):
        super().setUp()
        self.user = os.path.join(TMP, "r2user")
        self.home = os.path.join(self.user, ".config", "herdr-pa")
        self.spaces = os.path.join(self.home, "spaces")
        self.space = os.path.join(self.spaces, "sp1")
        self.evidence = os.path.join(self.space, "evidence")
        self.wt = os.path.join(self.user, "work", "wt")
        for d in [os.path.join(self.evidence, "t-x"), os.path.join(self.wt, ".git"), os.path.join(self.wt, "src"),
                  os.path.join(self.wt, ".claude")]:
            os.makedirs(d, exist_ok=True)
        for f in [os.path.join(self.wt, "src", "a.py"), os.path.join(self.wt, "b.py"), os.path.join(self.space, "ledger.json"),
                  os.path.join(self.home, "config.json"), os.path.join(self.wt, ".claude", "pa-task.json")]:
            open(f, "a").close()
        patcher = mock.patch.dict(os.environ, {"HOME": self.user, "HERDR_PA_HOME": self.home})
        patcher.start()
        self.addCleanup(patcher.stop)

    def r2(self, line, cwd=None):
        return self.check(line, cwd=cwd or self.wt)

    def fill(self, line):
        # {name} placeholders only; other braces are shell text
        for name in ("user", "home", "spaces", "space", "evidence", "wt"):
            line = line.replace("{" + name + "}", getattr(self, name))
        return line

    def assert_allowed(self, lines):
        for line in lines:
            line = self.fill(line)
            with self.subTest(line=line):
                self.assertIsNone(self.r2(line), line)


class R2b_TaskRecord(Round2):
    """(e) REVIEW-3 M3: the editor rule protects the task record and the hook settings."""

    def test_editor(self):
        for path in [os.path.join(self.wt, ".claude", "pa-task.json"), os.path.join(self.wt, ".claude", "settings.local.json"),
                     os.path.join(self.wt, ".claude", "PA-TASK.json")]:
            with self.subTest(path=path):
                self.assertIsNotNone(guard.check_edit(path, self.spaces, cwd=self.wt, home=self.home))
        self.assertIsNone(guard.check_edit(os.path.join(self.wt, "src", "a.py"), self.spaces, cwd=self.wt, home=self.home))


VARIABLE_WORDS = ["sets a git or gh variable", "report", "Project Agent", "There is no override"]


class R2c_GitVariables(Round2):
    """(c) REVIEW-3 I2, REVIEW-4 M3: a command that sets GIT_* or GH_* is refused."""

    def test_refused(self):
        for line in ["export GIT_CONFIG_COUNT=1 && export GIT_CONFIG_KEY_0=core.fsmonitor && export GIT_CONFIG_VALUE_0=x && git status",
                     "export GIT_DIR=/tmp/x && git status", "GIT_SSH_COMMAND=x make", "env GIT_DIR=x make",
                     "GH_REPO=a/b gh pr view 1", "export gh_token=x", "declare -x GIT_WORK_TREE=/tmp", "typeset GIT_PAGER=cat",
                     "local GIT_X=1", "readonly GH_HOST=x", "set GIT_X=1", "export GIT_DIR", "export -n Git_Dir",
                     "if true; then GIT_DIR=x git status; fi", "GIT_DIR=x; git status", "GIT_X+=y make",
                     "export GIT_PAGER='git push origin HEAD'; git log", "export GIT_DIR=$(mktemp -d) && git status",
                     "GIT_EDITOR=vi make > out", "builtin export GIT_DIR=x"]:
            with self.subTest(line=line):
                message = self.r2(line)
                self.assertIsNotNone(message, line)
                for words in VARIABLE_WORDS:
                    self.assertIn(words, message)

    def test_values_naming_git_involve_git(self):
        for line in ["export PAGER='git push origin HEAD'", "EDITOR='gh pr create' make", "export LESSOPEN='|hc run x'"]:
            with self.subTest(line=line):
                self.assertIsNotNone(self.r2(line), line)

    def test_allowed(self):
        self.assert_allowed(["export FOO=1 && make", "env FOO=bar make", "export GITHUB_WORKSPACE_X=1", "make CC=gcc",
                             "git commit -m 'GIT_DIR=x is bad'", "echo GIT_DIR > /tmp/x", "python3 x.py --tool=git"])


class R2e_DashedAndWrappers(Round2):
    """(e) REVIEW-3 M4, VERIFY-2 F2: git-<cmd> programs, csh, tcsh, fish, watch and env -S."""

    def test_refused(self):
        for line in ["git-push origin x", "/Library/Developer/CommandLineTools/usr/libexec/git-core/git-push origin HEAD",
                     "git-lfs push origin x", "xargs git-push", "GIT-PUSH origin x", "csh -c 'git push'", "tcsh -c 'git push'",
                     "/bin/tcsh -c 'git push'", "echo 'git push' | csh", "env -S 'git push origin x'", "env -S'git push'",
                     "env --split-string='git push'", "fish -c 'git push'", "watch 'git push'", "git-push origin x > /tmp/o"]:
            with self.subTest(line=line):
                self.assertIsNotNone(self.r2(line), line)

    def test_allowed(self):
        self.assert_allowed(["cat docs/git-guide.md", "env | sort", "ls -la"])


class R2e_NeverAdd(AC8ProfileRows):
    """(e) REVIEW-3 M1: more rows can never be added."""

    def test_more_rows_are_ignored(self):
        for command in [["git", "subtree"], ["git", "subtree", "push"], ["git", "credential-osxkeychain"],
                        ["git", "credential-store"], ["git", "credential-cache"], ["git", "Credential-Store"],
                        ["git", "maintenance"], ["git", "lfs", "install"], ["git", "lfs", "uninstall"], ["git", "lfs", "lock"],
                        ["git", "lfs", "unlock"], ["git", "receive-pack"], ["git", "upload-pack"], ["git", "shell"],
                        ["git", "cvsserver"], ["hc", "run"], ["hc", "kill"], ["hc", "rm"], ["hc", "interrupt"]]:
            with self.subTest(command=command):
                rows, note = self.rows([{"command": command, "args": "any"}])
                self.assertEqual(rows, [])
                self.assertIn(" ".join(command), note)

    def test_lfs_pull_still_added(self):
        rows, note = self.rows([{"command": ["git", "lfs", "pull"], "args": "any"}])
        self.assertEqual((len(rows), note), (1, ""))


class R2_Hook(unittest.TestCase):
    """(d) REVIEW-4 M1, (e) REVIEW-3 M5, REVIEW-3 N3 / REVIEW-4 N4: through pa_hooks.hook_guard."""

    @classmethod
    def setUpClass(cls):
        HookGuard.setUpClass.__func__(cls)
        cls.elsewhere = os.path.join(TMP, "r2-elsewhere")
        os.makedirs(cls.elsewhere, exist_ok=True)
        open(os.path.join(cls.wt, "only-in-root.py"), "a").close()

    def call(self, tool, tool_input, cwd, env=None):
        err = io.StringIO()
        data = {"tool_name": tool, "tool_input": tool_input, "cwd": cwd}
        with mock.patch.dict(os.environ, dict({"HC_TERMINAL_ID": OWN}, **(env or {}))), contextlib.redirect_stderr(err):
            try:
                code = pa_hooks.hook_guard(data, [])
            except SystemExit as e:
                code = e.code
        return code, err.getvalue()

    def test_unavailable_message(self):
        for words in GUARD_UNAVAILABLE_WORDS:
            self.assertIn(words, pa_hooks.GUARD_UNAVAILABLE)

    def test_malformed_input_is_refused(self):
        for data in [{}, {"tool_input": {"command": "git push"}}]:
            with self.subTest(data=data), contextlib.redirect_stderr(io.StringIO()) as err:
                with self.assertRaises(SystemExit) as caught:
                    pa_hooks.hook_guard(data, [])
                self.assertEqual(caught.exception.code, 2)
                self.assertIn("guard unavailable", err.getvalue())

    def test_the_hook_does_not_assume_its_folder(self):  # REVIEW-3 M5
        env = {"CLAUDE_PROJECT_DIR": self.wt}
        # the reported folder has no task record, but the project folder has: the Project Agent's address still applies
        self.assertEqual(self.call("Bash", {"command": "hc send w9 'done'"}, self.elsewhere, env)[0], 2)
        self.assertEqual(self.call("Bash", {"command": "hc send pa1 'done'"}, self.elsewhere, env)[0], 0)
        # a file in the project folder is not checked out over, whichever folder the hook was given
        self.assertEqual(self.call("Bash", {"command": "git checkout only-in-root.py"}, self.elsewhere, env)[0], 2)
        self.assertEqual(self.call("Bash", {"command": "git checkout feature"}, self.elsewhere, env)[0], 0)

    def test_task_record_through_the_hook(self):
        record = os.path.join(self.wt, ".claude", "pa-task.json")
        self.assertEqual(self.call("Write", {"file_path": record}, self.wt)[0], 2)
        self.assertEqual(self.call("Read", {"file_path": record}, self.wt)[0], 0)


# ---- Split (card d-048): the Bash path rule goes to a later task; what stays is tightened -----------------
# Finding ids: VERIFY-3 Fn, REVIEW-5 In/Mn/Nn, REVIEW-6 In/Mn/Nn.

VARIABLE = ("Refused by the Project Agent's allowlist: {} sets a git or gh variable (GIT_*, GH_*), which changes what the "
            "allowed git and gh commands do. Run the command without it. If it is needed, stop and report this to the "
            "Project Agent with hc send. There is no override.")


class Split(GuardTest):
    """A user home (HOME patched) with a worktree in it."""

    def setUp(self):
        super().setUp()
        self.user = tempfile.mkdtemp(dir=TMP)
        self.wt = os.path.join(self.user, "work", "wt")
        os.makedirs(os.path.join(self.wt, "sub"))
        for f in [os.path.join(self.wt, "a.py"), os.path.join(self.user, "a.py"), os.path.join(self.user, "sub-a.py")]:
            open(f, "a").close()
        patcher = mock.patch.dict(os.environ, {"HOME": self.user})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("CDPATH", "XDG_CONFIG_HOME"):  # put back by the patcher
            os.environ.pop(name, None)

    def at(self, line, cwd=None):
        return self.check(line, cwd=cwd or self.wt)


class Split_PersonalSettings(Split):
    """(2) decided with d-048: the editor rule protects the person's own git and gh settings."""

    def setUp(self):
        super().setUp()
        self.spaces = os.path.join(TMP, "split-spaces")
        for d in [os.path.join(self.user, ".config", "git"), os.path.join(self.user, ".config", "gh"),
                  os.path.join(self.user, "real-gh")]:
            os.makedirs(d, exist_ok=True)
        link = os.path.join(self.wt, "settings-link")
        if not os.path.lexists(link):
            os.symlink(os.path.join(self.user, ".gitconfig"), link)

    def edit(self, path, cwd=None):
        return guard.check_edit(path, self.spaces, cwd=cwd or self.wt, home=os.path.join(self.user, "pa"))

    def test_refused(self):
        u = self.user
        for path, cwd in [(os.path.join(u, ".gitconfig"), None), ("~/.gitconfig", None), (".gitconfig", u),
                          ("../../.gitconfig", None), (os.path.join(u, ".GITCONFIG"), None),
                          (os.path.join(u, ".config", "git", "config"), None), (os.path.join(u, ".config", "git", "ignore"), None),
                          (os.path.join(u, ".config", "git"), None), ("~/.config/gh/config.yml", None),
                          (os.path.join(u, ".config", "gh", "hosts.yml"), None), (os.path.join(u, ".config", "GH", "config.yml"), None),
                          (os.path.join(self.wt, "settings-link"), None)]:
            with self.subTest(path=path):
                self.assertEqual(self.edit(path, cwd), OFF_TABLE.format(path))

    def test_through_a_linked_folder(self):
        link = os.path.join(self.wt, "ghlink")
        if not os.path.lexists(link):
            os.symlink(os.path.join(self.user, ".config", "gh"), link)
        self.assertIsNotNone(self.edit(os.path.join(link, "config.yml")))

    def test_xdg_config_home(self):
        xdg = os.path.join(self.user, "xdg")
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": xdg}):
            self.assertIsNotNone(self.edit(os.path.join(xdg, "git", "config")))
            self.assertIsNotNone(self.edit(os.path.join(xdg, "gh", "config.yml")))
            self.assertIsNotNone(self.edit(os.path.join(self.user, ".config", "git", "config")))

    def test_allowed(self):
        u = self.user
        for path in [os.path.join(u, ".gitconfig.bak"), os.path.join(u, ".gitconfig-notes.md"),
                     os.path.join(u, ".config", "github-notes.md"), os.path.join(u, ".config", "ghx", "a"),
                     os.path.join(u, ".config", "other", "git", "config"), os.path.join(self.wt, ".gitconfig"),
                     os.path.join(self.wt, "a.py"), os.path.join(self.wt, ".github", "workflows", "ci.yml")]:
            with self.subTest(path=path):
                self.assertIsNone(self.edit(path))

    def test_through_the_hook(self):
        HookGuard.setUpClass.__func__(self)
        for tool, key in [("Edit", "file_path"), ("Write", "file_path"), ("MultiEdit", "file_path"), ("NotebookEdit", "notebook_path")]:
            with self.subTest(tool=tool):
                self.assertEqual(HookGuard.run_hook(self, tool, {key: os.path.join(self.user, ".gitconfig")})[0], 2)
                self.assertEqual(HookGuard.run_hook(self, tool, {key: os.path.join(self.user, ".config", "gh", "config.yml")})[0], 2)
        self.assertEqual(HookGuard.run_hook(self, "Read", {"file_path": os.path.join(self.user, ".gitconfig")})[0], 0)


class Split_HiddenNames(Split):
    """VERIFY-3 F3, REVIEW-5 M2: a not-plain line that spells git, gh or hc through $'...', $'' or an expansion that may be
    empty involves them."""

    def test_refused_not_plain(self):
        for line in ["gi$''t push origin HEAD > /dev/null", "$'\\x67it' push origin HEAD > /dev/null", "gi$'t' push origin HEAD",
                     "$'\\147it' push > /dev/null", "$'\\u0067it' push > /dev/null", "g$()it push origin HEAD", "gi${NOPE}t push",
                     "h$'c' send x y > /dev/null", "g$\"\"h pr create > /dev/null", "gi$NOPE''t push > /dev/null"]:
            with self.subTest(line=line):
                self.assertEqual(self.at(line), NOT_PLAIN)

    def test_ordinary_not_plain_lines_stay_allowed(self):
        for line in ["make test > build.log 2>&1", "echo $HOME > /tmp/x", "for f in *.py; do echo $f; done",
                     "python3 -c 'print(1)' | tee out.txt", "echo ${PATH} > /tmp/p", "printf $'a\\tb\\n' > /tmp/t"]:
            with self.subTest(line=line):
                self.assertIsNone(self.at(line))


class Split_PipeIntoShell(Split):
    """REVIEW-5 M1: what an allowed row prints is not run by a shell in the same line."""

    def test_refused(self):
        for line, shown_part in [("git log -1 --format=%s | bash", "bash"), ("git show HEAD:x.sh | bash", "bash"),
                                 ("git show HEAD:x.sh | sh -s", "sh -s"), ("git diff | grep x | zsh", "zsh"),
                                 ("git log --format=%s | xargs sh -c", "xargs sh -c"), ("gh pr view 1 | bash", "bash"),
                                 ("hc read w1 | sh", "sh"), ("git status && echo x | sh", "sh")]:
            with self.subTest(line=line):
                self.assertEqual(self.at(line), OFF_TABLE.format(shown_part))

    def test_allowed(self):
        for line in ["git log | head", "git diff | grep x | wc -l", "echo hi | bash", "cat x.sh | sh", "git status && bash tests/run-all.sh",
                     "git log --oneline | tee log.txt"]:
            with self.subTest(line=line):
                self.assertIsNone(self.at(line))


class Split_Reflog(Split):
    """REVIEW-5 N1: git reflog with options first does not take a subcommand word after them."""

    def test_refused(self):
        for line in ["git reflog -n 5 expire --all", "git reflog -n 5 delete HEAD", "git reflog --all drop",
                     "git reflog -n 1 exists HEAD"]:
            with self.subTest(line=line):
                self.assertEqual(self.at(line), OFF_TABLE.format(line))

    def test_allowed(self):
        for line in ["git reflog -n 5", "git reflog --date=iso", "git reflog show HEAD", "git reflog -n 5 HEAD"]:
            with self.subTest(line=line):
                self.assertIsNone(self.at(line))


class Split_CdTracking(Split):
    """VERIFY-3 F5 and F4 (CDPATH), for the folder git checkout and git restore are checked in: a cd whose target the
    guard cannot work out leaves the folder unknown."""

    def test_tilde_forms_the_shell_expands_differently(self):
        for line in ["cd ~- && git checkout a.py", "cd ~+ && git checkout a.py", "cd ~-/sub && git checkout a.py",
                     "cd ~+ && git restore a.py", "cd ~nosuchuser-x && git checkout x"]:
            with self.subTest(line=line):
                self.assertIsNotNone(self.at(line, cwd=os.path.join(self.wt, "sub")))

    def test_cdpath(self):
        for line in ["export CDPATH=" + self.user + " && cd work && git checkout feature",
                     "export CDPATH=" + self.user + " && cd sub && git restore a.py"]:
            with self.subTest(line=line):
                self.assertIsNotNone(self.at(line, cwd=self.user))
        with mock.patch.dict(os.environ, {"CDPATH": self.user}):
            self.assertIsNotNone(self.at("cd sub && git checkout a.py"))
            self.assertIsNone(self.at("cd ./sub && git checkout feature"))
            self.assertIsNone(self.at("cd " + os.path.join(self.wt, "sub") + " && git checkout feature"))

    def test_still_known(self):
        for line in ["cd ~ && git checkout feature", "cd sub && git checkout feature", "cd ./sub && git restore b.py",
                     "cd ~/work/wt && git restore a.py"]:
            with self.subTest(line=line):
                self.assertIsNone(self.at(line))
        self.assertIsNotNone(self.at("cd ~ && git checkout a.py"))  # a.py exists in ~: a file restore


class R2b_UnknownFolderAndQuotes(Round2):
    """Put back (VERIFY-4 V4-1, REVIEW-7 M2): the cd tracking for git checkout, which the split keeps."""

    def test_known_cd_then_or_makes_the_folder_unknown(self):  # VERIFY-2 F3 (mutation M31)
        self.assertIsNotNone(self.r2("cd src && git status || git checkout feature"))
        self.assertIsNotNone(self.r2("cd src && git status ; git checkout feature"))
        self.assertIsNone(self.r2("cd src && git status && git checkout feature"))


class SplitFix_WrapperVariables(Split):
    """REVIEW-7 I1: R12 applies inside an eval or shell -c text too, as it did at ca7adf8."""

    def test_refused(self):
        for line, word in [("eval 'export GIT_DIR=/tmp/x' && git status", "GIT_DIR=/tmp/x"),
                           ("eval 'export GIT_CONFIG_GLOBAL=/tmp/x'; git status", "GIT_CONFIG_GLOBAL=/tmp/x"),
                           ("eval 'export GIT_SSH_COMMAND=x' && git fetch origin", "GIT_SSH_COMMAND=x"),
                           ("eval 'export GIT_WORK_TREE=/tmp/o GIT_DIR=/tmp/o/.git'", "GIT_WORK_TREE=/tmp/o"),
                           ("eval 'export GIT_DIR' && git status", "GIT_DIR"),
                           ("bash -c 'export GH_TOKEN=x; make'", "GH_TOKEN=x"),
                           ("env -S 'FOO=1 GIT_DIR=x make'", "GIT_DIR=x")]:
            with self.subTest(line=line):
                self.assertEqual(self.at(line), VARIABLE.format(word))

    def test_allowed(self):
        for line in ["eval 'make'", "sh -c 'make test'", "bash -c 'export FOO=1; make'", "eval 'echo hi' && git status"]:
            with self.subTest(line=line):
                self.assertIsNone(self.at(line))


class SplitFix_HiddenNames(Split):
    """REVIEW-7 I2: a program word built from an expansion or substitution whose text spells git, gh or hc."""

    def test_refused_not_plain(self):
        for line in ["${X:-git} push origin HEAD > /dev/null", "${X-git} push > /dev/null", "gi`:`t push > /dev/null",
                     "gi$(:)t push > /dev/null", "gi$(true)t push > /dev/null", "${X:+gh} pr create > /dev/null",
                     "${X:=hc} send a b > /dev/null", "${X=git} push > /dev/null", "${X:-gi}t push > /dev/null",
                     "gi$(echo $(true))t push > /dev/null"]:
            with self.subTest(line=line):
                self.assertEqual(self.at(line), NOT_PLAIN)

    def test_inside_a_wrapper_text(self):
        for line in ["sh -c 'gi$(:)t push'", "eval '${X:-git} push'"]:
            with self.subTest(line=line):
                self.assertIsNotNone(self.at(line))

    def test_ordinary_not_plain_lines_stay_allowed(self):
        for line in ["echo ${X:-default} > /tmp/x", "echo $(date) > /tmp/d", "echo `date` > /tmp/d", "make -j$(nproc) > log",
                     "echo ${HOME:-/tmp}/x > /tmp/y", "sh -c 'make test'"]:
            with self.subTest(line=line):
                self.assertIsNone(self.at(line))


class SplitFix_PipeIntoShell(Split):
    """REVIEW-7 Minor (M1): only the program word after the pipe counts; source and . read the pipe too."""

    def test_allowed(self):
        for line in ["git log | grep bash", "git ls-files | grep -c watch", "git log | grep -w sh", "git diff | grep eval",
                     "git log --oneline | grep -e source"]:
            with self.subTest(line=line):
                self.assertIsNone(self.at(line))

    def test_refused(self):
        for line, shown_part in [("git show HEAD:x.sh | source /dev/stdin", "source /dev/stdin"),
                                 ("git show HEAD:x.sh | . /dev/stdin", ". /dev/stdin"),
                                 ("git show HEAD:x.sh | env bash", "env bash"),
                                 ("git show HEAD:x.sh | exec sh", "exec sh"),
                                 ("git show HEAD:x.sh | xargs -0 bash -c", "xargs -0 bash -c"),
                                 ("git show HEAD:x.sh | /bin/bash", "/bin/bash")]:
            with self.subTest(line=line):
                self.assertEqual(self.at(line), OFF_TABLE.format(shown_part))


class PipeFix_PipeIntoShell(Split):
    """VERIFY-5 V5-1, REVIEW-8 I-1, I-2, M2: after a |, a known text filter is allowed whatever its arguments say; a
    launcher is judged by the program it starts; for any other program a later word naming a shell refuses the line,
    as at b43e3e5. source and . count only as the program word or the launched program word."""

    def test_shell_after_a_program_the_guard_does_not_know(self):
        for part in ["caffeinate sh", "caffeinate -i bash", "FOO=1 caffeinate sh", "setsid bash",
                     "arch -arm64 bash", "ionice -c3 sh", "doas sh", "script -q /dev/null sh", "busybox sh",
                     "chrt -b 0 zsh", "unbuffer -p bash"]:
            with self.subTest(part=part):
                self.assertEqual(self.at("git show HEAD:x.sh | " + part), OFF_TABLE.format(part))

    def test_launchers_judge_the_program_they_start(self):
        for part in ["nohup bash", "time bash", "timeout 5 bash", "nice -n 10 sh", "env -i PATH=/bin bash",
                     "xargs -I% sh -c x", "nohup caffeinate sh", "command . /dev/stdin", "builtin source /dev/stdin",
                     "time source /dev/stdin"]:
            with self.subTest(part=part):
                self.assertEqual(self.at("git show HEAD:x.sh | " + part), OFF_TABLE.format(part))

    def test_filters_whatever_their_arguments(self):
        for line in ["git log | grep bash", "git ls-files | xargs ls -d .", "git ls-files | xargs grep -l source",
                     "git ls-files | xargs -0 grep -l bash", "git log | rg -w sh", "git log | head -n 3 sh",
                     "git log | tail -n 2 bash", "git ls-files | wc -l bash", "git log | sort -k 2 zsh",
                     "git log | uniq -c sh", "git log | cut -d ' ' -f 1 bash", "git log | tr a-z A-Z",
                     "git log | tee bash.log sh", "git show HEAD:x.sh | cat - bash", "git log | less bash",
                     "git log --format=%s | jq -R .", "git log | column -t sh", "git ls-files | grep -l source",
                     "git log | nohup grep sh", "git log | timeout 5 grep bash",
                     "git log | timeout -k 5 10s grep bash", "git log | nice -n 10 grep sh", "git log | xargs -n 1 grep sh", "git ls-files | env LC_ALL=C sort .",
                     "git log | time wc -l", "git ls-files | caffeinate ls -d ."]:
            with self.subTest(line=line):
                self.assertIsNone(self.at(line))

    def test_reserved_word_before_a_shell(self):
        self.assertEqual(self.at("git show HEAD:x.sh | ! bash"), OFF_TABLE.format("'!' bash"))

    def test_option_value_named_like_a_filter(self):
        self.assertEqual(self.at("git show HEAD:x.sh | xargs -I grep bash -c x"), OFF_TABLE.format("xargs -I grep bash -c x"))

    def test_assignment_before_a_shell(self):
        self.assertEqual(self.at("git show HEAD:x.sh | FOO=1 bash"), OFF_TABLE.format("FOO=1 bash"))


class PipeFix_WrapperTexts(Split):
    """VERIFY-5 V5-6 (a), (b): the env -S prefix in a wrapped word, and message texts in a wrapped line."""

    def test_env_split_prefix_joined(self):
        self.assertEqual(self.at("env -SGIT_DIR=x make"), VARIABLE.format("GIT_DIR=x"))

    def test_message_in_a_wrapped_line(self):
        self.assertIsNone(self.at("bash -c 'make' && git commit -m 'set GIT_DIR=x later; ok'"))


class Split_Texts(GuardTest):
    """VERIFY-3 N1: the R12 text is pinned."""

    def test_variable_text(self):
        self.assertEqual(guard.VARIABLE, VARIABLE)
        self.assertEqual(self.check("export GIT_DIR=x"), VARIABLE.format("GIT_DIR=x"))


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
