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
t-117 (the limits from t-107 J1 and J2; spec R-numbers):
  q1  R1: sh's script, argv[0] and a session-check word judged by realpath; a symlink in --commit C's tree
  q2  R2: README and the docstring say pa-run trusts the worktree's code (python3 -c, node -e)
  q3  R3: a refusal after the argv check (a secret, not an ancestor) writes build.log and test-runs.jsonl
  q4  R4: TERM while the tree to run is made: 143, a record `interrupted`, no traceback, its scratch removed
  q5  R5: a closed stdout (| head -1) does not end the run: argv's code, records, cleaned, the whole output kept;
      q5c an error in run_remote that is neither Interrupted nor SetupFailed: cleaned there, recorded exit 1, raised
  q6  R6: a clean filter never runs (it writes under .git): 125 naming the path and the filter
  q9  R9: every 124 is listed in the Exit line, run-all.sh's exit comment and README
  q10 R10: --cwd that is not a folder in the tree to run: 125 before anything runs; a folder of the tree to run
      (a worktree change, a new folder, --commit with --overlay, remote and local) runs (round 2, REVIEW MF-1)
  q11 R11: a patch too large to send: this Mac with "mac" (saying why), 125 with "none"
  q12 R12: --overlay words are literal paths
  q13 R13: suiteMachines with no usable entry and "none": 125
t-103 J1: j1a a run there that died without its code ends at once, 124 (R5); j1b-c a failed cleanup after a setup
failure is left for the next run's sweep (R7)
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
from unittest import mock

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

    def test_r2d_a_same_size_edit_in_the_second_of_a_checkout_is_sent(self):
        """t-130: an entry as new as the index file is racy, so git re-hashes it. The scratch index must keep the
        real index's mtime, or git trusts the stat data and stages HEAD's blob for a same-size edit made in the same
        second as the checkout that wrote the index."""
        path = os.path.join(self.wt, "r.txt")
        index = os.path.join(self.wt, sh(self.wt, "git", "rev-parse", "--git-path", "index"))
        write(path, "racy-old\n")
        sh(self.wt, "git", "add", "r.txt")
        sh(self.wt, "git", "commit", "-q", "-m", "racy")
        for _ in range(20):
            while time.time() % 1 > 0.3:  # start early in a second, so checkout and edit fit in it
                time.sleep(0.02)
            write(path, "racy-mid\n")
            sh(self.wt, "git", "checkout", "--", "r.txt")  # writes r.txt and the index, now
            with open(path, "r+b") as f:  # same size, same inode, in place
                f.write(b"racy-new\n")
            if int(os.stat(index).st_mtime) == int(os.stat(path).st_mtime):
                break
        else:
            self.fail("could not make the edit in the same second as the index write")
        self.assertEqual(os.path.getsize(path), len(b"racy-old\n"))
        second = int(os.stat(index).st_mtime)
        while int(time.time()) <= second + 1:  # a copy of the index made now is newer than the entry
            time.sleep(0.05)
        found = pa_hooks.load_tool("pa_run").tree_to_run(pa_hooks, self.wt)
        self.assertIn("r.txt", found["paths"])
        patch = found["patch"] if isinstance(found["patch"], bytes) else found["patch"].encode()
        self.assertIn(b"+racy-new", patch)


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
        # t-117 R11: a patch too large to send goes the setup-failed way; with suiteFallback "none" that is 125
        cfg = pa.read_json(os.path.join(pa.ROOT, "config.json"), {}) or {}
        cfg["suiteFallback"] = "none"
        dump(cfg, os.path.join(pa.ROOT, "config.json"))
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

    def test_r11b_term_inside_watcher_start_is_143_and_cleans(self):
        """t-144 V-1: the TERM comes while the watcher is being started (here before its thread runs, on purpose:
        no timing). pa-run ends 143, not 1 from watcher.stop() joining a thread never started, and cleans up."""
        import threading
        go = os.path.join(self.tmp, "go")
        self.addCleanup(lambda: write(go, "go\n"))  # the probe there, if it was left, ends
        real = threading.Thread.start
        hit = []

        def start(thread):
            if type(thread).__name__ == "RemoteWatcher" and not hit:
                hit.append(thread)
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(10)  # pa-run's handler raises here (PEP 475), so the real start never runs
            return real(thread)
        threading.Thread.start = start
        try:
            code, out = self.pa_run("--", "sh", "probe.sh", "wait=" + go)
        except RuntimeError as e:  # what V-1 saw: watcher.stop() on a thread never started
            code, out = None, "RuntimeError: {}".format(e)
        finally:
            threading.Thread.start = real
        self.assertTrue(hit, out)
        self.assertNotIn("RuntimeError", out)
        self.assertEqual(code, 143, out)
        run = self.run_id(out)
        self.assertFalse(os.path.exists(self.remote_folder(run)), out)
        self.assertEqual(self.progress(run).get("exit"), 143, out)


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


