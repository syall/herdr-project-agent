#!/bin/sh
# Ends what one run of tests/pa_session_check.sh started (t-044), also when that run was killed before its
# own cleanup could run (SIGKILL, a closed terminal, a tool's time limit):
#   pa_session_reap.sh reap  <scratch dir> <hc session> <core pid> <core start time>
#   pa_session_reap.sh watch <check pid> <scratch dir> <hc session> <core pid> <core start time>
# reap: `hc server stop` for the scratch core (it ends its holder and the launchers in its terminals), then
# whatever is still running: each process whose command line names "<scratch dir>/" (a fresh mktemp folder,
# so only that run's processes do), and the core pid while its start time is still the recorded one.
# A process that leads its own group gets the whole group (the stub's `sleep` goes with it). TERM, then KILL
# after 3 s. Never itself or its parent. watch: wait while the check pid lives, then reap.
set -u
mode=${1:?usage: pa_session_reap.sh reap|watch ...}; shift
if [ "$mode" = watch ]; then
  check=${1:?check pid}; shift
  while kill -0 "$check" 2>/dev/null; do sleep 1; done
fi
S=${1:?scratch dir} SESSION=${2:?hc session} core=${3:-} core_began=${4:-}
case "$S" in */) echo "refusing: $S ends with / (this script's own command line would match)" >&2; exit 2 ;; esac
case "$S" in /tmp/?*|/private/tmp/?*|/var/folders/?*) ;; *) echo "refusing: $S is not a temp folder" >&2; exit 2 ;; esac
if [ -d "$S/run" ]; then
  env -u HC_TERMINAL_ID -u HC_SOCKET -u HC_TERMINAL_SOCKET -u HC_SCOPE -u CLAUDECODE \
    HC_SESSION="$SESSION" XDG_STATE_HOME="$S/state" XDG_CONFIG_HOME="$S/config" XDG_RUNTIME_DIR="$S/run" \
    HERDR_PA_HOME="$S/pa" HERDR_APP_HOME="$S/app" CLAUDE_CONFIG_DIR="$S/claude" PATH="$S/bin:$PATH" \
    hc server stop >/dev/null 2>&1
fi
# The core pid counts only while it is the same process (its start time as recorded at the gate).
{ [ -n "$core" ] && [ -n "$core_began" ] && [ "$(ps -o lstart= -p "$core" 2>/dev/null)" = "$core_began" ]; } || core=""
ours() { # "pid pgid" of each process still running that this run started
  ps -axo pid=,pgid=,command= | REAP_PAT="$S/" REAP_ME="$$" REAP_UP="$PPID" REAP_CORE="$core" awk '
    $1 == ENVIRON["REAP_ME"] || $1 == ENVIRON["REAP_UP"] { next }
    index($0, ENVIRON["REAP_PAT"]) || ($1 == ENVIRON["REAP_CORE"] && ENVIRON["REAP_CORE"] != "") { print $1, $2 }'
}
end_all() { # signal
  ours | while read -r p g; do
    if [ "$p" = "$g" ]; then kill -"$1" -- "-$g" 2>/dev/null || kill -"$1" "$p" 2>/dev/null
    else kill -"$1" "$p" 2>/dev/null; fi
  done
}
n=5
while [ "$n" -gt 0 ] && [ -n "$(ours)" ]; do sleep 1; n=$((n - 1)); done  # hc server stop ends them in a moment
if [ -n "$(ours)" ]; then
  end_all TERM
  sleep 3
  end_all KILL
fi
exit 0
