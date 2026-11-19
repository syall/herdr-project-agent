# Project Agent

A per-space orchestrator agent for Herdr, and the panel you run it from. The agent plans work,
hands it to worker agents and asks you only for decisions; the panel shows its decision cards,
tasks, rules and pass schedule, read from plain files the agent keeps on disk.

## What you see

A right-panel view on the theme's `chrome` background, text 11 to 13 px, top to bottom:

1. A 34 px header: `PA: <name>`, the agent's status dot and word on the right.
2. Two dim lines: the project folder, then `profile <name> · <n> active of <m> tasks · pass <k>`.
3. The schedule bar: a one-line summary (`Passes every 5 min · next 14:32`, or paused, off,
   `no active tasks`, `quiet hours until 08:00`), then `Pause passes`, `Run pass now` and
   `Run retrospective now`. Clicking the summary opens cadence presets (5, 10, 15, 30, 60), a
   custom number, Off, quiet hours and "Only while tasks are active". Quiet hours' Set takes an
   empty field as the time it shows (19:00 to 08:00); a typed time is 24-hour H:MM or HH:MM, and
   one that is not gets a message naming the field and what was typed.
4. Four view pills: `Decisions · n`, `Tasks · n`, `Rules · n`, `Costs` (with the space's estimated
   total once read, `Costs · 12.34 est.`).
5. The chosen view, scrolling: cards with a 1 px border and 7 px corners on `surface`.

A new space opens with a 600 x 440 dialog: a Project Agent card (`Set up for <folder>`,
`Another folder…`) and three tiles (Claude, Codex, Terminal).

## How it behaves

- **New space:** the dialog's primary button turns the space's first tab into the Project Agent
  tab. "Another folder…" swaps the page for a path field with folder completion (Tab or a click
  completes, Enter confirms; "Browse…" on this computer). A tile opens that session instead.
  Keys: Enter, 1 to 5, Esc. The tab underneath accepts the same choice typed.
