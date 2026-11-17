#!/usr/bin/env python3
"""Project Agent helpers for Herdr. Python standard library only (3.7+).

One file, selected by the name it is invoked as (symlinks in ~/.local/bin):

  pa-start [folder] [--profile P] [--space ID]   turn on a Project Agent for a space
  pa-session ...                                 the tab's program: runs the orchestrator, restarts it
  pa-claude [args]                               the profile's Claude command (workers, plain tabs)
  pa-codex [args]                                the profile's Codex command
  pa-profile [show|apply|set NAME]               show / apply to Herdr's programs file / set default
  pa-peers [--json] [--all]                      list Project Agents on this machine
  pa-pass [--retro] [--space ID]                 run a pass now (queued if the agent is mid-turn)
  pa-pass --pause | --resume                     pause or resume scheduled passes for the space
  pa-cleanup [--kill] [--dry-run] [--space ID]   remove finished workers whose evidence is recorded
  pa-doctor [--space ID]                         check a space's state files; exit 1 if any is broken
  pa-task list|done|reopen|park|delete|restore|purge   the person's own changes to tasks (see pa-task --help)
  pa-decide <card> --answer LABEL --quote TEXT   record a decision the person made in conversation
  pa-decide <card> --withdraw --why TEXT         close a card the conversation made moot
  pa-sync --on MACHINE [--dry-run]               copy core/, tools/ and profiles/ to another Herdr machine
  pa-core-check                                  fail if the generic core names a profile's tools
"""
import argparse
import datetime
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

HOME = os.path.expanduser("~")
ROOT = os.environ.get("HERDR_PA_HOME") or os.path.join(HOME, ".config", "herdr-pa")
APP = os.environ.get("HERDR_APP_HOME") or os.path.join(HOME, ".config", "herdr-app")
CLAUDE_HOME = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
ID = re.compile(r"^[A-Za-z0-9_-]+$")


class Problem(Exception):
    pass


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_text(path, text):
    """Atomic replace, so a reader never sees half a file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_json(path, value):
    write_text(path, json.dumps(value, indent=2) + "\n")


def backup(path, change):
    """Copy path to path.bak-<change> (numbered if taken). Returns the copy's path."""
    if not os.path.exists(path):
        return None
    target, n = "{}.bak-{}".format(path, change), 1
    while os.path.exists(target):
        n += 1
        target = "{}.bak-{}-{}".format(path, change, n)
    shutil.copy2(path, target)
    return target


def call(argv, check=True):
    try:
        r = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    except FileNotFoundError:
        raise Problem("{} is not installed or not on PATH".format(argv[0]))
    if check and r.returncode:
        raise Problem("{} failed: {}".format(" ".join(argv[:3]), (r.stderr or r.stdout).strip()))
    return r.stdout


def ident(value, what):
    if not value or not ID.match(value):
        raise Problem("invalid {}: {!r}".format(what, value))
    return value


def local_id(value):
    """A space or tab id as this machine's labels spell it (no machine/ prefix)."""
    return (value or "").split("/")[-1]


# ---- profiles ---------------------------------------------------------------------------------

def config():
    return read_json(os.path.join(ROOT, "config.json"), {}) or {}


def load_profile(name=None):
    name = ident(name or os.environ.get("HERDR_PA_PROFILE") or config().get("defaultProfile"), "profile name")
    folder = os.path.join(ROOT, "profiles", name)
    p = read_json(os.path.join(folder, "profile.json"))
    if not p or p.get("name") != name:
        raise Problem("no profile {!r} in {}".format(name, os.path.join(ROOT, "profiles")))
    for kind in ("claude", "codex"):
        v = p.get(kind)
        if not isinstance(v, list) or not v or not all(isinstance(x, str) and x for x in v):
            raise Problem("profile {}: {!r} must be a non-empty list of strings".format(name, kind))
    p["_folder"] = folder
    return p


def hc_get(address):
    if not address:
        return {}
    try:
        return json.loads(call(["hc", "get", address, "--json"]))
    except (Problem, ValueError):
        return {}


def status_word(info):
    """hc's status of a terminal as a word. `hc ls` gives a word; `hc get` gives an object
    {app, state, msg}. '' when there is none, or it is in neither shape."""
    status = (info or {}).get("status")
    if isinstance(status, dict):
        status = status.get("state")
    return status if isinstance(status, str) else ""


def space_dir(space):
    return os.path.join(ROOT, "spaces", ident(local_id(space), "space id"))


def profile_for_terminal(explicit=None):
    """--profile, else the Project Agent this terminal is or was started by, else the default."""
    if explicit:
        return load_profile(explicit), None
    seen, address, space = set(), os.environ.get("HC_TERMINAL_ID"), None
    while address and address not in seen and len(seen) < 6:
        seen.add(address)
        info = hc_get(address)
        labels = info.get("labels") or {}
        if labels.get("herdr.pa.profile"):
            return load_profile(labels["herdr.pa.profile"]), (labels.get("herdr.pa") if len(seen) > 1 else None)
        space = space or labels.get("herdr.space")
        address = info.get("parent")
    # A plain tab in a space that has a Project Agent uses that space's profile.
    if space and ID.match(local_id(space)):
        chosen = (read_json(os.path.join(ROOT, "spaces", local_id(space), "agent.json"), {}) or {}).get("profile")
        if chosen:
            return load_profile(chosen), None
    return load_profile(), None


def resolve(argv, p):
    """argv with its program as an absolute path: PATH, then the profile's extra folders.
    Never the shim that called us (PA_SHIM), or `claude` would start itself forever."""
    argv = [os.path.expanduser(argv[0])] + list(argv[1:])
    if "/" in argv[0]:
        if not os.access(argv[0], os.X_OK):
            raise Problem("profile command not found: " + argv[0])
        return argv
    skip = {os.path.realpath(x) for x in (os.environ.get("PA_SHIM"), os.path.abspath(sys.argv[0])) if x}

    def is_shim(path):  # a `claude` shim that routes back here, wherever it was put
        try:
            with open(path, "rb") as f:
                return b"PA_SHIM" in f.read(2048)
        except OSError:
            return False
    folders = os.environ.get("PATH", "").split(os.pathsep) + [os.path.expanduser(x) for x in p.get("path", [])]
    for folder in folders:
        candidate = os.path.join(folder, argv[0])
        if (os.path.isfile(candidate) and os.access(candidate, os.X_OK) and os.path.realpath(candidate) not in skip
                and not is_shim(candidate)):
            return [candidate] + argv[1:]
    raise Problem("profile {}: command {!r} not found on PATH or in {}".format(p["name"], argv[0], p.get("path", [])))


def read_text(path):
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        return ""


def worker_commands(p):
    return ("Claude workers: `pa-claude` (it runs this profile's Claude command, `{}`, and adds the worker rules and model). "
            "Codex workers: `pa-codex` (runs `{}`).").format(" ".join(p["claude"]), " ".join(p["codex"]))


def plain_steering(p):
    claude, codex = " ".join(p["claude"]), " ".join(p["codex"])
    return ("You are running inside Herdr. To start worker agents, use hc (run `hc --skill` once for usage), not your "
            "built-in subagents, so they show in Herdr's Agents views. Always launch Claude workers with the `{0}` "
            "command and Codex workers with `{1}`: for example `hc run --bg --name <name> --cwd <dir> {0} \"<task>. "
            "Report with hc send $HC_TERMINAL_ID when done or stuck.\"`.").format(claude, codex)


