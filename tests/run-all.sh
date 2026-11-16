#!/bin/sh
# All of this repo's tests. Exits non-zero if any part fails.
#   tests/run-all.sh             full suite: Python 3.7 parse check, pa.py unit tests, keeper harness,
#                                and the pa-session integration check against a private hc core
#   tests/run-all.sh --affected  the fast set only (no integration check). With PA_CHANGED_FILES
#                                (paths separated by spaces or newlines), parts whose files did not
#                                change are skipped; without it, the whole fast set runs.
# Node: $NODE, else `node` on PATH, else the newest mise install.
set -u
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
want_py=1 want_keeper=1
if [ "$affected" = 1 ] && [ -n "${PA_CHANGED_FILES:-}" ]; then
  want_py=0 want_keeper=0
  for f in $PA_CHANGED_FILES; do
    case "$f" in
      *runtime/tools/*|*tests/test_pa_session.py|*tests/run-all.sh) want_py=1 ;;
    esac
    case "$f" in
      *mods/project-agent/*|*tests/keeper/*|*tests/run-all.sh) want_keeper=1 ;;
    esac
  done
  [ "$want_py" = 0 ] && [ "$want_keeper" = 0 ] && { echo "no tested files changed: nothing to run"; exit 0; }
fi

parse37() { "$PY" -c "import ast,sys; ast.parse(open(sys.argv[1]).read(), feature_version=(3,7)); print('pa.py parses as Python 3.7')" runtime/tools/pa.py; }
units() { env -u HC_TERMINAL_ID "$PY" -m unittest -v tests/test_pa_session.py; }
keeper() { [ -n "$NODE" ] || { echo "no node found (set NODE)"; return 1; }; "$NODE" tests/keeper/run.mjs; }
integration() {
  S=$(mktemp -d /tmp/t001.XXXXXX) || return 1
  sh tests/setup-scratch.sh "$S" && sh tests/pa_session_check.sh "$S"
  code=$?
  rm -rf "$S"
  return $code
}

if [ "$want_py" = 1 ]; then part "python 3.7 parse" parse37; part "pa.py unit tests" units; fi
[ "$want_keeper" = 1 ] && part "keeper harness" keeper
[ "$affected" = 0 ] && part "pa-session integration (private hc core)" integration

[ "$failed" = 0 ] && echo "=== all passed" || echo "=== $failed part(s) failed"
[ "$failed" = 0 ]
