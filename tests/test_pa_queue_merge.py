"""pa-queue's queue card, merge, refresh's merge and brief (t-052 J3b, plan.md JJ.5 M1-M16), RL-J3c-2 and the rule
r-disjoint-carry (a clean disjoint rebase keeps the suite pass, d-202).

Run: python3 -m unittest -v tests/test_pa_queue_merge.py (tests/run-all.sh runs it in shards, tests/unit_shards.py)
Every case runs in scratch repositories under a mkdtemp folder and a fake space (tests/queue_fixtures.py). The only
remote is a bare repository under that folder (file://); its refs are compared byte for byte. The push allowlist is
the temp home's own. No real space, queue.json, push card or push.
"""
import fcntl
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from queue_fixtures import *  # noqa: F401,F403
from queue_fixtures import setUpModule, tearDownModule  # noqa: F401
from unit_shards import load_tests  # noqa: F401 (tests/run-all.sh runs this module in shards)

LIMITS = "Recorded limits: t-A none; t-B none; t-C none; t-D none"
ALL = "Merge all in order"


def review_file(tip, reviewer, verdict="pass", independent=None):
    lines = ["# short review", "", "Start: " + pa.now(), "End: " + pa.now(), "Reviewer: " + reviewer, ""]
    if independent:
        lines.append("Independent: " + independent)
    lines += ["", "Checked: " + tip, "Verdict: " + verdict]
    return "\n".join(lines) + "\n"


class MBase(QBase):
    """QBase, plus a bare origin.git (proj's only remote, main pushed) and a decisions folder."""

    def setUp(self):
        super().setUp()
        self.origin = os.path.join(self.tmp, "origin.git")
        sh(self.tmp, "git", "init", "-q", "--bare", self.origin)
        self.url = "file://" + self.origin
        sh(self.proj, "git", "remote", "add", "origin", self.url)
        sh(self.proj, "git", "push", "-q", "origin", "main")
        self.m0 = self.ref("main")
        os.makedirs(os.path.join(self.dir, "decisions"), exist_ok=True)

    # ---- facts
    def remote(self):
        return sh(self.origin, "git", "for-each-ref", "--format=%(refname) %(objectname)")

    def card(self, cid):
        return pa.read_json(os.path.join(self.dir, "decisions", cid + ".json"))

    def queue_cards(self):
        folder = os.path.join(self.dir, "decisions")
        return sorted(n[:-5] for n in os.listdir(folder) if n.startswith("d-queue-") and n.endswith(".json"))

    def merged(self):
        return [m["task"] for m in self.qrepo().get("merged") or [] if m.get("by") == "queue"]

    def approval(self, tid):
        return self.entry(tid).get("approval")

    def allow(self):
        dump({"version": 1, "enabled": True, "repos": [{"path": self.proj, "url": self.url, "branch": "main", "space": SPACE,
                                                         "enabled": True}]}, os.path.join(pa.ROOT, "push-allow.json"))

    # ---- steps
    def joined(self, tid, f, start="main"):
        self.task(tid, [(f, tid + "\n")], start=start)
        self.add(tid)
        self.assertEqual(self.gate(tid)[0], 0, tid)
        return self.tip(tid)

    def three(self):
        for t, f in (("t-A", "fa.txt"), ("t-B", "fb.txt"), ("t-C", "fc.txt")):
            self.joined(t, f)

    def refresh(self, *extra):
        return self.queue("refresh", "--repo", self.proj, *extra)

    def passed(self, *tids):
        for t in tids:
            self.suite(t, 0)
        code, out, err = self.refresh("--no-merge")
        self.assertEqual(code, 0, out + err)

    def make_card(self, limits=LIMITS):
        return self.queue("card", "--repo", self.proj, "--limits", limits)

    def answer(self, cid, label):
        code, out, err = self.run_main(pa, ["pa-decide", cid, "--answer", label, "--quote", "yes: " + label, "--space", SPACE])
        self.assertEqual(code, 0, out + err)

    def approve(self, label=ALL, cid="d-queue-1"):
        code, out, err = self.make_card()
        self.assertEqual(code, 0, out + err)
        self.answer(cid, label)

    def merge(self):
        return self.queue("merge", "--repo", self.proj)

    def brief(self, tid):
        return self.queue("brief", tid, "--repo", self.proj)


# ---- M1, M2: the queue card (R10 AC10.1-10.3, Q-J3-2) ------------------------------------------------

