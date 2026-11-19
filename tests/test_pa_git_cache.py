"""Unit tests for the per-run cache of object-addressed git answers (t-094 J2: R3, R6, R7, AC3.1-AC3.5, AC10.3).

Run: python3 -m unittest -v tests/test_pa_git_cache.py
Every case runs in scratch repositories under a mkdtemp folder and a fake space (tests/queue_fixtures.py): no hc
call, no real home, space, queue.json or push. The only remote is a bare repository under that folder (file://).
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from queue_fixtures import *  # noqa: F401,F403
from queue_fixtures import setUpModule, tearDownModule  # noqa: F401
import test_pa_queue_merge as merge_tests  # noqa: E402 (MBase: the merge fixture; its tests are not collected here)

NEVER = ("status", "symbolic-ref", "config", "worktree", "unpack-file", "merge-file", "HEAD")


class Counting:
    """A runner with git()'s signature that records every argv it runs (after the path)."""

    def __init__(self, inner=None, before=None):
        self.inner = inner or pa_hooks.git
        self.before = before
        self.calls = []

    def __call__(self, path, *args, **kw):
        self.calls.append(tuple(args))
        if self.before:
            self.before(path, list(args))
        return self.inner(path, *args, **kw)

    def content(self):
        """The content-path calls (CONTENT_GIT first), without it, and without the paths call."""
        n = len(pa_hooks.CONTENT_GIT)
        return [c[n:] for c in self.calls if c[:n] == pa_hooks.CONTENT_GIT and "--git-path" not in c]

    def having(self, word):
        return [c for c in self.calls if word in c]


