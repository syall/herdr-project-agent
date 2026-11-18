#!/bin/sh
# Integration check for pa-session (t-001, spec §4.1: S1 S2 S3 S4 S5 S6a S7; t-037 J2: S8 S9 S10, the restart in
# place; round 2: S11 S12; round 3: D4 inside S11; J3: S13, the --end-terminal bridge; t-041 J4: S14, the fresh
# context) against a PRIVATE hc core.
# Usage: tests/pa_session_check.sh <scratch dir made by tests/setup-scratch.sh>
# Every hc call goes through scratch(): its own HC_SESSION and XDG folders under the scratch dir,
# HC_TERMINAL_ID / HC_SOCKET / HC_TERMINAL_SOCKET / HC_SCOPE unset (HC_TERMINAL_SOCKET names the live
# core's socket inside a Herdr terminal). It refuses to run unless `hc server status` under that
# environment reports a core with no terminals whose listening socket is under the scratch folder. Stops that core at the end,
# also when this script is killed: a detached tests/pa_session_reap.sh waits for it to go and ends what it started (t-044).
# The reaper knows what the run started by pid and start time (the core at the gate, each launcher as it starts,
# their descendants by ancestry), never by a command line; the R step checks it leaves other processes alone.
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
  env -u HC_TERMINAL_ID -u HC_SOCKET -u HC_TERMINAL_SOCKET -u HC_SCOPE -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT -u CODEX_SANDBOX \
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
  # Its first act records its pid and start time for the reaper (then exec keeps that pid for pa-session).
  name=$1 space=$2 project=$3; shift 3
  scratch hc run --bg --no-parent --no-notify --name "$name" --cwd "$S/proj" -- \
    sh -c 'echo "$$ $(ps -o lstart= -p $$)" >> "$0"; exec "$@"' "$S/reap.started" \
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
  [ -n "${sampler:-}" ] && kill "$sampler" 2>/dev/null
  for t in n1 n2 n3 n4 n5 n6 n7 n8 n9 n10 n11 n12 w8 w9 w11 n13 w13 k13 r13 n13b w13b k13b r13b tab13; do scratch hc kill "$t" >/dev/null 2>&1; done
  sleep 1
  for t in n1 n2 n3 n4 n5 n6 n7 n8 n9 n10 n11 n12 w8 w9 w11 n13 w13 k13 r13 n13b w13b k13b r13b tab13; do scratch hc rm "$t" >/dev/null 2>&1; done
  scratch hc server stop >/dev/null 2>&1
  # Whatever the hc calls did not end (an hc call that failed under load), and then the watcher, not needed now.
  sh "$here/pa_session_reap.sh" reap "$S" "$SESSION" "$pid" "$core_began"
  w=$(cat "$S/reaper.pid" 2>/dev/null)
  [ -n "$w" ] && ps -o command= -p "$w" 2>/dev/null | grep -qF "pa_session_reap.sh watch $$ $S " && kill "$w" 2>/dev/null
  return 0
}
trap cleanup EXIT

# S1: a fresh start gets a new session id; the terminal is labelled.
start n1 "$SP" "$S/proj"
if wait_for $START_WAIT has_lines 1; then
  sid=$(agent "$SP" sessionId)
  case "$(last)" in *"--session-id $sid "*) pass "S1 new session (--session-id $sid)" ;; *) fail S1 "stub argv: $(last | cut -c1-120)" ;; esac
  grep -q "start resume=False session=$sid reason=new" "$S/pa/spaces/$SP/session.log" && pass "S1 session.log reason=new" || fail S1 "session.log: $(tail -n 2 "$S/pa/spaces/$SP/session.log")"
  case "$(last)" in *"--autocompact 400000 "*) pass "S1 the 400k net (--autocompact 400000, t-041 J3)" ;; *) fail S1 "no --autocompact 400000: $(last | grep -o -- '--autocompact [0-9]*' || echo none)" ;; esac
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

