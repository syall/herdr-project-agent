#!/bin/sh
# All of this repo's tests. Exits non-zero if any part fails.
#   tests/run-all.sh             full suite: Python 3.7 parse check, unit tests (tests/test_*.py),
#                                keeper harness, panel harness, store harness, and the pa-session integration check
#                                against a private hc core
#   tests/run-all.sh --affected  the fast set only (no integration check). With PA_CHANGED_FILES
#                                (paths separated by spaces or newlines), parts whose files did not
#                                change are skipped; without it, the whole fast set runs.
# The parts run at the same time (they share no temp folder, home, port or hc core); PA_SERIAL=1 runs them
# one after another, each streamed as it runs. Each part's whole output is kept in its own file in one temp
# folder and printed in the order above, then a summary: every part with ok or FAILED, for a failed part its
# failing tests (lines starting FAIL or ERROR), else its last 40 lines. The last line names the folder.
# Exit: 0 all passed, 1 a part failed, 2 a usage error or no temp folder, 130/143 interrupted.
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

# Which parts the changed files touch (only with --affected and PA_CHANGED_FILES).
want_py=1 want_keeper=1 want_panel=1 want_store=1
if [ "$affected" = 1 ] && [ -n "${PA_CHANGED_FILES:-}" ]; then
  want_py=0 want_keeper=0 want_panel=0 want_store=0
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
    case "$f" in
      *mods/project-agent/*|*tests/store/*|*tests/panel/stubs.mjs|*tests/keeper/stubs.mjs|*tests/run-all.sh) want_store=1 ;;
    esac
  done
  [ "$want_py" = 0 ] && [ "$want_keeper" = 0 ] && [ "$want_panel" = 0 ] && [ "$want_store" = 0 ] && { echo "no tested files changed: nothing to run"; exit 0; }
fi

parse37() { "$PY" -c "import ast,sys
for f in sys.argv[1:]: ast.parse(open(f).read(), feature_version=(3,7)); print(f, 'parses as Python 3.7')" runtime/tools/pa.py runtime/tools/pa_hooks.py runtime/tools/pa_guard.py runtime/tools/pa_costs.py runtime/tools/pa_push.py; }
units() { "$PY" -m unittest discover -s tests -p 'test_*.py' -v; }
keeper() { [ -n "$NODE" ] || { echo "no node found (set NODE)"; return 1; }; "$NODE" tests/keeper/run.mjs; }
panel() { [ -n "$NODE" ] || { echo "no node found (set NODE)"; return 1; }; "$NODE" tests/panel/run.mjs; }
store() { [ -n "$NODE" ] || { echo "no node found (set NODE)"; return 1; }; "$NODE" tests/store/run.mjs; }
integration() {
  S=$(mktemp -d /tmp/t001.XXXXXX) || return 1
  sh tests/setup-scratch.sh "$S" && sh tests/pa_session_check.sh "$S"
  code=$?
  rm -rf "$S"
  return $code
}

# The parts to run, in the order they are printed: part_<i> is the function, name_<i> its name.
count=0
add() { count=$((count + 1)); eval "part_$count=\$1; name_$count=\$2"; }
if [ "$want_py" = 1 ]; then add parse37 "python 3.7 parse"; add units "unit tests"; fi
[ "$want_keeper" = 1 ] && add keeper "keeper harness"
[ "$want_panel" = 1 ] && add panel "panel harness"
[ "$want_store" = 1 ] && add store "store harness"
[ "$affected" = 0 ] && add integration "pa-session integration (private hc core)"

tmp=${TMPDIR:-/tmp}
out=$(mktemp -d "${tmp%/}/run-all.XXXXXX") || { echo "run-all.sh: cannot make a temp folder under $tmp" >&2; exit 2; }
log() { eval "echo \"\$out/\$1-\$part_$1.log\""; }
# Print a file whole, ending in a newline.
show() { cat "$1"; [ -s "$1" ] && [ -n "$(tail -c 1 "$1")" ] && echo; return 0; }

# Ctrl-C or TERM: stop every part still running (each is its own process group), then exit.
pids=
stop() {
  for p in $pids; do kill -TERM -- "-$p" 2>/dev/null || kill -TERM "$p" 2>/dev/null; done
  echo "=== interrupted (outputs so far: $out)"
  exit "$1"
}
trap 'stop 130' INT
trap 'stop 143' TERM

i=1
if [ "${PA_SERIAL:-}" = 1 ]; then
  while [ "$i" -le "$count" ]; do
    eval "p=\$part_$i; name=\$name_$i"
    echo "=== $name"
    { "$p" 2>&1; echo $? > "$out/$i.code"; } </dev/null | tee "$(log "$i")"
    code=$(cat "$out/$i.code" 2>/dev/null) || code=1
    eval "code_$i=\${code:-1}"
    [ "$code" = 0 ] && echo "--- $name: ok" || echo "--- $name: FAILED"
    i=$((i + 1))
  done
else
  set -m 2>/dev/null  # each part in its own process group, so stop() can end all of it
  while [ "$i" -le "$count" ]; do
    eval "p=\$part_$i"
    "$p" </dev/null >"$(log "$i")" 2>&1 &
    eval "pid_$i=\$!"
    pids="$pids $!"
    i=$((i + 1))
  done
  set +m 2>/dev/null
  i=1
  while [ "$i" -le "$count" ]; do
    eval "pid=\$pid_$i; name=\$name_$i"
    wait "$pid"
    code=$?
    eval "code_$i=\$code"
    echo "=== $name"
    show "$(log "$i")"
    [ "$code" = 0 ] && echo "--- $name: ok" || echo "--- $name: FAILED"
    i=$((i + 1))
  done
fi
pids=

# The summary: always the last lines (pa-full-suite keeps the tail of this output).
echo "=== summary (each part's whole output: $out)"
failed=0 names= i=1
while [ "$i" -le "$count" ]; do
  eval "name=\$name_$i; code=\$code_$i"
  f=$(log "$i")
  if [ "$code" != 0 ]; then
    failed=$((failed + 1))
    names="${names:+$names, }$name"
    hits=$(grep -E '^(FAIL|ERROR)(:| )' "$f" | cut -c1-200)
    if [ -n "$hits" ]; then
      echo "--- $name: FAILED"
      printf '%s\n' "$hits" | head -n 20 | sed 's/^/  /'
      n=$(printf '%s\n' "$hits" | wc -l | tr -d ' ')
      [ "$n" -gt 20 ] && echo "  ... and $((n - 20)) more in $f"
    else
      echo "--- $name: FAILED (no failing test named; its last 40 lines:)"
      tail -n 40 "$f" | sed 's/^/  /'
    fi
  fi
  i=$((i + 1))
done
i=1
while [ "$i" -le "$count" ]; do
  eval "name=\$name_$i; code=\$code_$i"
  [ "$code" = 0 ] && echo "  $name: ok" || echo "  $name: FAILED"
  i=$((i + 1))
done
[ "$failed" = 0 ] && echo "=== all passed (outputs: $out)" || echo "=== $failed part(s) failed: $names (outputs: $out)"
[ "$failed" = 0 ]
