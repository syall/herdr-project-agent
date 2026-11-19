"""pa-run (t-107 J1): any test command in the tree to run (HEAD plus the worktree's changes, or --commit C with
--overlay paths) on a configured machine, its output streamed back and its exit code kept. Nothing leaves this Mac:
hc is tests/stub-hc (a machine is a temp folder; box-1 is an invented name), and the temp HERDR_PA_HOME, space and
worktree come from test_pa_suite's fixture (through test_pa_suite_remote's Remote).

  r1  argv rules (R2): refusals are 125 with the reason and no hc call
  r2  the tree to run (R4, R6): edits, new, deleted, binary, mode change, symlink; a clean tree has no patch
  r3  nothing is written under .git (R6), for the tree alone and a whole remote run
  r4  a remote run (R1, R7, R16): output, exit code, the tree there is T, the patch that arrived, argv quoted,
      --cwd, every hc call a fixed form, the folder and terminal gone
  r5  --commit/--overlay (R5)
  r6  never sent (R8): .env, .claude/, a profile's secretPaths, too large
  r7  the tree id is compared there (R7)
  r8  slots (R9 as J1-D1: the machine's cap slots), waiting, 124
  r9  suiteFallback (R11): mac, none, --on, --local; pa-full-suite with none
  r10 records (R18, R19): run-<id>.out, build.log, test-runs.jsonl (local and remote; 30 days)
  r11 TERM during a remote run: 143, cleaned
  r12 the 1 s start poll (R17)
  r13 docs, help, Python 3.7
"""
import base64
import contextlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_pa_suite_remote as tpr  # noqa: E402 (its fixture: only names bound here are collected)
from test_pa_suite import pa, pa_hooks, pa_suite, dump, write, sh  # noqa: E402

REPO = tpr.REPO
PA_RUN = os.path.join(tpr.BIN, "pa-run")
RUN = re.compile(r"pa-run: run ([0-9a-f]{12})")

PROBE = r"""#!/bin/sh
# What the run saw: its folder, the tree (with `tree`), git status, each word, a file's text (cat=<path>).
echo "cwd $(pwd)"
code=0
for a in "$@"; do
  case "$a" in
    tree) echo "tree $(git write-tree)" ;;
    status) echo "status [$(git status --porcelain | tr '\n' ' ')]" ;;
    cat=*) f=${a#cat=}; if [ -e "$f" ]; then echo "file $f: $(cat "$f")"; else echo "file $f: missing"; fi ;;
    exit=*) code=${a#exit=} ;;
    wait=*) w=${a#wait=}; while [ ! -f "$w" ]; do sleep 0.1; done ;;
    *) echo "arg [$a]" ;;
  esac
done
echo "remote ${PA_SUITE_REMOTE:-unset}"
if [ -n "${PA_SUITE_REMOTE:-}" ]; then hc ls > /dev/null 2>&1; echo "hc-exit $?"; fi  # a local run's hc is the stub's log
exit $code
"""


def setUpModule():
    tpr.setUpModule()
    if not os.path.lexists(PA_RUN):
        os.symlink(os.path.join(REPO, "runtime", "tools", "pa_hooks.py"), PA_RUN)


tearDownModule = tpr.tearDownModule


def git_state(folder):
    """Every path under folder with its size and mtime (and a folder's entries): what a write would change."""
    seen = {}
    for root, dirs, files in os.walk(folder):
        for name in dirs + files:
            path = os.path.join(root, name)
            st = os.lstat(path)
            seen[os.path.relpath(path, folder)] = (st.st_size if name in files else None, st.st_mtime_ns)
    return seen


