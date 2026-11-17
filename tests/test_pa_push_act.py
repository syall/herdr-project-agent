"""pa-push-main's act: the merge and the push (t-007 part 7b, A4).

Run: python3 -m unittest -v tests/test_pa_push_act.py
The fixture (temp homes, the bare origin under a temp folder, the person-only check's injected environment) is
tests/push_fixtures.py; see its docstring. Split from tests/test_pa_push.py by t-030 so the parts run at once.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from push_fixtures import *  # noqa: F401,F403 (the shared fixture and helpers)
from push_fixtures import setUpModule, tearDownModule  # noqa: F401 (the module's temp homes and environment)

# ---- A4: the act --------------------------------------------------------------------------------

class Act(Runs):
    def assertPushed(self, code, r, merged=True, how="pushed"):
        self.assertEqual(code, 0, r)
        self.assertTrue(r["ok"] and r["done"], r)
        self.assertEqual(self.origin_head(), self.task_commit)
        self.assertEqual(self.head(self.main, "refs/heads/main"), self.task_commit)
        c = self.card()
        self.assertEqual((c["status"], c["answer"], c["answeredBy"], c["push"]["done"]), ("answered", "Push", "person, in the panel", True))
        res = c["push"]["result"]
        self.assertEqual((res["from"], res["to"], res["url"], res["branch"], res["merged"], res["how"]),
                         (self.origin_before, self.task_commit, self.url, "main", merged, how))
        self.assertTrue(res["at"].endswith("Z"))
        return res

    def test_x01_push_with_fast_forward(self):
        code, r = self.run_push(note="ship it")
        self.assertPushed(code, r)
        self.assertEqual(self.card()["note"], "ship it")
        self.assertEqual(len(git_calls(self.calls, "merge")), 1)
        self.assertEqual(len(git_calls(self.calls, "push")), 1)
        self.assertIn("pushed", r["message"])

    def test_x02_push_without_merge(self):
        sh(self.main, "git", "merge", "-q", "--ff-only", "t-1-x")
        code, r = self.run_push(seen=FRESH)
        self.assertPushed(code, r, merged=False)
        self.assertEqual(git_calls(self.calls, "merge"), [])

    def test_x03_second_run_pushes_nothing(self):
        self.assertPushed(*self.run_push())
        self.calls[:] = []
        code, r = self.run_push(seen="anything")
        self.assertEqual((code, r["ok"], r["done"]), (0, True, True))
        self.assertIn("already pushed", r["message"])
        self.assertEqual(self.calls, [])

    def test_x04_already_on_remote_is_success(self):
        sh(self.main, "git", "push", "-q", "origin", "t-1-x:main")
        res = self.assertPushed(*self.run_push(), how="already on the remote")
        self.assertEqual(git_calls(self.calls, "push"), [])
        self.assertTrue(res["merged"])

    def test_x05_not_now_runs_no_git(self):
        code, r = self.run_push(answer="Not now", note="later")
        self.assertEqual((code, r["ok"], r["done"], r["answer"]), (0, True, False, "Not now"))
        self.assertEqual(self.calls, [])
        c = self.card()
        self.assertEqual((c["status"], c["answer"], c["note"], c["answeredBy"], c["push"]["done"]), ("answered", "Not now", "later", "person, in the panel", False))
        self.assertEqual(self.origin_head(), self.origin_before)
        self.assertEqual(self.journal()[-1]["why"], "answered Not now")

    def test_x06_server_rejects(self):
        hook = os.path.join(self.origin, "hooks", "pre-receive")
        write(hook, "#!/bin/sh\necho protected branch >&2\nexit 1\n")
        os.chmod(hook, 0o755)
        code, r = self.run_push()
        self.assertEqual((code, r["refusal"]["check"]), (1, "push"), r)
        self.assertIn("failed: git push was refused", r["refusal"]["message"])
        self.assertIn("fast-forwarded by this run", r["refusal"]["message"])
        self.assertEqual(self.origin_head(), self.origin_before)
        self.assertEqual(self.head(self.main, "refs/heads/main"), self.task_commit)  # the local fast-forward stays
        c = self.card()
        self.assertEqual((c["status"], c["push"]["done"], len(c["push"]["attempts"])), ("open", False, 1))

    def test_x07_prepush_gets_pristine_clone(self):
        out = os.path.join(self.tmp, "seen")
        os.makedirs(out)
        script = 'ls -a "$1" > "$2/ls.txt"; echo "$1|$3|$4|$5|$6" > "$2/args.txt"; pwd -P > "$2/pwd.txt"'
        self.replan(prePush=["/bin/sh", "-c", script, "sh", "{path}", out, "{repo}", "{commit}", "{url}", "{branch}"])
        write(os.path.join(self.wt, "junk.txt"), "untracked\n")
        self.assertPushed(*self.run_push())
        with open(os.path.join(out, "ls.txt")) as f:
            listing = f.read().split()
        self.assertIn("a.txt", listing)
        self.assertIn("b.txt", listing)
        self.assertNotIn("junk.txt", listing)
        with open(os.path.join(out, "args.txt")) as f:
            clone, repo, commit, url, branch = f.read().strip().split("|")
        self.assertEqual((repo, commit, url, branch), (self.main, self.task_commit, self.url, "main"))
        self.assertNotIn(clone, (self.main, self.wt))
        self.assertFalse(os.path.exists(clone), "the clone is removed")
        with open(os.path.join(out, "pwd.txt")) as f:
            self.assertEqual(os.path.realpath(f.read().strip()), os.path.realpath(clone))

    def test_x08_repo_hooks_never_run(self):
        marks = os.path.join(self.tmp, "marks")
        os.makedirs(marks)
        for name in ("pre-push", "post-merge", "reference-transaction", "post-checkout", "pre-merge-commit", "push-to-checkout"):
            hook = os.path.join(self.main, ".git", "hooks", name)
            write(hook, "#!/bin/sh\ntouch {}/{}\n".format(marks, name))
            os.chmod(hook, 0o755)
        self.assertPushed(*self.run_push())
        self.assertEqual(os.listdir(marks), [])

    def test_x09_git_env_dropped(self):
        planted = (("GIT_DIR", "/nonexistent"), ("GIT_WORK_TREE", "/nonexistent"), ("GIT_SSH_COMMAND", "/bin/false"))
        for k, v in planted:
            os.environ[k] = v
            self.addCleanup(os.environ.pop, k, None)
        result = self.run_push()
        for k, _v in planted:  # the fixture's own git calls below must not see them either
            os.environ.pop(k, None)
        self.assertPushed(*result)
        env = pa_push.clean_env({"GIT_DIR": "x", "git_dir": "y", "Git_Config_Parameters": "z", "PATH": "/bin", "HOME": "/h"})
        self.assertEqual(env, {"PATH": "/bin", "HOME": "/h", "GIT_TERMINAL_PROMPT": "0"})

    def test_x10_argv_never_forbidden(self):
        self.replan(prePush=[sys.executable, "-c", "pass", "{path}"])
        self.assertPushed(*self.run_push())
        self.assertTrue(self.calls)
        for a in self.calls:
            if a[:1] != ["git"]:
                continue
            bad = [w for w in a if w in FORBIDDEN or w.startswith("--force")]
            self.assertEqual(bad, [], a)
            hooks = [w for w in a if w.startswith("core.hooksPath=")]
            self.assertEqual(len(hooks), 1, a)
            self.assertFalse(os.path.exists(hooks[0].split("=", 1)[1]), "the hooks folder is removed afterwards")
        pushes = git_calls(self.calls, "push")
        self.assertEqual(len(pushes), 1)
        p = pushes[0]
        self.assertEqual(p[p.index("push"):], ["push", "--porcelain", "--no-follow-tags", "--recurse-submodules=no", self.url,
                                               self.task_commit + ":refs/heads/main"])
        self.assertFalse(any(w.startswith("+") for w in p))
        self.assertNotIn("origin", p)

    def test_x11_two_runs_in_threads_one_push(self):
        results = []
        seen = self.seen

        def one():
            results.append(pa_push.run_push(SPACE, CARD, "Push", None, seen, PERSON_ENV, PANEL_TREE, None))

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            threads = [threading.Thread(target=one) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(len(git_calls(self.calls, "push")), 1)
        self.assertEqual(self.origin_head(), self.task_commit)
        codes = sorted(code for code, _r in results)
        self.assertEqual(codes[0], 0)
        for code, r in results:
            self.assertTrue(code == 0 or r["refusal"]["check"] == "C2", r)

    def test_x12_journal_lines(self):
        self.assertPushed(*self.run_push())
        line = self.journal()[-1]
        c7, r7 = self.task_commit[:7], self.origin_before[:7]
        self.assertEqual((line["by"], line["trigger"], line["task"], line["decision"]), ("person", "panel", TASK, CARD))
        self.assertEqual(line["why"], "pushed {} main {}..{} (1 commits)".format(self.url, r7, c7))
        p = line["push"]
        self.assertEqual(set(p), {"path", "url", "branch", "from", "to", "merged", "allowSha", "allowMtime", "gateAt"})
        self.assertEqual((p["from"], p["to"], p["merged"], p["allowSha"]), (self.origin_before, self.task_commit, True, self.card()["push"]["allowSha"]))

    def test_x13_killed_after_push_recovers(self):
        real = pa_push.save_card

        def killed(directory, card):
            if card.get("status") == "answered":
                raise RuntimeError("killed before the card write")
            return real(directory, card)

        pa_push.save_card = killed
        try:
            with self.assertRaises(RuntimeError):
                self.run_push()
        finally:
            pa_push.save_card = real
        self.assertEqual(self.origin_head(), self.task_commit)
        self.assertEqual(self.card()["status"], "open")
        self.calls[:] = []
        self.assertPushed(*self.run_push(), merged=False, how="already on the remote")
        self.assertEqual(git_calls(self.calls, "push"), [])

if __name__ == "__main__":
    unittest.main()
