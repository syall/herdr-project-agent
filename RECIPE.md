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
   custom number, Off, quiet hours and "Only while tasks are active".
4. Three view pills: `Decisions · n`, `Tasks · n`, `Rules · n`.
5. The chosen view, scrolling: cards with a 1 px border and 7 px corners on `surface`.

A new space opens with a 600 x 440 dialog: a Project Agent card (`Set up for <folder>`,
`Another folder…`) and three tiles (Claude, Codex, Terminal).

## How it behaves

- **New space:** the dialog's primary button turns the space's first tab into the Project Agent
  tab. "Another folder…" swaps the page for a path field with folder completion (Tab or a click
  completes, Enter confirms; "Browse…" on this computer). A tile opens that session instead.
  Keys: Enter, 1 to 5, Esc. The tab underneath accepts the same choice typed.
- **The agent's tab:** kept first in its space. If its launcher is gone (a bare shell is left, or
  its terminal finished), it is restored in the same tab within about 45 s and resumed with the
  same conversation; the tab stays protected throughout. A tab that vanished is reopened in a new
  tab. A launcher that cannot start shows why and waits for Enter. It ends when its space is closed.
  After updating this repo, run `pa-sync --on <machine>` so remote launchers get the same behaviour.
- **Decisions:** open cards first, newest first. Clicking an option selects it; `Submit answer`
  sends it with the note. The answer is written into the card file and the agent is told with
  `hc send`; a failed send is retried every 10 s and the panel says so. The card's link opens
  the evidence file it is about. Answered cards fold away below.
- **Tasks:** one section per state (finished ones start folded), newest first. An expanded task
  shows its goal, tier, a chip per worker (a live one opens in the Agents view), a button per
  open decision, a button per artifact link, Evidence (only when there is some), the profile's
  declared actions, and your own controls: Reopen, Mark done, Park, Delete… (two clicks; the
  task, its evidence and its cards go to a trash folder).
- **Workers stay until you close them.** The × on a worker (in a task, or in an Agents view
  that opens this dialog) asks what to delete with it: its terminal, its worktree when nobody
  else uses it (not ticked when it holds unsaved work), and optionally its task.
- **Rules:** proposals first (Approve, Edit then approve, Decline), then your rules in precedence
  order with an On/Off pill, Edit and a two-click Delete; drag a rule by its grip onto another
  to place it above. Add, Export, Import. An invalid `rules.json` is shown as an error and never
  written over.
- **Passes:** every 15 s one copy of the mod checks each live agent's `schedule.json` and sends
  `scheduled pass` when due, never while the agent is `working` or `blocked`. On-demand passes
  queue the same way.
- **No agent in the space:** a short explanation and, outside the home folder, a start button.

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
  terminal }`), `project-agent.open`, `project-agent.worktrees`, `project-agent.workers`,
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
- Scheduled passes need Herdr's window open. The minimum cadence is 5 minutes. Rule fields are
  single-line inputs.
- Without the runtime in `~/.config/herdr-pa` every space shows "no Project Agent".

## Done when

- `herdr-app check mods/project-agent` passes and `pa-core-check` reports no profile terms.
- A new space shows the dialog; its primary button gives a first tab named `PA: <folder>` that
  cannot be closed and comes back at position 1 when its terminal is killed.
- A space whose state folder has an open card shows it; picking an option and submitting
  changes the file to `answered` and the agent's terminal receives `decision <id> answered`.
- Breaking `rules.json` by hand shows the error and no button writes to it.
- With a task active and the last pass older than the cadence, the agent's terminal receives
  `scheduled pass` within 15 s, and `schedule.json` gets a new `lastPass` and `nextPass`.