# ---- t-117 ----

def set_fallback(value):
    cfg = pa.read_json(os.path.join(pa.ROOT, "config.json"), {}) or {}
    cfg["suiteFallback"] = value
    dump(cfg, os.path.join(pa.ROOT, "config.json"))


def scratch_trees():
    """tree_to_run's scratch folders (pa-run-tree-*) in this process's temp folder."""
    import tempfile
    return {n for n in os.listdir(tempfile.gettempdir()) if n.startswith("pa-run-tree-")}


class Q1Symlinks(Run):
    """R1 (RL-6, RL-1's symlink part, VERIFY J1r3 Nit-7): a script word and an argv[0] path are judged by the
    realpath of top/subdir/word, with no lexical normalising first."""

    def setUp(self):
        super().setUp()
        for rel in ("tests/pa_session_check.sh", "wrap.sh", "tests/sub/f"):
            write(os.path.join(self.wt, rel), "#!/bin/sh\necho ran {}\n".format(rel))
        for rel in ("wrap.sh", "tests/sub/f"):
            os.chmod(os.path.join(self.wt, rel), 0o755)
        write(os.path.join(self.tmp, "other", "tests", "run-all.sh"), "echo another worktree's run-all.sh\n")
        os.symlink("tests/sub", os.path.join(self.wt, "lnk"))  # an in-tree folder link
        os.symlink("../other/tests/run-all.sh", os.path.join(self.wt, "ra1"))  # another worktree's run-all.sh
        os.symlink("/proc/self/cwd/tests/run-all.sh", os.path.join(self.wt, "ra2"))  # the cwd's run-all.sh there
        os.symlink("..", os.path.join(self.wt, "lnkw"))  # the folder above the tree
        os.symlink("/bin/sh", os.path.join(self.wt, "shx"))  # a program outside the tree
        sh(self.wt, "git", "add", "-A")
        sh(self.wt, "git", "commit", "-q", "-m", "links")

    def test_q1a_paths_through_symlinks_are_judged_by_realpath(self):
        # AC1.1. On Linux, `sh ra2` from the tree's top resolves to the top's run-all.sh here (refused before t-117
        # too); on a Mac /proc is not there, so its realpath leaves the tree.
        cases = [
            ([], ["sh", "ra2"], "pa-run: not run"),
            ([], ["sh", "ra1"], "leaves the tree"),
            ([], ["sh", "lnk/../run-all.sh"], "use pa-full-suite"),
            ([], ["sh", "lnk/../pa_session_check.sh", "x"], "pa_session_check.sh"),
            ([], ["sh", "lnkw/tests/run-all.sh"], "leaves the tree"),
            (["--cwd", "lnkw"], ["sh", "tests/run-all.sh"], "pa-run: not run"),
            ([], ["./shx", "-c", "x"], "leaves the tree"),
            ([], ["python3", "x.py", "lnk/../pa_session_check.sh"], "pa_session_check.sh"),  # any word naming it
        ]
        for opts, argv, why in cases:
            with self.subTest(argv=argv, opts=opts):
                code, out = self.pa_run(*(opts + ["--"] + argv))
                self.assertEqual(code, 125, out)
                self.assertIn("pa-run: not run", out)
                self.assertIn(why, out)
        self.assertEqual(self.calls(), [])

    def test_q1b_in_tree_paths_and_links_that_stay_inside_still_run(self):
        # AC1.2
        pa_run = pa_hooks.load_tool("pa_run")
        for argv in (["sh", "wrap.sh"], ["sh", "tests/sub/f"], ["sh", "tests/run-all.sh", "--affected"],
                     ["sh", "lnk/f"], ["lnk/f"], ["./wrap.sh"]):
            with self.subTest(argv=argv):
                self.assertIsNone(pa_run.check_argv(argv, self.wt, "", None, [], pa_hooks), argv)

    def test_q1c_a_hard_link_to_run_all_is_run_all(self):
        # AC1.3 (pins the samefile compare; VERIFY J1r3 Nit-7, M4)
        os.link(os.path.join(self.wt, "tests", "run-all.sh"), os.path.join(self.wt, "hard.sh"))
        why = pa_hooks.load_tool("pa_run").check_argv(["sh", "hard.sh"], self.wt, "", None, [], pa_hooks)
        self.assertIsNotNone(why)
        self.assertIn("use pa-full-suite", why)

    def test_q1d_a_symlink_in_the_commits_tree_is_refused(self):
        # AC1.4: in C, x -> tests/run-all.sh; the worktree's x is a plain file (what runs is C's)
        os.symlink("tests/run-all.sh", os.path.join(self.wt, "x"))
        sh(self.wt, "git", "add", "-A")
        sh(self.wt, "git", "commit", "-q", "-m", "x is a link")
        base = sh(self.wt, "git", "rev-parse", "HEAD")
        os.remove(os.path.join(self.wt, "x"))
        write(os.path.join(self.wt, "x"), "echo plain\n")
        code, out = self.pa_run("--commit", base, "--", "sh", "x")
        self.assertEqual(code, 125, out)
        self.assertIn("pa-run: not run", out)
        self.assertIn("symlink", out)
        self.assertEqual(self.remote_runs(), [])


