#!/usr/bin/env python3
"""Project Agent helpers for Herdr. Python standard library only (3.7+).

One file, selected by the name it is invoked as (symlinks in ~/.local/bin):

  pa-start [folder] [--profile P] [--space ID]   turn on a Project Agent for a space
  pa-session ...                                 the tab's program: runs the orchestrator, restarts it
  pa-claude [args]                               the profile's Claude command (workers, plain tabs)
  pa-codex [args]                                the profile's Codex command
  pa-profile [show|apply|set NAME]               show / apply to Herdr's programs file / set default
  pa-peers [--json] [--all] [--older]            list Project Agents on this machine (and who runs older code)
  pa-pass [--retro] [--space ID]                 run a pass now (queued if the agent is mid-turn)
  pa-pass --pause | --resume                     pause or resume scheduled passes for the space
  pa-cleanup [--kill] [--dry-run] [--space ID]   remove finished workers whose evidence is recorded
  pa-doctor [--space ID]                         check a space's state files; exit 1 if any is broken
  pa-task list|done|reopen|park|next|pause|unpause|delete|restore|purge|archive|unarchive   the person's own changes to tasks
  pa-decide <card> --answer LABEL|Other --quote TEXT   record a decision the person made in conversation
  pa-decide <card> --withdraw --why TEXT         close a card the conversation made moot (or answered Other)
  pa-sync --on MACHINE [--dry-run]               copy core/, tools/ and profiles/ to another Herdr machine
  pa-core-check                                  fail if the generic core names a profile's tools
  pa-stamp [--commit C] [--dirty]                write installed.json: hashes of the installed tools, core, mods
  pa-restart [--space ID] [--on M] [--now] [--fresh] [--why TEXT] [--wait S] [--dry-run]
                                                 restart the space's Project Agent in place (terminal, workers stay)
  pa-context set [--mode fresh|off] [--net N] ...   the Project Agent's context settings (t-041; the person's)
  pa-context compact-instructions|start-pack|compacted   the Project Agent's compaction hooks (Claude Code runs them)
  pa-context fresh [--why TEXT] [--withdraw] [--dry-run]   a fresh context at its next clean point (t-041 J4)
  pa-context followup [--since T]   the fresh reset's follow-up report (t-041 R17; read-only)
  pa-restart --space ID --end-terminal [--anyway] [--dry-run]
                                                 for a launcher from before restart support only: end its terminal
                                                 (and its workers), after a typed yes; the keeper or pa-start reopens it

In pa_costs.py (see its docstring), linked as pa-costs: cost tracking per space (collect, report,
reconstruct, prices, billed ...). Tokens are measured; money is an estimate from the profile's price table.

In pa_push.py (see its docstring): pa-push-main plan|check|run, the person's push card, and
pa-push-allow show|add|set|remove|on|off, the person's push allowlist. pa-decide refuses a push card, and a queue
card that pushes (t-140).
"""
import argparse
import datetime
import errno
import fcntl
import json
import os
import math
import re
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
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


def iso_time(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


# ---- decision cards and their archive (t-054) ------------------------------------------------------------------
# Old answered and withdrawn cards are moved by the person (pa-task archive) from decisions/ to decisions-archive/, a
# sibling the panel reads only on request. Open cards never move. Every tool that reads a card by id reads it through
# card_file, so an archived card still counts (an answered Keep is never asked again); every tool that writes one
# writes through put_card (card_for_write under .archive.lock), so a card a tool changes is in decisions/ again, an open
# card is never archived, and a write never lands beside a copy pa-task archive moved meanwhile (t-131: one copy).
# Lock order: the push lock (when taken), then .archive.lock; pa-task archive takes them in that order too.
ARCHIVE = "decisions-archive"
ARCHIVE_DAYS = 14


def card_file(directory, card_id):
    """Where card `card_id` is: decisions/<id>.json, else decisions-archive/<id>.json if only that exists."""
    live = os.path.join(directory, "decisions", card_id + ".json")
    old = os.path.join(directory, ARCHIVE, card_id + ".json")
    return old if not os.path.lexists(live) and os.path.lexists(old) else live


def move_card(src, dst):
    """One card file moved as is (a rename: a writer's replace after it makes a new file and loses nothing)."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.rename(src, dst)


def archive_lock(directory):
    """<space>/.archive.lock, held while pa-task archive runs and while a card comes back from the archive.
    Returns the open file; close it to unlock."""
    f = open(os.path.join(directory, ".archive.lock"), "a")
    fcntl.flock(f, fcntl.LOCK_EX)
    return f


def card_for_write(directory, card_id):
    """decisions/<id>.json, the only place a tool writes a card: an archived copy, when it is the only one, is moved
    back there first."""
    live = os.path.join(directory, "decisions", card_id + ".json")
    if not os.path.lexists(live):
        old = os.path.join(directory, ARCHIVE, card_id + ".json")
        if os.path.lexists(old):
            lock = archive_lock(directory)
            try:
                if not os.path.lexists(live) and os.path.lexists(old):
                    move_card(old, live)
            finally:
                lock.close()
    return live


def put_card(directory, card_id, card):
    """Writes card `card_id` to decisions/ under .archive.lock (t-131): pa-task archive moves cards only under that lock,
    so it never moves the card between this write's path and the write itself (which would leave two copies)."""
    lock = archive_lock(directory)
    try:
        live = os.path.join(directory, "decisions", card_id + ".json")
        old = os.path.join(directory, ARCHIVE, card_id + ".json")
        if not os.path.lexists(live) and os.path.lexists(old):
            move_card(old, live)
        write_json(live, card)
    finally:
        lock.close()
    return live


def write_card(directory, path, card):
    """Writes a card read from `path` (in decisions/ or the archive) to decisions/ (put_card)."""
    put_card(directory, os.path.basename(path)[:-len(".json")], card)


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


CALL_TIMEOUT = None  # seconds a `call` may take; None: no limit. Only a detached waiter sets it (t-133, pa-worker --when-idle)


def call(argv, check=True):
    try:
        # stdin /dev/null: while Claude has the terminal, this launcher is a background group (t-184)
        r = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=CALL_TIMEOUT)
    except FileNotFoundError:
        raise Problem("{} is not installed or not on PATH".format(argv[0]))
    except subprocess.TimeoutExpired:
        raise Problem("{} did not answer in {} s".format(" ".join(argv[:3]), CALL_TIMEOUT))
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


def profile_for_terminal(explicit=None, walked=None):
    """--profile, else the Project Agent this terminal is or was started by, else the default.
    walked: a list that receives each `hc get` record read on the way (this terminal's first)."""
    if explicit:
        return load_profile(explicit), None
    seen, address, space = set(), os.environ.get("HC_TERMINAL_ID"), None
    while address and address not in seen and len(seen) < 6:
        seen.add(address)
        info = hc_get(address)
        if walked is not None:
            walked.append(dict(info, address=info.get("address") or address))
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
            # t-163 R9: a seeded rule is its file's, whatever the file says (never the person's: no standing approval)
            r["source"] = source
            r["history"] = [{"at": now(), "by": source, "change": "seeded"}]
            rules.append(r)
    for i, r in enumerate(rules):
        r.setdefault("order", (len(rules) - i) * 10)
    return {"version": 1, "rules": rules}


# ---- what must be on disk before a context is dropped (t-041 J2: R7, R8, R9) ----------------------------------

STANDING_FILE = "standing.md"
STANDING_HEADINGS = ("# Standing words", "# Open asks", "# Hold", "# Working notes")
STANDING_CAP = 6000  # characters (about 1.5k tokens)
WORKING_NOTES_MAX = 10
STANDING_ENTRY = re.compile(r"^- \d{4}-\d\d-\d\d")


def standing_seed():
    """standing.md as init_state writes it: the four headings, nothing under them."""
    return "\n\n".join(STANDING_HEADINGS) + "\n"


def parse_standing(text):
    """(sections, problems, warnings) of standing.md's text. sections: heading -> its non-blank lines. problems (a
    reset is refused on any, t-041 R2 (e)): over STANDING_CAP characters, one of the four headings missing or twice,
    another level-1 heading. warnings (pa-doctor notes only): an entry not starting `- YYYY-MM-DD`, text before the
    first heading, more than WORKING_NOTES_MAX working notes. The headings' order is free; `##` sub-headings and
    indented continuation lines are allowed."""
    sections, problems, warnings, current, seen = {}, [], [], None, []
    if len(text) > STANDING_CAP:
        problems.append("over the cap: {:,} of {:,} characters".format(len(text), STANDING_CAP))
    for line in text.splitlines():
        if line.startswith("# "):
            heading = line.rstrip()
            if heading not in STANDING_HEADINGS:
                problems.append("unknown heading {!r}".format(heading[:60]))
                current = None
            elif heading in seen:
                problems.append("heading {!r} twice".format(heading))
                current = heading
            else:
                seen.append(heading)
                sections[heading] = []
                current = heading
            continue
        if not line.strip():
            continue
        if current is None:
            if not seen and "text before the first heading" not in warnings:
                warnings.append("text before the first heading")
            continue
        sections[current].append(line)
        if line.startswith("- ") and not STANDING_ENTRY.match(line):
            warnings.append("entry without a date under {}: {!r}".format(current, line[:60]))
    for heading in STANDING_HEADINGS:
        if heading not in seen:
            problems.append("heading {!r} missing".format(heading))
    notes = [l for l in sections.get("# Working notes", []) if l.startswith("- ")]
    if len(notes) > WORKING_NOTES_MAX:
        warnings.append("{} working notes (at most {})".format(len(notes), WORKING_NOTES_MAX))
    return sections, problems, warnings


def read_standing(directory):
    """(text, sections, problems, warnings) of the space's standing.md; text None when it cannot be used whole.
    A file over 4 x the cap in bytes is not read. Never raises."""
    path = os.path.join(directory, STANDING_FILE)
    try:
        if not os.path.isfile(path):
            return None, {}, ["missing (the launcher creates it at its next start)"], []
        size = os.path.getsize(path)
        if size > 4 * STANDING_CAP:
            return None, {}, ["over the cap: {:,} bytes, the cap is {:,} characters".format(size, STANDING_CAP)], []
        with open(path, "rb") as f:
            text = f.read().decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return None, {}, ["unreadable ({})".format(type(e).__name__)], []
    sections, problems, warnings = parse_standing(text)
    return text, sections, problems, warnings


def checkpoint_problems(ctx):
    """What is wrong with the ledger's `context` checkpoint (t-041 R7), [] when it is well formed."""
    if not isinstance(ctx, dict):
        return ["not an object"]
    out = []
    n = ctx.get("pass")
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        out.append("pass must be a whole number")
    if not isinstance(ctx.get("at"), str) or parse_time(ctx["at"]) is None:
        out.append("at must be a UTC time")
    if ctx.get("session") is not None and not isinstance(ctx.get("session"), str):
        out.append("session must be a string or null")
    clean = ctx.get("clean")
    if not isinstance(clean, bool):
        out.append("clean must be true or false")
    if ctx.get("boundary") not in ("task", "pass", None):
        out.append("boundary must be task, pass or null")
    elif clean is True and ctx.get("boundary") is None:
        out.append("a clean checkpoint needs boundary task or pass")
    if clean is False and not (isinstance(ctx.get("why"), str) and ctx["why"].strip()):
        out.append("a checkpoint that is not clean needs why")
    return out


def handoff_file(directory, task_id, worker):
    """(found, expected) paths of a worker's handoff file evidence/<task>/handoff-<job>.md (t-041 R8), job = its
    name without "<task>-"; a second try "<job>-N" may use its job's file. (None, None) when the name says no job."""
    name = worker.get("name") if isinstance(worker, dict) else None
    prefix = "{}-".format(task_id)
    if not isinstance(name, str) or not task_id or not name.startswith(prefix) or name == prefix:
        return None, None
    job = job_slug(name[len(prefix):])
    folder = os.path.join(directory, "evidence", str(task_id))
    expected = os.path.join(folder, "handoff-{}.md".format(job))
    tries = [expected]
    base = re.sub(r"-\d+$", "", job)
    if base != job:
        tries.append(os.path.join(folder, "handoff-{}.md".format(base)))
    return next((p for p in tries if os.path.isfile(p)), None), expected


def checkpoint_line(ledger):
    """One line on the ledger's `context` checkpoint, as pa-doctor and the start pack (t-041 J3) say it."""
    ctx = ledger.get("context") if isinstance(ledger, dict) else None
    if ctx is None:
        return "checkpoint: none yet"
    if checkpoint_problems(ctx):
        return "checkpoint malformed: {}".format(checkpoint_problems(ctx)[0])
    if ctx["clean"]:
        return "checkpoint: pass {} at {}, clean, boundary {}{}".format(
            ctx["pass"], ctx["at"], ctx["boundary"], ", session " + ctx["session"][:8] if ctx.get("session") else "")
    return "checkpoint: pass {} at {}, not clean ({})".format(ctx["pass"], ctx["at"], ctx["why"][:80])


def context_notes(directory, ledger, environ=None):
    """pa-doctor's notes on what must be on disk (t-041 J2): this session's id, the checkpoint, standing.md, tasks whose
    `due` passed, and workers not finished without a handoff file. Notes only, never problems. Never raises."""
    notes = []
    try:
        environ = os.environ if environ is None else environ
        sid = environ.get("CLAUDE_CODE_SESSION_ID") or ""
        if re.match(r"^[A-Za-z0-9-]{8,64}$", sid):
            notes.append("this session: {}".format(sid))
        ledger = ledger if isinstance(ledger, dict) else {}
        notes.append(checkpoint_line(ledger))
        text, _sections, problems, warnings = read_standing(directory)
        if not problems and text is not None:
            notes.append("standing.md: {:,} of {:,} characters".format(len(text), STANDING_CAP))
        notes.extend("standing.md: {}".format(p) for p in problems[:5])
        notes.extend("standing.md: {}".format(w) for w in warnings[:3])
        tasks = [t for t in ledger.get("tasks") or [] if isinstance(t, dict)] if isinstance(ledger.get("tasks"), list) else []
        due, handoffs, stamp = [], [], time.time()
        for t in tasks:
            if t.get("due") is not None and t.get("state") not in ("done", "parked"):
                at = parse_time(t["due"]) if isinstance(t["due"], str) else None
                if at is None:
                    due.append("{} due {!r} is not a UTC time".format(t.get("id"), t["due"]))
                elif at <= stamp:
                    due.append("{} due {} (passed): {}".format(t.get("id"), t["due"], str(t.get("next") or "")[:80]))
            for w in t.get("workers") or []:
                if not isinstance(w, dict) or w.get("finished"):
                    continue
                found, expected = handoff_file(directory, t.get("id"), w)
                if expected and not found:
                    handoffs.append("worker {} ({} {}) has no handoff file {}".format(
                        w.get("address"), t.get("id"), os.path.basename(expected)[len("handoff-"):-len(".md")],
                        os.path.relpath(expected, directory)))
        blocks = []
        for t in tasks:
            for w in t.get("workers") or [] if isinstance(t.get("workers"), list) else []:
                if isinstance(w, dict) and not w.get("finished"):
                    found, expected = handoff_file(directory, t.get("id"), w)
                    why = handoff_block(directory, t.get("id"), found) if found else None
                    if why:
                        blocks.append("worker {} ({} {}) handoff {}".format(
                            w.get("address"), t.get("id"), os.path.basename(expected)[len("handoff-"):-len(".md")], why))
        for group in (due, handoffs, blocks):
            notes.extend(group[:5])
            if len(group) > 5:
                notes.append("and {} more".format(len(group) - 5))
    except Exception:  # noqa: BLE001 - notes only
        pass
    notes.extend(rules_notes(directory, ledger))
    return notes


RULES_BLOCK = "Space rules for this job"  # the first words of pa-handoff's rules block (t-190 R3, R7)
HANDOFF_READ_MAX = 64 * 1024  # bytes pa-doctor reads of a handoff (t-190 H13)


def handoff_block(directory, task_id, path):
    """None when the handoff at path has a line starting RULES_BLOCK; else what pa-doctor notes: 'has no rules block'
    or 'not read (<why>)' (a symlink, outside evidence/<task>/, not a file, over HANDOFF_READ_MAX, not UTF-8)."""
    folder = os.path.realpath(os.path.join(directory, "evidence", str(task_id)))
    if os.path.islink(path):
        return "not read (a symlink)"
    if os.path.dirname(os.path.realpath(path)) != folder:
        return "not read (outside evidence/{}/)".format(task_id)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError as e:
        return "not read ({})".format(e.strerror or type(e).__name__)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return "not read (not a file)"
        if st.st_size > HANDOFF_READ_MAX:
            return "not read (over 64 KB)"
        data = os.read(fd, HANDOFF_READ_MAX + 1)
    finally:
        os.close(fd)
    if len(data) > HANDOFF_READ_MAX:
        return "not read (over 64 KB)"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "not read (not UTF-8)"
    if any(line.startswith(RULES_BLOCK) for line in text.splitlines()):
        return None
    return "has no rules block"


def rules_notes(directory, ledger):
    """pa-doctor's notes on rules.json (t-190 R6, H4): the active rules (id, appliesTo, title), duplicate ids, and the
    rules with a history entry later than the checkpoint's `at` (I1: strictly later; a time not in the UTC Z form is
    listed 'time unreadable', H8). Nothing when rules.json gives no rules list (pa-doctor has that problem).
    Notes only; never raises."""
    notes = []
    try:
        active, why = read_rules(directory)
        if why:
            return notes
        listed = []
        for r in active:
            target = one_line(r.get("appliesTo"), 30) or "all"
            title = one_line(r.get("title"), RULE_TITLE_MAX)
            listed.append("{} ({}){}".format(r["id"], target, ": " + title if title else ""))
        notes.append(capped_list("rules: {} active: ".format(len(active)) if active else "rules: 0 active", listed,
                                 "", RULES_LINE_MAX))
        rules = [r for r in read_json(os.path.join(directory, "rules.json"), {})["rules"] if isinstance(r, dict)
                 and isinstance(r.get("id"), str) and RULE_ID.match(r["id"])]
        seen, dup = set(), []
        for r in rules:
            if r["id"] in seen and r["id"] not in dup:
                dup.append(r["id"])
            seen.add(r["id"])
        notes.extend("rules: duplicate id {}".format(rid) for rid in dup[:5])
        ctx = as_dict(ledger).get("context")
        if ctx is None or checkpoint_problems(ctx):
            notes.append("rules: no checkpoint; read every active rule")
            return notes
        since = parse_time(ctx["at"])
        changed = []
        for r in rules:
            for h in r.get("history") if isinstance(r.get("history"), list) else []:
                if not isinstance(h, dict):
                    continue
                at = parse_time(h["at"]) if isinstance(h.get("at"), str) else None
                if at is None:
                    changed.append("{} time unreadable".format(r["id"]))
                elif at > since:
                    change = one_line(h.get("change") if isinstance(h.get("change"), str) else h.get("what"), 30)
                    changed.append("{} {} {}".format(r["id"], change or "changed", h["at"]))
        if changed:
            notes.append(capped_list("rules changed since your checkpoint ({}): ".format(ctx["at"]), changed, "",
                                     RULES_LINE_MAX))
    except Exception:  # noqa: BLE001 - notes only
        pass
    return notes


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
        STANDING_FILE: standing_seed,
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


STATUSLINE_REFRESH = 30  # Claude Code's refreshInterval for the recording status line when the profile names none


def cc_statusline(p):
    """(config, problem) from the profile's "statusLine" key: the person's own status line command, which the recording
    status line runs inside (t-029 R3). (None, None) when the profile has none; (None, why) when it is malformed."""
    if "statusLine" not in p or p.get("statusLine") is None:
        return None, None
    v = p["statusLine"]
    if not isinstance(v, dict):
        return None, "statusLine must be an object"
    command = v.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(a, str) and a for a in command):
        return None, "statusLine.command must be a non-empty list of strings"
    refresh = v.get("refreshInterval", STATUSLINE_REFRESH)
    if isinstance(refresh, bool) or not isinstance(refresh, int) or not 1 <= refresh <= 300:
        return None, "statusLine.refreshInterval must be a whole number from 1 to 300"
    return {"command": list(command), "refreshInterval": refresh}, None


def cc_statusline_setting(p, space):
    """The "statusLine" setting that makes Claude Code's status line `pa-costs statusline`, which records the session's
    own cost figure and prints the profile's status line unchanged (t-029 R4); None when the profile has no valid
    statusLine, PA_NO_CC_STATUSLINE is set, or anything is missing. Never raises."""
    try:
        if os.environ.get("PA_NO_CC_STATUSLINE") not in (None, "", "0"):
            return None
        space = local_id(space or "")
        cfg, _problem = cc_statusline(p)
        if (not cfg or not ID.match(space) or not sys.executable or not os.path.isfile(COSTS_FILE)
                or not os.path.isfile(STATUSLINE_FILE)):
            return None
        inner = resolve(cfg["command"], p)  # as the profile's own commands are found; not found: no status line of ours
        # t-111: pa_statusline.py, which runs `pa-costs statusline` from compiled code it caches outside tools/
        command = " ".join([shlex.quote(sys.executable), shlex.quote(STATUSLINE_FILE), "statusline", "--space", space, "--then"]
                           + [shlex.quote(a) for a in inner])
        return {"type": "command", "command": command, "refreshInterval": cfg["refreshInterval"]}
    except Exception:  # noqa: BLE001 - the launch goes on without it
        return None


def cc_settings_args(p, space, argv):
    """["--settings", <json>] with the recording status line, for a worker's launch (t-029 R4); [] when the caller gave
    its own --settings or there is no status line of ours. Never raises: a launch never fails because of it."""
    try:
        if any(a == "--settings" or a.startswith("--settings=") for a in argv):
            return []
        status = cc_statusline_setting(p, space)
        return ["--settings", json.dumps({"statusLine": status}, separators=(",", ":"))] if status else []
    except Exception:  # noqa: BLE001 - the launch goes on without it
        return []


# t-040: the Project Agent's own session asks Claude Code for the one-hour prompt cache with the `promptCacheTtl`
# setting in its --settings (never an environment variable, which the workers it starts would inherit). Workers keep
# Claude Code's default. The profile key chooses: "1h" (the default when absent), "5m", or "default" (set nothing).
PA_CACHE_KEY = "orchestratorPromptCacheTtl"
PA_CACHE_DEFAULT = "1h"
CACHE_TTLS = ("5m", "1h")  # the values Claude Code takes; it ignores any other


def pa_cache_ttl(p):
    """(ttl, problem) for the Project Agent's promptCacheTtl from profile p: ("1h", None) when the key is absent,
    (value, None) for "1h" or "5m", (None, None) for "default", (None, why) for anything else (nothing is set)."""
    if PA_CACHE_KEY not in p:
        return PA_CACHE_DEFAULT, None
    v = p[PA_CACHE_KEY]
    if isinstance(v, str) and v in CACHE_TTLS:
        return v, None
    if v == "default":
        return None, None
    return None, '{} must be "1h", "5m" or "default"'.format(PA_CACHE_KEY)


def cache_in_effect(p, env):
    """(ttl, by): the main conversation's cache lifetime the Project Agent launched from p with environment env asks
    for, in Claude Code's documented order: FORCE_PROMPT_CACHING_5M, CLAUDE_CODE_PROMPT_CACHE_TTL, the setting (by
    "profile" when the profile names it, "pa" for this file's default), ENABLE_PROMPT_CACHING_1H, else ("default",
    "claude-code"). What is asked for, not what the provider gives (some models on some providers have no 1h)."""
    if env.get("FORCE_PROMPT_CACHING_5M") not in (None, "", "0"):
        return "5m", "FORCE_PROMPT_CACHING_5M"
    if env.get("CLAUDE_CODE_PROMPT_CACHE_TTL") in CACHE_TTLS:
        return env["CLAUDE_CODE_PROMPT_CACHE_TTL"], "CLAUDE_CODE_PROMPT_CACHE_TTL"
    ttl, _why = pa_cache_ttl(p)
    if ttl:
        return ttl, ("profile" if PA_CACHE_KEY in p else "pa")
    if env.get("ENABLE_PROMPT_CACHING_1H") not in (None, "", "0"):
        return "1h", "ENABLE_PROMPT_CACHING_1H"
    return "default", "claude-code"


def pa_settings(p, space, directory=None, version=None):
    """The Project Agent's own Claude Code settings: the recording status line (when there is one) and promptCacheTtl
    (t-040). One dict, given as one --settings, so another setting for the Project Agent only is added here. With the
    state folder (t-041 J3, R5/R12): the compaction hooks and, from Claude Code 2.1.290, idleCompaction false, unless
    context.json says mode off."""
    out = {}
    status = cc_statusline_setting(p, space)
    if status:
        out["statusLine"] = status
    try:
        ttl, _why = pa_cache_ttl(p)
    except Exception:  # noqa: BLE001 - the launch goes on without it
        ttl = None
    if ttl:
        out["promptCacheTtl"] = ttl
    if directory is not None:
        try:
            plan = context_launch(directory, version)
            if plan["idle"]:
                out["idleCompaction"] = False
            hooks = context_hooks(space) if plan["hooks"] else None
            if hooks:
                out["hooks"] = hooks
        except Exception:  # noqa: BLE001 - the launch goes on without them
            pass
    return out


def pa_settings_args(p, space, directory=None, version=None):
    """["--settings", <json>] for the Project Agent's own launch, or [] when there is nothing to set."""
    settings = pa_settings(p, space, directory, version)
    return ["--settings", json.dumps(settings, separators=(",", ":"))] if settings else []


# ---- t-041 J3: the Project Agent's context settings and the 400k net (R5, R12, R13) ------------------------------
# context.json in the state folder holds the person's settings (pa-context set; later the panel). With mode fresh
# (also when there is no file) the Project Agent's Claude gets --autocompact <net> (a flag: nothing it starts
# inherits it), and in its one --settings three hooks (PreCompact: what the summary keeps; SessionStart after a
# compaction: the start pack; PostCompact: a `compact` record) and idleCompaction false. The flag and the key need
# a minimum Claude Code version, read once per launcher process with `<claude> --version`; unknown: left out.
# mode off is the back-out: none of it.

CONTEXT_FILE = "context.json"
CONTEXT_DEFAULTS = {"mode": "fresh", "taskCap": 150000, "passCap": 250000, "net": 400000}
CONTEXT_MODES = ("fresh", "off")
CONTEXT_BY = ("person", "panel")
CONTEXT_CAPS = ("taskCap", "passCap", "net")
CAP_MIN, CAP_MAX = 100000, 900000
VERSION_TIMEOUT = 10  # seconds for `<claude> --version` (approved on the J3 plan: 10, as t-037's --help probe)
AUTOCOMPACT_SINCE = (2, 1, 221)  # --autocompact (cli-reference)
IDLE_COMPACTION_SINCE = (2, 1, 290)  # the idleCompaction setting (settings-reference)
START_PACK_MAX = 9000  # characters (R10)
HOOK_TIMEOUT = 10  # seconds, each hook command
HOOK_STDIN_MAX = 1 << 20  # bytes a hook reads from its stdin
PA_FILE = os.path.realpath(__file__)
AGENT_VARS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_SANDBOX", "CODEX_HOME_SESSION")
VERSION = re.compile(r"(?<![\d.])(\d+)\.(\d+)\.(\d+)(?:\.\d+)*(?![\d.])")  # t-190 T3: a build number may follow


def context_problems(value):
    """What is wrong with context.json's content (merged over the defaults); [] when it is valid."""
    if not isinstance(value, dict):
        return ["not an object"]
    out = []
    if value.get("v") != 1 or isinstance(value.get("v"), bool):
        out.append("v must be 1")
    if value.get("mode") not in CONTEXT_MODES:
        out.append("mode must be fresh or off")
    caps_ok = True
    for k in CONTEXT_CAPS:
        n = value.get(k)
        if isinstance(n, bool) or not isinstance(n, int) or not CAP_MIN <= n <= CAP_MAX:
            out.append("{} must be a whole number of tokens from {:,} to {:,}".format(k, CAP_MIN, CAP_MAX))
            caps_ok = False
    if caps_ok and not value["taskCap"] <= value["passCap"] < value["net"]:
        out.append("taskCap <= passCap < net must hold")
    at = value.get("enabledAt")
    if at is not None and (not isinstance(at, str) or parse_time(at) is None):
        out.append("enabledAt must be a UTC time or null")
    if value.get("by") is not None and value.get("by") not in CONTEXT_BY:
        out.append("by must be person or panel")
    return out


def context_defaults():
    return dict(CONTEXT_DEFAULTS, v=1, enabledAt=None, by=None)


def read_context(directory):
    """(settings, problem): context.json over the defaults; the defaults with problem None when there is no file (or
    no folder), with the reason when it is not valid. Never raises."""
    try:
        if directory is None:
            return context_defaults(), None
        path = os.path.join(directory, CONTEXT_FILE)
        if not os.path.isfile(path):
            return context_defaults(), None
        try:
            value = json.loads(read_text(path))
        except ValueError:
            return context_defaults(), "not JSON"
        if not isinstance(value, dict):
            return context_defaults(), "not an object"
        merged = dict(context_defaults(), **value)
        problems = context_problems(merged)
        return (context_defaults(), problems[0]) if problems else (merged, None)
    except Exception as e:  # noqa: BLE001 - unreadable: the defaults
        return context_defaults(), "unreadable ({})".format(type(e).__name__)


def parse_cc_version(text):
    """(major, minor, patch) from `claude --version` output: the first X.Y.Z (a build number .N after it is left out,
    t-190 T3) on a line that names Claude Code, else on the first line when that line starts with it or with v and it
    (a wrapper's banner with its own version does not count); else None."""
    if not isinstance(text, str):
        return None
    lines = text.splitlines()
    for line in lines:
        if "Claude Code" in line:
            m = VERSION.search(line)
            if m:
                return tuple(int(g) for g in m.groups())
    if lines:
        first = lines[0].strip()
        m = VERSION.match(first[1:] if first[:1] in ("v", "V") else first)
        if m:
            return tuple(int(g) for g in m.groups())
    return None


