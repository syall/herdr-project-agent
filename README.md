# Herdr Project Agent

A per-space orchestrator agent for [Herdr](https://herdr.dev): it plans work, hands it to worker
agents, and brings you in only for decisions. This repo is the generic part. It names no
company's tools; a profile maps its terms to real ones.

| Folder | Installed to | What it is |
|---|---|---|
| `mods/project-agent/` | `~/.config/herdr-app/mods/project-agent/` | The panel (decisions, tasks, rules, schedule), the new-space setup dialog, the pass timer, and the keeper that tracks each Project Agent tab |
| `runtime/core/` | `~/.config/herdr-pa/core/` | The orchestrator and worker prompts, the SDLC, default rules, hook wrappers |
| `runtime/tools/` | `~/.config/herdr-pa/tools/` | The `pa-*` commands (`pa.py`, `pa_hooks.py`), linked into `~/.local/bin` |
| `runtime/profiles/personal/` | `~/.config/herdr-pa/profiles/personal/` | A profile for personal development |

## Install

```sh
./install.sh                 # this machine
./install.sh --check         # what differs from the installed copy
./install.sh --on <machine>  # also copy the runtime to another Herdr machine (see `hc machine ls`)
```

Then add the mod to your layout, as `RECIPE.md` describes: a panel block
`{ "mod": "project-agent" }` and a floating block `{ "mod": "project-agent", "props": { "timer": true } }`.

State lives in `~/.config/herdr-pa/spaces/<space-id>/` and is not part of this repo.

## What your tab bar needs

The mod keeps shared state `project-agent.tabs` (tab id -> `{ space, project, profile, name, terminal }`,
plus `recovering` while the keeper restores that tab, and `alive: true` while the keeper knows the
launcher runs in `terminal`: the terminal is live, labelled for the space, not an idle shell, and a
heartbeat at most 40 s old names it). The keeper that wrote the marks also writes
`project-agent.keeper` (`{ id, at }`, rewritten every 10 s); readers trust `alive` only while that
keeper holds the timer lease (`project-agent.timer`, fresh within 50 s) and its mark is under 20 s
old, so marks left by a reloaded or closed copy count for nothing until the new keeper has judged.
`stopped: true` marks a record whose keeper gave up restoring it (3 failures in 10 minutes).
It cannot draw or guard controls that other mods own, so those mods read that key (presence and
`terminal` are enough):

- the tab bar: no close or pin control on a listed tab; the close-tab shortcut refuses it;
- the tab and terminal right-click menus: no "Close" / "Close pane" for it;
- a pane header: no close button on the pane whose terminal is the record's `terminal`.

New spaces start the launcher with `space.new { argv: ["pa-session", "--ask"] }`.

## How the keeper restores a Project Agent

- **Configured** means `spaces/<id>/agent.json` exists on the space's machine and has no `closed`
  mark. The keeper does nothing for other spaces and drops any record it had for them; while a
  machine is offline, or before its agent.json was read, it keeps the record and does nothing.
- **Detection:** the launcher (`pa-session`) rewrites `spaces/<id>/heartbeat` every 10 s. Read every
  15 s, a heartbeat unchanged for 2 comparisons of reads 12-30 s apart (closer reads do not count; a
  longer gap, such as a sleeping computer, starts again) means the launcher is gone, about 45 s at
  most. A terminal younger than 30 s is never judged.
- **In place:** only if what is left is an idle shell at its prompt (the program table names a shell, the
  foreground process is a shell when the app reports one, and nothing reports an agent status), or the
  terminal finished but the tab remains, the keeper takes `spaces/<id>/keeper.lock` (so a second Herdr client does not do the same), splits the
  tab's pane with a new `pa-session`, points the record at the new terminal as soon as it appears,
  and closes the old pane only once the new launcher is labelled and beating, and only if the old
  terminal is still an idle shell (otherwise it is left open and the notice says so). The tab id, and so
  the record that protects the tab, never changes. The new launcher resumes the same session.
- **Hands off:** a tab running anything else (an editor, a build, `ssh`, an unknown program, or any agent,
  idle or not, e.g. a hand-started `claude`) gets one notice and is left alone; when in doubt the keeper
  restores too rarely rather than close something alive. Worker terminals (`parent` set) are never touched.
- **Failures:** a new `pa-session` labels its terminal `herdr.pa.starting` as soon as it begins; one
  that cannot start shows the error in its tab, labels it `herdr.pa.error=<why>`, and waits for Enter.
  The keeper waits with either and never closes them (it says once if an error shows, or if a start
  takes over 3 minutes). If no new pane appears within 30 s, the restore is given up. If the new pane
  shows no launcher at all (an older `pa-session` that refused, or one the person stopped), the keeper
  says so after 30 s and gives up after 2 minutes, closing that pane only if it is an idle shell it made.
  A launcher that said it began and then died (its terminal is an idle shell for 30 s) is given up the
  same way. It tries again a minute after the last attempt began; after 3 failures in 10 minutes it
  stops (notice "Project Agent needs you"; with an older remote `pa-session` that takes about 7 minutes).
- **Install order for other machines:** run `pa-sync --on <machine>` before installing the new mod.
  An older `pa-session` there refuses to start next to the dead shell it should replace (its
  one-per-space check does not test the heartbeat), so every restore on that machine ends as a plain
  shell: the keeper gives up on it after 2 minutes (closing it if it is an idle shell), retries, and
  after 3 tries says "Project Agent needs you". Nothing is
  lost: the old tab and its record stay; `pa-sync` and then `pa-start` in the space fix it.
- **A vanished tab** (closed some way the mods do not guard) is still reopened in a new tab; its old
  record stays until the new tab is recorded.
- **Open Project Agent** (Project view) covers what the keeper does not reopen by itself: a space with
  no record (a hand-run or never-recorded agent, or a tab closed while no keeper ran), or a keeper
  that gave up after 3 failures. It shows whenever agent.json exists, the space's machine is ready,
  the current keeper has judged, and no record is `alive`. While the machine is offline the header
  says "Machine offline · status unknown"; before a keeper has judged, "Status unknown"; while the
  keeper restores the record's tab, "Restoring…" (with the launcher's error, if it waits on one), and
  while it reopens a vanished one, "Reopening…". None of these offers Open. A keeper that stopped
  after 3 failures marks its records `stopped: true`: then the panel says "Not running" and offers
  Open, also when the tab has vanished. An Open over a record that is not running (a kept shell, a
  hand-run program) replaces that record and retires its terminal: it is not adopted or marked alive
  again unless a launcher really starts there later (a new heartbeat, fresh, naming it).