class M1Card(MBase):
    def test_m1_one_card_for_the_ready_front(self):
        self.three()
        self.passed("t-A")
        self.assertEqual([e["state"] for e in self.entries()], ["passed", "checking", "checking"])
        code, out, err = self.make_card()
        self.assertEqual(code, 0, out + err)
        self.assertIn("d-queue-1", out)
        c = self.card("d-queue-1")
        self.assertEqual((c["kind"], c["task"], c["status"]), ("queue", "t-A", "open"))
        self.assertEqual([o["label"] for o in c["options"]], [ALL, "Merge up to t-B", "Merge up to t-A", "Not now"])
        self.assertEqual(c["recommendation"], ALL)
        self.assertEqual(c["evidence"], ["REVIEW.md"])
        q = c["queue"]
        self.assertEqual((q["version"], q["repo"], q["mainBefore"]), (1, self.proj, self.m0))
        self.assertEqual([(x["task"], x["state"]) for x in q["entries"]], [("t-A", "passed"), ("t-B", "checking"), ("t-C", "checking")])
        for x, e in zip(q["entries"], self.entries()):
            for k in ("branch", "tip", "onto", "base"):
                self.assertEqual(x[k], e[k], k)
            self.assertEqual((x["change"], x["sequence"], x["verify"]), (e["checked"]["change"], e["checked"]["sequence"], e["checked"]["verify"]))
        self.assertIn(LIMITS, c["context"])
        for t in ("t-A", "t-B", "t-C"):
            self.assertIn("evidence/{}/".format(t), c["context"])
        self.assertEqual(len(self.journal("card")), 1)

    def test_m1b_limits_must_name_each_task(self):
        self.three()
        code, out, err = self.make_card("Recorded limits: t-A none; t-B none")
        self.assertEqual(code, 1, out + err)
        self.assertIn("t-C", err)
        self.assertEqual(self.queue_cards(), [])

    def test_m1d_limits_that_do_not_fit_are_refused_not_cut(self):
        """t-109 MF-1 (AC10.3): the facts plus --limits over the card's context cap refuse; no limit is ever cut."""
        self.three()
        limits = LIMITS + "; t-C: RL-1 " + "x" * pa_hooks.CONTEXT_MAX + " END-OF-LIMITS"
        code, out, err = self.make_card(limits)
        self.assertEqual(code, 1, out + err)
        self.assertIn("refused", err)
        self.assertIn("fewer entries", err)
        self.assertIn("shorten", err)
        self.assertEqual(self.queue_cards(), [])
        self.assertEqual(self.journal("card"), [])
        code, out, err = self.make_card()
        self.assertEqual(code, 0, out + err)
        context = self.card("d-queue-1")["context"]
        self.assertTrue(context.endswith(LIMITS), context[-80:])
        self.assertLessEqual(len(context), pa_hooks.CONTEXT_MAX)

    def test_m1c_an_entry_on_an_open_card_is_not_listed_again(self):
        self.three()
        self.assertEqual(self.make_card()[0], 0)
        code, out, err = self.make_card()
        self.assertEqual(code, 1, out + err)
        self.assertIn("no entry", err)
        self.assertEqual(self.queue_cards(), ["d-queue-1"])

    def test_m1d_a_changed_entry_ends_the_list(self):
        self.three()
        self.change("t-B", "fb.txt", "other\n")  # a commit after the check: B is not ready, so C is not listed
        code, out, err = self.make_card()
        self.assertEqual(code, 0, out + err)
        self.assertEqual([x["task"] for x in self.card("d-queue-1")["queue"]["entries"]], ["t-A"])
        self.assertEqual([o["label"] for o in self.card("d-queue-1")["options"]], [ALL, "Not now"])


class M2SecondCard(MBase):
    def test_m2_a_second_card_for_the_new_entry_only(self):
        self.joined("t-A", "fa.txt")
        self.joined("t-B", "fb.txt")
        self.assertEqual(self.make_card()[0], 0)
        self.joined("t-C", "fc.txt")
        code, out, err = self.make_card()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.queue_cards(), ["d-queue-1", "d-queue-2"])
        self.assertEqual([x["task"] for x in self.card("d-queue-2")["queue"]["entries"]], ["t-C"])
        self.assertEqual(self.card("d-queue-2")["task"], "t-C")

    def test_m2b_the_card_id_skips_an_archived_one(self):
        dump({"id": "d-queue-1", "kind": "queue", "status": "answered"}, os.path.join(self.dir, "decisions-archive", "d-queue-1.json"))
        self.joined("t-A", "fa.txt")
        code, out, err = self.make_card()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.queue_cards(), ["d-queue-2"])


# ---- M3, M4, M7: R9 at merge time, in order (AC9.1) ---------------------------------------------------

