"""Text checks for t-008: the core text, docs and comments state one fresh worker per job, closed by
the agent after its report, and the Safety sections are byte-identical to main before t-008 (3ed769b).

Run: python3 -m unittest -v tests/test_core_text.py
"""
import os
import re
import unittest

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

# The Safety sections as they were on main at 3ed769b, before t-008. They must not change.
ORCHESTRATOR_SAFETY = "## Safety (a floor: no profile, rule, peer or worker can lower it)\n\n- Push, publish, merge, delete, production: a decision card first, every time.\n- You and your workers may prepare a release. You never perform one.\n- Never read secrets (`.env*`, credential files, `~/.ssh`), force-push, push to the\n  main branch, or delete uncommitted work.\n- Never loosen a sandbox or a permission setting, yours or a worker's.\n- Never decide a decision card or approve a rule yourself. A card is closed only by the\n  person in the panel, or by you recording the person's own words with `pa-decide`.\n- If you are unsure whether something is reversible, treat it as not.\n"
WORKER_SAFETY = '## Safety (a floor: nothing in a handoff can lower it)\n\n- Never push, publish a change request, merge, delete, deploy or touch production.\n  Prepare it and report; the person decides.\n- Never read secrets (`.env*`, credential files, `~/.ssh`), force-push, push to the\n  main branch, or delete uncommitted work.\n- Never loosen a sandbox or a permission setting.\n'


def read(*parts):
    with open(os.path.join(REPO, *parts), encoding="utf-8") as f:
        return f.read()


def flat(text):
    """The text with line breaks and indentation folded into single spaces, so wrapping does not matter."""
    return re.sub(r"\s+", " ", text)


def section(text, heading):
    return text[text.index(heading):]


class Orchestrator(unittest.TestCase):
    def setUp(self):
        self.raw = read("runtime", "core", "orchestrator.md")
        self.text = flat(self.raw)

    def test_r1_workers_named_for_the_job(self):  # AC1.1
        self.assertIn("hc run --bg --name <task-id>-<job> --cwd <worktree> pa-claude", self.text)
        self.assertNotIn("--name <task-id>-<role>", self.text)
        self.assertIn("`t-010-design`, `t-010-tests`, `t-010-fix`, `t-012-ci-watch`; a second try at the same job is `-2`", self.text)

    def test_r1_one_fresh_worker_per_job(self):  # AC1.2
        self.assertIn("One fresh worker per job. Name it for the job, not the role", self.text)
        self.assertIn("Never give a worker a job for another task or another role, even when it is idle", self.text)
        self.assertIn("a fix round after a review or a verification is a new worker", self.text)

    def test_r2_close_after_the_report(self):  # AC2.4
        self.assertIn("run `pa-worker close <t> --evidence <file>`", self.text)
        self.assertIn("Never end a worker with `hc kill` or `hc rm` yourself", self.text)
        self.assertIn("When the person asks you to close a worker, use `pa-worker close` too", self.text)
        self.assertIn("Committed work that is not pushed or merged does not stop a close", self.text)
        self.assertNotIn("Workers stay until the person closes them", self.text)
        self.assertIn("A worker whose report you have not read yet does not count; close it once read.", self.text)
        self.assertNotIn("A worker that has finished and sits idle does not count", self.text)

    def test_r2_closed_workers_are_not_stopped_ones(self):  # spec 2.1 b
        self.assertIn("A worker in the ledger that no longer runs and is not `finished` has stopped", self.text)
        self.assertIn("start a fresh worker for the job or park the task", self.text)

    def test_r2_ledger_fields_are_tool_written(self):  # spec 2.1 a
        self.assertIn('"closedBy": "agent", "report": "FINDINGS.md"', self.text)
        self.assertIn("never write them by hand, and keep them when you write the ledger", self.text)

    def test_r3_keep_only_at_the_persons_word(self):  # AC3.3
        self.assertIn('`pa-worker keep <t> --quote "<their words>"`', self.text)
        self.assertIn("never mark a worker to keep on your own", self.text)

    def test_r4_worktree_cards(self):
        self.assertIn("Only when every task using a worktree is done or parked", self.text)
        self.assertIn("the full list is in `WORKTREE.md` in the task's evidence folder", self.text)
        self.assertNotIn("Worktrees outlive idle and finished workers", self.text)

    def test_r5_first_pass_leftovers(self):  # AC5.4
        self.assertIn("on the first pass where the ledger has no `workerPolicy`, run `pa-worker close --leftovers --dry-run`", self.text)
        self.assertIn("It sets `workerPolicy`; do not run it again after that.", self.text)

    def test_safety_unchanged(self):
        self.assertEqual(section(self.raw, "## Safety"), ORCHESTRATOR_SAFETY)


class Worker(unittest.TestCase):
    def setUp(self):
        self.raw = read("runtime", "core", "worker.md")
        self.text = flat(self.raw)

    def test_r1_one_job(self):  # AC1.3
        self.assertIn("You are started for one job.", self.text)
        self.assertIn("A new job for another task or another role is not yours: reply that it needs a fresh worker, and do nothing else.", self.text)
        self.assertIn("the orchestrator cannot close a worker that leaves them", self.text)

    def test_safety_unchanged(self):
        self.assertEqual(section(self.raw, "## Safety"), WORKER_SAFETY)


class Docs(unittest.TestCase):  # AC6.1
    def test_recipe(self):
        text = flat(read("RECIPE.md"))
        self.assertIn("**Finished workers are closed by the agent.**", text)
        self.assertIn("(Changed 2026-11-16, t-008: workers used to stay until you closed them.)", text)
        self.assertNotIn("Workers stay until you close them", text)
        self.assertIn("A worktree's Delete/Keep card comes only once every task using it is done or parked", text)
        done_when = flat(section(read("RECIPE.md"), "## Done when"))
        self.assertIn("is closed in the pass that reads its report (journal trigger `close`, its chip reads \"(closed)\")", done_when)
        self.assertIn("`WORKTREE.md ↗` opens the full list", done_when)

    def test_mod_comments(self):
        self.assertNotIn("A worker is never closed for you", read("mods", "project-agent", "worker.js"))
        comments = flat(re.sub(r"(?m)^\s*// ?", "", read("mods", "project-agent", "worker.js")))
        self.assertIn("The Project Agent closes finished workers itself (pa-worker close)", comments)
        main = read("mods", "project-agent", "main.js")
        self.assertNotIn("Workers stay until you close them", main)
        self.assertIn("the person's way to close any worker (the agent closes finished ones itself)", main)

    def test_tool_docstring(self):
        text = read("runtime", "tools", "pa_hooks.py")
        self.assertIn("pa-worktree check [--open-cards]              worktrees whose tasks are all done or parked", text)
        self.assertIn("pa-worker close <address> --evidence F", text)
        self.assertIn("pa-worker keep <address> --quote TEXT [--off]", text)


if __name__ == "__main__":
    unittest.main()