def probe_version(p):
    """The profile's Claude Code version, from `<claude> --version` bounded by VERSION_TIMEOUT; None when unknown."""
    try:
        _code, output = run_bounded(resolve(list(p["claude"]) + ["--version"], p), VERSION_TIMEOUT)
        return parse_cc_version(output.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - not known: the version-gated flag and key are left out
        return None


def cc_version(p, ctx=None):
    """probe_version once per launcher process and claude command (cached in ctx, as snapshot_flag is)."""
    cache = (ctx if ctx is not None else {}).setdefault("versionProbe", {})
    key = json.dumps(p.get("claude"))
    if key not in cache:
        cache[key] = probe_version(p)
    return cache[key]


def version_at_least(version, since):
    return isinstance(version, tuple) and version >= since


def context_launch(directory, version):
    """What J3 adds to one Project Agent launch: {"mode", "net" (the --autocompact value or None), "idle" (set
    idleCompaction false), "hooks", "notes" (lines for session.log)}. Never raises."""
    settings, problem = read_context(directory)
    notes = ["context.json ignored ({}); using the defaults".format(problem)] if problem else []
    if settings["mode"] == "off":
        return {"mode": "off", "net": None, "idle": False, "hooks": False, "notes": notes}
    if version is None:
        notes.append("claude --version not known: --autocompact and idleCompaction left out")
    else:
        shown = ".".join(map(str, version))
        if not version_at_least(version, AUTOCOMPACT_SINCE):
            notes.append("claude {} is older than 2.1.221: no --autocompact".format(shown))
        if not version_at_least(version, IDLE_COMPACTION_SINCE):
            notes.append("claude {} is older than 2.1.290: idleCompaction left out".format(shown))
    return {"mode": "fresh", "net": settings["net"] if version_at_least(version, AUTOCOMPACT_SINCE) else None,
            "idle": version_at_least(version, IDLE_COMPACTION_SINCE), "hooks": True, "notes": notes}


def hook_command(space, sub):
    """The command line of one compaction hook for this space, or None when the space is not an id."""
    space = local_id(space or "")
    if not ID.match(space) or not sys.executable:
        return None
    # Shell form (sh -c). `|| true`: the line itself never exits 2, which would block a compaction (PreCompact): a
    # missing PA_FILE, or a pa.py that does not know `context` (installed while this session runs), both exit 2.
    return " ".join(shlex.quote(a) for a in (sys.executable, PA_FILE, "context", sub, "--space", space)) + " || true"


def context_hooks(space):
    """The "hooks" setting: PreCompact (manual and auto) prints what the summary keeps, SessionStart after a
    compaction prints the start pack, PostCompact records it. None when a command cannot be built."""
    out = {}
    for event, sub, matcher in (("PreCompact", "compact-instructions", None), ("SessionStart", "start-pack", "compact"),
                                ("PostCompact", "compacted", None)):
        command = hook_command(space, sub)
        if not command:
            return None
        entry = {"hooks": [{"type": "command", "command": command, "timeout": HOOK_TIMEOUT}]}
        if matcher:
            entry = dict({"matcher": matcher}, **entry)
        out[event] = [entry]
    return out


def compact_instructions():
    """PreCompact's stdout: what the compaction summary must keep (Claude Code reads it as instructions)."""
    return ("This is the Project Agent's conversation. In the summary, keep: the open asks (what was asked of or "
            "promised to the person and is not answered yet, with what waits on it); the person's own words since the "
            "last ledger checkpoint, quoted exactly; each running worker's address, task and job; the next step per "
            "task; any hold or resume the person said; and the ids and titles of the active rules in rules.json and the numbers "
            "in them that handoffs follow. The ledger, the journal and standing.md are read again after the "
            "compaction, so do not copy them.\n")


START_PACK_BODY = ("Your state is on disk. This is not a new space. Do as orchestrator.md says under 'After a restart "
                   "or a compaction': run `pa-doctor`, read ledger.json, rules.json (every active rule's text), "
                   "`standing.md`, the last 30 journal lines, the open decisions and `hc ls`; read the reports that came in; take the next step the ledger's `next` "
                   "sets; start nothing else until asked. If the last hold or resume line in the journal is a hold, "
                   "start nothing. Your standing notes follow, the person's words in quotes; they stand as said.")


def start_pack(directory, head):
    """head, the body, standing.md verbatim (or why it is not there), and the checkpoint line, in at most
    START_PACK_MAX characters. J4's fresh-context prompt reuses it with its own head (R10). Never raises."""
    parts = [head + " " + START_PACK_BODY]
    try:
        text, _sections, problems, _warnings = read_standing(directory)
    except Exception as e:  # noqa: BLE001
        text, problems = None, ["unreadable ({})".format(type(e).__name__)]
    block = []
    if text is not None:
        block.append(text if len(text) <= STANDING_CAP else text[:STANDING_CAP] + "\n(cut: over the cap)")
    block.extend("standing.md: {}".format(p) for p in problems[:5])
    parts.append("\n".join(b.rstrip("\n") for b in block))
    try:
        ledger = read_json(os.path.join(directory, "ledger.json"), None)
        if ledger is None:
            parts.append(checkpoint_line({}))
        else:
            n = as_dict(ledger).get("passCount")
            nxt = n + 1 if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else None
            parts.append(checkpoint_line(ledger) + ("\nYour next pass is {}.".format(nxt) if nxt is not None else ""))
    except Exception:  # noqa: BLE001
        parts.append("checkpoint: ledger not readable")
    rules = active_rules_line(directory)
    if rules:
        parts.append(rules)
        room = START_PACK_MAX - len("\n\n".join(parts)) - 2
        fresh = new_rules_text(directory, room)
        if fresh:
            parts.append(fresh)
    return "\n\n".join(parts)[:START_PACK_MAX]


# ---- t-190: the space's rules as the Project Agent's start pack, pa-doctor and pa-handoff read them --------------
RULES_LINE_MAX = 1600  # characters of the start pack's active-rules line (t-041 J7-R4; t-190 R1: with titles)
RULE_TITLE_MAX = 100  # characters of one rule's title in a list
RULE_TEXT_MAX = 1200  # characters of one rule's text where a list gives it in full
RULE_ID = re.compile(r"^[\w.-]{1,80}$")
RULE_NEW = ("created", "approved", "edited and approved", "approved in conversation", "edited", "imported")  # T1, t-192 R5.2: the rule's text is new
RULE_NEW_SECONDS = 86400
REST = "… (read the rest in rules.json)"


def one_line(text, cap):
    """text on one line: control characters to spaces, runs of white space as one, cut at cap characters with '…';
    '' when it is not text."""
    if not isinstance(text, str):
        return ""
    text = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]", " ", text)).strip()
    return text if len(text) <= cap else text[:cap - 1].rstrip() + "…"


def rule_text(rule, cap=RULE_TEXT_MAX):
    """A rule's text on one line, cut at cap characters with REST."""
    text = one_line(rule.get("text"), 1 << 20)
    return text if len(text) <= cap else text[:max(cap - len(REST), 0)].rstrip() + REST


def read_rules(directory):
    """(rules, why): the rules of rules.json whose status is exactly 'active' and whose id is an id (H2, H3), highest
    `order` first (a missing or non-number order last, file order among equals: H7); why is None, or why the file
    gives none (missing, not JSON, no rules list: H1). Never raises."""
    try:
        path = os.path.join(directory, "rules.json")
        if not os.path.isfile(path):
            return [], "missing"
        try:
            doc = json.loads(read_text(path))
        except ValueError:
            return [], "not JSON"
        rules = doc.get("rules") if isinstance(doc, dict) else None
        if not isinstance(rules, list):
            return [], "no rules list"
        active = [r for r in rules if isinstance(r, dict) and r.get("status") == "active"
                  and isinstance(r.get("id"), str) and RULE_ID.match(r["id"])]

        def key(item):
            n, r = item
            o = r.get("order")
            if isinstance(o, (int, float)) and not isinstance(o, bool) and o == o and abs(o) != float("inf"):
                return (0, -o, n)
            return (1, 0, n)
        return [r for _n, r in sorted(enumerate(active), key=key)], None
    except Exception as e:  # noqa: BLE001
        return [], "unreadable ({})".format(type(e).__name__)


def rule_named(rule):
    """'<id>: <title>' (the id alone when it has no title)."""
    title = one_line(rule.get("title"), RULE_TITLE_MAX)
    return "{}: {}".format(rule["id"], title) if title else rule["id"]


def capped_list(head, items, tail, cap, sep="; "):
    """head + items joined + tail in at most cap characters; '…' stands for the items left out."""
    shown = []
    for item in items:
        if len(head) + len(sep.join(shown + [item])) + len(sep + "…") + len(tail) > cap:
            return head + sep.join(shown + ["…"]) + tail
        shown.append(item)
    return head + sep.join(shown) + tail


def active_rules_line(directory):
    """'Active rules (rules.json, highest order first): <id>: <title>; ... Read each one's text before your next
    action.' at most RULES_LINE_MAX characters (t-190 R1); None when rules.json is missing, invalid or has none."""
    rules, _why = read_rules(directory)
    if not rules:
        return None
    return capped_list("Active rules (rules.json, highest order first): ", [rule_named(r) for r in rules],
                       ". Read each one's text before your next action.", RULES_LINE_MAX)


def rule_new_at(rule, at=None):
    """The time (epoch s) of the rule's newest history entry that made its text new (RULE_NEW: created, approved),
    when it is within RULE_NEW_SECONDS before `at` (now); else None. A time that is not UTC Z is not read."""
    at = time.time() if at is None else at
    times = [parse_time(h["at"]) for h in rule.get("history") if isinstance(h, dict) and isinstance(h.get("at"), str)
             and (h.get("change") or h.get("what")) in RULE_NEW] if isinstance(rule.get("history"), list) else []
    times = [t for t in times if t is not None and at - RULE_NEW_SECONDS <= t <= at + 60]
    return max(times) if times else None


def new_rules_text(directory, room):
    """t-190 T1: the full text of the active rules approved in the last 24 h, one bullet each, in at most `room`
    characters (each text cut with REST to share it); None when there is none or no room."""
    rules, _why = read_rules(directory)
    fresh = [r for r in rules if rule_new_at(r) is not None]
    head = "Rules approved in the last 24 h (their full text; read it before your next action):"
    if not fresh or room < len(head) + 40:
        return None
    lines = [head]
    for n, r in enumerate(fresh):
        start = "- {}: ".format(r["id"])
        share = (room - len("\n".join(lines)) - 1) // (len(fresh) - n) - len(start)
        if share < len(REST) + 20:
            lines.append("- and {} more in rules.json".format(len(fresh) - n))
            break
        lines.append(start + rule_text(r, min(share, RULE_TEXT_MAX)))
    text = "\n".join(lines)
    return text if len(text) <= room else None


def parse_amount(text):
    """A token count from the command line: a whole number, or one with k (400k = 400,000)."""
    m = re.match(r"^(\d+)([kK]?)$", text or "")
    if not m:
        raise Problem("{!r} is not a whole number of tokens (like 400000 or 400k)".format(text))
    return int(m.group(1)) * (1000 if m.group(2) else 1)