class Q2Trust(unittest.TestCase):
    """R2 (RL-1; Q1 answered: document, no refusal): README and pa_run.py's docstring say pa-run trusts the
    worktree's code as the suite does."""
    read = R13Docs.read

    def test_q2a_readme_and_docstring_say_the_tree_is_trusted(self):
        for path in (("README.md",), ("runtime", "tools", "pa_run.py")):
            with self.subTest(path=path):
                text = self.read(*path)
                self.assertIn("trusts the worktree's code", text)
                self.assertIn("python3 -c", text)
                self.assertIn("node -e", text)

    def test_q2b_python3_c_and_node_e_are_not_refused(self):
        pa_run = pa_hooks.load_tool("pa_run")
        for argv in (["python3", "-c", "print(1)"], ["node", "-e", "1"]):
            self.assertIsNone(pa_run.check_argv(argv, REPO, "", None, []), argv)


class Q3RefusedRecords(Run):
    """R3 (RL-2, VERIFY Nit-J2-1): a pa-run that passed the argv check and then did not run writes its records:
    one build.log line `-> exit 125` with the reason, one test-runs.jsonl line exit 125 with the note."""

    def assert_recorded(self, code, out, reason):
        self.assertEqual(code, 125, out)
        self.assertIn(reason, out)
        log = [l for l in (self.build_log() or "").splitlines() if " pa-run " in l]
        self.assertEqual(len(log), 1, self.build_log())
        self.assertIn("-> exit 125", log[0])
        self.assertIn(reason, log[0])
        rows = self.run_rows()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["exit"], 125, rows)
        self.assertIn(reason, rows[0].get("note") or "", rows)
        self.assertEqual(self.remote_runs(), [])

    def test_q3a_a_changed_env_file_is_recorded(self):
        # AC3.1
        write(os.path.join(self.wt, "conf", ".env"), "X=1\n")
        code, out = self.pa_run("--", "sh", "probe.sh")
        self.assert_recorded(code, out, "conf/.env")

    def test_q3b_a_commit_that_is_not_an_ancestor_is_recorded(self):
        sh(self.wt, "git", "checkout", "-q", "-b", "side")
        write(os.path.join(self.wt, "side.txt"), "s\n")
        sh(self.wt, "git", "add", "-A")
        sh(self.wt, "git", "commit", "-q", "-m", "side")
        side = sh(self.wt, "git", "rev-parse", "HEAD")
        sh(self.wt, "git", "checkout", "-q", "main")
        code, out = self.pa_run("--commit", side, "--", "sh", "probe.sh")
        self.assert_recorded(code, out, "ancestor")

    def test_q3c_a_refused_argv_stays_unrecorded(self):
        # J1-D10, unchanged
        code, out = self.pa_run("--", "hc", "ls")
        self.assertEqual(code, 125, out)
        self.assertEqual(self.run_rows(), [])
        self.assertNotIn(" pa-run ", self.build_log() or "")