class Run(tpr.Remote):
    def setUp(self):
        super().setUp()
        write(os.path.join(self.wt, "probe.sh"), PROBE)
        os.chmod(os.path.join(self.wt, "probe.sh"), 0o755)
        write(os.path.join(self.wt, "d.txt"), "to delete\n")
        write(os.path.join(self.wt, "m.sh"), "echo m\n")
        write(os.path.join(self.wt, "sub", "s.txt"), "in sub\n")
        sh(self.wt, "git", "add", "-A")
        sh(self.wt, "git", "commit", "-q", "-m", "probe")
        self.keep = os.path.join(self.tmp, "kept")
        self.setenv(STUB_HC_KEEP=self.keep, PA_SUITE_WAIT_LIMIT=None,
                    PA_SUITE_REMOTE=None)  # the suite itself run on a machine has it set; a local run here must not

    def pa_run(self, *args):
        out, err = io.StringIO(), io.StringIO()
        saved, sys.argv = sys.argv, ["pa-run"] + list(args)
        code = 0
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    pa_hooks.main()
                except SystemExit as e:
                    code = e.code or 0
        finally:
            sys.argv = saved
        return code, out.getvalue() + err.getvalue()

    def dirty(self):
        """An edit, a new file, a deletion, a binary file, a mode change and a new symlink."""
        write(os.path.join(self.wt, "a.txt"), "a changed\n")
        write(os.path.join(self.wt, "n.txt"), "new\n")
        os.remove(os.path.join(self.wt, "d.txt"))
        with open(os.path.join(self.wt, "bin.dat"), "wb") as f:
            f.write(bytes(range(256)) * 4)
        os.chmod(os.path.join(self.wt, "m.sh"), 0o755)
        os.symlink("a.txt", os.path.join(self.wt, "l"))

    def expected_tree(self):
        """The tree `git add -A` would stage, made in a copy of the worktree (never in it)."""
        copy = os.path.join(self.tmp, "copy")
        shutil.rmtree(copy, ignore_errors=True)
        shutil.copytree(self.wt, copy, symlinks=True)
        sh(copy, "git", "add", "-A")
        return sh(copy, "git", "write-tree")

    def run_rows(self):
        path = os.path.join(pa.ROOT, "test-runs.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(l) for l in f if l.strip()]

    def run_out(self, run):
        return pa.read_text(os.path.join(self.dir, "evidence", "t-1", "run-{}.out".format(run)))

    def run_id(self, out):
        m = RUN.search(out)
        self.assertTrue(m, out)
        return m.group(1)

    def remote_runs(self):
        return [c for c in self.calls() if c[0] == "run"]


class R1Argv(Run):
    def test_r1a_refusals_are_125_and_no_hc_call(self):
        write(os.path.join(self.wt, "tests", "pa_session_check.sh"), "echo x\n")
        write(os.path.join(self.wt, "plain.txt"), "not executable\n")
        os.symlink("tests/run-all.sh", os.path.join(self.wt, "ra.sh"))  # round 3: a symlink in the tree counts as its target
        os.symlink("tests/pa_session_check.sh", os.path.join(self.wt, "psc.sh"))
        sh(self.wt, "git", "add", "-A")
        sh(self.wt, "git", "commit", "-q", "-m", "more")
        cases = [
            (["hc", "ls"], "hc"),
            (["ls", "-l"], "ls"),
            (["/bin/sh", "probe.sh"], "absolute"),
            (["../x"], ".."),
            (["plain.txt"], "executable"),
            (["sh", "-c", "echo hi"], "sh -c"),
            (["sh", "-ec", "echo hi"], "sh -c"),
            (["sh", "tests/pa_session_check.sh"], "pa_session_check.sh"),
            (["sh", "./tests//pa_session_check.sh"], "pa_session_check.sh"),
            (["sh", "tests/run-all.sh"], "use pa-full-suite"),
            (["tests/run-all.sh", "--only-integration"], "use pa-full-suite"),
            (["sh", "tests/run-all.sh", "--only-integration"], "use pa-full-suite"),
            # round 2 (MF-1): sh options with an argument, combined flags, --, stdin, what cannot be parsed
            (["sh", "-o", "errexit", "-c", "hc ls"], "sh -c"),
            (["sh", "+o", "x", "-c", "y"], "sh -c"),
            (["sh", "-eo", "pipefail", "-c", "x"], "sh -c"),
            (["sh", "-e", "-o", "nounset", "-xc", "x"], "sh -c"),
            (["sh", "-ce", "x"], "sh -c"),
            (["sh", "+c", "x"], "sh -c"),
            (["sh", "--", "-c", "x"], "starts with -"),
            (["sh", "-s"], "stdin"),
            (["sh", "-es", "x"], "stdin"),
            (["sh"], "stdin"),
            (["sh", "-e"], "stdin"),
            (["sh", "-o"], "cannot parse"),
            (["sh", "--norc", "probe.sh"], "cannot parse"),
            (["sh", "-i", "probe.sh"], "cannot parse"),
            (["sh", "-eZ", "probe.sh"], "cannot parse"),
            # round 2 (MF-2): the two scripts spelled another way or after sh's options
            (["sh", "-e", "tests/run-all.sh"], "use pa-full-suite"),
            (["sh", "--", "tests/run-all.sh"], "use pa-full-suite"),
            (["sh", "-", "tests/run-all.sh"], "use pa-full-suite"),
            (["sh", "-x", "tests/run-all.sh", "--only-integration"], "use pa-full-suite"),
            (["sh", "-o", "errexit", "tests/run-all.sh"], "use pa-full-suite"),
            (["sh", "tests/../tests/run-all.sh"], "use pa-full-suite"),
            (["sh", "./tests//run-all.sh"], "use pa-full-suite"),
            (["tests/../tests/run-all.sh"], "use pa-full-suite"),
            (["./tests//run-all.sh"], "use pa-full-suite"),
            (["sh", "-e", "tests/pa_session_check.sh"], "pa_session_check.sh"),
            (["sh", "--", "tests/pa_session_check.sh"], "pa_session_check.sh"),
            (["sh", "tests/../tests/pa_session_check.sh"], "pa_session_check.sh"),
            (["sh", "tests//../tests/pa_session_check.sh", "/tmp/x"], "pa_session_check.sh"),
            (["tests/../tests/pa_session_check.sh"], "pa_session_check.sh"),
            # round 3 (MF-3): run-all.sh only with a flag that is not a whole run; "" is its no-flag run
            (["sh", "tests/run-all.sh", ""], "use pa-full-suite"),
            (["tests/run-all.sh", ""], "use pa-full-suite"),
            (["sh", "-e", "tests/run-all.sh", ""], "use pa-full-suite"),
            (["sh", "tests/run-all.sh", "", "--affected"], "use pa-full-suite"),
            (["sh", "tests/run-all.sh", "--"], "use pa-full-suite"),
            (["sh", "tests/run-all.sh", "--bogus"], "use pa-full-suite"),
            # round 3 (MF-4): paths that leave the tree and come back, absolute paths, symlinks in the tree
            (["sh", "../w/tests/run-all.sh"], "leaves the tree"),
            (["sh", "../w/tests/run-all.sh", "--only-integration"], "leaves the tree"),
            (["sh", "../wt/tests/run-all.sh"], "leaves the tree"),
            (["sh", os.path.join(self.wt, "tests", "run-all.sh")], "absolute"),
            (["sh", "/proc/self/cwd/tests/run-all.sh"], "absolute"),
            (["sh", "-e", "--", "../w/tests/run-all.sh"], "leaves the tree"),
            (["sh", "../w/tests/pa_session_check.sh", "x"], "leaves the tree"),
            (["sh", "../wt/tests/pa_session_check.sh"], "leaves the tree"),
            (["sh", os.path.join(self.wt, "tests", "pa_session_check.sh")], "absolute"),
            (["sh", "/proc/self/cwd/tests/pa_session_check.sh", "x"], "absolute"),
            (["sh", "ra.sh"], "use pa-full-suite"),
            (["sh", "ra.sh", "--only-integration"], "use pa-full-suite"),
            (["./ra.sh"], "use pa-full-suite"),
            (["sh", "psc.sh"], "pa_session_check.sh"),
            (["./psc.sh"], "pa_session_check.sh"),
        ]
        for argv, why in cases:
            code, out = self.pa_run("--", *argv)
            self.assertEqual(code, 125, (argv, out))
            self.assertIn("pa-run: not run", out, argv)
            self.assertIn(why, out, argv)
        self.assertEqual(self.calls(), [])

    def test_r1g_part_words_do_not_get_around_the_session_check_refusal(self):
        """t-112 R8: the integration check's --part <k> (three parts) adds no way past the refusal: each of
        today's pa_session_check.sh cases again with the part words, --part=1, and wrappers as argv[0]."""
        write(os.path.join(self.wt, "tests", "pa_session_check.sh"), "echo x\n")
        os.symlink("tests/pa_session_check.sh", os.path.join(self.wt, "psc.sh"))
        sh(self.wt, "git", "add", "-A")
        sh(self.wt, "git", "commit", "-q", "-m", "more")
        cases = []
        for k in ("1", "2", "3"):
            part = ["--part", k]
            cases += [
                (["sh", "tests/pa_session_check.sh", "/tmp/x"] + part, "pa_session_check.sh"),
                (["sh", "-e", "tests/pa_session_check.sh", "/tmp/x"] + part, "pa_session_check.sh"),
                (["sh", "--", "tests/pa_session_check.sh", "/tmp/x"] + part, "pa_session_check.sh"),
                (["./tests/pa_session_check.sh"] + part, "pa_session_check.sh"),
                (["sh", "tests//pa_session_check.sh"] + part, "pa_session_check.sh"),
                (["sh", "tests/../tests/pa_session_check.sh"] + part, "pa_session_check.sh"),
                (["sh", "psc.sh"] + part, "pa_session_check.sh"),
                (["./psc.sh"] + part, "pa_session_check.sh"),
                (["sh", "tests/pa_session_check.sh", "--part=" + k], "pa_session_check.sh"),
                (["sh", "../w/tests/pa_session_check.sh"] + part, "leaves the tree"),
                (["sh", os.path.join(self.wt, "tests", "pa_session_check.sh")] + part, "absolute"),
                (["env", "sh", "tests/pa_session_check.sh"] + part, "pa-run: not run"),  # refused (today by the path rule, before argv[0]'s)
                (["bash", "tests/pa_session_check.sh"] + part, "pa-run: not run"),  # refused (today by the path rule, before argv[0]'s)
                (["timeout", "60", "sh", "tests/pa_session_check.sh"] + part, "pa-run: not run"),  # refused (today by the path rule, before argv[0]'s)
                (["nice", "sh", "tests/pa_session_check.sh"] + part, "pa-run: not run"),  # refused (today by the path rule, before argv[0]'s)
                (["sh", "tests/run-all.sh", "--only-integration"] + part, "use pa-full-suite"),
            ]
        cases.append((["sh", "tests/run-all.sh", "--only-integration"], "use pa-full-suite"))
        for argv, why in cases:
            code, out = self.pa_run("--", *argv)
            self.assertEqual(code, 125, (argv, out))
            self.assertIn("pa-run: not run", out, argv)
            self.assertIn(why, out, argv)
        self.assertEqual(self.calls(), [])

    def test_r1e_refusals_from_a_subfolder(self):
        """--cwd tests: the two scripts by their names there, and by a .. spelling."""
        for argv, why in ((["sh", "run-all.sh"], "use pa-full-suite"), (["sh", "-e", "./run-all.sh"], "use pa-full-suite"),
                          (["sh", "../tests/run-all.sh"], "use pa-full-suite"),
                          (["sh", "--", "pa_session_check.sh"], "pa_session_check.sh"),
                          (["sh", "../tests/pa_session_check.sh"], "pa_session_check.sh"),
                          # round 3 (MF-4)
                          (["sh", "../../w/tests/run-all.sh"], "leaves the tree"),
                          (["sh", "../../wt/tests/pa_session_check.sh"], "leaves the tree"),
                          (["sh", os.path.join(self.wt, "tests", "run-all.sh")], "absolute"),
                          (["sh", "run-all.sh", ""], "use pa-full-suite")):
            code, out = self.pa_run("--cwd", "tests", "--", *argv)
            self.assertEqual(code, 125, (argv, out))
            self.assertIn(why, out, argv)
        self.assertEqual(self.calls(), [])

    def test_r1b_cwd_must_stay_inside(self):
        for value in ("../x", "/tmp", "sub/../../x"):
            code, out = self.pa_run("--cwd", value, "--", "sh", "probe.sh")
            self.assertEqual(code, 125, (value, out))
        self.assertEqual(self.calls(), [])

    def test_r1c_allowed_forms_pass_the_check(self):
        pa_run = pa_hooks.load_tool("pa_run")
        for argv in (["python3", "-m", "unittest", "tests.test_x"], ["node", "x.mjs"], ["sh", "probe.sh"],
                     ["./probe.sh"], ["probe.sh"], ["sh", "tests/run-all.sh", "--except-integration"],
                     ["sh", "tests/run-all.sh", "--affected"], ["sh", "tests/run-all.sh", "--module", "test_x"],
                     ["python3", "-c", "print(1)"], ["sh", "-e", "probe.sh"], ["sh", "-eo", "pipefail", "probe.sh"],
                     ["sh", "-o", "errexit", "+x", "probe.sh"], ["sh", "--", "probe.sh"], ["sh", "-", "probe.sh"],
                     ["sh", "-x", "tests/run-all.sh", "--affected"], ["sh", "-e", "probe.sh", "-c", "x"],
                     ["sh", "sub/../probe.sh"], ["python3", "-m", "unittest", "../w/tests/run-all.sh"]):
            self.assertIsNone(pa_run.check_argv(argv, self.wt, "", None, []), argv)

    def test_r1f_an_allowed_run_all_flag_still_runs(self):
        """round 3: the allowlist leaves run-all.sh's flags that are not a whole run working (here the fake's part)."""
        code, out = self.pa_run("--", "sh", "tests/run-all.sh", "--except-integration")
        self.assertEqual(code, 0, out)
        self.assertIn("fake remote part in", out)
        self.assertTrue(self.remote_runs(), out)

    def test_r1d_no_argv_is_a_usage_error(self):
        code, out = self.pa_run()
        self.assertNotEqual(code, 0, out)


class R2Tree(Run):
    def test_r2a_every_kind_of_change_is_in_the_tree(self):
        self.dirty()
        want = self.expected_tree()
        pa_run = pa_hooks.load_tool("pa_run")
        found = pa_run.tree_to_run(pa_hooks, self.wt)
        self.assertEqual(found["tree"], want)
        self.assertEqual(found["commit"], sh(self.wt, "git", "rev-parse", "HEAD"))
        self.assertTrue(found["patch"])
        for name in ("a.txt", "n.txt", "d.txt", "bin.dat", "m.sh", "l"):
            self.assertIn(name, found["paths"])
        self.assertIn(b"GIT binary patch", found["patch"].encode() if isinstance(found["patch"], str) else found["patch"])

    def test_r2b_a_clean_tree_has_no_patch(self):
        pa_run = pa_hooks.load_tool("pa_run")
        found = pa_run.tree_to_run(pa_hooks, self.wt)
        self.assertEqual(found["tree"], sh(self.wt, "git", "rev-parse", "HEAD^{tree}"))
        self.assertFalse(found["patch"])
        self.assertEqual(found["paths"], [])

    def test_r2c_ignored_files_are_not_sent(self):
        write(os.path.join(self.wt, ".gitignore"), ".claude/\nbuild/\n")
        sh(self.wt, "git", "commit", "-q", "-am", "ignore build")
        write(os.path.join(self.wt, "build", "out.txt"), "made\n")
        found = pa_hooks.load_tool("pa_run").tree_to_run(pa_hooks, self.wt)
        self.assertFalse(found["patch"])


class R3NoGitWrites(Run):
    def test_r3a_the_tree_alone_writes_nothing_under_git(self):
        self.dirty()
        before = git_state(os.path.join(self.wt, ".git"))
        pa_hooks.load_tool("pa_run").tree_to_run(pa_hooks, self.wt)
        pa_hooks.load_tool("pa_run").tree_to_run(pa_hooks, self.wt, commit="HEAD~1", overlay=["a.txt"])
        self.assertEqual(git_state(os.path.join(self.wt, ".git")), before)

    def test_r3b_a_whole_remote_run_writes_nothing_under_git(self):
        self.dirty()
        before = git_state(os.path.join(self.wt, ".git"))
        code, out = self.pa_run("--", "sh", "probe.sh", "tree")
        self.assertEqual(code, 0, out)
        self.assertTrue(self.remote_runs(), out)
        self.assertEqual(git_state(os.path.join(self.wt, ".git")), before)


class R4Remote(Run):
    def test_r4a_output_exit_and_the_tree_there(self):
        self.dirty()
        want = self.expected_tree()
        commit = sh(self.wt, "git", "rev-parse", "HEAD")
        code, out = self.pa_run("--for", "single", "--", "sh", "probe.sh", "tree", "status", "cat=a.txt", "cat=d.txt",
                                "exit=3")
        self.assertEqual(code, 3, out)
        self.assertIn("tree " + want, out)
        self.assertIn("file a.txt: a changed", out)
        self.assertIn("file d.txt: missing", out)
        self.assertRegex(out, r"status \[[^\]]*M  a\.txt")
        self.assertIn("remote 1", out)
        self.assertIn("hc-exit 2", out)  # the refusing shim
        run = self.run_id(out)
        self.assertFalse(os.path.exists(self.remote_folder(run)))
        terms = os.path.join(self.stub, "box-1", "terminals")
        self.assertEqual([n for n in os.listdir(terms) if n.endswith(".json")], [])
        # the patch that arrived is git diff --binary C T, and the setup compared the tree there
        with open(os.path.join(self.keep, "p.b64"), "rb") as f:
            patch = base64.b64decode(f.read())
        self.assertIn(b"GIT binary patch", patch)
        self.assertIn(b"n.txt", patch)
        setup = [c for c in self.calls() if c[0] == "exec" and "apply --index" in " ".join(c)]
        self.assertTrue(setup, self.calls())
        self.assertIn("write-tree", " ".join(setup[0]))
        self.assertEqual(self.lines(), [])  # never a suite record
        self.assertIn(commit, json.dumps(self.run_rows()[-1]))

    def test_r4b_argv_words_arrive_quoted_and_are_not_run(self):
        marker = os.path.join(self.tmp, "pwned")
        code, out = self.pa_run("--", "sh", "probe.sh", "a b", "$(touch {})".format(marker), "it's; ls")
        self.assertEqual(code, 0, out)
        self.assertIn("arg [a b]", out)
        self.assertIn("arg [$(touch {})]".format(marker), out)
        self.assertIn("arg [it's; ls]", out)
        self.assertFalse(os.path.exists(marker))

    def test_r4c_cwd_runs_in_the_subfolder(self):
        code, out = self.pa_run("--cwd", "sub", "--", "sh", "../probe.sh", "cat=s.txt")
        self.assertEqual(code, 0, out)
        self.assertIn("file s.txt: in sub", out)
        self.assertRegex(out, r"cwd /\S*/w/sub")

    def test_r4d_every_hc_call_is_a_fixed_form(self):
        self.dirty()
        code, out = self.pa_run("--", "sh", "probe.sh")
        self.assertEqual(code, 0, out)
        run = self.run_id(out)
        folder = "/tmp/pa-suite-" + run
        for c in self.calls():
            self.assertIn(c[0], ("exec", "file", "run", "kill", "rm", "ls"), c)
            if c[0] in ("exec", "file", "run"):
                self.assertEqual(c[c.index("--on") + 1], "box-1", c)
            if c[0] == "file":
                self.assertTrue(c[4].startswith(folder + "/"), c)  # file read|write --on M <path> ...
            if c[0] == "run":
                for flag in ("--bg", "--rm", "--no-notify"):
                    self.assertIn(flag, c)
                self.assertEqual(c[c.index("--cwd") + 1], folder + "/w")
                script = c[-1]
                for name in ("HERDR_PA_HOME=" + folder + "/home", "TMPDIR=" + folder + "/tmp", "HOME=" + folder + "/u",
                             "PA_SUITE_REMOTE=1", "export PATH=" + folder + "/bin:$PATH", "sh probe.sh"):
                    self.assertIn(name, script)


class R5Commit(Run):
    def test_r5a_base_code_with_heads_test(self):
        base = sh(self.wt, "git", "rev-parse", "HEAD")
        write(os.path.join(self.wt, "code.txt"), "new code\n")
        write(os.path.join(self.wt, "test.txt"), "new test\n")
        sh(self.wt, "git", "add", "-A")
        sh(self.wt, "git", "commit", "-q", "-m", "fix")
        write(os.path.join(self.wt, "test.txt"), "newer test\n")  # uncommitted: the overlay takes the worktree's
        code, out = self.pa_run("--commit", base, "--overlay", "test.txt", "--for", "failing-first", "--",
                                "sh", "probe.sh", "cat=code.txt", "cat=test.txt", "status")
        self.assertEqual(code, 0, out)
        self.assertIn("file code.txt: missing", out)
        self.assertIn("file test.txt: newer test", out)
        self.assertRegex(out, r"status \[A  test\.txt \]")

    def test_r5b_commit_alone_runs_it_as_committed(self):
        base = sh(self.wt, "git", "rev-parse", "HEAD")
        write(os.path.join(self.wt, "code.txt"), "new\n")
        sh(self.wt, "git", "add", "-A")
        sh(self.wt, "git", "commit", "-q", "-m", "c")
        write(os.path.join(self.wt, "a.txt"), "dirty\n")
        code, out = self.pa_run("--commit", base, "--", "sh", "probe.sh", "cat=code.txt", "cat=a.txt")
        self.assertEqual(code, 0, out)
        self.assertIn("file code.txt: missing", out)
        self.assertIn("file a.txt: a", out)

    def test_r5c_not_an_ancestor_is_125(self):
        sh(self.wt, "git", "checkout", "-q", "-b", "side")
        write(os.path.join(self.wt, "side.txt"), "s\n")
        sh(self.wt, "git", "add", "-A")
        sh(self.wt, "git", "commit", "-q", "-m", "side")
        side = sh(self.wt, "git", "rev-parse", "HEAD")
        sh(self.wt, "git", "checkout", "-q", "main")
        code, out = self.pa_run("--commit", side, "--", "sh", "probe.sh")
        self.assertEqual(code, 125, out)
        self.assertIn("ancestor", out)
        self.assertEqual(self.remote_runs(), [])

    def test_r5d_an_overlay_missing_in_the_worktree_is_deleted(self):
        os.remove(os.path.join(self.wt, "d.txt"))
        code, out = self.pa_run("--commit", "HEAD", "--overlay", "d.txt", "--", "sh", "probe.sh", "cat=d.txt")
        self.assertEqual(code, 0, out)
        self.assertIn("file d.txt: missing", out)


class R6NeverSent(Run):
    def assert_refused(self, name, out, code):
        self.assertEqual(code, 125, out)
        self.assertIn(name, out)
        self.assertEqual(self.remote_runs(), [])
        self.assertFalse([c for c in self.calls() if c[0] == "file" and c[1] == "write"], self.calls())

    def test_r6a_an_env_file_refuses(self):
        write(os.path.join(self.wt, "conf", ".env"), "X=1\n")
        code, out = self.pa_run("--", "sh", "probe.sh")
        self.assert_refused("conf/.env", out, code)

    def test_r6b_a_claude_file_refuses(self):
        write(os.path.join(self.wt, ".claude", "x.txt"), "x\n")
        sh(self.wt, "git", "add", "-f", ".claude/x.txt")
        code, out = self.pa_run("--", "sh", "probe.sh")
        self.assert_refused(".claude/x.txt", out, code)

    def test_r6c_a_profile_secret_path_refuses(self):
        profile = os.path.join(pa.ROOT, "profiles", "test", "profile.json")
        value = pa.read_json(profile)
        value["secretPaths"] = [r"(^|/)secret\.key$"]
        dump(value, profile)
        write(os.path.join(self.wt, "k", "secret.key"), "k\n")
        code, out = self.pa_run("--", "sh", "probe.sh")
        self.assert_refused("k/secret.key", out, code)

    def test_r6d_too_large_refuses(self):
        with open(os.path.join(self.wt, "big.bin"), "wb") as f:
            f.write(os.urandom(5 * 1024 * 1024))
        code, out = self.pa_run("--", "sh", "probe.sh")
        self.assertEqual(code, 125, out)
        self.assertIn("too large", out)
        self.assertEqual(self.remote_runs(), [])


class R7TreeChecked(Run):
    def test_r7a_another_tree_there_is_a_setup_failure(self):
        write(os.path.join(self.wt, "a.txt"), "changed\n")
        want = self.expected_tree()
        self.setenv(STUB_HC_SUB="{}={}".format(want, "0" * 40))
        self.configure([{"machine": "box-1"}])
        code, out = self.pa_run("--on", "box-1", "--", "sh", "probe.sh")
        self.assertEqual(code, 125, out)
        self.assertIn("setup", out)
        self.assertEqual(self.remote_runs(), [])
        self.assertFalse(os.listdir(os.path.join(self.stub, "box-1", "tmp")))

    def test_r7b_a_patch_that_does_not_apply_is_a_setup_failure(self):
        write(os.path.join(self.wt, "a.txt"), "changed\n")
        self.setenv(STUB_HC_FAIL="garble:p.b64")
        code, out = self.pa_run("--on", "box-1", "--", "sh", "probe.sh")
        self.assertEqual(code, 125, out)
        self.assertEqual(self.remote_runs(), [])
        self.assertFalse(os.listdir(os.path.join(self.stub, "box-1", "tmp")))


class R8Slots(Run):
    def test_r8a_a_run_takes_the_machines_slot(self):
        self.setenv(PA_SUITE_WAIT_LIMIT="1")
        self.hold(self.machine_slot("box-1"))
        code, out = self.pa_run("--", "sh", "probe.sh")
        self.assertEqual(code, 124, out)
        self.assertIn("waiting", out)
        self.assertEqual(self.remote_runs(), [])
        self.assertFalse(os.path.exists(os.path.join(pa_suite.slot_dir(), "box-1", "light-0")))

    def test_r8b_a_cap_of_two_lets_it_in(self):
        self.configure([{"machine": "box-1", "cap": 2}])
        self.hold(self.machine_slot("box-1", 0))
        code, out = self.pa_run("--", "sh", "probe.sh")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.run_rows()[-1]["machine"], "box-1")

    def test_r8c_the_second_machine_when_the_first_is_busy(self):
        self.configure([{"machine": "box-1"}, {"machine": "box-2"}])
        self.hold(self.machine_slot("box-1"))
        code, out = self.pa_run("--", "sh", "probe.sh")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.run_rows()[-1]["machine"], "box-2")


