#!/bin/sh
# All of this repo's tests. Exits non-zero if any part fails.
#   tests/run-all.sh             full suite: Python 3.7 parse check, unit tests (each tests/test_*.py its own part;
#                                the long ones in unit_shards below are several parts, each case in exactly one),
#                                keeper harness, panel harness (four parts: run.mjs --shard=1/4 .. 4/4, each case in
#                                exactly one), store harness, and the pa-session integration check: three
#                                integration parts (pa_session_check.sh --part 1..3), each with its own private hc
#                                core and scratch folder, each step in exactly one part (t-112)
#   tests/run-all.sh --affected  the fast set only (no integration check). With PA_CHANGED_FILES
#                                (paths separated by spaces or newlines), parts whose files did not
#                                change are skipped; without it, the whole fast set runs. A mods/* or
#                                tests/panel/* change also runs tests/test_panel_shards.py and
#                                tests/test_mod_imports.py (not every unit module) (t-065)
#   tests/run-all.sh --module test_x  one unit test module (tests/test_x.py) as one part: the same slot, limit,
#                                progress file and summary as in a full run (pa-full-suite --module runs it)
#   tests/run-all.sh --except-integration  every part of the full run but the integration check (same order, names,
#                                shards, slot, progress); PA_CHANGED_FILES is not read. pa-full-suite runs it on a
#                                configured machine (t-089)
#   tests/run-all.sh --only-integration  the integration check alone: its three parts (same slot, progress, summary):
#                                the part pa-full-suite keeps on this Mac while the rest runs on a machine
# The parts run at the same time (they share no temp folder, home, port or hc core); PA_SERIAL=1 runs them
# one after another, each streamed as it runs. Each part's whole output is kept in its own file in one temp
# folder and printed in the order above, then a summary: for a failed part its failing tests (lines starting FAIL or
# ERROR), else its last 40 lines; then the line `=== parts` and every part with ok or FAILED and its seconds (t-103:
# pa-full-suite's record reads its parts after that line only). The last line names the folder.
# Each part's TMPDIR is its own folder in there (<n>.tmp), so no part sees another's temp files, nor another
# suite's running beside it.
# The suite slot (t-072): a run with parts to run takes pa-full-suite's slot first (runtime/tools/pa_suite.py: an
# flock under HERDR_PA_HOME/suite-slots, at most PA_SUITE_CAP runs at once), waiting its turn and saying for whom;
# a dead holder never blocks, and a run whose caller holds the slot (pa-full-suite) does not wait for itself. The
# summary's first line and the run folder's file `slot` say how long it waited. A run that selects no part takes
# no slot; nor do a few tests run on their own (python3 -m unittest tests.test_x.T.test_y).
# On a machine (t-107 J2): with machines in HERDR_PA_HOME/config.json's suiteMachines, a --affected or --module run
# by hand or by a hook (no caller's slot, not on a machine, not placed here by pa-run) is run there by the slot
# step, through pa-run's code: its first line `run-all.sh: on <machine> (<os>)`, then this script's output from
# there (parts, summary); 124/125 end with `run-all.sh: not run on <machine>: <why>`, or, for a run that started and
# did not end (the deadline or the machine's limit during it), `run-all.sh: did not finish on <machine>: <why>` (the
# hook reads that as a failure to fix). PA_SUITE_DEADLINE (an epoch second, the affected-tests hook's) ends the wait
# and the run before it.
# Progress and the part limit (t-076): pa_suite.py, holding the slot, watches the run folder (the files `parts`,
# <i>.pid, <i>.start, <i>.end, <i>.code): it keeps the run's progress file under HERDR_PA_HOME/suite-runs
# (pa-full-suite --status reads it) and stops a part past max(PA_PART_LIMIT or 900 s, 3x its recorded median),
# writing <i>.hung; the summary names it HUNG, the other parts finish, and the run exits 1.
# The run's own limit (t-079 I2): with PA_SUITE_RUN_LIMIT (the affected-tests hook's), the slot step ends a run
# here still running that long after it took the slot: `run-all.sh: did not finish on this Mac: ...`, 124; also
# at PA_SUITE_DEADLINE when that comes first (t-147: after falling back to this Mac, say).
# A killed slot step (t-079 I3): under the slot step, a watchdog checks every 2 s that this script's parent is still
# the step; when it is not (the step was killed, even with SIGKILL, and the slot is free again), every part is
# stopped as for TERM and the run says so (exit 143).
# Exit: 0 all passed, 1 a part failed, 2 a usage error or no temp folder, 124: no slot came free, the deadline
# (PA_SUITE_DEADLINE, before or during a run), the machine lost, the run there ended without an exit code
# (killed), the machine's REMOTE_TIMEOUT, or the run limit
# (PA_SUITE_RUN_LIMIT) after it took the slot; 125 not run on a machine (refused, not reachable with suiteFallback
# "none", no usable machine in suiteMachines with "none", the slot step failed),
# 130/143/129 interrupt, terminate, hangup.
# Node: $NODE, else `node` on PATH, else the newest mise install.
set -u
worker=${HC_TERMINAL_ID:-}  # named to a run waiting for the slot (read before it is dropped below)
# t-107 R13: the slot step (which may run this on a machine through hc) gets them as PA_SUITE_HC_<NAME>, exported
# only on its exec line below; these shell variables are never exported, so no part sees them.
for v in TERMINAL_ID SOCKET TERMINAL_SOCKET SCOPE; do
  eval "if [ -n \"\${HC_$v+x}\" ]; then keep_hc_$v=\$HC_$v; else unset keep_hc_$v; fi"