GIT_STALL = r"""#!/bin/sh
# git, except that the first `add` touches $GIT_STALL_MARK and sleeps (in place of git) until it is killed
if [ ! -e "$GIT_STALL_MARK" ]; then
  for a in "$@"; do
    if [ "$a" = add ]; then : > "$GIT_STALL_MARK"; exec sleep 60; fi
  done
fi
exec "$GIT_STALL_REAL" "$@"
"""


class Q4TermInTheTree(Run):
    """R4 (RL-3, Nit-J2-4): TERM while tree_to_run runs (its git add here) ends 143, records `interrupted`, prints
    no traceback, and leaves no scratch folder."""

    def test_q4a_term_during_git_add_is_143_and_recorded(self):
        folder = os.path.join(self.tmp, "stallbin")
        write(os.path.join(folder, "git"), GIT_STALL)
        os.chmod(os.path.join(folder, "git"), 0o755)
        mark = os.path.join(self.tmp, "stalled")
        temp = os.path.join(self.tmp, "runtmp")
        os.makedirs(temp)
        self.dirty()
        env = dict(os.environ, PATH=folder + os.pathsep + os.environ["PATH"], GIT_STALL_MARK=mark,
                   GIT_STALL_REAL=shutil.which("git"), TMPDIR=temp)
        log = os.path.join(self.tmp, "bg.out")
        with open(log, "w") as f:
            p = subprocess.Popen([sys.executable, PA_RUN, "--", "sh", "probe.sh"], cwd=self.wt, env=env,
                                 stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT)
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.wait()))
        self.assertTrue(self.until(lambda: os.path.exists(mark)), pa.read_text(log))
        p.send_signal(signal.SIGTERM)
        self.assertEqual(p.wait(timeout=60), 143, pa.read_text(log))
        out = pa.read_text(log)
        self.assertNotIn("Traceback", out)
        rows = self.run_rows()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["exit"], 143, rows)
        self.assertIn("interrupted", rows[0].get("note") or "", rows)
        self.assertEqual([n for n in os.listdir(temp) if n.startswith("pa-run-tree-")], [])
        self.assertEqual(self.remote_runs(), [])


class Q5ClosedStdout(Run):
    """R5 (RL-4): `pa-run ... | head -1`: the reader goes after the first line; the run goes on to its end."""

    def closed_after_first_line(self, *args):
        go = os.path.join(self.tmp, "go")
        err = os.path.join(self.tmp, "err.out")
        with open(err, "w") as f:
            p = subprocess.Popen([sys.executable, PA_RUN] + list(args) + ["--", "sh", "probe.sh", "wait=" + go,
                                                                         "after", "exit=3"],
                                 cwd=self.wt, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=f)
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.wait()))
        first = p.stdout.readline()
        p.stdout.close()  # the reader is gone
        write(go, "")
        code = p.wait(timeout=60)
        return code, first.decode(), pa.read_text(err)

    def check(self, code, first, err, place):
        self.assertTrue(first.startswith("cwd "), first)
        self.assertEqual(code, 3, err)
        self.assertNotIn("Traceback", err)
        self.assertNotIn("BrokenPipeError", err)
        run = self.run_id(err)
        self.assertIn("arg [after]", self.run_out(run))  # the whole output, after the reader went
        log = [l for l in self.build_log().splitlines() if " pa-run " in l]
        self.assertEqual(len(log), 1, self.build_log())
        self.assertIn("-> exit 3", log[0])
        rows = self.run_rows()
        self.assertEqual([(r["run"], r["exit"], r["place"]) for r in rows], [(run, 3, place)], rows)
        self.assertEqual(self.progress(run).get("state"), "done")
        return run

    def test_q5a_a_local_run(self):
        code, first, err = self.closed_after_first_line("--local")
        self.check(code, first, err, "local")

    def test_q5b_a_remote_run(self):
        code, first, err = self.closed_after_first_line()
        run = self.check(code, first, err, "box-1")
        self.assertFalse(os.path.exists(self.remote_folder(run)))
        self.assertEqual([n for n in os.listdir(os.path.join(self.stub, "box-1", "terminals")) if n.endswith(".json")],
                         [])