class R9Fallback(Run):
    def set_fallback(self, value):
        cfg = pa.read_json(os.path.join(pa.ROOT, "config.json"), {}) or {}
        cfg["suiteFallback"] = value
        dump(cfg, os.path.join(pa.ROOT, "config.json"))

    def test_r9a_unreachable_with_mac_runs_here_and_says_why(self):
        self.setenv(STUB_HC_FAIL="down:box-1")
        code, out = self.pa_run("--", "sh", "probe.sh")
        self.assertEqual(code, 0, out)
        self.assertIn("this Mac", out)
        self.assertIn("needs_login", out)
        self.assertIn("remote unset", out)
        row = self.run_rows()[-1]
        self.assertEqual(row["place"], "local")
        self.assertIn("needs_login", row.get("note") or "")

    def test_r9b_none_never_runs_here(self):
        self.set_fallback("none")
        self.setenv(STUB_HC_FAIL="down:box-1")
        code, out = self.pa_run("--", "sh", "probe.sh")
        self.assertEqual(code, 125, out)
        self.assertIn("pa-run: not run on box-1", out)
        self.assertNotIn("remote unset", out)
        self.assertNotIn("cwd ", out)

    def test_r9c_on_unreachable_is_125(self):
        self.setenv(STUB_HC_FAIL="down:box-1")
        code, out = self.pa_run("--on", "box-1", "--", "sh", "probe.sh")
        self.assertEqual(code, 125, out)
        self.assertNotIn("cwd ", out)
        code, out = self.pa_run("--on", "box-9", "--", "sh", "probe.sh")
        self.assertEqual(code, 125, out)

    def test_r9d_local_makes_no_hc_call(self):
        code, out = self.pa_run("--local", "--", "sh", "probe.sh", "exit=4")
        self.assertEqual(code, 4, out)
        self.assertIn("remote unset", out)
        self.assertEqual(self.calls(), [])

    def test_r9e_no_machines_runs_here(self):
        self.configure([])
        self.set_fallback("none")  # not read without machines
        code, out = self.pa_run("--", "sh", "probe.sh")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.calls(), [])

    def test_r9f_local_commit_overlay_runs_in_a_scratch_tree(self):
        base = sh(self.wt, "git", "rev-parse", "HEAD")
        write(os.path.join(self.wt, "code.txt"), "new\n")
        sh(self.wt, "git", "add", "-A")
        sh(self.wt, "git", "commit", "-q", "-m", "c")
        write(os.path.join(self.wt, "a.txt"), "worktree a\n")
        before = git_state(os.path.join(self.wt, ".git"))
        code, out = self.pa_run("--local", "--commit", base, "--overlay", "a.txt", "--", "sh", "probe.sh",
                                "cat=code.txt", "cat=a.txt")
        self.assertEqual(code, 0, out)
        self.assertIn("file code.txt: missing", out)
        self.assertIn("file a.txt: worktree a", out)
        self.assertNotIn("cwd " + self.wt, out)
        self.assertEqual(git_state(os.path.join(self.wt, ".git")), before)

    def test_r9g_pa_full_suite_with_none_behaves_as_on(self):
        self.set_fallback("none")
        self.setenv(STUB_HC_FAIL="down:box-1")
        code, out = self.main()
        self.assertEqual(code, 2, out)
        self.assertIn("--on box-1", out)
        self.assertEqual(self.lines(), [])

    def test_r9h_a_bad_value_is_named_and_read_as_mac(self):
        self.set_fallback("sometimes")
        self.setenv(STUB_HC_FAIL="down:box-1")
        code, out = self.pa_run("--", "sh", "probe.sh")
        self.assertEqual(code, 0, out)
        self.assertIn("suiteFallback", out)


