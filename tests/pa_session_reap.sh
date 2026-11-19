#!/bin/sh
# Ends what one run of tests/pa_session_check.sh started (t-044), also when that run was killed before its
# own cleanup could run (SIGKILL, a closed terminal, a tool's time limit):
#   pa_session_reap.sh reap  <scratch dir> <hc session> <core pid> <core start time>
#   pa_session_reap.sh watch <check pid> <scratch dir> <hc session> <core pid> <core start time>
#   pa_session_reap.sh list  <scratch dir> <hc session> <core pid> <core start time>
# What the run started is known by identity, never by a command line or a folder name: a pid together with
# its start time (ps lstart), recorded when it started. The check records the scratch core (its pid and start
# time at the gate, passed here) and each launcher (it writes "<pid> <start time>" to <scratch>/reap.started
# as its first act, then execs pa-session in the same pid). Their descendants (the core's holder, the
# launchers, the stubs and their sleeps) are added by ancestry from a recorded pid that is still the same
# process, and kept in memory and in that file (a launcher that dies leaves its stub with ppid 1).
# reap: note the descendants, `hc server stop` for the scratch core, then TERM what still runs, KILL after
# 3 s. Each signal goes to one pid, only after checking that pid still has the recorded start time (a reused
# pid is never signalled); no process group is signalled. Never itself or its parent.
# watch: while the check pid lives, note descendants each second; then reap (from memory, so a scratch folder
# removed meanwhile does not hide them). list: print the recorded pids that still run.
exec python3 - "$0" "$@" <<'PY'
import os, signal, subprocess, sys, time

mode = sys.argv[2] if len(sys.argv) > 2 else ""
args = sys.argv[3:]
if mode not in ("reap", "watch", "list") or len(args) < (2 if mode != "watch" else 3):
    sys.stderr.write("usage: pa_session_reap.sh reap|watch|list ...\n")
    sys.exit(2)
check = int(args.pop(0)) if mode == "watch" else None
S, SESSION = args[0].rstrip("/"), args[1]
core = args[2] if len(args) > 2 else ""
core_began = args[3] if len(args) > 3 else ""
ROOTS = ("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/")
REAL = os.path.realpath(S)  # t-112 §10: /tmp/../etc, or a /tmp link to another folder, is not a temp folder
if not (S.startswith(ROOTS[:3]) and len(S.split("/")) > 2 and REAL.startswith(ROOTS) and len(REAL.split("/")) > 2):
    sys.stderr.write("refusing: {} is not a temp folder\n".format(S))
    sys.exit(2)
RECORD = S + "/reap.started"
SKIP = {os.getpid(), os.getppid()}


def norm(s):
    return " ".join(s.split())


def table():  # pid -> (ppid, start time) of every process now
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,lstart="], stdout=subprocess.PIPE,
                         universal_newlines=True).stdout
    t = {}
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 7 and f[0].isdigit() and f[1].isdigit():
            t[int(f[0])] = (int(f[1]), " ".join(f[2:]))
    return t


def began(pid):  # start time of one pid now, "" when it is gone
    r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], stdout=subprocess.PIPE,
                       stderr=subprocess.DEVNULL, universal_newlines=True)
    return norm(r.stdout) if r.returncode == 0 else ""


known = {}  # pid -> start time: what this run started


def note(pid, start):
    if pid in SKIP or not start or pid in known:
        return False
    known[pid] = start
    return True


def load():
    if core.isdigit() and norm(core_began):
        note(int(core), norm(core_began))
    try:
        with open(RECORD) as f:
            for line in f:
                p = line.split(None, 1)
                if len(p) == 2 and p[0].isdigit():
                    note(int(p[0]), norm(p[1]))
    except OSError:
        pass


def grow():  # add the descendants of each recorded pid that is still the same process
    t = table()
    live = [p for p, b in known.items() if p in t and t[p][1] == b]
    kids = {}
    for p, (pp, b) in t.items():
        kids.setdefault(pp, []).append(p)
    new = []
    while live:
        for c in kids.get(live.pop(), []):
            if note(c, t[c][1]):
                new.append(c)
                live.append(c)
    if new and os.path.isdir(S):
        try:
            with open(RECORD, "a") as f:
                for c in new:
                    f.write("{} {}\n".format(c, known[c]))
        except OSError:
            pass


def alive():
    t = table()
    return sorted(p for p, b in known.items() if p in t and t[p][1] == b)


def send(sig):
    grow()
    for p in alive():
        if began(p) == known[p]:  # still the process this run started
            try:
                os.kill(p, sig)
            except OSError:
                pass


def reap():
    load()
    grow()
    if os.path.isdir(S + "/run"):
        env = dict(os.environ)
        for k in ("HC_TERMINAL_ID", "HC_SOCKET", "HC_TERMINAL_SOCKET", "HC_SCOPE", "CLAUDECODE"):
            env.pop(k, None)
        env.update(HC_SESSION=SESSION, XDG_STATE_HOME=S + "/state", XDG_CONFIG_HOME=S + "/config",
                   XDG_RUNTIME_DIR=S + "/run", HERDR_PA_HOME=S + "/pa", HERDR_APP_HOME=S + "/app",
                   CLAUDE_CONFIG_DIR=S + "/claude", PATH=S + "/bin:" + env.get("PATH", ""))
        subprocess.run(["hc", "server", "stop"], env=env, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(5):  # hc server stop ends them in a moment
        grow()
        if not alive():
            return
        time.sleep(1)
    send(signal.SIGTERM)
    time.sleep(3)
    for _ in range(3):  # KILL, again for anything started meanwhile
        grow()
        if not alive():
            return
        send(signal.SIGKILL)
        time.sleep(0.5)


if mode == "watch":
    def running(p):
        try:
            os.kill(p, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
    while running(check):
        load()
        grow()
        time.sleep(1)
    reap()
elif mode == "reap":
    reap()
else:
    load()
    grow()
    print(" ".join(str(p) for p in alive()))
sys.exit(0)
PY
