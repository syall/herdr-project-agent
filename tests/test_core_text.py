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
        # review round 1, M7: the old sentence could be read as "working workers do not count"
        self.assertIn("A worker that has reported, but whose report you have not read yet, does not count; close it once read.", self.text)
        self.assertNotIn("A worker whose report you have not read yet does not count", self.text)
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

    def test_r5_first_pass_leftovers(self):  # AC5.4, as changed by the person on card d-031
        self.assertIn("on the first pass where the ledger has no `workerPolicy`, run `pa-worker close --leftovers`. It closes nothing", self.text)
        self.assertIn("puts that list on one decision card (`d-leftovers`", self.text)
        self.assertIn("When it is answered Close all listed, run `pa-worker close --card d-leftovers`", self.text)
        self.assertIn("Answered Close none: leave them", self.text)
        # review round 3: R1 (one card at a time, the rest on the next) and R2 (any note closes nothing)
        self.assertIn("If the person wrote a note, nothing is closed", self.text)
        self.assertIn("then run `pa-worker close --leftovers` again: it opens the next card", self.text)
        self.assertNotIn("may name workers to keep in its note", self.text)
        self.assertNotIn("journal its list, then run `pa-worker close --leftovers` in the same pass", self.text)

    def test_close_is_not_a_delete(self):  # review round 1, M8 (in the Workers section, not Safety)
        workers = flat(self.raw[self.raw.index("## Workers"):self.raw.index("## Phases and gates")])
        self.assertIn("Closing a reported worker with `pa-worker close` ends its terminal only and deletes no work: "
                      "its evidence, worktree, branch and task stay. That is why it needs no decision card; "
                      "anything that deletes work still needs one.", workers)

    def test_workers_record_their_start(self):  # review round 1, I4
        self.assertIn('"started": "<UTC time>"', self.text)
        self.assertIn("record the worker in the ledger with its `started` time", self.text)
        self.assertIn("a file that worker wrote after it started", self.text)

    # t-017 J1 (R1, R5, R8): finding labels, must-fix bar, stop rule, scope, gate before the check
    def test_t017_r1_stop_rule(self):  # AC1.3, AC1.5
        self.assertIn("- Rounds: a round is one build or fix worker and the check that follows it. A fix round "
                      "fixes only Must fix findings; Recorded limits and Nits go on the merge card. Stop rule: "
                      "when a task reaches its round cap (low 2, normal 3, high 4) or two rechecks in a row "
                      "find Must fix in the same area, start no further round: open a decision card (cut that "
                      "area into a new task, record it as a limit, or one more round).", self.text)

    def test_t017_r1_handoff_states_bar(self):  # AC1.4
        self.assertIn("the phase and its artifact (see `sdlc.md`), the verification points, the must-fix bar "
                      "and the stop rule (see Rounds), the evidence folder", self.text)

    def test_t017_r1_merge_card_limits(self):  # AC1.3, G6
        cards = flat(self.raw[self.raw.index("## Decision cards"):self.raw.index("## Workers")])
        self.assertIn('A merge card lists every Recorded limit, each with its follow-up task or "accepted".', cards)

    def test_t017_r5_scope(self):  # AC5.1-5.3
        self.assertIn("- Scope: the handoff lists the requirements the job builds. A requirement that appears "
                      "after the build starts, from the person, from a finding outside the must-fix bar, or "
                      "from you, becomes a new task. When the person asks for one mid-task, open a card with "
                      '"New task" recommended and "Add to this task" as the other option.', self.text)

    def test_t017_r8_gate_before_check(self):  # AC8.1
        gates = flat(section(self.raw, "## Phases and gates"))
        self.assertIn("Before a task leaves a phase, run `pa-gate <task-id> --phase <phase>`. When a build or "
                      "fix worker reports, run `pa-gate <task-id> --phase plan` and `--phase build` before "
                      "starting its check; a missing artifact goes back to that worker as a follow-up for its "
                      "own job.", gates)

    def test_safety_unchanged(self):
        self.assertEqual(section(self.raw, "## Safety"), ORCHESTRATOR_SAFETY)


class Worker(unittest.TestCase):
    def setUp(self):
        self.raw = read("runtime", "core", "worker.md")
        self.text = flat(self.raw)

    def test_r1_one_job(self):  # AC1.3
        self.assertIn("You are started for one job.", self.text)
        self.assertIn("A new job for another task or another role is not yours: reply that it needs a fresh worker, and do nothing else.", self.text)
        self.assertIn("the orchestrator cannot close a worker that leaves them; it decides what to do with them", self.text)

    # t-017 J1 (R1, R9)
    def test_t017_r1_finding_labels(self):  # AC1.1, AC1.2
        self.assertIn("- Verifiers, reviewers and check workers label every finding **Must fix** (it breaks a "
                      "requirement, the plan, a verification point, safety or security; say why it blocks the "
                      "merge), **Recorded limit** (real, but outside the handoff's must-fix bar; name a "
                      "follow-up) or **Nit**, and end the file with `Verdict: pass` or `Verdict: must fix (<n>)`.",
                      self.text)
        self.assertIn("write `VERIFY.md`, and report. Never fix anything. On a recheck the handoff names a base "
                      "commit: check the diff from it to HEAD.", self.text)
        self.assertIn("compliance with `spec.md` and `plan.md`, and write `REVIEW.md`. On a diff recheck, review "
                      "the diff from the base commit the handoff names.", self.text)
        self.assertNotIn("Important or Nit", self.text)

    def test_t017_r9_no_background_shell(self):  # AC9.1, G1: in Rules of the job, not Safety
        rules = flat(self.raw[self.raw.index("## Rules of the job"):self.raw.index("## Roles")])
        self.assertIn("- Before you report, stop every background command you started (a watcher, a server, a "
                      "background shell): your session must go idle, or it cannot be closed.", rules)

    def test_safety_unchanged(self):
        self.assertEqual(section(self.raw, "## Safety"), WORKER_SAFETY)


class Sdlc(unittest.TestCase):  # t-017 J1 (R1)
    def test_t017_r1_gate_cell(self):
        text = flat(read("runtime", "core", "sdlc.md"))
        self.assertIn("| Must fix findings fixed, recorded limits listed on the merge card, checks green, then the "
                      "person approves publishing (decision card) |", text)
        self.assertNotIn("Important findings", text)


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
