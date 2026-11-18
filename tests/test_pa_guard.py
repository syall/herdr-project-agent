"""Unit tests for the worker guard (t-025: a short denylist; before it t-007 part 7a and t-016):
runtime/tools/pa_guard.py and its call from `pa-hook guard`.

Run: python3 -m unittest -v tests/test_pa_guard.py
The command lines below are data: they are only handed to the check functions, never run. No hc calls
and no real homes: HERDR_PA_HOME, CLAUDE_CONFIG_DIR and HERDR_APP_HOME point at a temp folder while
pa.py and pa_hooks.py are loaded, and every space, profile and repository is made under that folder.
The rules removed by t-025 (the allowlist, plain lines, the Bash write rule) are described in
docs/worker-guard-history.md; their tests are in this file as it was at af47aa1.
"""
import ast
import contextlib
import importlib.util
import inspect
import io
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.join(HERE, "..")
TOOLS = os.path.join(REPO, "runtime", "tools")
TMP = os.path.realpath(tempfile.mkdtemp(prefix="t025-guard-"))

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

# The module under test, loaded by path. If it does not load, every test fails on its own (setUp), not as one import error.
try:
    _spec = importlib.util.spec_from_file_location("pa_guard_under_test", os.path.join(TOOLS, "pa_guard.py"))
    guard = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(guard)
except (OSError, ImportError) as e:
    guard, GUARD_ERROR = None, e

PA = "pa1"       # the Project Agent's address (agent.json "terminal")
OWN = "w1"       # this worker's own address (HC_TERMINAL_ID)

# ---- The messages (spec section 3 and 3.5) ----------------------------------------------------------------------
TEXT_HINT = ("If this is text, not a command (a heredoc line or an unquoted argument), quote it or write it with the "
             "editor tool.")
D1 = ("Refused by the Project Agent's guard: {} pushes to a remote. Agents never push (worker.md, Safety): commit your "
      "work and say in your report that it is ready; the person pushes. " + TEXT_HINT + " There is no override.")
D2 = ("Refused by the Project Agent's guard: {} publishes or merges a change request. That is the person's decision "
      "(worker.md, Safety): say in your report that the work is ready, and the Project Agent asks them on a decision "
      "card. " + TEXT_HINT + " There is no override.")
D3 = ("Refused by the Project Agent's guard: {} deletes a branch or a worktree. Deleting is the person's decision "
      "(worker.md, Safety): leave it, and name it in your report if it should go. " + TEXT_HINT + " There is no override.")
D4 = ("Refused by the Project Agent's guard: {}. pa-push-main and pa-push-allow are the person's commands: a main branch "
      "is pushed only when the person answers a push card in the panel. Say in your report that the work is ready to "
      "release. There is no override.")
EDIT = ("Refused by the Project Agent's guard: {} is inside a .git folder, the Project Agent's files or the person's git "
        "and gh settings, which workers do not edit. Write reports, logs and notes in your task's evidence folder "
        "(evidence/<task-id>/ in the Project Agent's state folder). If the edit is needed, stop and report it to the "
        "Project Agent with hc send. There is no override.")


def shown(inner):
    """{command}: the matching simple command's words, shlex-quoted and joined by blanks, cut to 300 characters."""
    text = " ".join(shlex.quote(w) for w in inner.split(" "))
    return text if len(text) <= 300 else text[:297] + "..."


def msg(row, inner):
    return row.format(shown(inner))