done
# Never reach the person's hc core from a test: drop every variable that names it.
unset HC_TERMINAL_ID HC_SOCKET HC_TERMINAL_SOCKET HC_SCOPE CLAUDECODE
unset PA_SUITE_HC_TERMINAL_ID PA_SUITE_HC_SOCKET PA_SUITE_HC_TERMINAL_SOCKET PA_SUITE_HC_SCOPE  # never inherited
unset PA_UNIT_SHARD  # set below for one shard part only (t-078), never inherited
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/.." && pwd)
cd "$repo" || exit 2
affected=0 module= place=
usage() { echo "usage: tests/run-all.sh [--affected | --module test_x | --except-integration | --only-integration]" >&2; exit 2; }
case "${1:-}" in
  --affected) [ $# = 1 ] || usage; affected=1 ;;
  --except-integration|--only-integration) [ $# = 1 ] || usage; place=${1#--} ;;
  --module) [ $# = 2 ] || usage; module=$2
    case "$module" in test_*/*|test_*.*) usage ;; test_?*) [ -f "tests/$module.py" ] || usage ;; *) usage ;; esac ;;
  "") ;;
  *) usage ;;
esac

NODE=${NODE:-$(command -v node 2>/dev/null || ls -d "$HOME"/.local/share/mise/installs/node/*/bin/node 2>/dev/null | sort -V | tail -n 1)}
PY=$(command -v python3)