def layered(p, role, directory=None, agent=None):
    """Core, then the profile, then the space: later layers add, never remove."""
    parts = [read_text(os.path.join(ROOT, "core", role + ".md")).strip()]
    context = read_text(os.path.join(p["_folder"], "context.md")).strip()
    facts = ["# This setup",
             "- Profile: {}. Its data: {}".format(p["name"], os.path.join(p["_folder"], "profile.json")),
             "- Core files: {} (orchestrator.md, worker.md, sdlc.md)".format(os.path.join(ROOT, "core")),
             "- " + worker_commands(p)]
    for role_name, model in sorted((p.get("models") or {}).items()):
        facts.append("- Default {} model: {}".format(role_name, model))
    cr = p.get("changeRequest") or {}
    if cr:
        facts.append("- A change request here is a {} ({}).".format(cr.get("name", "change request"), cr.get("tool", "")))
    if p.get("ci"):
        facts.append("- CI checks here: " + ", ".join(p["ci"]) + ".")
    tests = p.get("tests") or {}
    if tests:
        facts.append("- Tests: affected = `{}`, full suite = `{}`.".format(tests.get("affected", ""), tests.get("full", "")))
    if directory:
        facts.append("- Your state folder: {} (agent.json, ledger.json, journal.jsonl, decisions/, rules.json, "
                     "evidence/, schedule.json). Read it before acting.".format(directory))
    if agent:
        facts.append("- Space: {} ({}). Project folder: {}. Your terminal address: {}.".format(
            agent.get("name"), agent.get("space"), agent.get("project"), agent.get("terminal")))
    parts.append("\n".join(facts))
    if context:
        parts.append("# Profile context ({})\n\n{}".format(p["name"], context))
    parts.append("The core safety rules above are a floor. Profile and space rules add to them and cannot remove them.")
    return "\n\n".join(x for x in parts if x)


# ---- state ------------------------------------------------------------------------------------