# ---- Table 3: the forms each row must catch. (line, the inner simple command, as words joined by blanks) ----------
# The inner command is the line itself unless given.
D1_FORMS = [
    ("git push",), ("git push origin t-1",), ("git push -f",), ("git push --force",), ("git push --force-with-lease",),
    ("git push origin +t-1",), ("git push --mirror",), ("git push --all",), ("git push --tags",),
    ("git push --delete x",), ("git push origin :x",), ("git push -o ci.skip",), ("git -C ../wt push",),
    ("git -c push.default=current push",), ("git --no-pager push",), ("git --git-dir=x push",), ("/usr/bin/git push",),
    ("GIT push",), ('"git" push', "git push"), ("g'i't push", "git push"), ("\\git push", "git push"),
    ("GIT_SSH_COMMAND=ssh git push",), ("env X=1 git push",), ("sudo -u me git push",), ("timeout 60 git push",),
    ("echo x | xargs git push", "xargs git push"), ("cd wt && git push", "git push"), ("(cd wt; git push)", "git push"),
    ("{ git push; } | tee log", "{ git push"), ("for b in a c; do git push origin $b; done", "do git push origin $b"),
    ("git push > log 2>&1", "git push"), ("echo $(git push 2>&1)", "git push"), ("echo `git push`", "git push"),
    ('x="$(git push)"', "git push"), ("sh -c 'git push'", "git push"), ('bash -lc "cd wt && git push"', "git push"),
    ('eval "git push"', "git push"), ("bash <<EOF\ngit push\nEOF", "git push"), ("git subtree push --prefix d o b",),
    ("git lfs push origin b",), ("git-push origin b",), ("/usr/libexec/git-core/git-send-pack x",),
]
D2_FORMS = [
    ("gh pr create --fill",), ("gh pr new",), ("gh pr ready 12",), ("gh pr ready 12 --undo",),
    ("gh pr merge 12 --squash --delete-branch",), ("gh pr --repo o/r merge 5",), ("gh pr -R o/r create",),
    ("/opt/homebrew/bin/gh pr create",), ("GH_TOKEN=x gh pr create",), ("cd wt && gh pr create", "gh pr create"),
    ("sh -c 'gh pr merge 3'", "gh pr merge 3"), ("$(gh pr create --fill)", "gh pr create --fill"),
]
D3_FORMS = [
    ("git branch -d t-1",), ("git branch -D t-1",), ("git branch --delete t-1",), ("git branch -dr origin/t-1",),
    ("git branch -Df t-1",), ("git -C wt branch -D t-1",), ("cd wt && git branch -d x", "git branch -d x"),
    ("git worktree remove ../wt",), ("git worktree remove --force ../wt",), ("git -C main worktree remove wt",),
    ("git update-ref -d refs/heads/t-1",), ("sh -c 'git branch -D x'", "git branch -D x"),
]
D4_FORMS = [
    ("pa-push-main run d-1 --answer Push --space s",), ("pa-push-main check d-1",), ("pa-push-main plan t-1 --repo r",),
    ("pa-push-allow add --path p --space s",), ("pa-push-allow show",), ("~/.local/bin/pa-push-main run x",),
    ("PA-PUSH-MAIN run",), ("./runtime/tools/pa_push.py run",), ("python3 runtime/tools/pa_push.py run",),
    ("python3 -I runtime/tools/pa_push.py show",), ("cd wt && pa-push-main run x", "pa-push-main run x"),
    ("env X=1 pa-push-allow on",), ("sudo pa-push-main",), ("nohup pa-push-main run x",),
    ("sh -c 'pa-push-main run x'", "pa-push-main run x"), ("y=$(pa-push-allow show)", "pa-push-allow show"),
    ("if pa-push-main check x; then :; fi", "if pa-push-main check x"),
]
ROWS = [("D1", D1, D1_FORMS), ("D2", D2, D2_FORMS), ("D3", D3, D3_FORMS), ("D4", D4, D4_FORMS)]
# t-037 J2: pa-restart is the person's (or the Project Agent's at their word), never a worker's.
D5 = ("Refused by the Project Agent's guard: {}. pa-restart is the person's command, or the Project Agent's at "
      "their word: a worker never restarts a Project Agent. Say in your report what should be restarted and why. "
      "There is no override.")
D5_FORMS = [
    ("pa-restart --space s",), ("pa-restart",), ("~/.local/bin/pa-restart --now",), ("PA-RESTART",),
    ("python3 ~/.config/herdr-pa/tools/pa.py restart --space s",), ("python3 -I runtime/tools/pa.py restart",),
    ("./runtime/tools/pa.py restart --dry-run",), ("env X=1 pa-restart",), ("cd x && pa-restart", "pa-restart"),
    ("sh -c 'pa-restart --now'", "pa-restart --now"), ("y=$(pa-restart --dry-run)", "pa-restart --dry-run"),
    ("pa-restart --space s --end-terminal --anyway",),  # t-037 J3
]
D5_ALLOWED = ["grep -n pa-restart README.md", "python3 runtime/tools/pa.py peers", "pa-peers --older",
              "echo pa-restart", "man pa-restart-notes"]
