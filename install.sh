#!/bin/sh
# Installs the Project Agent: the Herdr mod, and its runtime (prompts, hooks, commands,
# the personal profile) into ~/.config/herdr-pa. Safe to run again; it only copies files.
#   ./install.sh            install on this machine
#   ./install.sh --check    list installed files that differ from this repo
#   ./install.sh --on NAME  after installing here, copy the runtime to another Herdr machine
set -e
here=$(cd "$(dirname "$0")" && pwd)
APP=${HERDR_APP_HOME:-$HOME/.config/herdr-app}
PA=${HERDR_PA_HOME:-$HOME/.config/herdr-pa}
check=0; [ "$1" = "--check" ] && check=1 && shift
drift=0
# copy <folder in this repo> <destination>: installs every file, deletes nothing.
# With --check it only reports files that differ from what is installed.
copy() {
  (cd "$here/$1" && find . -type f ! -name .DS_Store) | while read -r f; do
    src="$here/$1/$f"; dst="$2/${f#./}"
    if cmp -s "$src" "$dst"; then continue; fi
    if [ "$check" = 1 ]; then echo "differs: $dst"; else mkdir -p "$(dirname "$dst")"; cp -p "$src" "$dst"; echo "installed $dst"; fi
  done
}

copy runtime "$PA"
[ -d "$APP/mods" ] && copy mods "$APP/mods"
[ "$check" = 1 ] && exit 0
chmod +x "$PA"/tools/*.py "$PA"/core/hooks/*.sh
mkdir -p "$PA/spaces" "$HOME/.local/bin"
[ -f "$PA/config.json" ] || printf '{\n  "version": 1,\n  "defaultProfile": "personal"\n}\n' > "$PA/config.json"
for n in pa-start pa-session pa-claude pa-codex pa-profile pa-peers pa-pass pa-cleanup pa-doctor pa-task pa-decide pa-sync pa-core-check pa-stamp pa-restart pa-context; do ln -sfn "$PA/tools/pa.py" "$HOME/.local/bin/$n"; done
for n in pa-gate pa-hooks pa-hook pa-full-suite pa-worktree pa-worker pa-trust; do ln -sfn "$PA/tools/pa_hooks.py" "$HOME/.local/bin/$n"; done
for n in pa-costs; do ln -sfn "$PA/tools/pa_costs.py" "$HOME/.local/bin/$n"; done
for n in pa-queue; do ln -sfn "$PA/tools/pa_queue.py" "$HOME/.local/bin/$n"; done
for n in pa-push-main pa-push-allow; do ln -sfn "$PA/tools/pa_push.py" "$HOME/.local/bin/$n"; done
"$HOME/.local/bin/pa-core-check"
# Last: installed.json, the hashes of what is installed now, and the commit it came from (t-037).
# A launcher compares what it loaded with it ("older code" in pa-peers and pa-doctor).
if commit=$(git -C "$here" rev-parse HEAD 2>/dev/null); then
  dirty=; [ -n "$(git -C "$here" status --porcelain 2>/dev/null)" ] && dirty=--dirty
  "$HOME/.local/bin/pa-stamp" --commit "$commit" $dirty
else
  "$HOME/.local/bin/pa-stamp"
fi
[ "$1" = "--on" ] && [ -n "$2" ] && "$HOME/.local/bin/pa-sync" --on "$2"
echo "Project Agent installed. Add the mod to a layout (see RECIPE.md, Built from)."
