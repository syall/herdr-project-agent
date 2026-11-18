#!/bin/sh
# Integration check for pa-session (t-001, spec §4.1: S1 S2 S3 S4 S5 S6a S7) against a PRIVATE hc core.
# Usage: tests/pa_session_check.sh <scratch dir made by tests/setup-scratch.sh>
# Every hc call goes through scratch(): its own HC_SESSION and XDG folders under the scratch dir,
# HC_TERMINAL_ID / HC_SOCKET / HC_TERMINAL_SOCKET / HC_SCOPE unset (HC_TERMINAL_SOCKET names the live
# core's socket inside a Herdr terminal). It refuses to run unless `hc server status` under that
# environment reports a core with no terminals whose listening socket is under the scratch folder. Stops that core at the end,
# also when this script is killed: a detached tests/pa_session_reap.sh waits for it to go and ends what it started (t-044).
# A wait for a start allows START_WAIT seconds: a pass is as quick as before, only a failure waits longer (load, t-044).
set -u
S=${1:?usage: pa_session_check.sh <scratch dir>}
S=${S%/}
case "$S" in /tmp/*|/private/tmp/*|/var/folders/*) ;; *) echo "refusing: $S is not a temp folder" >&2; exit 2 ;; esac
[ -x "$S/bin/pa-session" ] && [ -x "$S/bin/stub-claude" ] || { echo "run tests/setup-scratch.sh $S first" >&2; exit 2; }
SESSION="pa-t001-$$"
START_WAIT=60
here=$(cd "$(dirname "$0")" && pwd)
SP=sp0123456789abcdef0123456789abcdef
SP2=spfedcba9876543210fedcba9876543210
SP3=sp00112233445566778899aabbccddeeff
SP4=sp4444444444444444444444444444aaaa
LOG="$S/stub.log"
scratch() {
  env -u HC_TERMINAL_ID -u HC_SOCKET -u HC_TERMINAL_SOCKET -u HC_SCOPE -u CLAUDECODE \
    HC_SESSION="$SESSION" XDG_STATE_HOME="$S/state" XDG_CONFIG_HOME="$S/config" XDG_RUNTIME_DIR="$S/run" \
    HERDR_PA_HOME="$S/pa" HERDR_APP_HOME="$S/app" CLAUDE_CONFIG_DIR="$S/claude" STUB_LOG="$LOG" \
    PATH="$S/bin:$PATH" "$@"
}
fails=0
pass() { echo "PASS $1"; }
fail() { echo "FAIL $1: $2"; fails=$((fails + 1)); }
json() { python3 -c "import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1]))" "$1"; }
label() { scratch hc get "$1" --json 2>/dev/null | json "(d.get('labels') or {}).get('$2','')"; }
state() { scratch hc get "$1" --json 2>/dev/null | json "d.get('state','')"; }
agent() { python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2],''))" "$S/pa/spaces/$1/agent.json" "$2" 2>/dev/null; }
lines() { [ -f "$LOG" ] && wc -l < "$LOG" | tr -d ' ' || echo 0; }
wait_for() { # seconds, then a command that succeeds when ready
  n=$1; shift
  while [ "$n" -gt 0 ]; do "$@" && return 0; sleep 1; n=$((n - 1)); done
  "$@"
}
start() { # name space project [env...]: a pa-session in its own terminal
  name=$1 space=$2 project=$3; shift 3
  scratch hc run --bg --no-parent --no-notify --name "$name" --cwd "$S/proj" -- \
    env "STUB_LOG=$LOG" "CLAUDE_CONFIG_DIR=$S/claude" "$@" "$S/bin/pa-session" --space "$space" --project "$project" --profile test >/dev/null
}
not_live() { [ "$(state "$1")" != live ]; }
has_lines() { [ "$(lines)" -ge "$1" ]; }
last() { tail -n 1 "$LOG"; }

# The gate (plan C5): a private core, not the person's.
status=$(scratch hc server status --json) || { echo "SKIP: scratch core did not start"; scratch hc server stop >/dev/null 2>&1; exit 2; }
live=$(echo "$status" | json "d.get('live')")
pid=$(echo "$status" | json "d.get('pid')")
# The sockets that core process has open (lsof on our own process; read-only): one must be ours, under $S.
socks=$(lsof -a -p "$pid" -U -F n 2>/dev/null | sed -n 's/^n//p' | grep '^/' )
sock=$(echo "$socks" | grep -F "$S/" | grep -F "$SESSION.sock" | head -1)
outside=$(echo "$socks" | grep -v -F "$S/" | grep -F '.sock' | head -1)
real_s=$(cd "$S" && pwd -P)
[ -z "$sock" ] && sock=$(echo "$socks" | grep -F "$real_s/" | grep -F "$SESSION.sock" | head -1)
[ -n "$outside" ] && echo "$outside" | grep -qF "$real_s/" && outside=""
if [ "$live" != 0 ] || [ -z "$sock" ] || [ -n "$outside" ]; then
  echo "SKIP: scratch core not isolated (live=$live pid=$pid socket=${sock:-none under $S} other=${outside:-none})"
  scratch hc server stop >/dev/null 2>&1
  exit 2
fi
echo "scratch core: session $SESSION, pid $pid, listening socket $sock (under $S), live=$live"
core_began=$(ps -o lstart= -p "$pid" 2>/dev/null)
# The watcher: detached (fork, setsid, fork) so that neither a kill of this script's group nor of its process
# tree reaches it; it waits for this script's pid to go, then reaps. Its pid is in $S/reaper.pid.
python3 - "$S/reaper.pid" sh "$here/pa_session_reap.sh" watch "$$" "$S" "$SESSION" "$pid" "$core_began" <<'PY'
import os, sys
first = os.fork()
if first:
    os.waitpid(first, 0)
    sys.exit(0)
os.setsid()
second = os.fork()
if second:
    with open(sys.argv[1], "w") as f:
        f.write("{}\n".format(second))
    os._exit(0)
null = os.open(os.devnull, os.O_RDWR)
for fd in (0, 1, 2):
    os.dup2(null, fd)
os.execvp(sys.argv[2], sys.argv[2:])
PY
cleanup() {
  for t in n1 n2 n3 n4 n5 n6 n7 n8; do scratch hc kill "$t" >/dev/null 2>&1; done
  sleep 1
  for t in n1 n2 n3 n4 n5 n6 n7 n8; do scratch hc rm "$t" >/dev/null 2>&1; done
  scratch hc server stop >/dev/null 2>&1
  # Whatever the hc calls did not end (an hc call that failed under load), and then the watcher, not needed now.
  sh "$here/pa_session_reap.sh" reap "$S" "$SESSION" "$pid" "$core_began"
  w=$(cat "$S/reaper.pid" 2>/dev/null)
  [ -n "$w" ] && ps -o command= -p "$w" 2>/dev/null | grep -qF pa_session_reap.sh && kill "$w" 2>/dev/null
  return 0
}
trap cleanup EXIT

# S1: a fresh start gets a new session id; the terminal is labelled.
start n1 "$SP" "$S/proj"
if wait_for $START_WAIT has_lines 1; then
  sid=$(agent "$SP" sessionId)
  case "$(last)" in *"--session-id $sid "*) pass "S1 new session (--session-id $sid)" ;; *) fail S1 "stub argv: $(last | cut -c1-120)" ;; esac
  grep -q "start resume=False session=$sid reason=new" "$S/pa/spaces/$SP/session.log" && pass "S1 session.log reason=new" || fail S1 "session.log: $(tail -n 2 "$S/pa/spaces/$SP/session.log")"
  [ "$(label n1 herdr.pa)" = "$SP" ] && pass "S1 label herdr.pa" || fail S1 "label herdr.pa=$(label n1 herdr.pa)"
else fail S1 "stub never started; n1: $(scratch hc read n1 2>&1 | tail -n 5)"; fi

# S2: killed after 25 s, the next start resumes the same session.
sleep 25
scratch hc kill n1 >/dev/null
wait_for 10 not_live n1
start n2 "$SP" "$S/proj"
if wait_for $START_WAIT has_lines 2; then
  case "$(last)" in *"--resume $sid "*) pass "S2 resumed (--resume $sid)" ;; *) fail S2 "stub argv: $(last | cut -c1-120)" ;; esac
  tail -n 1 "$S/pa/spaces/$SP/session.log" | grep -Eq "start resume=True session=$sid reason=(flag|ran)" && pass "S2 session.log $(tail -n 1 "$S/pa/spaces/$SP/session.log" | grep -o 'reason=[a-z]*')" || fail S2 "session.log: $(tail -n 1 "$S/pa/spaces/$SP/session.log")"
else fail S2 "no second start; n2: $(scratch hc read n2 2>&1 | tail -n 5)"; fi

# S5: one quick failed --resume does not make the next start a blank session.
scratch hc kill n2 >/dev/null; sleep 2
rm -f "$LOG.failed"
before=$(lines)
start n3 "$SP" "$S/proj" STUB_FAIL_RESUME_ONCE=1
if wait_for $START_WAIT has_lines $((before + 2)); then
  first=$(sed -n "$((before + 1))p" "$LOG"); second=$(sed -n "$((before + 2))p" "$LOG")
  case "$first|$second" in *"--resume $sid "*"|"*"--resume $sid "*) pass "S5 still --resume $sid after one quick failure" ;; *) fail S5 "starts: [$(echo "$first" | cut -c1-80)] [$(echo "$second" | cut -c1-80)]" ;; esac
  [ -f "$LOG.failed" ] && pass "S5 the first resume did fail" || fail S5 "stub did not fail once"
else fail S5 "fewer than 2 starts; n3: $(scratch hc read n3 2>&1 | tail -n 5)"; fi

# S6a: a second pa-session for the space while the first one beats is refused (and waits, showing why).
beating() { hb=$(cat "$S/pa/spaces/$SP/heartbeat" 2>/dev/null); [ "${hb#* }" = "$(scratch hc get n3 --json | json "d['address']")" ]; }
wait_for $START_WAIT beating
before=$(lines)
start n4 "$SP" "$S/proj"
refused() { scratch hc read n4 2>&1 | grep -q "already has a Project Agent"; }
wait_for $START_WAIT refused
screen=$(scratch hc read n4 2>&1)
echo "$screen" | grep -q "already has a Project Agent" && pass "S6a second launcher refused" || fail S6a "n4 screen: $(echo "$screen" | tail -n 4)"
[ "$(lines)" = "$before" ] && pass "S6a no second agent started" || fail S6a "stub started again: $(last | cut -c1-100)"
[ "$(state n3)" = live ] && pass "S6a first launcher still live" || fail S6a "n3 state $(state n3)"
case "$(label n4 herdr.pa.error)" in *"already has a Project Agent"*) pass "S6a herdr.pa.error label set" ;; *) fail S6a "label: $(label n4 herdr.pa.error)" ;; esac
scratch hc kill n4 >/dev/null

# S7: a start that cannot work (no project folder) waits with the error shown; Enter retries.
start n5 "$SP2" "$S/missing"
waits_enter() { scratch hc read n5 2>&1 | grep -q "press Enter"; }
wait_for $START_WAIT waits_enter
screen=$(scratch hc read n5 2>&1)
[ "$(state n5)" = live ] && pass "S7 terminal stays live" || fail S7 "n5 state $(state n5)"
echo "$screen" | grep -q "press Enter" && pass "S7 shows the error and waits for Enter" || fail S7 "n5 screen: $(echo "$screen" | tail -n 4)"
case "$(label n5 herdr.pa.error)" in *"project folder does not exist"*) pass "S7 herdr.pa.error label set" ;; *) fail S7 "label: $(label n5 herdr.pa.error)" ;; esac
mkdir -p "$S/missing"
before=$(lines)
scratch hc keys n5 enter >/dev/null
if wait_for $START_WAIT has_lines $((before + 1)); then
  case "$(last)" in *"--session-id "*) pass "S7 Enter retried and started" ;; *) fail S7 "stub argv: $(last | cut -c1-100)" ;; esac
  [ -z "$(label n5 herdr.pa.error)" ] && pass "S7 herdr.pa.error removed" || fail S7 "label still: $(label n5 herdr.pa.error)"
  [ "$(label n5 herdr.pa)" = "$SP2" ] && pass "S7 labelled after the retry" || fail S7 "herdr.pa=$(label n5 herdr.pa)"
else fail S7 "no start after Enter; n5: $(scratch hc read n5 2>&1 | tail -n 5)"; fi

# S3: killed after 5 s (the session never ran 20 s), the next start is a new session.
before=$(lines)
start n6 "$SP3" "$S/proj"
if wait_for $START_WAIT has_lines $((before + 1)); then
  sid3=$(agent "$SP3" sessionId)
  sleep 4
  scratch hc kill n6 >/dev/null
  wait_for 10 not_live n6
  sleep 20  # a restore comes later than this (the keeper needs ~30-45 s): the session is long dead
  start n7 "$SP3" "$S/proj"
  if wait_for $START_WAIT has_lines $((before + 2)); then
    case "$(last)" in
      *"--resume "*) fail S3 "resumed a session that never ran: $(last | cut -c1-90)" ;;
      *"--session-id $sid3 "*) fail S3 "reused the never-run session id $sid3" ;;
      *"--session-id "*) pass "S3 new session after a 5-s run" ;;
      *) fail S3 "stub argv: $(last | cut -c1-90)" ;;
    esac
    tail -n 1 "$S/pa/spaces/$SP3/session.log" | grep -q "reason=new" && pass "S3 session.log reason=new" || fail S3 "session.log: $(tail -n 1 "$S/pa/spaces/$SP3/session.log")"
  else fail S3 "no second start; n7: $(scratch hc read n7 2>&1 | tail -n 5)"; fi
else fail S3 "stub never started; n6: $(scratch hc read n6 2>&1 | tail -n 5)"; fi

# S4: migration. sessionStarted false, the session's start line 2 days ago with no exit line, and the
# previous launcher's heartbeat a day after it: the session ran, so the real start path resumes it.
mkdir -p "$S/pa/spaces/$SP4"
X4=bbbbbbbb-cccc-dddd-eeee-ffffffffffff
began=$(( $(date +%s) - 172800 ))
python3 - "$S/pa/spaces/$SP4" "$S/proj" "$X4" "$began" <<'PY'
import json, sys, time
d, proj, sid, began = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
import os
json.dump({"version": 1, "space": d.rsplit("/", 1)[1], "project": os.path.realpath(proj), "profile": "test", "name": "proj",
           "terminal": "t999", "sessionId": sid, "sessionStarted": False}, open(d + "/agent.json", "w"))
open(d + "/session.log", "w").write("{} start resume=False session={}\n".format(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(began)), sid))
open(d + "/heartbeat", "w").write("{} t999\n".format(began + 86400))
PY
before=$(lines)
start n8 "$SP4" "$S/proj"
if wait_for $START_WAIT has_lines $((before + 1)); then
  case "$(last)" in *"--resume $X4 "*) pass "S4 migration resumed (--resume $X4)" ;; *) fail S4 "stub argv: $(last | cut -c1-90)" ;; esac
  tail -n 1 "$S/pa/spaces/$SP4/session.log" | grep -q "start resume=True session=$X4 reason=ran" && pass "S4 session.log reason=ran" || fail S4 "session.log: $(tail -n 1 "$S/pa/spaces/$SP4/session.log")"
else fail S4 "stub never started; n8: $(scratch hc read n8 2>&1 | tail -n 5)"; fi

# R (t-044): the reap ends only what this run started. Two decoys this script starts itself, which the run's
# core and launchers did not: a `tail -f` on a file in the scratch folder, and the leader of a group of its
# own (setsid) whose command line names the folder, as a caller's tool shell does. Both must outlive the
# reap. A decoy ends its tail and itself on TERM and when this script goes (a killed run leaves neither).
: > "$S/decoy.log"
python3 - "$$" "$S/decoy.log" "$S/" > "$S/decoy.pids" 2>/dev/null <<'PY' &
import os, signal, subprocess, sys, time
check, log = int(sys.argv[1]), sys.argv[2]
os.setsid()
tail = subprocess.Popen(["tail", "-f", log], stdout=subprocess.DEVNULL)
print(os.getpid(), tail.pid, flush=True)
def end(*_):
    tail.terminate()
    tail.wait()
    os._exit(0)
signal.signal(signal.SIGTERM, end)
began = time.time()
while os.getppid() == check and time.time() - began < 120:
    time.sleep(0.2)
end()
PY
decoys_written() { [ -n "$(cat "$S/decoy.pids" 2>/dev/null)" ]; }
wait_for 10 decoys_written
read -r dlead dtail < "$S/decoy.pids"
cleanup
trap - EXIT
kill -0 "$dtail" 2>/dev/null && pass "R the reap left a tail -f on a file in the scratch folder" || fail R "tail -f $dtail was ended"
kill -0 "$dlead" 2>/dev/null && pass "R the reap left a group leader naming the scratch folder" || fail R "decoy leader $dlead was ended"
left=$(sh "$here/pa_session_reap.sh" list "$S" "$SESSION" "$pid" "$core_began")
[ -z "$left" ] && pass "R nothing this run started still runs" || fail R "still running: $left"
ps -o command= -p "$dlead" 2>/dev/null | grep -qF "$S/decoy.log" && kill -TERM "$dlead" 2>/dev/null

[ "$fails" = 0 ] && echo "all pa-session checks passed" || echo "$fails pa-session check(s) failed"
[ "$fails" = 0 ]