class M3Merge(MBase):
    def setUp(self):
        super().setUp()
        self.three()
        self.passed("t-A", "t-B", "t-C")
        self.approve()

    def stops_at_b(self, word, void):
        a_tip = self.tip("t-A")
        code, out, err = self.merge()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.ref("main"), a_tip, out + err)
        self.assertEqual(self.merged(), ["t-A"])
        self.assertEqual([e["task"] for e in self.entries()], ["t-B", "t-C"])
        self.assertIn("stopped at t-B", out)
        self.assertIn(word, out)
        stopped = self.journal("merge-stopped")
        self.assertEqual(stopped[-1]["task"], "t-B")
        self.assertIn(word, stopped[-1]["why"])
        b, c = self.approval("t-B"), self.approval("t-C")
        if void:
            self.assertIn(word, b.get("void") or "")
            self.assertIn("open a new card", out)
            self.assertEqual(self.journal("void")[-1]["task"], "t-B")
        else:
            self.assertFalse(b.get("void"), b)
            self.assertEqual(self.journal("void"), [])
        self.assertFalse(c.get("void"), c)
        self.assertEqual(self.remote(), "refs/heads/main " + self.m0)
        return out

    def test_m3_all_merge_in_order(self):
        code, out, err = self.merge()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.merged(), ["t-A", "t-B", "t-C"])
        self.assertEqual(self.ref("main"), self.tip("t-C"))
        self.assertEqual(self.entries(), [])
        m = self.qrepo()["merged"]
        self.assertEqual([x["from"] for x in m], [self.m0, m[0]["to"], m[1]["to"]])
        for x in m:
            self.assertEqual((x["by"], x["card"]), ("queue", "d-queue-1"))
            self.assertEqual(x["tip"], x["to"])
            self.assertEqual(set(x["suite"]) >= {"tree", "worker", "started", "ended"}, True, x)
            self.assertEqual(x["commits"], 1)
        self.assertEqual([l["task"] for l in self.journal("merge")], ["t-A", "t-B", "t-C"])
        self.assertIn("pushing is the person's step", out)
        self.assertEqual(self.card("d-queue-1")["queue"]["applied"]["approved"], [
            {"task": "t-A", "job": None}, {"task": "t-B", "job": None}, {"task": "t-C", "job": None}])
        self.assertEqual(self.remote(), "refs/heads/main " + self.m0)

    def test_m3_1_approval_for_other_content(self):
        self.merge_card_applied_then(lambda r: r["entries"][1]["approval"].update(change="0" * 64))
        self.stops_at_b("content changed", void=True)

    def merge_card_applied_then(self, change):
        """The card applied by refresh (it records approvals even with --no-merge), then queue.json changed."""
        code, out, err = self.refresh("--no-merge")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.approval("t-B")["card"], "d-queue-1")
        self.set_entries(change)

    def test_m3_2_checks_saw_other_content(self):
        self.checked("t-B", self.tip("t-A"))  # VERIFY.md now names another commit
        self.stops_at_b("did not see", void=True)

    def test_m3_3_a_rebase_that_was_not_clean(self):
        self.merge_card_applied_then(lambda r: r["entries"][1]["rebases"].append(
            {"at": pa.now(), "from": "1" * 40, "to": "2" * 40, "onto": "t-A-x", "result": "changed", "files": ["x"]}))
        self.stops_at_b("a rebase that was not clean", void=True)

    def test_m3_3b_a_commit_after_the_check(self):
        self.change("t-B", "fb.txt", "later\n")
        self.stops_at_b("a commit after the check", void=True)

    def test_m3_4_gate_fails(self):
        os.remove(os.path.join(self.evidence("t-B"), "REVIEW.md"))
        self.stops_at_b("gate", void=False)

    def test_m3_4b_worktree_dirty(self):
        write(os.path.join(self.trees["t-B"], "fb.txt"), "dirty\n")
        self.stops_at_b("uncommitted", void=False)

    def test_m3_5_not_passed(self):
        lines = read(os.path.join(self.dir, "content", "suites.jsonl")).splitlines()
        tree_b = sh(self.trees["t-B"], "git", "rev-parse", "HEAD^{tree}")
        write(os.path.join(self.dir, "content", "suites.jsonl"),
              "".join(l + "\n" for l in lines if json.loads(l).get("tree") != tree_b))
        self.stops_at_b("not passed", void=False)

    def test_m3_6_main_checkout_dirty_between_merges(self):
        real = pa_queue.ff_merge

        def dirty_after(ctx, rec, e, tip):
            out = real(ctx, rec, e, tip)
            write(os.path.join(self.proj, "README"), "dirty\n")
            return out
        pa_queue.ff_merge = dirty_after
        try:
            self.stops_at_b("main checkout", void=False)
        finally:
            pa_queue.ff_merge = real


class M3MainCheckout(MBase):
    """R9 (6) failing before the first merge: nothing merges, nothing is voided."""

    def setUp(self):
        super().setUp()
        self.three()
        self.passed("t-A", "t-B", "t-C")
        self.approve()

    def stops_at_a(self, word):
        refs = self.refs()
        code, out, err = self.merge()
        self.assertEqual(self.merged(), [], out + err)
        self.assertEqual(self.refs(), refs)
        self.assertIn("stopped at t-A", out)
        self.assertIn(word, out)
        self.assertTrue(all(not e["approval"].get("void") for e in self.entries()))
        self.assertEqual(self.remote(), "refs/heads/main " + self.m0)

    def test_m3_6a_dirty(self):
        write(os.path.join(self.proj, "README"), "dirty\n")
        self.stops_at_a("uncommitted")

    def test_m3_6b_off_main(self):
        sh(self.proj, "git", "checkout", "-q", "-b", "side")
        self.stops_at_a("not on main")

    def test_m3_6c_local_main_moved(self):
        self.on_main("README", "moved by hand\n")
        main = self.ref("main")
        self.stops_at_a("does not sit on")
        self.assertEqual(self.ref("main"), main)

    def test_m3_6d_risky_setting(self):
        sh(self.proj, "git", "config", "core.hooksPath", os.path.join(self.tmp, "hooks"))
        self.stops_at_a("core.hookspath")