class Q5RemoteError(Run):
    """R5's catch-all in run_remote (VERIFY MF-1, spec V1/A5): an error there that is neither Interrupted nor
    SetupFailed (here the watcher's last read raises, after the run ended there) ends and cleans the run on the
    machine, writes one record (build.log and test-runs.jsonl, exit 1, never 0) and is raised again."""

    def test_q5c_an_error_in_run_remote_is_cleaned_recorded_and_raised(self):
        real = pa_hooks.load_tool

        def broken_final(watcher):
            raise RuntimeError("the last read broke")

        def load_tool(name):
            module = real(name)
            if name == "pa_run":
                real_load = module.load

                def load(h):
                    s = real_load(h)
                    s.RemoteWatcher = type("BrokenWatcher", (s.RemoteWatcher,), {"final": broken_final})
                    return s
                module.load = load
            return module
        out, saved, sys.argv = io.StringIO(), sys.argv, ["pa-run", "--", "sh", "probe.sh"]
        try:
            with mock.patch.object(pa_hooks, "load_tool", load_tool), contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(out):
                with self.assertRaises(RuntimeError) as caught:
                    pa_hooks.main()
        finally:
            sys.argv = saved
        text = out.getvalue()
        self.assertIn("the last read broke", str(caught.exception))
        run = self.run_id(text)
        self.assertTrue(self.remote_runs(), self.calls())  # it ran there
        self.assertFalse(os.path.exists(self.remote_folder(run)), text)
        self.assertEqual([n for n in os.listdir(os.path.join(self.stub, "box-1", "terminals")) if n.endswith(".json")],
                         [])
        rows = self.run_rows()
        self.assertEqual([(r["run"], r["exit"], r["place"]) for r in rows], [(run, 1, "box-1")], rows)
        self.assertIn("pa-run failed: RuntimeError: the last read broke", rows[0].get("note") or "", rows)
        log = [l for l in (self.build_log() or "").splitlines() if " pa-run " in l]
        self.assertEqual(len(log), 1, self.build_log())
        self.assertIn("-> exit 1 ", log[0])
        self.assertEqual(self.progress(run).get("state"), "done")


class Q6CleanFilter(Run):
    """R6 (RL-5): tree_to_run never runs a repo's clean filter (it could write under .git): a path that needs one
    is 125, naming the path and the filter; a filter attribute with no driver configured is no filter."""

    def setUp(self):
        super().setUp()
        write(os.path.join(self.wt, ".gitattributes"), "*.bin filter=mark\n")
        write(os.path.join(self.wt, "x.bin"), "x old\n")
        sh(self.wt, "git", "add", "-A")
        sh(self.wt, "git", "commit", "-q", "-m", "a filtered file")  # before the filter is configured
        self.marker = os.path.join(self.wt, ".git", "filter-ran")

    def configure_filter(self):
        import shlex
        sh(self.wt, "git", "config", "filter.mark.clean", "touch {}; cat".format(shlex.quote(self.marker)))

    def test_q6a_a_changed_filtered_file_is_refused_and_nothing_written_under_git(self):
        # AC6.1
        self.configure_filter()
        write(os.path.join(self.wt, "x.bin"), "x new\n")
        before, scratch = git_state(os.path.join(self.wt, ".git")), scratch_trees()
        code, out = self.pa_run("--", "sh", "probe.sh")
        self.assertEqual(code, 125, out)
        self.assertIn("x.bin", out)
        self.assertIn("mark", out)
        self.assertIn("clean filter", out)
        self.assertFalse(os.path.exists(self.marker), "the clean filter ran")
        self.assertEqual(git_state(os.path.join(self.wt, ".git")), before)
        self.assertEqual(scratch_trees(), scratch)
        self.assertEqual(self.remote_runs(), [])

    def test_q6b_an_attribute_with_no_driver_is_no_filter(self):
        # AC6.2
        write(os.path.join(self.wt, "x.bin"), "x new\n")
        code, out = self.pa_run("--", "sh", "probe.sh", "cat=x.bin")
        self.assertEqual(code, 0, out)
        self.assertIn("file x.bin: x new", out)

    def test_q6c_an_overlay_through_the_filter_is_refused(self):
        # AC6.3
        self.configure_filter()
        write(os.path.join(self.wt, "x.bin"), "x new\n")
        code, out = self.pa_run("--commit", "HEAD", "--overlay", "x.bin", "--", "sh", "probe.sh")
        self.assertEqual(code, 125, out)
        self.assertIn("x.bin", out)
        self.assertIn("mark", out)
        self.assertFalse(os.path.exists(self.marker), "the clean filter ran")
        self.assertEqual(self.remote_runs(), [])