# t-041 J3: pa-context (set, and J4's fresh) is the person's; its hook subcommands run in the Project Agent's
# session, never through a worker's Bash, so every pa-context subcommand is refused to a worker.
D6 = ("Refused by the Project Agent's guard: {}. pa-context is the person's command (its other subcommands are the "
      "Project Agent's own hooks): a worker never changes a Project Agent's context settings. Say in your report what "
      "should change and why. There is no override.")
D6_FORMS = [
    ("pa-context set --mode off",), ("pa-context",), ("~/.local/bin/pa-context compacted --space s",), ("PA-CONTEXT start-pack",),
    ("python3 ~/.config/herdr-pa/tools/pa.py context set --net 500k",), ("python3 -I runtime/tools/pa.py context fresh",),
    ("pa-context fresh --why x",), ("pa-context fresh --space s --withdraw",),  # t-041 J4 (W1)
    ("python3 runtime/tools/pa.py context followup --space s",),
    ("./runtime/tools/pa.py context set",), ("env X=1 pa-context set",), ("cd x && pa-context set", "pa-context set"),
    ("sh -c 'pa-context set --mode off'", "pa-context set --mode off"), ("y=$(pa-context set)", "pa-context set"),
]
D6_ALLOWED = ["grep -n pa-context README.md", "python3 runtime/tools/pa.py doctor", "pa-costs report --by context",
              "echo pa-context", "man pa-context-notes"]

# ---- Table 4.1: must be allowed ----------------------------------------------------------------------------------
ALLOWED = {
    "reset": ["git reset HEAD~1", "git reset --soft HEAD~3", "git reset --mixed main", "git reset --keep HEAD~1",
              "git reset --merge", "git reset -- a.py", "git reset -q HEAD a.py", "git -C wt reset --soft main"],
    "squash": ["git merge --squash t-1", 'git merge --squash t-1 && git commit -m "squash t-1"',
               "git reset --soft main && git commit -m one", "git commit --fixup HEAD~1",
               "GIT_SEQUENCE_EDITOR=: git rebase -i --autosquash main"],
    "rebase": ["git rebase main", "git rebase --onto main a b", "git rebase -i main", "git rebase --autosquash -i main",
               'git rebase -x "make test" main', "git rebase --continue", "GIT_EDITOR=true git rebase --continue",
               "git rebase --abort", "git -C wt rebase main"],
    "stash": ["git stash", "git stash push -m wip -- a.py", "git stash -u", "git stash pop", "git stash apply stash@{1}",
              "git stash list", "git stash show -p", "git stash branch t-2"],
    "redirects": ["git diff main > /tmp/x.diff", "git log --oneline > notes.txt 2>&1", "git diff >> d.txt",
                  "git show HEAD:f > f.old", "git status 2>/dev/null", "hooks/tests.sh affected > build.log 2>&1",
                  "echo x > .git/info/exclude"],
    "substitutions": ['git commit -m "$(cat msg.txt)"', "echo $(git rev-parse HEAD)", "B=`git branch --show-current`",
                      'test -n "$(git status --porcelain)"', 'for f in $(git diff --name-only); do wc -l "$f"; done'],
    "pipes": ["git log -5 | head", "git diff | tee d.txt", "git ls-files | xargs wc -l", "git status --porcelain | grep -c ."],
    "heredocs": ["cat > notes.md <<'EOF'\nthe guard refuses pa-push-main\nEOF", "git commit -F - <<EOF\nmsg\nEOF",
                 "python3 - <<EOF\nprint(1)\nEOF"],
    "-C": ["git -C ../wt commit -am x", "git -C wt log -1", "git -C wt add -A"],
    "-q": ['git commit -qm "x; y"', "git fetch -q origin", "git checkout -q main", "git pull -q --rebase origin main"],
    "compound": ["git add -A && git commit -m x && git log -1", "cd wt && git status; git diff | tee d.txt",
                 'for f in a b; do git add "$f"; done', "{ git status; git diff; } > s.txt", "(cd wt && git log -1)",
                 "git fetch && git rebase origin/main || git rebase --abort", "if git diff --quiet; then echo clean; fi"],
    "other git": ["git checkout -- a.py", "git restore src/", "git cherry-pick abc", "git revert abc", "git mv a b",
                  "git rm a", "git tag v1", "git config user.name x", "git remote -v", "git worktree add ../wt b",
                  "git worktree list", "git worktree prune", "git branch -m a b", "git branch -f x y",
                  "git commit --amend --no-edit", "git archive HEAD | tar t", "git hash-object a.py",
                  "git fetch origin main:main", "git merge main"],
    "text that names a row": ['grep -rn "git push" .', 'git commit -m "the guard refuses git push"',
                              "git log --grep push", "git help push", "man git-push", "grep -n pa-push-main README.md",
                              "git add runtime/tools/pa_push.py", "git show HEAD:runtime/tools/pa_push.py > /tmp/p.py",
                              "python3 -m unittest tests/test_pa_push.py", "echo \"gh pr merge is the person's\""],
    "gh, hc": ["gh pr view 5", "gh pr list", "gh pr checks", "gh pr diff 5", "gh issue list", "gh api repos/o/r/pulls",
               'hc send pa1 "done"', 'hc send anyone "x"', "hc read w1"],
}
# Table 4.2: allowed by the denylist, refused by the older safety hook (unchanged).
OLDER_ONLY = ["git reset --hard", "git reset --hard HEAD~1", "git clean -f", "git clean -fdx", "git checkout .",
              "git restore .", "git stash drop", "git stash clear", "gh release create v1", "npm publish",
              "terraform apply", "cat .env", "pa-costs billed fetch --now"]
