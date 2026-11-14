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

The mod keeps shared state `project-agent.tabs` (tab id -> `{ space, project, profile, name, terminal }`).
It cannot draw or guard controls that other mods own, so those mods read that key:

- the tab bar: no close or pin control on a listed tab; the close-tab shortcut refuses it;
- the tab and terminal right-click menus: no "Close" / "Close pane" for it;
- a pane header: no close button on the pane whose terminal is the record's `terminal`.

New spaces start the launcher with `space.new { argv: ["pa-session", "--ask"] }`.

## Profiles

A profile is a folder in `~/.config/herdr-pa/profiles/<name>/` (`profile.json`, `context.md`, `rules.json`,
`hooks/`). `pa-profile set <name>` makes it the machine's default. Profiles live in their own repos.

## Checks

`pa-core-check` fails if `runtime/core`, `runtime/tools` or the mod names a profile's tools. Each
profile lists its own terms in `profile.json` (`"coreCheck": ["..."]`), so this repo holds no such list.
`herdr-app check mods/project-agent` loads the mod once.

## Licence

Apache License 2.0; see `LICENSE` and `NOTICE`.