class M4Prefix(MBase):
    def test_m4_merge_up_to_a(self):
        self.three()
        self.passed("t-A", "t-B", "t-C")
        self.approve("Merge up to t-A")
        code, out, err = self.merge()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.merged(), ["t-A"])
        self.assertEqual(self.ref("main"), self.tip("t-A"))
        self.assertIsNone(self.approval("t-B"))
        self.assertIsNone(self.approval("t-C"))
        self.assertEqual(self.entry("t-B")["onto"], "main")


class M7Void(MBase):
    def test_m7_a_commit_after_the_check_voids(self):
        self.three()
        self.passed("t-A", "t-B", "t-C")
        self.approve()
        self.change("t-A", "fa.txt", "later\n")
        code, out, err = self.merge()
        self.assertEqual(self.merged(), [], out + err)
        self.assertEqual(self.ref("main"), self.m0)
        self.assertIn("a commit after the check", self.approval("t-A")["void"])
        self.assertIn("open a new card", out)


# ---- M5: an approval is used once (AC9.2) -----------------------------------------------------------

class M5Once(MBase):
    def test_m5a_an_applied_card_edited_back_approves_nothing(self):
        self.three()
        self.passed("t-A")
        self.approve()
        code, out, err = self.merge()
        self.assertEqual(self.merged(), ["t-A"], out + err)
        path = os.path.join(self.dir, "decisions", "d-queue-1.json")
        c = pa.read_json(path)
        c["queue"].pop("applied")
        dump(c, path)
        self.set_entries(lambda r: [e.update(approval=None) for e in r["entries"]])
        self.passed("t-B", "t-C")
        code, out, err = self.merge()
        self.assertEqual(self.merged(), ["t-A"], out + err)
        self.assertIsNone(self.approval("t-B"))

    def test_m5b_a_task_that_rejoined_is_not_approved_by_the_old_card(self):
        self.joined("t-A", "fa.txt")
        self.assertEqual(self.make_card()[0], 0)
        code, out, err = self.queue("out", "t-A", "--repo", self.proj, "--why", "held")
        self.assertEqual(code, 0, out + err)
        self.add("t-A")
        self.passed("t-A")
        self.answer("d-queue-1", ALL)
        code, out, err = self.merge()
        self.assertEqual(self.merged(), [], out + err)
        self.assertIsNone(self.approval("t-A"))
        self.assertIn("t-A", out)

    def test_m5c_a_rejoin_in_the_same_second_is_not_approved_by_the_old_card(self):
        # AC9.2, R9 (1), D-J3b-10: a fixed clock puts the card, the out and the new add in one second (as a fast
        # machine does), so 'added' and the checked digests are all the same; the old card still binds only the
        # entry instance it listed.
        fixed = "2026-01-02T03:04:05Z"
        with mock.patch.object(pa, "now", return_value=fixed):
            self.joined("t-A", "fa.txt")
            self.assertEqual(self.make_card()[0], 0)
            code, out, err = self.queue("out", "t-A", "--repo", self.proj, "--why", "held")
            self.assertEqual(code, 0, out + err)
            self.add("t-A")
            self.passed("t-A")
            self.assertEqual(self.entry("t-A").get("added"), fixed)
            self.answer("d-queue-1", ALL)
            code, out, err = self.merge()
        self.assertEqual(self.merged(), [], out + err)
        self.assertIsNone(self.approval("t-A"))
        self.assertIn("not the entry the card asked about", out)


# ---- M6, M8: carry-over and refresh's merge (W4, Wanted 1) ------------------------------------------

class M6M8Refresh(MBase):
    def test_m6_the_entry_ahead_fails_the_approval_carries(self):
        self.three()
        self.approve()
        self.suite("t-A", 0)
        self.suite("t-B", 3)
        code, out, err = self.refresh()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.merged(), ["t-A"])
        self.assertEqual([e["task"] for e in self.entries()], ["t-C"])
        self.assertEqual(self.entry("t-C")["rebases"][-1]["result"], "clean")
        self.assertFalse(self.approval("t-C").get("void"))
        self.assertEqual(self.gate("t-C")[0], 0)
        self.suite("t-C", 0)
        code, out, err = self.refresh()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.merged(), ["t-A", "t-C"])
        self.assertEqual(self.ref("main"), self.tip("t-C"))
        self.assertEqual(self.queue_cards(), ["d-queue-1"])
        self.assertFalse(os.path.exists(os.path.join(self.proj, "fb.txt")))

    def test_m8_approved_while_checking_merged_by_refresh(self):
        self.joined("t-A", "fa.txt")
        self.approve()
        code, out, err = self.refresh()
        self.assertEqual(self.merged(), [], out + err)
        self.assertEqual(self.approval("t-A")["card"], "d-queue-1")
        self.suite("t-A", 0)
        code, out, err = self.refresh("--no-merge")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.merged(), [])
        self.assertEqual(self.entry("t-A")["state"], "passed")
        code, out, err = self.refresh()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.merged(), ["t-A"])
        self.assertEqual(self.ref("main"), self.tip("t-A"))


# ---- M9: one push card for several merges (Wanted 3, R20, J5) ---------------------------------------