# Which parts the changed files touch (only with --affected and PA_CHANGED_FILES).
want_py=1 want_keeper=1 want_panel=1 want_store=1
py_some=  # t-065: with want_py=0, these unit modules only (each read files that are not Python)
if [ "$affected" = 1 ] && [ -n "${PA_CHANGED_FILES:-}" ]; then
  want_py=0 want_keeper=0 want_panel=0 want_store=0
  for f in $PA_CHANGED_FILES; do
    case "$f" in
      *runtime/tools/*|*tests/*.py|*tests/run-all.sh) want_py=1 ;;  # the test helpers too (queue_fixtures.py ...)
    esac
    case "$f" in  # t-060 RL-3, t-069: they read run.mjs, the gpui stand-ins and the mod's imports
      *mods/*|*tests/panel/*) py_some="test_panel_shards test_mod_imports" ;;
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
  [ "$want_py" = 0 ] && [ -z "$py_some" ] && [ "$want_keeper" = 0 ] && [ "$want_panel" = 0 ] && [ "$want_store" = 0 ] && { echo "no tested files changed: nothing to run"; exit 0; }
fi

parse37() { "$PY" -c "import ast,sys
for f in sys.argv[1:]: ast.parse(open(f).read(), feature_version=(3,7)); print(f, 'parses as Python 3.7')" runtime/tools/pa.py runtime/tools/pa_hooks.py runtime/tools/pa_guard.py runtime/tools/pa_costs.py runtime/tools/pa_push.py runtime/tools/pa_queue.py runtime/tools/pa_places.py runtime/tools/pa_statusline.py runtime/tools/pa_handoff.py; }
units() { "$PY" -m unittest discover -s tests -p "$1.py" -v; }  # one module: the same loader and top folder
# The long unit modules run as several parts (t-078): <module>:<n> runs the module n times at once, the k-th with
# PA_UNIT_SHARD=k/n, which its load_tests (tests/unit_shards.py) turns into every n-th test from the k-th, so each
# test runs in exactly one part and a test added later needs no edit here. A part's argument is <module>@<k>.
unit_shards="test_pa_queue:4 test_pa_queue_merge:4 test_pa_suite_remote:2 test_pa_worker:2"
shards_of() { for s in $unit_shards; do [ "${s%%:*}" = "$1" ] && { echo "${s#*:}"; return 0; }; done; echo 1; }
shard() { sm=${1%@*} sk=${1##*@}; PA_UNIT_SHARD="$sk/$(shards_of "$sm")" "$PY" -m unittest discover -s tests -p "$sm.py" -v; }
keeper() { [ -n "$NODE" ] || { echo "no node found (set NODE)"; return 1; }; "$NODE" tests/keeper/run.mjs; }
# The panel harness is one part per shard (t-031): its time is nearly all short real timers, not CPU, so its
# shards running at once divide its wall time. $1 is the shard, 1..panel_parts.
panel_parts=4
panel() { [ -n "$NODE" ] || { echo "no node found (set NODE)"; return 1; }; "$NODE" tests/panel/run.mjs "--shard=$1/$panel_parts"; }
store() { [ -n "$NODE" ] || { echo "no node found (set NODE)"; return 1; }; "$NODE" tests/store/run.mjs; }
# The integration check is three parts at once (t-112): $1 is the part, 1..integration_parts, each with its own
# scratch folder and so its own private hc core.
integration_parts=3
integration() {
  S=$(mktemp -d /tmp/t001.XXXXXX) || return 1
  sh tests/setup-scratch.sh "$S" && sh tests/pa_session_check.sh "$S" --part "$1"
  code=$?
  rm -rf "$S"
  return $code
}

# The parts to run, in the order they are printed: part_<i> is the function, name_<i> its name, arg_<i> its
# argument. Each unit test module is its own part (they share no state: each makes its own temp folder and
# homes), so they run at once; every tests/test_*.py, as discover found them, so each test runs once; a module in
# unit_shards is one part per shard.
count=0
add() { count=$((count + 1)); eval "part_$count=\$1; name_$count=\$2; arg_$count=\${3:-}"; }
if [ -n "$module" ]; then
  want_py=0 want_keeper=0 want_panel=0 want_store=0 affected=1
  add units "unit tests: $module" "$module"
fi
if [ "$place" = only-integration ]; then want_py=0 want_keeper=0 want_panel=0 want_store=0; fi
if [ "$want_py" = 1 ]; then
  add parse37 "python 3.7 parse"
  for f in tests/test_*.py; do
    [ -f "$f" ] || continue
    m=${f#tests/}; m=${m%.py}
    n=$(shards_of "$m")
    if [ "$n" = 1 ]; then add units "unit tests: $m" "$m"; continue; fi
    k=1
    while [ "$k" -le "$n" ]; do add shard "unit tests: $m $k/$n" "$m@$k"; k=$((k + 1)); done
  done
elif [ -n "$py_some" ] && [ -z "$module" ] && [ "$place" != only-integration ]; then
  for m in $py_some; do [ -f "tests/$m.py" ] && add units "unit tests: $m" "$m"; done
fi
[ "$want_keeper" = 1 ] && add keeper "keeper harness"
if [ "$want_panel" = 1 ]; then
  k=1
  while [ "$k" -le "$panel_parts" ]; do add panel "panel harness $k/$panel_parts" "$k"; k=$((k + 1)); done
fi
[ "$want_store" = 1 ] && add store "store harness"
if [ "$affected" = 0 ] && [ "$place" != except-integration ]; then
  k=1
  while [ "$k" -le "$integration_parts" ]; do add integration "pa-session integration $k/$integration_parts (private hc core)" "$k"; k=$((k + 1)); done
fi

# Take the slot: pa_suite.py runs this script again under it, with PA_SUITE_SLOT_PID set to its own pid (this
# script's parent then) and PA_SUITE_HOLD saying how it got the slot.
helper=$repo/runtime/tools/pa_suite.py
if [ "${PA_SUITE_SLOT_PID:-}" != "$PPID" ]; then
  if [ -n "$PY" ] && [ -f "$helper" ]; then
    for v in TERMINAL_ID SOCKET TERMINAL_SOCKET SCOPE; do
      eval "if [ -n \"\${keep_hc_$v+x}\" ]; then PA_SUITE_HC_$v=\$keep_hc_$v; export PA_SUITE_HC_$v; fi"
    done
    exec "$PY" "$helper" slot --for "tests/run-all.sh${*:+ $*}" --worker "$worker" --dir "$repo" -- \
      sh "$here/$(basename "$0")" "$@"
  fi
  PA_SUITE_HOLD="none (no $helper: this run took no slot)"
fi
hold=${PA_SUITE_HOLD:-unknown}

tmp=${TMPDIR:-/tmp}
out=$(mktemp -d "${tmp%/}/run-all.XXXXXX") || { echo "run-all.sh: cannot make a temp folder under $tmp" >&2; exit 2; }
printf '%s\n' "$hold" > "$out/slot"
# The real git (t-093, t-062 F1): on a Mac /usr/bin/git is xcrun's front end, ~4x the CPU of the git it finds on
# each of the suite's many git calls. Asked once here, xcrun's <prefix>/bin/git goes first on PATH for the parts
# as $out/git/bin/git, a link named git in a folder of its own (its prefix's bin also holds a python3, make, cc ...
# that must not shadow the caller's), beside links to the prefix's other folders (git finds its helpers and
# templates beside the path it was started by). Without xcrun, or when it names no <prefix>/bin/git, PATH is as it was.
real=
command -v xcrun >/dev/null 2>&1 && real=$(xcrun --find git 2>/dev/null)
pre=${real%/bin/git}
if [ -n "$pre" ] && [ "$pre/bin/git" = "$real" ] && [ -x "$real" ] && mkdir -p "$out/git/bin"; then
  for d in "$pre"/*; do [ "${d##*/}" = bin ] || ln -s "$d" "$out/git/${d##*/}"; done
  ln -s "$real" "$out/git/bin/git" && PATH="$out/git/bin:$PATH" && export PATH
fi
log() { eval "echo \"\$out/\$1-\$part_$1\${arg_$1:+-\$arg_$1}.log\""; }
# The parts for pa_suite.py's watcher (t-076): one line each, <i> TAB <name> TAB <log>; then the run folder is
# named in the file PA_SUITE_WATCH (made whole, then moved), and the watcher's variables are not the parts'.
i=1
while [ "$i" -le "$count" ]; do eval "printf '%s\t%s\t%s\n' \"\$i\" \"\$name_$i\" \"\$(log \$i)\""; i=$((i + 1)); done > "$out/parts"
if [ -n "${PA_SUITE_WATCH:-}" ]; then
  printf '%s\n' "$out" > "$PA_SUITE_WATCH.new" && mv "$PA_SUITE_WATCH.new" "$PA_SUITE_WATCH"
fi
unset PA_SUITE_WATCH PA_SUITE_PROGRESS
# t-117 R16: the slot step's own (its deadline, pa-run's local mark, its run limit) are never the parts': the first
# pass above passed them to the step; here, before any part starts, they go
unset PA_SUITE_DEADLINE PA_SUITE_LOCAL_RUN PA_SUITE_RUN_LIMIT
# Print a file whole, ending in a newline.
show() { cat "$1"; [ -s "$1" ] && [ -n "$(tail -c 1 "$1")" ] && echo; return 0; }
# Run part $1 (function $2) with its own TMPDIR in the run folder: a test that looks for its own leftovers in
# the temp folder then sees no other part's, nor those of another suite running beside this one.
# Its start and end (whole seconds) go beside its log, for its time in the summary (t-056; parts run at once, so
# a part's own time, not how long it was waited on).
# Its exit code goes there too, as <i>.code, before <i>.end (pa_suite.py's watcher reads .code once .end is there),
# and the run reads it from that file, not from `wait` (t-113): on a Linux machine whose sh is bash, `wait <pid>`
# answered "pid N is not a child of this shell" (127) for parts that had passed, and they were marked FAILED.
# A wait that answers while the part still runs (t-079 I4) is followed by waiting for its .end (or .hung, or its
# pid gone). The run writes <i>.code itself only when the part wrote none, whole (setcode), and never after .end
# with another text (t-079 I6).
setcode() { echo "$2" > "$out/$1.code.new" && mv "$out/$1.code.new" "$out/$1.code"; }
# Until part $1 (pid $2) wrote its .end, was stopped as hung, or is gone.
untilend() {
  while [ ! -f "$out/$1.end" ] && [ ! -f "$out/$1.hung" ] && kill -0 "$2" 2>/dev/null; do sleep 1; done
}
# Until part $1 (pid $2) wrote its .end or .hung, or its process is gone or a zombie, by polling (t-165): a trapped
# signal that comes as `wait <pid>` starts can go unseen until that child ends (bash 4.2: 8 of 3000 under load),
# and a hung part never ends, so the run would not stop. A signal is taken after the poll's next command (<= 0.2 s).
untilout() {
  while [ ! -f "$out/$1.end" ] && [ ! -f "$out/$1.hung" ] && kill -0 "$2" 2>/dev/null; do
    case $(ps -o stat= -p "$2" 2>/dev/null) in *Z*) return 0 ;; esac
    sleep 0.2
  done
}
inpart() {
  date +%s > "$out/$1.start"
  TMPDIR="$out/$1.tmp"; export TMPDIR; mkdir "$TMPDIR" || { echo 1 > "$out/$1.code"; date +%s > "$out/$1.end"; return 1; }
  "$2" "$3"; c=$?
  echo "$c" > "$out/$1.code"
  date +%s > "$out/$1.end"
  return $c
}
# A part's seconds, from those files (0 when either is missing).
took() { s=$(cat "$out/$1.start" 2>/dev/null) e=$(cat "$out/$1.end" 2>/dev/null); echo $(( ${e:-0} - ${s:-${e:-0}} )); }

