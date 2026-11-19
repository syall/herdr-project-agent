#!/bin/sh
# Builds a private Project Agent home for tests under $1 (made by the caller with mktemp -d):
#   $1/pa       HERDR_PA_HOME: core/, profiles/test/profile.json, config.json
#   $1/bin      pa-session, pa-start, pa-restart (links to this worktree's pa.py), stub-claude, herdr-app (a fake)
#   $1/pa/tools a copy of this worktree's runtime/tools, with installed.json (the exec target of a restart)
#   $1/state, $1/config, $1/run, $1/app, $1/claude   scratch XDG / app / Claude homes
# Never touches the person's ~/.config, ~/.claude or live Herdr.
set -eu
S=${1:?usage: setup-scratch.sh <scratch dir>}
case "$S" in /tmp/*|/private/tmp/*|/var/folders/*) ;; *) echo "refusing: $S is not a temp folder" >&2; exit 2 ;; esac
# Once resolved too (t-112 §10): /tmp/../etc, or a /tmp link to another folder, is not a temp folder.
real_s=$(cd "$S" 2>/dev/null && pwd -P) || { echo "refusing: $S is not a temp folder (no such folder)" >&2; exit 2; }
case "$real_s" in /tmp/*|/private/tmp/*|/var/folders/*|/private/var/folders/*) ;; *) echo "refusing: $S is not a temp folder (resolved: $real_s)" >&2; exit 2 ;; esac
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/.." && pwd)
mkdir -p "$S/pa/profiles/test" "$S/bin" "$S/state" "$S/config" "$S/run" "$S/app" "$S/claude" "$S/proj"
rm -rf "$S/pa/core"
cp -R "$repo/runtime/core" "$S/pa/core"
printf '{"name":"test","claude":["%s/bin/stub-claude"],"codex":["true"],"systemPromptSnapshot":"keep"}\n' "$S" > "$S/pa/profiles/test/profile.json"
printf '{"defaultProfile":"test"}\n' > "$S/pa/config.json"
ln -sf "$repo/runtime/tools/pa.py" "$S/bin/pa-session"
ln -sf "$repo/runtime/tools/pa.py" "$S/bin/pa-start"
ln -sf "$repo/runtime/tools/pa.py" "$S/bin/pa-restart"
# The installed tools a restart execs (t-037 J2): this worktree's, stamped as install.sh does.
rm -rf "$S/pa/tools"
cp -R "$repo/runtime/tools" "$S/pa/tools"
rm -rf "$S/pa/tools/__pycache__"
HERDR_PA_HOME="$S/pa" HERDR_APP_HOME="$S/app" python3 "$S/pa/tools/pa.py" stamp >/dev/null
cp "$here/stub-claude" "$S/bin/stub-claude"
chmod +x "$S/bin/stub-claude"
# A stand-in herdr-app (t-037 J3 S13): pa-start in the scratch never reaches a real app.
cp "$here/fake-herdr-app" "$S/bin/herdr-app"
chmod +x "$S/bin/herdr-app"
echo "scratch ready: $S"