class M9PushCard(MBase):
    def test_m9_the_open_push_card_is_replaced(self):
        self.allow()
        a = self.joined("t-A", "fa.txt")
        b = self.joined("t-B", "fb.txt")
        self.approve()
        self.suite("t-A", 0)
        code, out, err = self.refresh()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.merged(), ["t-A"])
        first = self.card("d-push-t-A-1")
        self.assertEqual(first["status"], "open", out + err)
        self.assertEqual([c["sha"] for c in first["push"]["commits"]], [a])
        self.suite("t-B", 0)
        code, out, err = self.refresh()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.merged(), ["t-A", "t-B"])
        old, new = self.card("d-push-t-A-1"), self.card("d-push-t-B-1")
        self.assertEqual((old["status"], old["replacedBy"]), ("withdrawn", "d-push-t-B-1"))
        self.assertEqual(new["status"], "open")
        self.assertEqual(new["push"]["replaces"], "d-push-t-A-1")
        self.assertEqual([(c["sha"], c.get("queueTask")) for c in new["push"]["commits"]], [(self.tip("t-B"), "t-B"), (a, "t-A")])
        self.assertTrue(set(c["sha"] for c in old["push"]["commits"]) <= set(c["sha"] for c in new["push"]["commits"]))
        self.assertIn("(from t-A)", new["context"])
        self.assertEqual(self.remote(), "refs/heads/main " + self.m0)
        self.assertEqual(self.ref("main"), b)

    def test_m9b_a_refused_plan_is_printed_and_the_merge_stands(self):
        self.allow()
        self.joined("t-A", "fa.txt")
        self.passed("t-A")
        self.approve()
        dump({"id": "d-push-t-Z-1", "task": "t-Z", "kind": "push", "status": "open",
              "push": {"version": 1, "space": SPACE, "task": "t-Z", "path": self.proj, "url": "file:///elsewhere", "branch": "main",
                       "commit": "f" * 40, "commits": [{"sha": "f" * 40}]}}, os.path.join(self.dir, "decisions", "d-push-t-Z-1.json"))
        code, out, err = self.merge()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.merged(), ["t-A"])
        self.assertIn("push card not opened", out)
        self.assertIn("d-push-t-Z-1", out)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "decisions", "d-push-t-A-1.json")))


# ---- M10: the merge is local, fast-forward only, hooks off (R11) -----------------------------------

class M10Safety(MBase):
    def test_m10_fast_forward_only_no_hook_no_remote(self):
        self.three()
        self.passed("t-A", "t-B", "t-C")
        self.approve()
        marker = os.path.join(self.tmp, "marker")
        for hook in ("pre-merge-commit", "post-merge", "post-checkout", "reference-transaction"):
            path = os.path.join(self.proj, ".git", "hooks", hook)
            write(path, "#!/bin/sh\necho {} >> {}\n".format(hook, marker))
            os.chmod(path, 0o755)
        write(os.path.join(TEST_ENV["HOME"], ".gitconfig"), "[merge]\n\tff = false\n[pull]\n\tff = false\n")
        remote = self.remote()
        code, out, err = self.merge()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.merged(), ["t-A", "t-B", "t-C"])
        self.assertEqual(self.ref("main"), self.tip("t-C"))
        self.assertEqual(sh(self.proj, "git", "rev-list", "--merges", "main"), "")
        self.assertFalse(os.path.exists(marker), "a hook ran")
        self.assertEqual(self.remote(), remote)
        self.assertEqual(sh(self.proj, "git", "status", "--porcelain", "--untracked-files=no"), "")

    def test_m10b_the_code_moves_refs_only_by_rebase_and_ff_merge(self):
        text = read(os.path.join(TOOLS, "pa_queue.py"))
        for word in ('"push"', '"fetch"', '"reset"', '"update-ref"', '"branch", "-f"', '"pull"', '"--force"'):
            self.assertNotIn(word, text, word)
        self.assertIn('"merge", "--ff-only"', text)


# ---- M11, M12: brief, and no rerun (Wanted 2, the standing words) -----------------------------------