class Q9Exit124(unittest.TestCase):
    """R9 (Nit-1): pa_run.py's Exit line, run-all.sh's exit comment and README's pa-run paragraph list every 124."""
    read = R13Docs.read
    WHY = (("no slot came free", r"no slot came free"), ("the deadline", r"deadline"), ("the machine lost", r"\blost\b"),
           ("the machine's REMOTE_TIMEOUT", r"REMOTE_TIMEOUT|timed out"),
           ("the run limit", r"PA_SUITE_RUN_LIMIT|run limit"))

    def check(self, text, where):
        for what, pattern in self.WHY:
            with self.subTest(where=where, what=what):
                self.assertRegex(text, pattern, "{} does not name {} as a 124: {!r}".format(where, what, text))

    def test_q9a_the_exit_line_the_comment_and_readme(self):
        doc = self.read("runtime", "tools", "pa_run.py")
        self.check(doc[doc.index("Exit:"):doc.index('"""', doc.index("Exit:"))], "pa_run.py's Exit line")
        sh_text = self.read("tests", "run-all.sh")
        self.check(sh_text[sh_text.index("# Exit:"):sh_text.index("# Node:")], "run-all.sh's exit comment")
        readme = self.read("README.md")
        start = readme.index("124:", readme.index("pa-run -- <command>"))
        self.check(readme[start:readme.index("Each run is one line", start)], "README's pa-run paragraph")


class Q10Cwd(Run):
    """R10 (Nit-2): --cwd that is not a folder in the tree to run is 125 before anything runs."""

    def test_q10a_a_folder_of_ignored_files_on_a_machine(self):
        write(os.path.join(self.wt, ".gitignore"), ".claude/\nbuild/\n")
        sh(self.wt, "git", "commit", "-q", "-am", "ignore build")
        write(os.path.join(self.wt, "build", "out.txt"), "made\n")
        code, out = self.pa_run("--cwd", "build", "--", "sh", "../probe.sh")
        self.assertEqual(code, 125, out)
        self.assertIn("--cwd build is not a folder in the tree to run", out)
        self.assertEqual(self.remote_runs(), [])
        self.assertFalse([c for c in self.calls() if c[:2] == ["file", "write"]], self.calls())

    def test_q10b_a_missing_folder_locally(self):
        code, out = self.pa_run("--local", "--cwd", "nope", "--", "sh", "../probe.sh")
        self.assertEqual(code, 125, out)
        self.assertIn("--cwd nope is not a folder in the tree to run", out)

    def test_q10c_a_remote_run_with_a_worktree_change(self):
        # REVIEW MF-1 (1): the tree to run is not one of the repo's own trees
        self.dirty()
        write(os.path.join(self.wt, "sub", "s.txt"), "in sub, changed\n")
        code, out = self.pa_run("--cwd", "sub", "--", "sh", "../probe.sh", "cat=s.txt", "cat=../a.txt")
        self.assertEqual(code, 0, out)
        self.assertIn("file s.txt: in sub, changed", out)
        self.assertIn("file ../a.txt: a changed", out)
        self.assertRegex(out, r"cwd /\S*/w/sub")
        self.assertIn("remote 1", out)

    def test_q10d_a_new_folder_of_the_worktree_on_a_machine(self):
        write(os.path.join(self.wt, "fresh", "f.txt"), "fresh\n")
        code, out = self.pa_run("--cwd", "fresh", "--", "sh", "../probe.sh", "cat=f.txt")
        self.assertEqual(code, 0, out)
        self.assertIn("file f.txt: fresh", out)
        self.assertIn("remote 1", out)

    def test_q10e_commit_and_overlay_on_a_machine(self):
        # REVIEW MF-1 (2)
        base = sh(self.wt, "git", "rev-parse", "HEAD")
        write(os.path.join(self.wt, "sub", "new.txt"), "x\n")
        code, out = self.pa_run("--commit", base, "--overlay", "sub/new.txt", "--cwd", "sub", "--", "sh", "../probe.sh",
                                "cat=new.txt", "cat=s.txt")
        self.assertEqual(code, 0, out)
        self.assertIn("file new.txt: x", out)
        self.assertIn("file s.txt: in sub", out)
        self.assertIn("remote 1", out)

    def test_q10f_commit_and_overlay_locally(self):
        # REVIEW MF-1: the same check in run_local's --commit path
        base = sh(self.wt, "git", "rev-parse", "HEAD")
        write(os.path.join(self.wt, "nsub", "new.txt"), "y\n")
        code, out = self.pa_run("--local", "--commit", base, "--overlay", "nsub/new.txt", "--cwd", "nsub", "--", "sh",
                                "../probe.sh", "cat=new.txt")
        self.assertEqual(code, 0, out)
        self.assertIn("file new.txt: y", out)
        self.assertIn("remote unset", out)