# Spec section 3, "overlap": both refuse; through the hook the older message wins.
OVERLAP = [("git push --force origin t-x", D1, "a force push"),
           ("git push origin main", D1, "a push to the main branch"),
           ("git branch -D x", D3, "force-deleting a branch"),
           ("git worktree remove --force x", D3, "force-removing a worktree"),
           ("gh pr merge 1", D2, "merging a change request")]


def write_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(value, f)


class GuardTest(unittest.TestCase):
    def setUp(self):
        if guard is None:
            self.fail("runtime/tools/pa_guard.py does not load: {!r}".format(GUARD_ERROR))
        self.cwd = tempfile.mkdtemp(dir=TMP)

    def check(self, line):
        return guard.check_command(line)


class T025Deny(GuardTest):  # AC4.1: every form of table 3 gives exactly its row's message
    def assert_row(self, row, forms):
        for form in forms:
            line, inner = form[0], form[-1]
            with self.subTest(line=line):
                self.assertEqual(self.check(line), msg(row, inner))

    def test_d1_push(self):
        self.assert_row(D1, D1_FORMS)

    def test_d2_change_request(self):
        self.assert_row(D2, D2_FORMS)

    def test_d3_delete_branch_or_worktree(self):
        self.assert_row(D3, D3_FORMS)

    def test_d4_push_tools(self):
        self.assert_row(D4, D4_FORMS)

    def test_message_shape(self):
        for _name, row, forms in ROWS:
            with self.subTest(row=_name):
                text = msg(row, forms[0][-1])
                self.assertTrue(text.startswith("Refused by the Project Agent's guard: "))
                self.assertTrue(text.endswith("There is no override."))
                self.assertNotIn("allowlist", text)
                self.assertEqual(TEXT_HINT in text, row is not D4)

    def test_first_match_in_line_order(self):
        self.assertEqual(self.check("gh pr create && git push"), msg(D2, "gh pr create"))
        self.assertEqual(self.check("git status; git branch -d x; git push"), msg(D3, "git branch -d x"))

    def test_long_command_is_cut(self):
        line = "git push origin " + "b" * 400
        self.assertEqual(self.check(line), msg(D1, line))
        self.assertTrue(msg(D1, line).count("...") >= 1)


class T025Allowed(GuardTest):  # AC1.2, AC2.2, AC3.1, AC4.2
    def test_table_4_1(self):
        for group, lines in ALLOWED.items():
            for line in lines:
                with self.subTest(group=group, line=line):
                    self.assertIsNone(self.check(line))

    def test_table_4_2_is_left_to_the_older_hook(self):
        for line in OLDER_ONLY:
            with self.subTest(line=line):
                self.assertIsNone(self.check(line))

    def test_overlap_gives_the_row_alone(self):
        for line, row, _older in OVERLAP:
            with self.subTest(line=line):
                self.assertEqual(self.check(line), msg(row, line))

    def test_bash_writes_are_not_judged(self):  # AC3.1, limit L10 (accepted, Q3)
        for line in ["echo x > .git/HEAD", "cd - && echo x > notes.txt", "cp a ~/.config/herdr-pa/push-allow.json",
                     "tee .claude/settings.local.json < x"]:
            with self.subTest(line=line):
                self.assertIsNone(self.check(line))