# S8 (t-037 J2, AC6.6/AC11.1): pa-restart restarts the launcher in place. The terminal keeps its address and pid
# and is never finished; its worker W stays live and still its child; the next Claude resumes the same session with
# the new core text (S8b: a marker, --system-prompt-snapshot off from the profile, prompt rebuilt).
SP8=sp8888888888888888888888888888bbbb
L8="$S/pa/spaces/$SP8/launcher.json"
jf() { python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(eval(sys.argv[2]))" "$@" 2>/dev/null; }
before=$(lines)
start n9 "$SP8" "$S/proj" "STUB_TERM_LOG=$S/term.log"
if wait_for 15 has_lines $((before + 1)); then
  sid8=$(agent "$SP8" sessionId)
  addr0=$(scratch hc get n9 --json | json "d['address']")
  pid0=$(scratch hc get n9 --json | json "d.get('pid')")
  scratch hc run --bg --parent n9 --no-notify --name w8 -- sleep 600 >/dev/null
  inst0=$(jf "$L8" "d['instance']")
  sleep 24  # the session must have run (RAN) to be resumed
  echo "MARKER-T037-$$" >> "$S/pa/core/orchestrator.md"
  printf '{"name":"test","claude":["%s/bin/stub-claude"],"codex":["true"],"systemPromptSnapshot":"off"}\n' "$S" > "$S/pa/profiles/test/profile.json"
  scratch python3 "$S/pa/tools/pa.py" stamp >/dev/null
  : > "$S/samples"
  ( while :; do scratch hc get n9 --json 2>/dev/null | json "'{} {}'.format(d.get('state'), d.get('pid'))" >> "$S/samples"; sleep 0.5; done ) &
  sampler=$!
  before=$(lines)
  scratch pa-restart --space "$SP8" --wait 90 > "$S/restart.out" 2>&1
  rc=$?
  kill "$sampler" 2>/dev/null; sampler=""
  [ "$rc" = 0 ] && grep -q "Restarted" "$S/restart.out" && pass "S8 pa-restart reports the restart" || fail S8 "pa-restart exit $rc: $(tail -n 5 "$S/restart.out")"
  [ "$(state n9)" = live ] && pass "S8 terminal still live" || fail S8 "n9 state $(state n9)"
  [ "$(scratch hc get n9 --json | json "d['address']")" = "$addr0" ] && pass "S8 same address $addr0" || fail S8 "address changed"
  [ "$(scratch hc get n9 --json | json "d.get('pid')")" = "$pid0" ] && pass "S8 same pid $pid0" || fail S8 "pid now $(scratch hc get n9 --json | json "d.get('pid')")"
  bad=$(grep -v "^live $pid0\$" "$S/samples" | head -1)
  [ -s "$S/samples" ] && [ -z "$bad" ] && pass "S8 live with pid $pid0 in all $(wc -l < "$S/samples" | tr -d ' ') samples" || fail S8 "a sample: ${bad:-none taken}"
  [ "$(state w8)" = live ] && pass "S8 worker w8 still live" || fail S8 "w8 state $(state w8)"
  [ "$(scratch hc get w8 --json | json "d.get('parent')")" = "$addr0" ] && pass "S8 w8 still the PA terminal's child" || fail S8 "w8 parent $(scratch hc get w8 --json | json "d.get('parent')")"
  scratch hc send n9 "report from w8" >/dev/null 2>&1 && pass "S8 hc send to the PA still works" || fail S8 "hc send failed"
  if wait_for 15 has_lines $((before + 1)); then
    new=$(sed -n "$((before + 1))p" "$LOG")
    case "$new" in *"--resume $sid8 "*) pass "S8 resumed the same session" ;; *) fail S8 "stub argv: $(echo "$new" | cut -c1-100)" ;; esac
    case "$new" in *"--system-prompt-snapshot off"*) pass "S8b --system-prompt-snapshot off passed" ;; *) fail S8b "no snapshot flag" ;; esac
    case "$new" in *"MARKER-T037-$$"*) pass "S8b the new core text is in the resumed launch" ;; *) fail S8b "no marker in the argv" ;; esac
  else fail S8 "no Claude start after the restart; n9: $(scratch hc read n9 2>&1 | tail -n 5)"; fi
  grep -q TERM "$S/term.log" 2>/dev/null && pass "S8 Claude was sent SIGTERM" || fail S8 "no TERM in the stub's log"
  [ "$(jf "$L8" "d['restartedFrom']")" = "$inst0" ] && [ "$(jf "$L8" "d['instance']")" != "$inst0" ] && pass "S8 launcher.json: a new instance restarted from the old" || fail S8 "launcher.json: $(head -c 300 "$L8")"
  [ "$(jf "$L8" "d['restart']['outcome']")" = ok ] && [ "$(jf "$L8" "d['pid']")" = "$pid0" ] && pass "S8 outcome ok, same pid" || fail S8 "restart $(jf "$L8" "d['restart']")"
  wait_for 10 sh -c "[ \"\$(python3 -c \"import json;print((json.load(open('$L8')).get('session') or {}).get('prompt'))\")\" = rebuilt ]" && pass "S8b session.prompt rebuilt" || fail S8b "session $(jf "$L8" "d['session']")"
  [ ! -e "$S/pa/spaces/$SP8/restart-request.json" ] && pass "S8 request consumed" || fail S8 "request left"
  [ -z "$(label n9 herdr.pa.restarting)" ] && pass "S8 herdr.pa.restarting removed" || fail S8 "label $(label n9 herdr.pa.restarting)"

  # S10: a request for another instance is refused: nothing restarts, nothing ends.
  before=$(lines)
  python3 - "$S/pa/spaces/$SP8" "$SP8" "$addr0" <<'PY2'