class M11M12Brief(MBase):
    def test_m11_no_rebase_the_verifiers_suite_counts(self):
        self.task("t-A", [("fa.txt", "a\n")])
        self.suite("t-A", 0)  # the verifier's own run at HEAD
        self.add("t-A")
        code, out, err = self.refresh("--no-merge")
        self.assertEqual(self.entry("t-A")["state"], "passed", out + err)
        code, out, err = self.brief("t-A")
        self.assertEqual(code, 0, out + err)
        self.assertIn("nothing to run: its suite passed on this tree", out)
        self.assertNotIn("pa-full-suite", out)

    def test_m12_moved_clean_and_passed(self):
        self.task("t-A", [("a.txt", (30, "thirty"))])
        self.add("t-A")
        self.suite("t-A", 0)
        self.task("t-B", [("fb.txt", "b\n")])
        self.add("t-B")
        self.gate("t-B")
        self.task("t-C", [("a.txt", (100, "hundred"))])
        self.add("t-C")
        self.assertEqual(self.entry("t-C")["rebases"][-1]["result"], "moved")
        watch = (self.qfile(), os.path.join(self.dir, "content"), os.path.join(self.dir, "journal.jsonl"))
        before, refs = snapshot(*watch), self.refs()
        code, out, err = self.brief("t-C")
        self.assertEqual(code, 0, out + err)
        self.assertIn("carry-over: suite + short review", out)
        self.assertIn("run pa-full-suite", out)
        self.assertIn(self.trees["t-C"], out)
        self.assertIn(self.entry("t-C")["needs"]["shortReview"]["file"], out)
        self.assertIn("role: reviewer", out)
        self.assertIn("hc send", out)
        code, out, err = self.brief("t-B")
        self.assertIn("carry-over: suite\n", out)
        self.assertIn("run pa-full-suite", out)
        self.assertNotIn("short review", out)
        code, out, err = self.brief("t-A")
        self.assertIn("carry-over: none", out)
        self.assertIn("nothing to run: its suite passed on this tree", out)
        self.assertEqual(snapshot(*watch), before)
        self.assertEqual(self.refs(), refs)
        code, out, err = self.queue("show", "--repo", self.proj)
        self.assertIn("carry-over: none", out)
        self.assertIn("carry-over: suite\n", out)
        self.assertIn("carry-over: suite + short review", out)

    def test_m12b_show_lists_the_approval_and_the_open_cards(self):
        self.joined("t-A", "fa.txt")
        self.joined("t-B", "fb.txt")
        self.passed("t-A")
        self.approve("Merge up to t-A")
        self.refresh("--no-merge")
        self.assertEqual(self.make_card()[0], 0)
        code, out, err = self.queue("show", "--repo", self.proj)
        self.assertIn("approval: d-queue-1 (Merge up to t-A)", out)
        self.assertIn("approved and mergeable", out)
        self.assertIn("open queue cards: d-queue-2 (t-B)", out)


# ---- M13, M14: pa-decide, Not now, Other ---------------------------------------------------------

class M13M14Answers(MBase):
    def setUp(self):
        super().setUp()
        self.joined("t-A", "fa.txt")
        self.passed("t-A")
        self.assertEqual(self.make_card()[0], 0)

    def test_m13_pa_decide_merge_all(self):
        self.answer("d-queue-1", ALL)
        code, out, err = self.merge()
        self.assertEqual(self.merged(), ["t-A"], out + err)
        a = self.qrepo()["merged"][0]
        self.assertEqual(a["card"], "d-queue-1")
        self.assertEqual(len(self.journal("approve")), 1)

    def test_m13b_other_merges_nothing(self):
        self.answer("d-queue-1", "Other")
        code, out, err = self.merge()
        self.assertEqual(self.merged(), [], out + err)
        self.assertIsNone(self.approval("t-A"))
        self.assertNotIn("applied", self.card("d-queue-1")["queue"])

    def test_m14_not_now(self):
        self.answer("d-queue-1", "Not now")
        refs = self.refs()
        code, out, err = self.merge()
        self.assertEqual(self.merged(), [], out + err)
        self.assertEqual(self.refs(), refs)
        self.assertIsNone(self.approval("t-A"))
        self.assertEqual(self.card("d-queue-1")["queue"]["applied"]["approved"], [])
        self.assertEqual(self.journal("void"), [])

    def test_m14b_no_answer(self):
        refs = self.refs()
        code, out, err = self.merge()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.merged(), [])
        self.assertEqual(self.refs(), refs)
        self.assertIn("nothing approved", out)


# ---- M15, M16: callers, the locks, a stable queue top ---------------------------------------------