class T025Limits(GuardTest):
    """Section 4.3: pinned with today's verdict, so that a change is deliberate."""

    def test_not_caught(self):
        for line, limit in [("git config alias.p push; git p", "L2"), ("git -c alias.x=push x", "L2"),
                            ("G=git; $G push", "L3"), ("${X:-git} push", "L3"), ("printf 'git push' | sh", "L5"),
                            ("ssh host 'git push'", "L5"), ("gh api -X PUT repos/o/r/pulls/1/merge", "L6"),
                            ("rm -rf ../wt", "L7"), ("git show HEAD:runtime/tools/pa_push.py | python3", "L8"),
                            ("sudo -u bob pa-push-main run x", "L4")]:
            with self.subTest(line=line, limit=limit):
                self.assertIsNone(self.check(line))

    def test_over_refusals(self):
        for line, inner in [("echo git push", "echo git push"), ("grep -rn git push .", "grep -rn git push ."),
                            ("cat > f <<EOF\nrun git push later\nEOF", "run git push later"),
                            ("echo '$(git push)'", "git push")]:
            with self.subTest(line=line):
                self.assertEqual(self.check(line), msg(D1, inner))


class T025Robust(GuardTest):  # AC4.3
    ODD = ["", " ", "\t\n", "'", '"', "$(", "`", "(", ")", "$(git push", "git push '", 'git "push', "\\", "a\\\nb",
           "x" * 10000, "git push\x00", "\x00", "gît push", "git push ünïcode", "日本 git status", "$'", "$'git push",
           "<<", ">", "&&", ";;", "|", "<(", "eval", "sh -c", "git", "gh pr", "git -C", "git branch", "$((1+2))"]

    def test_no_exception(self):
        for line in self.ODD:
            with self.subTest(line=line[:40]):
                result = self.check(line)
                self.assertTrue(result is None or isinstance(result, str), result)

    def test_time_bound(self):
        base = [f[0] for _n, _r, forms in ROWS for f in forms] + [l for ls in ALLOWED.values() for l in ls] + self.ODD
        padded = ["git status && " * 700, "echo " + "a " * 5000, "$(" * 3000 + "git push", "eval " * 2000 + "git push",
                  "sh -c " * 1500 + "'git push'", "'" * 10000, "`" * 10000, "a;" * 5000, "x=$(y) " * 1400,
                  "git -C " * 1400 + "push", "git branch " * 900 + "-d", "gh pr " * 1600 + "merge", "sh " * 3000 + "-c x",
                  "python3 " * 1200 + "pa_push.py", "$(echo " * 1400 + "git push" + ")" * 1400]
        lines = (base * 2)[:170] + padded * 2  # 200 lines, 30 of them about 10 kB
        self.assertEqual(len(lines), 200)
        for line in lines:
            start = time.perf_counter()
            self.check(line)
            spent = time.perf_counter() - start
            self.assertLess(spent, 0.05, line[:60])


REMOVED_BOTH = ["ALLOWLIST", "READS", "NEVER_ADD", "profile_rows", "guardAllow", "OFF_TABLE", "NOT_PLAIN", "VARIABLE",
                "git_variable", "judge_plain", "read_redirects", "check_writes", "file_targets", "folder_change", "operands",
                "WRITE", "FOLDER_UNKNOWN", "HOME_VAR", "REDIRECT"]  # AC1.1, AC3.1
REMOVED_GUARD = ["split_plain", "split_plain_ops", "involves", "involves_text", "person_text", "names_in_text", "wraps",
                 "runs_input", "filters_only", "WRAPPERS", "FILTERS", "PIPE_READERS", "MAY_BE_EMPTY", "DEFAULT_VALUE",
                 "ansi_c", "ANSI_ESCAPE", "ADDED_SHAPES", "cd_target", "changes_folder"]  # AC2.1, R1