import json, sys, time
d, sp, t = sys.argv[1:4]
at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
exp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 600))
json.dump({"version": 1, "id": "s10-request", "space": sp, "instance": "not-this-one", "terminal": t, "at": at,
           "expires": exp, "by": "pa-restart", "why": "S10", "now": True}, open(d + "/restart-request.json", "w"))
PY2
  refused() { [ "$(jf "$L8" "(d.get('restart') or {}).get('outcome')")" = refused ]; }
  wait_for 10 refused && pass "S10 another instance's request refused" || fail S10 "launcher.json restart: $(jf "$L8" "d.get('restart')")"
  sleep 2
  [ "$(lines)" = "$before" ] && [ "$(scratch hc get n9 --json | json "d.get('pid')")" = "$pid0" ] && [ "$(state w8)" = live ] && pass "S10 nothing restarted or ended" || fail S10 "stub restarted or a terminal changed"
else fail S8 "stub never started; n9: $(scratch hc read n9 2>&1 | tail -n 5)"; fi

# S11 (t-037 J2 round 2, VERIFY MF4): the installed pa.py passes the self-check but raises on the restarted path.
# The exec'd launcher shows the error and waits (herdr.pa.error); its terminal, pid and worker stay. After a fixed
# install, Enter loads the installed launcher again: it records the restart and starts Claude.
SP11=sp1111111111111111111111111111dddd
L11="$S/pa/spaces/$SP11/launcher.json"
cp "$S/pa/tools/pa.py" "$S/pa.py.good"
mkdir -p "$S/proj11" "$S/proj12"  # own project folders: a finished space's folder is adopted by project (find_orphan)
before=$(lines)
start n11 "$SP11" "$S/proj11"
if wait_for 15 has_lines $((before + 1)); then
  scratch hc run --bg --parent n11 --no-notify --name w11 -- sleep 600 >/dev/null
  pid11=$(scratch hc get n11 --json | json "d.get('pid')")
  inst11=$(jf "$L11" "d['instance']")
  python3 - "$S/pa/tools/pa.py" <<'PY3'