class Q11TooLarge(Run):
    """R11 (Nit-3): a patch too large to send goes the way of a setup failure: this Mac with "mac", saying why;
    125 with "none" (r6d)."""

    def test_q11a_too_large_with_mac_runs_here_and_says_why(self):
        with open(os.path.join(self.wt, "big.bin"), "wb") as f:
            f.write(os.urandom(5 * 1024 * 1024))
        code, out = self.pa_run("--", "sh", "probe.sh")
        self.assertEqual(code, 0, out)
        self.assertIn("this Mac", out)
        self.assertIn("too large", out)
        self.assertIn("remote unset", out)
        row = self.run_rows()[-1]
        self.assertEqual(row["place"], "local")
        self.assertIn("too large", row.get("note") or "")
        self.assertEqual(self.remote_runs(), [])


class Q12LiteralOverlay(Run):
    """R12 (Nit-5): --overlay words are literal paths (git add --literal-pathspecs)."""

    def test_q12a_a_glob_that_names_no_file_is_refused(self):
        write(os.path.join(self.wt, "tests", "t1.py"), "old\n")
        sh(self.wt, "git", "add", "-A")
        sh(self.wt, "git", "commit", "-q", "-m", "t1")
        write(os.path.join(self.wt, "tests", "t1.py"), "new\n")  # a glob would take it
        code, out = self.pa_run("--commit", "HEAD", "--overlay", "tests/*.py", "--", "sh", "probe.sh")
        self.assertEqual(code, 125, out)
        self.assertIn("tests/*.py", out)
        self.assertEqual(self.remote_runs(), [])

    def test_q12b_a_name_with_a_star_takes_only_that_file(self):
        for name in ("a*.py", "ab.py"):
            write(os.path.join(self.wt, name), "old\n")
        sh(self.wt, "git", "add", "-A")
        sh(self.wt, "git", "commit", "-q", "-m", "two")
        for name in ("a*.py", "ab.py"):
            write(os.path.join(self.wt, name), "new\n")
        code, out = self.pa_run("--commit", "HEAD", "--overlay", "a*.py", "--", "sh", "probe.sh", "cat=a*.py",
                                "cat=ab.py")
        self.assertEqual(code, 0, out)
        self.assertIn("file a*.py: new", out)
        self.assertIn("file ab.py: old", out)