EDITOR_RULE = {"forms", "protected", "personal_settings", "check_edit", "starts_of"}
FILE_CALLS = {"open", "stat", "lstat", "exists", "lexists", "isfile", "isdir", "islink", "listdir", "scandir", "walk",
              "realpath", "readlink"}


def source(name):
    with open(os.path.join(TOOLS, name), encoding="utf-8") as f:
        return f.read()


class T025Removed(unittest.TestCase):  # AC1.1, AC2.1, AC3.1, AC4.4, R10
    def test_names_absent(self):
        for name in ("pa_guard.py", "pa_hooks.py"):
            text = source(name)
            for word in REMOVED_BOTH + (REMOVED_GUARD if name == "pa_guard.py" else []):
                with self.subTest(file=name, word=word):
                    self.assertIsNone(re.search(r"(?<![\w])" + re.escape(word) + r"(?![\w])", text))
            self.assertNotIn("allowed(", text)

    def test_no_folder_tracking(self):
        defined = {n.name for n in ast.walk(ast.parse(source("pa_guard.py"))) if isinstance(n, ast.FunctionDef)}
        self.assertFalse(defined & {"folders", "cd_target", "changes_folder", "allowed", "shown_plain"}, defined)

    def test_stdlib_no_subprocess_no_file_access_in_the_denylist(self):
        tree = ast.parse(source("pa_guard.py"))
        imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                    for a in (n.names if isinstance(n, ast.Import) else [ast.alias(name=n.module or "", asname=None)])}
        self.assertNotIn("subprocess", imported)
        self.assertLessEqual(imported, {"os", "re", "shlex", "sys"})
        for fn in [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name not in EDITOR_RULE]:
            for call in [c for c in ast.walk(fn) if isinstance(c, ast.Call)]:
                name = call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", "")
                with self.subTest(function=fn.name, call=name):
                    self.assertNotIn(name, FILE_CALLS)

    def test_one_argument(self):
        self.assertEqual(list(inspect.signature(guard.check_command).parameters), ["line"])

    def test_size(self):  # R10: about 230 lines (the build came to 245: DECISIONS.md, t-025 D4; t-041 J3's pa-context row: 254)
        self.assertLessEqual(len(source("pa_guard.py").splitlines()), 255)


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
        for group in ("reset", "squash", "redirects", "substitutions", "heredocs", "-C", "-q", "compound", "gh, hc"):
            for line in ALLOWED[group]:
                with self.subTest(line=line):
                    self.assertEqual(self.bash(line), (0, ""))
        for _name, row, forms in ROWS:
            line, inner = forms[0][0], forms[0][-1]
            with self.subTest(line=line):
                code, err = self.bash(line)
                self.assertEqual((code, err.strip()), (2, msg(row, inner)))

    def test_bash_writes_run(self):  # R3: the hook calls no write check (L10, accepted, Q3)
        for line in ["echo x > .git/HEAD", "cd - && echo x > notes.txt", "echo x > .claude/settings.local.json",
                     "cp a {}".format(os.path.join(self.state, "ledger.json"))]:
            with self.subTest(line=line):
                self.assertEqual(self.bash(line), (0, ""))

    def test_hc_send_to_any_address(self):
        self.assertEqual(self.bash("hc send pa1 'done'")[0], 0)
        self.assertEqual(self.bash("hc send w9 'done'")[0], 0)
        self.assertEqual(self.bash("hc read w1 --lines 20")[0], 0)

    def test_no_task_record_still_applies_the_denylist(self):
        self.assertEqual(self.bash("hc send anyone 'done'", cwd=self.bare)[0], 0)
        code, err = self.bash("git push origin t-x", cwd=self.bare)
        self.assertEqual((code, err.strip()), (2, msg(D1, "git push origin t-x")))
        self.assertEqual(self.bash("git status", cwd=self.bare)[0], 0)

    def test_a_profiles_guard_allow_is_ignored(self):  # the key is no longer read
        self.assertEqual(self.bash("git lfs pull")[0], 0)
        code, err = self.bash("git push origin t-x")
        self.assertEqual((code, err.strip()), (2, msg(D1, "git push origin t-x")))

    def test_older_refusals_first_and_overlap(self):
        for line in OLDER_ONLY:
            with self.subTest(line=line):
                code, err = self.bash(line)
                self.assertEqual(code, 2)
                self.assertTrue(err.startswith("Blocked by the Project Agent safety hook: "), err)
        for line, _row, older in OVERLAP:
            with self.subTest(line=line):
                code, err = self.bash(line)
                self.assertEqual(code, 2)
                self.assertTrue(err.startswith("Blocked by the Project Agent safety hook: " + older), err)

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
            ast.parse(f.read(), feature_version=(3, 7))

    def test_worker_rule_line(self):
        with open(os.path.join(REPO, "runtime", "core", "worker.md")) as f:
            text = f.read()
        rules = text.split("## Rules of the job", 1)[1].split("\n## ", 1)[0]
        flat = re.sub(r"\s+", " ", rules)
        self.assertIn("- The guard refuses a few commands that the Safety rules below keep for the person: `git push` "
                      "in any form,", flat)