import sys
f = sys.argv[1]
t = open(f).read()
t = t.replace('def cmd_session(args):\n', 'def cmd_session(args):\n    if getattr(args, "restarted", None):\n        raise RuntimeError("S11 a bug only on the restarted path")\n', 1)
open(f, "w").write(t)
PY3
  scratch python3 "$S/pa/tools/pa.py" stamp >/dev/null
  scratch pa-restart --space "$SP11" --now --wait 15 > "$S/restart11.out" 2>&1
  errored() { label n11 herdr.pa.error | grep -q "RuntimeError"; }
  wait_for 10 errored && pass "S11 the restarted launcher shows the error (herdr.pa.error)" || fail S11 "label $(label n11 herdr.pa.error); n11: $(scratch hc read n11 2>&1 | tail -n 4)"
  [ "$(state n11)" = live ] && [ "$(scratch hc get n11 --json | json "d.get('pid')")" = "$pid11" ] && pass "S11 terminal live, same pid $pid11" || fail S11 "n11 state $(state n11) pid $(scratch hc get n11 --json | json "d.get('pid')")"
  [ "$(state w11)" = live ] && [ "$(scratch hc get w11 --json | json "d.get('parent')")" = "$(scratch hc get n11 --json | json "d['address']")" ] && pass "S11 worker w11 live and still its child" || fail S11 "w11 state $(state w11)"
  # D4 (J2 round 3, VERIFY MF-r2-1): a "fix" that does not compile, stamped, then Enter: the pre-check refuses it,
  # the tab says why and waits again; the terminal, its pid and w11 stay.
  { printf 'def broken(:\n'; cat "$S/pa.py.good"; } > "$S/pa/tools/pa.py"
  scratch python3 "$S/pa.py.good" stamp >/dev/null
  scratch hc send n11 "retry" >/dev/null 2>&1
  refused11() { label n11 herdr.pa.error | grep -q "cannot be loaded"; }
  wait_for 30 refused11 && pass "D4 Enter with a pa.py that does not compile: refused, the tab says why" || fail D4 "label $(label n11 herdr.pa.error); n11: $(scratch hc read n11 2>&1 | tail -n 4)"
  [ "$(state n11)" = live ] && [ "$(scratch hc get n11 --json | json "d.get('pid')")" = "$pid11" ] && [ "$(state w11)" = live ] && pass "D4 terminal live, same pid $pid11, w11 live" || fail D4 "n11 state $(state n11) pid $(scratch hc get n11 --json | json "d.get('pid')") w11 $(state w11)"
  cp "$S/pa.py.good" "$S/pa/tools/pa.py"
  scratch python3 "$S/pa/tools/pa.py" stamp >/dev/null
  before=$(lines)
  scratch hc send n11 "retry" >/dev/null 2>&1
  recovered() { [ "$(jf "$L11" "d.get('restartedFrom')")" = "$inst11" ] && [ "$(jf "$L11" "(d.get('restart') or {}).get('outcome')")" = ok ]; }
  wait_for 20 recovered && pass "S11 Enter after a fixed install loads it: restartedFrom the old instance, outcome ok" || fail S11 "launcher.json: $(head -c 300 "$L11")"
  wait_for 15 has_lines $((before + 1)) && pass "S11 Claude starts again" || fail S11 "no Claude start; n11: $(scratch hc read n11 2>&1 | tail -n 4)"
  [ -z "$(label n11 herdr.pa.error)" ] && [ "$(scratch hc get n11 --json | json "d.get('pid')")" = "$pid11" ] && [ "$(state w11)" = live ] && pass "S11 error label gone, same pid, w11 live" || fail S11 "error $(label n11 herdr.pa.error) pid $(scratch hc get n11 --json | json "d.get('pid')") w11 $(state w11)"
else fail S11 "stub never started; n11: $(scratch hc read n11 2>&1 | tail -n 5)"; fi
cp "$S/pa.py.good" "$S/pa/tools/pa.py"
scratch python3 "$S/pa/tools/pa.py" stamp >/dev/null

# S12 (t-037 J2 round 2, VERIFY MF1, REVIEW A M2/B M1): a request taken while no Claude runs (here during the first
# launch's `--help` probe, 8 s) restarts before any Claude starts, and pa-restart gets the answer.
SP12=sp1212121212121212121212121212eeee
L12="$S/pa/spaces/$SP12/launcher.json"
mkdir -p "$S/pa/profiles/probe"
printf '{"name":"probe","claude":["%s/bin/stub-claude"],"codex":["true"]}\n' "$S" > "$S/pa/profiles/probe/profile.json"
before=$(lines)
scratch hc run --bg --no-parent --no-notify --name n12 --cwd "$S/proj" -- \
  env "STUB_LOG=$LOG" "CLAUDE_CONFIG_DIR=$S/claude" "STUB_HELP_SLEEP=8" "$S/bin/pa-session" --space "$SP12" --project "$S/proj12" --profile probe >/dev/null