class R10Records(Run):
    def test_r10a_remote_run_output_build_log_and_record(self):
        old = {"at": "2020-01-01T00:00:00Z", "run": "old", "place": "local"}
        write(os.path.join(pa.ROOT, "test-runs.jsonl"), json.dumps(old) + "\n")
        code, out = self.pa_run("--for", "mutation", "--", "sh", "probe.sh", "x" * 300)
        self.assertEqual(code, 0, out)
        run = self.run_id(out)
        self.assertIn("arg [" + "x" * 300 + "]", self.run_out(run))
        log = [l for l in self.build_log().splitlines() if " pa-run " in l]
        self.assertEqual(len(log), 1, self.build_log())
        self.assertRegex(log[0], r"^=== \S+ pa-run mutation: sh probe.sh x+ on box-1 -> exit 0 after \S+s \(waited \S+s\)")
        self.assertNotIn("cwd ", self.build_log())
        rows = self.run_rows()
        self.assertEqual([r["run"] for r in rows], [run])
        row = rows[0]
        for key in ("at", "run", "task", "worker", "kind", "argv", "place", "machine", "os", "waited", "seconds",
                    "overhead", "exit", "note"):
            self.assertIn(key, row)
        self.assertEqual((row["task"], row["kind"], row["place"], row["machine"], row["exit"]),
                         ("t-1", "mutation", "box-1", "box-1", 0))
        self.assertLessEqual(len(row["argv"]), 200)
        for key in ("probe", "ship", "start", "copyback", "cleanup"):
            self.assertIn(key, row["overhead"])
        progress = self.progress(run)
        self.assertEqual((progress.get("kind"), progress.get("state"), progress.get("place")), ("pa-run", "done", "box-1"))
        self.assertTrue(progress.get("cleaned"))
        self.assertEqual(self.lines(), [])

    def test_r10b_a_local_run_is_recorded_too(self):
        code, out = self.pa_run("--local", "--", "sh", "probe.sh")
        self.assertEqual(code, 0, out)
        row = self.run_rows()[-1]
        self.assertEqual((row["place"], row["machine"], row["kind"]), ("local", None, "other"))


