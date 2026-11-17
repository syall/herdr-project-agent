"""Unit tests for pa-push-main and pa-push-allow (t-007 part 7b, Job A): the person's push card.

Run: python3 -m unittest -v tests/test_pa_push.py
The fixture (temp homes, the bare origin under a temp folder, the person-only check's injected environment) is
tests/push_fixtures.py; see its docstring. Split from tests/test_pa_push.py by t-030 so the parts run at once.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from push_fixtures import *  # noqa: F401,F403 (the shared fixture and helpers)
from push_fixtures import setUpModule, tearDownModule  # noqa: F401 (the module's temp homes and environment)

# ---- A1: the allowlist ----------------------------------------------------------------------------

class Allowlist(Repo):
    def valid(self, **entry_changes):
        e = dict(self.entry, **entry_changes)
        return {"version": 1, "enabled": True, "repos": [e]}

    def test_a01_missing_file_is_off(self):
        os.remove(self.allow)
        data, problem, sha, mtime = pa_push.load_allow()
        self.assertEqual((data, problem, sha, mtime), (None, None, None, None))
        self.assertEqual(pa_push.allow_path(), os.path.join(pa.ROOT, "push-allow.json"))
        shown = pa_push.allow_report(SPACE)
        self.assertFalse(shown["exists"])
        self.assertIn("off (the default)", shown["state"])

    def test_a02_each_bad_field_is_invalid_with_reason(self):
        self.assertIsNone(pa_push.validate(self.valid()))
        self.assertIsNone(pa_push.validate(self.valid(prePush=["/bin/true", "{path}"], note="n")))
        top_bad = [({"version": 1, "enabled": True, "repos": [], "extra": 1}, "extra"),
                   ({"version": 1, "repos": []}, "enabled"),
                   ({"version": 1, "enabled": "yes", "repos": []}, "enabled"),
                   ({"version": 1, "enabled": True, "repos": {}}, "repos"),
                   ({"version": 1, "enabled": True, "repos": [dict(self.entry, path="/x/{}".format(i)) for i in range(51)]}, "50"),
                   ([], "object")]
        for data, word in top_bad:
            problem = pa_push.validate(data)
            self.assertIsNotNone(problem, data)
            self.assertIn(word, problem)
        entry_bad = [({"path": "relative/path"}, "path"),
                     ({"path": 3}, "path"),
                     ({"url": "-oProxyCommand=x"}, "url"),
                     ({"url": "ftp://host/x"}, "url"),
                     ({"url": "file://relative"}, "url"),
                     ({"url": "https://host/a b"}, "url"),
                     ({"branch": "-main"}, "branch"),
                     ({"branch": "ma*in"}, "branch"),
                     ({"branch": "a..b"}, "branch"),
                     ({"branch": "x.lock"}, "branch"),
                     ({"branch": "a b"}, "branch"),
                     ({"branch": ""}, "branch"),
                     ({"branch": "@{u}"}, "branch"),
                     ({"space": "sp/1"}, "space"),
                     ({"enabled": 1}, "enabled"),
                     ({"note": "x" * 201}, "note"),
                     ({"note": 5}, "note"),
                     ({"prePush": []}, "prePush"),
                     ({"prePush": ["relative.sh"]}, "prePush"),
                     ({"prePush": ["/bin/check", ""]}, "prePush"),
                     ({"prePush": "/bin/check"}, "prePush"),
                     ({"other": 1}, "other")]
        for change, word in entry_bad:
            problem = pa_push.validate(self.valid(**change))
            self.assertIsNotNone(problem, change)
            self.assertIn(word, problem, change)
        missing = dict(self.entry)
        del missing["url"]
        self.assertIn("url", pa_push.validate({"version": 1, "enabled": True, "repos": [missing]}))
        for url in ("ssh://git.example.invalid/x", "git@example.invalid:me/x.git", "https://example.invalid/x.git", "file:///tmp/x"):
            self.assertIsNone(pa_push.validate(self.valid(url=url)), url)
        for branch in ("main", "release/1.2", "trunk"):
            self.assertIsNone(pa_push.validate(self.valid(branch=branch)), branch)

    def test_a02b_invalid_file_allows_nothing_and_show_says_why(self):
        write(self.allow, "{not json")
        data, problem, sha, _ = pa_push.load_allow()
        self.assertIsNotNone(problem)
        self.assertIsNotNone(sha)
        report = pa_push.allow_report(SPACE)
        self.assertFalse(report["valid"])
        self.assertIn("not valid", report["state"])

    def test_a03_duplicate_path_space_invalid(self):
        data = self.valid()
        data["repos"].append(dict(self.entry, path=self.main + "/"))
        self.assertIn("twice", pa_push.validate(data))
        data["repos"][1]["space"] = "sp2"
        self.assertIsNone(pa_push.validate(data))

    def test_a04_version_not_1_invalid(self):
        for v in (2, 0, "1", True, None):
            data = self.valid()
            data["version"] = v
            self.assertIn("version", pa_push.validate(data), v)

    def test_a05_prepush_placeholders_and_absolute(self):
        ok = ["/bin/check", "{path}", "{repo}", "{commit}", "{url}", "{branch}", "--x={path}"]
        self.assertIsNone(pa_push.validate(self.valid(prePush=ok)))
        self.assertIn("placeholder", pa_push.validate(self.valid(prePush=["/bin/check", "{home}"])))
        self.assertIn("prePush", pa_push.validate(self.valid(prePush=["{path}/check.sh"])))
        argv = pa_push.fill(["/bin/check", "{path}", "x{commit}"], path="/T", repo="/R", commit="c" * 40, url="file:///u", branch="main")
        self.assertEqual(argv, ["/bin/check", "/T", "x" + "c" * 40])

    def test_a06_allow_change_refuses_agents(self):
        before = snapshot(pa.ROOT)
        for extra in ({"CLAUDECODE": "1"}, {"CODEX_SANDBOX": "seatbelt"}, {"CLAUDE_CODE_SSE_PORT": "1"}):
            env = dict(os.environ, **extra)
            code, out, err = self.cli("pa-push-allow", "add", "--path", self.main, "--space", "sp2", "--url", self.url,
                                      "--branch", "main", env=env)
            self.assertEqual(code, 1, err)
            self.assertIn("the person's command", err)
        code, out, err = self.cli("pa-push-allow", "off", env=dict(os.environ, CLAUDECODE="1"))
        self.assertEqual(code, 1)
        self.assertEqual(snapshot(pa.ROOT), before)
        # in-process: an agent ancestor refuses too, and a failed ps refuses
        args = pa_push.allow_parser().parse_args(["off"])
        for tree in ([{"pid": 5, "comm": "claude", "args": "claude"}], [{"pid": 5, "comm": "node", "args": "node /x/claude-code/cli.js"}], None):
            code, _o, err = self.quiet(pa_push.allow_change, args, env=PERSON_ENV, ancestors=tree)
            self.assertEqual(code, 1)
        self.assertEqual(snapshot(pa.ROOT), before)
        # show is anyone's
        code, out, err = self.cli("pa-push-allow", "show", "--json", env=dict(os.environ, CLAUDECODE="1"))
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(out)["valid"])

    def test_a06b_allow_change_refuses_an_orphaned_caller(self):
        """C0 cannot tell who an orphaned process is (its only ancestor is the system's first process): refused."""
        before = snapshot(pa.ROOT)
        args = pa_push.allow_parser().parse_args(["off"])
        for tree in ([{"pid": 1, "comm": "/sbin/launchd", "args": "/sbin/launchd"}], [{"pid": 1, "comm": "/sbin/init", "args": "/sbin/init"}]):
            code, _o, err = self.quiet(pa_push.allow_change, args, env=PERSON_ENV, ancestors=tree)
            self.assertEqual(code, 1, err)
            self.assertIn("the caller is unknown (an orphaned process)", err)
        self.assertEqual(snapshot(pa.ROOT), before)

    def test_a07_add_remove_on_off_write_valid_json_and_backup(self):
        os.remove(self.allow)
        parse = pa_push.allow_parser().parse_args
        code, out, _ = self.quiet(pa_push.allow_change, parse(["add", "--path", self.main, "--space", SPACE]), env=PERSON_ENV, ancestors=PERSON_TREE)
        self.assertEqual(code, 0)
        data = load(self.allow)
        self.assertIsNone(pa_push.validate(data))
        self.assertEqual(data["repos"], [{"path": self.main, "url": self.url, "branch": "main", "space": SPACE, "enabled": True}])
        self.assertIn(self.url, out)
        code, _o, err = self.quiet(pa_push.allow_change, parse(["add", "--path", self.main, "--space", SPACE]), env=PERSON_ENV, ancestors=PERSON_TREE)
        self.assertEqual(code, 1)
        self.assertIn("already", err)
        code, _o, _e = self.quiet(pa_push.allow_change, parse(["add", "--path", self.main, "--space", "sp2", "--url", "ssh://h.invalid/x",
                                                                "--branch", "trunk", "--pre-push", "/bin/true", "{path}"]),
                                  env=PERSON_ENV, ancestors=PERSON_TREE)
        self.assertEqual(code, 0)
        data = load(self.allow)
        self.assertEqual(data["repos"][1]["prePush"], ["/bin/true", "{path}"])
        self.assertEqual(data["repos"][1]["branch"], "trunk")
        self.assertTrue(os.path.exists(self.allow + ".bak-pa-push-allow"))
        self.quiet(pa_push.allow_change, parse(["off", "--path", self.main, "--space", "sp2"]), env=PERSON_ENV, ancestors=PERSON_TREE)
        self.assertFalse(load(self.allow)["repos"][1]["enabled"])
        self.quiet(pa_push.allow_change, parse(["off"]), env=PERSON_ENV, ancestors=PERSON_TREE)
        self.assertFalse(load(self.allow)["enabled"])
        self.quiet(pa_push.allow_change, parse(["on"]), env=PERSON_ENV, ancestors=PERSON_TREE)
        self.assertTrue(load(self.allow)["enabled"])
        code, _o, _e = self.quiet(pa_push.allow_change, parse(["remove", "--path", self.main, "--space", "sp2"]), env=PERSON_ENV, ancestors=PERSON_TREE)
        self.assertEqual(code, 0)
        self.assertEqual(len(load(self.allow)["repos"]), 1)
        code, _o, err = self.quiet(pa_push.allow_change, parse(["remove", "--path", self.main, "--space", "sp9"]), env=PERSON_ENV, ancestors=PERSON_TREE)
        self.assertEqual(code, 1)
        self.assertIsNone(pa_push.validate(load(self.allow)))
        backups = [n for n in os.listdir(pa.ROOT) if n.startswith("push-allow.json.bak-pa-push-allow")]
        self.assertGreaterEqual(len(backups), 4)
        # a change that would make the file invalid is refused, and the file is untouched
        before = snapshot(pa.ROOT)
        code, _o, err = self.quiet(pa_push.allow_change, parse(["add", "--path", self.main, "--space", "sp3", "--url=-x", "--branch", "main"]),
                                   env=PERSON_ENV, ancestors=PERSON_TREE)
        self.assertEqual(code, 1)
        self.assertIn("url", err)
        self.assertEqual(snapshot(pa.ROOT), before)

    def test_a08_sync_dry_run_never_lists_it(self):
        os.makedirs(os.path.join(pa.ROOT, "tools"), exist_ok=True)
        write(os.path.join(pa.ROOT, "tools", "pa.py"), "#\n")
        _v, out, _e = self.quiet(pa.cmd_sync, argparse.Namespace(on="m", dry_run=True))
        self.assertIn("tools/pa.py", out)
        self.assertNotIn("push-allow", out)

    def test_a09_install_sh_never_names_it(self):
        with open(os.path.join(REPO, "install.sh")) as f:
            text = f.read()
        self.assertNotIn("push-allow.json", text)


# ---- A2: plan and check; pa-decide ---------------------------------------------------------------

class Plan(Planned):
    def test_p01_plan_writes_card_shape(self):
        # an earlier task's commit, merged locally and not pushed yet: it rides along, marked not from this task
        earlier = self.commit_on(self.main, "e.txt", "earlier task")
        sh(self.wt, "git", "rebase", "-q", "main")
        self.assertEqual(self.gate(), 0)
        commit = self.head(self.wt)
        code, out, err = self.plan()
        self.assertEqual(code, 0, err)
        self.assertIn(CARD, out)
        c = self.card()
        for key in ("id", "task", "kind", "question", "context", "options", "evidence", "recommendation", "status", "answer",
                    "note", "created", "answered", "push"):
            self.assertIn(key, c)
        self.assertEqual((c["id"], c["task"], c["kind"], c["status"], c["answer"], c["recommendation"]), (CARD, TASK, "push", "open", None, "Push"))
        self.assertEqual([o["label"] for o in c["options"]], ["Push", "Not now"])
        self.assertIn("No force", c["options"][0]["detail"])
        p = c["push"]
        with open(self.allow, "rb") as f:
            sha = hashlib.sha256(f.read()).hexdigest()
        gate_at = load(os.path.join(self.dir, "evidence", TASK, "GATE.json"))["at"]
        self.assertEqual(p["version"], 1)
        self.assertEqual((p["space"], p["task"], p["path"], p["url"], p["branch"]), (SPACE, TASK, self.main, self.url, "main"))
        self.assertEqual((p["taskBranch"], p["commit"], p["localBefore"], p["remoteBefore"]), ("t-1-x", commit, earlier, self.origin_head()))
        self.assertEqual((p["merge"], p["allowSha"], p["gateAt"], p["done"], p["result"], p["attempts"]), (True, sha, gate_at, False, None, []))
        self.assertTrue(p["allowMtime"].endswith("Z"))
        self.assertEqual([(x["subject"], x["fromTask"]) for x in p["commits"]], [("task work", True), ("earlier task", False)])
        self.assertTrue(all(len(x["sha"]) == 40 and "<" in x["author"] for x in p["commits"]))
        self.assertIn(self.url, c["question"])
        self.assertIn("2 commits", c["question"])
        self.assertIn("not from this task", c["context"])
        line = self.journal()[-1]
        self.assertEqual((line["trigger"], line["task"], line["decision"]), ("push-plan", TASK, CARD))
        self.assertIn(line["by"], ("agent", "person"))
        # plan pushed and merged nothing
        self.assertEqual(self.head(self.main), earlier)
        self.assertNotEqual(self.origin_head(), commit)

    def test_p10_fromtask_by_task_branch_when_main_already_at_commit(self):
        """Local main was fast-forwarded to the gated commit before plan: the task's commit is still from this task,
        and an earlier task's commit is still marked (spec 6.1: on local main before this task)."""
        earlier = self.commit_on(self.main, "e.txt", "earlier task")
        sh(self.wt, "git", "rebase", "-q", "main")
        self.assertEqual(self.gate(), 0)
        commit = self.head(self.wt)
        sh(self.main, "git", "merge", "-q", "--ff-only", "t-1-x")
        code, out, err = self.plan()
        self.assertEqual(code, 0, err)
        c = self.card()
        p = c["push"]
        self.assertEqual((p["localBefore"], p["merge"], p["commit"]), (commit, False, commit))
        self.assertEqual([(x["subject"], x["fromTask"]) for x in p["commits"]], [("task work", True), ("earlier task", False)])
        lines = c["context"].splitlines()
        self.assertTrue(any(l.startswith("- " + earlier[:7]) and l.endswith("(not from this task)") for l in lines), lines)
        self.assertFalse(any(l.startswith("- " + commit[:7]) and "not from this task" in l for l in lines), lines)

    def test_p11_fromtask_main_at_commit_no_earlier_commit(self):
        sh(self.main, "git", "merge", "-q", "--ff-only", "t-1-x")
        code, out, err = self.plan()
        self.assertEqual(code, 0, err)
        c = self.card()
        self.assertEqual(c["push"]["localBefore"], self.task_commit)
        self.assertEqual([(x["subject"], x["fromTask"]) for x in c["push"]["commits"]], [("task work", True)])
        self.assertNotIn("not from this task", c["context"])

    def test_p12_fromtask_merged_in_commit_is_not_the_tasks(self):
        """An earlier task's commit that the task branch merged in (not on its own first-parent line) is marked."""
        self.commit_on(self.main, "e.txt", "earlier task")
        sh(self.wt, "git", "merge", "-q", "--no-ff", "-m", "merge main", "main")
        self.assertEqual(self.gate(), 0)
        code, out, err = self.plan()
        self.assertEqual(code, 0, err)
        marks = {x["subject"]: x["fromTask"] for x in self.card()["push"]["commits"]}
        self.assertEqual(marks, {"merge main": True, "task work": True, "earlier task": False})

    def test_p13_fromtask_unknown_without_the_branch_record(self):
        """Where the task branch started cannot be read (no reflog): fromTask is null, and the card says so."""
        sh(self.main, "git", "merge", "-q", "--ff-only", "t-1-x")
        log = os.path.join(self.main, ".git", "logs", "refs", "heads", "t-1-x")
        self.assertTrue(os.path.isfile(log))
        os.remove(log)
        code, out, err = self.plan()
        self.assertEqual(code, 0, err)
        c = self.card()
        self.assertEqual([(x["subject"], x["fromTask"]) for x in c["push"]["commits"]], [("task work", None)])
        self.assertIn("which of these commits are this task's cannot be told", c["context"])

    def test_p14_fromtask_branch_fast_forwarded_onto_newer_main(self):
        """The task branch caught up with a newer local main by a fast-forward before its own first commit: the
        earlier task's commit it picked up is not this task's."""
        sh(self.wt, "git", "reset", "-q", "--hard", "HEAD~1")
        earlier = self.commit_on(self.main, "e.txt", "earlier task")
        sh(self.wt, "git", "merge", "-q", "--ff-only", "main")
        self.commit_on(self.wt, "f.txt", "own work")
        self.assertEqual(self.gate(), 0)
        code, out, err = self.plan()
        self.assertEqual(code, 0, err)
        c = self.card()
        self.assertEqual({x["subject"]: x["fromTask"] for x in c["push"]["commits"]}, {"own work": True, "earlier task": False})
        lines = c["context"].splitlines()
        self.assertTrue(any(l.startswith("- " + earlier[:7]) and l.endswith("(not from this task)") for l in lines), lines)

    def test_p15_fromtask_branch_reset_onto_newer_main(self):
        """The task branch was reset --hard onto a newer local main, then committed."""
        self.commit_on(self.main, "e.txt", "earlier task")
        sh(self.wt, "git", "reset", "-q", "--hard", "main")
        self.commit_on(self.wt, "f.txt", "own work")
        self.assertEqual(self.gate(), 0)
        code, out, err = self.plan()
        self.assertEqual(code, 0, err)
        self.assertEqual({x["subject"]: x["fromTask"] for x in self.card()["push"]["commits"]}, {"own work": True, "earlier task": False})

    def test_p16_fromtask_unknown_after_a_move_the_tool_cannot_read(self):
        """A reflog entry that is neither the branch's own commit nor a known move onto main: unknown, never true."""
        earlier = self.commit_on(self.main, "e.txt", "earlier task")
        sh(self.wt, "git", "update-ref", "-m", "something else", "refs/heads/t-1-x", earlier)
        sh(self.wt, "git", "read-tree", "--reset", "-u", "HEAD")  # the files follow; no reflog entry of its own
        self.commit_on(self.wt, "f.txt", "own work")
        self.assertEqual(self.gate(), 0)
        code, out, err = self.plan()
        self.assertEqual(code, 0, err)
        c = self.card()
        self.assertEqual({x["subject"]: x["fromTask"] for x in c["push"]["commits"]}, {"own work": None, "earlier task": None})
        self.assertIn("which of these commits are this task's cannot be told", c["context"])

    def test_p02_plan_refuses_without_allowlist(self):
        os.remove(self.allow)
        code, out, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("no push allowlist", err)
        self.assertIn("main-branch pushes are off", err)
        self.assertFalse(os.listdir(os.path.join(self.dir, "decisions")))
        dump({"version": 1, "enabled": False, "repos": [self.entry]}, self.allow)
        code, _o, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("turned off", err)

    def test_p03_plan_refuses_other_space_or_url(self):
        self.set_allow(space="sp2")
        code, _o, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("names space sp2, not this space sp1", err)
        self.set_allow(space=SPACE, path=os.path.join(self.tmp, "elsewhere"))
        code, _o, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("no allowlist entry for", err)
        self.assertFalse(os.listdir(os.path.join(self.dir, "decisions")))
        # origin pointing elsewhere is a warning on the card, not a refusal (the URL comes from the allowlist)
        self.set_allow(path=self.main)
        sh(self.main, "git", "remote", "set-url", "origin", "file://" + os.path.join(self.tmp, "other.git"))
        code, _o, err = self.plan()
        self.assertEqual(code, 0, err)
        self.assertIn("origin", self.card()["context"])
        self.assertIn("other.git", self.card()["context"])
        self.assertEqual(self.card()["push"]["url"], self.url)

    def test_p04_plan_refuses_from_a_worker_worktree(self):
        dump({"task": TASK, "space": SPACE, "fix": False}, os.path.join(self.wt, ".claude", "pa-task.json"))
        os.chdir(self.wt)
        code, _o, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("workers do not open push cards", err)
        self.assertFalse(os.listdir(os.path.join(self.dir, "decisions")))

    def test_p05_second_plan_with_open_card_refused(self):
        self.assertEqual(self.plan()[0], 0)
        code, _o, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn(CARD, err)
        self.assertIn("withdraw", err)
        c = self.card()
        c["status"] = "withdrawn"
        dump(c, self.card_path())
        code, out, err = self.plan()
        self.assertEqual(code, 0, err)
        self.assertIn("d-push-t-1-2", out)

    def test_p06_plan_refuses_gate_not_review_or_failed(self):
        gate_file = os.path.join(self.dir, "evidence", TASK, "GATE.json")
        os.remove(gate_file)
        code, _o, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("GATE.json", err)
        self.gate(phase="build")
        code, _o, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("is from phase build", err)
        self.gate()
        g = load(gate_file)
        g["passed"] = False
        dump(g, gate_file)
        code, _o, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("did not pass", err)
        os.remove(os.path.join(self.dir, "evidence", TASK, "REVIEW.md"))
        code, _o, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("does not pass now", err)
        self.assertIn("REVIEW.md", err)
        self.assertFalse(os.listdir(os.path.join(self.dir, "decisions")))

    def test_p06b_plan_refuses_nothing_to_push(self):
        sh(self.main, "git", "merge", "-q", "--ff-only", "t-1-x")
        sh(self.main, "git", "push", "-q", "origin", "main")
        code, _o, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("already", err)


class Check(Planned):
    def setUp(self):
        Planned.setUp(self)
        self.assertEqual(self.plan()[0], 0)

    def test_p07_check_writes_nothing(self):
        os.remove(os.path.join(self.dir, ".push.lock"))  # made by plan; check takes no lock
        before = snapshot(pa.ROOT)
        refs = (self.head(self.main), self.origin_head(), self.head(self.main, "refs/heads/t-1-x"))
        report = self.check()
        self.assertTrue(report["ok"], report["refusal"])
        code, out, err = self.cli("pa-push-main", "check", CARD, "--space", SPACE, "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(snapshot(pa.ROOT), before)
        self.assertEqual((self.head(self.main), self.origin_head(), self.head(self.main, "refs/heads/t-1-x")), refs)
        self.assertFalse(os.path.exists(os.path.join(self.dir, ".push.lock")))

    def test_p08_check_json_contract(self):
        code, out, err = self.cli("pa-push-main", "check", CARD, "--space", SPACE, "--json")
        self.assertEqual(code, 0, err)
        r = json.loads(out)
        self.assertEqual(set(r), {"card", "ok", "refusal", "caller", "status", "done", "result", "facts", "seen"})
        self.assertEqual((r["card"], r["ok"], r["refusal"], r["status"], r["done"]), (CARD, True, None, "open", False))
        self.assertEqual(set(r["facts"]), FACT_KEYS)
        f = r["facts"]
        self.assertEqual((f["path"], f["url"], f["branch"], f["originUrl"], f["commit"], f["taskBranch"], f["branchTip"], f["merge"], f["count"]),
                         (self.main, self.url, "main", self.url, self.task_commit, "t-1-x", self.task_commit, True, 1))
        self.assertEqual((f["remoteNow"], f["localNow"]), (self.origin_head(), self.main_before))
        self.assertEqual(set(f["gate"]), {"at", "phase", "passed", "commit", "uncommitted"})
        self.assertEqual(set(f["allow"]), {"file", "sha", "mtime", "changedSinceCard", "entry"})
        self.assertFalse(f["allow"]["changedSinceCard"])
        self.assertEqual(set(f["commits"][0]), {"sha", "subject", "author", "fromTask"})
        flat = [" ".join(c) for c in f["commands"]]
        self.assertTrue(any(" merge --ff-only " in c for c in flat), flat)
        self.assertTrue(any(" push " in c and c.endswith(self.url + " " + self.task_commit + ":refs/heads/main") for c in flat), flat)
        self.assertEqual(len(r["seen"]), 64)
        self.assertEqual(set(r["caller"]), {"ok", "why", "ancestors"})
        # refused: exit 1, the first failing check named
        self.set_allow(enabled=False)
        code, out, _e = self.cli("pa-push-main", "check", CARD, "--space", SPACE, "--json")
        self.assertEqual(code, 1)
        r = json.loads(out)
        self.assertFalse(r["ok"])
        self.assertEqual(r["refusal"]["check"], "C6h")
        self.assertIsNone(r["seen"])
        code, _o, _e = self.cli("pa-push-main", "check", "d-nope", "--space", SPACE, "--json")
        self.assertEqual(code, 1)
        code, _o, _e = self.cli("pa-push-main", "check")
        self.assertEqual(code, 2)

    def test_p09_check_reports_caller(self):
        r = self.check()
        self.assertEqual(r["caller"], {"ok": True, "why": None, "ancestors": ["herdr-core", "Herdr", "launchd"]})
        r = self.check(env=dict(PERSON_ENV, CLAUDECODE="1"))
        self.assertFalse(r["caller"]["ok"])
        self.assertIn("CLAUDECODE", r["caller"]["why"])
        self.assertTrue(r["ok"])  # the caller line is shown; the facts are still checked
        r = self.check(env=dict(PERSON_ENV, HC_TERMINAL_ID="t9"))
        self.assertIn("Herdr terminal", r["caller"]["why"])
        r = pa_push.check_card(self.dir, SPACE, CARD, PERSON_ENV, None)
        self.assertIn("ps failed", r["caller"]["why"])

    def test_d01_decide_refuses_push_card(self):
        before = snapshot(self.dir)
        args = argparse.Namespace(space=SPACE, card=CARD, withdraw=False, answer="Push", quote="yes, push it", why=None)
        with self.assertRaises(pa.Problem) as e:
            pa.cmd_decide(args)
        self.assertIn("a push card is answered in the panel, not in conversation (its answer runs the push)", str(e.exception))
        c = self.card()
        del c["push"]  # recognised by kind alone too
        dump(c, self.card_path())
        with self.assertRaises(pa.Problem):
            pa.cmd_decide(args)
        c["push"], c["kind"] = {"version": 1}, "other"  # and by the push object alone
        dump(c, self.card_path())
        with self.assertRaises(pa.Problem):
            pa.cmd_decide(args)
        self.assertEqual(set(snapshot(self.dir)), set(before))

    def test_d02_decide_withdraw_works(self):
        args = argparse.Namespace(space=SPACE, card=CARD, withdraw=True, answer=None, quote=None, why="superseded by a new card")
        self.quiet(pa.cmd_decide, args)
        c = self.card()
        self.assertEqual((c["status"], c["push"]["done"]), ("withdrawn", False))
        self.assertNotEqual(self.origin_head(), self.task_commit)


# ---- A3: run's checks, one test per refusal -----------------------------------------------------------

class RunChecks(Runs):
    def test_r_c0_env_markers(self):
        for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_SANDBOX", "CODEX_HOME_SESSION", "CODEX_ANYTHING", "CLAUDE_CODE_SSE_PORT"):
            r = self.assertRefused(self.run_push(env=dict(PERSON_ENV, **{k: "1"})), "C0", k, attempts=0)
            self.assertIn("the person's command", r["refusal"]["message"])
        self.assertEqual(self.calls, [])  # nothing was read

    def test_r_c0_ancestor_claude(self):
        for tree in ([{"pid": 7, "comm": "/opt/bin/claude", "args": "claude --resume"}] + PANEL_TREE,
                     PANEL_TREE[:1] + [{"pid": 8, "comm": "node", "args": "node /usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"}]):
            self.assertRefused(self.run_push(tree=tree), "C0", "ancestor", attempts=0)
        self.assertEqual(self.calls, [])

    def test_r_c0_ancestor_codex(self):
        for tree in ([{"pid": 7, "comm": "codex", "args": "codex"}] + PANEL_TREE,
                     [{"pid": 8, "comm": "/usr/bin/node", "args": "node /x/@openai/codex/bin/codex.js"}]):
            self.assertRefused(self.run_push(tree=tree), "C0", "codex", attempts=0)

    def test_r_c0_ps_fails_closed(self):
        code, r = pa_push.run_push(SPACE, CARD, "Push", None, self.seen, PERSON_ENV, None, None)
        self.assertEqual((code, r["refusal"]["check"]), (1, "C0"))
        self.assertIn("ps failed", r["refusal"]["message"])
        self.assertIsNone(pa_push.ancestors(table={}))
        self.assertIsNone(pa_push.ancestors(pid=5, table={9: (1, "x", "x")}))
        tree = pa_push.ancestors(pid=5, table={5: (4, "python3", "python3 pa_push.py"), 4: (3, "/bin/zsh", "-zsh"), 3: (0, "launchd", "launchd")})
        self.assertEqual([a["comm"] for a in tree], ["/bin/zsh", "launchd"])

    def test_r_c0_hc_terminal(self):
        self.assertRefused(self.run_push(env=dict(PERSON_ENV, HC_TERMINAL_ID="t7")), "C0", "Herdr terminal", attempts=0)

    def test_r_c0_orphan(self):
        """A process whose parent chain holds nothing but the system's first process: the caller is unknown."""
        for tree in ([{"pid": 1, "comm": "/sbin/launchd", "args": "/sbin/launchd"}], [{"pid": 1, "comm": "launchd", "args": "launchd"}],
                     [{"pid": 1, "comm": "/sbin/init", "args": "/sbin/init splash"}], [{"pid": 1, "comm": "systemd", "args": "/sbin/init"}]):
            self.assertRefused(self.run_push(tree=tree), "C0", "the caller is not the panel", attempts=0)
        self.assertEqual(self.calls, [])
        tree = pa_push.ancestors(pid=5, table={5: (1, "python3", "python3 pa_push.py"), 1: (0, "/sbin/launchd", "/sbin/launchd")})
        self.assertEqual([a["pid"] for a in tree], [1])
        self.assertIn("the caller is not the panel", pa_push.caller_refusal(PERSON_ENV, tree) or "")
        self.assertIn("orphaned", pa_push.caller_refusal(PERSON_ENV, tree, strict=False) or "")
        self.assertIsNone(pa_push.caller_refusal(PERSON_ENV, PANEL_TREE))
        self.assertIsNone(pa_push.caller_refusal(PERSON_ENV, PERSON_TREE, strict=False))

    def test_r_c0_not_the_panel(self):
        """d-069: check and run pass only with the Herdr app or Herdr core among the ancestors (and no agent)."""
        launchd = {"pid": 1, "comm": "/sbin/launchd", "args": "/sbin/launchd"}
        user_systemd = {"pid": 41, "comm": "systemd", "args": "/usr/lib/systemd/systemd --user"}
        init = {"pid": 1, "comm": "systemd", "args": "/sbin/init splash"}
        detached = ([{"pid": 40, "comm": "/bin/sh", "args": "/bin/sh -c x; true"}, launchd],
                    [launchd],
                    [user_systemd, init],
                    [{"pid": 42, "comm": "sh", "args": "sh"}, user_systemd, init],
                    PERSON_TREE,
                    [{"pid": 43, "comm": "/bin/zsh", "args": "-zsh"}, {"pid": 44, "comm": "login", "args": "login -fp p"},
                     {"pid": 45, "comm": "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal", "args": "Terminal"}, launchd],
                    [{"pid": 46, "comm": "/tmp/herdrx", "args": "herdrx"}, launchd])
        for tree in detached:
            self.assertRefused(self.run_push(tree=tree), "C0", "the caller is not the panel", attempts=0)
            r = self.check(tree=tree)
            self.assertFalse(r["caller"]["ok"])
            self.assertIn("the caller is not the panel", r["caller"]["why"])
        for tree in (PANEL_TREE, PANEL_TREE[1:], [PANEL_TREE[0], launchd],
                     [{"pid": 47, "comm": "/bin/sh", "args": "sh -c pa-push-main"}] + PANEL_TREE,
                     [{"pid": 48, "comm": "herdr-core", "args": "herdr-core"}, user_systemd, init]):
            self.assertIsNone(pa_push.caller_refusal(PERSON_ENV, tree), tree)
        # the existing refusals still come first, with their own messages
        self.assertIn("CLAUDECODE", pa_push.caller_refusal(dict(PERSON_ENV, CLAUDECODE="1"), PANEL_TREE))
        self.assertIn("ancestor process is claude",
                      pa_push.caller_refusal(PERSON_ENV, [{"pid": 7, "comm": "claude", "args": "claude"}] + PANEL_TREE))
        self.assertIn("Herdr terminal", pa_push.caller_refusal(dict(PERSON_ENV, HC_TERMINAL_ID="t1"), PANEL_TREE))
        self.assertIn("ps failed", pa_push.caller_refusal(PERSON_ENV, None))
        # pa-push-allow (typed by the person in a plain terminal) keeps today's rule
        self.assertIsNone(pa_push.caller_refusal(PERSON_ENV, PERSON_TREE, strict=False))
        self.assertIsNone(pa_push.caller_refusal(PERSON_ENV, detached[0], strict=False))
        self.assertIn("orphaned", pa_push.caller_refusal(PERSON_ENV, [launchd], strict=False))
        self.assertEqual(set(pa_push.PANEL_NAMES), {"herdr", "herdr-core"})

    def test_r_c0_cli_refuses_in_this_session(self):
        """The real command line, started from this agent session: refused by C0, with the JSON contract."""
        code, out, err = self.cli("pa-push-main", "run", CARD, "--answer", "Push", "--space", SPACE, "--seen", self.seen, "--json",
                                  env=dict(os.environ, CLAUDECODE="1"))
        self.assertEqual(code, 1, err)
        r = json.loads(out)
        self.assertEqual(set(r), {"ok", "done", "answer", "refusal", "result", "message"})
        self.assertEqual(r["refusal"]["check"], "C0")
        self.assertEqual(self.origin_head(), self.origin_before)
        self.assertEqual(self.card()["push"]["attempts"], [])
        code, _o, _e = self.cli("pa-push-main", "run", CARD, "--space", SPACE)
        self.assertEqual(code, 2)  # --answer is required: a usage error

    def test_r_c1_no_space_state(self):
        self.assertRefused(self.run_push(space="sp9"), "C1", "no Project Agent state for space sp9", attempts=0)
        self.assertRefused(self.run_push(space=None), "C1", "--space", attempts=0)

    def test_r_c2_lock_held(self):
        with pa_push.push_lock(self.dir):
            t = time.monotonic()
            self.assertRefused(self.run_push(), "C2", "another push for this space is running", attempts=0)
            self.assertGreaterEqual(time.monotonic() - t, 4)

    def test_r_c3_not_push_card(self):
        dump({"id": "d-1", "question": "Q", "options": [{"label": "Push"}], "status": "open"}, os.path.join(self.dir, "decisions", "d-1.json"))
        self.assertRefused(self.run_push(card="d-1"), "C3", "d-1 is not a push card", attempts=0)
        self.assertRefused(self.run_push(card="d-none"), "C3", "no decision card d-none", attempts=0)
        self.assertRefused(self.run_push(card="../x"), "C3", "no decision card", attempts=0)
        self.card_edit(version=2)
        self.assertRefused(self.run_push(), "C3", "push version 2", attempts=0)

    def test_r_c3_other_space(self):
        self.card_edit(space="sp2")
        self.assertRefused(self.run_push(), "C3", "belongs to space sp2, not sp1", attempts=0)

    def test_r_c4_answered_or_withdrawn(self):
        for status in ("withdrawn", "answered"):
            c = self.card()
            c["status"] = status
            dump(c, self.card_path())
            self.assertRefused(self.run_push(), "C4", "{} is {}; nothing was pushed".format(CARD, status), attempts=0)

    def test_r_c5_bad_answer(self):
        self.assertRefused(self.run_push(answer="Yes"), "C5", "--answer must be one of: Push, Not now", attempts=0)

    def test_r_c6a(self):
        os.remove(self.allow)
        self.assertRefused(self.run_push(), "C6a", "no push allowlist")

    def test_r_c6b(self):
        write(self.allow, '{"version": 1, "enabled": true, "repos": [], "x": 1}')
        self.assertRefused(self.run_push(), "C6b", "the push allowlist is not valid: unknown key x")

    def test_r_c6c(self):
        dump(dict(load(self.allow), enabled=False), self.allow)
        self.assertRefused(self.run_push(), "C6c", "turned off (enabled: false)")

    def test_r_c6d(self):
        self.set_allow(path=os.path.join(self.tmp, "elsewhere"))
        self.assertRefused(self.run_push(), "C6d", "no allowlist entry for " + self.main)

    def test_r_c6e(self):
        self.card_edit(url="file://" + os.path.join(self.tmp, "other.git"))
        self.assertRefused(self.run_push(), "C6e", "has URL " + self.url)

    def test_r_c6f(self):
        self.card_edit(branch="dev")
        self.assertRefused(self.run_push(), "C6f", "has branch main, the card says dev")

    def test_r_c6g(self):
        self.set_allow(space="sp2")
        self.assertRefused(self.run_push(), "C6g", "names space sp2, not this space sp1")

    def test_r_c6h(self):
        self.set_allow(enabled=False)
        self.assertRefused(self.run_push(), "C6h", "is turned off")

    def test_r_c6i(self):
        self.set_allow(note="changed after the card")
        self.assertRefused(self.run_push(), "C6i", "changed since this card was made")

    def test_r_order_earlier_check_wins(self):
        self.set_allow(enabled=False)  # C6h, and the file's SHA changes too (C6i)
        self.assertRefused(self.run_push(), "C6h", "turned off")
        self.set_allow(enabled=True, note="x")  # C6i
        self.commit_on(self.wt, "late.txt", "late")  # C8f too
        self.assertRefused(self.run_push(), "C6i", "changed", attempts=2)

    def test_r_c7b_hookspath(self):
        sh(self.main, "git", "config", "core.hooksPath", os.path.join(self.tmp, "planted"))
        self.assertRefused(self.run_push(), "C7b", "has git setting core.hookspath (local)")

    def test_r_c7b_sshcommand(self):
        sh(self.main, "git", "config", "core.sshCommand", "/tmp/planted")
        self.assertRefused(self.run_push(), "C7b", "core.sshcommand")
        sh(self.main, "git", "config", "--unset", "core.sshCommand")
        sh(self.main, "git", "config", "remote.origin.pushurl", "file:///elsewhere")
        self.assertRefused(self.run_push(), "C7b", "remote.origin.pushurl", attempts=2)

    def test_r_c7c_pushinsteadof(self):
        gitconfig = os.path.join(os.environ["HOME"], ".gitconfig")
        self.addCleanup(lambda: os.path.exists(gitconfig) and os.remove(gitconfig))
        write(gitconfig, '[url "file:///nowhere/"]\n\tpushInsteadOf = file://{}\n'.format(TMP))
        self.assertRefused(self.run_push(), "C7c", "a URL rewrite")

    def test_r_c8a_evidence_removed(self):
        os.remove(os.path.join(self.dir, "evidence", TASK, "REVIEW.md"))
        self.assertRefused(self.run_push(), "C8a", "does not pass now: REVIEW.md (missing)")
        self.ledger_task(workers=[{"address": "t31", "role": "build"}])
        write(os.path.join(self.dir, "evidence", TASK, "REVIEW.md"), "x\n")
        self.assertRefused(self.run_push(), "C8a", "reviewer is not the author", attempts=2)

    def test_r_c8b_phase(self):
        self.gate(phase="build")
        self.assertRefused(self.run_push(), "C8b", "is from phase build")

    def test_r_c8c_no_pin(self):
        self.gate_edit(no_pin=True)
        self.assertRefused(self.run_push(), "C8c", "pins no commit for this repository")

    def test_r_c8d_uncommitted(self):
        self.gate_edit(uncommitted=2)
        self.assertRefused(self.run_push(), "C8d", "2 uncommitted file(s)")

    def test_r_c8e_pin_differs(self):
        self.gate_edit(commit=self.main_before)
        self.assertRefused(self.run_push(), "C8e", "the gate passed {}, the card says {}".format(self.main_before[:7], self.task_commit[:7]))

    def test_r_c8f_commit_after_gate(self):
        late = self.commit_on(self.wt, "late.txt", "after the gate")
        self.assertRefused(self.run_push(), "C8f", "commits were added to t-1-x after the gate (gate {}, branch now {})".format(
            self.task_commit[:7], late[:7]))

    def test_r_c8g_live_worktree_moved(self):
        sh(self.wt, "git", "checkout", "-q", "--detach", "HEAD~1")
        self.assertRefused(self.run_push(), "C8g", "not at the gated commit now")
        sh(self.wt, "git", "checkout", "-q", "t-1-x")
        write(os.path.join(self.wt, "b.txt"), "changed, not committed\n")
        self.assertRefused(self.run_push(), "C8g", "1 uncommitted change(s)", attempts=2)

    def test_r_c9_short_sha(self):
        """A short id never reaches git: with the gate pin and the card both short, the branch tip (always a full
        id) differs, so C8f refuses before C9 (C9 is the backstop)."""
        self.gate_edit(commit=self.task_commit[:7])
        self.card_edit(commit=self.task_commit[:7])
        self.assertRefused(self.run_push(), "C8f", "commits were added")
        self.assertFalse(any(self.task_commit[:7] in a for a in self.calls), self.calls)

    def test_r_c10a_local_moved(self):
        self.commit_on(self.main, "m.txt", "on main after the card")
        self.main_before = self.head(self.main)
        self.assertRefused(self.run_push(), "C10a", "local main moved since the card")

    def test_r_c10a_not_ancestor(self):
        x = self.commit_on(self.main, "m.txt", "diverged")
        self.main_before = x
        self.card_edit(localBefore=x)
        self.assertRefused(self.run_push(), "C10a", "local main ({}) is not an ancestor of {}".format(x[:7], self.task_commit[:7]))

    def test_r_c10b_not_on_branch(self):
        sh(self.main, "git", "checkout", "-q", "-b", "other")
        self.assertRefused(self.run_push(), "C10b", "is on other, not main")

    def test_r_c10b_dirty(self):
        write(os.path.join(self.main, "a.txt"), "dirty\n")
        self.assertRefused(self.run_push(), "C10b", "1 uncommitted change(s) to tracked files")

    def test_r_c11a_unreadable_url(self):
        moved = self.origin + ".away"
        os.rename(self.origin, moved)
        try:
            self.assertRefused(self.run_push(), "C11a", "could not read " + self.url, origin=False)
        finally:
            os.rename(moved, self.origin)
        self.assertEqual(self.origin_head(), self.origin_before)

    def test_r_c11a_no_branch(self):
        sh(self.origin, "git", "update-ref", "-d", "refs/heads/main")
        self.assertRefused(self.run_push(), "C11a", "has no branch main", origin=False)
        self.assertEqual(sh(self.origin, "git", "for-each-ref"), "")

    def test_r_c11b_remote_moved(self):
        other = os.path.join(self.tmp, "other")
        sh(self.tmp, "git", "clone", "-q", self.url, other)
        self.commit_on(other, "o.txt", "someone else")
        sh(other, "git", "push", "-q", "origin", "main")
        self.origin_before = self.origin_head()
        self.assertRefused(self.run_push(), "C11b", "remote main moved since the card")

    def test_r_c11c_rewritten(self):
        sh(self.main, "git", "checkout", "-q", "-b", "side", self.origin_before)
        x = self.commit_on(self.main, "s.txt", "rewritten history")
        sh(self.main, "git", "push", "-q", "origin", "side")
        sh(self.main, "git", "checkout", "-q", "main")
        sh(self.origin, "git", "update-ref", "refs/heads/main", x)
        self.origin_before = x
        self.card_edit(remoteBefore=x)
        self.assertRefused(self.run_push(), "C11c", "remote main ({}) is not an ancestor of {}; only a fast-forward".format(x[:7], self.task_commit[:7]))

    def test_r_c12_seen_missing(self):
        self.assertRefused(self.run_push(seen=""), "C12", "--seen is required")

    def test_r_c12_seen_differs(self):
        self.assertRefused(self.run_push(seen="0" * 64), "C12", "the facts changed since you looked")
        # a re-run of the gate after the person looked changes the token too
        time.sleep(1.1)
        self.gate()
        self.assertRefused(self.run_push(), "C12", "the facts changed since you looked", attempts=2)

    def test_r_c13_prepush_fails(self):
        self.replan(prePush=["/bin/sh", "-c", "echo checking; echo bad thing >&2; exit 3"])
        before = {n for n in os.listdir(tempfile.gettempdir()) if n.startswith("pa-push-")}
        r = self.assertRefused(self.run_push(), "C13", "the allowlist's prePush check failed (exit 3)")
        self.assertIn("bad thing", r["refusal"]["message"])
        leftovers = {n for n in os.listdir(tempfile.gettempdir()) if n.startswith("pa-push-")} - before
        self.assertEqual(leftovers, set(), "the temporary clone and hooks folder are removed")

    def test_r_deadline(self):
        r = self.assertRefused(self.run_push(deadline=time.monotonic() + 100), "deadline", "ran out of time before pushing; nothing was pushed")
        self.assertIn("nothing was pushed", r["message"])


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


# ---- fix round 2: the gate's git in the task worktree is the tool's hardened git ----------------------

class GateGit(Runs):
    """The task worktree is the agent's: nothing its git settings name may run inside plan, check or run."""

    def program(self, name):
        self.mark = os.path.join(self.tmp, "RAN-" + name)
        prog = os.path.join(self.tmp, name + ".sh")
        write(prog, "#!/bin/sh\necho ran >> '{}'\ncat >/dev/null\nexit 1\n".format(self.mark))
        os.chmod(prog, 0o755)
        return prog

    def worktree_scope(self):
        """extensions.worktreeConfig on, as a Bash append would turn it on (not a risky key), with an empty main file."""
        sh(self.main, "git", "config", "--local", "extensions.worktreeConfig", "true")
        write(os.path.join(self.main, ".git", "config.worktree"), "")

    def assertNeverRan(self):
        self.assertFalse(os.path.exists(self.mark), "a program from the task worktree's git settings ran")

    def test_g01_worktree_fsmonitor_check_and_run(self):
        prog = self.program("fsmonitor")
        self.worktree_scope()
        sh(self.wt, "git", "config", "--worktree", "core.fsmonitor", prog)
        r = self.check()
        self.assertNeverRan()
        self.assertFalse(r["ok"])
        self.assertEqual(r["refusal"]["check"], "C8a", r["refusal"])
        self.assertIn("core.fsmonitor", r["refusal"]["message"])
        r = self.assertRefused(self.run_push(), "C8a", "core.fsmonitor")
        self.assertIn("worktree", r["refusal"]["message"])
        self.assertNeverRan()
        # and plan refuses a new card the same way
        os.remove(self.card_path())
        code, _o, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("core.fsmonitor", err)
        self.assertNeverRan()

    def test_g02_redirected_git_file(self):
        prog = self.program("redirect")
        other = os.path.join(self.tmp, "other")
        sh(self.tmp, "git", "init", "-q", other)
        sh(other, "git", "config", "core.fsmonitor", prog)
        write(os.path.join(self.wt, ".git"), "gitdir: {}/.git\n".format(other))
        self.assertRefused(self.run_push(), "C8a", "core.fsmonitor")
        self.assertNeverRan()

    def test_g03_worktree_filter(self):
        prog = self.program("filter")
        self.worktree_scope()
        sh(self.wt, "git", "config", "--worktree", "filter.planted.clean", prog)
        write(os.path.join(self.main, ".git", "info", "attributes"), "* filter=planted\n")
        later = time.time() + 5
        os.utime(os.path.join(self.wt, "b.txt"), (later, later))
        self.assertRefused(self.run_push(), "C8a", "filter.planted.clean")
        self.assertNeverRan()

    def test_g04_gate_reads_only_through_git_call(self):
        """plan, check and run evaluate the gate with git_call's git (hooks and fsmonitor off, no GIT_*), never pa_hooks.git."""
        def forbidden(*a, **k):
            raise AssertionError("pa_hooks.git ran in pa-push-main: {}".format(a))
        real = pa_push.pa_hooks.git
        pa_push.pa_hooks.git = forbidden
        try:
            os.remove(self.card_path())
            code, _o, err = self.plan()
            self.assertEqual(code, 0, err)
            self.calls[:] = []
            code, r = self.run_push(seen=FRESH)
        finally:
            pa_push.pa_hooks.git = real
        self.assertEqual(code, 0, r)
        self.assertEqual(self.origin_head(), self.task_commit)
        wt = os.path.realpath(self.wt)
        status = [a for a in self.calls if a[:3] == ["git", "-C", wt] and "status" in a]
        self.assertGreaterEqual(len(status), 2, self.calls)  # the gate's and the live pin's, in check and run
        self.assertTrue(all("core.fsmonitor=false" in a and any(x.startswith("core.hooksPath=") for x in a) for a in status), status)
        self.assertTrue(all("--ignore-submodules=all" in a for a in status), status)

    def nested_repo(self, commit=True):
        """A nested repository wt/sub (a submodule entry), committed or only staged in the task worktree."""
        sub = os.path.join(self.wt, "sub")
        os.makedirs(sub)
        sh(sub, "git", "init", "-q")
        write(os.path.join(sub, "s.txt"), "s\n")
        sh(sub, "git", "add", "s.txt")
        sh(sub, "git", "commit", "-q", "-m", "s")
        write(os.path.join(self.wt, ".gitmodules"), '[submodule "sub"]\n\tpath = sub\n\turl = ./sub\n')
        sh(self.wt, "git", "add", ".gitmodules", "sub")
        if commit:
            sh(self.wt, "git", "commit", "-q", "-m", "add sub")
        return sub

    def plant_in_nested(self, sub):
        """The agent names a program in the nested repository's own settings (no place C8a's settings read covers)."""
        prog = self.program("nested")
        sh(sub, "git", "config", "filter.planted.clean", prog)
        write(os.path.join(sub, ".git", "info", "attributes"), "* filter=planted\n")
        later = time.time() + 5
        os.utime(os.path.join(sub, "s.txt"), (later, later))

    def test_g05_nested_repository_committed(self):
        sub = self.nested_repo(commit=True)
        self.assertEqual(self.gate(), 0)
        self.plant_in_nested(sub)
        r = self.check()
        self.assertNeverRan()
        self.assertFalse(r["ok"])
        self.assertEqual(r["refusal"]["check"], "C8a", r["refusal"])
        self.assertIn("nested repository", r["refusal"]["message"])
        self.assertIn("sub", r["refusal"]["message"])
        self.assertIn("committed", r["refusal"]["message"])
        self.assertRefused(self.run_push(), "C8a", "nested repository")
        self.assertNeverRan()
        os.remove(self.card_path())
        code, _o, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("nested repository", err)
        self.assertNeverRan()

    def test_g06_nested_repository_staged_only(self):
        sub = self.nested_repo(commit=False)
        self.plant_in_nested(sub)
        r = self.check()
        self.assertNeverRan()
        self.assertEqual(r["refusal"]["check"], "C8a", r["refusal"])
        self.assertIn("nested repository", r["refusal"]["message"])
        self.assertIn("staged", r["refusal"]["message"])
        self.assertRefused(self.run_push(), "C8a", "sub")
        self.assertNeverRan()

    def test_g07_status_calls_do_not_look_inside_nested_repositories(self):
        """Behind C8a: the gate's status (through the runner) and the live pin's status never enter a nested repository."""
        sub = self.nested_repo(commit=True)
        self.plant_in_nested(sub)
        s = pa_push.Session()
        try:
            wt = os.path.realpath(self.wt)
            pins = pa_hooks.gate_commits(wt, runner=s.gate_git)
            self.assertEqual(len(pins), 1)
            self.assertNeverRan()
            self.assertEqual(pa_push.tracked_changes(s, wt), [])
            self.assertNeverRan()
        finally:
            s.close()
        status = [a for a in self.calls if "status" in a]
        self.assertTrue(status and all("--ignore-submodules=all" in a for a in status), status)

# ---- A5: links --------------------------------------------------------------------------------

class Links(unittest.TestCase):
    def read(self, rel):
        with open(os.path.join(REPO, rel)) as f:
            return f.read()

    def test_i01_install_links_both_names(self):
        lines = [l for l in self.read("install.sh").splitlines() if "pa_push.py" in l]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("for n in pa-push-main pa-push-allow; do", lines[0])
        self.assertIn('ln -sfn "$PA/tools/pa_push.py" "$HOME/.local/bin/$n"', lines[0])

    def test_i02_parse37_lists_pa_push(self):
        line = next(l for l in self.read("tests/run-all.sh").splitlines() if "parses as Python 3.7" in l)
        self.assertIn("runtime/tools/pa_push.py", line)


if __name__ == "__main__":
    unittest.main()
