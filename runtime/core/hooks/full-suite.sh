#!/bin/sh
# Full test suite, once, at the end of the Test phase: see tools/pa_hooks.py.
exec "$HOME/.local/bin/pa-full-suite" "$@"