if wait_for 15 test -s "$L12"; then
  sleep 1
  pid12=$(scratch hc get n12 --json | json "d.get('pid')")
  inst12=$(jf "$L12" "d['instance']")
  [ "$(lines)" = "$before" ] && pass "S12 no Claude yet (the probe runs)" || fail S12 "a Claude started before the request"
  scratch pa-restart --space "$SP12" --now --wait 40 > "$S/restart12.out" 2>&1
  rc=$?
  [ "$rc" = 0 ] && grep -q "Restarted" "$S/restart12.out" && pass "S12 pa-restart reports the restart" || fail S12 "pa-restart exit $rc: $(tail -n 3 "$S/restart12.out")"
  log12="$S/pa/spaces/$SP12/session.log"
  ex=$(grep -n "restart exec" "$log12" | head -1 | cut -d: -f1)
  st=$(grep -n "start resume=" "$log12" | head -1 | cut -d: -f1)
  [ -n "$ex" ] && [ -n "$st" ] && [ "$st" -gt "$ex" ] && pass "S12 the exec came before any Claude start" || fail S12 "session.log: $(tr '\n' '|' < "$log12" | cut -c1-400)"
  [ "$(jf "$L12" "d.get('restartedFrom')")" = "$inst12" ] && [ "$(scratch hc get n12 --json | json "d.get('pid')")" = "$pid12" ] && [ "$(state n12)" = live ] && pass "S12 restarted in place, same pid $pid12" || fail S12 "launcher.json: $(head -c 300 "$L12")"
  [ -z "$(label n12 herdr.pa.restarting)" ] && pass "S12 herdr.pa.restarting removed" || fail S12 "label $(label n12 herdr.pa.restarting)"
  [ "$(lines)" = $((before + 1)) ] && pass "S12 one Claude start (after the restart)" || fail S12 "stub starts $(( $(lines) - before ))"
else fail S12 "no launcher.json; n12: $(scratch hc read n12 2>&1 | tail -n 5)"; fi

# S13 (t-037 J3, spec R10, AC10.2): a launcher from before restart support (here: a J1-era version-1 launcher.json)
# is ended only by pa-restart --end-terminal after a typed yes, and comes back as one launcher per space.
# S13a plain pa-restart names --end-terminal; S13b workers without --anyway: refused; S13c the keeper restores first
# (pa-start not needed); S13d no keeper: pa-start opens it (fake herdr-app), and a late keeper's launcher is refused.
SP13=sp1313131313131313131313131313ffff
SP13B=sp1313131313131313131313131313bbbb
mkdir -p "$S/proj13" "$S/proj13b"
: > "$S/herdr-app.log"
old_launcher() { # name space project: its terminal's argv is exactly pa-session for the space (R10 check (e))
  scratch hc run --bg --no-parent --no-notify --name "$1" --cwd "$3" -- "$S/bin/pa-session" --space "$2" --project "$3" --profile test >/dev/null
}
as_j1() { python3 - "$S/pa/spaces/$1/launcher.json" <<'PY13'
import json, sys
f = sys.argv[1]
d = json.load(open(f))
d["version"] = 1
d.pop("accepts", None)
json.dump(d, open(f, "w"))
PY13
}
addr() { scratch hc get "$1" --json 2>/dev/null | json "d.get('address','')"; }
beats() { hb=$(cat "$S/pa/spaces/$1/heartbeat" 2>/dev/null); [ "${hb#* }" = "$2" ] && [ $(( $(date +%s) - ${hb%% *} )) -le 30 ]; }
pa_count() { # space: live terminals labelled for it that are not a refused launcher (no herdr.pa.error)
  scratch python3 - "$1" <<'PY13'
import json, subprocess, sys
listed = json.loads(subprocess.run(["hc", "ls", "--json"], stdout=subprocess.PIPE, universal_newlines=True).stdout or "{}")
n = 0
for t in listed.get("terminals", []):
    if t.get("state") != "live":
        continue
    d = json.loads(subprocess.run(["hc", "get", t["address"], "--json"], stdout=subprocess.PIPE, universal_newlines=True).stdout or "{}")
    labels = d.get("labels") or {}
    if labels.get("herdr.pa") == sys.argv[1] and not labels.get("herdr.pa.error"):
        n += 1
print(n)
PY13
}
end13() { # terminal space outfile: pa-restart --end-terminal --anyway in its own terminal (a tty to type yes in)
  scratch hc run --bg --no-parent --no-notify --name "$1" --cwd "$S" -- \
    sh -c '"$0" --space "$1" --end-terminal --anyway --why S13 > "$2" 2>&1; echo "rc=$?" >> "$2"' "$S/bin/pa-restart" "$2" "$3" >/dev/null
}
answer13() { # terminal outfile: wait for the question, type yes, wait for the exit code
  wait_for 30 grep -q "Type yes" "$2" || return 1
  scratch hc send "$1" yes >/dev/null 2>&1
  wait_for 120 grep -q "^rc=" "$2"
}