# Ctrl-C, TERM or a hangup (the pane is closed): stop every part still running (each is its own process group,
# in PA_SERIAL=1 mode too), then exit 130, 143 or 129.
pids= watchdog= step=
stop() {
  [ -f "$out/orphan" ] && echo "run-all.sh: the slot step (pid $step) is gone: stopping the parts (the slot is free"\
    "again, so this run must not go on beside another)"
  [ -n "$watchdog" ] && kill "$watchdog" 2>/dev/null
  for p in $pids; do kill -TERM -- "-$p" 2>/dev/null || kill -TERM "$p" 2>/dev/null; done
  echo "=== interrupted (outputs so far: $out)"
  exit "$1"
}
trap 'stop 130' INT
trap 'stop 143' TERM
trap 'stop 129' HUP
# The watchdog (t-079 I3, t-072 RL-1): only under the slot step (this script's parent). It reads this script's
# parent pid (ps), so a reused pid cannot fool it: not the step any more -> <out>/orphan, then TERM to this script.
# Its output goes nowhere (it never holds the caller's pipe open); it is ended before the summary.
if [ "${PA_SUITE_SLOT_PID:-}" = "$PPID" ]; then
  step=$PPID main=$$
  (
    trap - INT TERM HUP
    while sleep 2; do
      now=$(ps -o ppid= -p "$main" 2>/dev/null | tr -d ' ')
      [ -n "$now" ] || exit 0
      [ "$now" = "$step" ] && continue
      : > "$out/orphan"
      kill -TERM "$main" 2>/dev/null
      exit 0
    done
  ) </dev/null >/dev/null 2>&1 &
  watchdog=$!