def hook_input():
    """The hook's JSON on stdin (at most HOOK_STDIN_MAX bytes) as a dict; {} when it is anything else."""
    try:
        stream = getattr(sys.stdin, "buffer", sys.stdin)
        data = stream.read(HOOK_STDIN_MAX)
        value = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
        return value if isinstance(value, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def hook_directory(space):
    """The state folder of an existing space, or None (a hook never creates one)."""
    try:
        directory = space_dir(space)
        return directory if os.path.isfile(os.path.join(directory, "agent.json")) else None
    except Exception:  # noqa: BLE001
        return None


def cmd_context(args):
    if args.action == "set":
        if getattr(args, "on", None):
            refuse_set_in_agent()  # before the relay: on the far side it runs under hc exec (REVIEW J4 MF-J4-1)
            return context_relay(args)
        return context_set(args)
    if args.action == "fresh":
        return context_fresh(args)
    if args.action == "followup":
        return context_followup(args)
    # The hooks: Claude Code runs them in the Project Agent's session. Each exits 0 whatever happens (PreCompact must
    # never block a compaction: exit 2 would postpone it to the 967k default).
    try:
        if args.action == "compact-instructions":
            sys.stdout.write(compact_instructions())
        elif args.action == "start-pack":
            directory = hook_directory(args.space) or os.path.join(ROOT, "spaces", "-")
            sys.stdout.write(start_pack(directory, "Your context was compacted.") + "\n")
        elif args.action == "compacted":
            value = hook_input()
            directory = hook_directory(args.space)
            trigger = value.get("trigger") if value.get("trigger") in ("manual", "auto") else None
            sid = value.get("session_id")
            sid = sid if isinstance(sid, str) and re.match(r"^[A-Za-z0-9_-]{1,80}$", sid) else None
            costs = costs_module() if directory else None
            if costs:
                costs.append_session(directory, {"v": 1, "ev": "compact", "trigger": trigger, "session": sid, "at": now()})
    except Exception as e:  # noqa: BLE001 - a hook never fails the session
        if args.action == "start-pack":
            sys.stdout.write("Your context was compacted. " + START_PACK_BODY + "\n")
        print("pa-context {}: {}".format(args.action, ascii(e)[:200]), file=sys.stderr)
    sys.stdout.flush()


def refuse_set_in_agent():
    if any(os.environ.get(k) for k in AGENT_VARS):
        raise Problem("pa-context set is the person's command: type it in a terminal, not in an agent's session")


def context_set(args):
    """pa-context set: the person's command. Merges the options over the current settings, checks them and writes
    context.json; with no option it shows them."""
    refuse_set_in_agent()
    directory, _agent = space_for_command(args.space)
    path = os.path.join(directory, CONTEXT_FILE)
    current, problem = read_context(directory)
    had = os.path.isfile(path) and problem is None
    if problem and os.path.isfile(path):
        print("context.json was not valid ({}); starting from the defaults".format(problem))
    changes = {}
    if args.mode is not None:
        changes["mode"] = args.mode
    for flag, key in (("task_cap", "taskCap"), ("pass_cap", "passCap"), ("net", "net")):
        if getattr(args, flag) is not None:
            changes[key] = parse_amount(getattr(args, flag))
    if not changes:
        print(context_summary(current, had))
        if getattr(args, "json", False):
            print(json.dumps({k: current.get(k) for k in ("v", "mode", "taskCap", "passCap", "net", "enabledAt", "by")}))
        return
    new = dict(current, **changes)
    new["v"], new["by"] = 1, "panel" if getattr(args, "by", None) == "panel" else "person"
    if new["mode"] == "fresh" and (not had or current.get("mode") == "off"):
        new["enabledAt"] = now()
    problems = context_problems(new)
    if problems:
        raise Problem("; ".join(problems))
    write_json(path, {k: new.get(k) for k in ("v", "mode", "taskCap", "passCap", "net", "enabledAt", "by")})
    print(context_summary(new, True))
    print("The net applies at the Project Agent's next start (a restart in place loads it); the caps and the mode's "
          "resets at its launcher's next check.")
    if getattr(args, "json", False):
        print(json.dumps({k: new.get(k) for k in ("v", "mode", "taskCap", "passCap", "net", "enabledAt", "by")}))


NET_MARGIN = 34000  # Claude Code compacts about this far below the window it is given (t-041 J7-R3: 365-367k at 400k)


def context_summary(settings, saved):
    return "mode {}  taskCap {:,}  passCap {:,}  net {:,} (compacts at about {}k)  enabledAt {}  by {}{}".format(
        settings["mode"], settings["taskCap"], settings["passCap"], settings["net"],
        (settings["net"] - NET_MARGIN) // 1000, settings.get("enabledAt") or "-",
        settings.get("by") or "-", "" if saved else "  (the defaults: no context.json)")


# ---- the fresh reset (t-041 J4, R1-R4, R6, R10, R16) ----------------------------------------------------------
# The watcher thread looks every CONTEXT_EVERY s: the context size from the transcript's tail (R1); when a cap is
# crossed or the person asked (R3, R6) and the PA is at a clean point (R2), it ends its own Claude child and its
# group (end_child) and leaves ctx["after"] of kind "fresh"; the loop then starts Claude in a new session with the
# start prompt (R4, R10). The terminal, its address, its labels and its workers stay.

CONTEXT_REQUEST = "context-request.json"
CONTEXT_REQUEST_LIFE = 7200  # a context request expires 2 h after it was written (R6)
CONTEXT_REQUEST_BY = ("person", "panel", "orchestrator")
TAIL_MAX = 1 << 20  # bytes of the transcript one check reads (R1)
JOURNAL_TAIL = 256 * 1024  # bytes of journal.jsonl the clean-point check reads at a time
JOURNAL_BACK_MAX = 4 << 20  # bytes of journal.jsonl it reads back at most to cover R2 (c)'s window (t-041 J7-R1)
UNACTED_SLACK = 1800  # R2 (c) also looks at cards answered this long before this conversation began (J7-R1)
UNACTED_DAY = 86400  # ... and never further back than this (J7-R1)
IDLE_MIN = 20  # seconds the PA must have been idle (R2 a)
RESET_GAP = 600  # seconds between two resets (R2 f)
NUDGE_GAP = 3600  # seconds between two of the launcher's lines (R2)
CONTEXT_EVERY = 10  # seconds between two context checks of the watcher
PREVIOUS_MAX = 20  # agent.json previousSessions
CONTEXT_COPIES_MAX = 200  # costs/context/<session>.txt copies kept (R16)
NUDGE_CAP = ("[launcher] context at {}k, over the cap: if this is a clean point, write the ledger's context "
             "checkpoint and end your turn")
NUDGE_ASKED = ("[launcher] context: a fresh context was asked for: if this is a clean point, write the ledger's "
               "context checkpoint and end your turn")
STAMP = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?Z$")
SESSION_ID = re.compile(r"^[A-Za-z0-9-]{8,80}$")


def parse_stamp(text):
    """Epoch seconds of a UTC time with or without fractions (transcripts have them), else None."""
    m = STAMP.match(text) if isinstance(text, str) else None
    base = parse_time(m.group(1) + "Z") if m else None
    return None if base is None else base + (float(m.group(2)) if m.group(2) else 0.0)


def transcript_path(project, sid):
    """Where Claude keeps this session's transcript (as transcript_state looks), or None."""
    if not project or not isinstance(sid, str) or not SESSION_ID.match(sid):
        return None
    return os.path.join(CLAUDE_HOME, "projects", claude_slug(project), sid + ".jsonl")


def read_tail(path, limit):
    """At most `limit` bytes from the end of the file, starting at a line (a cut first line is dropped)."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        start = max(0, size - limit)
        f.seek(start)
        data = f.read(limit)
    if start:
        cut = data.find(b"\n")
        data = data[cut + 1:] if cut >= 0 else b""
    return data


def token_count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def is_message(obj):
    """A user entry that is a message (the person's or a pasted line), not a tool result or a meta entry."""
    if obj.get("isMeta") or "toolUseResult" in obj:
        return False
    content = as_dict(obj.get("message")).get("content")
    return isinstance(content, str) or (isinstance(content, list) and bool(content) and isinstance(content[0], dict)
                                        and content[0].get("type") == "text")


def context_size(project, sid, cache=None):
    """The Project Agent's context from its transcript's tail (R1): {ctx, source, lastAssistant, turnStart,
    userAfter, midTool, key}, or None (no transcript, no usage in the tail, a compaction after the last answer).
    Main conversation only (sidechain entries skipped). cache (a dict): the tail is read again only when the file
    changed. Never raises."""
    try:
        path = transcript_path(project, sid)
        if not path or not os.path.isfile(path):
            return None
        st = os.stat(path)
        if cache is not None and cache.get("of") == (path, st.st_size, st.st_mtime):
            return cache.get("size")
        size = transcript_size(path, st)
        if cache is not None:
            cache.update({"of": (path, st.st_size, st.st_mtime), "size": size})
        return size
    except Exception:  # noqa: BLE001 - unknown: no reset
        return None


def transcript_size(path, st):
    try:
        last, turn, after, boundary = None, None, False, False
        for line in read_tail(path, TAIL_MAX).splitlines():
            try:
                obj = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(obj, dict) or obj.get("isSidechain"):
                continue
            kind, message = obj.get("type"), as_dict(obj.get("message"))
            if kind == "assistant" and isinstance(message.get("usage"), dict):
                last, after, boundary = obj, False, False
            elif kind == "user":
                after = after or last is not None
                if is_message(obj):
                    turn = parse_stamp(obj.get("timestamp"))
            elif kind == "system" and obj.get("subtype") == "compact_boundary":
                boundary = True
        if last is None or boundary:
            return None
        message = last["message"]
        usage = message["usage"]
        ctx = sum(token_count(usage.get(k)) for k in ("input_tokens", "cache_creation_input_tokens",
                                                       "cache_read_input_tokens"))
        return {"ctx": ctx, "source": "transcript", "lastAssistant": parse_stamp(last.get("timestamp")),
                "turnStart": turn, "userAfter": after, "midTool": message.get("stop_reason") == "tool_use",
                "key": (st.st_size, st.st_mtime)}
    except Exception:  # noqa: BLE001 - unknown: no reset
        return None


def statusline_ctx(directory, sid):
    """The recording status line's ctx for this session (J1), the second source: no turn facts, so never a reset."""
    try:
        if not isinstance(sid, str) or not SESSION_ID.match(sid):
            return None
        record = as_dict(read_json_safe(os.path.join(directory, "costs", "cc", sid + ".json")))
        n = record.get("ctx")
        if record.get("session") != sid or isinstance(n, bool) or not isinstance(n, (int, float)) or n < 0:
            return None
        return {"ctx": int(n), "source": "statusline", "lastAssistant": None, "turnStart": None, "userAfter": False,
                "midTool": False, "key": None}
    except Exception:  # noqa: BLE001
        return None


def current_size(directory, project, sid, cache=None):
    return context_size(project, sid, cache) or statusline_ctx(directory, sid)


def read_context_request(directory):
    """(request, None) for a well-formed context-request.json; (None, why) for one that is not; (None, None) for
    none. Every field is checked before use (as read_request checks a restart's)."""
    path = os.path.join(directory, CONTEXT_REQUEST)
    try:
        text = read_text(path)
    except OSError as e:
        return None, "unreadable: {}".format(e)
    if not text and not os.path.exists(path):
        return None, None
    try:
        req = json.loads(text)
    except ValueError:
        return None, "not JSON"
    if not isinstance(req, dict):
        return None, "not an object"
    if req.get("v") != 1 or isinstance(req.get("v"), bool):
        return None, "v is not 1"
    for key in ("id", "space", "by", "at", "expires"):
        if not isinstance(req.get(key), str) or not req[key]:
            return None, "field {!r} missing or not text".format(key)
    for key in ("id", "space"):
        if not REQUEST_ID.match(req[key]):
            return None, "bad {}".format(key)
    if req["by"] not in CONTEXT_REQUEST_BY:
        return None, "field 'by' is not one of {}".format(", ".join(CONTEXT_REQUEST_BY))
    why = req.get("why")
    if why is not None and (not isinstance(why, str) or len(why) > 2000
                            or any((ord(c) < 32 and c not in "\t\n") or ord(c) == 127 for c in why)):
        return None, "field 'why' is not text of at most 2000 characters without control characters"
    at, expires = parse_time(req["at"]), parse_time(req["expires"])
    if at is None or expires is None:
        return None, "at/expires are not times"
    if expires - at > CONTEXT_REQUEST_LIFE:
        return None, "it expires more than 2 h after it was written"
    req["_at"], req["_expires"] = at, expires
    return req, None


def remove_context_request(directory, rid=None, text=None):
    """Deletes context-request.json while it is still the one meant (same id, or same text)."""
    path = os.path.join(directory, CONTEXT_REQUEST)
    try:
        current = read_text(path)
        if rid is not None:
            try:
                if as_dict(json.loads(current)).get("id") != rid:
                    return False
            except ValueError:
                return False
        elif text is not None and current != text:
            return False
        os.remove(path)
        return True
    except OSError:
        return False


def line_stamp(line):
    try:
        return parse_stamp(as_dict(json.loads(line)).get("at"))
    except ValueError:
        return None


def journal_tail(directory, since=None):
    """The journal's last JOURNAL_TAIL bytes as lines. With `since` (epoch), it reads back in JOURNAL_TAIL steps
    until a line stamped before `since` is read or JOURNAL_BACK_MAX bytes are (t-041 J7-R1), and returns (lines,
    earliest): `earliest` is the first stamp read, or None when the whole journal was read."""
    path = os.path.join(directory, "journal.jsonl")
    if since is None:
        try:
            return read_tail(path, JOURNAL_TAIL).decode("utf-8", "replace").splitlines()
        except OSError:
            return []
    data, start = b"", 0
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = start = f.tell()
            while start > 0 and size - start < JOURNAL_BACK_MAX:
                step = min(JOURNAL_TAIL, start, JOURNAL_BACK_MAX - (size - start))
                start -= step
                f.seek(start)
                data = f.read(step) + data
                whole = data.split(b"\n")[1 if start else 0:]
                first = next((t for t in (line_stamp(x) for x in whole[:50]) if t is not None), None)
                if first is not None and first < since:
                    break
    except OSError:
        return [], None
    if start:
        cut = data.find(b"\n")
        data = data[cut + 1:] if cut >= 0 else b""
    lines = data.decode("utf-8", "replace").splitlines()
    earliest = next((t for t in (line_stamp(x) for x in lines) if t is not None), None) if start else None
    return lines, earliest


def answered_unacted(directory, lines, since=None, earliest=None):
    """(id, far) of cards in decisions/ answered with no later journal line naming them (R2 c): an answer being
    acted on. With `since`, only cards answered at or after it (J7-R1); `far` when the answer is older than
    `earliest`, the first journal stamp read, so the lines read cannot show it acted on."""
    folder, out = os.path.join(directory, "decisions"), []
    names = sorted(os.listdir(folder)) if os.path.isdir(folder) else []
    stamped = []
    for line in lines:
        try:
            at = parse_stamp(as_dict(json.loads(line)).get("at"))
        except ValueError:
            at = None
        if at is not None:
            stamped.append((at, line))
    for name in names:
        if not name.endswith(".json"):
            continue
        card = as_dict(read_json_safe(os.path.join(folder, name)))
        cid = card.get("id") if isinstance(card.get("id"), str) else name[:-len(".json")]
        answered = parse_stamp(card.get("answered"))
        if card.get("status") != "answered" or answered is None or not cid:
            continue
        if since is not None and answered < since:
            continue
        if earliest is not None and answered < earliest:
            out.append((cid, True))
            continue
        named = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(cid) + r"(?![A-Za-z0-9_])")
        if not any(at >= int(answered) and named.search(line) for at, line in stamped):
            out.append((cid, False))
    return out


def worker_handoffs_missing(directory, ledger, live):
    """(task, address, job) of each ledger worker not finished, live, and without its handoff file (R2 d, R8). A
    worker whose name names no job has no handoff path to find: it counts as missing (plan Q4)."""
    out = []
    tasks = as_dict(ledger).get("tasks")
    for t in tasks if isinstance(tasks, list) else []:
        for w in as_dict(t).get("workers") or []:
            w = as_dict(w)
            if w.get("finished") or w.get("address") not in live:
                continue
            found, expected = handoff_file(directory, as_dict(t).get("id"), w)
            if not found:
                job = os.path.basename(expected)[len("handoff-"):-len(".md")] if expected else "no job in its name"
                out.append((as_dict(t).get("id"), w.get("address"), job))
    return out


def clean_point(ctx, directory, agent, size, at, address):
    """(ok, reasons) for R2: reasons [(letter, text)], letters a-f as the spec lists them; ok when there is none."""
    reasons = []
    turn = agent_turn(address)
    if turn != "idle":
        reasons.append(("a", "turn " + turn))
    since = ctx.get("idleSince")
    if since is None or at - since < IDLE_MIN:
        reasons.append(("a", "idle {}s < {}s".format(int(round(at - since)) if since is not None else 0, IDLE_MIN)))
    if not size or size.get("source") != "transcript":
        reasons.append(("a", "no transcript to read the turn from"))
    elif size.get("userAfter") or size.get("midTool"):
        reasons.append(("a", "message waiting"))
    ledger = as_dict(read_json_safe(os.path.join(directory, "ledger.json")))
    check = ledger.get("context")
    sid = as_dict(agent).get("sessionId")
    if check is None:
        reasons.append(("b", "checkpoint none"))
    elif checkpoint_problems(check):
        reasons.append(("b", "checkpoint malformed: " + checkpoint_problems(check)[0]))
    elif not check["clean"]:
        reasons.append(("b", "checkpoint not clean ({})".format(check["why"][:80])))
    else:
        written = parse_time(check["at"])
        turn_start = (size or {}).get("turnStart")
        began = as_dict(ctx.get("running")).get("since")
        ours = turn_start is not None and written >= int(turn_start) and began is not None and written >= int(began)
        if check.get("session") is not None and check["session"] != sid and not ours:
            reasons.append(("b", "checkpoint of another session ({} not {})".format(check["session"][:8], str(sid)[:8])))
        elif check.get("session") is not None and check["session"] != sid:
            # written in this conversation's current turn whatever id it names (t-041 J7-R2): said once, not refused
            ctx["checkpointNote"] = "checkpoint names session {}, this session {}: accepted by its time".format(
                check["session"][:8], str(sid)[:8])
        elif check.get("session") is None and (began is None or written < int(began)):
            reasons.append(("b", "checkpoint older than this session"))
        elif turn_start is None or written < int(turn_start):
            reasons.append(("b", "checkpoint older than the turn"))
    began = as_dict(ctx.get("running")).get("since")
    window = max(began - UNACTED_SLACK, at - UNACTED_DAY) if isinstance(began, (int, float)) else at - UNACTED_DAY
    lines, earliest = journal_tail(directory, window)
    for cid, far in answered_unacted(directory, lines, window, earliest):
        reasons.append(("c", "card {} answered, {}".format(cid, "journal not read back that far" if far else "not acted on")))
    terminals = live_terminals()
    if terminals is None:  # hc ls failed: which workers are live is unknown, so no reset (REVIEW J4 MF-J4-2)
        reasons.append(("d", "terminal list unavailable"))
    for task, worker, job in worker_handoffs_missing(directory, ledger, set((terminals or {}).keys())):
        reasons.append(("d", "worker {} ({} {}) has no handoff file".format(worker, task, job)))
    _text, _sections, problems, _warnings = read_standing(directory)
    for problem in problems[:2]:
        reasons.append(("e", "standing.md " + problem))
    if os.path.exists(os.path.join(directory, REQUEST)):
        reasons.append(("f", "restart request pending"))
    last = ctx.get("lastResetAt")
    if last is not None and at - last < RESET_GAP:
        reasons.append(("f", "reset {}s ago".format(int(round(at - last)))))
    return not reasons, reasons


def reset_reason(size, checkpoint, settings, request):
    """R3: task-cap | pass-cap | person | orchestrator, or None. A request needs no size; mode off: never."""
    if as_dict(settings).get("mode") != "fresh":
        return None
    if request:
        return "orchestrator" if request.get("by") == "orchestrator" else "person"
    if not size:
        return None
    n, boundary = size.get("ctx") or 0, as_dict(checkpoint).get("boundary")
    if n >= settings["taskCap"] and boundary == "task":
        return "task-cap"
    if n >= settings["passCap"]:
        return "pass-cap"
    return None


def fresh_prompt(directory, n, reason, previous):
    """R10: the fresh session's first prompt, at most START_PACK_MAX characters."""
    head = ("Fresh context (reset {} today, reason {}; previous session {}). Your conversation was reset on "
            "purpose.".format(n, reason, previous or "none"))
    return start_pack(directory, head)


def resets_today(directory, at=None):
    day = time.strftime("%Y-%m-%d", time.gmtime(time.time() if at is None else at))
    count = 0
    try:
        lines = read_text(os.path.join(directory, "session.log")).splitlines()
    except (OSError, ValueError):
        return 0
    for line in lines:
        words = line.split()
        if len(words) >= 2 and words[0].startswith(day) and words[1] == "fresh":
            count += 1
    return count


def context_capable(record):
    record = as_dict(record)
    version, accepts = record.get("version"), record.get("accepts")
    return (isinstance(version, int) and not isinstance(version, bool) and version >= 2
            and isinstance(accepts, list) and "context" in accepts)


def context_outcome(ctx, directory, rid, outcome, why):
    entry = {"request": rid, "outcome": outcome, "why": why, "at": now()}
    try:
        launcher_update(ctx, directory, lambda r: r.__setitem__("context", entry))
    except Exception:  # noqa: BLE001 - never stops the Project Agent
        pass


def nudge(ctx, directory, address, text, at, size, reason):
    """The launcher's one line to its own PA (R2), at most once per NUDGE_GAP."""
    if ctx.get("nudgedAt") is not None and at - ctx["nudgedAt"] < NUDGE_GAP:
        return False
    ctx["nudgedAt"] = at
    call(["hc", "send", address, text], check=False)
    session_log(directory, "context nudge ctx={} reason={}".format((size or {}).get("ctx"), reason))
    return True


def context_once(ctx, directory, address, space, at=None):
    """One context check of the watcher (R2-R4, R6). A restart taken or waiting outranks it (R2 f)."""
    at = time.time() if at is None else at
    if (ctx.get("restarting") or ctx.get("after") or ctx.get("waitingFor") or ctx.get("child") is None
            or os.path.exists(os.path.join(directory, REQUEST))):
        return
    settings, _problem = read_context(directory)
    if settings["mode"] != "fresh":
        return
    req, why = read_context_request(directory)
    if why:
        session_log(directory, "context request refused: " + why)
        try:
            remove_context_request(directory, text=read_text(os.path.join(directory, CONTEXT_REQUEST)))
        except (OSError, ValueError):
            pass
        context_outcome(ctx, directory, None, "refused", why)
    elif req and req["space"] != space:
        session_log(directory, "context request {} refused: for space {}, not {}".format(req["id"], req["space"], space))
        remove_context_request(directory, req["id"])
        context_outcome(ctx, directory, req["id"], "refused", "for another space")
        req = None
    elif req and at > req["_expires"]:
        session_log(directory, "context request {} expired".format(req["id"]))
        remove_context_request(directory, req["id"])
        context_outcome(ctx, directory, req["id"], "expired", "no clean point within 2 h")
        req = None
    agent = as_dict(read_json_safe(os.path.join(directory, "agent.json")))
    sid = agent.get("sessionId")
    size = current_size(directory, ctx.get("project") or agent.get("project"), sid, ctx.setdefault("sizeCache", {}))
    turn = agent_turn(address)
    key = (size or {}).get("key")
    if turn != "idle":
        ctx["idleSince"] = None
    elif ctx.get("idleSince") is None or key != ctx.get("idleKey"):
        ctx["idleSince"] = at
    ctx["idleKey"] = key
    ledger = as_dict(read_json_safe(os.path.join(directory, "ledger.json")))
    reason = reset_reason(size, ledger.get("context"), settings, req)
    if not reason:
        return
    ctx.pop("checkpointNote", None)
    ok, reasons = clean_point(ctx, directory, agent, size, at, address)
    note = ctx.pop("checkpointNote", None)
    if note and ctx.get("checkpointNoted") != (ctx.get("idleSince"), note):
        ctx["checkpointNoted"] = (ctx.get("idleSince"), note)
        session_log(directory, note)
    if ok:
        begin_fresh(ctx, directory, address, {
            "reason": reason, "ctx": (size or {}).get("ctx"), "pass": as_dict(ledger.get("context")).get("pass"),
            "from": sid, "request": req["id"] if req else None, "by": req["by"] if req else None})
        return
    letters = {letter for letter, _text in reasons}
    if "a" in letters:
        return  # mid-turn or not idle long enough: wait, say nothing
    if letters == {"b"}:
        nudge(ctx, directory, address, NUDGE_ASKED if req else NUDGE_CAP.format(int((size or {}).get("ctx", 0) // 1000)),
              at, size, reason)
    said = (ctx.get("idleSince"), reason)
    if ctx.get("contextSaid4") != said:
        ctx["contextSaid4"] = said
        session_log(directory, "context not reset (ctx={} reason={}): {}".format(
            (size or {}).get("ctx"), reason, "; ".join(text for _letter, text in reasons)))


def begin_fresh(ctx, directory, address, plan):
    """Takes the reset: ctx["after"] of kind fresh, then ends this launcher's own Claude child and its group."""
    with child_lock(ctx):
        if ctx.get("after") or ctx.get("restarting"):
            return False
        ctx["after"] = dict(plan, kind="fresh")
    print("\n[Project Agent] Context reset ({}k): continuing from the state folder. Previous session: {}".format(
        int((plan.get("ctx") or 0) // 1000), plan.get("from")), flush=True)
    session_log(directory, "context reset taken ctx={} reason={}{}".format(
        plan.get("ctx"), plan.get("reason"), " request=" + plan["request"] if plan.get("request") else ""))
    end_child(ctx, "fresh")  # no child running: the loop takes ctx["after"] next
    return True


def fresh_records(ctx, directory, address, after, new_sid, prompt):
    """The reset's records (R4, R16): the session.log line, the reset record, the prompt's copy, launcher.json;
    a request taken is removed. Each in its own try: none stops the launch."""
    old, reason, n = after.get("from"), after.get("reason"), resets_today(directory) + 1
    steps = []
    steps.append(lambda: session_log(directory, "fresh from={} to={} ctx={} reason={} checkpoint={} by={} n={}".format(
        old, new_sid, after.get("ctx"), reason, after.get("pass"), after.get("by") or "-", n)))

    def record():
        costs = costs_module()
        if costs:
            costs.append_session(directory, {"v": 1, "ev": "reset", "at": now(), "from": old, "to": new_sid,
                                             "ctx": after.get("ctx"), "reason": reason, "pass": after.get("pass"),
                                             "terminal": address})

    def copy():
        folder = os.path.join(directory, "costs", "context")
        os.makedirs(folder, exist_ok=True)
        write_text(os.path.join(folder, new_sid + ".txt"), prompt or "")
        names = [os.path.join(folder, x) for x in os.listdir(folder) if x.endswith(".txt")]
        names.sort(key=lambda x: (os.path.getmtime(x), x))
        for x in names[:max(0, len(names) - CONTEXT_COPIES_MAX)]:
            os.remove(x)

    def launcher():
        last = {"at": now(), "from": old, "to": new_sid, "reason": reason, "ctx": after.get("ctx")}
        launcher_update(ctx, directory, lambda r: r.__setitem__("lastReset", last))
        if after.get("request"):
            remove_context_request(directory, after["request"])
            context_outcome(ctx, directory, after["request"], "ok", None)
    steps += [record, copy, launcher]
    for step in steps:
        try:
            step()
        except Exception:  # noqa: BLE001 - never stops the Project Agent
            pass
    ctx["lastResetAt"] = time.time()


# ---- pa-context fresh and followup: the person's commands (R6, R17) --------------------------------------------

def context_flags(args):
    flags = ["--space", args.space]
    for name in ("why", "mode", "task_cap", "pass_cap", "net", "by", "since", "until"):
        if getattr(args, name, None) is not None:
            flags += ["--" + name.replace("_", "-"), getattr(args, name)]
    for name in ("dry_run", "withdraw", "json"):
        if getattr(args, name, False):
            flags.append("--" + name.replace("_", "-"))
    return flags


def context_relay(args):
    """pa-context <action> --on M: the same command on the space's machine (as pa-restart --on)."""
    if not args.space:
        raise Problem("--on needs --space <id> (the space's id on that machine)")
    script = ('"$HOME/.config/herdr-pa/tools/pa.py" context ' + args.action + " "
              + " ".join(shlex.quote(a) for a in context_flags(args)))
    if args.action == "fresh" and os.environ.get("CLAUDECODE") not in (None, "", "0") and args.why:
        journal_caller({"by": "orchestrator", "trigger": "conversation", "why": "fresh context: " + args.why,
                        "on": args.on, "space": args.space})
    code = relay(["hc", "exec", "--on", args.on, "--timeout", "60", "--", "sh", "-c", script])
    if code == 127:
        print("pa-context is not installed on {}: run pa-sync --on {} first".format(args.on, args.on), file=sys.stderr)
    if code:
        sys.exit(code)


def context_report(directory, agent):
    """What a fresh context request would meet now (the dry run): size, due reason and clean-point reasons."""
    settings, _problem = read_context(directory)
    req, _why = read_context_request(directory)
    sid = agent.get("sessionId")
    size = current_size(directory, agent.get("project"), sid)
    turn = agent_turn(agent.get("terminal"))
    ctx = {"idleSince": (size or {}).get("lastAssistant") if turn == "idle" else None,
           "running": {"since": parse_time(last_start(directory) or "")}}
    ledger = as_dict(read_json_safe(os.path.join(directory, "ledger.json")))
    due = reset_reason(size, ledger.get("context"), settings, req)
    _ok, reasons = clean_point(ctx, directory, agent, size, time.time(), agent.get("terminal"))
    return settings, size, due, reasons, req


def context_fresh(args):
    """pa-context fresh: asks the launcher for a fresh context at its next clean point (R6); --withdraw, --dry-run."""
    in_claude = os.environ.get("CLAUDECODE") not in (None, "", "0")
    if args.by == "panel" and any(os.environ.get(k) for k in AGENT_VARS):
        raise Problem("--by panel is the panel's own flag; it is refused inside Claude")
    if in_claude and not args.dry_run and not (args.why or "").strip():
        raise Problem("inside Claude, pa-context fresh needs --why quoting the person's own words")
    if args.on:  # after the checks, as cmd_restart: the far side runs under hc exec (REVIEW J4 MF-J4-1)
        return context_relay(args)
    directory, agent = space_for_command(args.space)
    if args.dry_run:
        settings, size, due, reasons, req = context_report(directory, agent)
        print("mode {}  taskCap {:,}  passCap {:,}".format(settings["mode"], settings["taskCap"], settings["passCap"]))
        print("context: {}".format("{}k ({})".format(size["ctx"] // 1000, size["source"]) if size else "unknown"))
        print("request: {}".format(req["id"] if req else "none"))
        print("reset due: {}".format(due or "no"))
        print("clean point: " + ("yes" if not reasons else "no: " + "; ".join(t for _l, t in reasons)))
        print("Dry run: nothing written.")
        if args.json:
            print(json.dumps({"outcome": "dry-run", "ctx": size["ctx"] if size else None,
                              "source": size["source"] if size else None, "due": due,
                              "request": req["id"] if req else None, "expires": req["expires"] if req else None,
                              "reasons": [t for _l, t in reasons]}))
        return
    if args.withdraw:
        req, _why = read_context_request(directory)
        removed = bool(req) and remove_context_request(directory, req["id"])
        print("Withdrawn ({}).".format(req["id"]) if removed else "No request was pending.")
        if args.json:
            print(json.dumps({"outcome": "withdrawn" if removed else "none", "request": req["id"] if removed else None}))
        return
    settings, _problem = read_context(directory)
    if settings["mode"] != "fresh":
        raise Problem("the context mode is off (context.json), so no fresh context is taken: pa-restart --fresh gives a "
                      "new conversation now, or set the mode to fresh first")
    if not context_capable(read_json_safe(os.path.join(directory, "launcher.json"))):
        raise Problem("its launcher is older and takes no context request: restart it once with pa-restart")
    by = "panel" if args.by == "panel" else ("orchestrator" if in_claude else "person")
    pending, _why = read_context_request(directory)
    at, rid = time.time(), str(uuid.uuid4())
    req = {"v": 1, "id": rid, "space": agent.get("space") or os.path.basename(directory), "by": by, "why": args.why,
           "at": iso_time(at), "expires": iso_time(at + CONTEXT_REQUEST_LIFE)}
    if by == "orchestrator":
        with open(os.path.join(directory, "journal.jsonl"), "a") as journal:
            journal.write(json.dumps({"at": now(), "by": "orchestrator", "trigger": "conversation",
                                      "why": "fresh context: " + args.why, "request": rid}) + "\n")
    write_json(os.path.join(directory, CONTEXT_REQUEST), req)
    if pending:
        print("It replaces the pending request {}.".format(pending["id"]))
    print("Requested ({}): a fresh context at the Project Agent's next clean point (it waits up to 2 h; a pass is "
          "never cut short).".format(rid))
    if args.json:
        print(json.dumps({"outcome": "requested", "request": rid, "expires": req["expires"]}))


def followup_report(directory, since, until=None):
    """R17's numbers and markers for the follow-up worker (read-only). The verdict is keep or tune by the rule;
    back out is never decided here: its candidates are listed under judgement."""
    lo, hi = parse_stamp(since), parse_stamp(until) if until else None
    inside = (lambda t: t is not None and t >= lo and (hi is None or t <= hi))
    day_lo, day_hi = since[:10], until[:10] if until else None
    days = [d for d in as_dict(read_json_safe(os.path.join(directory, "costs", "summary.json"))).get("paContext") or []
            if isinstance(d, dict) and isinstance(d.get("day"), str)]

    def per_pass(rows):
        money = sum(r["money"] for r in rows if isinstance(r.get("money"), (int, float)) and r.get("passes"))
        passes = sum(r["passes"] for r in rows if isinstance(r.get("money"), (int, float)) and r.get("passes"))
        return {"days": len(rows), "money": round(money, 6), "passes": passes,
                "perPass": round(money / passes, 6) if passes else None}
    before = per_pass([d for d in days if d["day"] < day_lo])
    after = per_pass([d for d in days if d["day"] >= day_lo and (day_hi is None or d["day"] <= day_hi)])
    fell = (round((before["perPass"] - after["perPass"]) / before["perPass"], 6)
            if before["perPass"] and after["perPass"] is not None else None)
    records = []
    for line in read_text(os.path.join(directory, "costs", "sessions.jsonl")).splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("ev") in ("reset", "compact") and inside(parse_stamp(r.get("at"))):
            records.append(r)
    resets = sorted((parse_stamp(r["at"]), r) for r in records if r["ev"] == "reset")
    by_reason = {}
    for _t, r in resets:
        key = r.get("reason") if isinstance(r.get("reason"), str) else "(none)"
        by_reason[key] = by_reason.get(key, 0) + 1
    most = max([sum(1 for u, _r in resets if t <= u < t + 3600) for t, _r in resets] or [0])
    journal, markers, standing = [], [], []
    for line in read_text(os.path.join(directory, "journal.jsonl")).splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        t = parse_stamp(as_dict(e).get("at"))
        if not inside(t):
            continue
        journal.append((t, e))
        for kind in ("repeat", "reask", "redo"):
            if isinstance(e.get(kind), str):
                markers.append({"at": e["at"], "kind": kind, "text": e[kind][:300], "pass": e.get("pass")})
        if e.get("standing") in ("added", "removed"):
            standing.append({"at": e["at"], "standing": e["standing"], "entry": str(e.get("entry") or "")[:80]})
    judgement = []
    for t, r in resets:
        for m in markers:
            mt = parse_stamp(m["at"])
            if m["kind"] in ("repeat", "reask") and mt > t:
                passes = {e.get("pass") for u, e in journal if t < u <= mt and e.get("pass") is not None}
                if len(passes) <= 2:
                    judgement.append("reset at {} then {} at {}: {}".format(r["at"], m["kind"], m["at"], m["text"][:120]))
        for folder in ("decisions", ARCHIVE):
            path = os.path.join(directory, folder)
            for name in sorted(os.listdir(path)) if os.path.isdir(path) else []:
                card = as_dict(read_json_safe(os.path.join(path, name)))
                answered = parse_stamp(card.get("answered"))
                if card.get("status") == "answered" and answered is not None and t - 1800 <= answered <= t:
                    judgement.append("reset at {} within 30 min of card {} answered at {}".format(
                        r["at"], card.get("id") or name[:-5], card["answered"]))
    refusals, by_refusal, accepted = 0, {}, 0
    for line in read_text(os.path.join(directory, "session.log")).splitlines():
        stamp, _sp, text = line.partition(" ")
        if not inside(parse_stamp(stamp)):
            continue
        if text.startswith("context not reset"):
            refusals += 1
            for key in {refusal_key(part) for part in reason_parts(text.partition("): ")[2])}:  # t-190 T5: once a line
                by_refusal[key] = by_refusal.get(key, 0) + 1
        elif text.startswith("checkpoint names session ") and text.endswith(": accepted by its time"):
            accepted += 1
    per_pass = {}
    for t, e in journal:
        n = e.get("pass")
        if isinstance(n, int) and not isinstance(n, bool):
            p = per_pass.setdefault(n, {"pass": n, "lines": 0, "from": e["at"], "to": e["at"], "_lo": t, "_hi": t})
            p["lines"] += 1
            if t < p["_lo"]:
                p["_lo"], p["from"] = t, e["at"]
            if t > p["_hi"]:
                p["_hi"], p["to"] = t, e["at"]
    long_passes = [{k: p[k] for k in ("pass", "lines", "from", "to")} for _n, p in sorted(per_pass.items())
                   if p["lines"] > PASS_LONG_LINES or p["_hi"] - p["_lo"] > PASS_LONG_SECONDS]
    passes = {"ids": len(per_pass), "maxLines": max([p["lines"] for p in per_pass.values()] or [0]), "long": long_passes}
    if any(m["kind"] in ("repeat", "reask") for m in markers) or most > 1:
        verdict = "tune"
    elif fell is not None and fell >= 0.25:
        verdict = "keep"
    else:
        verdict = "needs judgement"
    return {"since": since, "until": until, "days": days, "before": before, "after": after, "fell": fell,
            "resets": {"total": len(resets), "byReason": by_reason, "maxPerHour": most},
            "compact": sum(1 for r in records if r["ev"] == "compact"), "markers": markers, "standing": standing,
            "judgement": judgement, "verdict": verdict, "refusals": {"total": refusals, "byReason": by_refusal},
            "acceptedByTime": accepted, "passes": passes}


PASS_LONG_LINES = 20  # a pass id on more journal lines than this, or
PASS_LONG_SECONDS = 1800  # ... spanning longer than this, is listed as long by the follow-up (t-041 J7-R5)


def reason_parts(text):
    """A 'context not reset' line's reasons: split at '; ' outside parentheses (a reason's own words may hold one)."""
    parts, depth, start = [], 0, 0
    for i, c in enumerate(text):
        depth += (c == "(") - (c == ")" and depth > 0)
        if depth == 0 and text.startswith("; ", i):
            parts.append(text[start:i])
            start = i + 2
    return parts + [text[start:]]


def refusal_key(text):
    """A clean-point reason without its ids and numbers, so the follow-up counts like reasons together (J7-R7)."""
    while re.search(r"\([^()]*\)", text):  # innermost first: nested parentheses go whole
        text = re.sub(r"\s*\([^()]*\)", "", text)
    text = re.sub(r"\s+[^\s]*\d[^\s]*", "", " " + text).strip()
    return text[:120]


def context_followup(args):
    directory, _agent = space_for_command(args.space)
    since = args.since or read_context(directory)[0].get("enabledAt")
    if not since or parse_stamp(since) is None:
        raise Problem("--since <UTC time> is needed (context.json has no enabledAt)")
    if args.until and parse_stamp(args.until) is None:
        raise Problem("--until is not a UTC time")
    r = followup_report(directory, since, args.until)
    if args.json:
        print(json.dumps(r))
        return
    print("Since {}{}".format(since, " until " + args.until if args.until else ""))
    for name in ("before", "after"):
        b = r[name]
        print("  {:6}  days {}  USD {}  passes {}  USD/pass {}".format(name, b["days"], b["money"], b["passes"], b["perPass"]))
    print("  USD/pass fell: {}".format("{:.1%}".format(r["fell"]) if r["fell"] is not None else "unknown"))
    print("  resets {} (by reason {}; most in one hour {}); compactions {}".format(
        r["resets"]["total"], r["resets"]["byReason"], r["resets"]["maxPerHour"], r["compact"]))
    print("  refusals {} (by reason {}); checkpoints accepted by their time {}".format(
        r["refusals"]["total"], r["refusals"]["byReason"], r["acceptedByTime"]))
    print("  passes {} (most lines on one {}; long {})".format(
        r["passes"]["ids"], r["passes"]["maxLines"], ", ".join(str(p["pass"]) for p in r["passes"]["long"]) or "none"))
    for m in r["markers"]:
        print("  marker {} at {}: {}".format(m["kind"], m["at"], m["text"]))
    for line in r["judgement"]:
        print("  needs judgement: " + line)
    print("verdict: {} (back out is the follow-up worker's call, from the lines above)".format(r["verdict"]))


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


def read_terminal(read):
    """read() of fd 0 (t-193 R2.3): with SIGTTIN ignored, a read while this launcher is not the terminal's foreground
    group gets EIO instead of stopping it: take the terminal back and read once more; a second EIO sleeps 3 s and
    gives None (the caller loops; it never exits)."""
    for attempt in (0, 1):
        try:
            return read()
        except OSError as e:
            if e.errno != errno.EIO:
                raise
            if attempt == 0:
                give_tty(os.getpgrp())
    time.sleep(3)
    return None


def wait_enter(ctx=None):
    """Waits for Enter: True. A launcher with a record (its watcher may take a restart meanwhile, t-037 round 2)
    also stops waiting, False, as soon as a restart is taken (ctx["after"]), so the exec happens now rather than at
    the next Enter. A closed stdin waits 60 s, as input()'s EOFError did."""
    if not ctx or not ctx.get("launcher"):
        while True:
            try:
                if read_terminal(input) is None:
                    continue
            except EOFError:
                time.sleep(60)
            return True
    until = None
    while True:
        if ctx.get("after"):
            return False
        if until is not None:
            if time.time() >= until:
                return True
            time.sleep(0.5)
            continue
        try:
            ready = select.select([sys.stdin], [], [], 0.5)[0]
        except (OSError, ValueError, TypeError, AttributeError):  # no selectable stdin
            until = time.time() + 60
            continue
        if ready:
            line = read_terminal(sys.stdin.readline)
            if line is None:
                continue
            if line:
                return True
            until = time.time() + 60  # end of input


def error_wait(address, problem, ctx=None):
    """pa-session could not start: show why in the tab and wait for Enter, instead of leaving a
    plain shell that looks like a Project Agent. The label tells the keeper (and people) why. Returns early when a
    restart is taken meanwhile (wait_enter); cmd_session then execs it."""
    message = " ".join(str(problem).split())
    signal.signal(signal.SIGINT, signal.default_int_handler)  # Ctrl-C stops the wait, even after the launcher ignored it
    call(["hc", "label", address, "herdr.pa.error=" + message[:120], "herdr.pa.starting="], check=False)
    print("\n[Project Agent] It cannot start: {}\n"
          "               Fix the cause, then press Enter to try again.".format(message), flush=True)
    wait_enter(ctx)
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
                error_wait(address, e, ctx)
                if ctx.get("after"):  # a restart taken while it waited: the exec now (returns only when it failed)
                    take_after(ctx, ctx.get("project") or project, ctx.get("directory"), address)
    except KeyboardInterrupt:  # the person stops it (in error-wait or before): the tab's shell takes over
        sys.exit(2)
    finally:  # however it ends, it no longer says it is starting or waiting
        reset_tty()  # t-193 R3.1: before the tab's shell gets the terminal
        call(["hc", "label", address, "herdr.pa.error=", "herdr.pa.starting="], check=False)


def restarted_session(args):
    """pa-session exec'd by a restart (t-037 R6; round 2, VERIFY MF4). The new code passed the self-check but may
    still raise later, on the session path: any exception goes to error_wait (the tab says what failed and how to
    recover) instead of ending the process, its terminal and the workers tied to it. Enter execs the installed
    pa.py again with the same flags, so a fixed install is loaded without closing the terminal; that exec first runs
    the restart's own pre-check (round 3, REVIEW A/B M2, VERIFY MF-r2-1): an install half done or a pa.py that cannot
    start is not exec'd, the tab says why and waits again."""
    address = os.environ.get("HC_TERMINAL_ID")
    try:
        return cmd_session(args)
    except Exception as e:  # noqa: BLE001 - whatever the new code raises, the terminal stays
        if not address:
            raise
        traceback.print_exc()
        failure = e
    argv = [sys.executable, os.path.join(ROOT, "tools", "pa.py"), "session"] + sys.argv[1:]
    message = ("the restarted launcher failed: {}: {}. Its terminal and workers stay. Install a fixed pa.py (install.sh "
               "or pa-sync), then press Enter: it loads the installed launcher again".format(type(failure).__name__, failure))
    try:
        while True:
            error_wait(address, message)
            try:
                _pre, why = restart_precheck({}, args.space, args.profile, argv)  # installed.json, argv + --self-check
            except Exception as e:  # noqa: BLE001 - a check that cannot run is a failed check
                why = "the check could not run: {}: {}".format(type(e).__name__, e)
            if why:
                message = ("the installed launcher cannot be loaded yet: {}. Its terminal and workers stay. Install a "
                           "fixed pa.py (install.sh or pa-sync), then press Enter to check again".format(why))
                continue
            signal.signal(signal.SIGINT, signal.SIG_IGN)  # as across a restart's exec (spec S5)
            try:
                os.execv(sys.executable, argv)
            except (OSError, ValueError, TypeError) as e:
                message = "the installed launcher could not be exec'd: {}. Its terminal and workers stay. Press Enter to try again".format(e)
    except KeyboardInterrupt:  # the person stops it: the tab's shell takes over (as cmd_session)
        sys.exit(2)


# ---- the stamp: what is installed (installed.json) and what a launcher loaded (launcher.json), t-037 J1 ----------

def tree_hash(folder, top_py_only=False):
    """sha256 over the sorted relative paths and bytes of the files under folder, skipping what pa-sync skips
    (names with .bak-, .pyc, dotfiles) and __pycache__. top_py_only: only the *.py files directly in folder (tools/).
    Raises OSError when the folder is missing or a file cannot be read."""
    import hashlib
    if not os.path.isdir(folder):
        raise FileNotFoundError("no folder " + folder)
    def fail(error):  # an unreadable folder is an error, not an empty one
        raise error
    names = []
    for here, dirs, files in os.walk(folder, onerror=fail):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__" and not d.startswith(".") and not top_py_only)
        for name in files:
            if ".bak-" in name or name.endswith(".pyc") or name.startswith("."):
                continue
            if top_py_only and not name.endswith(".py"):
                continue
            names.append(os.path.relpath(os.path.join(here, name), folder).replace(os.sep, "/"))
    h = hashlib.sha256()
    for rel in sorted(names):
        hash_file(h, os.path.join(folder, rel), rel)
    return h.hexdigest()


HASH_CHUNK = 1024 * 1024


def hash_file(h, path, rel):
    """Adds one file to h as `rel\0length\0bytes` (J1's form). Follows a symlink (a linked context.md counts by
    content). A FIFO, socket or device is never opened: it is added as `rel\0special\0` (t-037 J2, REVIEW L4).
    Read in chunks, so a large file is never held whole. Raises OSError when it cannot be read."""
    import stat
    if not stat.S_ISREG(os.stat(path).st_mode):
        h.update("{}\0special\0".format(rel).encode())
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))  # a FIFO swapped in after the stat: no hang
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            h.update("{}\0special\0".format(rel).encode())
            return
        h.update("{}\0{}\0".format(rel, st.st_size).encode())
        seen = 0
        while True:
            chunk = f.read(HASH_CHUNK)
            if not chunk:
                break
            seen += len(chunk)
            h.update(chunk)
    if seen != st.st_size:
        raise OSError("{} changed while it was hashed".format(path))


def hash_or_none(folder, top_py_only=False):
    try:
        return tree_hash(folder, top_py_only)
    except OSError:
        return None


def mtime_or_none(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def installed_stamp():
    s = read_json_safe(os.path.join(ROOT, "installed.json"))
    return s if isinstance(s, dict) else None


def read_json_safe(path):
    try:
        return read_json(path)
    except (OSError, ValueError):
        return None


def cmd_stamp(args):
    """Writes ~/.config/herdr-pa/installed.json: the hashes of what is installed now (R1). Every hash is computed
    before anything is written, so a folder that cannot be read leaves the previous stamp in place."""
    try:
        tools = tree_hash(os.path.join(ROOT, "tools"), top_py_only=True)
        core = tree_hash(os.path.join(ROOT, "core"))
        mods_dir = os.path.join(APP, "mods", "project-agent")
        mods = tree_hash(mods_dir) if os.path.isdir(mods_dir) else None
    except OSError as e:
        raise Problem("cannot stamp the install (previous installed.json kept): {}".format(e))
    stamp = {"version": 1, "at": now(), "by": "pa-sync" if args.synced_from else "install.sh",
             "source": {"commit": args.commit, "dirty": bool(args.dirty)} if args.commit else None,
             "tools": tools, "core": core, "mods": mods, "syncedFrom": args.synced_from or None}
    write_json(os.path.join(ROOT, "installed.json"), stamp)
    print("stamped {}: tools {} core {}{}".format(os.path.join(ROOT, "installed.json"), tools[:12], core[:12],
                                                   " ({}{})".format(args.commit[:12], ", dirty" if args.dirty else "") if args.commit else ""))


def write_launcher(directory, record):
    write_json(os.path.join(directory, "launcher.json"), record)


def as_dict(value):
    """value when it is a dict, else {}: a stamp or record with a wrong shape is read as empty (REVIEW L3)."""
    return value if isinstance(value, dict) else {}


def as_text(value):
    return value if isinstance(value, str) else None


LAUNCHER_VERSION = 2  # version 2: the launcher takes requests (accepts); a version-1 record is from J1 code
ACCEPTS = ["restart", "context"]  # t-041 J4: a context request (context-request.json)


def restart_capable(record):
    """True when launcher.json was written by a launcher that takes a restart request (spec S4)."""
    record = as_dict(record)
    version, accepts = record.get("version"), record.get("accepts")
    return (isinstance(version, int) and not isinstance(version, bool) and version >= 2
            and isinstance(accepts, list) and "restart" in accepts)


def launcher_update(ctx, directory, change):
    """The one writer of launcher.json (REVIEW L2): change(record) and the write happen under one lock, so the main
    thread (session, profile) and the watcher (restart outcomes) never lose each other's fields. False when this
    launcher has no record (run_agent called alone)."""
    record = ctx.get("launcher")
    if not record:
        return False
    with ctx.setdefault("launcherLock", threading.Lock()):
        change(record)
        write_launcher(directory, record)
    return True


def profile_record(p):
    folder = p.get("_folder") or os.path.join(ROOT, "profiles", p["name"])
    return {"name": p["name"], "readAt": now(), "mtime": mtime_or_none(os.path.join(folder, "profile.json")),
            "hash": hash_or_none(folder)}


def launcher_start(ctx, address, space, directory, p):
    """launcher.json for this launcher, written after claim (R2). Never stops the launch (AC2.2)."""
    try:
        instance = ctx.setdefault("instance", str(uuid.uuid4()))  # one per process start; a retry keeps it
        ctx.setdefault("launcherLock", threading.Lock())
        me = os.path.realpath(__file__)
        tools = hash_or_none(os.path.dirname(me), top_py_only=True)
        installed = as_dict(installed_stamp())
        commit = (as_text(as_dict(installed.get("source")).get("commit"))
                  if tools is not None and installed.get("tools") == tools else None)
        record = {"version": LAUNCHER_VERSION, "accepts": list(ACCEPTS), "instance": instance, "pid": os.getpid(),
                  "ppid": os.getppid(),  # t-077 O3: diagnostic only; identification reads the live process tree
                  "terminal": address, "space": space,
                  "started": ctx.get("startedAt") or ctx.setdefault("startedAt", now()),
                  "code": {"tools": tools, "file": me, "installed": as_text(installed.get("tools")), "commit": commit},
                  "profile": profile_record(p),
                  "session": None, "restart": None, "restartedFrom": None, "context": None, "lastReset": None}
        restarted = ctx.get("restarted")
        if restarted:  # this process was exec'd by a restart (R6): what it came from
            record["restartedFrom"] = restarted.get("from")
            record["restart"] = {"request": restarted.get("request"), "outcome": "ok", "at": now(),
                                 "by": restarted.get("by"), "why": None}
        ctx["launcher"] = record
        session_log(directory, "launcher instance={} tools={} core={} profile={}@{}".format(
            instance, (tools or "none")[:12], (hash_or_none(os.path.join(ROOT, "core")) or "none")[:12], p["name"],
            record["profile"]["mtime"]))
        launcher_update(ctx, directory, lambda r: None)
    except Exception as e:  # noqa: BLE001 - never stops the Project Agent
        print("[Project Agent] launcher.json not written: {}".format(e), flush=True)
        try:
            session_log(directory, "launcher.json not written: {}".format(e))
        except OSError:
            pass


def launcher_session(ctx, directory, agent, resume, snapshot=False):
    """Rewrites launcher.json's session at each Claude start: core text and context are read then (R2). `prompt`
    says what the model runs with (spec S1): new (a new session), rebuilt (a resume with --system-prompt-snapshot
    off) or recorded (a resume that keeps the prompt recorded at the session's first request); promptCore and
    promptContext are that prompt's core hash and context.md mtime, kept in agent.json `prompt` across launchers."""
    record = ctx.get("launcher")
    if not record:
        return
    try:
        folder = os.path.join(ROOT, "profiles", as_text(as_dict(record.get("profile")).get("name")) or "")
        sid = agent.get("sessionId")
        core, context = hash_or_none(os.path.join(ROOT, "core")), mtime_or_none(os.path.join(folder, "context.md"))
        mode = "new" if not resume else ("rebuilt" if snapshot else "recorded")
        path = os.path.join(directory, "agent.json")
        current = read_json_safe(path)
        current = current if isinstance(current, dict) else {}
        before = as_dict(current.get("prompt"))
        if mode == "recorded":
            prompt_core, prompt_context = ((before.get("core"), before.get("context")) if before.get("session") == sid
                                           else (None, None))
        else:
            prompt_core, prompt_context = core, context
            # The record is the prompt of the session's first request (round 2, REVIEW A M5): written only for a new
            # session; a rebuilt start runs with the current text (reported above) but does not replace it, so a
            # later recorded resume still reports what it really runs with.
            if current and mode == "new":
                current["prompt"] = {"session": sid, "core": core, "context": context, "at": now()}
                write_json(path, current)
        session = {"id": sid, "resume": bool(resume), "at": now(), "core": core, "context": context,
                   "prompt": mode, "promptCore": prompt_core, "promptContext": prompt_context}
        launcher_update(ctx, directory, lambda r: r.__setitem__("session", session))
    except Exception as e:  # noqa: BLE001 - never stops the Project Agent
        print("[Project Agent] launcher.json not written: {}".format(e), flush=True)


def code_state(launcher, installed, terminal):
    """(word, detail) for R3: current | older (tools) | older (core) | older (tools, core) | unknown."""
    if not isinstance(installed, dict) or not as_text(installed.get("tools")):
        return "unknown", "no installed.json on this machine (run install.sh or pa-sync)"
    if not isinstance(launcher, dict):
        return "unknown", "started before restart support (no launcher.json)"
    if terminal and launcher.get("terminal") != terminal:
        return "unknown", "launcher.json is from another terminal ({})".format(launcher.get("terminal"))
    tools = as_text(as_dict(launcher.get("code")).get("tools"))
    session = as_dict(launcher.get("session"))
    recorded = session.get("prompt") == "recorded"
    # The core text the model runs with (spec S1): on a resume without --system-prompt-snapshot off it is the one
    # recorded when the session began, not the one read at this start. A J1 record has no promptCore: its core.
    core = as_text(session.get("promptCore") if "promptCore" in session else session.get("core"))
    if not tools or not core:
        if tools and recorded:
            return "unknown", "the core text this resumed session runs with is not known (pa-restart --fresh loads the current one)"
        return "unknown", "launcher.json has no {} hash".format("tools" if not tools else "core")
    older = [n for n, a in (("tools", tools), ("core", core)) if a != installed.get(n)]
    if older:
        return "older ({})".format(", ".join(older)), "runs tools {} core {}; installed tools {} core {}{}".format(
            tools[:12], core[:12], (as_text(installed.get("tools")) or "")[:12], (as_text(installed.get("core")) or "")[:12],
            "; the resumed session keeps the core text it began with until it compacts (pa-restart --fresh loads it now)"
            if recorded and "core" in older else "")
    return "current", "tools {} core {}".format(tools[:12], core[:12])


def space_code_state(space, terminal):
    directory = os.path.join(ROOT, "spaces", space) if space and ID.match(space) else None
    launcher = read_json_safe(os.path.join(directory, "launcher.json")) if directory else None
    return code_state(launcher, installed_stamp(), terminal)


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
    if getattr(args, "restarted", None) and "restarted" not in ctx:  # exec'd by a restart (t-037 R6)
        ctx["restarted"] = {"request": args.restarted, "from": getattr(args, "from_instance", None),
                            "by": getattr(args, "restart_by", None) or "pa-restart", "done": False}
    lock = launch_lock(space)
    try:
        directory, agent, previous = claim(args, address, labels, space, project, p, existing, directory)
        launcher_start(ctx, address, space, directory, p)  # t-037 R2: what this launcher loaded
    finally:
        lock.close()  # releases the lock
    restarted = ctx.get("restarted")
    if restarted and not restarted["done"]:
        restarted["done"] = True
        ctx.setdefault("formerInstances", set()).add(restarted.get("from"))
        remove_request(directory, restarted["request"])
        call(["hc", "label", address, "herdr.pa.restarting="], check=False)
        session_log(directory, "restarted request={} from={} by={}".format(restarted["request"], restarted.get("from"),
                                                                           restarted.get("by")))
    # The heartbeat the previous launcher left: the only sign that a killed session ran. Kept from
    # this launcher's first claim; on a retry the file holds this launcher's own beats.
    ctx.setdefault("previous", previous)
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl-C belongs to the agent, not this wrapper
    # t-193 R2.1: no terminal access by anything in this launcher's group can stop it (a stopped launcher is a tab
    # whose shell took the terminal back); child_setup gives its Claude the defaults again.
    for sig in JOB_SIGNALS:
        signal.signal(sig, signal.SIG_IGN)
    if "restartNext" not in ctx:  # t-193 R5.2: pa-restart --recover --fresh asked for a new conversation
        ctx["restartNext"] = True
        if take_restart_next(directory):
            args.fresh = True
            session_log(directory, "restart-next: this start is a new conversation (pa-restart --recover --fresh)")
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


def reload_profile(p):
    """The profile as it is now, by the same name, so a restart in run_agent's loop sees an edit made since the
    launcher began (t-033); the one in hand when it cannot be read now (missing, malformed, refused by load_profile).
    run_agent also keeps the one in hand when no launch can be built from the new one (round 2), and launches with
    it when the new one's program cannot be started (round 4)."""
    try:
        return load_profile(p["name"])
    except Exception:  # noqa: BLE001 - a restart never fails because of it
        return p


def launch_command(p, agent, resume, directory, snapshot=False, version=None, prompt=None):
    """(argv, model) for one launch of the Project Agent from profile p; raises when p cannot give one (also when
    the exec would refuse the argv: an element not a str, or with a NUL byte). version: the claude command's Claude
    Code version (cc_version), which gates --autocompact and idleCompaction (t-041 J3); None leaves both out.
    prompt: a new session's first prompt (t-041 J4, the fresh context's start prompt); a resume keeps its own."""
    argv = list(p["claude"])
    model = (agent.get("models") or {}).get("orchestrator") or (p.get("models") or {}).get("orchestrator")
    if model:
        argv += ["--model", model]
    argv += mcp_args(p)
    argv += ["--resume", agent["sessionId"]] if resume else ["--session-id", agent["sessionId"]]
    argv += ["--name", "PA: " + agent["name"], "--append-system-prompt", layered(p, "orchestrator", directory, agent)]
    if snapshot:  # t-037 S2: the appended text reaches the model on a resume too (not the one recorded at its start)
        argv += ["--system-prompt-snapshot", "off"]
    net = context_launch(directory, version)["net"]
    if net:  # t-041 R5: the 400k net, a flag (not inherited by anything the Project Agent starts), before the prompt
        argv += ["--autocompact", str(net)]
    if resume:
        line = active_rules_line(directory) if directory else None  # t-190 R1
        rules = " " + line if line else ""
        argv += ["Your session was restarted. Read your state folder (ledger.json, journal.jsonl, decisions/, "
                 "rules.json), then say in one line where things stand. Continue where you left off without "
                 "asking, as orchestrator.md defines it under \"After a restart or a compaction\": read the "
                 "reports that came in, take the next planned step, and start a fresh worker for a job whose "
                 "worker stopped. Start nothing else until asked: no new task, no move to a phase that needs "
                 "the person, no worker for a task with an open card. If the last hold or resume line in the "
                 "journal is a hold, start nothing at all until the person says go. `paused` in "
                 "`schedule.json` stops timed passes only; it is not a hold." + rules]
    elif prompt:  # t-041 J4: the fresh context's start prompt, where the resume prompt sits
        if not isinstance(prompt, str) or prompt.startswith("-"):
            raise Problem("a start prompt must be text that does not start with '-'")
        argv += [prompt]
    argv += pa_settings_args(p, agent.get("space") or os.path.basename(directory), directory, version)  # last (t-029 R4, t-040, t-041)
    argv = resolve(argv, p)
    # The exec refuses anything else (TypeError / ValueError); refuse it here, so run_agent's trial build of a
    # re-read profile fails and the loop keeps the one in hand (t-033 round 3). A no-op for every valid profile.
    for a in argv:
        if not isinstance(a, str) or "\0" in a:
            raise Problem("profile {}: the launch command has an argument that is not text or holds a NUL byte: "
                          "{!r} (check models.orchestrator and the profile's strings)".format(p.get("name"), a)[:300])
    return argv, model


# ---- the launcher's own Claude child (t-037 J2, R6/R9; the hooks t-041 J4 uses; its group t-184) -----------------
# run_child starts Claude with a Popen handle kept in ctx["child"], in a process group of its own (spawn; its pgid,
# the child's pid, in ctx["childGroup"]); end_child ends that group, and only it: the handle and the pgid spawn made
# (never a pid or group looked up). A wrapper between pa.py and Claude that exits on SIGTERM without ending what it
# started no longer leaves that Claude running on the terminal (t-184). With a terminal, the child's group is its
# foreground group while it runs; the launcher takes the terminal back when it ends (R3). ctx["ended"] tells the
# loop the exit was ours, not a crash; ctx["after"] is what the loop does once that child has exited
# ({"kind": "exec", ...} for a restart; t-041 adds "fresh").

END_GRACE = 20  # seconds between SIGTERM and SIGKILL to the launcher's own Claude child's group
KILL_WAIT = 5  # seconds end_child waits for the group after SIGKILL before it logs what is left and goes on
GROUP_POLL = 0.1  # seconds between two looks at the group
JOB_SIGNALS = (signal.SIGTTOU, signal.SIGTTIN, signal.SIGTSTP)  # ignored by the launcher, the defaults in its child (t-193)


def foreground_tty():
    """True when fd 0 is a terminal and this process's group is its foreground group (then the child gets it)."""
    try:
        return os.isatty(0) and os.tcgetpgrp(0) == os.getpgrp()
    except OSError:
        return False


def give_tty(group):
    """Makes `group` the terminal's foreground group; SIGTTOU blocked for the call (a background group calling
    tcsetpgrp gets it otherwise), the mask restored after. Never raises."""
    try:
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTTOU})
    except (OSError, ValueError, AttributeError):
        return
    try:
        os.tcsetpgrp(0, group)
    except OSError:
        pass
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def child_setup(tty):
    """The child's first steps after the fork, before the exec: a process group of its own and, when the launcher
    had the terminal, that group in the foreground. Only setpgid / sigmask / tcsetpgrp (no lock the launcher's
    threads could hold). A failure starts the child as before rather than not at all. t-193 R2.2: first the job-control
    signals the launcher ignores (run_session) back to their defaults (an ignored signal stays ignored across exec)."""
    def setup():
        for sig in JOB_SIGNALS:
            try:
                signal.signal(sig, signal.SIG_DFL)
            except (OSError, ValueError):
                pass
        try:
            os.setpgid(0, 0)
        except OSError:
            return
        if tty:
            give_tty(os.getpgrp())
    return setup


def spawn(argv, cwd):
    tty = foreground_tty()
    child = subprocess.Popen(argv, cwd=cwd, preexec_fn=child_setup(tty))
    try:
        os.setpgid(child.pid, child.pid)  # the race-free pair with the child's own call (EACCES after its exec)
    except OSError:
        pass
    child.pa_group, child.pa_tty = child.pid, tty
    return child


def child_lock(ctx):
    """One lock between starting the child (run_child) and taking a restart (begin_restart): either the restart sees
    the child and ends it, or run_child sees the restart and starts nothing (round 2, REVIEW A M2)."""
    return ctx.setdefault("childLock", threading.Lock())


def group_size(group):
    """How many live (not zombie) processes are in `group`, from `ps` (read-only); None when ps cannot tell."""
    try:
        out = subprocess.run(["ps", "-A", "-o", "pgid=", "-o", "stat="], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, universal_newlines=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    n = 0
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == str(group) and not parts[1].startswith("Z"):
            n += 1
    return n


def run_child(ctx, argv, cwd):
    """subprocess.call(argv, cwd=cwd), keeping the handle in ctx["child"] while it runs (same exceptions at start).
    When a restart was taken before the start, nothing starts: None, ended "restart" (the loop execs).
    It returns only when the child's group is gone (t-184 R2, R5): after an end the launcher took, once end_child
    has finished; after an exit of its own, it ends what the child left in its group (logged)."""
    with child_lock(ctx):
        if ctx.get("after"):
            ctx["ended"] = "restart"
            return None
        done = threading.Event()
        child = spawn(argv, cwd)
        ctx["child"], ctx["childGroup"], ctx["childDone"] = child, getattr(child, "pa_group", None), done
    try:
        return child.wait()
    except BaseException:  # as subprocess.call: the child does not outlive an error here
        child.kill()
        child.wait()
        raise
    finally:
        try:
            settle_child(ctx, child, done)
        finally:
            ctx["child"] = None


def settle_child(ctx, child, done):
    """After the child's wait (any end): the terminal back to the launcher when the child had it (R3), then its
    group gone (R2, R5). Only for a child spawn put in a group (a test's stand-in has none)."""
    group = ctx.get("childGroup")
    if not group or getattr(child, "pa_group", None) != group:
        return
    if getattr(child, "pa_tty", False):
        give_tty(os.getpgrp())
    try:
        settle_group(ctx, group, done)
    finally:
        reset_tty()  # t-193 R3.1: once the group is gone, what it left on in the terminal is turned off


def settle_group(ctx, group, done):
    if ctx.get("ended"):  # the launcher is ending it (end_child, the watcher thread): wait until that has finished
        done.wait(END_GRACE + KILL_WAIT + 5)
        return
    left = group_size(group)
    if left:
        end_child(ctx, None)
        directory = ctx.get("directory")
        if directory:
            try:
                session_log(directory, "exit left {} in group: ended".format(left))
            except OSError:
                pass


def mark_started(ctx):
    """agent.json sessionStarted, before the launcher ends its child, when the session ran >= RAN s or its
    transcript exists: the next start resumes it."""
    running, directory = ctx.get("running") or {}, ctx.get("directory")
    if not running.get("since") or not directory:
        return
    if time.time() - running["since"] < RAN and transcript_state(ctx.get("project"), running.get("session")) is not True:
        return
    path = os.path.join(directory, "agent.json")
    current = read_json(path, {}) or {}
    if current.get("sessionId") == running.get("session") and not current.get("sessionStarted"):
        current["sessionStarted"] = True
        write_json(path, current)
    running["marked"] = True


def end_child(ctx, why, grace=None):
    """Ends the launcher's own Claude child and its process group (the pgid spawn made, t-184): SIGTERM to the
    handle and the group; after the handle's exit, waits for the group's last member until `grace` seconds
    (END_GRACE) from the TERM; then SIGKILL to what is left and a short bounded wait (KILL_WAIT), after which it logs
    what is still there and goes on (never hangs). Returns the handle's exit code, or None when no child runs.
    why=None: what a child that exited by itself left in its group (no ended mark, the handle not signalled).
    With end_leftovers (t-193; its steps end_pids, also pa-restart --recover's) the only code in pa.py that signals a
    process; this one never the launcher's own group, never a pid looked up."""
    child = ctx.get("child")
    if child is None:
        return None
    group = ctx.get("childGroup") if getattr(child, "pa_group", None) == ctx.get("childGroup") else None
    done = ctx.get("childDone") if group else None
    if why is not None:
        ctx["ended"] = why
        try:
            mark_started(ctx)
        except (OSError, ValueError):
            pass
    if group == os.getpgrp():  # never this launcher's own group (a failed setpgid in the child)
        group = None

    def to_group(sig):
        try:
            os.killpg(group, sig)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:  # a member it may not signal: count it as there
            return True
        except OSError:
            return False

    def group_gone(until):
        looks = 0
        while to_group(0):
            looks += 1
            if looks % 10 == 0 and group_size(group) == 0:  # only zombies left, waiting for their reaper
                return True
            if time.time() >= until:
                return False
            time.sleep(GROUP_POLL)
        return True
    try:
        began = time.time()
        limit = END_GRACE if grace is None else grace
        if why is not None:
            try:
                child.send_signal(signal.SIGTERM)
            except OSError:
                pass
        if group:
            to_group(signal.SIGTERM)
            to_group(signal.SIGCONT)  # t-193 R1.4: a stopped member acts on the SIGTERM now, not at SIGKILL
        try:
            code = child.wait(timeout=max(0.0, began + limit - time.time()))
        except subprocess.TimeoutExpired:
            code = None
        if group is None:
            if code is None:
                child.kill()
                code = child.wait()
            return code
        if code is not None and group_gone(began + limit):
            return code
        if code is None:
            child.kill()
        to_group(signal.SIGKILL)
        if code is None:
            code = child.wait()
        if not group_gone(time.time() + KILL_WAIT):
            directory = ctx.get("directory")
            if directory:
                try:
                    session_log(directory, "end: {} processes still in group {} after SIGKILL".format(
                        group_size(group), group))
                except OSError:
                    pass
        return code
    finally:
        if done is not None:
            done.set()


# ---- what an earlier launch left on the launcher's terminal (t-193) ------------------------------------------------
# A Claude started before t-184 ran in the launcher's own group; ending only its wrapper left it there, on the tab's
# terminal. When the next Claude took the terminal's foreground, that leftover's next terminal access stopped the
# launcher's whole group (SIGTTOU) and the tab's shell took the terminal back (spec R1-R3). run_agent therefore ends,
# before every Claude start, what is on its terminal and is neither this launcher, its ancestors (the tab's shell) nor
# its descendants (its own calls): end_leftovers. With end_child it is the only code here that signals a process
# (pa-restart --recover reuses its steps, end_pids). It runs only in a Herdr tab (HC_TERMINAL_ID names the launcher's
# terminal) with fd 0 a terminal, so a test runner's own terminal is never swept.

SWEEP_PS = ["ps", "-A", "-o", "pid=", "-o", "ppid=", "-o", "pgid=", "-o", "uid=", "-o", "tty=", "-o", "stat=",
            "-o", "etime=", "-o", "lstart="]
ARGV_PS = ["ps", "-A", "-ww", "-o", "pid=", "-o", "command="]
PS_TIMEOUT = 5
NO_TTY = ("", "?", "??", "-")


def run_ps(argv):
    """ps's stdout, or None when it is missing, fails, or takes longer than PS_TIMEOUT."""
    try:
        r = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           universal_newlines=True, timeout=PS_TIMEOUT)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return r.stdout if r.returncode == 0 else None


def etime_seconds(text):
    """ps's elapsed time ([[dd-]hh:]mm:ss) in seconds, or None."""
    days, _, rest = text.rpartition("-")
    parts = rest.split(":")
    if not 2 <= len(parts) <= 3 or not all(p.isdigit() for p in parts) or (days and not days.isdigit()):
        return None
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + int(p)
    return seconds + (int(days) * 86400 if days else 0)


def ps_rows(argv=False):
    """Every process on this machine from one ps read (Linux and macOS print the same columns): a list of
    {pid, ppid, pgid, uid, tty, stat, etime, lstart, at}, `at` the read instant. A malformed line is skipped (never
    read as pid 0 or -1); None when ps cannot be read. argv=True adds `argv` (a list; Linux: /proc/<pid>/cmdline,
    else ps's command split on whitespace)."""
    at = time.time()
    out = run_ps(SWEEP_PS)
    if out is None:
        return None
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 12 or not all(p.isdigit() for p in parts[:4]):
            continue
        pid, ppid, pgid, uid = (int(p) for p in parts[:4])
        etime = etime_seconds(parts[6])
        if pid <= 0 or etime is None:
            continue
        rows.append({"pid": pid, "ppid": ppid, "pgid": pgid, "uid": uid, "tty": parts[4], "stat": parts[5],
                     "etime": etime, "lstart": " ".join(parts[7:]), "at": at})
    if argv:
        commands = {}
        for line in (run_ps(ARGV_PS) or "").splitlines():
            head, _, command = line.strip().partition(" ")
            if head.isdigit():
                commands[int(head)] = command.split()
        for r in rows:
            r["argv"] = proc_argv(r["pid"]) or commands.get(r["pid"]) or []
    return rows


def proc_argv(pid):
    """A process's argv elements from /proc (Linux), or None."""
    try:
        with open("/proc/{}/cmdline".format(pid), "rb") as f:
            data = f.read()
    except OSError:
        return None
    if not data:
        return None
    return [a.decode("utf-8", "replace") for a in data.rstrip(b"\0").split(b"\0")]


def launcher_tty():
    """fd 0's terminal as ps names it (pts/3, ttys003), or None when fd 0 is not a terminal."""
    try:
        if not os.isatty(0):
            return None
        name = os.ttyname(0)
    except OSError:
        return None
    return name[len("/dev/"):] if name.startswith("/dev/") else name


def ancestors_of(pid, by_pid):
    seen, here = set(), by_pid.get(pid)
    while here and here["ppid"] > 0 and here["ppid"] not in seen and len(seen) < 1000:
        seen.add(here["ppid"])
        here = by_pid.get(here["ppid"])
    return seen


def descendants_of(pids, rows):
    """Every process below `pids` by ppid (not pids themselves)."""
    kids = {}
    for r in rows:
        kids.setdefault(r["ppid"], []).append(r["pid"])
    found, todo = set(), list(pids)
    while todo:
        for k in kids.get(todo.pop(), ()):
            if k not in found and k not in pids:
                found.add(k)
                todo.append(k)
    return found


def leftover_targets(rows, me, tty, uid):
    """(targets, foreign) of R1.1 from one ps read: on terminal `tty` (compared exactly), not `me`, its ancestors or its
    live descendants, pid > 1, not a zombie; with what each started (any tty). foreign: those of another uid (never
    signalled, R1.5)."""
    if not tty or tty in NO_TTY:
        return [], []
    rows = [r for r in rows if isinstance(r.get("pid"), int) and r["pid"] > 1 and not str(r.get("stat", "")).startswith("Z")]
    by_pid = {r["pid"]: r for r in rows}
    spared = {me} | ancestors_of(me, by_pid) | descendants_of({me}, rows)
    first = {r["pid"] for r in rows if r.get("tty") == tty and r["pid"] not in spared}
    # Only a leftover it signals brings what it started; one of another uid (left, R1.5) does not (t-193 REVIEW M1).
    chosen = first | (descendants_of({p for p in first if by_pid[p].get("uid") == uid}, rows) - spared)
    picked = [by_pid[p] for p in sorted(chosen)]
    return [r for r in picked if r.get("uid") == uid], [r for r in picked if r.get("uid") != uid]


def signal_pid(pid, sig):
    """The one call that signals a leftover (tests replace it). Never pid <= 1 or this process."""
    if not isinstance(pid, int) or pid <= 1 or pid == os.getpid():
        raise ProcessLookupError(pid)
    os.kill(pid, sig)


def started_utc(r):
    try:
        return iso_time(r["at"] - r["etime"])
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return "unknown"


def end_pids(targets, directory, label, extra=None):
    """R1.2's steps for `targets` (ps rows): SIGTERM, SIGCONT, a wait up to END_GRACE, SIGKILL to what is left, a wait up
    to KILL_WAIT. Before every signal the pid is read again and signalled only with the same start time and tty.
    One session.log line per pid: `<label> ended pid= tty= started= how=term|kill`, `<label> left ... why=` or `<label>
    not signalled pid= tty=: another process has its pid now`. extra(r): words added to each line. Returns (ended,
    left) as lists of rows."""
    extra = extra or (lambda r: "")
    ended, left, live = [], [], list(targets)

    def line(text):
        if directory:
            try:
                session_log(directory, text)
            except OSError:
                pass

    def tag(r):
        more = extra(r)
        return "pid={} tty={} started={}{}".format(r["pid"], r.get("tty") or "?", started_utc(r), " " + more if more else "")

    def now_rows():
        rows = ps_rows()
        return None if rows is None else {r["pid"]: r for r in rows}

    def same(r, cur):
        c = cur.get(r["pid"])
        return bool(c) and c.get("lstart") == r.get("lstart") and c.get("tty") == r.get("tty") and not str(c.get("stat", "")).startswith("Z")

    def send(sigs, how):
        cur = now_rows()
        if cur is None:
            for r in live:
                left.append(r)
                line("{} left {} why=ps-unreadable".format(label, tag(r)))
            live[:] = []
            return
        keep = []
        for r in live:
            c = cur.get(r["pid"])
            if not c or str(c.get("stat", "")).startswith("Z"):
                ended.append(dict(r, how=how))
                line("{} ended {} how={}".format(label, tag(r), how))
                continue
            if not same(r, cur):
                line("{} not signalled pid={} tty={}: another process has its pid now".format(label, r["pid"], r.get("tty") or "?"))
                continue
            try:
                for sig in sigs:
                    signal_pid(r["pid"], sig)
            except ProcessLookupError:
                ended.append(dict(r, how=how))
                line("{} ended {} how={}".format(label, tag(r), how))
                continue
            except PermissionError:
                left.append(r)
                line("{} left {} why=not-permitted".format(label, tag(r)))
                continue
            except OSError as e:
                left.append(r)
                line("{} left {} why={}".format(label, tag(r), e.errno))
                continue
            keep.append(r)
        live[:] = keep

    def wait(until, how):
        while live:
            cur = now_rows()
            if cur is not None:
                for r in list(live):
                    if not same(r, cur):
                        live.remove(r)
                        ended.append(dict(r, how=how))
                        line("{} ended {} how={}".format(label, tag(r), how))
            if not live or time.time() >= until:
                return
            time.sleep(GROUP_POLL * 2)

    def how(sig):  # the log's how=: term, kill
        return sig.name[len("SIG"):].lower()
    send((signal.SIGTERM, signal.SIGCONT), how(signal.SIGTERM))
    wait(time.time() + END_GRACE, how(signal.SIGTERM))
    if live:
        send((signal.SIGKILL,), how(signal.SIGKILL))
        wait(time.time() + KILL_WAIT, how(signal.SIGKILL))
    for r in live:
        left.append(r)
        line("{} left {} why=still-running".format(label, tag(r)))
    return ended, left


def end_leftovers(ctx, address):
    """R1.1-R1.5: before a Claude start, ends what an earlier launch left on this launcher's terminal (see above).
    Never raises; the start goes on whatever it could not end."""
    directory = ctx.get("directory")

    def once(why):
        said = ctx.setdefault("sweepSaid", set())
        if why in said or not directory:
            return
        said.add(why)
        try:
            session_log(directory, "leftovers: not swept: " + why)
        except OSError:
            pass
    try:
        if not address or os.environ.get("HC_TERMINAL_ID") != address:
            return once("not in a Herdr tab (HC_TERMINAL_ID does not name this launcher's terminal)")
        tty = launcher_tty()
        if not tty:
            return once("fd 0 is not a terminal")
        rows = ps_rows()
        if rows is None:
            return once("ps could not be read")
        targets, foreign = leftover_targets(rows, os.getpid(), tty, os.getuid())
        for r in foreign:
            if directory:
                session_log(directory, "leftover left pid={} tty={} started={} why=another-user".format(
                    r["pid"], r.get("tty") or "?", started_utc(r)))
        if targets:
            end_pids(targets, directory, "leftover")
    except Exception as e:  # noqa: BLE001 - never stops the start (R2 outranks R1)
        once("{}: {}".format(type(e).__name__, e))


# ---- the terminal after a child (t-193 R3) -------------------------------------------------------------------------

TTY_RESET = ("\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1005l\x1b[?1006l\x1b[?1015l\x1b[?1004l\x1b[?2004l\x1b[<u"
             "\x1b[?25h\x1b[?1049l")  # mouse modes, focus, bracketed paste off; kitty keyboard pop; cursor; main screen
TTY_SETTLE = 0.2  # seconds for the terminal's late replies before the input is flushed


def reset_tty():
    """Turns off what a Claude left on in the terminal (mouse reports, focus, paste) and drops replies not read, so
    the tab's shell or the next Claude never reads them as typed input. Nothing when fd 0 is not a terminal."""
    try:
        if not os.isatty(0):
            return
        os.write(0, TTY_RESET.encode())
        time.sleep(TTY_SETTLE)
        import termios
        termios.tcflush(0, termios.TCIFLUSH)
    except Exception:  # noqa: BLE001 - OSError, termios.error: never stops the launcher
        pass


# ---- the restart in place (t-037 R4, R6, R7; spec S1-S5) ---------------------------------------------------------
# pa-restart (or the panel) writes spaces/<id>/restart-request.json naming this launcher's instance. The watcher
# thread takes it when the turn has ended, checks that the installed code loads (restart_precheck), ends its own
# Claude child (end_child) and leaves ctx["after"]; the main loop then execs the installed pa.py in this process
# (exec_restart): the pid, the terminal, its address and its workers stay. A request names no command, path or pid.

REQUEST = "restart-request.json"
REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")  # a uuid; never a leading `-` (it reaches the argv)
REQUEST_BY = ("pa-restart", "panel", "orchestrator")  # R4; "exit" is the launcher's own (R7), never a file's
REQUEST_MAX = 3600  # a request expires at most 60 min after it was written
WATCH_EVERY = 2  # seconds between two looks at the request file
SELF_CHECK_TIMEOUT = 20
PROBE_TIMEOUT = 10  # `<claude> --help`, to learn whether it knows --system-prompt-snapshot


def snapshot_flag(p, ctx=None):
    """Whether this launch passes --system-prompt-snapshot off (spec S2): the profile key systemPromptSnapshot
    ("off" | "keep") decides when set; else whether `<the profile's claude command> --help` lists the flag, probed
    once per launcher process and command. Never raises: unknown means not passed."""
    value = p.get("systemPromptSnapshot")
    if value == "off":
        return True
    if value == "keep":
        return False
    cache = (ctx if ctx is not None else {}).setdefault("snapshotProbe", {})
    key = json.dumps(p.get("claude"))
    if key not in cache:
        cache[key] = probe_snapshot(p)
    return cache[key]


def run_bounded(argv, timeout, env=None):
    """(exit code, output bytes) of argv, ended by its own handle after `timeout` s (TimeoutExpired). Output goes to
    a temp file, not a pipe, so a program that leaves a child holding its output cannot hold the wait past it."""
    with tempfile.TemporaryFile() as out:
        r = subprocess.run(argv, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, timeout=timeout, env=env)
        out.seek(0)
        return r.returncode, out.read()


def probe_snapshot(p):
    try:
        _code, output = run_bounded(resolve(list(p["claude"]) + ["--help"], p), PROBE_TIMEOUT)
        return b"--system-prompt-snapshot" in output
    except Exception:  # noqa: BLE001 - not known: the flag is not passed
        return False


def read_request(directory):
    """(request, None) when restart-request.json holds a well-formed request; (None, why) when it is not; (None,
    None) when there is none."""
    path = os.path.join(directory, REQUEST)
    try:
        text = read_text(path)
    except OSError as e:
        return None, "unreadable: {}".format(e)
    if not text and not os.path.exists(path):
        return None, None
    try:
        req = json.loads(text)
    except ValueError:
        return None, "not JSON"
    if not isinstance(req, dict):
        return None, "not an object"
    for key in ("id", "space", "instance", "terminal", "at", "expires", "by"):
        if not isinstance(req.get(key), str) or not req[key]:
            return None, "field {!r} missing or not text".format(key)
    # Every field is checked before use (round 2, REVIEW A M1): shape, characters, length.
    for key in ("id", "space"):
        if not REQUEST_ID.match(req[key]):
            return None, "bad {} (letters, digits, _ and -, starting with a letter or digit, at most 80)".format(key)
    for key in ("instance", "terminal"):
        if len(req[key]) > 200 or any(c.isspace() or not c.isprintable() for c in req[key]):
            return None, "bad {} (printable, no spaces, at most 200)".format(key)
    if req["by"] not in REQUEST_BY:
        return None, "field 'by' is not one of {}".format(", ".join(REQUEST_BY))
    for key in ("at", "expires"):
        if len(req[key]) > 40:
            return None, "field {!r} is too long".format(key)
    for key in ("now", "fresh"):
        if not isinstance(req.get(key, False), bool):
            return None, "field {!r} is not true or false".format(key)
    why = req.get("why")
    if why is not None and not isinstance(why, str):
        return None, "field 'why' is not text"
    if why is not None and (len(why) > 2000 or any((ord(c) < 32 and c not in "\t\n") or ord(c) == 127 for c in why)):
        return None, "field 'why' is longer than 2000 or has control characters"
    at, expires = parse_time(req["at"]), parse_time(req["expires"])
    if at is None or expires is None:
        return None, "at/expires are not times"
    req["_at"], req["_expires"] = at, expires
    return req, None


def remove_request(directory, request_id=None, text=None):
    """Deletes restart-request.json when it is still the one meant (same id, or same text): a newer request written
    meanwhile stays."""
    path = os.path.join(directory, REQUEST)
    try:
        current = read_text(path)
        if request_id is not None:
            try:
                if as_dict(json.loads(current)).get("id") != request_id:
                    return False
            except ValueError:
                return False
        elif text is not None and current != text:
            return False
        os.remove(path)
        return True
    except OSError:
        return False


def request_current(directory, req):
    """None when restart-request.json still holds this request (same fields) and it has not expired; else why not
    (round 2, VERIFY MF2): a request pa-restart withdrew, a newer one, or one past its expiry is never acted on."""
    current, why = read_request(directory)
    if current is None:
        return "the request was withdrawn (pa-restart stopped waiting)" if not why else "the request is unreadable now: " + why
    if current["id"] != req.get("id"):
        return "the request was replaced by a newer one ({})".format(current["id"])
    if ({k: v for k, v in current.items() if not k.startswith("_")}
            != {k: v for k, v in req.items() if not k.startswith("_")}):
        return "the request changed after it was taken"
    if time.time() > current["_expires"]:
        return "the request expired before the restart"
    return None


def restart_outcome(ctx, directory, request_id, outcome, why, by=None):
    entry = {"request": request_id, "outcome": outcome, "why": why, "at": now(), "by": by}
    try:
        launcher_update(ctx, directory, lambda r: r.__setitem__("restart", entry))
    except Exception:  # noqa: BLE001 - never stops the Project Agent
        pass
    session_log(directory, "restart {} request={}: {}".format(outcome, request_id, why))


def agent_turn(address):
    """idle | working | blocked | unknown: the Project Agent's turn, from hc's status of its terminal."""
    word = status_word(hc_get(address))
    if word in ("working", "blocked"):
        return word
    return "idle" if word else "unknown"


def restart_argv(ctx, rid, by, fresh, project, directory):
    """The exec's argv (R6): the installed pa.py with every value as --flag=value, so no value is read as a flag
    (round 2, REVIEW A M1). The pre-check's self-check parses exactly this argv (REVIEW B M3)."""
    agent = as_dict(read_json_safe(os.path.join(directory, "agent.json")))
    record = as_dict(ctx.get("launcher"))
    argv = [sys.executable, os.path.join(ROOT, "tools", "pa.py"), "session",
            "--space=" + (as_text(agent.get("space")) or os.path.basename(directory)), "--project=" + project,
            "--profile=" + (as_text(agent.get("profile")) or as_text(as_dict(record.get("profile")).get("name")) or "")]
    if as_text(agent.get("name")):
        argv.append("--name=" + agent["name"])
    argv += ["--restarted=" + rid, "--from-instance=" + (as_text(record.get("instance")) or ""), "--restart-by=" + by]
    if fresh:
        argv.append("--fresh")
    return argv


def restart_precheck(ctx, space, profile, argv=None):
    """({tools, core, argv}, None) when the installed launcher may be exec'd: installed.json matches the installed
    files (no install half done) and the installed file's self-check, run as the exec's own argv plus --self-check
    (so its parser reads every flag the exec passes), passes within SELF_CHECK_TIMEOUT s. (None, why) otherwise;
    nothing is ended either way."""
    installed = as_dict(installed_stamp())
    if not as_text(installed.get("tools")):
        return None, "no installed.json on this machine (run install.sh or pa-sync)"
    tools = hash_or_none(os.path.join(ROOT, "tools"), top_py_only=True)
    core = hash_or_none(os.path.join(ROOT, "core"))
    if tools != installed.get("tools"):
        return None, "the installed tools do not match installed.json (an install not finished?)"
    if core != installed.get("core"):
        return None, "the installed core text does not match installed.json (an install not finished?)"
    target = os.path.join(ROOT, "tools", "pa.py")
    if argv is None:
        argv = [sys.executable, target, "session", "--space=" + (space or ""), "--profile=" + (profile or "")]
    try:
        code, output = run_bounded(list(argv) + ["--self-check"], SELF_CHECK_TIMEOUT,
                                   env=dict(os.environ, HERDR_PA_HOME=ROOT, HERDR_APP_HOME=APP))  # this launcher's homes
    except subprocess.TimeoutExpired:
        return None, "the installed launcher's self-check did not finish within {} s".format(SELF_CHECK_TIMEOUT)
    except (OSError, ValueError) as e:
        return None, "the installed launcher's self-check could not run: {}".format(e)
    last = output.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
    if code:
        return None, "the installed launcher's self-check failed (exit {}): {}".format(code, last[0][:200])
    try:
        result = as_dict(json.loads(last[0]))
    except ValueError:
        result = {}
    if result.get("ok") is not True or result.get("tools") != tools:
        return None, "the installed launcher's self-check printed no matching result: {}".format(last[0][:200])
    return {"tools": tools, "core": core, "argv": list(argv)}, None


def cmd_self_check(args):
    """pa.py session --self-check: what a restart runs before it ends anything. Imports and runs, without starting
    or writing anything and without hc, what the launcher uses after its start (C2): the cost module, the profile,
    the launch command, the hashes. Prints one JSON line; exit 0 only when all of it worked."""
    import hashlib  # noqa: F401 - imported later by the launcher (tree_hash)
    import importlib.util  # noqa: F401 - costs_module
    import socket  # noqa: F401 - stamp_script
    import stat  # noqa: F401 - hash_file
    p = load_profile(args.profile)
    space = local_id(args.space or "") or "self-check"
    directory = os.path.join(ROOT, "spaces", space)
    agent = {"sessionId": str(uuid.uuid4()), "name": "self-check", "space": space, "terminal": "", "project": ""}
    launch_command(p, agent, True, directory, snapshot=True)
    launch_command(p, agent, False, directory)
    costs = costs_module()
    tools = tree_hash(os.path.join(ROOT, "tools"), top_py_only=True)
    core = tree_hash(os.path.join(ROOT, "core"))
    print(json.dumps({"ok": True, "tools": tools, "core": core, "profile": p["name"], "costs": costs is not None,
                      "file": os.path.realpath(__file__)}))


def watch_once(ctx, directory, address, space):
    """One look at restart-request.json (the watcher thread, every WATCH_EVERY s)."""
    if ctx.get("restarting") or ctx.get("after"):
        return
    path = os.path.join(directory, REQUEST)
    if not os.path.exists(path):
        ctx.pop("waitingFor", None)
        return
    try:
        text = read_text(path)
    except OSError:
        return
    req, why = read_request(directory)
    if req is None:
        if why:
            restart_outcome(ctx, directory, None, "refused", "unreadable request: " + why)
            remove_request(directory, text=text)
        return
    rid, by = req["id"], req["by"]
    me = as_dict(ctx.get("launcher"))

    def refuse(reason):
        ctx.pop("waitingFor", None)
        restart_outcome(ctx, directory, rid, "refused", reason, by)
        remove_request(directory, rid)
    if req["instance"] in ctx.get("formerInstances", ()):
        remove_request(directory, rid)  # left from before this launcher's exec: silently
        return
    if req["space"] != space:
        return refuse("the request is for space {}, not {}".format(req["space"], space))
    if req["instance"] != me.get("instance"):
        return refuse("the request is for launcher instance {}, not this one".format(req["instance"]))
    if req["terminal"] != address:
        return refuse("the request is for terminal {}, not {}".format(req["terminal"], address))
    if req["_expires"] - req["_at"] > REQUEST_MAX:
        return refuse("the request expires more than {} min after it was written".format(REQUEST_MAX // 60))
    if time.time() > req["_expires"]:
        return refuse("still mid-turn at expiry" if ctx.get("waitingFor") == rid else "the request expired")
    if not req.get("now"):
        turn = agent_turn(address)
        if turn in ("working", "blocked"):
            ctx["waitingFor"] = rid  # it restarts when this turn ends
            return
    ctx.pop("waitingFor", None)
    agent = read_json_safe(os.path.join(directory, "agent.json"))
    profile = as_text(as_dict(agent).get("profile")) or as_text(as_dict(me.get("profile")).get("name"))
    project = ctx.get("project") or as_text(as_dict(agent).get("project")) or os.getcwd()
    pre, why = restart_precheck(ctx, space, profile,
                                restart_argv(ctx, rid, by, req.get("fresh") is True, project, directory))
    if not pre:
        restart_outcome(ctx, directory, rid, "failed", why, by)
        remove_request(directory, rid)
        return
    stale = request_current(directory, req)  # the pre-check took a while: still the same request? (VERIFY MF2)
    if stale:
        restart_outcome(ctx, directory, rid, "refused", stale, by)
        remove_request(directory, rid)  # only when it is still this one (expired)
        return
    begin_restart(ctx, directory, address, req, pre)


def begin_restart(ctx, directory, address, req, pre):
    ctx["restarting"] = req["id"]
    call(["hc", "label", address, "herdr.pa.restarting=" + req["id"]], check=False)
    session_log(directory, "restart taken request={} by={} now={}: ending the agent".format(req["id"], req["by"],
                                                                                           bool(req.get("now"))))
    with child_lock(ctx):  # run_child either started its child before (ended below) or will start none
        ctx["after"] = {"kind": "exec", "request": {k: v for k, v in req.items() if not k.startswith("_")}, "pre": pre,
                        "by": req["by"], "file": True}
    end_child(ctx, "restart")  # no child running (between two launches, a wait): the loop takes ctx["after"] next


def watcher(ctx, directory, address, space):
    looked = 0.0
    while True:
        try:
            watch_once(ctx, directory, address, space)
        except Exception:  # noqa: BLE001 - the watcher never stops the launcher
            pass
        if time.time() - looked >= CONTEXT_EVERY:  # t-041 J4: the fresh reset's check, after the restart's
            looked = time.time()
            try:
                context_once(ctx, directory, address, space)
            except Exception:  # noqa: BLE001
                pass
        time.sleep(WATCH_EVERY)


def exec_restart(ctx, after, project, directory, address):
    """Execs the installed pa.py in this process (R6). Returns only when it could not: the outcome is `failed` (or
    `refused` for a request withdrawn, replaced or expired since it was taken), the label and the request are
    removed, and the loop goes on (resuming as after /exit)."""
    req = as_dict(after.get("request"))
    rid, by = req.get("id"), after.get("by") or req.get("by") or "pa-restart"
    pre = as_dict(after.get("pre"))
    agent = as_dict(read_json_safe(os.path.join(directory, "agent.json")))
    record = as_dict(ctx.get("launcher"))
    failed, outcome, lock = None, "failed", None
    argv = restart_argv(ctx, rid, by, req.get("fresh") is True, project, directory)
    if pre.get("tools") and hash_or_none(os.path.join(ROOT, "tools"), top_py_only=True) != pre["tools"]:
        failed = "the installed tools changed after the check (an install running?)"
    elif pre.get("argv") is not None and pre["argv"] != argv:
        failed = "the launch arguments changed after the check"
    elif after.get("file"):
        # pa-restart withdraws its request under the launch lock; holding it from this read to the exec means a
        # withdrawn request is never exec'd (VERIFY MF2). The file is close-on-exec: the new process can take it.
        try:
            lock = launch_lock(as_text(agent.get("space")) or os.path.basename(directory), wait=10)
        except (Problem, OSError) as e:
            failed = "the space could not be locked: {}".format(e)
        else:
            stale = request_current(directory, req)
            if stale:
                failed, outcome = stale, "refused"
    try:
        if not failed:
            session_log(directory, "restart exec request={} from={}".format(rid, record.get("instance")))
            print("\n[Project Agent] Restarting in place (request {}): loading the installed launcher.".format(rid), flush=True)
            try:
                sys.stdout.flush()
                sys.stderr.flush()
                # SIGINT ignored across the exec (spec S5), whatever path leads here (round 3, REVIEW A/B M1: from
                # cmd_session's error_wait it is handled): a Ctrl-C before the new launcher's claim cannot end the
                # terminal and its workers. The new process ignores it again in run_session.
                previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
                try:
                    os.execv(sys.executable, argv)
                    failed = "exec returned"
                finally:  # only when the exec did not happen
                    signal.signal(signal.SIGINT, previous)
            except (OSError, ValueError, TypeError) as e:  # ValueError: a NUL in a value (REVIEW A M1)
                failed = "exec: {}".format(e)
    finally:
        if lock is not None:
            lock.close()
    restart_outcome(ctx, directory, rid, outcome, failed, by)
    remove_request(directory, rid)  # only when it is still this one
    call(["hc", "label", address, "herdr.pa.restarting="], check=False)
    ctx.pop("restarting", None)
    print("[Project Agent] The restart did not happen: {}. The agent resumes here.".format(failed), flush=True)


def take_after(ctx, project, directory, address):
    after = ctx.pop("after", None)
    if after and after.get("kind") == "exec":
        exec_restart(ctx, after, project, directory, address)
    elif after and after.get("kind") == "fresh":  # t-041 J4: the loop starts a new session with the start prompt
        ctx["freshNext"] = after


def reload_after_exit(ctx, project, directory, address, space):
    """R7: Claude exited on its own and an install changed the tools since this launcher started: the same
    pre-check and exec as a restart (by "exit"). Otherwise, or when the pre-check fails (said once), nothing."""
    record = as_dict(ctx.get("launcher"))
    mine = as_text(as_dict(record.get("code")).get("tools"))
    installed = as_text(as_dict(installed_stamp()).get("tools"))
    if not record or not mine or not installed or installed == mine:
        return
    agent = as_dict(read_json_safe(os.path.join(directory, "agent.json")))
    rid = "exit-" + str(uuid.uuid4())
    pre, why = restart_precheck(ctx, space, as_text(agent.get("profile")) or as_text(as_dict(record.get("profile")).get("name")),
                                restart_argv(ctx, rid, "exit", False, project, directory))
    if not pre:
        if ctx.get("reloadSaid") != installed:
            ctx["reloadSaid"] = installed
            print("[Project Agent] The tools were installed again, but not reloading them: {}".format(why), flush=True)
            session_log(directory, "not reloading after exit: {}".format(why))
        return
    exec_restart(ctx, {"kind": "exec", "request": {"id": rid, "by": "exit"}, "pre": pre, "by": "exit"},
                 project, directory, address)


def run_agent(args, address, project, p, directory, agent, ctx, started):
    running = ctx.setdefault("running", {"since": None, "session": None, "marked": False})
    ctx["directory"], ctx["project"] = directory, project

    wake = ctx.setdefault("beatWake", threading.Event())  # set by starting(): a session's RAN mark is timed from it

    def beat():
        # The tab bar reads this: a live terminal whose heartbeat stopped has lost its launcher.
        # Also keeps the last ledger that parsed, so a half-written or mangled one can be restored.
        ledger, seen = os.path.join(directory, "ledger.json"), None
        while True:
            pause = 10
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
            # t-041 J3 round 2: the RAN mark is not left to the 10 s beat's phase (the probes before a start shift it,
            # and a beat just short of RAN, the next after the tab was killed, left a session that ran unresumed):
            # wake at the start + RAN, or at a new start.
            since = running["since"]
            if since and not running["marked"]:
                pause = min(pause, max(0.05, since + RAN - time.time() + 0.05))
            wake.wait(pause)
            wake.clear()
    space = agent.get("space") or os.path.basename(directory)
    child_lock(ctx)  # made before the watcher thread exists
    if not ctx["beat"]:  # one heartbeat thread, also when a start is retried after an error
        threading.Thread(target=beat, daemon=True).start()
        if ctx.get("launcher"):  # the request watcher (t-037 R6); a separate thread keeps the heartbeat's pace
            threading.Thread(target=lambda: watcher(ctx, directory, address, space), daemon=True).start()
        ctx["beat"] = True
    quick, quick_resume_fails = 0, 0
    while True:
        take_after(ctx, project, directory, address)  # a restart taken while no Claude ran (returns if it failed)
        fresh_next = ctx.pop("freshNext", None)  # t-041 J4: a fresh context was taken
        fresh = reload_profile(p)
        agent = read_json(os.path.join(directory, "agent.json"), agent)
        resume, why = should_resume(agent, directory, project, before=started, heartbeat=ctx.get("previous"))
        if args.fresh or fresh_next:
            resume, why = False, "fresh" if args.fresh else "context"
        if not resume:
            old = agent.get("sessionId")
            if why in ("fresh", "context") and isinstance(old, str) and old:  # the person can /resume it
                previous = [s for s in agent.get("previousSessions") or [] if isinstance(s, str) and s != old]
                agent["previousSessions"] = ([old] + previous)[:PREVIOUS_MAX]
            agent["sessionId"], agent["sessionStarted"] = str(uuid.uuid4()), False
        prompt = (fresh_prompt(directory, resets_today(directory) + 1, fresh_next.get("reason"), fresh_next.get("from"))
                  if fresh_next else None)
        args.fresh = False
        agent["updated"] = now()
        write_json(os.path.join(directory, "agent.json"), agent)
        held, adopted = p, False
        if fresh is not p:
            # The re-read profile is used only when a launch can be built from it (t-033 round 2): a file that
            # loads but has a wrong shape in a key read here (models a list, path a number, ...) keeps the old one.
            try:
                snap, ver = snapshot_flag(fresh, ctx), cc_version(fresh, ctx)  # t-041 J3: the version gates the net
                argv, model = launch_command(fresh, agent, resume, directory, snapshot=snap, version=ver, prompt=prompt)
                p, adopted = fresh, fresh != held
            except Exception:  # noqa: BLE001 - a restart never fails because of the reload
                fresh = p
        if fresh is p:
            snap, ver = snapshot_flag(p, ctx), cc_version(p, ctx)  # t-041 J3: the version gates the net
            argv, model = launch_command(p, agent, resume, directory, snapshot=snap, version=ver, prompt=prompt)

        def starting(model):  # what is written as a launch starts; returns its start time
            ttl, by = cache_in_effect(p, os.environ)  # t-040: the cache lifetime this start asks for
            plan = context_launch(directory, ver)  # t-041 J3: what this start asks for (net=, idle=); notes once, before
            said = ctx.setdefault("contextSaid", set())
            for note in plan["notes"]:
                if note not in said:
                    said.add(note)
                    session_log(directory, note)
            if fresh_next and not fresh_next.get("recorded"):  # t-041 J4: before the start line (it stays last)
                fresh_next["recorded"] = True
                fresh_records(ctx, directory, address, fresh_next, agent["sessionId"], prompt)
            session_log(directory, "start resume={} session={} reason={} cache={} cacheBy={} net={} idle={}".format(
                resume, agent["sessionId"], why, ttl, by, plan["net"] or ("off" if plan["mode"] == "off" else "none"),
                "off" if plan["idle"] else "kept"))
            began = time.time()
            running.update(since=began, session=agent["sessionId"], marked=False)
            wake.set()  # the heartbeat times this session's RAN mark from now
            launcher_session(ctx, directory, agent, resume, snapshot=snap)
            if adopted:  # t-033's re-read profile is the one running now (J1 Recorded limit L1)
                launcher_update(ctx, directory, lambda r: r.__setitem__("profile", profile_record(p)))
            costs = costs_module()
            if costs:
                try:
                    costs.record_agent_start(directory, agent, address, project, model, resume, profile=p, command=argv)
                except Exception:  # noqa: BLE001 - never stops the Project Agent
                    pass
            return began
        if ctx.get("after"):  # taken while this launch was built (the --help probe): the exec, before any Claude starts
            take_after(ctx, project, directory, address)
            continue
        end_leftovers(ctx, address)  # t-193 R1.1: what an earlier launch left on this terminal, before every start
        began = starting(model)
        try:
            code = run_child(ctx, argv, project)
        except (OSError, ValueError, TypeError) as e:
            # t-033 round 4: a changed profile whose program the exec could not start (a raise, not an exit code:
            # a lone surrogate, a folder or non-program as claude, an argument list too long, ...) does not stop
            # the launcher: this launch uses the profile in hand. Its own build and start are not guarded, so a
            # failure there is raised as before.
            if not adopted:
                raise
            session_log(directory, "the re-read profile did not start ({}); launching with the profile in hand"
                        .format(ascii(e)[:200]))
            p, adopted = held, False
            snap, ver = snapshot_flag(p, ctx), cc_version(p, ctx)  # t-041 J3: the version gates the net
            argv, model = launch_command(p, agent, resume, directory, snapshot=snap, version=ver, prompt=prompt)
            end_leftovers(ctx, address)
            began = starting(model)
            code = run_child(ctx, argv, project)
        running["since"] = None
        lasted = time.time() - began
        ended = ctx.pop("ended", None)  # the launcher ended it (end_child): not a crash, not a quick exit
        session_log(directory, "exit code={} after={:.0f}s{}".format(code, lasted, " ended=" + ended if ended else ""))
        if ended:
            print("\n[Project Agent] The agent was ended by its launcher ({}).".format(ended), flush=True)
            take_after(ctx, project, directory, address)  # the restart's exec; returns only when it failed
            continue
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
        reload_after_exit(ctx, project, directory, address, space)  # R7: an install since this launcher began
        if quick >= 3:
            print("[Project Agent] It exited {} times within seconds, so it is not restarting by itself.\n"
                  "               Fix the cause (see the output above), then press Enter to try again.".format(quick), flush=True)
            wait_enter(ctx)  # also ends when a restart is taken meanwhile: the loop's top execs it
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


COSTS_FILE = os.path.join(os.path.dirname(os.path.realpath(__file__)), "pa_costs.py")
STATUSLINE_FILE = os.path.join(os.path.dirname(COSTS_FILE), "pa_statusline.py")  # the status line's launcher (t-111)
_COSTS = {}


def costs_module():
    """pa_costs.py beside this file, loaded once, or None when it is missing or broken: cost records are never
    allowed to stop a launch (t-009 R1 AC1.5)."""
    if COSTS_FILE in _COSTS:
        return _COSTS[COSTS_FILE]
    module = None
    try:
        import importlib.util
        me = sys.modules.get(__name__)
        me = me if me is not None and getattr(me, "__dict__", None) is globals() else None
        saved = sys.modules.get("pa")
        if me is not None:
            sys.modules["pa"] = me  # pa_costs imports pa: give it this one, not a second copy
        try:
            spec = importlib.util.spec_from_file_location("pa_costs", COSTS_FILE)
            module = importlib.util.module_from_spec(spec)
            # t-135: no tools/__pycache__/pa_costs.*.pyc (the loader writes it before running the code). Not restored:
            # pa_costs sets the same flag itself when it runs, so the state after is what it was before t-135.
            sys.dont_write_bytecode = True
            spec.loader.exec_module(module)
        finally:
            if me is not None:
                if saved is None:
                    sys.modules.pop("pa", None)
                else:
                    sys.modules["pa"] = saved
    except Exception:  # noqa: BLE001 - any failure here only means: no cost record
        module = None
    _COSTS[COSTS_FILE] = module
    return module


def cmd_launch(kind, argv):
    explicit = None
    if len(argv) >= 2 and argv[0] == "--profile":
        explicit, argv = argv[1], argv[2:]
    walked = []
    p, pa_space = profile_for_terminal(explicit, walked)
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
    if pa_space and ID.match(pa_space) and not passthrough:
        # A worker of a Project Agent: its session id and one start record, for the space's costs (t-009 R1).
        costs = costs_module()
        if costs:
            try:
                command = costs.record_launch(space_dir(pa_space), pa_space, kind, argv, command, walked, profile=p,
                                              own=len(p[kind]) + len(argv))  # t-177 R5: the person's part of the command
            except Exception:  # noqa: BLE001 - never stops a launch
                pass
        if kind == "claude":  # Claude Code's own cost figure, through its status line: the last --settings (t-029 R4)
            command += cc_settings_args(p, pa_space, argv)
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
        a["code"], a["codeNote"] = space_code_state(a.get("space"), a.get("terminal"))  # t-037 R3
        if (a["live"] or args.all) and not (getattr(args, "older", False) and a["code"] == "current"):
            rows.append(a)
    if args.json:
        print(json.dumps(rows, indent=2))
    elif not rows:
        if getattr(args, "older", False):
            print("No Project Agent on this machine runs older code." + ("" if args.all else " (--all also checks stopped ones)"))
        else:
            print("No Project Agents running on this machine." + ("" if args.all else " (--all lists stopped ones)"))
    else:
        for a in rows:
            code = a["code"] if a["code"] != "unknown" else "unknown: " + a["codeNote"]
            print("{}\t{}\t{}\t{}\t{}\t{}{}".format(a.get("address"), a.get("name"), a.get("project"), a.get("profile"), a.get("space"), code, "" if a["live"] else "\t(stopped)"))


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


# ---- pa-restart: the person's command (or the Project Agent's at their word), t-037 R5 --------------------------

RESTART_POLL = 1.0  # seconds between two reads of launcher.json while pa-restart waits
RESTART_WAIT = 180
RESTART_WAIT_MAX = 540  # below hc exec's 600 s, so `--on` gets the answer
RELAY_MARGIN = 60  # `--on`: hc exec's --timeout is the wait plus this (at most 600)
REQUEST_LIFE = 600  # a request expires 10 min after it was written


def restart_wait(value):
    return max(0.0, min(float(RESTART_WAIT if value is None else value), RESTART_WAIT_MAX))


def relay(argv):
    """Runs argv with this terminal's output (pa-restart --on): its exit code."""
    return subprocess.run(argv).returncode


def last_start(directory):
    """The time of the last Claude start in the space's session.log, or None."""
    try:
        with open(os.path.join(directory, "session.log")) as f:
            lines = f.read().splitlines()
    except (OSError, ValueError):
        return None
    for line in reversed(lines):
        parts = line.split()
        if len(parts) > 1 and parts[1] == "start":
            return parts[0]
    return None


def r10_message(agent, record, workers, directory=None):
    started = (as_text(as_dict(record).get("started")) or (last_start(directory) if directory else None)
               or "before launcher.json existed")
    names = ", ".join("{} {}".format(a, n) for a, n in workers) or "none"
    space = as_text(as_dict(agent).get("space")) or "ID"
    return ("This Project Agent's launcher started before restart support ({}). It cannot take a restart request; "
            "it can only be restarted by ending its terminal, which also ends its {} worker(s): {}. Run "
            "`pa-restart --space {} --end-terminal` when they are done, or add `--anyway`.".format(
                started, len(workers), names, space))


def live_workers(directory, agent):
    """(address, name) of the live terminals that are this Project Agent's workers: recorded in its ledger and not
    finished, or children of its terminal (any it has run in)."""
    terms = live_terminals() or {}
    mine = {t for t in as_dict(agent).get("terminals") or [] if isinstance(t, str)} | {agent.get("terminal")}
    recorded = set()
    for task in as_dict(read_json_safe(os.path.join(directory, "ledger.json"))).get("tasks") or []:
        for w in as_dict(task).get("workers") or []:
            w = as_dict(w)
            if isinstance(w.get("address"), str) and not w.get("finished"):
                recorded.add(w["address"])
    rows = []
    for address, info in sorted(terms.items()):
        info = as_dict(info)
        if address == agent.get("terminal") or info.get("state") != "live":
            continue
        if info.get("parent") in mine or address in recorded:
            rows.append((address, info.get("name") or ""))
    return rows


def check_terminal(directory, agent):
    """(terminal, hc record) when R5's checks (a), (c), (d) hold: agent.json open and naming a terminal, a fresh
    heartbeat naming it, and hc's terminal live, labelled for the space, with no parent. Else Problem."""
    space = agent.get("space")
    if agent.get("closed"):
        raise Problem("this space's Project Agent is closed ({}); open it first".format(agent.get("closed")))
    terminal = as_text(agent.get("terminal"))
    if not terminal:
        raise Problem("agent.json names no terminal for this Project Agent")
    problems = []
    hb = read_heartbeat(directory)
    if not hb:
        problems.append("no heartbeat: its launcher is not running")
    elif hb[1] != local_id(terminal):
        problems.append("the heartbeat names terminal {}, not {}".format(hb[1], terminal))
    elif time.time() - hb[0] > FRESH:
        problems.append("the heartbeat is {:.0f} s old: its launcher is not running".format(time.time() - hb[0]))
    term = as_dict(hc_get(terminal))
    if term.get("state") != "live":
        problems.append("terminal {} is not live ({})".format(terminal, term.get("state") or "not found"))
    elif as_dict(term.get("labels")).get("herdr.pa") != space:
        problems.append("terminal {} has no herdr.pa label for this space".format(terminal))
    elif term.get("parent"):
        problems.append("terminal {} has a parent ({}): not a Project Agent's tab".format(terminal, term.get("parent")))
    if problems:
        raise Problem("cannot restart: " + "; ".join(problems))
    return terminal, term


def identify_launcher(directory, agent):
    """(launcher record, hc record) of the space's running launcher when every R5 check holds, else Problem naming
    the check that failed. Nothing is written or ended here."""
    terminal, term = check_terminal(directory, agent)
    space = agent.get("space")
    record = read_json_safe(os.path.join(directory, "launcher.json"))
    if not restart_capable(record):
        raise Problem(r10_message(agent, record, live_workers(directory, agent), directory))
    instead = " (pa-restart --space {} --end-terminal ends its terminal instead)".format(space)
    if record.get("space") != space or record.get("terminal") != terminal:
        raise Problem("cannot restart: launcher.json is for terminal {} in space {}, not terminal {}{}".format(
            record.get("terminal"), record.get("space"), terminal, instead))
    if term.get("pid") != record.get("pid"):
        wrapped = wrapper_of(term)
        if wrapped is None:  # R4: a tab that is not Herdr's wrapper (a shell where it was typed) stays refused
            raise Problem("cannot restart: terminal {} runs pid {} ({}), launcher.json says pid {}: not the launcher "
                          "that wrote it{}".format(terminal, term.get("pid"), seen_text(term.get("argv")),
                                                   record.get("pid"), instead))
        named = any(w == "--space" or w.startswith("--space=") for w in wrapped)
        if not launcher_words(wrapped) or (named and not pa_session_argv(wrapped, space)):
            # d-187 (t-065): the wrapper itself must run a launcher of this space (or a --ask first tab, d-181 O1),
            # not something else whose child happens to be one (a pa-session exec'd in a wrapped shell)
            raise Problem("cannot restart: terminal {} runs pid {} ({}), launcher.json says pid {}: the wrapper runs "
                          "no launcher of this space{}".format(terminal, term.get("pid"), seen_text(None, wrapped),
                                                               record.get("pid"), instead))
        why = child_link(term.get("pid"), record.get("pid"))
        if why:  # R1 (a), (b): the record's pid is the wrapper's live launcher child, or it is refused
            raise Problem("cannot restart: terminal {} runs pid {} ({}), launcher.json says pid {}: {}{}".format(
                terminal, term.get("pid"), seen_text(None, wrapped), record.get("pid"), why, instead))
    return record, term


def loaded_text(record, installed=None):
    """What a launcher record says it runs, in one line (pa-restart); core is `older` when installed.json differs."""
    code, session = as_dict(record.get("code")), as_dict(record.get("session"))
    profile = as_dict(record.get("profile"))
    tools, commit = as_text(code.get("tools")) or "", as_text(code.get("commit"))
    mode, core = session.get("prompt"), as_text(session.get("promptCore") if "promptCore" in session else session.get("core"))
    if mode == "recorded":
        core_text = ("core text: still the text this conversation began with ({}): this Claude Code keeps a resumed "
                     "conversation's prompt until it compacts; pa-restart --fresh loads it now".format((core or "unknown")[:12]))
    else:
        older = installed is not None and core != as_text(as_dict(installed).get("core"))
        core_text = "core text: {} ({})".format("older" if older else "current", (core or "unknown")[:12])
    return "tools {} ({}), {}, profile {} read {}, session {} {}".format(
        tools[:12] or "unknown", "commit " + commit[:12] if commit else "no commit", core_text, profile.get("name"),
        profile.get("readAt"), session.get("id"), "resumed" if session.get("resume") else "new")


def journal_caller(entry):
    """Appends entry to the journal of the Project Agent whose terminal runs this command (pa-restart --on from its
    Claude, round 2): nothing when this terminal is not a local Project Agent's."""
    address = os.environ.get("HC_TERMINAL_ID")
    if not address:
        return False
    space = local_id(as_text(as_dict(as_dict(hc_get(address)).get("labels")).get("herdr.pa")) or "")
    if not space or not ID.match(space):
        return False
    directory = space_dir(space)
    if as_dict(read_json_safe(os.path.join(directory, "agent.json"))).get("terminal") != address:
        return False
    with open(os.path.join(directory, "journal.jsonl"), "a") as journal:
        journal.write(json.dumps(dict({"at": now()}, **entry)) + "\n")
    return True


def restart_flags(args):
    flags = ["--space", args.space]
    for name in ("now", "fresh", "dry_run", "json", "recover"):
        if getattr(args, name, False):
            flags.append("--" + name.replace("_", "-"))
    if args.why is not None:
        flags += ["--why", args.why]
    if args.wait is not None:
        wait = restart_wait(args.wait)
        flags += ["--wait", str(int(wait)) if wait == int(wait) else str(wait)]
    for name in ("expect_instance", "by"):  # t-037 J4: the panel's two hidden flags, passed through
        if getattr(args, name, None) is not None:
            flags += ["--" + name.replace("_", "-"), getattr(args, name)]
    return flags


def cmd_restart(args):
    """pa-restart: asks the space's launcher to restart in place (R5). It checks that the terminal's program is that
    space's launcher, says what runs now and which workers keep running, writes restart-request.json and waits for
    what the new launcher loaded. It ends nothing itself; the launcher ends its own Claude and execs."""
    in_claude = os.environ.get("CLAUDECODE") not in (None, "", "0")
    if getattr(args, "recover", False) and (args.now or getattr(args, "expect_instance", None) is not None
                                             or getattr(args, "end_terminal", False) or getattr(args, "anyway", False)):
        raise Problem("--recover is used alone or with --fresh, --json, --dry-run, --why, --on (not --now, "
                      "--expect-instance, --end-terminal or --anyway)")
    if getattr(args, "end_terminal", False):  # t-037 J3, R10: the person's command only, also with --on
        if in_agent():  # every agent marker, as pa-task (round 2, RL-J3-2)
            raise Problem("pa-restart --end-terminal is the person's command: it ends the Project Agent's terminal and "
                          "its workers. Ask the person to run it.")
        return end_terminal(args)
    if in_claude and not (args.why or "").strip():  # also for --on (round 2, REVIEW A M4)
        raise Problem("inside Claude, pa-restart needs --why quoting the person's own words")
    by_panel = getattr(args, "by", None) == "panel"
    if by_panel and in_claude:  # t-037 J4: the orchestrator's requests keep their own `by`
        raise Problem("--by panel is the panel's own flag; it is refused inside Claude")
    if args.on:
        if not args.space:
            raise Problem("--on needs --space <id> (the space's id on that machine)")
        wait = restart_wait(args.wait)
        timeout = min(600, int(math.ceil(wait)) + RELAY_MARGIN)  # hc exec's default 60 s would end a longer wait
        script = '"$HOME/.config/herdr-pa/tools/pa.py" restart ' + " ".join(shlex.quote(a) for a in restart_flags(args))
        if in_claude:
            journal_caller({"by": "orchestrator", "trigger": "conversation", "why": "restart: " + args.why,
                            "on": args.on, "space": args.space})
        began = time.time()
        code = relay(["hc", "exec", "--on", args.on, "--timeout", str(timeout), "--", "sh", "-c", script])
        if code == 127:
            print("pa-restart is not installed on {}: run pa-sync --on {} first".format(args.on, args.on), file=sys.stderr)
        elif code and time.time() - began >= timeout * 0.9:
            print("pa-restart on {} gave no answer within {} s (hc exec's timeout): whether it restarted is unknown. "
                  "See its tab, or run pa-restart --on {} --space {} --dry-run".format(args.on, timeout, args.on, args.space),
                  file=sys.stderr)
            if args.json:
                print(json.dumps({"outcome": "unknown", "why": "hc exec timed out after {} s".format(timeout)}))
            sys.exit(3)
        if code:
            sys.exit(code)
        return
    if getattr(args, "recover", False):  # t-193 R5: on the space's machine (--on relayed above)
        return cmd_recover(args, "panel" if by_panel else "orchestrator" if in_claude else "pa-restart")
    directory, agent = space_for_command(args.space)
    record, term = identify_launcher(directory, agent)
    space, terminal = agent.get("space"), agent.get("terminal")
    expected = getattr(args, "expect_instance", None)
    if expected is not None and record.get("instance") != expected:  # t-037 J4: only the launcher the panel showed
        raise Problem("cannot restart: its launcher restarted since you looked (now instance {}, not {}); look "
                      "again".format(record.get("instance"), expected))
    by = ("orchestrator" if in_claude and os.environ.get("HC_TERMINAL_ID") == terminal
          else "panel" if by_panel else "pa-restart")
    installed = as_dict(installed_stamp())
    workers = live_workers(directory, agent)
    turn = agent_turn(terminal)
    lines = ["Project Agent {} (space {}) in terminal {}, pid {}.".format(agent.get("name"), space, terminal, term.get("pid")),
             "Runs now: " + loaded_text(record, installed) + ".",
             "Installed: tools {}, core {}.".format((as_text(installed.get("tools")) or "none")[:12],
                                                     (as_text(installed.get("core")) or "none")[:12])]
    if workers:
        lines.append("Workers ({}), each keeps running; its reports still reach {}:".format(len(workers), terminal))
        lines += ["  {} {}: keeps running".format(a, n) for a, n in workers]
    else:
        lines.append("No workers running.")
    if args.now:
        lines.append("--now: it restarts at once; its current turn is cut off.")
    elif turn in ("working", "blocked"):
        lines.append("It is mid-turn ({}): it restarts when this turn ends.".format(turn))
    else:
        lines.append("It restarts when this turn ends (at once when it is idle).")
    if args.fresh:
        lines.append("--fresh: it starts a new conversation; the previous one ({}) stays in /resume.".format(agent.get("sessionId")))
    for line in lines:
        print(line)
    if args.dry_run:
        print("Dry run: nothing written.")
        return
    at = time.time()
    rid = str(uuid.uuid4())
    req = {"version": 1, "id": rid, "space": space, "instance": record.get("instance"), "terminal": terminal,
           "at": iso_time(at), "expires": iso_time(at + REQUEST_LIFE), "by": by, "why": args.why,
           "now": bool(args.now), "fresh": bool(args.fresh)}
    if by == "orchestrator":
        with open(os.path.join(directory, "journal.jsonl"), "a") as journal:
            journal.write(json.dumps({"at": now(), "by": "orchestrator", "trigger": "conversation",
                                      "why": "restart: " + args.why, "request": rid}) + "\n")
    write_json(os.path.join(directory, REQUEST), req)
    if by == "orchestrator":
        print("Requested ({}); the restart happens when this turn ends.".format(rid))
        return
    wait = restart_wait(args.wait)
    print("Requested ({}); waiting up to {:.0f} s.".format(rid, wait), flush=True)
    deadline, old = time.time() + wait, record.get("instance")
    while True:
        rec = as_dict(read_json_safe(os.path.join(directory, "launcher.json")))
        outcome = as_dict(rec.get("restart"))
        if outcome.get("request") == rid and outcome.get("outcome") in ("refused", "failed"):
            print("The restart did not happen ({}): {}".format(outcome.get("outcome"), outcome.get("why")), file=sys.stderr)
            if args.json:
                print(json.dumps({"request": rid, "outcome": outcome.get("outcome"), "why": outcome.get("why")}))
            sys.exit(1)
        if outcome.get("request") == rid and outcome.get("outcome") == "ok" and rec.get("restartedFrom") == old:
            if rec.get("session") or time.time() >= deadline:
                print("Restarted {} in place: terminal {}, pid {} (unchanged).".format(agent.get("name"), terminal, rec.get("pid")))
                print("Loaded: " + loaded_text(rec, as_dict(installed_stamp())) + ".")
                if args.json:
                    print(json.dumps({"request": rid, "outcome": "ok", "launcher": rec}))
                return
        if time.time() >= deadline:
            break
        time.sleep(RESTART_POLL)
    lock = launch_lock(space, wait=10)
    try:
        removed = remove_request(directory, rid)
    finally:
        lock.close()
    print("The restart did not happen within {:.0f} s ({}; {}).".format(
        wait, "it is mid-turn" if agent_turn(terminal) in ("working", "blocked") else "the launcher did not answer",
        "the request was withdrawn" if removed else "a newer request is waiting"), file=sys.stderr)
    sys.exit(1)


# ---- pa-restart --recover: a launcher that is not running (t-193 R5) -----------------------------------------------
# A launcher stopped by its terminal (or killed) writes no heartbeat, so pa-restart refuses ("its launcher is not
# running") and no request can reach it. --recover, on the space's machine, ends what is left of it (its stopped
# launchers with their groups, the other processes on their terminals but the tab's shell, and the orphaned Claudes of
# the space's sessions) by end_pids' steps; then the panel files the keeper's open request. It never ends a launcher
# that runs (not stopped) while its heartbeat is stale: that one is reported. With --fresh the next start (within 10
# min) is a new conversation (restart-next.json, read by run_session).

RECOVER_GAP = 10  # seconds between the two heartbeat reads
RECOVER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
SESSION_FLAGS = ("--session-id", "--resume")
RESTART_NEXT = "restart-next.json"
RESTART_NEXT_LIFE = 600  # seconds after `at` that a launcher's first start still reads it
USER_INITS = ("launchd",)  # an orphan's parent besides pid 1 (also `systemd --user`)


def recover_wait():
    time.sleep(RECOVER_GAP)


def recover_heartbeat(directory):
    """The heartbeat's epoch, read the way read_heartbeat reads it, but only from a regular file in the state folder
    (never a symlink followed, never a FIFO opened): None when missing or not `<epoch> <address>` (H17)."""
    import stat
    path = os.path.join(directory, "heartbeat")
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as f:
            parts = f.read(4096).decode("utf-8", "replace").split()
        return int(parts[0]) if len(parts) >= 2 else None
    except (OSError, ValueError):
        return None


def launcher_form(argv, space):
    """argv is a launcher of `space` (the spec's launcher form): python running <...>/tools/pa.py session, or
    <...>/pa-session (run by python or itself), with one --space value element exactly `space`."""
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        return False
    if not isinstance(space, str) or not RECOVER_ID.match(space):
        return False
    base = os.path.basename(argv[0])
    if base == "pa-session":
        rest = argv[1:]
    elif re.match(r"(?i)python[0-9.]*$", base) and len(argv) >= 2 and os.path.basename(argv[1]) == "pa-session":
        rest = argv[2:]
    elif (re.match(r"(?i)python[0-9.]*$", base) and len(argv) >= 3 and os.path.basename(argv[1]) == "pa.py"
          and argv[2] == "session"):
        rest = argv[3:]
    else:
        return False
    values, i = [], 0
    while i < len(rest):
        if rest[i] == "--space":
            if i + 1 >= len(rest):
                return False
            values.append(rest[i + 1])
            i += 2
            continue
        if rest[i].startswith("--space="):
            values.append(rest[i][len("--space="):])
        i += 1
    return values == [space]


def session_flag(argv, ids):
    """argv has --session-id or --resume followed by (or `=`) an element exactly one of ids (H13)."""
    if not isinstance(argv, list):
        return False
    ids = {i for i in ids if isinstance(i, str) and i}
    for i, a in enumerate(argv):
        if not isinstance(a, str):
            continue
        if a in SESSION_FLAGS and i + 1 < len(argv) and argv[i + 1] in ids:
            return True
        for flag in SESSION_FLAGS:
            if a.startswith(flag + "=") and a[len(flag) + 1:] in ids:
                return True
    return False


def user_init(r, by_pid):
    """The process was orphaned: its parent is pid 1, or the user's init (systemd --user, launchd)."""
    if r.get("ppid") == 1:
        return True
    parent = by_pid.get(r.get("ppid")) or {}
    words = parent.get("argv") or []
    name = os.path.basename(words[0]) if words else ""
    return name in USER_INITS or (name == "systemd" and "--user" in words)


def recover_targets(rows, space, ids, me):
    """From one ps read (with argv): {"ended": rows with `what`, "running": launchers of `space` that are not stopped}.
    ended: (a) the stopped launchers of `space` and their groups, (b) the other processes on their terminals but the
    session leaders there and the launchers' ancestors, (c) orphans of this uid naming one of `ids`; with their
    descendants. Never `me`, its ancestors or descendants, its group, pid <= 1, another uid or a zombie."""
    uid = os.getuid()
    rows = [r for r in rows if isinstance(r.get("pid"), int) and r["pid"] > 1 and not str(r.get("stat", "")).startswith("Z")]
    by_pid = {r["pid"]: r for r in rows}
    spared = {me} | ancestors_of(me, by_pid) | descendants_of({me}, rows)
    own_group = os.getpgrp() if me == os.getpid() else None
    launchers = [r for r in rows if r.get("uid") == uid and launcher_form(r.get("argv"), space)]
    stopped = [r for r in launchers if str(r.get("stat", "")).startswith("T")]
    running = [r for r in launchers if not str(r.get("stat", "")).startswith("T")]
    what = {}
    for r in stopped:
        what[r["pid"]] = "launcher"
    for r in stopped:
        spared |= ancestors_of(r["pid"], by_pid)
    groups = {r["pgid"] for r in stopped if isinstance(r.get("pgid"), int) and r["pgid"] > 1}
    for r in rows:
        if r.get("pgid") in groups:
            what.setdefault(r["pid"], "launcher group")
    ttys = {r.get("tty") for r in stopped} - set(NO_TTY) - {None}
    leaders = {r["pid"] for r in rows if r.get("tty") in ttys and "s" in str(r.get("stat", ""))}
    for p in set(leaders):
        spared |= {p} | ancestors_of(p, by_pid)
    for r in rows:
        if r.get("tty") in ttys:
            what.setdefault(r["pid"], "terminal")
    for r in rows:
        if r.get("uid") == uid and user_init(r, by_pid) and session_flag(r.get("argv"), ids):
            what.setdefault(r["pid"], "orphan")
    def ends(p):
        r = by_pid.get(p)
        return bool(r) and p not in spared and r.get("uid") == uid and not (own_group is not None
                                                                           and r.get("pgid") == own_group)
    # Only what is ended brings what it started: a spared or never-signalled one (a leader, an ancestor, own group,
    # another uid) does not (t-193 REVIEW M1).
    for p in descendants_of({p for p in what if ends(p)}, rows):
        what.setdefault(p, "started by one")
    ended = [dict(by_pid[p], what=what[p]) for p in sorted(what) if ends(p)]
    return {"ended": ended, "running": running}


def take_restart_next(directory):
    """R5.2: True when restart-next.json asks for a fresh first start (`fresh` true, `at` within RESTART_NEXT_LIFE s
    before now); the file is removed either way (also a symlink, which is never followed)."""
    import stat
    if not directory:
        return False
    path = os.path.join(directory, RESTART_NEXT)
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        return False
    value = None
    if stat.S_ISREG(mode):
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd) as f:
                value = json.loads(f.read(65536))
        except (OSError, ValueError):
            value = None
    try:
        os.unlink(path)
    except OSError:
        pass
    if not isinstance(value, dict) or value.get("fresh") is not True or not isinstance(value.get("at"), str):
        return False
    at = parse_time(value["at"])
    return at is not None and -60 <= time.time() - at <= RESTART_NEXT_LIFE


def cmd_recover(args, by):
    """pa-restart --recover (R5.1, R5.2). Prints the answer (JSON with --json); exits 2 when refused."""
    def answer(outcome, why, ended=(), left=(), code=0):
        result = {"outcome": outcome, "ended": [{"pid": r["pid"], "what": r.get("what")} for r in ended],
                  "left": [{"pid": r["pid"], "what": r.get("what")} for r in left], "why": why}
        print("Recover {}: {}{}".format(outcome, why, "".join(
            "\n  {} pid {} ({})".format("would end" if args.dry_run else "ended", r["pid"], r.get("what")) for r in ended)))
        if args.json:
            print(json.dumps(result))
        if code:
            sys.exit(code)
        return result
    space = args.space
    if not isinstance(space, str) or not RECOVER_ID.match(space):
        raise Problem("--recover needs --space <id> (letters, digits, - and _; not starting with -): {!r}".format(space))
    directory, agent = space_for_command(space)
    if agent.get("space") not in (None, space):
        raise Problem("agent.json in {} is for space {!r}, not {!r}".format(directory, agent.get("space"), space))
    first = recover_heartbeat(directory)
    if first is not None and time.time() - first <= FRESH:
        return answer("refused", "its launcher is running: the heartbeat is {:.0f} s old".format(time.time() - first),
                      code=2)
    recover_wait()
    second = recover_heartbeat(directory)
    if second is not None and time.time() - second <= FRESH:
        return answer("nothing", "its launcher came back: the heartbeat is {:.0f} s old".format(time.time() - second))
    rows = ps_rows(argv=True)
    if rows is None:
        return answer("refused", "ps could not be read on this machine", code=2)
    ids = {s for s in [agent.get("sessionId")] + list(agent.get("previousSessions") or []) if isinstance(s, str) and s}
    found = recover_targets(rows, space, ids, os.getpid())
    if found["running"]:
        return answer("refused", "its launcher runs but does not beat: pid {}; look at its tab".format(
            ", ".join(str(r["pid"]) for r in found["running"])), code=2)
    targets = found["ended"]
    if args.dry_run:
        return answer("recovered" if targets else "nothing", "dry run: nothing ended or written", ended=targets)
    ended, left = end_pids(targets, directory, "recover", extra=lambda r: "what=" + str(r.get("what")).replace(" ", "-")) if targets else ([], [])
    why = (args.why or "").strip() or "its launcher was not running"
    with open(os.path.join(directory, "journal.jsonl"), "a") as journal:
        journal.write(json.dumps({"at": now(), "by": by, "trigger": "recover", "why": "recover: " + why,
                                  "ended": len(ended), "left": len(left)}) + "\n")
    session_log(directory, "recover by={} ended={} left={}".format(by, len(ended), len(left)))
    if args.fresh:
        write_json(os.path.join(directory, RESTART_NEXT), {"fresh": True, "at": now(), "by": by})
    outcome = "recovered" if ended else "nothing"
    return answer(outcome, "{} ended, {} left; open it again".format(len(ended), len(left)) if targets
                  else "nothing of its launcher is left; open it again", ended=ended, left=left)


# ---- pa-restart --end-terminal: the one-time bridge for a launcher from before restart support, t-037 R10 ----------
# Only for a launcher with no restart-capable launcher.json of its own (spec S4). It ends that terminal through hc
# (its Claude and every worker tied to it end with it), after every check, the person's typed yes and the checks
# again under the launch lock; then the keeper restores it, or pa-start opens it, or the person uses the panel's Open.
# The person's command only: refused inside Claude, and the guard refuses pa-restart in workers.

KEEPER_WAIT = 45  # seconds to wait for the keeper's restore before pa-start (the Mac restores an ended PA in seconds)
END_WAIT = 15  # seconds for hc kill's terminal to finish
END_POLL = 0.5
END_TOKEN = "end-terminal-token.json"  # the --on relay's one-time binding of the confirmed answer (J3 round 2)
END_TOKEN_TTL = 600  # seconds the person has to answer between the relay's dry run and its bound run
AGENT_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_SANDBOX")  # what an agent's shell carries (Claude, Codex)


def in_agent():
    """True when this runs in an agent's shell: any marker in AGENT_ENV is set (the person's commands refuse it)."""
    return any(os.environ.get(k) for k in AGENT_ENV)
PYTHON_NAME = re.compile(r"python[0-9.]*$")


def proc_info(pid):
    """(parent pid, command line) of a process on this machine, from ps (the same flags on macOS and Linux); None
    when pid is not a positive number, the process is gone, or ps fails (t-077 R6: not proven, never accepted)."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        r = subprocess.run(["ps", "-ww", "-o", "ppid=", "-o", "command=", "-p", str(pid)], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, universal_newlines=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = (r.stdout or "").strip().splitlines() if r.returncode == 0 else []
    parts = lines[0].split(None, 1) if lines else []
    if len(parts) != 2 or not parts[0].isdigit():
        return None
    return int(parts[0]), parts[1].strip()


# Herdr's login-shell wrapper (t-077 R3). hc reports a tab's argv as `sh -c <script> sh <command...>`; the script is
# matched by its parts (spec O2: a reworded script is "not the wrapper", refused, safe)
WRAPPER_SCRIPT_PARTS = ("shell=${SHELL:-/bin/sh}", 'case "${shell##*/}" in',
                        "exec \"$shell\" -lic 'command $argv[2..]; exec $argv[1] -li' \"$shell\" \"$@\"",
                        "exec \"$shell\" -lic '\"$@\"; exec \"$0\" -li' \"$shell\" \"$@\"",
                        "exec sh -c '\"$@\"; exec \"${SHELL:-sh}\" -i' sh \"$@\"", "esac")
# ... and ps shows the same process after the script's exec as `<shell> <words> <shell> <command...>`, one per branch:
# (the shell's names, the words between the shell and its own $0, which is the shell again)
WRAPPER_PS_FORMS = ((("sh", "bash", "zsh", "dash", "ksh"), ["-lic", '"$@";', "exec", '"$0"', "-li"]),
                    (("fish",), ["-lic", "command", "$argv[2..];", "exec", "$argv[1]", "-li"]),
                    (("sh",), ["-c", '"$@";', "exec", '"${SHELL:-sh}"', "-i"]))


def wrapped_command(value):
    """The command words Herdr's login-shell wrapper runs ("$@" after its own $0), from hc's argv (a list) or a ps
    command line (a string); None for anything else, a shell where something was typed included (R4)."""
    if isinstance(value, list):
        if not all(isinstance(a, str) for a in value):
            return None
        if (len(value) >= 5 and os.path.basename(value[0]) == "sh" and value[1] == "-c" and value[3] == "sh"
                and all(" ".join(part.split()) in " ".join(value[2].split()) for part in WRAPPER_SCRIPT_PARTS)):
            return value[4:]
        value = " ".join(value)
    if not isinstance(value, str):
        return None
    words = value.split()  # ps gives a string: split on whitespace only; a path with spaces fails to match (safe)
    for names, middle in WRAPPER_PS_FORMS:
        k = 1 + len(middle)
        if (len(words) > k + 1 and os.path.basename(words[0]) in names and words[1:k] == middle
                and words[k] == words[0]):
            return words[k + 1:]
    return None


def wrapper_of(term):
    """The command Herdr's login-shell wrapper runs in this terminal now; None when the terminal's program is not the
    wrapper. ps of hc's pid must show a live wrapper form (the wrapper's login shell after its exec): hc's argv is the
    tab's start argv and stays the wrapper after the wrapped program ends and that shell execs "$0" -li in the same
    pid, the person's shell (t-077 round 2, R4). When hc's argv is the wrapper too, both must run the same words."""
    info = proc_info(term.get("pid"))
    wrapped = wrapped_command(info[1]) if info else None
    if wrapped is None:
        return None
    started = wrapped_command(term.get("argv"))
    if started is not None and " ".join(started).split() != wrapped:
        return None
    return wrapped


def launcher_words(words):
    """A launcher's command words: pa-session, or a python running pa-session (the symlink, as ps shows it) or
    pa.py session."""
    if not words or not all(isinstance(w, str) for w in words):
        return False
    base = os.path.basename(words[0])
    if base == "pa-session":
        return True
    if not PYTHON_NAME.match(base) or len(words) < 2:
        return False
    script = os.path.basename(words[1])
    return script == "pa-session" or (script == "pa.py" and len(words) >= 3 and words[2] == "session")


def child_link(term_pid, pid):
    """None when pid is a live launcher whose parent is term_pid (the wrapper's login shell forks the launcher, so
    hc's pid and launcher.json's differ: R1 (a), (b)); else what was seen, in one line."""
    info = proc_info(pid)
    if not info:
        return "pid {} is not running (or cannot be read)".format(pid)
    ppid, command = info
    if ppid != term_pid:
        return "pid {}'s parent is {}, not {}".format(pid, ppid, term_pid)
    if not launcher_words(command.split()):
        return "pid {} runs {!r}, not a launcher".format(pid, short_words(command.split()))
    return None


def short_words(words, n=4):
    """The first n words, each cut to 60 characters, in one line."""
    return " ".join(w if len(w) <= 60 else w[:57] + "..." for w in words[:n])


def seen_text(argv, wrapped=None):
    """What a terminal runs, in one line (R5): the wrapper and the first words it runs, or argv's first words with a
    script (a newline or a long word) shown as <script>; never the script's text."""
    if wrapped is not None:
        return "Herdr's login-shell wrapper running {!r}".format(short_words(wrapped))
    if isinstance(argv, str):
        argv = argv.split()
    if not isinstance(argv, list):
        return "'unknown'"
    words = [a if "\n" not in a and len(a) <= 60 else "<script>" for a in argv[:6] if isinstance(a, str)]
    return repr(" ".join(" ".join(words).split())) if words else "'unknown'"


def pa_session_argv(argv, space):
    """R10 (e): hc's start argv of the terminal is a launcher for this space: `pa-session ... --space <id>`, or a
    python running pa.py session with that flag; exactly one --space value, the space's local id."""
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        return False
    base = os.path.basename(argv[0])
    if base == "pa-session":
        rest = argv[1:]
    elif PYTHON_NAME.match(base) and len(argv) >= 2 and os.path.basename(argv[1]) == "pa-session":
        rest = argv[2:]  # the symlinked launcher as ps shows it (t-077)
    elif PYTHON_NAME.match(base) and len(argv) >= 3 and os.path.basename(argv[1]) == "pa.py" and argv[2] == "session":
        rest = argv[3:]
    else:
        return False
    values, i = [], 0
    while i < len(rest):
        if rest[i] == "--space":
            if i + 1 >= len(rest):
                return False
            values.append(rest[i + 1])
            i += 2
            continue
        if rest[i].startswith("--space="):
            values.append(rest[i][len("--space="):])
        i += 1
    return bool(space) and len(values) == 1 and local_id(values[0]) == space


def identify_old_launcher(directory, agent):
    """(terminal, hc record, note) of the space's running launcher when R10's checks hold and it cannot take an
    in-place restart, else Problem naming what failed. Nothing is written or ended here."""
    terminal, term = check_terminal(directory, agent)
    space = agent.get("space")
    argv = term.get("argv")
    wrapped = None
    if not pa_session_argv(argv, space):
        wrapped = wrapper_of(term)  # t-077 R2: Herdr's login-shell wrapper running this space's pa-session
        if not pa_session_argv(wrapped, space):
            shown = seen_text(argv, wrapped)
            ran = wrapped if wrapped is not None else argv
            if (isinstance(ran, list) and ran and isinstance(ran[0], str) and os.path.basename(ran[0]) == "pa-session"
                    and "--ask" in ran):  # card d-161 'B: keep strict': refused, said plainly (wrapped or not)
                raise Problem("cannot end it: its terminal is a space's first tab, started with {} (no --space), so "
                              "it cannot be proven to be this space's launcher; close it by hand if it is the Project "
                              "Agent (the keeper or the panel's Open then starts a restart-capable one)".format(shown))
            raise Problem("cannot end it: its terminal's program is {}, not this space's pa-session; close it by hand "
                          "if it is the Project Agent".format(shown))
    record = read_json_safe(os.path.join(directory, "launcher.json"))
    rec = as_dict(record)
    note = None
    # the record's launcher is this terminal's: the same pid, or the wrapper's live launcher child (t-077 R2)
    same = rec.get("pid") == term.get("pid") or (
        wrapped is not None and child_link(term.get("pid"), rec.get("pid")) is None)
    if restart_capable(record):
        if rec.get("terminal") == terminal and same:
            text = ("This launcher takes an in-place restart: run pa-restart --space {} (its workers keep "
                    "running).".format(space))
            last = as_dict(rec.get("restart"))
            if last.get("outcome") == "failed":
                text += (" Its last restart failed: {}. Ending the terminal would start the same installed code; fix "
                         "the install (pa-doctor), then pa-restart.".format(as_text(last.get("why")) or "no reason recorded"))
            raise Problem(text)
        note = "launcher.json is from terminal {} pid {}, not this launcher".format(rec.get("terminal"), rec.get("pid"))
    elif rec.get("terminal") == terminal and "pid" in rec and not same:
        raise Problem("cannot end it: launcher.json disagrees with the terminal (pid {} in launcher.json, {} in hc): "
                      "not certain which launcher runs there".format(rec.get("pid"), term.get("pid")))
    return terminal, term, note


def called_from(terminal):
    """This command runs in `terminal` or in one of its children (it would end with it)."""
    address, seen = os.environ.get("HC_TERMINAL_ID"), set()
    while address and address not in seen and len(seen) < 6:
        if local_id(address) == local_id(terminal):
            return True
        seen.add(address)
        address = as_dict(hc_get(address)).get("parent")
    return False


def interactive():
    """Someone can type the answer: stdin is a terminal."""
    return sys.stdin.isatty()


def ask_yes(prompt):
    """The typed answer, or None on end of input or Ctrl-C."""
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        return None


def herdr_app():
    return shutil.which("herdr-app")


def relay_output(argv):
    """Runs argv (pa-restart --end-terminal --on): (exit code, its stdout); its stderr goes to this terminal."""
    r = subprocess.run(argv, stdout=subprocess.PIPE, universal_newlines=True)
    return r.returncode, r.stdout


def end_result(args, outcome, code, terminal=None, workers=(), restored=None, why=None, err=None, token=None):
    if err:
        print(err, file=sys.stderr)
    if getattr(args, "json", False):
        data = {"outcome": outcome, "terminal": terminal, "workers": list(workers), "restoredBy": restored, "why": why}
        if token:
            data["token"] = token
        print(json.dumps(data))
    if code:
        sys.exit(code)


def end_summary(agent, terminal, workers, note, installed, turn):
    """What ending the terminal ends and what stays (said before the question, and by --dry-run)."""
    sid = agent.get("sessionId")
    turn_text = ("mid-turn ({}): its current turn is cut off".format(turn) if turn in ("working", "blocked")
                 else "idle" if turn == "idle" else "its turn is not known")
    lines = ["Project Agent {} (space {}) in terminal {}: its launcher started before restart support{}.".format(
        agent.get("name"), agent.get("space"), terminal, " ({})".format(note) if note else "")]
    lines.append("Ends: terminal {} (the Project Agent's tab program), its Claude {} (session {}; {}), and its {} "
                 "worker(s){}".format(terminal, agent.get("name"), sid, turn_text, len(workers), ":" if workers else "."))
    lines += ["  {} {}: ends; its work in progress is lost".format(a, n) for a, n in workers]
    lines.append("Keeps: the state folder (ledger, journal, decisions, evidence), the session id {} (the new launcher "
                 "resumes it), the worktrees and every commit in them.".format(sid))
    lines.append("Then: a new launcher running the installed code (tools {}, core {}), restored by the keeper or "
                 "opened by pa-start; it restarts in place from then on.".format(
                     (as_text(installed.get("tools")) or "none")[:12], (as_text(installed.get("core")) or "none")[:12]))
    return lines


def relay_form(args):
    """(issue, bound) for the --on relay's own flags (round 2, RL-J3-2). Both are the remote side's only: refused on
    a run that has a terminal (a tty, or HC_TERMINAL_ID: hc exec runs without one) and with --on."""
    issue = bool(getattr(args, "relay_issue", False))
    parts = [getattr(args, k, None) for k in ("relay_token", "confirmed_terminal", "confirmed_workers")]
    bound = any(p is not None for p in parts)
    if not (issue or bound):
        return False, False
    if getattr(args, "on", None) or os.environ.get("HC_TERMINAL_ID") or interactive():
        raise Problem("--relay-issue, --relay-token and --confirmed-* are pa-restart --on's relay only (hc exec on the "
                      "space's machine, no terminal); run pa-restart --end-terminal yourself and type yes")
    if issue and (bound or not args.dry_run):
        raise Problem("--relay-issue goes with --dry-run only (the relay's checks)")
    if bound and not all(parts):
        raise Problem("the relay's bound form needs --relay-token, --confirmed-terminal and --confirmed-workers")
    return issue, bound


def take_end_token(directory):
    """The relay's token record, removed from the folder whatever it holds (one try per token); {} if none."""
    path = os.path.join(directory, END_TOKEN)
    taken = "{}.taken.{}".format(path, os.getpid())
    try:
        os.rename(path, taken)
    except OSError:
        return {}
    try:
        return as_dict(read_json_safe(taken))
    finally:
        try:
            os.unlink(taken)
        except OSError:
            pass


def end_token_matches(record, given, terminal, workers):
    import hmac
    secret, expires = record.get("token"), record.get("expires")
    return (isinstance(secret, str) and len(secret) >= 32 and isinstance(given, str)
            and hmac.compare_digest(secret.encode(), given.encode())
            and isinstance(expires, (int, float)) and time.time() < expires
            and record.get("terminal") == terminal and record.get("workers") == list(workers))


def end_terminal(args):
    """pa-restart --end-terminal (R10): the steps of plan.md J3.2. The one ending call is `hc kill <terminal>`."""
    if getattr(args, "now", False) or getattr(args, "fresh", False):
        raise Problem("--end-terminal takes neither --now nor --fresh: the terminal ends at once and the new launcher "
                      "resumes the same session")
    issue, bound = relay_form(args)
    if getattr(args, "on", None):
        return end_terminal_on(args)
    directory, agent = space_for_command(args.space)
    token = take_end_token(directory) if bound else None  # used up by any try, before anything else is read
    terminal, term, note = identify_old_launcher(directory, agent)
    if called_from(terminal):
        raise Problem("run it from another tab: this one is terminal {} or one of its children and would end with "
                      "it".format(terminal))
    space = agent.get("space")
    workers = live_workers(directory, agent)
    addresses = sorted(a for a, _n in workers)
    for line in end_summary(agent, terminal, workers, note, as_dict(installed_stamp()), agent_turn(terminal)):
        print(line)
    if args.dry_run:
        print("Dry run: nothing ended.")
        if issue:  # the relay's own dry run: the one-time token its bound run must bring back
            secret = uuid.uuid4().hex + uuid.uuid4().hex
            write_json(os.path.join(directory, END_TOKEN), {"token": secret, "terminal": terminal, "workers": addresses,
                                                            "at": now(), "expires": time.time() + END_TOKEN_TTL})
            return end_result(args, "dry-run", 0, terminal, addresses, token=secret)
        return end_result(args, "dry-run", 0, terminal, addresses)
    if workers and not getattr(args, "anyway", False):
        return end_result(args, "refused", 1, terminal, addresses, why="workers running",
                          err="refused: {} worker(s) running (listed above). Run it again when they are done, or add "
                              "--anyway to end them too.".format(len(workers)))
    confirmed = getattr(args, "confirmed_terminal", None)
    if bound:  # the --on relay: the person answered yes on the other machine for exactly these
        want = sorted(w for w in (getattr(args, "confirmed_workers", None) or "").split(",") if w and w != "-")
        if not end_token_matches(token, args.relay_token, terminal, addresses):
            return end_result(args, "refused", 1, terminal, addresses, why="no relay token",
                              err="refused: the relay's token is missing, used, expired or not this one; nothing "
                                  "ended. Run pa-restart --end-terminal --on <machine> again")
        if confirmed != terminal or want != addresses:
            return end_result(args, "refused", 1, terminal, addresses, why="changed",
                              err="refused: it changed since you answered (you confirmed terminal {} and workers {}; "
                                  "now terminal {} and workers {}); nothing ended".format(
                                      confirmed, ", ".join(want) or "none", terminal, ", ".join(addresses) or "none"))
    else:
        if not interactive():
            return end_result(args, "refused", 1, terminal, addresses, why="no terminal to answer in",
                              err="refused: --end-terminal asks you to type yes; run it in a terminal")
        if workers:
            print("It also ends these {} worker(s):".format(len(workers)))
            for a, n in workers:
                print("  {} {}".format(a, n))
        sys.stdout.flush()
        answer = ask_yes("Type yes to end terminal {} and {} worker(s): ".format(terminal, len(workers)))
        if (answer or "").strip().lower() != "yes":
            print("Not ended.")
            return end_result(args, "not-ended", 1, terminal, addresses, why="not confirmed")
    lock = launch_lock(space, wait=10)
    try:
        changed = None
        try:  # the checks again, right before the end: nothing may have moved since the answer
            again = as_dict(read_json_safe(os.path.join(directory, "agent.json")))
            terminal2, term2, _note = identify_old_launcher(directory, again)
            workers2 = sorted(a for a, _n in live_workers(directory, again))
        except Problem as e:
            changed = str(e)
        else:
            if terminal2 != terminal or term2.get("pid") != term.get("pid"):
                changed = "the launcher is now terminal {} pid {}".format(terminal2, term2.get("pid"))
            elif workers2 != addresses:
                changed = "workers now: {}".format(", ".join(workers2) or "none")
        if changed:
            return end_result(args, "refused", 1, terminal, addresses, why="changed",
                              err="refused: it changed since you answered ({}); nothing ended".format(changed))
        why = (getattr(args, "why", None) or "").strip() or "replace a launcher from before restart support"
        with open(os.path.join(directory, "session.log"), "a") as log:
            log.write("{} end-terminal terminal={} workers={} by=pa-restart\n".format(now(), terminal, len(addresses)))
        with open(os.path.join(directory, "journal.jsonl"), "a") as journal:
            journal.write(json.dumps({"at": now(), "by": "person", "trigger": "pa-restart --end-terminal", "why": why,
                                      "terminal": terminal, "workers": addresses}) + "\n")
        error = None
        try:
            call(["hc", "kill", terminal])  # hc ends the terminal it names; no pid is used (R9)
        except Problem as e:
            error = str(e)
    finally:
        lock.close()
    deadline = time.time() + END_WAIT
    while as_dict(hc_get(terminal)).get("state") == "live" and time.time() < deadline:
        time.sleep(END_POLL)
    state = as_text(as_dict(hc_get(terminal)).get("state"))
    # a failed hc kill counts as ended only when hc says the terminal finished; no answer is not an end (round 2, M1)
    if (state != "finished") if error else (state == "live"):
        seen = "it is still live" if state == "live" else "hc says its state is {}".format(state or "unknown")
        with open(os.path.join(directory, "session.log"), "a") as log:
            log.write("{} end-terminal not-ended terminal={}\n".format(now(), terminal))
        return end_result(args, "not-ended", 1, terminal, addresses, why=error or "still live",
                          err="hc kill did not end terminal {} ({}{}); nothing new was started. Its tab may still run: "
                              "run pa-restart --end-terminal again, or close it by hand.".format(
                                  terminal, error + "; " if error else "", seen))
    print("Terminal {} ended{}.".format(terminal, " (hc kill said: {})".format(error) if error else ""), flush=True)
    deadline = time.time() + KEEPER_WAIT
    while True:
        current = as_text(as_dict(read_json_safe(os.path.join(directory, "agent.json"))).get("terminal"))
        if current and current != terminal and launcher_alive(directory, current):
            print("Restored by the keeper in terminal {}.".format(current))
            return end_result(args, "ended", 0, terminal, addresses, restored="keeper")
        if time.time() >= deadline:
            break
        time.sleep(max(0.01, min(1.0, KEEPER_WAIT / 10.0)))
    if not herdr_app():
        print("Terminal {} is ended. Open the Project Agent again with the panel's Open Project Agent (this machine "
              "has no herdr-app).".format(terminal))
        return end_result(args, "ended", 0, terminal, addresses)
    print("No restore within {:.0f} s: pa-start --space {}.".format(KEEPER_WAIT, space), flush=True)
    try:
        cmd_start(argparse.Namespace(space=space, folder=agent.get("project"), profile=agent.get("profile"),
                                     dry_run=False, app_space=None))
    except Problem as e:
        return end_result(args, "ended", 1, terminal, addresses, why=str(e),
                          err="pa-start failed: {}. The state folder is intact: the keeper or the panel's Open Project "
                              "Agent restores it (the same session).".format(e))
    return end_result(args, "ended", 0, terminal, addresses, restored="pa-start")


def end_terminal_on(args):
    """--end-terminal --on M (plan.md J3.5): M runs every check (a dry run); the person answers here; then M runs it
    bound to the terminal and the workers that were confirmed, and refuses if either changed."""
    if not args.space:
        raise Problem("--on needs --space <id> (the space's id on that machine)")
    base = ["--space", args.space, "--end-terminal"] + (["--anyway"] if getattr(args, "anyway", False) else [])
    if args.why:
        base += ["--why", args.why]

    def remote(flags, timeout):
        script = '"$HOME/.config/herdr-pa/tools/pa.py" restart ' + " ".join(shlex.quote(a) for a in flags)
        return ["hc", "exec", "--on", args.on, "--timeout", str(timeout), "--", "sh", "-c", script]

    code, out = relay_output(remote(base + ["--dry-run", "--json", "--relay-issue"], 60))
    if code == 127:
        print("pa-restart is not installed on {}: run pa-sync --on {} first".format(args.on, args.on), file=sys.stderr)
        sys.exit(127)
    lines = (out or "").splitlines()
    data = None
    if lines:
        try:
            data = json.loads(lines[-1])
        except ValueError:
            data = None
    for line in (lines[:-1] if isinstance(data, dict) else lines):
        print(line)
    if code or not isinstance(data, dict) or data.get("outcome") != "dry-run" or not as_text(data.get("terminal")):
        print("The checks on {} did not pass (exit {}); nothing ended.".format(args.on, code), file=sys.stderr)
        sys.exit(code or 1)
    terminal = as_text(data.get("terminal"))
    workers = sorted(w for w in data.get("workers") or [] if isinstance(w, str))
    token = as_text(data.get("token"))
    if not token:
        print("pa-restart on {} gave no relay token (an older pa-restart there? run pa-sync --on {}); nothing "
              "ended.".format(args.on, args.on), file=sys.stderr)
        sys.exit(1)
    if workers and not getattr(args, "anyway", False):
        print("refused: {} worker(s) running on {} (listed above). Run it again when they are done, or add --anyway to "
              "end them too.".format(len(workers), args.on), file=sys.stderr)
        sys.exit(1)
    if not interactive():
        print("refused: --end-terminal asks you to type yes; run it in a terminal", file=sys.stderr)
        sys.exit(1)
    answer = ask_yes("Type yes to end terminal {} on {} and {} worker(s): ".format(terminal, args.on, len(workers)))
    if (answer or "").strip().lower() != "yes":
        print("Not ended.")
        sys.exit(1)
    flags = base + ["--confirmed-terminal", terminal, "--confirmed-workers", ",".join(workers) or "-",
                    "--relay-token", token]
    code = relay(remote(flags + (["--json"] if args.json else []), 120))
    if code:
        sys.exit(code)


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


def cost_fact_notes(directory, agent):
    """Notes (never problems) on the facts the launcher records for costs (t-032): the profile's pricing rules and the
    machine's costs rules that were refused, and recent launch records with a refused value (names only)."""
    costs = costs_module()
    if not costs:
        return []
    notes = []
    try:
        _rules, problems = costs.pricing_rules(load_profile(agent.get("profile")))
        _ar, _region, machine = costs.machine_rules()
        for why in (problems + machine)[:10]:
            notes.append("costs rule refused: {}".format(why))
        tail = []
        path = os.path.join(directory, "costs", "sessions.jsonl")
        if os.path.isfile(path):
            with open(path, "rb") as f:
                f.seek(max(0, os.path.getsize(path) - 256 * 1024))
                tail = f.read().decode("utf-8", "replace").splitlines()[-200:]
        names, count = set(), 0
        for text in tail:
            try:
                rec = json.loads(text)
            except ValueError:
                continue
            if isinstance(rec, dict) and (rec.get("regionBy") == "refused" or rec.get("refused")):
                count += 1
                names.update(n for n in rec.get("refused") or [] if isinstance(n, str) and len(n) <= 64)
        if count:
            notes.append("{} launch record(s) with a refused region or account value (variables: {}): not stored".format(
                count, ", ".join(sorted(names)) or "?"))
    except Exception:  # noqa: BLE001 - a note only
        pass
    return notes


def cache_notes(directory, agent):
    """pa-doctor's notes on the Project Agent's prompt cache (t-040): what the profile gives, the last start line's
    cache= and cacheBy= (what the launcher asked for, with its own environment), and a malformed profile key."""
    try:
        p = load_profile(agent.get("profile"))
    except Problem:
        return []
    ttl, why = pa_cache_ttl(p)
    if why:
        return ["profile {} is malformed ({}): the Project Agent gets Claude Code's default prompt cache".format(PA_CACHE_KEY, why)]
    last = None
    for line in read_text(os.path.join(directory, "session.log")).splitlines():
        words = line.split()
        if len(words) >= 2 and words[1] == "start":
            last = [w for w in words[2:] if w.startswith(("cache=", "cacheBy="))]
    shown = " ".join(last) if last else "not recorded (started before t-040)" if last is not None else "none yet"
    return ["prompt cache (Project Agent): the profile gives {}; last start: {}".format(
        ttl or "Claude Code's default", shown)]


def statusline_cache_note():
    """pa-doctor's line on the status line's compiled-code cache (t-111, t-135): used, none yet, or not used and why
    (pa_statusline.refused: the launcher itself cannot say so, its output is the status line). A note only."""
    folder = os.path.join(ROOT, "cache", "pycache")
    try:
        import types
        with open(STATUSLINE_FILE) as f:
            code = compile(f.read(), STATUSLINE_FILE, "exec", dont_inherit=True)  # compiled here: no .pyc in tools/
        sl = types.ModuleType("pa_statusline")
        sl.__file__ = STATUSLINE_FILE
        exec(code, sl.__dict__)
        bad = sl.refused(folder, missing_ok=True)
    except Exception as e:  # noqa: BLE001 - a note only
        return "status line cache: not checked ({})".format(e)
    if bad:
        fix = " (fix: chmod 700 {})".format(bad[0]) if bad[1] == "is writable by group or others" else ""
        return "status line cache: NOT used: {} {}; the status line compiles its code on every run{}".format(bad[0], bad[1], fix)
    if not os.path.isdir(folder):
        return "status line cache: none yet"
    return "status line cache: used, {} file(s) in {}".format(len([n for n in os.listdir(folder) if n.endswith(".pyc")]), folder)


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
                value = json.load(f)
        except ValueError as e:
            problems.append("{}: not valid JSON ({})".format(name, e))
            return None
        if not isinstance(value, dict):  # t-190 H1: `[]` is JSON, but no file here is a list
            problems.append("{}: not an object".format(name))
            return None
        return value

    ledger = load("ledger.json")
    if ledger is not None:
        if not isinstance(ledger.get("tasks"), list):
            problems.append("ledger.json: no tasks list")
        else:
            for t in [t for t in ledger["tasks"] if not isinstance(t, dict)]:
                problems.append("ledger.json: task {!r} is not an object".format(t))
            ids = [t.get("id") for t in ledger["tasks"] if isinstance(t, dict)]
            for t in [t for t in ledger["tasks"] if isinstance(t, dict)]:
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
    archived = os.path.join(directory, ARCHIVE)
    if os.path.isdir(archived):
        count = 0
        for name in sorted(os.listdir(archived)):
            if not name.endswith(".json"):
                continue
            card = load(os.path.join(ARCHIVE, name))
            if card is None:
                continue
            count += 1
            if not isinstance(card.get("question"), str) or not isinstance(card.get("options"), list) or card.get("status") not in ("answered", "withdrawn"):
                problems.append("{}/{}: needs question, options and status answered|withdrawn".format(ARCHIVE, name))
        notes.append("{} archived decision(s) in {}/".format(count, ARCHIVE))
    costs = os.path.join(directory, "costs")
    if os.path.isdir(costs):  # the Costs view's records (pa-costs); a note only, never a problem
        summary = read_json(os.path.join(costs, "summary.json"), None) if os.path.isfile(os.path.join(costs, "summary.json")) else None
        updated = summary.get("updated") if isinstance(summary, dict) and isinstance(summary.get("updated"), str) else None
        notes.append("costs/: {:.1f} KB ({})".format(folder_size(costs) / 1024.0, "summary updated " + updated if updated else "no summary yet"))
    else:
        notes.append("costs/: none yet")
    notes.extend(cost_fact_notes(directory, agent))
    try:
        _cfg, why = cc_statusline(load_profile(agent.get("profile")))
    except Problem:
        why = None
    if why:  # a note only: the launches go on without the recording status line
        notes.append("profile statusLine is malformed ({}): Claude Code's own cost figure is not recorded".format(why))
    notes.extend(cache_notes(directory, agent))
    notes.append(statusline_cache_note())
    notes.extend(context_notes(directory, ledger))
    term = hc_get(agent.get("terminal"))
    running = term.get("state") == "live" and (term.get("labels") or {}).get("herdr.pa") == agent.get("space")
    notes.append("agent {} in terminal {}".format("running" if running else "NOT running", agent.get("terminal")))
    word, detail = space_code_state(agent.get("space"), agent.get("terminal"))  # t-037 R3
    if word == "unknown" and running and not os.path.exists(os.path.join(directory, "launcher.json")):
        detail += "; or launcher.json could not be written (see session.log)"
    notes.append("code: {} ({})".format(word, detail))
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


def job_slug(text):
    """A job's short name: lower case, every run of other characters one '-', at most 40 characters; 'job' if nothing is left."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")[:40].strip("-")
    return slug or "job"


def legacy_job(task_id, path):
    """The job of a worktree recorded with no job (extraWorktrees): its folder's name, without the task id in front.
    The folder is the container in <project>/worktrees/<container>/<repo>, else the worktree's own name."""
    path = os.path.normpath(os.path.expanduser(str(path)))
    parent = os.path.dirname(path)
    name = os.path.basename(parent) if os.path.basename(os.path.dirname(parent)) == "worktrees" else os.path.basename(path)
    prefix = "{}-".format(task_id)
    if task_id and name.startswith(prefix):
        name = name[len(prefix):]
    return job_slug(name)


def task_worktrees(task):
    """Every worktree a ledger task records, main first: [{path (realpath), given (as written), job, keep, main,
    jobRecorded, created}]. Sources, in order: `worktree` (the main one), `worktrees` entries, the old `extraWorktrees`
    strings (job from the folder). Merged by realpath, the first one wins; an entry with `removed` is skipped and hides
    the same path in later sources, unless it is the `worktree` field itself (that field wins). Reads only."""
    if not isinstance(task, dict):
        return []
    tid = str(task.get("id") or "")
    entries = [e for e in task.get("worktrees") or [] if isinstance(e, dict)] if isinstance(task.get("worktrees"), list) else []
    extras = task.get("extraWorktrees") if isinstance(task.get("extraWorktrees"), list) else []
    real = lambda p: os.path.realpath(os.path.expanduser(p))
    usable = lambda e: isinstance(e.get("path"), str) and e["path"].strip() != ""
    out, seen, jobs = [], set(), set()

    def add(path, given, job, recorded, keep, main, created):
        if job in jobs:
            n = 2
            while "{}-{}".format(job, n) in jobs:
                n += 1
            job = "{}-{}".format(job, n)
        jobs.add(job)
        seen.add(path)
        out.append({"path": path, "given": given, "job": job, "keep": bool(keep is True or task.get("keepWorktree")), "main": main,
                    "jobRecorded": recorded, "created": created if isinstance(created, str) else None})

    def entry_job(e):
        if isinstance(e.get("job"), str) and e["job"].strip():
            return job_slug(e["job"]), True
        return legacy_job(tid, e["path"]), False

    main = task.get("worktree") if isinstance(task.get("worktree"), str) and task.get("worktree").strip() else None
    if main:
        path = real(main)
        own = next((e for e in entries if usable(e) and real(e["path"]) == path), None)
        job, recorded = entry_job(own) if own else ("main", True)
        add(path, main, job, recorded, (own or {}).get("keep"), True, (own or {}).get("created"))
    seen.update(real(e["path"]) for e in entries if usable(e) and e.get("removed"))
    for e in entries:
        if not usable(e) or e.get("removed") or real(e["path"]) in seen:
            continue
        job, recorded = entry_job(e)
        add(real(e["path"]), e["path"], job, recorded, e.get("keep"), False, e.get("created"))
    for given in extras:
        if isinstance(given, str) and given.strip() and real(given) not in seen:
            add(real(given), given, legacy_job(tid, given), False, None, False, None)
    return out


def card_stamp(card):
    """When a closed card was answered or withdrawn, as a UTC datetime: the later of `answered` and `withdrawnAt` (t-131:
    a card answered long ago and withdrawn today is today's); None when neither can be read."""
    found = []
    for key in ("answered", "withdrawnAt"):
        m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)", str(card.get(key) or ""))
        if m:
            try:
                found.append(datetime.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc))
            except ValueError:
                pass
    return max(found) if found else None


def archive_cards(directory, args):
    """pa-task archive (t-054): the person's command. Moves answered and withdrawn cards of every kind, answered more
    than --days ago, from decisions/ to decisions-archive/. Stays: open cards, any other status, a card whose stamp
    cannot be read, a file that is not a card, the cards of a task in the ledger that is not done or parked, and a name
    already in the archive (never overwritten). Nothing is deleted. --dry-run lists and moves nothing (an agent may
    run it). Each moved card is read again in the archive: one a writer reopened meanwhile goes back."""
    if not args.dry_run and in_agent():
        raise Problem("pa-task archive is the person's command; an agent asks them instead (pa-task archive --dry-run shows what would move)")
    if args.task:
        raise Problem("pa-task archive takes no task id")
    if args.days is None or args.days < 0:
        raise Problem("--days must be 0 or more")
    states = {t.get("id"): t.get("state") for t in (read_json_safe(os.path.join(directory, "ledger.json")) or {}).get("tasks") or [] if isinstance(t, dict)}
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=args.days)
    folder, archive = os.path.join(directory, "decisions"), os.path.join(directory, ARCHIVE)
    moved, taken, back = [], [], []
    locks = [] if args.dry_run else [push_card_lock(directory)]  # the push lock first, as every card writer (t-131)
    if not args.dry_run:
        try:
            locks.append(archive_lock(directory))
        except BaseException:
            locks[0].close()
            raise
    try:
        for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
            if not name.endswith(".json") or not ID.match(name[:-5]):
                continue
            src, dst = os.path.join(folder, name), os.path.join(archive, name)
            card = read_json_safe(src)
            if not isinstance(card, dict) or card.get("status") not in ("answered", "withdrawn") or not isinstance(card.get("question"), str):
                continue
            stamp = card_stamp(card)
            if stamp is None or stamp > cutoff:
                continue
            task = card.get("task")
            if task in states and states[task] not in ("done", "parked"):
                continue
            if os.path.lexists(dst):
                taken.append(name[:-5])
                continue
            if args.dry_run:
                moved.append(name[:-5])
                continue
            move_card(src, dst)
            again = read_json_safe(dst)
            if not (isinstance(again, dict) and again.get("status") in ("answered", "withdrawn")):
                if not os.path.lexists(src):
                    move_card(dst, src)
                back.append(name[:-5])
                continue
            moved.append(name[:-5])
        if moved and not args.dry_run:
            with open(os.path.join(directory, "journal.jsonl"), "a") as journal:
                ids = ", ".join(moved)
                journal.write(json.dumps({"at": now(), "by": "person", "trigger": "archive",
                                          "why": "archived {} decision card(s) answered more than {:g} days ago to {}/: {}".format(
                                              len(moved), args.days, ARCHIVE, ids if len(ids) <= 2000 else ids[:1999] + "…")}) + "\n")
    finally:
        for lock in reversed(locks):
            lock.close()
    verb = "Would move" if args.dry_run else "Moved"
    print("{} {} card(s) to {}/{}".format(verb, len(moved), ARCHIVE, ": " + ", ".join(moved) if moved else "."))
    if taken:
        print("Left in decisions/, a card of the same name is already in {}: {}".format(ARCHIVE, ", ".join(taken)))
    if back:
        print("Left in decisions/, changed while it was moved: {}".format(", ".join(back)))


def unarchive_card(directory, args):
    """pa-task unarchive <card-id> (t-131): the person's command. Moves decisions-archive/<id>.json back to decisions/ as
    is, under .archive.lock; never over a card of that id in decisions/. Journaled."""
    if in_agent():
        raise Problem("pa-task unarchive is the person's command; an agent asks them instead")
    if not args.task:
        raise Problem("pa-task unarchive needs a card id")
    card_id = ident(args.task, "card id")
    lock = archive_lock(directory)
    try:
        live = os.path.join(directory, "decisions", card_id + ".json")
        old = os.path.join(directory, ARCHIVE, card_id + ".json")
        if os.path.lexists(live):
            raise Problem("{} is already in decisions/; nothing was moved".format(card_id))
        if not os.path.lexists(old):
            raise Problem("{} is not in {}/".format(card_id, ARCHIVE))
        move_card(old, live)
        with open(os.path.join(directory, "journal.jsonl"), "a") as journal:
            journal.write(json.dumps({"at": now(), "by": "person", "trigger": "archive",
                                      "why": "moved decision card {} back from {}/ to decisions/".format(card_id, ARCHIVE)}) + "\n")
    finally:
        lock.close()
    print("Moved {} back to decisions/.".format(card_id))


def open_cards(directory):
    """{task id: the first open card's id} from decisions/ (by file name); archived cards are never open."""
    out = {}
    folder = os.path.join(directory, "decisions")
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if not name.endswith(".json"):
            continue
        card = read_json_safe(os.path.join(folder, name))
        if isinstance(card, dict) and card.get("status") == "open" and isinstance(card.get("task"), str):
            out.setdefault(card["task"], str(card.get("id") or name[:-5]))
    return out


# t-097 R18, R19, R21: the state each new move takes a task from and to, and the notice the Project Agent gets.
TASK_MOVES = {"next": (("queued",), "in-progress", "only a queued task"),
              "pause": (("in-progress", "investigating"), "queued", "only an in-progress or investigating task")}
TASK_NOTICES = {
    "next": "The person asked to start task {id} next: it is in progress now. Its next job goes first in the waiting list "
            "only while it is at the top of the task order (the panel's Start next puts it there; pa-task next alone "
            "changes only its state). Its after tasks, cards and holds still apply; no worker was started.",  # t-120
    "pause": "The person paused task {id}: let its running workers finish their job; start no new job for it, and block "
             "its waiting jobs (\"paused by the person\"), until they start it again.",
    "unpause": "The person unpaused task {id}: unblock its waiting jobs paused by the person; its turn comes at its place "
               "in the task order."}


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
    if args.action in ("delete", "purge") and in_agent():
        raise Problem("pa-task {} is the person's command; an agent asks them instead".format(args.action))
    if args.action == "archive":
        return archive_cards(directory, args)
    if args.action == "unarchive":
        return unarchive_card(directory, args)
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
            if args.action in ("done", "park", "pause", "next") and before == "needs-decision":  # t-097 R17
                card = open_cards(directory).get(args.task)
                if card:
                    raise Problem("{} is waiting on decision {}. Answer {} first, or ask the Project Agent to withdraw it. "
                                  "Nothing was changed.".format(args.task, card, card))
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
                for sub in ("decisions", ARCHIVE):  # its archived cards too (t-054); restore puts every one in decisions/
                    decisions = os.path.join(directory, sub)
                    for name in os.listdir(decisions) if os.path.isdir(decisions) else []:
                        card = read_json_safe(os.path.join(decisions, name)) if name.endswith(".json") else None
                        if isinstance(card, dict) and card.get("task") == args.task and not os.path.lexists(os.path.join(target, "decisions", name)):
                            os.rename(os.path.join(decisions, name), os.path.join(target, "decisions", name))
                ledger["tasks"] = [t for t in ledger["tasks"] if t.get("id") != args.task]
                after = "deleted"
                trees = task_worktrees(task)
                if len(trees) == 1:
                    note = "Its worktree {} was left in place.".format(trees[0]["given"])
                elif trees:
                    note = "Its worktrees were left in place: {}.".format(", ".join("{} ({})".format(w["given"], w["job"]) for w in trees))
            elif args.action in TASK_MOVES:
                sources, after, only = TASK_MOVES[args.action]
                if before not in sources:
                    raise Problem("{} is {}; pa-task {} moves {}. Nothing was changed.".format(args.task, before, args.action, only))
                task["state"], task["updated"] = after, stamp
                if args.action == "next":
                    task.pop("paused", None)
                else:
                    task["paused"] = {"at": stamp, "by": "person"}
            elif args.action == "unpause":
                if not task.get("paused"):
                    print("{} is not paused.".format(args.task))
                    return
                after = before
                task.pop("paused", None)
                task["updated"] = stamp
                note = "It is no longer paused."
            else:
                after = {"done": "done", "park": "parked", "reopen": "queued"}[args.action]
                if before == after:
                    print("{} is already {}.".format(args.task, after))
                    return
                if args.action == "reopen" and before not in ("done", "parked"):
                    raise Problem("{} is {}; only done or parked tasks are reopened".format(args.task, before))
                task["state"], task["updated"] = after, stamp
                if args.action == "reopen" and before == "done":  # t-120 (t-097 R5): it starts at the bottom of Queued
                    task["reopened"] = stamp  # pa-places and the panel do not read task-order.json's older place for it
                    order = ledger.get("taskOrder")
                    if isinstance(order, dict):  # and the Project Agent's own place for it goes
                        if isinstance(order.get("order"), list):
                            order["order"] = [x for x in order["order"] if x != args.task]
                        if isinstance(order.get("chat"), list):
                            order["chat"] = [e for e in order["chat"] if not (isinstance(e, dict) and e.get("task") == args.task)]
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
            notice = TASK_NOTICES[args.action].format(id=args.task) if args.action in TASK_NOTICES else (
                "The person changed task {} from {} to {} in the panel. Re-read ledger.json before you "
                "write it again, and do not undo this.".format(args.task, before, after))
            call(["hc", "send", agent["terminal"], notice])
        except Problem:
            print("(The Project Agent could not be told right now; it reads the ledger on its next pass.)")


OTHER = "Other"  # the answer in the person's own words (t-046): the words are in the card's note
OTHER_MAX = 2000
OTHER_TOO_LONG = "Your answer is longer than {} characters; shorten it, or write it in a file and give the path.".format(OTHER_MAX)
PUSH_LOCK_WAIT = 5  # seconds pa-decide waits for the space's push lock


def is_other(card):
    """An answer in the person's own words: the marker, or the label Other in any case, even on a card that has an
    option of that name. Every tool that acts on an answer checks this first and does nothing on it."""
    return isinstance(card, dict) and (card.get("other") is True or str(card.get("answer") or "").strip().lower() == "other")


def words_cut(text, limit=200):
    """The person's words on one line, cut at `limit` characters for a journal line; the card keeps them whole."""
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "… (the rest is on the card)"


def keep_previous(card, answer):
    """A withdraw keeps what the card being withdrawn was answered: `answer`, its words and when, written together so
    the three always describe the same answer (t-046 J1b round 2). Absent words leave no previousNote key; an older
    carried value of any of the three is replaced or dropped. Call it before the withdraw changes note or answered."""
    note, answered = card.get("note"), card.get("answered")
    for key, value in (("previousAnswer", answer), ("previousNote", note if str(note or "").strip() else None),
                       ("previousAnswered", answered)):
        if value is None:
            card.pop(key, None)
        else:
            card[key] = value


def pushing_queue_card(card):
    """t-140 R8: a queue card made where pushing is set up (its queue has a push object): its answer runs the push."""
    q = card.get("queue") if isinstance(card, dict) else None
    return isinstance(card, dict) and card.get("kind") == "queue" and isinstance(q, dict) and isinstance(q.get("push"), dict)


def push_card_lock(directory):
    """The space's push lock (<space>/.push.lock, as pa_push.push_lock takes it): every write of a push card holds
    it, so pa-decide never writes one while pa-push-main runs. Returns the open file; close it to unlock."""
    f = open(os.path.join(directory, ".push.lock"), "a")
    end = time.monotonic() + PUSH_LOCK_WAIT
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except OSError:
            if time.monotonic() > end:
                f.close()
                raise Problem("another push for this space is running; try again in a minute. Nothing was written")
            time.sleep(0.1)


def cmd_decide(args):
    """Closes an open decision card from the conversation: with the person's answer in their own
    words, or as withdrawn. For the orchestrator; every use is journaled and shown on the card.
    A push card is read and written under the space's push lock."""
    directory, agent = space_for_command(args.space)
    ident(args.card, "card id")
    path = card_file(directory, args.card)  # an archived card too (t-054)
    card = read_json(path)
    if not card:
        raise Problem("no decision card {}".format(args.card))
    if not (card.get("push") or card.get("kind") == "push" or pushing_queue_card(card)):
        return decide_card(directory, path, args)
    lock = push_card_lock(directory)
    try:
        return decide_card(directory, path, args)
    finally:
        lock.close()


def decide_card(directory, path, args):
    card = read_json(path)  # again: a push card is read under its lock
    if not card:
        raise Problem("no decision card {}".format(args.card))
    withdraw_other = args.withdraw and card.get("status") == "answered" and is_other(card)
    if card.get("status") != "open" and not withdraw_other:
        raise Problem("{} is already {}".format(args.card, card.get("status")))
    stamp = now()
    after = None
    if args.withdraw:
        if not (args.why or "").strip():
            raise Problem("--withdraw needs --why \"<reason>\"")
        if withdraw_other:
            # Their words asked for the question again: the tool that owns the card asks again; the words are kept.
            keep_previous(card, OTHER)
            card.pop("other", None)
        card.update(status="withdrawn", answer=None, note=args.why.strip(), answered=stamp, answeredBy="orchestrator: withdrawn")
        what = "withdrawn"
    else:
        if card.get("worktree"):
            raise Problem("a card that deletes a worktree is answered in the panel, not in conversation")
        if card.get("kind") == "worktrees":
            raise Problem("a card that deletes worktrees is answered in the panel, not in conversation")
        if card.get("push") or card.get("kind") == "push":
            raise Problem("a push card is answered in the panel, not in conversation (its answer runs the push)")
        if pushing_queue_card(card):
            raise Problem("a queue card that pushes is answered in the panel, not in conversation (its answer runs the push)")
        if card.get("kind") == "leftovers":
            raise Problem("a card that closes workers is answered in the panel, not in conversation")
        labels = [o.get("label") for o in card.get("options") or []]
        wanted = str(args.answer or "").strip().lower()
        match = OTHER if wanted == OTHER.lower() else next((l for l in labels if str(l).lower() == wanted), None)
        if match is None:
            listed = [str(l) for l in labels if str(l).strip().lower() != OTHER.lower()] + [OTHER]
            raise Problem("--answer must be one of this card's options: {}".format(", ".join(listed)))
        quote = (args.quote or "").strip()
        if len(quote) < 2:
            raise Problem("--quote needs the person's own words that decide it")
        if match == OTHER:
            if len(quote) > OTHER_MAX:
                raise Problem(OTHER_TOO_LONG)
            card.update(status="answered", answer=OTHER, other=True, note=quote, answered=stamp, answeredBy="person, in conversation")
            what = "answered Other"
            if card.get("rule"):
                after = "Rule {} stays proposed: an Other answer is not Approve or Decline.".format(card["rule"])
        else:
            card.update(status="answered", answer=match, note=quote, answered=stamp, answeredBy="person, in conversation")
            what = "answered {}".format(match)
        # A rule proposal's card decides the rule too, as it does in the panel; an Other answer decides nothing.
        lower = "" if match == OTHER else str(match).lower()
        decided = "active" if lower.startswith("approve") else "declined" if lower.startswith("decline") else None
        if card.get("rule") and decided:
            rules_path = os.path.join(directory, "rules.json")
            rules = read_json(rules_path) or {}
            for r in rules.get("rules", []):
                if r.get("id") == card["rule"] and r.get("status") == "proposed":
                    r["status"] = decided
                    r.setdefault("history", []).append({"at": stamp, "by": "person", "change": ("approved" if decided == "active" else "declined") + " in conversation"})
            if rules.get("rules") is not None:
                write_json(rules_path, rules)
    write_card(directory, path, card)  # in decisions/: one that was archived comes back
    with open(os.path.join(directory, "journal.jsonl"), "a") as journal:
        journal.write(json.dumps({"at": stamp, "by": "orchestrator", "trigger": "conversation", "task": card.get("task"), "decision": args.card,
                                  "why": "{}{}: {}".format(what, ' (was Other: "{}")'.format(words_cut(card.get("previousNote"))) if withdraw_other else "",
                                                           card["note"])}) + "\n")
    print("{} {} ({}).".format(args.card, what, card["answeredBy"]))
    if after:
        print(after)


def stamp_script():
    """The command pa-sync runs on the other machine to write its installed.json."""
    import socket
    source = as_dict(as_dict(installed_stamp()).get("source"))
    argv = ["--synced-from", socket.gethostname().split(".")[0] or "unknown"]
    if as_text(source.get("commit")):
        argv += ["--commit", source["commit"]] + (["--dirty"] if source.get("dirty") is True else [])
    return '"$HOME/.config/herdr-pa/tools/pa.py" stamp ' + " ".join(shlex.quote(a) for a in argv)


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
        print("would stamp installed.json there: " + stamp_script())
        return
    # Scripts must be runnable there, and every command linked where that machine's core looks.
    here = sorted(n for n in os.listdir(os.path.join(HOME, ".local", "bin")) if n.startswith("pa-")
                  and os.path.islink(os.path.join(HOME, ".local", "bin", n)))
    links = " ".join("ln -sfn \"$HOME/.config/herdr-pa/tools/{}\" \"$HOME/.local/bin/{}\";".format(
        os.path.basename(os.path.realpath(os.path.join(HOME, ".local", "bin", n))), n) for n in here)
    script = ("cd \"$HOME/.config/herdr-pa\" && find core tools profiles -type f \\( -name '*.sh' -o -name '*.py' \\) -exec chmod +x {} + ; "
              "mkdir -p \"$HOME/.local/bin\" spaces; " + links + " [ -f config.json ] || echo '{\"version\": 1, \"defaultProfile\": \"" + ident(config().get("defaultProfile"), "profile name") + "\"}' > config.json")
    call(["hc", "exec", "--on", args.on, "--", "sh", "-c", script])
    # Its own stamp, recomputed there over what was just copied (t-037 R1); the commit is this machine's.
    call(["hc", "exec", "--on", args.on, "--", "sh", "-c", stamp_script()])
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
        parser.add_argument("--self-check", action="store_true", help=argparse.SUPPRESS)  # t-037: a restart's pre-check
        parser.add_argument("--restarted", help=argparse.SUPPRESS)  # the request id that exec'd this process
        parser.add_argument("--from-instance", help=argparse.SUPPRESS)
        parser.add_argument("--restart-by", help=argparse.SUPPRESS)
        args = parser.parse_args()
        if args.self_check:
            return cmd_self_check(args)
        if args.restarted:
            return restarted_session(args)
        return cmd_session(args)
    if kind == "pa-profile":
        parser.add_argument("action", nargs="?", default="show", choices=("show", "apply", "set"))
        parser.add_argument("name", nargs="?")
        parser.add_argument("--profile")
        parser.add_argument("--json", action="store_true")
        return cmd_profile(parser.parse_args())
    if kind == "pa-peers":
        parser.add_argument("--json", action="store_true")
        parser.add_argument("--all", action="store_true")
        parser.add_argument("--older", action="store_true", help="only the ones not running the installed code")
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
                              "next <id>: a queued task to in-progress (start it next; no worker is started). pause <id>: an "
                              "in-progress or investigating task to queued, marked paused (nothing is stopped). unpause <id>: clear "
                              "the mark. A needs-decision task with an open card is not marked done, parked, paused or started. "
                              "delete <id>: move the task, its evidence and its decision cards to the trash. restore <id>: bring it back. "
                              "purge [<id>]: permanently delete what is in the trash. archive [--days N] [--dry-run]: move answered and withdrawn "
                              "decision cards older than N days (default 14) to decisions-archive/; open cards and the cards of a task that is not "
                              "done or parked stay; nothing is deleted. unarchive <card-id>: move one card back from decisions-archive/ to decisions/.")
        parser.add_argument("action", choices=("list", "done", "reopen", "park", "next", "pause", "unpause", "delete", "restore", "purge", "archive", "unarchive"))
        parser.add_argument("task", nargs="?")
        parser.add_argument("--space")
        parser.add_argument("--days", type=float, default=ARCHIVE_DAYS, help="archive: cards answered more than this many days ago (default {})".format(ARCHIVE_DAYS))
        parser.add_argument("--dry-run", action="store_true", help="archive: list what would move, move nothing")
        return cmd_task(parser.parse_args())
    if kind == "pa-decide":
        parser.add_argument("card")
        parser.add_argument("--answer", help="one of the card's option labels, or Other for the person's own answer")
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
    if kind == "pa-restart":
        parser.description = "restart this space's Project Agent in place (t-037): same terminal, same workers"
        parser.add_argument("--space", help="a space id (default: this terminal's)")
        parser.add_argument("--on", help="the machine the space lives on (runs pa-restart there with hc exec)")
        parser.add_argument("--now", action="store_true", help="do not wait for the current turn to end")
        parser.add_argument("--fresh", action="store_true", help="start a new conversation (loads all core text now)")
        parser.add_argument("--why", help="why (inside Claude: the person's own words; required there)")
        parser.add_argument("--wait", type=float, help="seconds to wait for the result (default 180, at most 540)")
        parser.add_argument("--dry-run", action="store_true", help="run the checks and show the summary only")
        parser.add_argument("--json", action="store_true")
        parser.add_argument("--end-terminal", action="store_true",
                            help="only for a launcher from before restart support: end its terminal and its workers "
                                 "(asks for yes), then the keeper or pa-start opens a new launcher")
        parser.add_argument("--anyway", action="store_true", help="with --end-terminal: also while workers run (they end)")
        parser.add_argument("--confirmed-terminal", help=argparse.SUPPRESS)  # the --on relay's bound answer (J3.5)
        parser.add_argument("--confirmed-workers", help=argparse.SUPPRESS)
        parser.add_argument("--relay-issue", action="store_true", help=argparse.SUPPRESS)  # the relay's dry run (round 2)
        parser.add_argument("--relay-token", help=argparse.SUPPRESS)  # its one-time token, for the bound run
        parser.add_argument("--expect-instance", help=argparse.SUPPRESS)  # t-037 J4: the panel's click, bound
        parser.add_argument("--by", choices=("panel",), help=argparse.SUPPRESS)  # t-037 J4: the request's `by`
        parser.add_argument("--recover", action="store_true",
                            help="its launcher is not running (a stale heartbeat): end what is left of it (a stopped "
                                 "launcher, orphaned Claudes of its sessions), then open it again (t-193)")
        return cmd_restart(parser.parse_args())
    if kind == "pa-context":
        parser.description = ("set: the Project Agent's context settings (context.json; the person's command, applied at "
                              "its next start). compact-instructions, start-pack, compacted: its compaction hooks (t-041). fresh: a "
                              "fresh context at its next clean point; followup: the reset's follow-up report (t-041 J4).")
        parser.add_argument("action", choices=("set", "compact-instructions", "start-pack", "compacted", "fresh", "followup"))
        parser.add_argument("--space", help="a space id (default: this terminal's)")
        parser.add_argument("--mode", choices=CONTEXT_MODES, help="fresh: the net and the hooks; off: none of them")
        parser.add_argument("--task-cap", help="tokens, like 150k (read by the fresh reset, t-041 J4)")
        parser.add_argument("--pass-cap", help="tokens, like 250k (read by the fresh reset, t-041 J4)")
        parser.add_argument("--net", help="tokens, like 400k: Claude Code's auto-compact window for the Project Agent")
        parser.add_argument("--why", help="fresh: why (inside Claude: the person's own words)")
        parser.add_argument("--on", help="set, fresh: run it on that machine (hc exec)")
        parser.add_argument("--dry-run", action="store_true", help="fresh: show the size and what a reset meets now")
        parser.add_argument("--withdraw", action="store_true", help="fresh: withdraw the pending request")
        parser.add_argument("--json", action="store_true", help="set, fresh, followup: one JSON line")
        parser.add_argument("--since", help="followup: from (default: context.json enabledAt)")
        parser.add_argument("--until", help="followup: until")
        parser.add_argument("--by", choices=("panel",), help=argparse.SUPPRESS)  # the panel's own flag (t-041 J5)
        return cmd_context(parser.parse_args())
    if kind == "pa-stamp":
        parser.add_argument("--commit", help="the repo commit installed from")
        parser.add_argument("--dirty", action="store_true", help="the checkout had uncommitted changes")
        parser.add_argument("--synced-from", help="the machine pa-sync copied from")
        return cmd_stamp(parser.parse_args())
    raise Problem("invoke as pa-start, pa-session, pa-claude, pa-codex, pa-profile, pa-peers, pa-pass, pa-cleanup, pa-doctor, pa-task, pa-decide, pa-sync, pa-core-check, pa-stamp, pa-restart or pa-context")


if __name__ == "__main__":
    try:
        main()
    except Problem as e:
        print("{}: {}".format(os.path.basename(sys.argv[0]), e), file=sys.stderr)
        sys.exit(2)