before=$(lines)
old_launcher n13 "$SP13" "$S/proj13"
if wait_for $START_WAIT has_lines $((before + 1)) && wait_for 10 test -s "$S/pa/spaces/$SP13/launcher.json"; then
  sid13=$(agent "$SP13" sessionId)
  a13=$(addr n13)
  scratch hc run --bg --parent n13 --no-notify --name w13 -- sleep 600 >/dev/null
  sleep 1
  as_j1 "$SP13"
  scratch pa-restart --space "$SP13" > "$S/r13a.out" 2>&1
  rc=$?
  [ "$rc" = 2 ] && grep -q "started before restart support" "$S/r13a.out" && grep -q -- "--end-terminal" "$S/r13a.out" && pass "S13a plain pa-restart refuses and names --end-terminal" || fail S13a "exit $rc: $(tail -n 2 "$S/r13a.out")"
  scratch pa-restart --space "$SP13" --end-terminal < /dev/null > "$S/r13b.out" 2>&1
  rc=$?
  [ "$rc" = 1 ] && grep -q "w13" "$S/r13b.out" && grep -q -- "--anyway" "$S/r13b.out" && [ "$(state n13)" = live ] && [ "$(state w13)" = live ] && pass "S13b workers without --anyway: refused, nothing ended" || fail S13b "exit $rc n13 $(state n13) w13 $(state w13): $(tail -n 2 "$S/r13b.out")"
  # The simulated keeper: once n13 has ended, a new launcher for the space a few seconds later (keeper.js restores
  # with the same argv in a new pane).
  ( wait_for 90 not_live n13 && sleep 4 && old_launcher k13 "$SP13" "$S/proj13" ) &
  keeper13=$!
  sleep 21  # past RAN (20 s): a session that ran is resumed by the next launcher; a shorter one is new (S3)
  before=$(lines)
  end13 r13 "$SP13" "$S/r13c.out"
  if answer13 r13 "$S/r13c.out"; then
    wait "$keeper13"
    grep -q "^rc=0" "$S/r13c.out" && grep -q "Restored by the keeper" "$S/r13c.out" && pass "S13c ended after yes; the keeper's restore was seen" || fail S13c "$(tail -n 4 "$S/r13c.out")"
    not_live n13 && not_live w13 && pass "S13c the terminal and its worker ended (as it said)" || fail S13c "n13 $(state n13) w13 $(state w13)"
    k13=$(addr k13)
    wait_for 20 beats "$SP13" "$k13" && [ "$(agent "$SP13" terminal)" = "$k13" ] && [ "$(pa_count "$SP13")" = 1 ] && pass "S13c one launcher for the space (k13)" || fail S13c "agent terminal $(agent "$SP13" terminal), k13 $k13, count $(pa_count "$SP13")"
    [ ! -s "$S/herdr-app.log" ] && pass "S13c pa-start not needed" || fail S13c "herdr-app called: $(cat "$S/herdr-app.log")"
    wait_for 20 has_lines $((before + 1)) && tail -n 1 "$LOG" | grep -q -- "--resume $sid13 " && pass "S13c the new launcher resumes the same session" || fail S13c "stub: $(tail -n 1 "$LOG" | cut -c1-100)"
    [ "$(jf "$S/pa/spaces/$SP13/launcher.json" "d.get('version')")" = 2 ] && pass "S13c the new launcher is restart-capable" || fail S13c "launcher.json $(head -c 200 "$S/pa/spaces/$SP13/launcher.json")"
    grep -q "end-terminal terminal=$a13 workers=1 by=pa-restart" "$S/pa/spaces/$SP13/session.log" && pass "S13c session.log line" || fail S13c "session.log: $(tail -n 3 "$S/pa/spaces/$SP13/session.log")"
  else fail S13c "no question or no answer: $(tail -n 4 "$S/r13c.out")"; kill "$keeper13" 2>/dev/null; fi