class Q13NoUsableMachine(Run):
    """R13 (Nit-6): suiteMachines set with no usable entry: 125 with "none", naming the bad entries; this Mac with
    "mac", saying why."""

    def test_q13a_none_is_125(self):
        self.configure([{"machine": "bad name!"}])
        set_fallback("none")
        code, out = self.pa_run("--", "sh", "probe.sh")
        self.assertEqual(code, 125, out)
        self.assertIn("suiteMachines in ", out)
        self.assertIn("bad name!", out)
        self.assertIn("no usable machine in suiteMachines", out)
        self.assertNotIn("cwd ", out)
        self.assertEqual(self.calls(), [])

    def test_q13b_mac_runs_here(self):
        self.configure([{"machine": "bad name!"}])
        code, out = self.pa_run("--", "sh", "probe.sh")
        self.assertEqual(code, 0, out)
        self.assertIn("remote unset", out)
        self.assertIn("bad name!", out)


class J1Dead(Run):
    """t-103 R5 (AC5.2): the run's sh there killed without writing its code: pa-run ends within a few polls with 124
    (as a lost machine) and the note, not after REMOTE_TIMEOUT; cleaned and recorded."""

    def test_j1a_a_killed_remote_run_ends_at_once_with_124(self):
        go = os.path.join(self.tmp, "never")
        self.addCleanup(lambda: write(go, "go\n"))
        log = os.path.join(self.tmp, "bg.out")
        with open(log, "w") as f:
            p = subprocess.Popen([sys.executable, PA_RUN, "--", "sh", "probe.sh", "wait=" + go], cwd=self.wt,
                                 stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT)
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.wait()))
        self.assertTrue(self.until(lambda: RUN.search(pa.read_text(log) or "")), pa.read_text(log))
        run = self.run_id(pa.read_text(log))
        self.assertTrue(self.until(lambda: os.path.exists(os.path.join(self.remote_folder(run), "t0"))))
        pid = self.until(lambda: tpr.remote_sh(self.stub))
        self.assertTrue(pid)
        os.kill(pid, signal.SIGKILL)
        try:
            code = p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.fail("pa-run still waits after the run there was killed: " + (pa.read_text(log) or ""))
        self.assertEqual(code, 124, pa.read_text(log))
        self.assertIn("the run on box-1 ended without an exit code after", pa.read_text(log))
        row = self.run_rows()[-1]
        self.assertEqual((row["run"], row["exit"], row["place"]), (run, 124, "box-1"))
        self.assertIn("ended without an exit code", row.get("note") or "")
        self.assertFalse(os.path.exists(self.remote_folder(run)))
        self.assertEqual(self.progress(run).get("exit"), 124)


class J1Leftover(Run):
    """t-103 R7 (AC7.1, AC7.2) for pa-run: setup fails and the rm -rf fails: `leftover`, not `cleaned`, and the next
    run on the machine removes the folder; a cleanup that worked is `cleaned`, no `leftover`."""

    def test_j1b_a_failed_cleanup_is_left_for_the_next_run(self):
        self.setenv(STUB_HC_FAIL="match:clone,match:rm -rf")
        code, out = self.pa_run("--", "sh", "probe.sh")
        self.assertEqual(code, 0, out)
        run = self.run_id(out)
        self.assertIn("this Mac", out)
        row = self.progress(run)
        self.assertNotEqual(row.get("cleaned"), True, row)
        self.assertEqual(row.get("leftover"), {"machine": "box-1", "folder": "/tmp/pa-suite-" + run, "terminal": None})
        self.assertTrue(os.path.isdir(self.remote_folder(run)))
        self.setenv(STUB_HC_FAIL=None)
        code, out = self.pa_run("--", "sh", "probe.sh")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.run_rows()[-1]["place"], "box-1")
        self.assertFalse(os.path.exists(self.remote_folder(run)), out)
        row = self.progress(run)
        self.assertIs(row.get("cleaned"), True, row)
        self.assertFalse(row.get("leftover"), row)

    def test_j1c_a_cleanup_that_worked_is_cleaned(self):
        self.setenv(STUB_HC_FAIL="match:clone")
        code, out = self.pa_run("--", "sh", "probe.sh")
        self.assertEqual(code, 0, out)
        row = self.progress(self.run_id(out))
        self.assertIs(row.get("cleaned"), True, row)
        self.assertFalse(row.get("leftover"), row)


if __name__ == "__main__":
    unittest.main()