class Repo(unittest.TestCase):
    """A plain scratch repository per test (no space)."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(dir=TMP))

    def repo(self, name="r", fmt=None):
        path = os.path.join(self.tmp, name)
        os.makedirs(path)
        sh(path, "git", "init", "-q", "-b", "main", *(["--object-format=" + fmt] if fmt else []))
        self.commit(path, "README", "base\n")
        return path

    def commit(self, path, f, text):
        write(os.path.join(path, f), text)
        sh(path, "git", "add", f)
        sh(path, "git", "commit", "-q", "-m", "change " + f)
        return sh(path, "git", "rev-parse", "HEAD")

    def common(self, path):
        return sh(path, "git", "rev-parse", "--path-format=absolute", "--git-common-dir")


# ---- AC3.1, AC3.3, AC10.3, the exit rules ------------------------------------------------------------------------

class Lookup(Repo):
    def test_ac3_1_the_same_record_twice_in_one_run_runs_git_once(self):
        path = self.repo()
        base = sh(path, "git", "rev-parse", "HEAD")
        self.commit(path, "a", "a\n")
        tip = self.commit(path, "b", "b\n")
        runner, cache = Counting(), pa_hooks.ContentCache()
        run = pa_hooks.content_git(runner, cache)
        first = pa_hooks.content_record(run, path, tip, "main", base)
        made = len(runner.calls)
        second = pa_hooks.content_record(run, path, tip, "main", base)
        self.assertNotIn("error", first)
        self.assertEqual({k: v for k, v in second.items() if k != "at"}, {k: v for k, v in first.items() if k != "at"})
        self.assertEqual(runner.calls[made:], [], "the second record is answered from the run's cache")
        self.assertGreater(cache.hits, 0)
        # And a record without a cache asks git every time, as before.
        plain = Counting()
        run = pa_hooks.content_git(plain)
        pa_hooks.content_record(run, path, tip, "main", base)
        once = len(plain.calls)
        pa_hooks.content_record(run, path, tip, "main", base)
        self.assertEqual(len(plain.calls), 2 * once - 1, "only the paths call is kept without a cache")

    def test_ac3_3_a_grafts_file_made_between_two_cached_calls_refuses_the_second(self):
        path = self.repo()
        tip = sh(path, "git", "rev-parse", "HEAD")
        runner, cache = Counting(), pa_hooks.ContentCache()
        run = pa_hooks.content_git(runner, cache)
        self.assertEqual(run(path, "cat-file", "commit", tip)[0], 0)
        self.assertEqual(len(cache.answers), 1)
        grafts = os.path.join(self.common(path), "info", "grafts")
        write(grafts, "")
        with self.assertRaises(pa_hooks.ContentError) as e:
            run(path, "cat-file", "commit", tip)
        self.assertIn("a grafts file ({})".format(grafts), str(e.exception))
        self.assertEqual(len(runner.having("cat-file")), 1, "the refused call runs no git")
        self.assertEqual(cache.hits, 0, "a refused call is not a hit")

    def test_ac3_3_a_shallow_file_refuses_a_would_be_hit_with_its_fix(self):
        path = self.repo()
        tip = sh(path, "git", "rev-parse", "HEAD")
        run = pa_hooks.content_git(pa_hooks.git, pa_hooks.ContentCache())
        run(path, "cat-file", "commit", tip)
        write(os.path.join(self.common(path), "shallow"), tip + "\n")
        with self.assertRaises(pa_hooks.ContentError) as e:
            run(path, "cat-file", "commit", tip)
        self.assertIn("git fetch --unshallow", str(e.exception))

    def test_ac10_3_an_answer_refused_after_the_call_is_not_stored(self):
        path = self.repo()
        tip = sh(path, "git", "rev-parse", "HEAD")
        grafts = os.path.join(self.common(path), "info", "grafts")
        made = []

        def before(_p, args):
            if "cat-file" in args and not made:
                write(grafts, "")
                made.append(1)
        runner, cache = Counting(before=before), pa_hooks.ContentCache()
        run = pa_hooks.content_git(runner, cache)
        with self.assertRaises(pa_hooks.ContentError):
            run(path, "cat-file", "commit", tip)
        self.assertEqual(cache.answers, {}, "an answer that failed its after-check is never stored")
        os.remove(grafts)
        code, out, _err = run(path, "cat-file", "commit", tip)
        self.assertEqual(code, 0)
        self.assertEqual(len(runner.having("cat-file")), 2, "the next call runs git again")
        self.assertEqual(len(cache.answers), 1)
        self.assertEqual(run(path, "cat-file", "commit", tip)[1], out)
        self.assertEqual(len(runner.having("cat-file")), 2)

    def test_a_failed_call_is_not_stored_and_is_ancestor_one_is(self):
        path = self.repo()
        a = sh(path, "git", "rev-parse", "HEAD")
        b = self.commit(path, "a", "a\n")
        runner, cache = Counting(), pa_hooks.ContentCache()
        run = pa_hooks.content_git(runner, cache)
        missing = "0" * 40
        self.assertNotEqual(run(path, "cat-file", "-e", missing + "^{commit}")[0], 0)
        self.assertNotEqual(run(path, "cat-file", "commit", missing)[0], 0)
        self.assertNotEqual(run(path, "merge-base", "--is-ancestor", missing, b)[0], 0)
        self.assertEqual(cache.answers, {})
        self.assertEqual(run(path, "merge-base", "--is-ancestor", b, a)[0], 1)
        self.assertEqual(run(path, "merge-base", "--is-ancestor", a, b)[0], 0)
        self.assertEqual(len(cache.answers), 2)
        self.assertEqual(run(path, "merge-base", "--is-ancestor", b, a)[0], 1)
        self.assertEqual(len(runner.having("--is-ancestor")), 3)

    def test_a_timeout_or_a_missing_git_is_not_stored(self):
        path = self.repo()
        tip = sh(path, "git", "rev-parse", "HEAD")
        for code in (124, 127, 1):
            def fake(p, *args, **kw):
                if "cat-file" in args:
                    return code, "", "no"
                return pa_hooks.git(p, *args, **kw)
            cache = pa_hooks.ContentCache()
            run = pa_hooks.content_git(fake, cache)
            self.assertEqual(run(path, "cat-file", "commit", tip)[0], code)
            self.assertEqual(cache.answers, {}, code)

    def test_the_key_is_the_common_dir_and_the_argv(self):
        path = self.repo()
        tip = sh(path, "git", "rev-parse", "HEAD")
        wt = os.path.join(self.tmp, "wt")
        sh(path, "git", "worktree", "add", "-q", "-b", "other", wt)
        runner, cache = Counting(), pa_hooks.ContentCache()
        run = pa_hooks.content_git(runner, cache)
        run(path, "cat-file", "commit", tip)
        run(wt, "cat-file", "commit", tip)  # a worktree of the same repository: the same objects
        self.assertEqual(len(runner.having("cat-file")), 1)
        self.assertEqual(list(cache.answers), [(os.path.realpath(self.common(path)), ("cat-file", "commit", tip))])
        other = self.repo("r2")
        run(other, "cat-file", "commit", tip)  # another repository: its own answer (here: no such object)
        self.assertEqual(len(runner.having("cat-file")), 2)

    def test_clear_drops_every_answer(self):
        path = self.repo()
        cache = pa_hooks.ContentCache()
        pa_hooks.content_git(pa_hooks.git, cache)(path, "cat-file", "commit", sh(path, "git", "rev-parse", "HEAD"))
        self.assertEqual(len(cache.answers), 1)
        cache.clear()
        self.assertEqual(cache.answers, {})


# ---- AC3.5: what is never cached ---------------------------------------------------------------------------------

class Shapes(Repo):
    A, B = "a" * 40, "b" * 40

    def test_the_shapes_that_are_cached(self):
        a, b, raw = self.A, self.B, pa_hooks.RAW_DIFF
        c = pa_hooks.cacheable
        self.assertEqual(c(("cat-file", "commit", a), 40), {0})
        self.assertEqual(c(("cat-file", "-e", a + "^{commit}"), 40), {0})
        self.assertEqual(c(raw + (a, b), 40), {0})
        self.assertEqual(c(raw + (a + "^", a), 40), {0})
        self.assertEqual(c(("rev-list", "--reverse", "--parents", a + ".." + b), 40), {0})
        self.assertEqual(c(("rev-list", "--count", a + ".." + b), 40), {0})
        self.assertEqual(c(("rev-parse", "--verify", "-q", a + "^{tree}"), 40), {0})
        self.assertEqual(c(("merge-base", a, b), 40), {0})
        self.assertEqual(c(("merge-base", "--is-ancestor", a, b), 40), {0, 1})
        s = "c" * 64
        self.assertEqual(c(("cat-file", "commit", s), 64), {0})
        self.assertEqual(c(("merge-base", s, s), 64), {0})

    def test_ac3_5_refs_head_status_and_near_misses_are_never_cached(self):
        a, b, raw = self.A, self.B, pa_hooks.RAW_DIFF
        never = [
            ("cat-file", "commit", "main"), ("cat-file", "commit", "HEAD"), ("cat-file", "commit", a[:7]),
            ("cat-file", "commit", "refs/heads/" + a), ("cat-file", "commit", "c" * 64),  # 64 hex in a SHA-1 repo
            ("cat-file", "-e", a), ("cat-file", "-e", "main^{commit}"), ("cat-file", "-p", a), ("cat-file", "blob", a),
            raw + ("main", b), raw + (a, "HEAD"), raw + (a + "^^", a), raw + (a, b + "^"), raw[:-1] + (a, b),
            ("diff", "--raw", a, b), raw + ("--output=/tmp/x", a, b), raw + (a, b, "--", "f"),
            ("rev-list", "--reverse", "--parents", "main.." + b), ("rev-list", a + ".." + b, "--all"),
            ("rev-list", "--reverse", "--parents", a + "..." + b), ("rev-list", "--all", a + ".." + b),
            ("rev-list", "--count", a + ".." + "HEAD"), ("rev-list", "--reverse", "--parents", b),
            ("rev-parse", "--verify", "-q", "refs/heads/main^{commit}"), ("rev-parse", "--verify", "-q", a + "^{commit}"),
            ("rev-parse", "--verify", "-q", "HEAD^{tree}"), ("rev-parse", "--show-toplevel"),
            ("rev-parse", "--path-format=absolute", "--git-common-dir"),
            ("merge-base", a, "refs/heads/main"), ("merge-base", "--is-ancestor", "main", b), ("merge-base", a, b, b),
            ("merge-base", "--all", a, b), ("merge-base", "--octopus", a, b),
            ("--no-optional-locks", "status", "--porcelain=v2"), ("status",), ("symbolic-ref", "--short", "-q", "HEAD"),
            ("config", "--get", "core.hooksPath"), ("worktree", "list", "--porcelain", "-z"),
            ("unpack-file", a), ("merge-file", "-q", "-p", "x", "y", "z"), ("update-ref", "refs/heads/x", a),
            ("diff", "-U0", "--no-color", "--output=/tmp/x", a, b), (),
        ]
        for argv in never:
            self.assertIsNone(pa_hooks.cacheable(argv, 40), argv)
        self.assertIsNone(pa_hooks.cacheable(("cat-file", "commit", a), None), "an unknown object format caches nothing")
        self.assertIsNone(pa_hooks.cacheable(("cat-file", "commit", a), 64), "40 hex in a SHA-256 repository")

    def test_concern_4_a_branch_named_like_a_commit_id_answers_the_object(self):
        path = self.repo()
        a = sh(path, "git", "rev-parse", "HEAD")
        b = self.commit(path, "a", "a\n")
        sh(path, "git", "branch", b, a)  # a branch named b's id, pointing at a: git warns and takes the object
        cache = pa_hooks.ContentCache()
        run = pa_hooks.content_git(pa_hooks.git, cache)
        code, out, _err = run(path, "cat-file", "commit", b)
        self.assertEqual(code, 0)
        self.assertEqual(out, pa_hooks.git(path, "cat-file", "commit", b)[1])
        self.assertIn("change a", out, "the object b, not the branch named b (at a)")
        self.assertEqual(run(path, "cat-file", "commit", b)[1], out)
        self.assertEqual(len(cache.answers), 1)

    def test_concern_4_a_64_hex_branch_in_a_sha1_repository_is_never_cached(self):
        path = self.repo()
        a = sh(path, "git", "rev-parse", "HEAD")
        name = "d" * 64
        sh(path, "git", "branch", name, a)  # in a SHA-1 repository git reads 64 hex as a ref name
        cache = pa_hooks.ContentCache()
        run = pa_hooks.content_git(pa_hooks.git, cache)
        self.assertEqual(run(path, "cat-file", "-e", name + "^{commit}")[0], 0, "git answers from the branch")
        self.assertEqual(run(path, "merge-base", "--is-ancestor", name, a)[0], 0)
        self.assertEqual(cache.answers, {}, "a ref read in disguise is never stored")

    def test_a_sha256_repository_caches_64_hex_and_never_40(self):
        path = self.repo(fmt="sha256")
        a = sh(path, "git", "rev-parse", "HEAD")
        self.assertEqual(len(a), 64)
        sh(path, "git", "branch", "e" * 40, a)
        cache = pa_hooks.ContentCache()
        run = pa_hooks.content_git(pa_hooks.git, cache)
        self.assertEqual(run(path, "cat-file", "commit", a)[0], 0)
        self.assertEqual(len(cache.answers), 1)
        self.assertEqual(run(path, "cat-file", "-e", "e" * 40 + "^{commit}")[0], 0, "git reads it as the branch")
        self.assertEqual(len(cache.answers), 1, "40 hex in a SHA-256 repository is never stored")


# ---- AC3.2, AC3.4, AC3.5, R6, R7: the runs ----------------------------------------------------------------------

class Spy:
    """Records the cache every evaluate_gate call is given, and which module asked."""

    def __init__(self):
        self.real = pa_hooks.evaluate_gate
        self.calls = []

    def __call__(self, *args, **kw):
        caller = os.path.basename(sys._getframe(1).f_code.co_filename)
        self.calls.append((caller, kw.get("cache")))
        return self.real(*args, **kw)


def answers_ok(test, answers):
    """AC3.5: every stored key is an R3 shape on full ids; nothing names a ref, HEAD or a non-object command."""
    for (common, argv), _answer in answers.items():
        test.assertTrue(os.path.isabs(common), common)
        for word in argv:
            test.assertNotIn("refs/", word, argv)
            test.assertFalse(word in NEVER or word.startswith("--output"), argv)
        test.assertIsNotNone(pa_hooks.cacheable(argv, 40), argv)


class Runs(QBase):
    def strip(self, gate):
        return {k: v for k, v in gate.items() if k != "at"}

    def test_ac3_2_two_evaluate_gate_calls_without_a_cache_share_nothing(self):
        self.task("t-A", [("fa.txt", "a\n")])
        self.change("t-A", "fb.txt", "b\n")  # HEAD one commit past the checked one: two records with a shared commit
        task = self.ledger_task("t-A")
        runner = Counting()
        pa_hooks.evaluate_gate(self.dir, task, "review", runner=runner)
        first = runner.content()
        repeats = [c for c in first if first.count(c) > 1 and pa_hooks.cacheable(c, 40)]
        self.assertEqual(repeats, [], "one evaluate_gate is one run: an object-addressed call runs once")
        pa_hooks.evaluate_gate(self.dir, task, "review", runner=runner)
        second = runner.content()[len(first):]
        self.assertEqual(second, first, "a second evaluate_gate with no cache asks git again, call for call")

    def test_ac3_2_an_evaluate_gate_given_a_cache_fills_it_and_answers_the_same(self):
        self.task("t-A", [("fa.txt", "a\n")])
        self.change("t-A", "fb.txt", "b\n")
        task = self.ledger_task("t-A")
        cache = pa_hooks.ContentCache()
        g1 = pa_hooks.evaluate_gate(self.dir, task, "review", cache=cache)
        self.assertGreater(len(cache.answers), 0)
        answers_ok(self, cache.answers)
        runner = Counting()
        g2 = pa_hooks.evaluate_gate(self.dir, task, "review", runner=runner, cache=cache)
        g3 = pa_hooks.evaluate_gate(self.dir, task, "review")
        self.assertEqual(self.strip(g1), self.strip(g3))
        self.assertEqual(self.strip(g2), self.strip(g3))
        self.assertEqual([c for c in runner.content() if pa_hooks.cacheable(c, 40)], [],
                         "the same run's second gate asks git no object-addressed call again")

    def test_ac3_4_a_rebase_then_the_gate_in_one_ctx_reads_the_new_tip(self):
        self.task("t-A", [("fa.txt", "a\n")])
        task = self.ledger_task("t-A")
        ctx = pa_queue.Ctx(self.dir, self.proj)
        try:
            self.assertIsInstance(ctx.s.cache, pa_hooks.ContentCache)
            before = pa_hooks.evaluate_gate(self.dir, task, "review", runner=ctx.s.gate_git, cache=ctx.s.cache)
            self.assertTrue(before["passed"], before)
            old = self.tip("t-A")
            self.on_main("b.txt", "main moved\n")
            sh(self.trees["t-A"], "git", "rebase", "-q", "main")
            new = self.tip("t-A")
            self.assertNotEqual(old, new)
            after = pa_hooks.evaluate_gate(self.dir, task, "review", runner=ctx.s.gate_git, cache=ctx.s.cache)
            fresh = pa_hooks.evaluate_gate(self.dir, task, "review", runner=ctx.s.gate_git)
            self.assertEqual(self.strip(after), self.strip(fresh))
            self.assertEqual(after["commits"][0]["commit"], new)
            self.assertEqual(after["content"][0]["base"], self.ref("main"))
            answers_ok(self, ctx.s.cache.answers)
        finally:
            ctx.close()
        self.assertIsNone(ctx.s.cache, "Session.close() drops the run's cache")

    def test_r7_one_cache_per_pa_queue_command(self):
        self.task("t-A", [("fa.txt", "a\n")])
        spy, kept = Spy(), []
        real_close = pa_push.Session.close

        def close(s):
            kept.append(dict(s.cache.answers) if s.cache is not None else None)
            real_close(s)
        with mock.patch.object(pa_hooks, "evaluate_gate", spy), mock.patch.object(pa_push.Session, "close", close):
            self.add("t-A")
            code, out, err = self.queue("show", "--repo", self.proj)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(kept), 2, "one Session per command")
        for answers in kept:
            self.assertIsNotNone(answers)
            answers_ok(self, answers)
        self.assertGreater(len(kept[0]), 0, "add's content calls fill the command's cache")
        self.assertTrue(spy.calls, "add evaluates the gate")
        self.assertTrue(all(c is not None for _m, c in spy.calls), spy.calls)


class MergeRuns(merge_tests.MBase):
    def test_ac3_4_merge_rebases_and_merges_three_with_one_cache(self):
        self.three()
        self.passed("t-A", "t-B", "t-C")
        self.approve()
        spy, closed = Spy(), []
        real_close = pa_push.Session.close

        def close(s):
            closed.append(s)
            if s.cache is not None:
                answers_ok(self, s.cache.answers)
            real_close(s)
        with mock.patch.object(pa_hooks, "evaluate_gate", spy), mock.patch.object(pa_push.Session, "close", close):
            code, out, err = self.merge()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.merged(), ["t-A", "t-B", "t-C"])
        self.assertEqual(self.ref("main"), self.tip("t-C"))
        self.assertEqual(self.entries(), [])
        caches = {id(c) for _m, c in spy.calls}
        self.assertGreaterEqual(len(spy.calls), 3, spy.calls)
        self.assertEqual(len(caches), 1, "every evaluate_gate of the command uses the Ctx's one cache")
        self.assertIsNotNone(spy.calls[0][1])
        self.assertTrue(closed and all(s.cache is None for s in closed), "each Session's cache is dropped at close")

    def test_r6_the_push_plan_gate_gets_its_own_session_cache(self):
        self.allow()
        self.joined("t-A", "fa.txt")
        self.approve()
        self.suite("t-A", 0)
        spy = Spy()
        with mock.patch.object(pa_hooks, "evaluate_gate", spy):
            code, out, err = self.refresh()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.merged(), ["t-A"])
        self.assertEqual(self.card("d-push-t-A-1")["status"], "open", out + err)
        push = [c for m, c in spy.calls if m == "pa_push.py"]
        queue = [c for m, c in spy.calls if m == "pa_queue.py"]
        self.assertEqual(len(push), 1, spy.calls)
        self.assertTrue(queue, spy.calls)
        self.assertIsNotNone(push[0])
        self.assertTrue(all(c is not None and c is not push[0] for c in queue), "the plan is its own run")

    def test_r6_session_close_drops_the_cache(self):
        s = pa_push.Session()
        self.assertIsInstance(s.cache, pa_hooks.ContentCache)
        cache = s.cache
        cache.answers[("x", ("cat-file", "commit", "a" * 40))] = (0, "", "")
        s.close()
        self.assertIsNone(s.cache)
        self.assertEqual(cache.answers, {})


if __name__ == "__main__":
    unittest.main()