class R11Interrupt(Run):
    def test_r11_term_ends_the_remote_run_and_cleans(self):
        go = os.path.join(self.tmp, "never")
        log = os.path.join(self.tmp, "bg.out")
        with open(log, "w") as f:
            p = subprocess.Popen([sys.executable, PA_RUN, "--", "sh", "probe.sh", "wait=" + go], cwd=self.wt,
                                 stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT)
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.wait()))
        self.assertTrue(self.until(lambda: RUN.search(pa.read_text(log) or "")), pa.read_text(log))
        run = self.run_id(pa.read_text(log))
        self.assertTrue(self.until(lambda: self.remote_runs()), pa.read_text(log))
        self.assertTrue(self.until(lambda: os.path.exists(os.path.join(self.remote_folder(run), "t0"))))
        p.send_signal(signal.SIGTERM)
        self.assertEqual(p.wait(timeout=60), 143, pa.read_text(log))
        self.assertFalse(os.path.exists(self.remote_folder(run)))
        self.assertEqual(self.progress(run).get("exit"), 143)


class R12StartPoll(Run):
    def test_r12_a_short_run_is_back_within_seconds_with_a_slow_poll(self):
        self.setenv(PA_SUITE_REMOTE_POLL="30")
        began = time.time()
        code, out = self.pa_run("--", "sh", "probe.sh")
        self.assertEqual(code, 0, out)
        self.assertLess(time.time() - began, 15, out)