fi

i=1
if [ "${PA_SERIAL:-}" = 1 ]; then
  while [ "$i" -le "$count" ]; do
    eval "p=\$part_$i; a=\$arg_$i; name=\$name_$i"
    echo "=== $name"
    # In the background in its own process group, and waited for: a signal then runs the trap at once (it
    # would wait for a foreground pipeline to end), and stop() can end the part, tee and its children.
    set -m 2>/dev/null
    ( { (inpart "$i" "$p" "$a") 2>&1; c=$?; [ -s "$out/$i.code" ] || echo "$c" > "$out/$i.code"; } </dev/null |
      tee "$(log "$i")" ) &
    pids=$!
    echo "$pids" > "$out/$i.pid"
    set +m 2>/dev/null
    untilout "$i" "$pids"
    wait "$pids"
    [ -s "$out/$i.code" ] || { untilend "$i" "$pids"; wait "$pids" 2>/dev/null; }
    pids=
    code=$(cat "$out/$i.code" 2>/dev/null)
    [ -n "$code" ] || { code=1; setcode "$i" "$code"; }
    [ -f "$out/$i.end" ] || date +%s > "$out/$i.end"  # a part stopped as hung wrote none
    eval "code_$i=\$code"
    [ "$code" = 0 ] && echo "--- $name: ok" || echo "--- $name: FAILED"
    i=$((i + 1))
  done