else fail S13 "stub never started; n13: $(scratch hc read n13 2>&1 | tail -n 5)"; fi

before=$(lines)
old_launcher n13b "$SP13B" "$S/proj13b"
if wait_for $START_WAIT has_lines $((before + 1)) && wait_for 10 test -s "$S/pa/spaces/$SP13B/launcher.json"; then
  scratch hc run --bg --parent n13b --no-notify --name w13b -- sleep 600 >/dev/null
  sleep 1
  as_j1 "$SP13B"
  end13 r13b "$SP13B" "$S/r13d.out"
  if answer13 r13b "$S/r13d.out"; then
    grep -q "^rc=0" "$S/r13d.out" && grep -q "pa-start" "$S/r13d.out" && pass "S13d no keeper: pa-start opened it" || fail S13d "$(tail -n 4 "$S/r13d.out")"
    [ "$(grep -c "do tab.new" "$S/herdr-app.log")" = 1 ] && grep -q "$SP13B" "$S/herdr-app.log" && pass "S13d herdr-app tab.new once, for the space" || fail S13d "herdr-app log: $(cat "$S/herdr-app.log")"
    t13=$(addr tab13)
    wait_for 30 beats "$SP13B" "$t13" && pass "S13d the opened launcher beats" || fail S13d "heartbeat $(cat "$S/pa/spaces/$SP13B/heartbeat" 2>/dev/null), tab13 $t13"
    not_live w13b && pass "S13d its worker ended" || fail S13d "w13b $(state w13b)"
    old_launcher k13b "$SP13B" "$S/proj13b"
    refused13() { scratch hc read k13b 2>&1 | grep -q "already has a Project Agent"; }
    wait_for 40 refused13 && pass "S13d a late keeper's launcher is refused" || fail S13d "k13b: $(scratch hc read k13b 2>&1 | tail -n 3)"
    [ "$(agent "$SP13B" terminal)" = "$t13" ] && [ "$(pa_count "$SP13B")" = 1 ] && beats "$SP13B" "$t13" && pass "S13d still one launcher for the space" || fail S13d "agent terminal $(agent "$SP13B" terminal), count $(pa_count "$SP13B")"
  else fail S13d "no question or no answer: $(tail -n 4 "$S/r13d.out")"; fi
else fail S13d "stub never started; n13b: $(scratch hc read n13b 2>&1 | tail -n 5)"; fi

# S14 (t-041 J4, AC4.2, AC11.1): over the cap at a clean point, the launcher starts Claude in a fresh session in the
# same terminal: a new --session-id, no --resume, the start prompt; the terminal keeps its address and pid, and its
# worker stays live and its child. The stub is a shell, so hc gives it no agent status: an hc shim on the launcher's
# PATH only (plan Q2) adds status idle to `hc get` while $S/stub-idle exists; everything else goes to the real hc.
SP14=sp1414141414141414141414141414cccc
D14="$S/pa/spaces/$SP14"
real_hc=$(command -v hc)
mkdir -p "$S/bin14"
cat > "$S/bin14/hc" <<SHIM
#!/bin/sh
if [ "\${1:-}" = get ] && [ -e "$S/stub-idle" ]; then
  out=\$("$real_hc" "\$@") || exit \$?
  printf '%s' "\$out" | python3 -c 'import json, sys
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except ValueError:
    sys.stdout.write(raw)
    sys.exit(0)
d["status"] = {"state": "idle"}
print(json.dumps(d))'
  exit 0
