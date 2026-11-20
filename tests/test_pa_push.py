"""Unit tests for pa-push-main and pa-push-allow (t-007 part 7b, Job A): the allowlist, plan and check, pa-decide, the links.

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

class T192PushJob(unittest.TestCase):
    """t-192 R2: which gate a push of one repository reads, from the gate files alone (push_job)."""

    def test_the_job_whose_gate_pins_the_repository(self):
        import tempfile
        d = tempfile.mkdtemp(dir=TMP)
        ev = os.path.join(d, "evidence", "t-1")
        a, b = os.path.join(d, "a.git"), os.path.join(d, "b.git")
        pin = lambda common: {"phase": "review", "passed": True, "commits": [{"common": common, "commit": "1" * 40}]}
        task = {"id": "t-1", "worktree": os.path.join(d, "wt"), "worktrees": [
            {"path": os.path.join(d, "wt"), "job": "main"}, {"path": os.path.join(d, "wt2"), "job": "j2"},
            {"path": os.path.join(d, "wt3"), "job": "j3"}, {"path": os.path.join(d, "wt4"), "job": "j4", "removed": "2026-01-01T00:00:00Z"},
            {"path": os.path.join(d, "x"), "job": "../x"}]}
        dump(pin(a), os.path.join(ev, "GATE.json"))
        dump(pin(b), os.path.join(ev, "GATE-j2.json"))
        dump(pin(b), os.path.join(ev, "GATE-j4.json"))  # removed: never read
        write(os.path.join(ev, "GATE-..", "x.json"), "{}")
        self.assertIsNone(pa_push.push_job(d, task, a), "the main job's gate pins it: as before")
        self.assertEqual(pa_push.push_job(d, task, b), "j2")
        self.assertIsNone(pa_push.push_job(d, task, os.path.join(d, "c.git")), "no gate pins it: as before (C8c refuses)")
        dump(pin(b), os.path.join(ev, "GATE-j3.json"))
        self.assertIsNone(pa_push.push_job(d, task, b), "two jobs pin it: none chosen (C8c refuses)")
        dump(pin(b), os.path.join(ev, "GATE.json"))
        self.assertIsNone(pa_push.push_job(d, task, b), "GATE.json pins it: the main job")


class T192R3PushCardJobFiles(Planned):
    """t-192 round 3 (RL1): a push card for a job's gate names that job's files as its evidence."""

    def test_rl1_the_push_card_of_a_job_names_its_gate_and_review(self):
        self.ledger_task(worktree=os.path.join(self.tmp, "elsewhere"),
                         worktrees=[{"path": os.path.join(self.tmp, "elsewhere"), "job": "main"}, {"path": self.wt, "job": "j2"}])
        ev = os.path.join(self.dir, "evidence", TASK)
        for name in ("VERIFY.md", "REVIEW.md"):
            write(os.path.join(ev, name.replace(".md", "-j2.md")), pa.read_text(os.path.join(ev, name)))
            os.unlink(os.path.join(ev, name))
        os.unlink(os.path.join(ev, "GATE.json"))  # Planned's main-job gate: this task's gate is the job's
        saved = sys.argv
        sys.argv = ["pa-gate", TASK, "--job", "j2", "--space", SPACE]
        try:
            code, out, err = self.quiet(pa_hooks.main)
        finally:
            sys.argv = saved
        self.assertIn(code, (0, None), out + err)
        self.assertTrue(os.path.exists(os.path.join(ev, "GATE-j2.json")), out + err)
        self.assertFalse(os.path.exists(os.path.join(ev, "GATE.json")))
        code, out, err = self.plan()
        self.assertIn(code, (0, None), out + err)
        self.assertEqual(self.card()["evidence"], ["GATE-j2.json", "REVIEW-j2.md"])


if __name__ == "__main__":
    unittest.main()
