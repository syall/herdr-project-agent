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
plus `recovering` while the keeper restores that tab). It cannot draw or guard controls that other mods
own, so those mods read that key:

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
- `pa-session` and `pa-start` refuse to start a second Project Agent only while another terminal's
  launcher is alive; a labelled terminal whose heartbeat stopped is replaced.

## Profiles

A profile is a folder in `~/.config/herdr-pa/profiles/<name>/` (`profile.json`, `context.md`, `rules.json`,
`hooks/`). `pa-profile set <name>` makes it the machine's default. Profiles live in their own repos.

## Checks

`pa-core-check` fails if `runtime/core`, `runtime/tools` or the mod names a profile's tools. Each
profile lists its own terms in `profile.json` (`"coreCheck": ["..."]`), so this repo holds no such list.
`herdr-app check mods/project-agent` loads the mod once.
`tests/run-all.sh` runs this repo's tests (a Python 3.7 parse check, `pa.py` unit tests, a keeper
harness under Node, and a `pa-session` check against a private hc core); `--affected` runs only the
fast set. After updating, run `pa-sync --on <machine>` before installing the mod, so remote launchers
get the new `pa-session` (see the install order above).

## Licence

Apache License 2.0; see `LICENSE` and `NOTICE`.