def registry_update(change):
    os.makedirs(ROOT, exist_ok=True)
    with open(os.path.join(ROOT, ".registry.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = os.path.join(ROOT, "registry.json")
        reg = read_json(path, None) or {"version": 1, "agents": []}
        reg["agents"] = change(reg.get("agents", []))
        write_json(path, reg)


def live_terminals():
    """address -> hc get record, for every live terminal on this machine."""
    try:
        listed = json.loads(call(["hc", "ls", "--json"])).get("terminals", [])
    except (Problem, ValueError):
        return None
    out = {}
    for t in listed:
        if t.get("state") == "live":
            out[t["address"]] = hc_get(t["address"])
    return out


# ---- launcher liveness and the resume rule ----------------------------------------------------
# The launcher writes spaces/<id>/heartbeat ("<epoch> <address>") every 10 s while it runs, and
# session.log lines "<iso> start resume=... session=<id> ..." and "<iso> exit code=C after=Ns".

FRESH = 30  # a heartbeat older than this many seconds means its launcher is gone
RAN = 20  # a session that ran this long exists and can be resumed


def read_heartbeat(directory):
    """(epoch, address) from the space's heartbeat file, or None."""
    try:
        parts = read_text(os.path.join(directory, "heartbeat")).split()
        return (int(parts[0]), parts[1]) if len(parts) >= 2 else None
    except ValueError:
        return None


def launcher_alive(directory, address, fresh=FRESH, now=None):
    """The space's launcher runs in `address`: the heartbeat names it and is at most `fresh` s old."""
    hb = read_heartbeat(directory)
    if not hb or not address or hb[1] != local_id(address):
        return False
    return (time.time() if now is None else now) - hb[0] <= fresh


def other_launcher_alive(directory, address, fresh=FRESH):
    """launcher_alive, but a heartbeat that is getting old (a beat missed, or its launcher just
    died) is watched until it either moves again or passes `fresh`, so a launcher that is
    replacing a dead one is not refused because it started a few seconds too early."""
    deadline = time.time() + fresh
    while True:
        hb = read_heartbeat(directory)
        if not hb or not address or hb[1] != local_id(address):
            return False
        age = time.time() - hb[0]
        if age > fresh:
            return False
        if age <= 12 or time.time() >= deadline:
            return True
        time.sleep(2)


def parse_time(text):
    try:
        return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        return None


READ_FILE = object()


def session_ran(directory, session_id, before=None, heartbeat=READ_FILE):
    """True when session.log shows that session_id ran for at least RAN seconds: its last start
    line is followed by an exit after >= RAN s, or (killed while running: no exit line) the
    heartbeat kept beating RAN s past the start. A heartbeat from `before` (epoch s) on is this
    launcher's own, not the one that ran the session, and does not count. `heartbeat`: the
    (epoch, address) the previous launcher left, read before this one wrote its own (default: the file)."""
    if not session_id:
        return False
    lines = read_text(os.path.join(directory, "session.log")).splitlines()
    start, last_start = None, None
    for i, line in enumerate(lines):
        words = line.split()
        if len(words) >= 2 and words[1] == "start":
            last_start = i
            if "session=" + session_id in words[2:]:
                start = i
    if start is None:
        return False
    for line in lines[start + 1:]:
        words = line.split()
        if len(words) >= 2 and words[1] == "start":
            break
        if len(words) >= 2 and words[1] == "exit":
            m = re.search(r"\bafter=(\d+)s\b", line)
            return bool(m and int(m.group(1)) >= RAN)
    began = parse_time(lines[start].split()[0])
    hb = read_heartbeat(directory) if heartbeat is READ_FILE else heartbeat
    if hb and before is not None and hb[0] >= int(before):
        hb = None
    return bool(start == last_start and began is not None and hb and hb[0] >= began + RAN)


def claude_slug(path):
    """Claude's folder name for a project under ~/.claude/projects."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def transcript_state(project, session_id):
    """True / False: Claude's transcript for session_id exists / does not. None: Claude's layout
    here is unknown (no projects folder, or none for this project). Existence only; never read."""
    folder = os.path.join(CLAUDE_HOME, "projects", claude_slug(project))
    if not session_id or not os.path.isdir(folder):
        return None
    return os.path.isfile(os.path.join(folder, session_id + ".jsonl"))


def should_resume(agent, directory, project, before=None, heartbeat=READ_FILE):
    """(resume?, why): why is flag | ran | transcript | new. `before`: when this launcher started
    (its own heartbeat is not evidence that an earlier session ran)."""
    sid = agent.get("sessionId")
    if not sid:
        return False, "new"
    if agent.get("sessionStarted"):
        return True, "flag"
    if session_ran(directory, sid, before, heartbeat):
        return True, "ran"
    if transcript_state(project, sid) is True:
        return True, "transcript"
    return False, "new"


def after_exit(agent, resume, code, lasted, quick_resume_fails, transcript):
    """agent.json after the agent exited. One quick failed resume is not proof the session is
    gone (a crash, a network error): only a missing transcript or a second one in a row is."""
    if lasted >= RAN:
        agent["sessionStarted"] = True
    elif resume and code != 0 and (transcript is False or quick_resume_fails >= 2):
        agent["sessionStarted"] = False  # nothing to resume: start a new session next
        agent.pop("sessionId", None)
    return agent


def live_spaces(terms):
    return {(t.get("labels") or {}).get("herdr.space") for t in terms.values()} - {None}


def find_orphan(project, terms):
    """State left by a closed space in the same project folder (newest first), if any."""
    if terms is None:
        return None
    alive, best = live_spaces(terms), None
    base = os.path.join(ROOT, "spaces")
    for name in os.listdir(base) if os.path.isdir(base) else []:
        agent = read_json(os.path.join(base, name, "agent.json"))
        if not agent or agent.get("project") != project or name in alive:
            continue
        if agent.get("terminal") in terms:
            continue
        if not best or agent.get("updated", "") > best[1].get("updated", ""):
            best = (name, agent)
    return best


def seed_rules(p):
    rules = []
    for source, path in (("core", os.path.join(ROOT, "core", "rules.default.json")),
                         ("profile:" + p["name"], os.path.join(p["_folder"], "rules.json"))):
        for r in (read_json(path, {}) or {}).get("rules", []):
            r = dict(r)
            r.setdefault("status", "active")
            r.setdefault("source", source)
            r.setdefault("history", [{"at": now(), "by": source, "change": "seeded"}])
            rules.append(r)
    for i, r in enumerate(rules):
        r.setdefault("order", (len(rules) - i) * 10)
    return {"version": 1, "rules": rules}


def init_state(space, project, p, name=None, terminal=None, tab=None, adopt=True, terms=None):
    """Create (or adopt, or update) spaces/<space>/ and register the agent. Returns (dir, agent)."""
    space = ident(local_id(space), "space id")
    directory = space_dir(space)
    adopted = None
    if adopt and not os.path.exists(os.path.join(directory, "agent.json")):
        orphan = find_orphan(project, terms)
        if orphan:
            os.makedirs(os.path.dirname(directory), exist_ok=True)
            if os.path.isdir(directory):
                os.rmdir(directory)  # only ever an empty leftover; fails loudly otherwise
            os.rename(os.path.join(ROOT, "spaces", orphan[0]), directory)
            adopted = orphan[0]
    for sub in ("decisions", "evidence"):
        os.makedirs(os.path.join(directory, sub), exist_ok=True)
    path = os.path.join(directory, "agent.json")
    agent = read_json(path, {}) or {}
    if agent.get("project") and agent["project"] != project:
        raise Problem("space {} already has a Project Agent for {}".format(space, agent["project"]))
    if agent.get("profile") and agent["profile"] != p["name"]:
        raise Problem("this space's Project Agent uses profile {!r}; edit {} to change it".format(agent["profile"], path))
    agent.update({"version": 1, "space": space, "project": project, "profile": p["name"],
                  "name": name or agent.get("name") or os.path.basename(project.rstrip("/")) or "project",
                  "updated": now()})
    agent.setdefault("created", now())
    if terminal:
        agent.pop("closed", None)  # a launcher runs here again: the space is open (Close Project Agent marks it)
        agent["terminal"] = terminal
        # Every terminal this agent has run in: workers started from an earlier one are still its own.
        agent["terminals"] = ([t for t in agent.get("terminals", []) if t != terminal] + [terminal])[-20:]
    if tab:
        agent["tab"] = tab
    if adopted:
        agent["adoptedFrom"] = adopted
    write_json(path, agent)
    seeds = {
        "ledger.json": lambda: json.dumps({"version": 1, "passCount": 0, "tasks": [], "updated": now()}, indent=2) + "\n",
        "journal.jsonl": lambda: "",
        "rules.json": lambda: json.dumps(seed_rules(p), indent=2) + "\n",
        "schedule.json": lambda: json.dumps({"everyMinutes": 5, "paused": False, "onlyWhileActive": True,
                                             "quietHours": None, "lastPass": None, "nextPass": None}, indent=2) + "\n",
    }
    for name_, make in seeds.items():
        target = os.path.join(directory, name_)
        if not os.path.exists(target):
            write_text(target, make())
    entry = {k: agent.get(k) for k in ("space", "name", "project", "profile", "terminal", "tab", "updated")}
    entry["address"] = agent.get("terminal")
    registry_update(lambda agents: [a for a in agents if a.get("space") not in (space, adopted)] + [entry])
    return directory, agent


def session_log(directory, text):
    with open(os.path.join(directory, "session.log"), "a") as f:
        f.write("{} {}\n".format(now(), text))


# ---- commands ---------------------------------------------------------------------------------

def mcp_args(p):
    """A profile's own MCP servers (profile.json "mcpServers"), added to whatever the person has configured."""
    servers = p.get("mcpServers")
    return ["--mcp-config", json.dumps({"mcpServers": servers})] if isinstance(servers, dict) and servers else []


def exec_plain(p, extra=()):
    """A plain Claude tab: the profile's command with the worker steering, no Project Agent."""
    argv = resolve(list(p["claude"]) + mcp_args(p) + ["--append-system-prompt", plain_steering(p)] + list(extra), p)
    os.execv(argv[0], argv)


def ask_setup(p, project, address):
    """A new space's first tab: set up the Project Agent now, or pick another kind of session.
    Herdr's window shows a setup page for this (it sees the herdr.pa.ask label and sends the
    choice here); the same choice can be typed. Returns the project folder to set up, or does
    not return (the tab becomes the chosen session)."""
    home_dir = os.path.realpath(HOME)
    at_home = project == home_dir
    claude, codex = " ".join(p["claude"]), " ".join(p["codex"])
    call(["hc", "label", address, "herdr.pa.ask=" + ("home" if at_home else "project"), "herdr.pa.ask.folder=" + project,
          "herdr.pa.ask.claude=" + claude, "herdr.pa.ask.codex=" + codex], check=False)

    def done():
        call(["hc", "label", address, "herdr.pa.ask=done"], check=False)

    print("\nNew space in {}".format(project.replace(home_dir, "~", 1)))
    print("Choose in the setup page in Herdr's window. Or type one of these and press Enter:")
    if not at_home:
        print("  here             set up a Project Agent for this folder")
    print("  other <folder>   set up a Project Agent for another folder")
    print("  claude           no Project Agent: open Claude ({})".format(claude))
    print("  codex            no Project Agent: open Codex ({})".format(codex))
    print("  shell            no Project Agent: a plain terminal")
    numbers = {"1": "here", "3": "claude", "4": "codex", "5": "shell"}
    while True:
        try:
            answer = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            done()
            sys.exit(0)
        word, _, rest = answer.partition(" ")
        word = numbers.get(word.lower(), word.lower()) or ("claude" if at_home else "here")
        if word == "here" and not at_home:
            done()
            return project
        if word in ("other", "2"):
            folder = os.path.realpath(os.path.expanduser(rest.strip())) if rest.strip() else ""
            if not folder or not os.path.isdir(folder) or folder == home_dir:
                print("Give a project folder: other <folder>")
                continue
            os.chdir(folder)
            done()
            return folder
        if word == "claude":
            done()
            exec_plain(p)
        if word == "codex":
            done()
            argv = resolve(list(p["codex"]), p)
            os.execv(argv[0], argv)
        if word == "shell":
            done()
            print("You can start a Project Agent later from the Project panel, or with pa-start.")
            sys.exit(0)  # the tab's own shell takes over
        print("Not one of the choices above.")


def error_wait(address, problem):
    """pa-session could not start: show why in the tab and wait for Enter, instead of leaving a
    plain shell that looks like a Project Agent. The label tells the keeper (and people) why."""
    message = " ".join(str(problem).split())
    signal.signal(signal.SIGINT, signal.default_int_handler)  # Ctrl-C stops the wait, even after the launcher ignored it
    call(["hc", "label", address, "herdr.pa.error=" + message[:120], "herdr.pa.starting="], check=False)
    print("\n[Project Agent] It cannot start: {}\n"
          "               Fix the cause, then press Enter to try again.".format(message), flush=True)
    try:
        input()
    except EOFError:
        time.sleep(60)
    call(["hc", "label", address, "herdr.pa.error="], check=False)  # `key=` removes the label


def cmd_session(args):
    address = os.environ.get("HC_TERMINAL_ID")
    info = hc_get(address)
    labels = info.get("labels") or {}
    space = local_id(args.space or labels.get("herdr.space"))
    project = os.path.realpath(os.path.expanduser(args.project or os.getcwd()))
    ctx = {"directory": None, "beat": False}
    if args.auto or not address or not space or not sys.stdin.isatty():
        return run_session(args, address, labels, space, project, ctx)  # no one to show an error to: as before
    try:
        while True:
            # Tell the keeper this launcher has begun, before the slow steps (hc calls, the launch
            # lock, another launcher's heartbeat), so a slow start is not taken for a plain shell.
            if not args.ask:  # a new space's first tab asks first; it is never a restore
                call(["hc", "label", address, "herdr.pa.starting=" + space], check=False)
            try:
                return run_session(args, address, labels, space, project, ctx)
            except (Problem, OSError) as e:
                error_wait(address, e)
    except KeyboardInterrupt:  # the person stops it (in error-wait or before): the tab's shell takes over
        sys.exit(2)
    finally:  # however it ends, it no longer says it is starting or waiting
        call(["hc", "label", address, "herdr.pa.error=", "herdr.pa.starting="], check=False)


def launch_lock(space, wait=60):
    """An exclusive lock, per space, around "is another launcher alive?" up to "this one is labelled
    and has beaten once", so two launchers started at the same moment cannot both pass the check.
    Outside spaces/<id>/ so init_state can still move a folder into place. Waits at most `wait` s:
    a launcher stuck inside it must not block every later one without a word."""
    os.makedirs(ROOT, exist_ok=True)
    f = open(os.path.join(ROOT, ".launch-{}.lock".format(ident(local_id(space), "space id"))), "a")
    deadline = time.time() + wait
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except (BlockingIOError, PermissionError):
            if time.time() >= deadline:
                f.close()
                raise Problem("another pa-session for this space is still starting (waited {} s)".format(wait))
            time.sleep(0.5)
        except OSError:  # e.g. ENOLCK on a network home: error-wait shows it
            f.close()
            raise


def run_session(args, address, labels, space, project, ctx):
    started = ctx.setdefault("started", time.time())  # heartbeats from this launcher's first start on are its own
    existing = read_json(os.path.join(ROOT, "spaces", space, "agent.json"), {}) if space and ID.match(space) else {}
    p = load_profile(args.profile or (existing or {}).get("profile"))
    # A space made in the home folder has no project: a plain Claude tab (pa-start turns one on).
    if args.auto and (not address or not space or project == os.path.realpath(HOME)):
        exec_plain(p)
    if not address or not space:
        raise Problem("pa-session runs in a Herdr tab (no HC_TERMINAL_ID or herdr.space label here)")
    directory = os.path.join(ROOT, "spaces", space) if ID.match(space) else None
    if args.ask:
        # A new space: ask first. Not when this space already has an agent (a second tab is a plain one).
        running = (existing or {}).get("terminal")
        info_other = hc_get(running) if running and running != address else {}
        if (info_other.get("state") == "live" and (info_other.get("labels") or {}).get("herdr.pa") == space
                and directory and launcher_alive(directory, running)):
            exec_plain(p)
        if sys.stdin.isatty():
            project = ask_setup(p, project, address)
            args.ask = False  # asked once; a retry after an error does not ask again
        elif project == os.path.realpath(HOME):
            exec_plain(p)
    if not os.path.isdir(project):
        raise Problem("project folder does not exist: " + project)
    lock = launch_lock(space)
    try:
        directory, agent, previous = claim(args, address, labels, space, project, p, existing, directory)
    finally:
        lock.close()  # releases the lock
    # The heartbeat the previous launcher left: the only sign that a killed session ran. Kept from
    # this launcher's first claim; on a retry the file holds this launcher's own beats.
    ctx.setdefault("previous", previous)
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl-C belongs to the agent, not this wrapper
    run_agent(args, address, project, p, directory, agent, ctx, started)


def claim(args, address, labels, space, project, p, existing, directory):
    """The one-per-space check, then state, labels and a first heartbeat (under launch_lock)."""
    terms = live_terminals()
    other = (existing or {}).get("terminal")
    # One per space: another live terminal labelled for it whose launcher still beats. A labelled
    # terminal whose launcher is gone (a shell left behind) is what this one replaces.
    if (other and other != address and terms and other in terms and (terms[other].get("labels") or {}).get("herdr.pa") == space
            and directory and other_launcher_alive(directory, other)):
        # A second tab in the same space is an ordinary Claude tab.
        if args.auto:
            exec_plain(p)
        raise Problem("this space already has a Project Agent in terminal {}".format(other))
    directory, agent = init_state(space, project, p, args.name, address, local_id(labels.get("herdr.tab")), terms=terms)
    previous = read_heartbeat(directory)  # before this launcher's first beat replaces it
    call(["hc", "label", address, "herdr.pa=" + space, "herdr.pa.profile=" + p["name"],
          "herdr.pa.project=" + project, "herdr.pa.name=" + agent["name"], "herdr.pa.starting="])
    write_text(os.path.join(directory, "heartbeat"), "{} {}\n".format(int(time.time()), address))
    return directory, agent, previous


def run_agent(args, address, project, p, directory, agent, ctx, started):
    running = ctx.setdefault("running", {"since": None, "session": None, "marked": False})
    ctx["directory"] = directory

    def beat():
        # The tab bar reads this: a live terminal whose heartbeat stopped has lost its launcher.
        # Also keeps the last ledger that parsed, so a half-written or mangled one can be restored.
        ledger, seen = os.path.join(directory, "ledger.json"), None
        while True:
            try:
                write_text(os.path.join(directory, "heartbeat"), "{} {}\n".format(int(time.time()), address))
                # A session that has run for 20 s exists: record it now, so a tab that is killed
                # (this launcher with it) still resumes the same conversation when it reopens.
                if running["since"] and not running["marked"] and time.time() - running["since"] >= RAN:
                    current = read_json(os.path.join(directory, "agent.json"), {}) or {}
                    if current.get("sessionId") == running["session"]:
                        current["sessionStarted"] = True
                        write_json(os.path.join(directory, "agent.json"), current)
                    running["marked"] = True
                stamp = os.path.getmtime(ledger)
                if stamp != seen:
                    seen = stamp
                    text = read_text(ledger)
                    if isinstance(json.loads(text).get("tasks"), list):
                        write_text(os.path.join(directory, "ledger.last-good.json"), text)
            except (OSError, ValueError, AttributeError):
                pass
            time.sleep(10)
    if not ctx["beat"]:  # one heartbeat thread, also when a start is retried after an error
        threading.Thread(target=beat, daemon=True).start()
        ctx["beat"] = True
    quick, quick_resume_fails = 0, 0
    while True:
        agent = read_json(os.path.join(directory, "agent.json"), agent)
        resume, why = should_resume(agent, directory, project, before=started, heartbeat=ctx.get("previous"))
        if args.fresh:
            resume, why = False, "fresh"
        if not resume:
            agent["sessionId"], agent["sessionStarted"] = str(uuid.uuid4()), False
        args.fresh = False
        agent["updated"] = now()
        write_json(os.path.join(directory, "agent.json"), agent)
        argv = list(p["claude"])
        model = (agent.get("models") or {}).get("orchestrator") or (p.get("models") or {}).get("orchestrator")
        if model:
            argv += ["--model", model]
        argv += mcp_args(p)
        argv += ["--resume", agent["sessionId"]] if resume else ["--session-id", agent["sessionId"]]
        argv += ["--name", "PA: " + agent["name"], "--append-system-prompt", layered(p, "orchestrator", directory, agent)]
        if resume:
            argv += ["Your session was restarted. Read your state folder (ledger.json, journal.jsonl, decisions/, "
                     "rules.json), then say in one line where things stand. Do not start new work until asked."]
        argv = resolve(argv, p)
        session_log(directory, "start resume={} session={} reason={}".format(resume, agent["sessionId"], why))
        began = time.time()
        running.update(since=began, session=agent["sessionId"], marked=False)
        code = subprocess.call(argv, cwd=project)
        running["since"] = None
        lasted = time.time() - began
        session_log(directory, "exit code={} after={:.0f}s".format(code, lasted))
        if lasted >= RAN:
            quick, quick_resume_fails = 0, 0
        else:
            quick += 1
            quick_resume_fails = quick_resume_fails + 1 if resume and code != 0 else 0
        session = agent["sessionId"]
        agent = read_json(os.path.join(directory, "agent.json"), agent)
        transcript = transcript_state(project, session) if resume and code != 0 and lasted < RAN else None
        agent = after_exit(agent, resume, code, lasted, quick_resume_fails, transcript)
        write_json(os.path.join(directory, "agent.json"), agent)
        print("\n[Project Agent] The agent exited (code {}). It restarts here; close the space to end it.".format(code), flush=True)
        if quick >= 3:
            print("[Project Agent] It exited {} times within seconds, so it is not restarting by itself.\n"
                  "               Fix the cause (see the output above), then press Enter to try again.".format(quick), flush=True)
            try:
                input()
            except EOFError:
                time.sleep(60)
            quick = 0
        else:
            time.sleep(3)


def cmd_start(args):
    address = os.environ.get("HC_TERMINAL_ID")
    info = hc_get(address)
    space = local_id(args.space or (info.get("labels") or {}).get("herdr.space"))
    if not space:
        raise Problem("run pa-start in a Herdr terminal, or pass --space <id> (see `herdr-app ls`)")
    project = os.path.realpath(os.path.expanduser(args.folder or os.getcwd()))
    if not os.path.isdir(project):
        raise Problem("not a folder: " + project)
    existing = read_json(os.path.join(space_dir(space), "agent.json"), {}) or {}
    p = load_profile(args.profile or existing.get("profile"))
    terms = live_terminals() or {}
    running = existing.get("terminal")
    # A labelled terminal whose launcher stopped beating is a shell left behind, not a Project Agent.
    if (running in terms and (terms[running].get("labels") or {}).get("herdr.pa") == space
            and launcher_alive(space_dir(space), running)):
        print("This space already has a Project Agent (terminal {}).".format(running))
        return
    session = ["pa-session", "--space", space, "--project", project, "--profile", p["name"]]
    if args.dry_run:
        print(json.dumps({"space": space, "project": project, "profile": p["name"], "argv": session}, indent=2))
        return
    if shutil.which("herdr-app"):
        # The space as the app names it (a machine/ prefix when it lives elsewhere).
        made = json.loads(call(["herdr-app", "do", "tab.new", json.dumps(
            {"space": args.app_space or space, "folder": project, "name": "PA: " + os.path.basename(project), "argv": session})]))
        if made.get("error"):
            raise Problem(made["error"])
        print("Project Agent tab opened: {}".format(json.dumps(made)))
    elif sys.stdin.isatty() and address:
        print("No herdr-app on this machine: this tab becomes the Project Agent tab.", flush=True)
        os.execvp(session[0], session)
    else:
        raise Problem("run pa-start in a Herdr tab's shell on this machine")


def cmd_launch(kind, argv):
    explicit = None
    if len(argv) >= 2 and argv[0] == "--profile":
        explicit, argv = argv[1], argv[2:]
    p, pa_space = profile_for_terminal(explicit)
    command = list(p[kind]) + list(argv)
    # Subcommands and one-shot flags (`claude mcp list`, `--version`) pass through untouched.
    passthrough = bool(argv) and argv[0] in (
        "--version", "-v", "--help", "-h", "agents", "auth", "auto-mode", "config", "doctor", "install", "mcp",
        "plugin", "plugins", "setup", "setup-token", "update", "upgrade", "attach", "gateway", "import", "logs",
        "purge", "respawn", "sessions", "ultrareview")
    if kind == "claude" and not passthrough:
        if pa_space and ID.match(pa_space):
            directory = space_dir(pa_space)
            agent = read_json(os.path.join(directory, "agent.json"), {}) or {}
            model = (agent.get("models") or {}).get("worker") or (p.get("models") or {}).get("worker")
            if model and not any(a == "--model" or a.startswith("--model=") for a in argv):
                command += ["--model", model]
            text = layered(p, "worker", None, None) + "\n\nThe Project Agent's state folder is {}. Write only inside evidence/<your task id>/ there.".format(directory)
        else:
            text = plain_steering(p)
        if "--append-system-prompt" not in argv:
            command += ["--append-system-prompt", text]
        if "--mcp-config" not in argv:
            command += mcp_args(p)
    command = resolve(command, p)
    if os.environ.get("PA_PRINT"):  # show what would run, for checking a setup
        print(json.dumps([a if len(a) < 80 else a[:77] + "..." for a in command]))
        return
    os.execv(command[0], command)


def cmd_profile(args):
    if args.action == "set":
        ident(args.name, "profile name")
        load_profile(args.name)
        cfg = config()
        cfg["defaultProfile"] = args.name
        write_json(os.path.join(ROOT, "config.json"), cfg)
        print("Default profile: {}. Run `pa-profile apply` to update Herdr's Claude entry.".format(args.name))
        return
    p = load_profile(args.profile)
    if args.action == "apply":
        # Herdr's Claude entry runs the launcher, not one profile's command: each tab then gets the
        # command and steering of its own space's profile (the default profile where a space has none).
        path = os.path.join(APP, "programs.json")
        current = read_json(path, {}) or {}
        run = ["pa-claude"]
        programs = current.setdefault("programs", {})
        if (programs.get("claude") or {}).get("run") == run:
            print("Herdr's Claude entry already runs pa-claude (default profile {}: {}).".format(p["name"], " ".join(p["claude"])))
            return
        kept = backup(path, "pa-profile")
        programs.setdefault("claude", {})["run"] = run
        write_json(path, current)
        print("Herdr's Claude entry now runs pa-claude, which starts each space's profile command "
              "(default profile {}: {}). Previous file: {}".format(p["name"], " ".join(p["claude"]), kept))
        return
    shown = {k: v for k, v in p.items() if not k.startswith("_")}
    if args.json:
        print(json.dumps(shown, indent=2))
    else:
        print("profile: {}\nclaude:  {}\ncodex:   {}\nmodels:  {}".format(p["name"], " ".join(p["claude"]), " ".join(p["codex"]), json.dumps(p.get("models", {}))))


def cmd_peers(args):
    agents = (read_json(os.path.join(ROOT, "registry.json"), {}) or {}).get("agents", [])
    terms = live_terminals() or {}
    rows = []
    for a in agents:
        t = terms.get(a.get("terminal"))
        a = dict(a, live=bool(t and (t.get("labels") or {}).get("herdr.pa") == a.get("space")))
        if a["live"] or args.all:
            rows.append(a)
    if args.json:
        print(json.dumps(rows, indent=2))
    elif not rows:
        print("No Project Agents running on this machine." + ("" if args.all else " (--all lists stopped ones)"))
    else:
        for a in rows:
            print("{}\t{}\t{}\t{}\t{}{}".format(a.get("address"), a.get("name"), a.get("project"), a.get("profile"), a.get("space"), "" if a["live"] else "\t(stopped)"))


def cmd_pass(args):
    """An on-demand pass: sent now if the agent is free, else queued for Herdr's pass timer."""
    space, seen, address = local_id(args.space or ""), set(), os.environ.get("HC_TERMINAL_ID")
    while not space and address and address not in seen and len(seen) < 6:
        seen.add(address)
        info = hc_get(address)
        space = (info.get("labels") or {}).get("herdr.pa") or (info.get("labels") or {}).get("herdr.space") or ""
        address = info.get("parent")
    directory = space_dir(space) if space else None
    agent = read_json(os.path.join(directory, "agent.json")) if directory else None
    if not agent:
        raise Problem("no Project Agent for this space; pass --space <id> (see pa-peers)")
    term = hc_get(agent.get("terminal"))
    if term.get("state") != "live" or (term.get("labels") or {}).get("herdr.pa") != agent["space"]:
        raise Problem("the Project Agent for {} is not running".format(agent.get("name")))
    request_path = os.path.join(directory, "pass-request.json")
    if args.pause or args.resume:
        schedule_path = os.path.join(directory, "schedule.json")
        schedule = read_json(schedule_path, {}) or {}
        schedule["paused"] = bool(args.pause)
        write_json(schedule_path, schedule)
        print("Scheduled passes {} for {}. On-demand passes still run.".format("paused" if args.pause else "resumed", agent.get("name")))
        return
    if not launcher_alive(directory, agent["terminal"]):  # a labelled terminal can be a bare shell its launcher left
        raise Problem("the Project Agent for {} is not running (its launcher's heartbeat is missing or old)".format(agent.get("name")))
    if status_word(term) in ("working", "blocked"):
        old = read_json(request_path, {}) or {}
        write_json(request_path, {"pending": True, "retro": bool(args.retro or (old.get("pending") and old.get("retro"))), "at": now(), "by": "pa-pass"})
        print("The Project Agent is mid-turn: the pass is queued and runs when the turn ends (Herdr's window must be open).")
        return
    call(["hc", "send", agent["terminal"], "manual pass with retrospective" if args.retro else "manual pass"])
    schedule_path = os.path.join(directory, "schedule.json")
    schedule = read_json(schedule_path, {}) or {}
    every = max(5, int(schedule.get("everyMinutes") or 0)) if schedule.get("everyMinutes") else 0
    stamp = datetime.datetime.now(datetime.timezone.utc)
    schedule["lastPass"] = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    schedule["nextPass"] = (stamp + datetime.timedelta(minutes=every)).strftime("%Y-%m-%dT%H:%M:%SZ") if every else None
    write_json(schedule_path, schedule)
    print("Pass requested{}.".format(" (with retrospective)" if args.retro else ""))


def space_for_command(explicit):
    space, seen, address = local_id(explicit or ""), set(), os.environ.get("HC_TERMINAL_ID")
    while not space and address and address not in seen and len(seen) < 6:
        seen.add(address)
        info = hc_get(address)
        space = (info.get("labels") or {}).get("herdr.pa") or (info.get("labels") or {}).get("herdr.space") or ""
        address = info.get("parent")
    directory = space_dir(space) if space else None
    agent = read_json(os.path.join(directory, "agent.json")) if directory else None
    if not agent:
        raise Problem("no Project Agent state for this space; pass --space <id> (see pa-peers --all)")
    return directory, agent


def cmd_cleanup(args):
    """Stale workers: a finished worker terminal is removed once its task's evidence folder has files.
    A live worker of a done or parked task is only listed, unless --kill."""
    directory, agent = space_for_command(args.space)
    tasks = {t.get("id"): t for t in (read_json(os.path.join(directory, "ledger.json"), {}) or {}).get("tasks", []) if t.get("id")}
    mine = set(agent.get("terminals", [])) | {agent.get("terminal")}
    listed = json.loads(call(["hc", "ls", "--all", "--json"])).get("terminals", [])
    removed, kept = [], []
    for t in listed:
        info = hc_get(t["address"])
        name = info.get("name") or ""
        task = next((tid for tid in sorted(tasks, key=len, reverse=True) if name.startswith(tid + "-")), None)
        recorded_worker = any(w.get("address") == t["address"] for task_ in tasks.values() for w in task_.get("workers", []) or [])
        if not task or not (info.get("parent") in mine or recorded_worker):
            continue
        evidence = os.path.join(directory, "evidence", task)
        has_evidence = os.path.isdir(evidence) and any(os.path.getsize(os.path.join(evidence, f)) > 0 for f in os.listdir(evidence)
                                                       if os.path.isfile(os.path.join(evidence, f)))
        finished = info.get("state") == "finished"
        task_over = tasks[task].get("state") in ("done", "parked")
        row = {"address": t["address"], "name": name, "task": task, "state": info.get("state"), "evidence": has_evidence}
        entry = next((w for w in tasks[task].get("workers") or [] if w.get("address") == t["address"]), {})
        if entry.get("keep"):  # the person said to keep it (pa-worker keep): never removed here, even with --kill
            kept.append(dict(row, why="marked keep"))
        elif not has_evidence:
            kept.append(dict(row, why="no evidence recorded yet"))
        elif finished or (task_over and args.kill):
            if not args.dry_run:
                if not finished:
                    call(["hc", "kill", t["address"]], check=False)
                call(["hc", "rm", t["address"]], check=False)
            removed.append(row)
        else:
            kept.append(dict(row, why="still running" + ("; its task is {} (--kill ends it)".format(tasks[task].get("state")) if task_over else "")))
    for row in removed:
        print("{} {} {} ({})".format("would remove" if args.dry_run else "removed", row["address"], row["name"], row["task"]))
    for row in kept:
        print("kept {} {} ({}): {}".format(row["address"], row["name"], row["task"], row["why"]))
    if not removed and not kept:
        print("No worker terminals of this Project Agent to clean up.")


def cmd_doctor(args):
    """Checks one space's state files. For the orchestrator after a restart or a compaction, and for people."""
    directory, agent = space_for_command(args.space)
    states = {"queued", "investigating", "in-progress", "needs-decision", "review", "done", "parked"}
    problems, notes = [], []

    def load(name, required=True):
        path = os.path.join(directory, name)
        if not os.path.exists(path):
            if required:
                problems.append("{}: missing".format(name))
            return None
        try:
            with open(path) as f:
                return json.load(f)
        except ValueError as e:
            problems.append("{}: not valid JSON ({})".format(name, e))
            return None

    ledger = load("ledger.json")
    if ledger is not None:
        if not isinstance(ledger.get("tasks"), list):
            problems.append("ledger.json: no tasks list")
        else:
            ids = [t.get("id") for t in ledger["tasks"]]
            for t in ledger["tasks"]:
                if not t.get("id") or t.get("state") not in states:
                    problems.append("ledger.json: task {!r} has state {!r}".format(t.get("id"), t.get("state")))
            if len(ids) != len(set(ids)):
                problems.append("ledger.json: two tasks share an id")
            notes.append("{} tasks, pass {}".format(len(ids), ledger.get("passCount", 0)))
    if any(p.startswith("ledger.json") for p in problems) and os.path.exists(os.path.join(directory, "ledger.last-good.json")):
        notes.append("ledger.last-good.json holds the last ledger that parsed; compare it with journal.jsonl before restoring")
    rules = load("rules.json")
    if rules is not None and not isinstance(rules.get("rules"), list):
        problems.append("rules.json: no rules list")
    schedule = load("schedule.json")
    if schedule is not None and schedule.get("everyMinutes") not in (None, 0) and not (isinstance(schedule.get("everyMinutes"), (int, float)) and schedule["everyMinutes"] >= 5):
        problems.append("schedule.json: everyMinutes must be 0 (off) or at least 5")
    bad_lines = 0
    for line in read_text(os.path.join(directory, "journal.jsonl")).splitlines():
        try:
            json.loads(line) if line.strip() else None
        except ValueError:
            bad_lines += 1
    if bad_lines:
        problems.append("journal.jsonl: {} line(s) are not JSON".format(bad_lines))
    open_cards = 0
    decisions = os.path.join(directory, "decisions")
    for name in sorted(os.listdir(decisions)) if os.path.isdir(decisions) else []:
        if not name.endswith(".json"):
            continue
        card = load(os.path.join("decisions", name))
        if card is None:
            continue
        if not isinstance(card.get("question"), str) or not isinstance(card.get("options"), list) or card.get("status") not in ("open", "answered", "withdrawn"):
            problems.append("decisions/{}: needs question, options and status open|answered|withdrawn".format(name))
        elif card["status"] == "open":
            open_cards += 1
    notes.append("{} open decision(s)".format(open_cards))
    term = hc_get(agent.get("terminal"))
    running = term.get("state") == "live" and (term.get("labels") or {}).get("herdr.pa") == agent.get("space")
    notes.append("agent {} in terminal {}".format("running" if running else "NOT running", agent.get("terminal")))
    print("{} ({})".format(agent.get("name"), directory))
    for n in notes:
        print("  " + n)
    for p in problems:
        print("  PROBLEM " + p)
    print("  state files: {}".format("ok" if not problems else "{} problem(s)".format(len(problems))))
    sys.exit(1 if problems else 0)


def folder_size(path):
    total = 0
    for folder, _dirs, names in os.walk(path):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(folder, name))
            except OSError:
                pass
    return total


def cmd_task(args):
    """The person's own changes to tasks, from the panel or a terminal. The orchestrator is told,
    so it re-reads the ledger instead of writing an old copy over the change.
    A deleted task goes to trash/ with its evidence and decision cards; purge empties the trash."""
    directory, agent = space_for_command(args.space)
    ledger_path, trash = os.path.join(directory, "ledger.json"), os.path.join(directory, "trash")
    human = lambda n: "{:.1f} MB".format(n / 1e6) if n >= 1e6 else "{} KB".format(max(1, n // 1000)) if n else "empty"
    if args.action == "list":
        ledger = read_json(ledger_path, {}) or {}
        for t in ledger.get("tasks", []):
            ev = os.path.join(directory, "evidence", str(t.get("id")))
            print("{}\t{}\t{}\tevidence {}".format(t.get("id"), t.get("state"), t.get("title", "")[:60], human(folder_size(ev)) if os.path.isdir(ev) else "none"))
        known = {str(t.get("id")) for t in ledger.get("tasks", [])}
        base = os.path.join(directory, "evidence")
        for name in sorted(os.listdir(base)) if os.path.isdir(base) else []:
            if name not in known:
                print("(no task)\t-\tevidence/{} {}: left by a task that is gone".format(name, human(folder_size(os.path.join(base, name)))))
        for name in sorted(os.listdir(trash)) if os.path.isdir(trash) else []:
            print("trash\t{}\t{}".format(name, human(folder_size(os.path.join(trash, name)))))
        return
    if args.action in ("delete", "purge") and any(os.environ.get(k) for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_SANDBOX")):
        raise Problem("pa-task {} is the person's command; an agent asks them instead".format(args.action))
    if args.action == "purge":
        gone = 0
        for name in sorted(os.listdir(trash)) if os.path.isdir(trash) else []:
            if args.task and not name.startswith(args.task + "@"):
                continue
            target = os.path.realpath(os.path.join(trash, name))
            if os.path.dirname(target) != os.path.realpath(trash):  # only ever direct children of trash/
                continue
            shutil.rmtree(target)
            gone += 1
        print("Permanently deleted {} item(s) from the trash.".format(gone))
        return
    if not args.task:
        raise Problem("pa-task {} needs a task id".format(args.action))
    ident(args.task, "task id")
    with open(os.path.join(directory, ".ledger.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ledger = read_json(ledger_path)
        if not ledger or not isinstance(ledger.get("tasks"), list):
            raise Problem("ledger.json is missing or broken; run pa-doctor")
        task = next((t for t in ledger["tasks"] if t.get("id") == args.task), None)
        stamp, note = now(), None
        if args.action == "restore":
            found = sorted(n for n in (os.listdir(trash) if os.path.isdir(trash) else []) if n.startswith(args.task + "@"))
            if task or not found:
                raise Problem("task {} is {}".format(args.task, "still in the ledger" if task else "not in the trash"))
            source = os.path.join(trash, found[-1])
            task = read_json(os.path.join(source, "task.json"))
            if os.path.isdir(os.path.join(source, "evidence")):
                os.rename(os.path.join(source, "evidence"), os.path.join(directory, "evidence", args.task))
            for name in os.listdir(os.path.join(source, "decisions")) if os.path.isdir(os.path.join(source, "decisions")) else []:
                os.rename(os.path.join(source, "decisions", name), os.path.join(directory, "decisions", name))
            shutil.rmtree(source)
            task["updated"] = stamp
            ledger["tasks"].append(task)
            before, after = "trash", task.get("state")
        else:
            if not task:
                raise Problem("no task {} in the ledger".format(args.task))
            before = task.get("state")
            if args.action == "delete":
                terms = live_terminals() or {}
                busy = [w.get("address") for w in task.get("workers") or [] if w.get("address") in terms]
                if busy:
                    raise Problem("workers are still running for {} ({}); end them first".format(args.task, ", ".join(busy)))
                target = os.path.join(trash, "{}@{}".format(args.task, stamp.replace(":", "")))
                os.makedirs(os.path.join(target, "decisions"))
                write_json(os.path.join(target, "task.json"), task)
                evidence = os.path.join(directory, "evidence", args.task)
                if os.path.isdir(evidence):
                    os.rename(evidence, os.path.join(target, "evidence"))
                decisions = os.path.join(directory, "decisions")
                for name in os.listdir(decisions) if os.path.isdir(decisions) else []:
                    card = read_json(os.path.join(decisions, name)) if name.endswith(".json") else None
                    if card and card.get("task") == args.task:
                        os.rename(os.path.join(decisions, name), os.path.join(target, "decisions", name))
                ledger["tasks"] = [t for t in ledger["tasks"] if t.get("id") != args.task]
                after = "deleted"
                if task.get("worktree"):
                    note = "Its worktree {} was left in place.".format(task["worktree"])
            else:
                after = {"done": "done", "park": "parked", "reopen": "queued"}[args.action]
                if before == after:
                    print("{} is already {}.".format(args.task, after))
                    return
                if args.action == "reopen" and before not in ("done", "parked"):
                    raise Problem("{} is {}; only done or parked tasks are reopened".format(args.task, before))
                task["state"], task["updated"] = after, stamp
        ledger["updated"] = stamp
        write_json(ledger_path, ledger)
        with open(os.path.join(directory, "journal.jsonl"), "a") as journal:
            journal.write(json.dumps({"at": stamp, "by": "person", "trigger": "panel", "task": args.task, "from": before, "to": after,
                                      "why": "changed by the person with pa-task " + args.action}) + "\n")
    print("{}: {} -> {}.{}".format(args.task, before, after, " " + note if note else ""))
    term = hc_get(agent.get("terminal"))
    if term.get("state") != "live" or (term.get("labels") or {}).get("herdr.pa") != agent.get("space") or not launcher_alive(directory, agent.get("terminal")):
        print("(The Project Agent is not running; it reads the ledger when it starts.)")
    else:
        try:
            call(["hc", "send", agent["terminal"], "The person changed task {} from {} to {} in the panel. Re-read ledger.json before you "
                  "write it again, and do not undo this.".format(args.task, before, after)])
        except Problem:
            print("(The Project Agent could not be told right now; it reads the ledger on its next pass.)")


def cmd_decide(args):
    """Closes an open decision card from the conversation: with the person's answer in their own
    words, or as withdrawn. For the orchestrator; every use is journaled and shown on the card."""
    directory, agent = space_for_command(args.space)
    ident(args.card, "card id")
    path = os.path.join(directory, "decisions", args.card + ".json")
    card = read_json(path)
    if not card:
        raise Problem("no decision card {}".format(args.card))
    if card.get("status") != "open":
        raise Problem("{} is already {}".format(args.card, card.get("status")))
    stamp = now()
    if args.withdraw:
        if not (args.why or "").strip():
            raise Problem("--withdraw needs --why \"<reason>\"")
        card.update(status="withdrawn", answer=None, note=args.why.strip(), answered=stamp, answeredBy="orchestrator: withdrawn")
        what = "withdrawn"
    else:
        if card.get("worktree"):
            raise Problem("a card that deletes a worktree is answered in the panel, not in conversation")
        if card.get("kind") == "leftovers":
            raise Problem("a card that closes workers is answered in the panel, not in conversation")
        labels = [o.get("label") for o in card.get("options") or []]
        match = next((l for l in labels if str(l).lower() == str(args.answer or "").strip().lower()), None)
        if match is None:
            raise Problem("--answer must be one of this card's options: {}".format(", ".join(map(str, labels))))
        quote = (args.quote or "").strip()
        if len(quote) < 2:
            raise Problem("--quote needs the person's own words that decide it")
        card.update(status="answered", answer=match, note=quote, answered=stamp, answeredBy="person, in conversation")
        what = "answered {}".format(match)
        # A rule proposal's card decides the rule too, as it does in the panel.
        decided = "active" if str(match).lower().startswith("approve") else "declined" if str(match).lower().startswith("decline") else None
        if card.get("rule") and decided:
            rules_path = os.path.join(directory, "rules.json")
            rules = read_json(rules_path) or {}
            for r in rules.get("rules", []):
                if r.get("id") == card["rule"] and r.get("status") == "proposed":
                    r["status"] = decided
                    r.setdefault("history", []).append({"at": stamp, "by": "person", "change": ("approved" if decided == "active" else "declined") + " in conversation"})
            if rules.get("rules") is not None:
                write_json(rules_path, rules)
    write_json(path, card)
    with open(os.path.join(directory, "journal.jsonl"), "a") as journal:
        journal.write(json.dumps({"at": stamp, "by": "orchestrator", "trigger": "conversation", "task": card.get("task"), "decision": args.card,
                                  "why": "{}: {}".format(what, card["note"])}) + "\n")
    print("{} {} ({}).".format(args.card, what, card["answeredBy"]))


def cmd_sync(args):
    """Each machine has its own copy of ~/.config/herdr-pa. This copies the shared parts (not
    config.json, the registry or any space's state) to another machine known to hc."""
    files = ["core-check.json"] if os.path.isfile(os.path.join(ROOT, "core-check.json")) else []
    for top in ("core", "tools", "profiles"):
        for folder, _dirs, names in os.walk(os.path.join(ROOT, top)):
            for name in sorted(names):
                if ".bak-" in name or name.endswith(".pyc") or name.startswith("."):
                    continue
                files.append(os.path.relpath(os.path.join(folder, name), ROOT))
    for rel in files:
        if args.dry_run:
            print("would copy " + rel)
            continue
        with open(os.path.join(ROOT, rel), "rb") as source:
            r = subprocess.run(["hc", "file", "write", "--on", args.on, os.path.join(".config", "herdr-pa", rel)],
                               stdin=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if r.returncode:
            raise Problem("copying {} failed: {}".format(rel, r.stderr.decode(errors="replace").strip()))
    if args.dry_run:
        return
    # Scripts must be runnable there, and every command linked where that machine's core looks.
    here = sorted(n for n in os.listdir(os.path.join(HOME, ".local", "bin")) if n.startswith("pa-")
                  and os.path.islink(os.path.join(HOME, ".local", "bin", n)))
    links = " ".join("ln -sfn \"$HOME/.config/herdr-pa/tools/{}\" \"$HOME/.local/bin/{}\";".format(
        os.path.basename(os.path.realpath(os.path.join(HOME, ".local", "bin", n))), n) for n in here)
    script = ("cd \"$HOME/.config/herdr-pa\" && find core tools profiles -type f \\( -name '*.sh' -o -name '*.py' \\) -exec chmod +x {} + ; "
              "mkdir -p \"$HOME/.local/bin\" spaces; " + links + " [ -f config.json ] || echo '{\"version\": 1, \"defaultProfile\": \"" + ident(config().get("defaultProfile"), "profile name") + "\"}' > config.json")
    call(["hc", "exec", "--on", args.on, "--", "sh", "-c", script])
    print("Copied {} files to {} and linked {} commands. Running agents there pick up prompt changes when their session restarts.".format(
        len(files), args.on, len(here)))


def cmd_core_check(_args):
    """The generic layer must not name any profile's tools. Each profile lists its own terms
    (profile.json "coreCheck"), so the word list never lives in the generic layer itself; an
    optional core-check.json beside the profiles adds more."""
    terms = list((read_json(os.path.join(ROOT, "core-check.json"), {}) or {}).get("forbidden", []))
    base = os.path.join(ROOT, "profiles")
    for name in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        terms += [t for t in (read_json(os.path.join(base, name, "profile.json"), {}) or {}).get("coreCheck", []) if isinstance(t, str) and t]
    terms = sorted(set(terms))
    if not terms:
        print("core check: no profile declares terms to keep out of the generic layer; nothing to check")
        return
    pattern = re.compile(r"(?i)(?<![a-z0-9])(" + "|".join(re.escape(t) for t in terms) + r")(?![a-z0-9])")
    roots = [os.path.join(ROOT, "core"), os.path.join(ROOT, "tools")]
    if os.path.isdir(os.path.join(APP, "mods")):  # a machine with only the core has no app folder
        roots.append(os.path.join(APP, "mods", "project-agent"))
    hits, files = [], 0
    for root in roots:
        if not os.path.isdir(root):
            hits.append(root + ": missing")
            continue
        for folder, _dirs, names in os.walk(root):
            for name in names:
                if ".bak-" in name or not name.endswith((".md", ".json", ".js", ".py", ".sh")):
                    continue
                files += 1
                path = os.path.join(folder, name)
                with open(path, errors="replace") as f:
                    for n, line in enumerate(f, 1):
                        m = pattern.search(line)
                        if m:
                            hits.append("{}:{}: {!r}".format(path, n, m.group(1)))
    if hits:
        print("\n".join(hits), file=sys.stderr)
        raise Problem("{} profile-specific term(s) in the generic layer".format(len(hits)))
    print("core check: {} files in {} folders, 0 profile-specific terms".format(files, len(roots)))


def main():
    kind = os.path.basename(sys.argv[0])
    if kind in ("pa.py", "pa") and len(sys.argv) > 1:
        kind, sys.argv = "pa-" + sys.argv[1], [sys.argv[0]] + sys.argv[2:]
    if kind in ("pa-claude", "pa-codex"):
        return cmd_launch(kind[3:], sys.argv[1:])
    parser = argparse.ArgumentParser(prog=kind)
    if kind == "pa-start":
        parser.add_argument("folder", nargs="?", help="the project folder (default: here)")
        parser.add_argument("--profile")
        parser.add_argument("--space", help="a space id on this machine (default: this terminal's)")
        parser.add_argument("--app-space", help=argparse.SUPPRESS)
        parser.add_argument("--dry-run", action="store_true")
        return cmd_start(parser.parse_args())
    if kind == "pa-session":
        for name in ("space", "project", "profile", "name"):
            parser.add_argument("--" + name)
        parser.add_argument("--auto", action="store_true", help="a plain Claude tab when the folder is home")
        parser.add_argument("--ask", action="store_true", help="a new space: ask whether to set one up, or what to open instead")
        parser.add_argument("--fresh", action="store_true", help="start a new conversation")
        return cmd_session(parser.parse_args())
    if kind == "pa-profile":
        parser.add_argument("action", nargs="?", default="show", choices=("show", "apply", "set"))
        parser.add_argument("name", nargs="?")
        parser.add_argument("--profile")
        parser.add_argument("--json", action="store_true")
        return cmd_profile(parser.parse_args())
    if kind == "pa-peers":
        parser.add_argument("--json", action="store_true")
        parser.add_argument("--all", action="store_true")
        return cmd_peers(parser.parse_args())
    if kind == "pa-pass":
        parser.add_argument("--retro", action="store_true", help="also review the rules")
        parser.add_argument("--pause", action="store_true", help="pause scheduled passes")
        parser.add_argument("--resume", action="store_true", help="resume scheduled passes")
        parser.add_argument("--space")
        return cmd_pass(parser.parse_args())
    if kind == "pa-cleanup":
        parser.add_argument("--kill", action="store_true", help="also end live workers of done or parked tasks")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--space")
        return cmd_cleanup(parser.parse_args())
    if kind == "pa-doctor":
        parser.add_argument("--space")
        return cmd_doctor(parser.parse_args())
    if kind == "pa-task":
        parser.description = ("list: tasks with their evidence size, and the trash. done / park / reopen <id>: change a task's state. "
                              "delete <id>: move the task, its evidence and its decision cards to the trash. restore <id>: bring it back. "
                              "purge [<id>]: permanently delete what is in the trash.")
        parser.add_argument("action", choices=("list", "done", "reopen", "park", "delete", "restore", "purge"))
        parser.add_argument("task", nargs="?")
        parser.add_argument("--space")
        return cmd_task(parser.parse_args())
    if kind == "pa-decide":
        parser.add_argument("card")
        parser.add_argument("--answer", help="one of the card's option labels")
        parser.add_argument("--quote", help="the person's own words that decide it")
        parser.add_argument("--withdraw", action="store_true", help="close the card without an answer")
        parser.add_argument("--why", help="why it no longer applies (with --withdraw)")
        parser.add_argument("--space")
        args = parser.parse_args()
        if not args.withdraw and not args.answer:
            raise Problem("give --answer LABEL --quote TEXT, or --withdraw --why TEXT")
        return cmd_decide(args)
    if kind == "pa-sync":
        parser.add_argument("--on", required=True, help="the machine's name in `hc machine ls`")
        parser.add_argument("--dry-run", action="store_true")
        return cmd_sync(parser.parse_args())
    if kind == "pa-core-check":
        return cmd_core_check(parser.parse_args())
    raise Problem("invoke as pa-start, pa-session, pa-claude, pa-codex, pa-profile, pa-peers, pa-pass, pa-cleanup, pa-doctor, pa-task, pa-decide, pa-sync or pa-core-check")


if __name__ == "__main__":
    try:
        main()
    except Problem as e:
        print("{}: {}".format(os.path.basename(sys.argv[0]), e), file=sys.stderr)
        sys.exit(2)