else
  set -m 2>/dev/null  # each part in its own process group, so stop() can end all of it
  while [ "$i" -le "$count" ]; do
    eval "p=\$part_$i; a=\$arg_$i"
    inpart "$i" "$p" "$a" </dev/null >"$(log "$i")" 2>&1 &
    eval "pid_$i=\$!"
    echo "$!" > "$out/$i.pid"
    pids="$pids $!"
    i=$((i + 1))
  done
  set +m 2>/dev/null
  i=1
  while [ "$i" -le "$count" ]; do
    eval "pid=\$pid_$i; name=\$name_$i"
    # wait only reaps the part and waits for it to end; its code is the one it wrote. No file: it was stopped
    # (hung, or a signal) before it wrote one, so wait's status when that is not 0, else 1.
    untilout "$i" "$pid"
    wait "$pid"
    code=$?
    [ -s "$out/$i.code" ] || untilend "$i" "$pid"
    w=$(cat "$out/$i.code" 2>/dev/null) && [ -n "$w" ] && code=$w || { [ "$code" = 0 ] && code=1; setcode "$i" "$code"; }
    [ -f "$out/$i.end" ] || date +%s > "$out/$i.end"
    eval "code_$i=\$code"
    echo "=== $name"
    show "$(log "$i")"
    [ "$code" = 0 ] && echo "--- $name: ok" || echo "--- $name: FAILED"
    i=$((i + 1))
  done
fi
pids=
[ -n "$watchdog" ] && kill "$watchdog" 2>/dev/null
watchdog=

# The summary: always the last lines (pa-full-suite keeps the tail of this output).
echo "=== summary (each part's whole output: $out)"
echo "  suite slot: $hold"
failed=0 names= i=1
while [ "$i" -le "$count" ]; do
  eval "name=\$name_$i; code=\$code_$i"
  f=$(log "$i")
  if [ "$code" != 0 ] && [ -f "$out/$i.hung" ]; then
    failed=$((failed + 1))
    names="${names:+$names, }$name (hung)"
    echo "--- $name: HUNG: $(head -n 1 "$out/$i.hung") (its last 40 lines:)"
    tail -n 40 "$f" | sed 's/^/  /'
  elif [ "$code" != 0 ]; then
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
echo "=== parts"  # t-103 R14: the part list follows (pa_suite.parse_parts reads only the lines after it)
i=1
while [ "$i" -le "$count" ]; do
  eval "name=\$name_$i; code=\$code_$i"
  if [ "$code" = 0 ]; then echo "  $name: ok ($(took "$i")s)"
  elif [ -f "$out/$i.hung" ]; then echo "  $name: HUNG ($(took "$i")s)"
  else echo "  $name: FAILED ($(took "$i")s)"; fi
  i=$((i + 1))
done
[ "$failed" = 0 ] && echo "=== all passed (outputs: $out)" || echo "=== $failed part(s) failed: $names (outputs: $out)"
[ "$failed" = 0 ]
