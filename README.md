# Herdr Project Agent

A per-space orchestrator agent for [Herdr](https://herdr.dev): it plans work, hands it to worker
agents, and brings you in only for decisions. This repo is the generic part. It names no
company's tools; a profile maps its terms to real ones.

| Folder | Installed to | What it is |
|---|---|---|
| `mods/project-agent/` | `~/.config/herdr-app/mods/project-agent/` | The panel (decisions, tasks, rules, costs, schedule), the new-space setup dialog, the pass timer, and the keeper that tracks each Project Agent tab |
| `runtime/core/` | `~/.config/herdr-pa/core/` | The orchestrator and worker prompts, the SDLC, default rules, hook wrappers |
| `runtime/tools/` | `~/.config/herdr-pa/tools/` | The `pa-*` commands (`pa.py`, `pa_hooks.py`, `pa_costs.py`, `pa_push.py`), linked into `~/.local/bin` |
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
- **Restarting in place:** while a launcher restarts in place (see "Restarting a Project Agent") it labels
  its terminal `herdr.pa.restarting=<request id>`. The keeper holds such a terminal for at most 2 minutes
  from when it first sees the label: it is never restored, an Open is refused ("it is restarting"), and
  it is not marked `alive`, so no notice is typed into the few seconds between the two Claude sessions
  (the panel keeps them and sends them after). The record carries `restarting: true` and the header says
  "Restarting…". A label on an idle shell does not hold, a finished terminal is restored as always, and a
  label kept past the 2 minutes is judged as any other terminal.

## Restarting a Project Agent

A restart makes a Project Agent run what is installed now, in the same terminal, with the same workers,
resuming the same conversation. Its launcher (`pa-session`) ends its own Claude and starts the installed
`pa.py` again in the same process: the pid, the terminal, its address and every worker tied to it stay.

- **What reloads:** the launcher's code (`tools/`), the profile (command, models, MCP servers, status line)
  and Claude Code itself. The core text (`core/orchestrator.md`) and the profile's `context.md` reach the
  model on a resume only when Claude Code rebuilds the system prompt: the launcher passes
  `--system-prompt-snapshot off` when the profile's `systemPromptSnapshot` is `off`, or, with no key, when
  `claude --help` lists the flag (`keep` never passes it). Otherwise the resumed conversation keeps the
  text it began with until it compacts, and a restart into a new conversation (`--fresh`) loads it now.
  Rules (`rules.json`) are read from disk each pass. Whether a resume re-reads your own `CLAUDE.md` files
  is not documented; it is not claimed.
- **From a terminal:** `pa-restart --space <id>` checks that the terminal's program is that space's
  launcher (agent.json, `launcher.json`, a fresh heartbeat, hc's label and pid), says what it runs now
  and what is installed, lists the workers (each keeps running), waits for the current turn to end
  (`--now` cuts it off), and prints what the new launcher loaded. `--dry-run` only checks and says;
  `--fresh` starts a new conversation (the previous one stays in `/resume`); `--on <machine>` runs it
  there. The Project Agent runs it only at your word, with `--why` quoting you. Workers are refused it.
- **From the panel:** **Restart** in the Project view's header (and beside the "Its instructions or tools
  changed" notice) asks first: it runs `pa-restart --dry-run` on the space's machine and shows its lines.
  **Restart** waits for the turn, **Restart now** cuts it off, and **Restart in a new conversation**
  shows when the resumed conversation would keep older core text. Confirming runs `pa-restart` once,
  tied to the launcher it showed (a hidden `--expect-instance`): a second click, or another window or
  Herdr client, after a restart finds a new launcher and restarts nothing. The header then says
  "Restarted: tools …, core …, profile read …" for a minute, or why it did not happen.
- **What runs, against what is installed:** each launcher writes `spaces/<id>/launcher.json` (what it
  loaded); `install.sh` and `pa-sync` write `installed.json`. The header shows "older code (tools)",
  "(core)", or "(tools, core)", or "started before restart support"; after an install,
  `pa-peers --older` lists the Project Agents still running older code. `/exit` in the Project Agent's
  tab also reloads the code when the installed tools changed.
- **Launchers from before restart support** cannot take a restart. The panel's button reads "Restart
  (ends workers)"; it shows `pa-restart --space <id> --end-terminal` (with `--on <machine>` for a remote
  space) to copy and run yourself. That ends the terminal and its workers, after it asks; the panel never
  ends a terminal.

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

Shared `project-agent.answering` = `{ "<space id>\u0000<card id>": { "label", "note", "at": <ms>, "by": "<panel copy>", "ttl"?, "unknown"? } }`.
A panel copy writes an entry when the person submits a decision card there; every other copy draws that
card answered ("Answered in another panel window.") and offers no option or Submit while the entry lasts
(an unknown push result says so instead, see below). For an ordinary or rule card that is 120 s from `at`.
A push card's entry lasts while its push run is in flight (its `ttl`, at most 725 s), then 120 s once the
run ends answered (an answered result, not an unknown one); a refused run removes it. An unknown push
result (`unknown: true`) never expires: every copy keeps the card locked, with only Check again, until a
check from any panel copy, asked at or after the result, reads the card open; that removes the one shared
entry, so every other copy unlocks. The copy that ran the push stays locked until a check of its own
reads the card open. A copy's load that reads the card file and finds it no longer open also
removes the entry (stale entries are cleaned up by `resolveMarks`), and the card is then drawn from the
file. Writing any entry also drops entries that have expired. The copy that submitted
draws the card answered from the click until a read of the card file shows the answer (or a change made
after it); only a write that really failed (the file still open) unlocks the card, with "Not saved: …".

Every card in the Decisions tab also has an **Other** row under its options: pick it, write your own
answer in the card's text field (at most 2000 characters) and submit. The card is written
`"answer": "Other", "other": true` with your words in `"note"`, and the notice to the Project Agent
carries them (on one line, cut at 400 characters; the card keeps them all). No tool acts on an Other
answer: a rule card answered Other leaves the rule proposed, and on a push card Other runs
`pa-push-main run --answer Other`, which closes the card and pushes nothing. A card withdrawn after an
Other answer keeps it as `previousAnswer` / `previousNote`, and a card asked again after it shows it.

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

## The worker guard

Each worker's `pa-hook guard` refuses a short list of commands that the core Safety rules keep for you:
`git push` in any form, opening, readying or merging a pull request, deleting a branch or a worktree, and
`pa-push-main`, `pa-push-allow` and `pa_push.py`. It keeps the older refusals (force pushes, pushes to a main
branch, discarding uncommitted work, reading secrets, a profile's `releaseTools`) and refuses editor writes
into `.git` and the Project Agent's state. It is a fence for workers that follow their rules, not a sandbox: a
script a worker writes can still do what your user can. When it cannot load, it refuses everything. The rules
it had before (an allowlist, plain lines, a Bash write rule) are described in `docs/worker-guard-history.md`,
with the commits that hold them.

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

## Costs per space

`pa-costs` adds up what each space spends, from the usage counters in the session files of the
sessions started for it (never their text). Tokens are measured; **every money figure is an
estimate** (`est.`) from the price table in the profile (`profiles/<name>/prices.json`; this repo
ships none, so without one the figures are tokens only).

- `pa-costs collect [--space ID] [--quiet]` reads the new bytes of the space's session files into
  `spaces/<id>/costs/` and rewrites `costs/summary.json`. The panel's timer runs it on the space's
  machine: every 120 s while that space's Costs view is shown, every 30 min otherwise. It passes the
  panel's timezone (`--zone-offsets`, `--zone-name`, kept in `costs/zone.json`), so the view's Today,
  Last 7 days and By day are the person's own days, cut at their local midnight, daylight saving
  included, wherever the space runs. Everything stored stays UTC; prices are looked up by a row's UTC
  day; a summary in another zone (or from an older pa-costs) is drawn with that zone named, e.g. "(UTC)".
  The timezone table covers the last 10 years (`--zone-from` says where it starts); a day older than
  that is its UTC day, marked "(UTC)" beside its date.
- `pa-costs report [--by KEY] [--json] [--billed] [--utc]` prints the same figures as the Costs view, by
  this terminal's local days (`--since`/`--until` too), or UTC days with `--utc`;
  `--by provider|endpoint|region|account|scope` groups by what each cost was priced under (`--by scope`
  also says which price scope priced it). `pa-costs report --account-wide [--account LABEL]` prints the
  estimates of that account label (default: this machine's) from every space on the machine. Under
  Today it prints the Project Agent's context
  (`PA context  mean 158k  peak 254k  resets 6 (fresh 6, compact 0)`: prompt tokens per call of its
  own conversation, its subagents left out, and its fresh starts and compactions that day);
  `pa-costs report --by context` prints it per UTC day with calls, mean, peak, resets, the first
  call of fresh sessions, est. and est. per pass. A peak marked `≥` is a lower bound (rows recorded
  before per-call peaks were kept).
- `pa-costs reconstruct [--dry-run]` finds the space's sessions from before launch records existed,
  best effort, marked `reconstructed` (◌ in the view). The view's `Reconstruct from session files` runs it.
- `pa-costs prices` shows the table in use, each price scope as a heading, each row's `source`, and the
  models seen without a price (per provider · endpoint · region · account when there are scopes).

A price table, in the profile:
```json
{"v": 1, "currency": "<code>", "unit": "per-1M-tokens", "source": "<where from>", "asOf": "2026-11-01",
 "models": {"model-a": [{"validFrom": "2026-11-01", "in": 1.0, "out": 5.0, "cw5m": 1.25, "cw1h": 2.0, "cr": 0.1}]}}
```
A model with no price is shown as tokens with `no price`, never as 0.00; a total that leaves part of
the use unpriced says `+ unpriced`.

**Price scopes** (optional keys, same `v: 1`; older `pa-costs` ignore them and price everything from
`models`): the same model can cost differently by provider, endpoint kind (`global`, `geo`,
`regional`, or a profile's own), source region and billable account.
```json
{"defaultScope": {"provider": "provider-a", "endpoint": "global", "region": null, "account": null},
 "models": {"model-a": [{"validFrom": "2026-11-01", "in": 1.0, "out": 5.0, "cw5m": 1.25, "cw1h": 2.0, "cr": 0.1}]},
 "scoped": [{"provider": "provider-a", "endpoint": "regional", "region": "region-a", "account": null,
             "models": {"model-a": [{"validFrom": "2026-11-01", "in": 1.1, "out": 5.5, "cw5m": 1.4, "cw1h": 2.2, "cr": 0.11}]}}],
 "scopedAliases": [{"match": "^model-c-", "model": "model-c"}]}
```
`defaultScope` is the scope of the top-level `models` (an older `"defaultProvider": "P"` means provider P,
any endpoint, region and account; without either, the profile's `pricing.defaultProvider`). A `scoped`
entry names at least one of the four (null = any); `scopedAliases` point at keys that exist only in
scoped tables. A cost is priced by the most specific scope that matches its facts (one that names the
account wins, then the region, then the endpoint, then the provider), from that scope's latest row on or
before the day; a scope without such a row passes to the next. An unknown provider or endpoint takes the
default scope's; an unknown region matches only scopes without a region. A cost is never priced from
another provider's or another endpoint's rows: with no matching row it shows `no price`. The summary,
`report --json` and the view say for each cost what it was priced under (`pricedAs`).

**What each session records for its price** (labels only, in its launch record and its ledger rows):
- provider and endpoint from the profile's `pricing.providers` (`{"program", "model": "<regex>",
  "provider"}`, first match) and `pricing.endpoints` (`{"model": "<regex>", "endpoint"}` on the
  requested `--model`, or `{"env": "<NAME>", "match", "endpoint"}`); `pricing.endpointPriceAs`
  (`{"geo": "regional"}`) prices one kind as another;
- the region from `PA_COST_REGION`, else the first variable named in the profile's `pricing.region`
  (`{"env": [...], "match": "<regex>"}`) that is set, stored only if it matches, else the machine
  config's `costs.region`, else unknown;
- the account from `PA_COST_ACCOUNT`, else the machine config's `costs.accountRules`
  (`[{"env": "<NAME>", "match": "<regex>", "account": "<label>"}]`: the variable is tested and only the
  label is stored), else the space's `agent.json` `costs.account`, the machine's `costs.account`, the
  profile's `billing.account`, `default`.
A variable's value is never stored unless it is the accepted region label; names that look like
credentials (`KEY`, `SECRET`, `TOKEN`, `PASSWORD`, `CREDENTIAL`, `SESSION`) are refused and not read,
and so is any label with 12 digits. `pa-doctor` notes refused rules and values (names only). Sessions
recorded before this get their provider and endpoint from the same model rules, region unknown, and the
account from that chain.

**Fetching prices from a public price list** (the person's command; agents and workers cannot run
it, `--dry-run` included, since it downloads): `pa-costs prices fetch [--source NAME] [--into PATH]
[--dry-run] [--yes] [--profile NAME]`. A profile declares its sources in `profile.json` as `prices.sources` (this repo
declares none, so the command says "declares no price source"):
```json
"prices": {"sources": {"src-a": {"label": "<text>", "providers": ["provider-a"],
  "defaultScope": {"provider": "provider-a", "endpoint": "global", "region": null, "account": null},
  "urls": {"region-a": {"url": "https://prices.example.test/region-a/index.json", "region": "region-a", "maxBytes": 5000000},
           "region-b": {"url": "https://prices.example.test/region-b/index.json", "region": "region-b", "maxBytes": 5000000}},
  "defaultRegion": "region-a", "regionFree": ["global"],
  "read": ["prices/<reader script>", "--file", "{file}", "--region", "{region}"], "currency": "USD",
  "models": {"include": [], "exclude": [], "onlyWithEndpoint": null}}}}
```
- **Download**: only the declared `https` URLs (a DNS host name, no port, user, query or IP address),
  no redirect followed, no credential, cookie or authentication sent (`User-Agent: herdr-pa-costs`
  only; proxies from `HTTPS_PROXY`), each file at most its `maxBytes` (50 MB at most), all of them
  within 120 s, into a temporary folder removed at the end. Certificate checks are never turned off.
- **Reader**: the profile's script (inside its folder, no shell, with only `PATH`, `LANG`, `HOME` and
  `TMPDIR` in its environment) runs once per file and prints one JSON line per price
  (`provider, account, endpoint, region, model, kind` (`in|out|cw5m|cw1h|cr` or `long.<kind>`),
  `price` (a number, or null with `why`), `effectiveDate`, `ref`), then
  `{"done": true, "source": {"url", "version", "publicationDate"}}`. One bad line refuses the whole
  batch (exit 4); exit 6 means it did not recognise the file's format (exit 4).
- **What is written**: a price of a `regionFree` endpoint comes from the `defaultRegion` file into a
  scope with no region (the other files' equal rows are named in `refs.<kind>.sameIn`; a region whose
  row differs gets a scope of its own); every other endpoint gets one scope per region. Existing rows
  are never edited or removed: a changed price is a new row from the source's `effectiveDate` (or from
  today when that is not after your latest row, so earlier days keep their prices); a null from the
  source never removes a price; models the source lacks, or the profile's `models` limits leave out,
  stay as they are. A file without `defaultScope` gets the declared one on its first fetch (the diff
  says so). Each new row carries `source`, `fetched` (url, version, publicationDate, sha256) and `refs`.
- **Flow**: it prints every change (`scope / model / kind: old → new (validFrom)`), then asks before
  writing (`--yes` skips only the question; off a terminal it needs `--yes`), copies the old file to
  `prices.json.<stamp>.bak` (the last 5 kept) and replaces it in one step. It writes nothing when the
  file changed meanwhile, is a link, or has a scoped entry this tool cannot read. The default target is
  the installed profile's `prices.json`, which the next install overwrites: use `--into <profile
  package checkout>/profiles/<name>/prices.json`. Exit 0 written or no change, 1 failed, 2 bad
  arguments, 3 refused (an agent, a worker's terminal, no terminal, over 120 s), 4 the source's format
  or the reader's output not accepted, 5 declined. `pa-costs prices` lists the declared sources.

The account label of estimates and of billed rows is the same chain: on a machine without
`costs.account`, a profile's `billing.account` now labels the estimates too (they said `default`
before), so `billed compare` sets this space's estimates beside the bill of that account, and
`--account-wide` finds them under that label. Set `costs.account` in the machine config to choose it.

**The Queue view** (Project panel, third tab) shows each repository's merge queue from `queue.json` as
`pa-queue` last wrote it, read only: the entries in merge order with their state, the branch each is
stacked on, what each waits for and its approval, then the entries that left (why, the fix round they
went to) and the last merges; `pa-queue show` reads each state now.

**The Costs view** (Project panel, fifth tab) reads only `costs/summary.json` on the space's machine,
only while it is shown, at most every 10 s. It shows Total, Today and Last 7 days, then the spend by
activity, task, session type and day, each with its drill-down, and `Refresh` (runs `pa-costs
collect`). Nothing in it writes anything except those two buttons and `Turn off` below.
It says which account and scope each cost belongs to, from the same summary: with one account label,
the label beside Total (`account <label>`); with two or more, an **Accounts** section (each with its
sessions). With two or more scopes, a **By scope** section, one row per scope in words (`provider p,
endpoint n, account a, no region recorded`: a fact not recorded, or refused, is said so), with `priced
from the price file's default prices valid from <date>` (or `… prices for provider p`) where its price
came from another, wider scope and `no price` where none applies; each session line then names its
scope (and `rows in N scopes` when it spans several: the largest is shown), and a model's drill lists
its scopes. With one scope priced from another, one line under the tiles says so (`All costs priced
from the price file's default prices valid from <date> and <date> (no provider, endpoint or region
recorded) · account <label>`). A summary from before these labels draws as before.

**Billed amounts** (optional): rows in a neutral shape in `<HERDR_PA_HOME>/billing/billed.jsonl` on
the panel's machine (`{account, from, to, label, model, kind, quantity, unit, amount, currency, final,
accountWide, source}`), from a script the profile declares:
- `pa-costs billed import <file> --via <importer>` takes rows from a file you exported;
- the scheduled billing query: a read-only query script in the profile, run with a read-only
  credential **you** name. Nothing runs until you turn it on, in your own terminal:
  `pa-costs billed enable --query <name> --credential <read-only profile> --read-only-match <pattern>`.
  Then the panel's timer asks it at most once an hour and it queries at most **once a day** (at most 3
  runs a UTC day, at most 3 requests each); the profile states the fee per request, and the view shows
  it with the last run. `pa-costs billed disable`, or `Turn off` in the Costs view, turns it off
  (billed rows are kept). Agents and workers cannot run `enable`, `fetch` or `import`.
- `pa-costs billed status`, `list`, `remove <id>`; `pa-costs billed compare --json` (read-only) is
  what the view shows: estimated (this space, all spaces here) beside billed, per model.

Billed figures are **account-wide**: they include use outside this space and outside Herdr, so they
are never shown as this space's spend. A price factor (from billed quantities) gives a `calibrated`
figure beside `est.`; a scope ratio is shown but never applied.

**Claude Code's own figure** (`CC`, optional): Claude Code computes a cost per session itself, at list
price, in USD, and hands it to its status line. When the profile declares `statusLine` (below),
`pa-claude` (for a Project Agent's workers) and `pa-session` (for the Project Agent) add one
`--settings` that makes the session's status line `pa-costs statusline --space <id> --then <your
status line command>`. It records the session id, that figure, its durations, the requested model,
Claude Code's version and the terminal in `spaces/<id>/costs/cc/<session>.json` (ids and numbers
only, never text), then prints exactly what your own command prints. It never slows the line by more
than its 2 s limit (a slower command shows nothing that update), and never fails. `collect` and
`report` show it beside `est.`: est. leads wherever a price table gives one; CC is shown next to it,
labelled "Claude Code ... USD list price (n of m Claude sessions)", and is the only money figure where
there is no price. It is never added to est. and never called billed: it is Claude Code's estimate,
not the bill (no provider tiers or discounts). A resumed session is counted once: Claude Code's figure
already holds the session's earlier turns, and a restart from zero (lower cost and duration that then grow no
faster than the clock) adds a segment. A record written before this that counted the history again (its duration
longer than its own time span) is read as its last figure alone, marked `ccCorrected` in the summary.
- Not added when: the caller gave its own `--settings`, the profile has no valid `statusLine`, or
  `PA_NO_CC_STATUSLINE=1` is set (the off switch; removing the key also turns it off). Your status
  line command must be found when the session starts, or nothing is added.
- Not covered: Codex sessions (no money figure; est. only); sessions started before this was
  installed; the last seconds of a killed session (CC is as of its last status line update, at most
  `refreshInterval` old); another machine until `pa-sync` and that machine's profile declare it; your
  own Claude tabs; split tasks, activities, phases and passes (CC is per session, and per task only
  for a session of that one task). After a model fallback the recorded model is the requested one.

## Profiles

A profile is a folder in `~/.config/herdr-pa/profiles/<name>/` (`profile.json`, `context.md`, `rules.json`,
`hooks/`). `pa-profile set <name>` makes it the machine's default. Profiles live in their own repos.

`profile.json` may name the status line you normally see, so that Claude Code's own cost figure is
recorded around it (see Costs per space): `"statusLine": {"command": ["<your status line command>",
"<its args>"], "refreshInterval": 30}` (`command` a non-empty list of strings, found like the profile's
other commands; `refreshInterval` 1-300 s, default 30). Sessions started by `pa-claude` and
`pa-session` then show that command's output, from that command alone: the profile must name the
status line your Claude command would otherwise set. A malformed key adds nothing and `pa-doctor`
notes it.

The Project Agent's own session asks Claude Code for the one-hour prompt cache: `pa-session` puts
`"promptCacheTtl": "1h"` in its `--settings` (the same one as the status line above, last on the command
line). A Project Agent sits idle between passes and reports for longer than five minutes, so with the
default five-minute cache each turn back rebuilds its whole context. It is a setting, not an environment
variable, so the workers it starts inherit nothing: workers (`pa-claude`) keep Claude Code's default cache,
which costs them less. `"orchestratorPromptCacheTtl"` in `profile.json` chooses: `"1h"` (the default when
the key is absent), `"5m"`, or `"default"` to set nothing and leave it to Claude Code (the off switch).
Any other value sets nothing and `pa-doctor` notes it. `PA_NO_CC_STATUSLINE=1` drops only the status line.
- Environment variables in the launcher's environment come first, in Claude Code's order:
  `FORCE_PROMPT_CACHING_5M=1`, then `CLAUDE_CODE_PROMPT_CACHE_TTL`, then this setting, then
  `ENABLE_PROMPT_CACHING_1H=1`. Needs Claude Code v2.1.242 or later (an earlier one ignores the key).
- Recorded: each `start` line in the space's `session.log` ends `cache=<1h|5m|default> cacheBy=<pa|profile|`
  the variable`|claude-code>` (what the launch asked for), and `pa-doctor` shows what the profile gives and
  the last start's line. Whether the provider gave it shows in the costs: one-hour cache writes (`cw1h`) are counted
  apart from five-minute ones. Some providers offer the one-hour cache for some models only; where the
  provider has none, the session runs with five minutes. A profile for such a provider sets `"5m"` or
  `"default"`.
- Idle compaction: with the one-hour cache, Claude Code can compact a long conversation (200,000 tokens by
  default) after about an hour idle. The Project Agent's `--settings` turns that off (`"idleCompaction":
  false`, see The Project Agent's context).
- Takes effect for Project Agents started (or restarted) after the update.

## The Project Agent's context

Chat history is not state: the Project Agent keeps what matters in its state folder, and since t-041 also
`standing.md` there: your standing words (quoted, with the date and what they apply to), its open asks to
you, the current hold, and a few working notes. Four fixed headings, at most 6,000 characters; `pa-doctor`
shows its size and any problem. Business data only: no secrets, no customer content.

Claude Code compacts a conversation by itself when it fills its window (about 967,000 tokens for a 1M
model), which makes every call before that expensive. For the Project Agent's own session only,
`pa-session` adds:
- `--autocompact 400000` (the net): Claude Code compacts at 400,000 tokens instead. A flag, not an
  environment variable, so the workers it starts get nothing of it. Needs Claude Code v2.1.221 or later.
- In its one `--settings`: `"idleCompaction": false` (v2.1.290 or later) and three hooks, added to your own
  hooks, not replacing them. Before a compaction, `pa-context compact-instructions` tells the summary what to
  keep (open asks, your words, running workers, the next step per task, a hold); after it, `pa-context
  start-pack` gives the new context what to read again, with `standing.md` inlined; `pa-context compacted`
  adds a `compact` record to the space's `costs/sessions.jsonl` (ids and times only; `pa-costs report`
  counts them). The hooks always exit 0, so they never block a compaction.
- The launcher reads `<claude> --version` once (10 s at most) and leaves out what that version does not
  support; when the version is not known it leaves out both, and `session.log` says so. Each `start` line
  in `session.log` ends `net=<tokens|none|off> idle=<off|kept>`.

`context.json` in the state folder holds the settings; no file means the defaults. Set them in a terminal
(not in an agent's session; workers are refused it too):

```sh
pa-context set                    # show the settings
pa-context set --net 450k         # the net, from 100k to 900k
pa-context set --mode off         # the back-out: no net, no hooks, idle compaction as Claude Code has it
pa-context set --mode fresh       # back on (the default)
```

`--task-cap` and `--pass-cap` (defaults 150k and 250k) are the fresh context's caps; they must stay below
the net. The net applies at the Project Agent's next start, or a restart in place; the caps and `--mode`
apply to resets at the launcher's next check.

### Fresh context

Before the net is reached, the launcher starts the Project Agent in a fresh session, in the same terminal:
same address, same labels, its workers keep running. It does so only at a clean point: Claude idle for 20
seconds with nothing waiting, the ledger's checkpoint written clean by this session in its last turn, no
answered card it is still acting on, a handoff file for every live worker, a valid `standing.md`, no
restart request, and no other reset in the last 10 minutes. Then it resets when the context (read from the
session's transcript) is at `taskCap` at a task boundary, or at `passCap` at any clean point. When only the
checkpoint is missing it types one line into the tab (`[launcher] context ...`, at most once an hour).

The new session starts with the usual system prompt (so the current core text) and a start prompt with
`standing.md` inlined; it reads the ledger, the journal and the open cards as after a restart. The tab
prints `[Project Agent] Context reset (<N>k) ... Previous session: <id>`; `agent.json` keeps the last 20
session ids in `previousSessions`, so `/resume` still finds the old conversation. `session.log` gets a
`fresh from=... to=... reason=...` line and `costs/sessions.jsonl` a `reset` record (`pa-costs report`
counts them).

```sh
pa-context fresh                  # a fresh context at the next clean point (waits up to 2 h)
pa-context fresh --dry-run        # the context size now, and what a reset would meet
pa-context fresh --withdraw       # take a pending request back
pa-context followup               # the follow-up report since the mode was turned on (read-only)
```

`--on <machine>` runs `fresh` or `set` on the space's machine. With `--mode off` there are no resets and
no request is taken (`pa-restart --fresh` still gives a new conversation at once).

## Checks

`pa-core-check` fails if `runtime/core`, `runtime/tools` or the mod names a profile's tools. Each
profile lists its own terms in `profile.json` (`"coreCheck": ["..."]`), so this repo holds no such list.
`herdr-app check mods/project-agent` loads the mod once.
`tests/run-all.sh` runs this repo's tests (a Python 3.7 parse check, the unit tests in
`tests/test_*.py`, a keeper harness, a panel render harness and a store harness under Node (the last two
simulate a slow or hung remote machine), and a `pa-session` check
against a private hc core); `--affected` runs only the
fast set. The parts run at the same time (each unit test module is its own part), and each part's whole
output is kept in a run folder that the last line names, after a summary of every part and failing test;
`PA_SERIAL=1` runs the parts one after another, each streamed as it runs. Every run that has parts to
run, full or `--affected`, first takes the same machine slot as `pa-full-suite` (at most `PA_SUITE_CAP`, default
1, at once per user), waiting its turn and saying for whom; its summary says how long it waited. It waits at most 840 s (`PA_SUITE_WAIT_LIMIT`), and stops waiting if its caller
is gone. A single test
module run on its own (`python3 -m unittest tests.test_x`) takes no slot. After updating, run `pa-sync --on <machine>` before installing the mod, so remote launchers
get the new `pa-session` (see the install order above).

## Licence

Apache License 2.0; see `LICENSE` and `NOTICE`.