class M15M16Callers(MBase):
    def setUp(self):
        super().setUp()
        self.three()
        self.passed("t-A", "t-B", "t-C")
        self.approve()

    def test_m15_worker_shells_are_refused(self):
        before, refs = snapshot(self.qfile()), self.refs()
        os.chdir(self.trees["t-A"])
        dump({"task": "t-A", "space": SPACE}, os.path.join(self.trees["t-A"], ".claude", "pa-task.json"))
        code, out, err = self.merge()
        os.chdir(self.tmp)
        self.assertEqual(code, 1, out + err)
        self.assertIn("worker", err)
        os.environ["HC_TERMINAL_ID"] = "t-Ab"
        try:
            code, out, err = self.merge()
            code2, out2, err2 = self.make_card()
        finally:
            os.environ.pop("HC_TERMINAL_ID", None)
        self.assertEqual((code, code2), (1, 1), out + err + out2 + err2)
        self.assertIn("worker", err)
        self.assertEqual(snapshot(self.qfile()), before)
        self.assertEqual(self.refs(), refs)

    def test_m16_an_add_while_merging_waits_and_goes_last(self):
        pa_queue.LOCK_WAIT = 0.3
        self.task("t-D", [("fd.txt", "d\n")])
        real, seen = pa_queue.ff_merge, []

        def during(ctx, rec, e, tip):
            code, out, err = self.queue("add", "t-D", "--repo", self.proj)
            seen.append((code, err))
            with open(os.path.join(self.dir, ".push.lock"), "a") as f:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    seen.append("push lock free")
                    fcntl.flock(f, fcntl.LOCK_UN)
                except OSError:
                    seen.append("push lock held")
            return real(ctx, rec, e, tip)
        pa_queue.ff_merge = during
        try:
            code, out, err = self.merge()
        finally:
            pa_queue.ff_merge = real
        self.assertEqual(self.merged(), ["t-A", "t-B", "t-C"], out + err)
        self.assertEqual(seen[0][0], 1)
        self.assertIn("another pa-queue", seen[0][1])
        self.assertEqual(seen[1], "push lock held")
        self.add("t-D")
        self.assertEqual([e["task"] for e in self.entries()], ["t-D"])

    def test_m16b_the_lock_held_refuses_the_merge(self):
        pa_queue.LOCK_WAIT = 0.3
        refs = self.refs()
        with open(os.path.join(self.dir, ".queue.lock"), "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            code, out, err = self.merge()
        self.assertEqual(code, 1, out + err)
        self.assertIn("another pa-queue", err)
        self.assertEqual(self.refs(), refs)


# ---- RL-J3c-2: an approval counts only on a passed entry whose gate passes on every pin -------------

class RLJ3c2EveryPin(MBase):
    def test_rl_j3c_2_another_pins_content_fails(self):
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        sh(other, "git", "init", "-q", "-b", "main")
        write(os.path.join(other, "o.txt"), "o\n")
        sh(other, "git", "add", "o.txt")
        sh(other, "git", "commit", "-q", "-m", "o")
        wt = os.path.join(self.tmp, "wt-multi")
        sh(self.proj, "git", "worktree", "add", "-q", "-b", "t-A-x", os.path.join(wt, "src", "proj"), "main")
        sh(other, "git", "worktree", "add", "-q", "-b", "t-A-o", os.path.join(wt, "src", "other"), "main")
        self.trees["t-A"] = wt
        for sub, f in (("proj", "fa.txt"), ("other", "fo.txt")):
            write(os.path.join(wt, "src", sub, f), "a\n")
            sh(os.path.join(wt, "src", sub), "git", "add", f)
            sh(os.path.join(wt, "src", sub), "git", "commit", "-q", "-m", "t-A " + f)
        led = self.ledger()
        led["tasks"].append({"id": "t-A", "title": "T", "state": "in-progress", "tier": "normal", "type": "feature", "worktree": wt,
                             "workers": [{"address": "t-Ab", "role": "build", "started": AFTER, "finished": True},
                                         {"address": "t-Ar", "role": "reviewer", "started": AFTER, "finished": True},
                                         {"address": "t-Av", "role": "verifier", "started": AFTER, "finished": True}]})
        self.set_ledger(led)
        for name in REVIEW_FILES:
            write(os.path.join(self.evidence("t-A"), name), "text\n")
        tips = [sh(os.path.join(wt, "src", s), "git", "rev-parse", "HEAD") for s in ("proj", "other")]
        write(os.path.join(self.evidence("t-A"), "VERIFY.md"), "text\n" + "".join("Checked: {}\n".format(t) for t in tips) + "Verdict: pass\n")
        self.assertEqual(self.gate("t-A")[0], 0)
        self.add("t-A")
        self.suite("t-A", 0)
        self.approve()
        code, out, err = self.refresh("--no-merge")
        self.assertEqual(self.approval("t-A")["card"], "d-queue-1", out + err)
        # the other repository's change moves after the check: the gate fails on that pin only
        write(os.path.join(wt, "src", "other", "fo.txt"), "changed\n")
        sh(os.path.join(wt, "src", "other"), "git", "commit", "-q", "-am", "after the check")
        code, out, err = self.merge()
        self.assertEqual(self.merged(), [], out + err)
        self.assertEqual(self.ref("main"), self.m0)
        self.assertIn("stopped at t-A", out)
        self.assertIn("gate", out)
        self.assertFalse(self.approval("t-A").get("void"))

    def test_rl_j3c_2_a_restored_approval_before_the_suite_never_merges(self):
        self.joined("t-A", "fa.txt")
        self.set_entries(lambda r: r["entries"][0].update(approval={
            "card": "d-queue-9", "answeredAt": pa.now(), "answeredBy": "person", "option": ALL,
            "change": r["entries"][0]["checked"]["change"], "sequence": r["entries"][0]["checked"]["sequence"],
            "added": r["entries"][0]["added"], "carried": "doc-only, short review SHORT-REVIEW-t-A-1.md"}))
        code, out, err = self.merge()
        self.assertEqual(self.merged(), [], out + err)
        self.assertIn("not passed", out)
        self.suite("t-A", 0)
        code, out, err = self.merge()
        self.assertEqual(self.merged(), ["t-A"], out + err)


# ---- r-disjoint-carry (d-202): a clean disjoint rebase keeps the earlier suite pass ----------------

class Carry(MBase):
    """A on main (fa.txt); B (fb.txt) checked from main with its suite passed there, then add rebases it onto A."""

    TIER = "normal"
    B_FILE = "fb.txt"
    B_SUITE = True

    def setUp(self):
        super().setUp()
        self.joined("t-A", "fa.txt")
        self.suite("t-A", 0)
        self.task("t-B", [(self.B_FILE, "b\n")])
        if self.TIER != "normal":
            led = self.ledger()
            next(t for t in led["tasks"] if t["id"] == "t-B")["tier"] = self.TIER
            self.set_ledger(led)
        if self.B_SUITE:
            self.suite("t-B", 0)  # the verifier's run on the checked tree, before the queue moved it
        self.old_tree = sh(self.trees["t-B"], "git", "rev-parse", "HEAD^{tree}")
        code, self.add_out, err = self.add("t-B")
        self.assertEqual(self.gate("t-B")[0], 0)

    def independence(self, answer="yes", verdict="pass"):
        reviewer = "t-Bi"
        self.add_worker("t-B", reviewer, "reviewer")
        need = self.entry("t-B")["needs"]["independence"]
        write(os.path.join(self.evidence("t-B"), need["file"]), review_file(self.tip("t-B"), reviewer, verdict, answer))
        return need["file"]


class CarryYes(Carry):
    def test_carry_the_case(self):
        self.assertEqual(self.entry("t-B")["rebases"][-1]["result"], "clean")
        need = self.entry("t-B")["needs"]["independence"]
        self.assertIn("independence", self.add_out)
        self.assertIn(need["file"], self.add_out)
        code, out, err = self.brief("t-B")
        self.assertEqual(code, 0, out + err)
        self.assertIn("carry-over: independence review", out)
        self.assertIn("no suite to run", out)
        self.assertNotIn("run pa-full-suite", out)
        self.assertIn(need["file"], out)
        self.assertIn("Independent: yes", out)
        code, out, err = self.refresh("--no-merge")
        self.assertEqual(self.entry("t-B")["state"], "checking", out + err)
        self.independence()
        code, out, err = self.refresh("--no-merge")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.entry("t-B")["state"], "passed", out + err)
        r = [l for l in read(os.path.join(self.dir, "content", "reviews.jsonl")).splitlines() if '"independent"' in l]
        self.assertEqual(len(r), 1)
        self.assertTrue(json.loads(r[0])["independent"])
        code, out, err = self.brief("t-B")
        self.assertIn("nothing to run", out)
        self.assertIn("carried", out)
        self.approve()
        code, out, err = self.merge()
        self.assertEqual(self.merged(), ["t-A", "t-B"], out + err)
        m = self.qrepo()["merged"][1]
        self.assertEqual(m["suite"]["tree"], self.old_tree)
        self.assertTrue(m["suite"]["carried"])

    def test_carry_independent_no_runs_the_suite(self):
        self.independence("no")
        code, out, err = self.refresh("--no-merge")
        self.assertEqual(self.entry("t-B")["state"], "checking", out + err)
        code, out, err = self.brief("t-B")
        self.assertIn("carry-over: suite", out)
        self.assertIn("run pa-full-suite", out)

    def test_carry_not_without_the_review(self):
        self.approve()
        code, out, err = self.merge()
        self.assertEqual(self.merged(), ["t-A"], out + err)
        self.assertIn("not passed", out)

    def test_carry_not_when_a_file_of_the_change_moved_under_it(self):
        # clean already implies it (the record holds each file's base blob); the rule's own test is checked alone
        real = pa_queue.touched
        pa_queue.touched = lambda ctx, a, b: real(ctx, a, b) | {"fb.txt"}
        try:
            self.independence()
            code, out, err = self.refresh("--no-merge")
            self.assertEqual(self.entry("t-B")["state"], "checking", out + err)
            code, out, err = self.brief("t-B")
            self.assertIn("run pa-full-suite", out)
        finally:
            pa_queue.touched = real