- `pa-session` and `pa-start` refuse to start a second Project Agent only while another terminal's
  launcher is alive; a labelled terminal whose heartbeat stopped is replaced.

## Open requests: `project-agent.openAgent`

Shared `project-agent.openAgent` = `{ "<space id>": { "at": <ms>, "by": "panel", "why"?: "<text>" } }`.
The Project view writes an entry when Open Project Agent is clicked; only the keeper deletes it, and
only the entry it handled (same `at`). The keeper takes a request once the space's machine is ready
and its agent.json has been read since the request: it takes `keeper.lock`, runs
`tab.new { space, folder: <project>, name: "PA: <name>", focus: true, argv: pa-session --space … --project … --profile … --name … }`
(the launcher resumes the conversation by its own rule), and records the new tab on the first tick it
shows, before the launcher labels it. It refuses (deletes the request, one notice) while a record's
launcher runs or beats elsewhere, a labelled terminal of the space has a fresh heartbeat, or a restore
or open is under way. While it waits it writes `why`; after 30 s it gives the request up with a notice.
A request is also refused while the space's recorded launcher is starting or waits for Enter on an
error; the panel keeps "Opening…" and shows that error. A live agent that looks like this Project
Agent but does not run under its launcher (tab name or pane title `PA: <name>`, a folder in the
project, or the space's label) gets a confirm in the panel first.
(`project-agent.open` is something else: the number of open decision cards per space.)

## Messages go only to a live launcher

The panel's "decision … answered" and "rule …" notices are sent to the record whose `alive` is true,
looked up when sending; with none, nothing is sent and the panel says "Answer saved. The Project
Agent is not running." The pass timer sends passes only to an `alive` record's terminal; new-card
notifications still cover every recorded space. `pa-pass`, and the notices of `pa-task` and
`pa-worker delete`, also require the launcher's heartbeat to name the terminal and be at most 30 s
old.

The remaining windows:
- When a launcher dies, the panel and the timer stop as soon as the app shows its terminal as an
  idle shell (the next keeper tick), or at the latest when its last heartbeat is 40 s old;
  `pa-pass`, `pa-task` and `pa-worker` stop when it is 30 s old. A kept shell that still shows the
  agent's last status or report is not counted as an idle shell, so it stays `alive` for up to
  about 40 s (measured 38 s). A message sent in those seconds can still reach the terminal the
  launcher just left.
- After a mod reload or a quick app restart, the old marks look current until the keeper's mark is
  20 s old (it rewrites it every 10 s while it runs). No pass is sent then (the timer waits for its
  own keeper), but an answer notice can still go to a terminal whose launcher died in those
  seconds, and the header can show a stale status.
- On the first tick after a keeper takes over, before its first heartbeat read lands, a record can
  show "Not running" with Open for a moment; a click then is refused ("already running").

A remote launcher must write the heartbeat
(run `pa-sync --on <machine>`): one from before the heartbeat existed is never `alive`, so it gets
no passes and no answer notices.

## Pushing main from the panel

Off by default. No agent pushes a main branch: not the Project Agent, not a worker. For the
repositories you name, a **push card** lets you push it yourself with one click in the panel.

- **The allowlist** is `~/.config/herdr-pa/push-allow.json` (under `HERDR_PA_HOME` when that is
  set), one per machine; a remote space uses the one on its machine. Only you write it, by hand or
  with `pa-push-allow` in your own terminal. `install.sh` and `pa-sync` never create, change or
  copy it. A missing file means off. Shape (every key checked; an invalid file allows nothing):

  ```json
  { "version": 1, "enabled": true,
    "repos": [ { "path": "/home/me/src/my-tool", "url": "git@example.com:me/my-tool.git",
                 "branch": "main", "space": "<space id>", "enabled": true,
                 "prePush": ["/home/me/bin/check-before-push.sh", "{path}"], "note": "optional" } ] }
  ```

  `path` is the main checkout; an entry matches only that path, that exact URL, that branch and
  that space. `prePush` (optional) is a program run before the push, in a fresh temporary clone of
  the commit; a non-zero exit refuses the push. Placeholders: `{path}` (that clone), `{repo}` (the
  main checkout), `{commit}`, `{url}`, `{branch}`.
- **`pa-push-allow`**: `show [--json]` (anyone, read-only); `add --path P --space S [--url U]
  [--branch B] [--pre-push ARG...]`, `remove --path P --space S`, `on` and `off` (you only: it
  refuses agent shells).
- **The push card.** Once a task for a listed repository has passed its gate, you have approved
  the card that asks to publish or merge it (the one that lists every Recorded limit), and nothing
  was committed after the gate, the Project Agent may open a push card with `pa-push-main plan
  <task-id> --repo <main checkout>`. The panel shows facts the tool works out when the card is shown
  (`pa-push-main check`): the repository, the URL from your allowlist, the remote and local heads,
  every commit that would go out (marked when not from this task), the gated commit and the exact
  commands. **Push** then **Submit** runs `pa-push-main run`; **Not now** pushes nothing.
  `pa-decide` cannot answer a push card.
- **What `pa-push-main run` checks and runs.** It refuses any agent process (agent variables or an
  agent among its parent processes, or a Herdr terminal: only the panel runs it). Under a lock it
  checks the allowlist again (and that it did not change since the card), the gate and its commit
  pin, that the local main and the remote main have not moved, that the push is a fast-forward, and
  that the facts you saw are still true. It runs `prePush`, fast-forwards the local main if needed,
  then pushes exactly the gated commit to the allowlist's URL (never a remote name), with the
  repository's hooks off and no `GIT_*` variables. It never forces. A second run of the same card
  pushes nothing. The result goes on the card and in the journal.
- **Turning it off:** set `"enabled": false` on the file or on an entry, run `pa-push-allow off`,
  or delete the file. It applies at the next check: open push cards show the refusal. Nothing
  else needs undoing.

## Profiles

A profile is a folder in `~/.config/herdr-pa/profiles/<name>/` (`profile.json`, `context.md`, `rules.json`,
`hooks/`). `pa-profile set <name>` makes it the machine's default. Profiles live in their own repos.

## Checks

`pa-core-check` fails if `runtime/core`, `runtime/tools` or the mod names a profile's tools. Each
profile lists its own terms in `profile.json` (`"coreCheck": ["..."]`), so this repo holds no such list.
`herdr-app check mods/project-agent` loads the mod once.
`tests/run-all.sh` runs this repo's tests (a Python 3.7 parse check, the unit tests in
`tests/test_*.py`, a keeper harness, a panel render harness and a store harness under Node (the last two
simulate a slow or hung remote machine), and a `pa-session` check
against a private hc core); `--affected` runs only the
fast set. After updating, run `pa-sync --on <machine>` before installing the mod, so remote launchers
get the new `pa-session` (see the install order above).

## Licence

Apache License 2.0; see `LICENSE` and `NOTICE`.