GUARD_UNAVAILABLE_WORDS = ["guard unavailable", "report", "Project Agent", "There is no override",
                           "hc send is refused too", "last message"]  # t-007 fix round 2: REVIEW-4 M1


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
    """Card d-038 'Block': a guard that cannot load or that raises refuses the call (R6)."""

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
        except Exception as e:  # noqa: BLE001
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

    def test_an_old_guard_api_blocks(self):  # install skew (spec section 6): a guard without the one-argument check
        hooks = self.copy("def check_command(line, pa_address, own_address, extra_rows, cwd):\n    return None\n")
        self.assert_unavailable(*call_hook(hooks, hooks.hook_guard, "Bash", {"command": "git status"}, self.wt))

    def test_a_check_that_raises_blocks(self):
        def boom(*_a, **_k):
            raise RuntimeError("boom")
        broken = mock.Mock(check_command=boom, check_edit=boom)
        with mock.patch.object(pa_hooks, "_load_guard", return_value=broken):
            self.assert_unavailable(*call_hook(pa_hooks, pa_hooks.hook_guard, "Bash", {"command": "git status"}, self.wt))
            self.assert_unavailable(*call_hook(pa_hooks, pa_hooks.hook_guard, "Write", {"file_path": os.path.join(self.wt, "a.py")}, self.wt))
        with mock.patch.object(pa_hooks, "hook_context", side_effect=OSError("gone")):
            self.assert_unavailable(*call_hook(pa_hooks, pa_hooks.hook_guard, "Bash", {"command": "git status"}, self.wt))

    def test_a_malformed_guard_allow_changes_no_verdict(self):
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
                self.assertEqual((code, err.strip()), (2, msg(D1, "git push origin t-x")))
                self.assertEqual(call_hook(pa_hooks, pa_hooks.hook_guard, "Bash", {"command": "git status"}, wt), (0, ""))


class R2_Hook(unittest.TestCase):
    """t-007 fix round 2 (REVIEW-4 M1, REVIEW-3 M5): through pa_hooks.hook_guard."""

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

    def test_bash_from_another_folder(self):  # REVIEW-3 M5: the hook does not assume its folder
        env = {"CLAUDE_PROJECT_DIR": self.wt}
        self.assertEqual(self.call("Bash", {"command": "hc send w9 'done'"}, self.elsewhere, env)[0], 0)
        self.assertEqual(self.call("Bash", {"command": "git checkout only-in-root.py"}, self.elsewhere, env)[0], 0)
        self.assertEqual(self.call("Bash", {"command": "git push origin t-x"}, self.elsewhere, env)[0], 2)
        self.assertEqual(self.call("Write", {"file_path": ".claude/pa-task.json"}, self.elsewhere, env)[0], 2)

    def test_task_record_through_the_hook(self):
        record = os.path.join(self.wt, ".claude", "pa-task.json")
        self.assertEqual(self.call("Write", {"file_path": record}, self.wt)[0], 2)
        self.assertEqual(self.call("Read", {"file_path": record}, self.wt)[0], 0)


# ---- The editor rule (t-007 R8; kept, Q1): verdicts unchanged, the message is EDIT ---------------------------------

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
                self.assertEqual(self.edit(path), EDIT.format(path))

    def test_t052_content_records_are_tool_written(self):  # t-052 J1: content/ is state, never a worker's edit
        self.assertIn("content/", guard.STATE_FILES)
        for path in [os.path.join(self.state, "content", "t-1.json"), os.path.join(self.state, "content", "suites.jsonl"),
                     os.path.join(self.state, "content", "since.json")]:
            with self.subTest(path=path):
                self.assertEqual(self.edit(path), EDIT.format(path))

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

    def test_long_path_is_cut(self):
        path = os.path.join(self.repo, ".git", "x" * 400)
        self.assertEqual(self.edit(path), EDIT.format(path[:297] + "..."))


