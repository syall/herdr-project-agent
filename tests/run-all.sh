#!/bin/sh
# All of this repo's tests. Exits non-zero if any part fails.
#   tests/run-all.sh             full suite: Python 3.7 parse check, unit tests (tests/test_*.py),
#                                keeper harness, panel harness, and the pa-session integration check
#                                against a private hc core
#   tests/run-all.sh --affected  the fast set only (no integration check). With PA_CHANGED_FILES
#                                (paths separated by spaces or newlines), parts whose files did not
#                                change are skipped; without it, the whole fast set runs.
# Node: $NODE, else `node` on PATH, else the newest mise install.
set -u
# Never reach the person's hc core from a test: drop every variable that names it.
unset HC_TERMINAL_ID HC_SOCKET HC_TERMINAL_SOCKET HC_SCOPE CLAUDECODE
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/.." && pwd)
cd "$repo" || exit 2
affected=0
case "${1:-}" in --affected) affected=1 ;; "") ;; *) echo "usage: tests/run-all.sh [--affected]" >&2; exit 2 ;; esac

NODE=${NODE:-$(command -v node 2>/dev/null || ls -d "$HOME"/.local/share/mise/installs/node/*/bin/node 2>/dev/null | sort -V | tail -n 1)}
PY=$(command -v python3)
failed=0
part() { name=$1; shift; echo "=== $name"; if "$@"; then echo "--- $name: ok"; else echo "--- $name: FAILED"; failed=$((failed + 1)); fi; }

# Which parts the changed files touch (only with --affected and PA_CHANGED_FILES).
want_py=1 want_keeper=1 want_panel=1
if [ "$affected" = 1 ] && [ -n "${PA_CHANGED_FILES:-}" ]; then
  want_py=0 want_keeper=0 want_panel=0
  for f in $PA_CHANGED_FILES; do
    case "$f" in
      *runtime/tools/*|*tests/test_*.py|*tests/run-all.sh) want_py=1 ;;
    esac
    case "$f" in
      *mods/project-agent/*|*tests/keeper/*|*tests/run-all.sh) want_keeper=1 ;;
    esac
    case "$f" in
      *mods/project-agent/*|*tests/panel/*|*tests/keeper/stubs.mjs|*tests/run-all.sh) want_panel=1 ;;
    esac
  done
  [ "$want_py" = 0 ] && [ "$want_keeper" = 0 ] && [ "$want_panel" = 0 ] && { echo "no tested files changed: nothing to run"; exit 0; }
fi

parse37() { "$PY" -c "import ast,sys
for f in sys.argv[1:]: ast.parse(open(f).read(), feature_version=(3,7)); print(f, 'parses as Python 3.7')" runtime/tools/pa.py runtime/tools/pa_hooks.py runtime/tools/pa_guard.py; }
units() { "$PY" -m unittest discover -s tests -p 'test_*.py' -v; }
keeper() { [ -n "$NODE" ] || { echo "no node found (set NODE)"; return 1; }; "$NODE" tests/keeper/run.mjs; }
panel() { [ -n "$NODE" ] || { echo "no node found (set NODE)"; return 1; }; "$NODE" tests/panel/run.mjs; }
integration() {
  S=$(mktemp -d /tmp/t001.XXXXXX) || return 1
  sh tests/setup-scratch.sh "$S" && sh tests/pa_session_check.sh "$S"
  code=$?
  rm -rf "$S"
  return $code
}

if [ "$want_py" = 1 ]; then part "python 3.7 parse" parse37; part "unit tests" units; fi
[ "$want_keeper" = 1 ] && part "keeper harness" keeper
[ "$want_panel" = 1 ] && part "panel harness" panel
[ "$affected" = 0 ] && part "pa-session integration (private hc core)" integration

[ "$failed" = 0 ] && echo "=== all passed" || echo "=== $failed part(s) failed"
[ "$failed" = 0 ]
