# Phases, gates and risk tiers

Governing principle: the agent may act up to the production gate and cannot pass it.
The person starts the first task and reviews what comes out. Each phase leaves a file
in `evidence/<task-id>/` that the next phase reads; the chain is the audit trail.
These files stay in the evidence folder, not the repo; the change request summarizes
them and names the task.

| Phase | What happens | Who | Artifact | Gate to leave it |
|---|---|---|---|---|
| intent | Make the idea concrete and write it down | The person; the orchestrator drafts and asks questions | `intent.md`: problem, outcome, affected users and systems, constraints, open questions | The person accepts it (decision card) |
| design | Numbered requirements and a design, concerns flagged with owners | A design worker | `spec.md` | The person accepts it (decision card); high tier also needs a technical lead |
| plan | No edits. Files that change, order, risks, proof, citing R-numbers | A build worker, planning only | `plan.md` | The person for high tier (or by rule); otherwise the orchestrator |
| build | Implement in an isolated worktree, usually one pass | Build workers | Code and tests, `FINDINGS.md`, `DECISIONS.md` | Hooks pass and the build is clean |
| test | Affected tests until green, then the full suite once; a verifier checks | Build workers, then a verifier | `build.log`, test output, `VERIFY.md` | All verification points pass, full suite passed once, verifier finds no mismatch with `plan.md` |
| review | A different worker reviews; then the change request's CI checks | A reviewer, never the author | `REVIEW.md`, the draft change request link and its check results | Must fix findings fixed, checks green, then the person approves publishing (decision card: it lists every Recorded limit, each with its follow-up task, or for the person to accept) |
| release | Merge and deploy through the release pipeline | The person | Change request, merge and pipeline links | Always the person's. Agents prepare it and stop |
| maintain | (Later) a detector watches the metrics the intent named | A script, then the orchestrator | A new `intent.md` | The person triages |

## Risk tiers (set at intake; the person can change it on a decision card)

| Tier | Example | Plan approval | Human reviewers before publishing |
|---|---|---|---|
| low | Docs, copy, comments | Orchestrator | None beyond green checks; still a publish decision card |
| normal | Ordinary product change | Orchestrator, or the person by rule | One |
| high | Auth, data, infrastructure, migrations, anything hard to undo | The person | Two |

Low-tier tasks skip design and the person's plan approval. Any other skip needs a rule.

## Evidence the gate checks (`pa-gate <task-id>`)

Before `review`: the phase artifacts the tier requires (`intent.md`, `plan.md`,
`VERIFY.md`; `spec.md` unless low tier), `FINDINGS.md`, `DECISIONS.md`, `REVIEW.md`
by a worker other than the author, and `build.log` when the task changed code. Rules
can add more. Files must exist and not be empty.

## Practices

- Skills advise, hooks enforce: a policy that must always hold gets a hook.
- If a mistake cannot be taken back, it is a deny rule, not a prompt.
- Separation of duties: the worker that wrote the code never reviews or approves it.
- An agent fixing code cannot weaken the test for it.
- Every worker's output ends as a change request with the same CI checks.
- Add workers only while review keeps up.