class R13Docs(unittest.TestCase):
    def read(self, *path):
        with open(os.path.join(REPO, *path)) as f:
            return " ".join(f.read().split())

    def test_r13a_worker_and_sdlc_name_pa_run(self):
        for name in ("worker.md", "sdlc.md"):
            text = self.read("runtime", "core", name)
            self.assertIn("pa-run -- ", text, name)
            self.assertIn("--commit <base> --overlay", text, name)
            self.assertIn("pa-run --local", text, name)

    def test_r13b_readme_names_suite_fallback(self):
        text = self.read("README.md")
        self.assertIn('"suiteFallback"', text)
        self.assertIn("pa-run", text)
        self.assertNotIn("lightCap", text)

    def test_r13c_help_and_install(self):
        r = subprocess.run([sys.executable, PA_RUN, "--help"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True)
        for flag in ("--on", "--local", "--commit", "--overlay", "--cwd", "--for"):
            self.assertIn(flag, r.stdout)
        self.assertIn("pa-run", self.read("install.sh"))

    def test_r13d_pa_run_parses_as_python_37(self):
        import ast
        with open(os.path.join(REPO, "runtime", "tools", "pa_run.py")) as f:
            ast.parse(f.read(), feature_version=(3, 7))


if __name__ == "__main__":
    unittest.main()