- **The agent's tab:** kept first in its space. If its launcher is gone (an idle shell is left at its
  prompt, or its terminal finished; anything else running there, an agent included, is left alone
  with a notice), it is restored in the same tab within about 45 s and resumed with the
  same conversation; the tab stays protected throughout. A tab that vanished is reopened in a new
  tab. A launcher that cannot start shows why and waits for Enter. It ends when its space is closed.
  When no launcher runs (the header says "Not running"), **Open Project Agent** in the header opens
  it again in a new tab, resumed (on a remote space too); it shows "Opening…" until the tab is
  recorded and says why if nothing started after 30 s. While the keeper restores or reopens the
  tab itself the header says "Restoring…" or "Reopening…" instead; once it has given up (3
  failures) it says "Not running" and offers Open again. If an agent that looks like this one runs
  by hand in the space, it asks first.
  After updating this repo, run `pa-sync --on <machine>` before installing the mod: an older
  `pa-session` on that machine refuses to replace the dead shell, so restores there fail (the keeper
  gives up on its new pane after 2 minutes, retries, and after 3 tries says "Project Agent needs
  you") until it is synced.
- **Decisions:** open cards first, newest first. Clicking an option selects it; `Submit answer`
  sends it with the note. The answer is written into the card file and the agent is told with
  `hc send`, in the terminal its launcher runs in at that moment; a failed send is retried every
  10 s and the panel says so. With no Project Agent running, nothing is sent: the panel says
  "Answer saved. The Project Agent is not running." and offers Open; the notice goes out once it
  runs. The card's link opens the evidence file it is about. Answered cards fold away below.
  A worktree's Delete/Keep card comes only once every task using it is done or parked, and lists
  its tasks, branch, commits (merged or pushed), files changed and uncommitted files;
  WORKTREE.md in the task's evidence has the full list.
- **Tasks:** one section per state (finished ones start folded), newest first. An expanded task
  shows its goal, tier, a chip per worker (a live one opens in the Agents view), a button per
  open decision, a button per artifact link, Evidence (only when there is some), the profile's
  declared actions, and your own controls: Reopen, Mark done, Park, Delete… (two clicks on the
  same button; the task, its evidence and its cards go to a trash folder). After the first click
  it asks "Delete the task, its evidence and its cards?" with a Cancel beside it; Cancel, a click
  anywhere else in the panel, collapsing the task, another view or space, or 10 s without a
  second click put it back to Delete….
- **Places:** above the state sections, a folding **Places** section headed `Places · 3 of 4
  working · 2 waiting`: each kind of place with its limit and holders (`Working · 3 of 4`,
  `No-test extra · 0 of 1`, `Suite step · 1 of 2`, one line per worker: address, task, job, role,
  since), the limits' rule, then `Waiting · n`, one line per job in the order they will start
  (position, task, job, role, kind, why it waits: `blocked: …`, `waits for a suite place (2 of 2)`,
  `next to start`) with ↑ ↓ ⤒ to move it. A merge-queue check shows `merge queue order (Queue tab)`
  and no buttons. A move writes your order and tells the agent; it only says which ready job goes
  first. A task row shows a badge per waiting or running job (`next #2 · suite step`, `running ·
  working`); a click unfolds Places and marks that line.
- **Finished workers are closed by the agent.** The agent starts one fresh worker per job, named
  for the job (`t-010-tests`, `t-010-fix`), and closes it in the pass where it reads its report,
  with `pa-worker close`. That command refuses a worker that is working or blocked, has no
  recorded evidence, has uncommitted files in its worktree, or that you marked to keep. To keep
  one open, tell the agent "keep t56". A closed worker leaves the task card (its entry stays in
  the ledger); a task whose workers are all closed shows no workers row. The × on a worker (in a task, or in an Agents view that opens this dialog) is still
  yours: it closes any worker and asks what to delete with it: its terminal, its worktree when
  nobody else uses it (not ticked when it holds unsaved work), and optionally its task.
  (Changed 2026-11-16, t-008: workers used to stay until you closed them.)
- **Rules:** proposals first (Approve, Edit then approve, Decline), then your rules in precedence
  order with an On/Off pill, Edit and a two-click Delete ("Delete for good?" with a Cancel beside it; it
  goes back to Delete the same ways a task's Delete does); drag a rule by its grip onto another
  to place it above. Add, Export, Import. An invalid `rules.json` is shown as an error and never
  written over.
- **Passes:** every 15 s one copy of the mod checks each live agent's `schedule.json` and sends
  `scheduled pass` when due, never while the agent is `working` or `blocked`. On-demand passes
  queue the same way.
- **No agent in the space:** a short explanation and, outside the home folder, a start button.
- **Costs:** the header names the price table (`Estimated from <source> prices as of <date>
  (<currency>) · updated 2 min ago · Refresh`), then tiles Total, Today, Last 7 days, then By
  activity, By task, By session type (kind, role, program, model, machine), By day; a row opens its
  drill-down (one per section). Every money figure is an estimate and says `est.`; one with no price
  says `no price`, a partial one `+ unpriced`. Billed amounts, when there are any, sit below,
  labelled `billed · account-wide`. The timer collects every 120 s while a Costs view shows the
  space, every 30 min otherwise; the view reads `costs/summary.json` only while it is shown.
  With no records: `Reconstruct from session files`. The billing query's line has `Turn off`.

## Pushing main from the panel

Off by default: with no `~/.config/herdr-pa/push-allow.json`, or `"enabled": false` in it, no
push card can be made and nothing about pushing changes. Agents never push a main branch.

- **Your allowlist.** `push-allow.json` lists the repositories you allow (main checkout path, exact
  remote URL, branch, space, on/off, an optional `prePush` check program). You write it by hand or
  with `pa-push-allow add|remove|on|off` in your own terminal; `pa-push-allow show` prints it and
  whether it is valid. README has the shape. No install or sync step writes it.
- **The push card.** After a task for a listed repository passed its gate and you approved the
  card that asks to publish or merge it (the one that lists every Recorded limit), the agent may
  open a push card (`pa-push-main plan <task-id> --repo <main checkout>`), options **Push** and
  **Not now**. When the card shows, the panel runs `pa-push-main check` and draws its facts: repository,
  URL "on your allowlist", remote and local heads, the commits that would go out ("not from this
  task" marked), the gated commit, the exact commands, and whether this panel may push. Push is
  disabled while the last check failed or is older than 120 s; "Check again" refreshes it.
- **Submit with Push** runs `pa-push-main run` once, on the space's machine. It refuses agent
  processes, re-checks everything under a lock (allowlist unchanged since the card, gate and its
  commit pin, local and remote main not moved, a fast-forward, the facts you saw), runs
  `prePush` in a fresh temporary clone, fast-forwards local main if needed and pushes exactly the
  gated commit to the allowlist's URL, hooks off. It never forces, and a second run pushes nothing.
  The card shows the result or the refusal, and the agent is told. **Not now** pushes nothing.
- **Turn it off:** `"enabled": false` on the file or an entry, `pa-push-allow off`, or delete the
  file; open push cards show the refusal at the next check.

## Built from

- `mods/project-agent/`: `main.js` (panel), `store.js` (file reads, guarded writes, links),
  `rules.js`, `timer.js` (schedule), `keeper.js` (the agent tab: tracking, ordering, restoring in place
  with `keeper.lock` between Herdr clients, the new-space dialog), `setup.js` (the new-space dialog), `worker.js` (the delete-worker dialog), `pathfield.js`, `project.svg`,
  `mod.json` (`{ "place": "none" }`).
- Layout: a block `{ "mod": "project-agent" }` in the right panel's stack, optionally with
  `props.profile`, and a floating block `{ "mod": "project-agent", "props": { "timer": true } }`
  that draws nothing and keeps the timer and keeper running. Optional ids it uses when present:
  a stack `panel-body` with children `files`, `agents-view` (holding a block `agents-live`).
- `runtime/` (installed to `~/.config/herdr-pa/` by `install.sh`): `core/` (the orchestrator
  and worker prompts, the phases and gates, default rules, hook wrappers), `tools/pa.py` and
  `tools/pa_hooks.py` (the `pa-*` commands), `profiles/personal/`.
- State it reads, on the space's machine: `~/.config/herdr-pa/spaces/<space-id>/` (`agent.json`,
  `ledger.json`, `decisions/*.json`, `rules.json`, `schedule.json`, `pass-request.json`,
  `evidence/<task>/`, `heartbeat`) and `~/.config/herdr-pa/profiles/<name>/profile.json`.
- `herdr` functions: `spaces`, `panes`, `agents`, `terminals`, `machines`, `focused`, `layout`,
  `window`, `readTextFile`, `writeTextFile`, `watchFolder`, `exec`, `hc`, `shared`, `setShared`,
  `run`, `openOverlay`, `closeOverlay`, `notify`, `pickFolder`, `props`, `home`, `theme`.
- Programs it runs: `hc send` (after an answer, a rule decision, a due pass, a dialog choice),
  `ls`, `sh`, `test` (listing cards, evidence and folders), `python3` (guarded writes on another
  machine), `pa-task` (your task changes), `pa-session` (starting an agent), the system opener
  for a link you click, and a profile's declared action when its button is clicked.
- Shared state it sets: `project-agent.tabs` (tab id -> `{ space, project, profile, name,
  terminal, alive? }`), `project-agent.open` (open cards per space), `project-agent.openAgent`
  (Open Project Agent requests; the panel writes, the keeper deletes), `project-agent.worktrees`, `project-agent.workers`,
  `project-agent.timer`, `my-files.root` and `my-files.places` (for a Files view that honours
  them). It reads `project-agent.show` (`{ space, task, at }`) to open a task on request.
- Agents are recognised by the label `herdr.pa=<space-id>` that the launcher sets on its own
  terminal; a launcher waiting for the dialog's choice has `herdr.pa.ask`.

## Watch out

- The mod cannot draw or guard other mods' controls. A tab bar, menu or pane header must read
  `project-agent.tabs` to leave out its close and pin controls for the agent's tab, and new
  spaces must start `space.new { argv: ["pa-session", "--ask"] }`. See README.
- `InputState.new()` throws inside `render`: note fields are made when cards load, forms and
  the path field when a button is clicked.
- Every write is compare-and-write against the file on disk; a changed file is reloaded, not
  overwritten. The mod writes only decision answers, rule changes, the schedule's settings,
  `lastPass`/`nextPass` (the timer copy only) and `pass-request.json`.
- `herdr-app check` renders the mod once with no data; it does not reach the cards, tasks or
  rules code. Test with a space that has state.
- A link is opened only if it is `https://`; a declared action whose fields the task cannot
  fill is not drawn.
- One copy of the mod does the sending and the tab keeping (a lease in `project-agent.timer`,
  taken over after 50 s of silence); after a reload the tab is unguarded for up to that long.
- Loading a space: agent.json, then the ledger, profile, schedule and pass request together (the
  header and tasks show), then the cards, rules, evidence and stale instructions together. On another
  machine all cards come from one `sh` call; if it fails, they are read one by one in parallel. Each
  remote read gives up after 10 s. One load per space runs at a time; a slow one is never dropped,
  and only a load with no read answering or timing out for 11 s is given up. A load that ends is
  used unless a newer load of its space has already been. The last load of each space (8 at most, in
  memory, also one that ends after a switch away) shows at once when the space is shown again, and
  refreshes. A read that times out, fails or is not valid JSON (the ledger, the profile, the
  schedule, the pass request, the card listing, one card, rules; evidence and stale instructions
  only when they time out) never counts as "none": the panel shows the last value known for the
  space, with the error. Except for one card (below), it writes nothing from it: the shared keys
  that part feeds (worktrees, workers, Files shortcuts, main branches, the open-card count), the
  kept load's copy of it and any note typed on a card stay as they were, so a newer value a hidden
  refresh wrote is not rolled back. Only a read that answered (a missing file or an empty decisions
  folder included) replaces them. Known limits: when the one-call card read fails, the listing
  answers and one card's own read does not, that card is shown from its last copy and the open-card
  count is written with it, so the count can be off (a newer count a hidden refresh wrote can be
  rolled back) until the next load whose reads answer; an evidence or stale-instructions call that fails rather than times out counts
  as none (the Files view loses the evidence shortcuts until the next answer); a decisions or
  evidence folder that cannot be read (permissions) counts as a missing one, not as unanswered, and
  a file that cannot be read may too (that depends on Herdr's file read). When agent.json does not answer, what is shown stays,
  with the error. Of a full load and a hidden refresh of a space, the one started later writes the
  shared keys; an older one ending later writes none. A typed note belongs to its space's card.
- While the panel is not the side panel's shown view (the `layout()` test the tabs mod uses, over
  the blocks with this copy's props), it reads, per space every 10 s and together, the cards (for
  the open-card count) and the ledger. From the ledger it refreshes the shared worktrees, workers
  and Files shortcuts (evidence counts as of the last full load); the main branches come from the last
  full load's profile, or with none, from the profile read once per space per session. New-card notices
  come from the timer copy and do not depend on it.
- Scheduled passes need Herdr's window open. The minimum cadence is 5 minutes. Rule fields are
  single-line inputs.
- Without the runtime in `~/.config/herdr-pa` every space shows "no Project Agent".
- Costs are an estimate: tokens times the profile's price table, which can differ from the bill.
  Billed figures are account-wide (other use of the account is in them). Reconstruction is best
  effort, from the session files still on disk; the programs sweep old ones after their retention.
  A remote space's billed comparison passes its summary (at most 100 KB) to the local `pa-costs`.

## Done when

- `herdr-app check mods/project-agent` passes and `pa-core-check` reports no profile terms.
- A new space shows the dialog; its primary button gives a first tab named `PA: <folder>` that
  cannot be closed and comes back at position 1 when its terminal is killed.
- A space whose state folder has an open card shows it; picking an option and submitting
  changes the file to `answered` and the agent's terminal receives `decision <id> answered`.
- Breaking `rules.json` by hand shows the error and no button writes to it.
- With a task active and the last pass older than the cadence, the agent's terminal receives
  `scheduled pass` within 15 s, and `schedule.json` gets a new `lastPass` and `nextPass`; once the
  app shows a dead launcher's terminal as an idle shell (or its heartbeat is 40 s old), that
  terminal receives no pass (README, "Messages go only to a live launcher", lists the remaining
  windows).
- With no Project Agent running in a configured space, the header says "Not running" and Open
  Project Agent opens one tab `PA: <name>` that resumes the conversation; a second click while it
  runs opens nothing.
- In a scratch space, each `t-0xx-<job>` worker is closed in the pass that reads its report (journal trigger `close`, its chip leaves the task card), and no `d-wt-*` card opens between jobs.
- Marking that task done opens a `d-wt-<task>` card on the next pass listing its tasks, branch, commits, files and uncommitted files, one per line; `WORKTREE.md ↗` opens the full list.