class CarryHighTier(Carry):
    TIER = "high"

    def test_carry_never_for_high_tier(self):
        self.assertNotIn("independence", self.entry("t-B").get("needs") or {})
        self.assertNotIn("independence", self.add_out)
        code, out, err = self.brief("t-B")
        self.assertIn("carry-over: suite", out)
        self.assertIn("run pa-full-suite", out)


class CarryNoEarlierSuite(Carry):
    B_SUITE = False

    def test_carry_needs_an_earlier_pass(self):
        self.assertNotIn("independence", self.entry("t-B").get("needs") or {})
        code, out, err = self.brief("t-B")
        self.assertIn("run pa-full-suite", out)


class CarryNotClean(Carry):
    def setUp(self):
        # A changes a.txt near B's line: B's rebase is moved (a shared file), never clean
        super(Carry, self).setUp()
        self.task("t-A", [("a.txt", (20, "twenty"))])
        self.add("t-A")
        self.gate("t-A")
        self.task("t-B", [("a.txt", (5, "five B"))])
        self.suite("t-B", 0)
        code, self.add_out, err = self.add("t-B")

    def test_carry_not_after_a_moved_rebase(self):
        self.assertEqual(self.entry("t-B")["rebases"][-1]["result"], "moved")
        self.assertNotIn("independence", self.entry("t-B").get("needs") or {})
        code, out, err = self.brief("t-B")
        self.assertIn("carry-over: suite + short review", out)


if __name__ == "__main__":
    unittest.main()