fi
exec "$real_hc" "\$@"
SHIM
chmod +x "$S/bin14/hc"
: > "$S/stub-idle"
before=$(lines)
start n14 "$SP14" "$S/proj" "PATH=$S/bin14:$S/bin:$PATH" "STUB_USAGE=300000"
if wait_for $START_WAIT has_lines $((before + 1)); then
  sid14=$(agent "$SP14" sessionId)
  addr14=$(addr n14)
  pid14=$(scratch hc get n14 --json | json "d.get('pid')")
  scratch hc run --bg --parent n14 --no-notify --name w14 -- sleep 600 >/dev/null
  w14=$(addr w14)
  mkdir -p "$D14/evidence/t-1"
  echo "Do the job." > "$D14/evidence/t-1/handoff-build.md"
  sleep 2  # the stub's transcript is written; the checkpoint comes after its turn
  python3 - "$D14" "$sid14" "$w14" <<'PY14'
import json, sys, time
d, sid, w = sys.argv[1:4]
path = d + "/ledger.json"
try:
    led = json.load(open(path))
except Exception:
    led = {"version": 1, "passCount": 0, "tasks": []}
led["context"] = {"pass": 1, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "session": sid, "clean": True,
                  "boundary": "pass", "why": None}
led["tasks"] = [{"id": "t-1", "state": "build", "workers": [{"address": w, "name": "t-1-build"}]}]
json.dump(led, open(path, "w"), indent=2)
PY14
  if wait_for 90 has_lines $((before + 2)); then
    new=$(sed -n "$((before + 2))p" "$LOG")
    sid14b=$(agent "$SP14" sessionId)
    [ -n "$sid14b" ] && [ "$sid14b" != "$sid14" ] && case "$new" in *"--session-id $sid14b "*) true ;; *) false ;; esac \
      && pass "S14a a fresh session (--session-id $sid14b)" || fail S14a "stub argv: $(echo "$new" | cut -c1-120)"
    case "$new" in *"--resume"*) fail S14a "the fresh start resumed" ;; *) pass "S14a no --resume" ;; esac
    case "$new" in *"Fresh context (reset 1 today, reason pass-cap; previous session $sid14)"*) pass "S14a the start prompt" ;; *) fail S14a "no start prompt in the argv" ;; esac
    grep -q "fresh from=$sid14 to=$sid14b ctx=300000 reason=pass-cap" "$D14/session.log" && pass "S14a session.log fresh line" || fail S14a "session.log: $(tail -n 3 "$D14/session.log")"
    [ "$(state n14)" = live ] && [ "$(addr n14)" = "$addr14" ] && [ "$(scratch hc get n14 --json | json "d.get('pid')")" = "$pid14" ] \
      && pass "S14b same terminal $addr14, pid $pid14" || fail S14b "n14 $(state n14) $(addr n14)"
    [ "$(state w14)" = live ] && [ "$(scratch hc get w14 --json | json "d.get('parent')")" = "$addr14" ] \
      && pass "S14c worker w14 still live and its child" || fail S14c "w14 $(state w14)"
    scratch hc send n14 "report from w14 S14" >/dev/null 2>&1
    seen14() { scratch hc read n14 2>&1 | grep -q "report from w14 S14"; }
    if wait_for 5 seen14; then pass "S14d the report reached the PA's terminal"
    elif [ "$(state w14)" = live ]; then pass "S14d (recorded limit C2) w14 still live for t-034's recovery"
    else fail S14d "report not seen and w14 gone"; fi
  else fail S14a "no fresh start; n14: $(scratch hc read n14 2>&1 | tail -n 5); session.log: $(tail -n 4 "$D14/session.log" 2>/dev/null)"; fi
else fail S14 "stub never started; n14: $(scratch hc read n14 2>&1 | tail -n 5)"; fi
rm -f "$S/stub-idle"

# S9 (negative control): in this core a child does end with its parent's program, so S8's "w8 still live" means it.
scratch hc run --bg --no-parent --no-notify --name n10 -- sleep 600 >/dev/null
scratch hc run --bg --parent n10 --no-notify --name w9 -- sleep 600 >/dev/null
sleep 1
scratch hc kill n10 >/dev/null
wait_for 10 not_live w9 && pass "S9 a child ends when its parent's program ends" || fail S9 "w9 state $(state w9)"

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
