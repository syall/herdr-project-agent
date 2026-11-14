#!/bin/sh
# affected-tests hook: see tools/pa_hooks.py. Installed into a worktree by `pa-hooks install`.
exec "$HOME/.local/bin/pa-hook" affected-tests "$@"
