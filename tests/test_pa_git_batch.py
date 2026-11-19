"""Unit tests for the batched content record (t-104: R1-R6, R11).

Run: python3 -m unittest -v tests/test_pa_git_batch.py
Every case runs in scratch repositories under a mkdtemp folder and a fake space (tests/queue_fixtures.py): no hc
call, no real home, space or push, no global or system git config.

A record is made two ways on the same repository: batched (a runner that says it takes stdin) and per call (the
same runner wrapped so it does not), and the two must be equal field by field (R2), on pa_hooks.git and on
pa_push.Session.gate_git.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from queue_fixtures import *  # noqa: F401,F403
from queue_fixtures import setUpModule, tearDownModule  # noqa: F401

P1 = ("the commit objects between {} and {} are not the chain git listed; something changed what git says a "
      "commit's parents are during a call (a grafts or shallow file); the content cannot be compared")


class Rec:
    """A runner that records every argv (after the path) and forwards everything else, input included. It takes
    stdin only when its inner runner does and stdin is not switched off (stdin=False: today's per-call path)."""

    def __init__(self, inner, stdin=True, answer=None):
        self.inner = inner
        self.calls = []
        self.answer = answer  # answer(args, kw, inner_answer_fn) -> answer, to fake failures
        if stdin and getattr(inner, "takes_input", False):
            self.takes_input = True

    def __call__(self, path, *args, **kw):
        self.calls.append(tuple(args))
        if self.answer is not None:
            return self.answer(args, kw, lambda: self.inner(path, *args, **kw))
        return self.inner(path, *args, **kw)

    def content(self):
        """The content calls (CONTENT_GIT first), without CONTENT_GIT and without the paths call."""
        n = len(pa_hooks.CONTENT_GIT)
        return [c[n:] for c in self.calls if c[:n] == pa_hooks.CONTENT_GIT and "--git-path" not in c]

    def batches(self):
        return [c for c in self.content() if "--batch" in c or "--stdin" in c]


def same(record):
    return {k: v for k, v in record.items() if k != "at"}


def write_bytes(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


class Repo(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(dir=TMP))
        self.sessions = []

    def tearDown(self):
        for s in self.sessions:
            s.close()

    def repo(self, name="r"):
        path = os.path.join(self.tmp, name)
        os.makedirs(path)
        sh(path, "git", "init", "-q", "-b", "main")
        self.commit(path, "README", "base\n")
        return path

    def commit(self, path, f, text, message=None):
        write(os.path.join(path, f), text)
        sh(path, "git", "add", "-A")
        sh(path, "git", "commit", "-q", "-m", message or "change " + f)
        return self.head(path)

    def head(self, path):
        return sh(path, "git", "rev-parse", "HEAD")

    def common(self, path):
        return sh(path, "git", "rev-parse", "--path-format=absolute", "--git-common-dir")

    def runners(self):
        """(name, runner) for both real runners."""
        s = pa_push.Session()
        self.sessions.append(s)
        return [("pa_hooks.git", pa_hooks.git), ("Session.gate_git", s.gate_git)]

    def chain(self, path, n=3):
        base = self.head(path)
        for i in range(n):
            self.commit(path, "f{}".format(i), "{}\n".format(i))
        return base, self.head(path)

    def both(self, inner, path, tip, onto="main", base=None, answer=None, cache=None):
        """(batched record, its Rec, per-call record, its Rec) of one record on one inner runner."""
        batched = Rec(inner, answer=answer)
        percall = Rec(inner, stdin=False, answer=answer)
        a = pa_hooks.content_record(pa_hooks.content_git(batched, cache), path, tip, onto, base)
        b = pa_hooks.content_record(pa_hooks.content_git(percall, cache), path, tip, onto, base)
        return a, batched, b, percall


# ---- R1: four content calls per record ------------------------------------------------------------------------

class Calls(Repo):
    def test_ac1_1_a_three_commit_record_makes_four_content_calls(self):
        path = self.repo()
        base, tip = self.chain(path, 3)
        for name, inner in self.runners():
            with self.subTest(runner=name):
                rec = Rec(inner)
                record = pa_hooks.content_record(pa_hooks.content_git(rec), path, tip, "main", base)
                self.assertNotIn("error", record)
                self.assertEqual(len(record["commits"]), 3)
                calls = rec.content()
                self.assertEqual(len(calls), 4, calls)
                self.assertEqual(calls[0][:2], ("merge-base", "--is-ancestor"))
                self.assertEqual(calls[1][0], "rev-list")
                self.assertEqual(sorted(c[0] for c in calls[2:]), ["cat-file", "diff-tree"])
                self.assertEqual(record["tree"], sh(path, "git", "rev-parse", tip + "^{tree}"))

    def test_ac1_1_a_computed_base_also_makes_four(self):
        path = self.repo()
        sh(path, "git", "checkout", "-q", "-b", "topic")
        _base, tip = self.chain(path, 3)
        rec = Rec(pa_hooks.git)
        record = pa_hooks.content_record(pa_hooks.content_git(rec), path, tip, "main")
        self.assertNotIn("error", record)
        self.assertEqual(len(rec.content()), 4, rec.content())

    def test_ac1_2_the_same_record_again_with_the_cache_makes_no_git_call(self):
        path = self.repo()
        base, tip = self.chain(path, 3)
        for name, inner in self.runners():
            with self.subTest(runner=name):
                rec, cache = Rec(inner), pa_hooks.ContentCache()
                run = pa_hooks.content_git(rec, cache)
                first = pa_hooks.content_record(run, path, tip, "main", base)
                made = len(rec.calls)
                second = pa_hooks.content_record(run, path, tip, "main", base)
                self.assertNotIn("error", first)
                self.assertEqual(same(second), same(first))
                self.assertEqual(rec.calls[made:], [], "the second record is answered from the run's cache")
                self.assertEqual(len(rec.batches()), 2)


# ---- R2: answers unchanged --------------------------------------------------------------------------------------

class Oracle(Repo):
    def build(self):
        """One repository holding every case of AC2.1 on main; returns (path, base, [tips to record])."""
        path = self.repo()
        base = self.head(path)
        tips = []

        def message_commit(data, allow_empty=True):
            write_bytes(os.path.join(self.tmp, "msg"), data)
            sh(path, "git", "add", "-A")
            sh(path, "git", "commit", "-q", "--allow-empty", "--allow-empty-message", "--cleanup=verbatim",
               "-F", os.path.join(self.tmp, "msg"))
            tips.append(self.head(path))
        write(os.path.join(path, "crlf"), "x\r\ny\n")
        message_commit(b"subject\r\n\r\nbody line\r\nmore\r\n")
        write(os.path.join(path, "lone"), "z\n")
        message_commit(b"lone\rcarriage\r")
        write(os.path.join(path, "spaces"), "s\n")
        message_commit(b"trailing   \n\n\n   \n")
        message_commit(b"")  # an empty commit with an empty message
        for name in ("new\nline", "tab\there", "cr\rhere", " leading space", "unicodé ü", "a" * 40,
                     ":colon", "trailing space "):
            write(os.path.join(path, "paths", name), name + "\n")
        message_commit(b"odd paths")
        write_bytes(os.path.join(path, "bin"), bytes(range(256)) * 4)
        message_commit(b"binary")
        os.chmod(os.path.join(path, "crlf"), 0o755)
        message_commit(b"mode change")
        os.symlink("crlf", os.path.join(path, "link"))
        message_commit(b"symlink")
        os.remove(os.path.join(path, "lone"))
        os.symlink("spaces", os.path.join(path, "lone"))
        message_commit(b"file to symlink")
        os.remove(os.path.join(path, "link"))
        write(os.path.join(path, "link"), "now a file\n")
        message_commit(b"symlink to file")
        os.remove(os.path.join(path, "spaces"))
        message_commit(b"delete")
        sub = base  # a gitlink to any commit id; no submodule folder is needed
        sh(path, "git", "update-index", "--add", "--cacheinfo", "160000,{},mod".format(sub))
        sh(path, "git", "commit", "-q", "-m", "gitlink added")
        tips.append(self.head(path))
        sh(path, "git", "update-index", "--force-remove", "mod")
        sh(path, "git", "update-index", "--add", "--cacheinfo", "160000,{},moved/mod".format(sub))
        sh(path, "git", "commit", "-q", "-m", "gitlink moved")
        tips.append(self.head(path))
        return path, base, tips

    def test_ac2_1_the_batched_record_equals_the_per_call_record(self):
        path, base, tips = self.build()
        for name, inner in self.runners():
            for tip in [tips[-1], tips[0], tips[len(tips) // 2]]:
                with self.subTest(runner=name, tip=tip[:7]):
                    a, batched, b, percall = self.both(inner, path, tip, base=base)
                    self.assertNotIn("error", b)
                    self.assertEqual(same(a), same(b))
                    self.assertEqual(len(batched.batches()), 2, batched.content())
                    self.assertEqual(percall.batches(), [])
            with self.subTest(runner=name, case="computed base"):
                sh(path, "git", "branch", "-f", "old", base)
                a, batched, b, _p = self.both(inner, path, tips[-1], onto="old")
                self.assertEqual(same(a), same(b))
                self.assertEqual(a["base"], base)
            with self.subTest(runner=name, case="n = 0"):
                a, batched, b, _p = self.both(inner, path, tips[-1], base=tips[-1])
                self.assertNotIn("error", b)
                self.assertEqual(same(a), same(b))
                self.assertEqual(a["commits"], [])
                self.assertEqual(a["tree"], sh(path, "git", "rev-parse", tips[-1] + "^{tree}"))

    def test_ac2_1_a_non_utf8_message_gives_the_same_error(self):
        path = self.repo()
        base = self.head(path)
        self.commit(path, "a", "a\n")
        write_bytes(os.path.join(self.tmp, "msg"), b"caf\xe9 \xff\xfe\n")
        write(os.path.join(path, "b"), "b\n")
        sh(path, "git", "add", "-A")
        sh(path, "git", "-c", "i18n.commitEncoding=latin-1", "commit", "-q", "-F", os.path.join(self.tmp, "msg"))
        tip = self.commit(path, "c", "c\n")
        for name, inner in self.runners():
            with self.subTest(runner=name):
                a, batched, b, _p = self.both(inner, path, tip, base=base)
                self.assertIn("error", b)
                self.assertIn("UnicodeDecodeError", b["error"])
                self.assertEqual(a, b)
                self.assertEqual(len(batched.batches()), 2, batched.content())

    def test_ac2_1_a_non_utf8_path_gives_the_same_error(self):
        path = self.repo()
        base = self.head(path)
        try:
            write_bytes(os.path.join(os.fsencode(path), b"caf\xe9"), b"x\n")
        except OSError:
            self.skipTest("the file system takes no non-UTF-8 name")
        sh(path, "git", "add", "-A")
        sh(path, "git", "commit", "-q", "-m", "latin-1 name")
        tip = self.commit(path, "c", "c\n")
        for name, inner in self.runners():
            with self.subTest(runner=name):
                a, _b, b, _p = self.both(inner, path, tip, base=base)
                self.assertEqual(a, b)


# ---- R3: fallback to today's path ------------------------------------------------------------------------------

class Fallback(Repo):
    def fails(self, code, which=("--batch", "--stdin")):
        def answer(args, kw, real):
            if any(w in args for w in which):
                return code, b"", "fatal: made to fail"
            return real()
        return answer

    def test_ac3_1_a_failing_batch_gives_the_per_call_record(self):
        path = self.repo()
        base, tip = self.chain(path, 3)
        for name, inner in self.runners():
            for code in (128, 124, 127):
                for which in (("--batch",), ("--stdin",)):
                    with self.subTest(runner=name, code=code, which=which):
                        a, batched, b, _p = self.both(inner, path, tip, base=base, answer=self.fails(code, which))
                        self.assertNotIn("error", b)
                        self.assertEqual(same(a), same(b))
                        self.assertTrue(batched.batches(), "the batch was tried")
                        self.assertIn(("cat-file", "commit", tip), batched.content(), "then today's calls")

    def test_ac3_1_an_answer_short_of_a_byte_gives_the_per_call_record(self):
        path = self.repo()
        base, tip = self.chain(path, 3)
        for which in ("--batch", "--stdin"):
            for cut in ("first", "last"):
                def answer(args, kw, real, which=which, cut=cut):
                    code, out, err = real()
                    if which in args:
                        out = out[1:] if cut == "first" else out[:-1]
                    return code, out, err
                with self.subTest(which=which, cut=cut):
                    a, batched, b, _p = self.both(pa_hooks.git, path, tip, base=base, answer=answer)
                    self.assertEqual(same(a), same(b))
                    self.assertTrue(batched.batches())

    def test_ac3_1_trailing_bytes_give_the_per_call_record(self):
        path = self.repo()
        base, tip = self.chain(path, 2)

        def answer(args, kw, real):
            code, out, err = real()
            if "--batch" in args or "--stdin" in args:
                out = out + b"x"
            return code, out, err
        a, batched, b, _p = self.both(pa_hooks.git, path, tip, base=base, answer=answer)
        self.assertEqual(same(a), same(b))
        self.assertTrue(batched.batches())

    def test_ac3_2_a_missing_object_gives_todays_message(self):
        path = self.repo()
        base, tip = self.chain(path, 3)
        gone = sh(path, "git", "rev-parse", tip + "~1")

        def answer(args, kw, real):
            if "--batch" in args:
                code, out, err = real()
                lines = kw["input"].decode().split("\n")
                parts, at = [], 0
                for oid in lines[:-1]:
                    head_end = out.index(b"\n", at)
                    size = int(out[at:head_end].split(b" ")[2])
                    piece = out[at:head_end + 1 + size + 1]
                    parts.append(oid.encode() + b" missing\n" if oid == gone else piece)
                    at = head_end + 1 + size + 1
                return 0, b"".join(parts), ""
            if args[-3:] == ("cat-file", "commit", gone):
                return 128, "", "fatal: git cat-file {}: bad file".format(gone)
            return real()
        a, batched, b, _p = self.both(pa_hooks.git, path, tip, base=base, answer=answer)
        self.assertEqual(b, {"error": "cannot read commit {} (fatal: git cat-file {}: bad file)".format(gone[:7], gone)})
        self.assertEqual(a, b)
        self.assertTrue(batched.batches())

    def test_ac3_3_a_runner_that_takes_no_stdin_keeps_todays_calls(self):
        path = self.repo()
        base, tip = self.chain(path, 3)
        rec = Rec(pa_hooks.git, stdin=False)
        pa_hooks.content_record(pa_hooks.content_git(rec), path, tip, "main", base)
        self.assertEqual(rec.batches(), [])
        self.assertEqual(len(rec.content()), 4 + 2 * 3, rec.content())

    def test_a_steering_refusal_during_the_batch_is_not_a_fallback(self):
        path = self.repo()
        base, tip = self.chain(path, 2)
        grafts = os.path.join(self.common(path), "info", "grafts")

        def answer(args, kw, real):
            if "--batch" in args:
                write(grafts, "")
            return real()
        rec = Rec(pa_hooks.git, answer=answer)
        try:
            record = pa_hooks.content_record(pa_hooks.content_git(rec), path, tip, "main", base)
        finally:
            if os.path.exists(grafts):
                os.remove(grafts)
        self.assertIn("a grafts file", record.get("error", ""))
        self.assertEqual(len(rec.batches()), 1, "nothing runs after the refusal")


# ---- R4: the cache's batch shapes -------------------------------------------------------------------------------

class Shapes(unittest.TestCase):
    A, B, C = "a" * 40, "b" * 40, "c" * 40

    def test_ac4_1_the_batch_shapes_that_are_cached(self):
        a, b, c, cache = self.A, self.B, self.C, pa_hooks.cacheable
        cat, tree = ("cat-file", "--batch"), pa_hooks.DIFF_TREE
        self.assertEqual(cache(cat, 40, input=(a + "\n").encode()), {0})
        self.assertEqual(cache(cat, 40, input=(a + "\n" + b + "\n" + c + "\n").encode()), {0})
        self.assertEqual(cache(tree, 40, input=(a + " " + b + "\n").encode()), {0})
        self.assertEqual(cache(tree, 40, input=(a + " " + b + "\n" + b + " " + c + "\n").encode()), {0})
        s = "d" * 64
        self.assertEqual(cache(cat, 64, input=(s + "\n").encode()), {0})
        self.assertEqual(cache(tree, 64, input=(s + " " + s + "\n").encode()), {0})

    def test_ac4_1_near_misses_are_never_cached(self):
        a, b, cache = self.A, self.B, pa_hooks.cacheable
        cat, tree = ("cat-file", "--batch"), pa_hooks.DIFF_TREE
        never = [
            (cat, b"refs/heads/main\n"), (cat, b"HEAD\n"), (cat, (a[:7] + "\n").encode()),
            (cat, (a.upper() + "\n").encode()), (cat, (a + " " + b + "\n").encode()), (cat, (a + " \n").encode()),
            (cat, a.encode()), (cat, b""), (cat, None), (cat, (a + "\n\n").encode()), (cat, ("\n" + a + "\n").encode()),
            (cat, ("c" * 64 + "\n").encode()), (cat, (a + "\r\n").encode()), (cat, ("x" * 40 + "\n").encode()),
            (tree, (a + "\n").encode()), (tree, (a + " " + b + " " + a + "\n").encode()),
            (tree, (a + " " + b + " \n").encode()), (tree, (a + "  " + b + "\n").encode()), (tree, (a + " " + b).encode()),
            (tree, b""), (tree, None), (tree, (a + " HEAD\n").encode()), (tree, ("refs/heads/x " + b + "\n").encode()),
            (tree, (a + "\t" + b + "\n").encode()),
            (("cat-file", "--batch-check"), (a + "\n").encode()), (("cat-file", "--batch", "--follow-symlinks"), (a + "\n").encode()),
            (("cat-file", "--batch=%(objectname)"), (a + "\n").encode()), (("cat-file", "--batch", "--buffer"), (a + "\n").encode()),
            (tree + ("--extra",), (a + " " + b + "\n").encode()), (tree[:-1], (a + " " + b + "\n").encode()),
            (("diff-tree", "--stdin"), (a + " " + b + "\n").encode()),
            (("cat-file", "commit", a), b""), (("cat-file", "commit", a), (a + "\n").encode()),
            (("rev-list", "--reverse", "--parents", a + ".." + b), (a + "\n").encode()),
            (pa_hooks.RAW_DIFF + (a, b), (a + " " + b + "\n").encode()),
        ]
        for argv, data in never:
            self.assertIsNone(cache(argv, 40, input=data), (argv, data))
        self.assertIsNone(cache(cat, None, input=(a + "\n").encode()), "an unknown object format caches nothing")
        self.assertIsNone(cache(cat, 64, input=(a + "\n").encode()), "40 hex in a SHA-256 repository")
        self.assertEqual(cache(("cat-file", "commit", a), 40), {0}, "every other shape as before, without input")

    def test_ac4_1_a_batch_answer_is_stored_under_its_input(self):
        tmp = os.path.realpath(tempfile.mkdtemp(dir=TMP))
        path = os.path.join(tmp, "r")
        os.makedirs(path)
        sh(path, "git", "init", "-q", "-b", "main")
        write(os.path.join(path, "a"), "a\n")
        sh(path, "git", "add", "a")
        sh(path, "git", "commit", "-q", "-m", "a")
        one = sh(path, "git", "rev-parse", "HEAD")
        write(os.path.join(path, "b"), "b\n")
        sh(path, "git", "add", "b")
        sh(path, "git", "commit", "-q", "-m", "b")
        two = sh(path, "git", "rev-parse", "HEAD")
        cache = pa_hooks.ContentCache()
        rec = Rec(pa_hooks.git)
        run = pa_hooks.content_git(rec, cache)
        first = run(path, "cat-file", "--batch", input=(one + "\n").encode())
        again = run(path, "cat-file", "--batch", input=(one + "\n").encode())
        other = run(path, "cat-file", "--batch", input=(two + "\n").encode())
        self.assertEqual(first, again)
        self.assertNotEqual(first[1], other[1])
        self.assertEqual(len(rec.batches()), 2, "the same input is a hit, another input is not")
        self.assertEqual(cache.hits, 1)
        self.assertTrue(first[1].startswith((one + " commit ").encode()), first)


# ---- R5: no program runs ----------------------------------------------------------------------------------------

class NoProgram(Repo):
    def test_ac5_1_planted_programs_never_run_on_the_batch_path(self):
        path = self.repo()
        base = self.head(path)
        marker = os.path.join(self.tmp, "ran")
        prog = os.path.join(self.tmp, "prog")
        write(prog, "#!/bin/sh\necho ran >> '{}'\ncat >/dev/null\nexit 0\n".format(marker))
        os.chmod(prog, 0o755)
        self.commit(path, "a.txt", "a\n")
        # A commit object with a signature header (git cat-file and diff-tree never check it).
        body = sh(path, "git", "cat-file", "commit", "HEAD")
        head, _sep, msg = body.partition("\n\n")
        signed = head + "\ngpgsig -----BEGIN PGP SIGNATURE-----\n \n abc\n -----END PGP SIGNATURE-----\n\n" + msg + "\n"
        write(os.path.join(self.tmp, "obj"), signed)
        sig = sh(path, "git", "hash-object", "-t", "commit", "-w", os.path.join(self.tmp, "obj"))
        sh(path, "git", "reset", "-q", "--hard", sig)
        tip = self.commit(path, "b.txt", "b\n")
        for key, value in (("diff.external", prog), ("diff.planted.textconv", prog), ("log.showSignature", "true"),
                           ("gpg.program", prog), ("core.pager", prog)):
            sh(path, "git", "config", key, value)
        write(os.path.join(self.common(path), "info", "attributes"), "* diff=planted\n")
        rec = Rec(pa_hooks.git)
        record = pa_hooks.content_record(pa_hooks.content_git(rec), path, tip, "main", base)
        self.assertNotIn("error", record)
        self.assertEqual(len(rec.batches()), 2)
        self.assertEqual([c["sha"] for c in record["commits"]], [sig, tip])
        self.assertFalse(os.path.exists(marker), "no program the repository names ran")


# ---- R6: parents from the commit objects ------------------------------------------------------------------------

class Parents(Repo):
    def transient(self, path, name, text, during):
        """A raw runner that makes <common>/<name> just before a call whose argv has `during`, and removes it
        just after, so neither of content_git's checks sees it."""
        target = os.path.join(self.common(path), name)

        def run(p, *args, **kw):
            if during in args:
                write(target, text)
                try:
                    return pa_hooks.git(p, *args, **kw)
                finally:
                    os.remove(target)
            return pa_hooks.git(p, *args, **kw)
        run.takes_input = True
        return run

    def setUp(self):
        super().setUp()
        self.path = self.repo()
        self.base = self.head(self.path)
        self.c1 = self.commit(self.path, "a", "a\n")
        self.c2 = self.commit(self.path, "b", "b\n")
        self.tip = self.commit(self.path, "c", "c\n")
        self.refusal = {"error": P1.format(self.base[:7], self.tip[:7])}

    def test_ac6_1_a_transient_grafts_file_during_rev_list_is_refused(self):
        run = self.transient(self.path, "info/grafts", "{} {}\n".format(self.tip, self.base), "rev-list")
        record = pa_hooks.content_record(pa_hooks.content_git(run), self.path, self.tip, "main", self.base)
        self.assertEqual(record, self.refusal)

    def test_ac6_2_a_transient_shallow_file_during_rev_list_is_refused(self):
        run = self.transient(self.path, "shallow", self.c2 + "\n", "rev-list")
        record = pa_hooks.content_record(pa_hooks.content_git(run), self.path, self.tip, "main", self.base)
        self.assertEqual(record, self.refusal)

    def test_ac6_3_the_per_call_path_checks_the_objects_too(self):
        for name, text in (("info/grafts", "{} {}\n".format(self.tip, self.base)), ("shallow", self.c2 + "\n")):
            with self.subTest(file=name):
                rec = Rec(self.transient(self.path, name, text, "rev-list"), stdin=False)
                record = pa_hooks.content_record(pa_hooks.content_git(rec), self.path, self.tip, "main", self.base)
                self.assertEqual(record, self.refusal)
                self.assertEqual(rec.batches(), [])

    def test_ac6_4_a_stored_base_made_an_ancestor_during_is_ancestor_is_refused(self):
        sh(self.path, "git", "checkout", "-q", "-b", "side", self.base)
        other = self.commit(self.path, "s", "s\n")
        sh(self.path, "git", "checkout", "-q", "main")
        self.assertNotEqual(sh(self.path, "git", "merge-base", other, self.tip), other)
        run = self.transient(self.path, "info/grafts", "{} {}\n".format(self.tip, other), "--is-ancestor")
        for stdin in (True, False):
            with self.subTest(stdin=stdin):
                rec = Rec(run, stdin=stdin)
                record = pa_hooks.content_record(pa_hooks.content_git(rec), self.path, self.tip, "main", other)
                self.assertEqual(record, {"error": P1.format(other[:7], self.tip[:7])})

    def test_the_diffs_name_the_parent_from_the_object(self):
        rec = Rec(pa_hooks.git, stdin=False)
        record = pa_hooks.content_record(pa_hooks.content_git(rec), self.path, self.tip, "main", self.base)
        self.assertNotIn("error", record)
        diffs = [c for c in rec.content() if c[0] == "diff"]
        self.assertEqual([c[-2:] for c in diffs], [(self.base, self.tip), (self.base, self.c1), (self.c1, self.c2),
                                                    (self.c2, self.tip)])
        self.assertFalse(any(w.endswith("^") for c in diffs for w in c))

    def test_a_failing_per_commit_diff_keeps_todays_text(self):
        def answer(args, kw, real):
            if args[-2:] == (self.c1, self.c2) and "diff" in args:
                return 128, "", "fatal: no"
            return real()
        rec = Rec(pa_hooks.git, stdin=False, answer=answer)
        record = pa_hooks.content_record(pa_hooks.content_git(rec), self.path, self.tip, "main", self.base)
        self.assertEqual(record, {"error": "git diff {}..{} failed (fatal: no)".format(self.c2[:7], self.c2[:7])})

    def test_the_merge_message_keeps_its_priority(self):
        sh(self.path, "git", "checkout", "-q", "-b", "side", self.c1)
        self.commit(self.path, "s", "s\n")
        sh(self.path, "git", "checkout", "-q", "main")
        sh(self.path, "git", "merge", "-q", "--no-edit", "side")
        tip = self.head(self.path)
        record = pa_hooks.content_record(pa_hooks.content_git(pa_hooks.git), self.path, tip, "main", self.base)
        self.assertEqual(record, {"error": "the branch has a merge commit ({}); the content check takes linear "
                                           "branches".format(tip[:7])})


# ---- R11: the cache's lifetime ----------------------------------------------------------------------------------

class Lifetime(QBase):
    def test_ac11_1_after_session_close_ctx_run_caches_nothing(self):
        tip = self.ref("main")
        ctx = pa_queue.Ctx(self.dir, self.proj)
        cache = ctx.s.cache
        try:
            first = ctx.run(self.proj, "cat-file", "commit", tip)
            self.assertEqual(ctx.run(self.proj, "cat-file", "commit", tip), first)
            self.assertEqual((len(cache.answers), cache.hits), (1, 1))
        finally:
            ctx.close()
        self.assertTrue(cache.closed)
        self.assertEqual((cache.answers, cache.hits), ({}, 0), "close drops the answers and the hits")
        self.assertEqual(ctx.run(self.proj, "cat-file", "commit", tip), first, "the closure still answers")
        ctx.run(self.proj, "cat-file", "commit", tip)
        self.assertEqual((cache.answers, cache.hits), ({}, 0), "a closed cache is never looked up or filled")

    def test_ac11_2_clear_resets_the_hits(self):
        tip = self.ref("main")
        cache = pa_hooks.ContentCache()
        run = pa_hooks.content_git(pa_hooks.git, cache)
        run(self.proj, "cat-file", "commit", tip)
        run(self.proj, "cat-file", "commit", tip)
        self.assertEqual(cache.hits, 1)
        cache.clear()
        self.assertEqual((cache.answers, cache.hits), ({}, 0))
        self.assertFalse(cache.closed, "clear is not close: the cache is used again")
        run(self.proj, "cat-file", "commit", tip)
        self.assertEqual(len(cache.answers), 1)


if __name__ == "__main__":
    unittest.main()