class FixH_EditorRule(GuardTest):
    """t-007 (h) reviewer 1 #7, verifier F4 (profiles folder), reviewer 2 M2 (.git reached through a link)."""

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
                self.assertEqual(self.edit(path), EDIT.format(path))

    def test_allowed(self):
        for path in [os.path.join(self.state, "evidence", "t-x", "x.md"), os.path.join(self.state, "evidence", "t-y", "build.log"),
                     os.path.join(self.repo, "realgit", "notes.txt"), os.path.join(self.repo, "sub", "a.py")]:
            with self.subTest(path=path):
                self.assertIsNone(self.edit(path))

    def test_through_the_hook(self):
        profile = os.path.join(pa.ROOT, "profiles", "guardp", "profile.json")
        code, err = self.run_hook("Edit", {"file_path": profile})
        self.assertEqual((code, err.strip()), (2, EDIT.format(profile)))
        self.assertEqual(self.run_hook("Write", {"file_path": os.path.join(pa.space_dir("spguard"), "schedule.json")})[0], 2)
        self.assertEqual(self.run_hook("Write", {"file_path": os.path.join(pa.space_dir("spguard"), "evidence", "t-x", "a.md")})[0], 0)


class R2b_TaskRecord(GuardTest):
    """t-007 (e) REVIEW-3 M3: the editor rule protects the task record and the hook settings."""

    def setUp(self):
        super().setUp()
        user = os.path.join(TMP, "r2user")
        self.home = os.path.join(user, ".config", "herdr-pa")
        self.spaces = os.path.join(self.home, "spaces")
        self.wt = os.path.join(user, "work", "wt")
        for d in [os.path.join(self.spaces, "sp1", "evidence", "t-x"), os.path.join(self.wt, "src"),
                  os.path.join(self.wt, ".claude")]:
            os.makedirs(d, exist_ok=True)
        patcher = mock.patch.dict(os.environ, {"HOME": user, "HERDR_PA_HOME": self.home})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_editor(self):
        for path in [os.path.join(self.wt, ".claude", "pa-task.json"), os.path.join(self.wt, ".claude", "settings.local.json"),
                     os.path.join(self.wt, ".claude", "PA-TASK.json")]:
            with self.subTest(path=path):
                self.assertEqual(guard.check_edit(path, self.spaces, cwd=self.wt, home=self.home), EDIT.format(path))
        self.assertIsNone(guard.check_edit(os.path.join(self.wt, "src", "a.py"), self.spaces, cwd=self.wt, home=self.home))


class Split_PersonalSettings(GuardTest):
    """Decided with d-048: the editor rule protects the person's own git and gh settings."""

    def setUp(self):
        super().setUp()
        self.user = tempfile.mkdtemp(dir=TMP)
        self.wt = os.path.join(self.user, "work", "wt")
        os.makedirs(os.path.join(self.wt, "sub"))
        patcher = mock.patch.dict(os.environ, {"HOME": self.user})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("XDG_CONFIG_HOME", None)  # put back by the patcher
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
                self.assertEqual(self.edit(path, cwd), EDIT.format(path))

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


class T037Restart(GuardTest):  # t-037 J2: workers never run pa-restart
    def test_d5_forms(self):
        for form in D5_FORMS:
            line, inner = form[0], form[-1]
            with self.subTest(line=line):
                self.assertEqual(self.check(line), msg(D5, inner))

    def test_d5_allowed(self):
        for line in D5_ALLOWED:
            with self.subTest(line=line):
                self.assertIsNone(self.check(line))



class T041Context(GuardTest):  # t-041 J3: workers never run pa-context
    def test_d6_forms(self):
        for form in D6_FORMS:
            line, inner = form[0], form[-1]
            with self.subTest(line=line):
                self.assertEqual(self.check(line), msg(D6, inner))

    def test_d6_allowed(self):
        for line in D6_ALLOWED:
            with self.subTest(line=line):
                self.assertIsNone(self.check(line))


if __name__ == "__main__":
    unittest.main()
