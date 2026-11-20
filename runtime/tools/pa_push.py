#!/usr/bin/env python3
"""The person's push of a main branch, from a push card in the Project panel. Python standard library only (3.7+).

Selected by the name it is invoked as (symlinks in ~/.local/bin):

  pa-push-main plan <task-id> --repo <main checkout> [--space ID]
                                                 open a push card for a task that passed its full gate
  pa-push-main check <card> [--space ID] [--json]
                                                 the card's facts read now, the first failing check, the seen token
  pa-push-main run <card> --answer Push|"Not now"|Other --space ID [--seen TOKEN] [--note T] [--json]
                                                 the person's answer, run by the Project panel on their click:
                                                 every check again under a lock, then a fast-forward push of
                                                 exactly the gated commit to the allowlist URL (never a force).
                                                 On a queue card that pushes (d-queue-N with queue.push, t-140 Part
                                                 A), check reads what each merge option would merge and push now, and
                                                 run (--answer <the card's label>) takes the queue lock, then the push
                                                 lock, merges the option's entries and pushes exactly those commits
  pa-push-main run --standing <rule id> --repo <main checkout> --space ID [--json]
                                                 the Project Agent's push of the queue's merges made on the person's
                                                 standing rule (t-140 Part B): only while the rule is in force (active,
                                                 the person's, bound on the allowlist with its text unchanged) and not
                                                 stopped; every check of run but the card's; a refusal stops the rule
  pa-push-allow show [--json] [--space ID]       the push allowlist (<PA home>/push-allow.json): valid or why not,
                                                 on or off, its SHA-256 and time, each entry
  pa-push-allow add --path P --space S [--url U] [--branch B] [--note T] [--pre-push ARG...] [--standing RULE]
  pa-push-allow set --path P --space S --standing RULE | --unbind
                                                 bind (or unbind) the person's standing rule: its id and its text's SHA
  pa-push-allow remove --path P --space S
  pa-push-allow on|off [--path P --space S]      the person's commands: change the allowlist (refused for agents)

Off by default: with no push-allow.json, or "enabled": false, nothing here pushes. Agents never push, but for the
Project Agent's --standing run under the person's bound rule: the person-only commands refuse an agent's shell (its
variables, a claude or codex ancestor process, or a failed ps). That fence stops agents that follow their rules; it
does not prove a human.
"""
import argparse
import contextlib
import datetime
import fcntl
import hashlib
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True  # keep tools/ free of caches
if __name__ == "__main__":  # run as pa-push-main: pa_queue's import finds this module, not a second copy
    sys.modules.setdefault("pa_push", sys.modules[__name__])
import pa  # noqa: E402
import pa_hooks  # noqa: E402

ALLOW_FILE = "push-allow.json"
MAX_REPOS = 50
NOTE_MAX = 200
TOP_KEYS = ("version", "enabled", "repos")
ENTRY_KEYS = ("path", "url", "branch", "space", "enabled", "prePush", "note", "standing")
STANDING_KEYS = ("rule", "textSha")  # t-140 R10: the person's binding of their standing rule to this entry
_QUEUE = [None]


def use_queue(module):
    """pa_queue registers itself when it loads (t-140): the queue card's check and run use that module."""
    _QUEUE[0] = module


def queue_tool():
    if _QUEUE[0] is None:
        import pa_queue  # noqa: F401 (it registers itself with use_queue)
        _QUEUE[0] = _QUEUE[0] or pa_queue
    return _QUEUE[0]
ENTRY_REQUIRED = ("path", "url", "branch", "space", "enabled")
URL = re.compile(r"^(ssh://\S+|[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:\S+|https://\S+|file:///\S+)$")
PLACEHOLDERS = ("path", "repo", "commit", "url", "branch")
PLACEHOLDER = re.compile(r"\{([^{}]*)\}")


class Refused(Exception):
    """A check that failed: check is its name (C0, C6a, ...), message what the person reads."""

    def __init__(self, check, message):
        Exception.__init__(self, message)
        self.check, self.message = check, message


# ---- the allowlist (R3) -----------------------------------------------------------------------------

def allow_path():
    return os.path.join(pa.ROOT, ALLOW_FILE)


def utc(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_allow():
    """(data, problem, sha, mtime). A missing file is (None, None, None, None): off. An invalid file has a problem
    and allows nothing; its SHA-256 and time are still given."""
    path = allow_path()
    try:
        with open(path, "rb") as f:
            raw = f.read()
        mtime = utc(os.stat(path).st_mtime)
    except FileNotFoundError:
        return None, None, None, None
    except OSError as e:
        return None, "cannot read it ({})".format(e.strerror or e), None, None
    sha = hashlib.sha256(raw).hexdigest()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        return None, "not JSON ({})".format(e), sha, mtime
    return data, validate(data), sha, mtime


def branch_problem(branch):
    """None when branch is a name git accepts for a branch (git check-ref-format --branch rules), without a
    leading - or a *; else why not."""
    if not isinstance(branch, str) or not branch:
        return "must be a non-empty string"
    if branch.startswith("-"):
        return "must not start with -"
    if branch == "@" or "@{" in branch or ".." in branch or "//" in branch:
        return "has @{, .., // or is @"
    if branch.startswith("/") or branch.endswith("/") or branch.endswith("."):
        return "must not start or end with / or end with ."
    if re.search(r"[\x00-\x20\x7f~^:?*\[\\]", branch):
        return "has a space, a control character or one of ~ ^ : ? * [ \\"
    for part in branch.split("/"):
        if part.startswith(".") or part.endswith(".lock"):
            return "has a part starting with . or ending with .lock"
    return None


def entry_problem(e):
    if not isinstance(e, dict):
        return "must be an object"
    extra = sorted(set(e) - set(ENTRY_KEYS))
    if extra:
        return "unknown key {}".format(", ".join(extra))
    for key in ENTRY_REQUIRED:
        if key not in e:
            return "{} is missing".format(key)
    if not isinstance(e["path"], str) or not os.path.isabs(e["path"]) or "\x00" in e["path"]:
        return "path must be an absolute path"
    if not isinstance(e["url"], str) or e["url"].startswith("-") or not URL.match(e["url"]):
        return "url must be ssh://..., user@host:path, https://... or file:///..., with no spaces"
    why = branch_problem(e["branch"])
    if why:
        return "branch {}".format(why)
    if not isinstance(e["space"], str) or not pa.ID.match(e["space"]):
        return "space must be a space id (letters, digits, _ and -)"
    if not isinstance(e["enabled"], bool):
        return "enabled must be true or false"
    if "prePush" in e:
        pre = e["prePush"]
        if not isinstance(pre, list) or not pre or not all(isinstance(a, str) and a for a in pre):
            return "prePush must be a list of non-empty strings"
        if not os.path.isabs(pre[0]) or PLACEHOLDER.search(pre[0]):
            return "prePush[0] must be an absolute program path, without a placeholder"
        for arg in pre:
            for name in PLACEHOLDER.findall(arg):
                if name not in PLACEHOLDERS:
                    return "prePush has an unknown placeholder {{{}}} (known: {})".format(
                        name, ", ".join("{" + p + "}" for p in PLACEHOLDERS))
    if "note" in e and (not isinstance(e["note"], str) or len(e["note"]) > NOTE_MAX):
        return "note must be a string of at most {} characters".format(NOTE_MAX)
    if "standing" in e:
        b = e["standing"]
        if not isinstance(b, dict) or sorted(b) != sorted(STANDING_KEYS) or not isinstance(b.get("rule"), str) or \
                not pa.ID.match(b["rule"]) or not isinstance(b.get("textSha"), str) or not re.match(r"^[0-9a-f]{64}$", b["textSha"]):
            return "standing must be {\"rule\": <rule id>, \"textSha\": <SHA-256 of its text>} (pa-push-allow set --standing)"
    return None


def validate(data):
    """None when data is a valid allowlist; else why not. All or nothing: one bad entry makes the file invalid."""
    if not isinstance(data, dict):
        return "the file must hold a JSON object"
    extra = sorted(set(data) - set(TOP_KEYS))
    if extra:
        return "unknown key {}".format(", ".join(extra))
    if type(data.get("version")) is not int or data.get("version") != 1:
        return "version must be 1"
    if not isinstance(data.get("enabled"), bool):
        return "enabled must be true or false"
    repos = data.get("repos")
    if not isinstance(repos, list):
        return "repos must be a list"
    if len(repos) > MAX_REPOS:
        return "repos has {} entries; at most {}".format(len(repos), MAX_REPOS)
    seen = set()
    for i, e in enumerate(repos):
        why = entry_problem(e)
        if why:
            return "repos[{}]: {}".format(i, why)
        key = (os.path.realpath(e["path"]), e["space"])
        if key in seen:
            return "repos[{}]: {} is listed twice for space {}".format(i, e["path"], e["space"])
        seen.add(key)
    return None


def fill(argv, **values):
    """prePush with its placeholders filled in ({path} {repo} {commit} {url} {branch})."""
    return [PLACEHOLDER.sub(lambda m: values[m.group(1)], a) for a in argv]


def allow_report(space=None):
    """What pa-push-allow show prints (and the Rules view reads with --json)."""
    data, problem, sha, mtime = load_allow()
    exists = data is not None or problem is not None
    report = {"file": allow_path(), "exists": exists, "valid": problem is None, "problem": problem, "sha": sha, "mtime": mtime,
              "enabled": bool(exists and problem is None and data.get("enabled")), "space": space, "repos": []}
    if not exists:
        report["state"] = "off (the default): no push allowlist"
    elif problem:
        report["state"] = "not valid, so off: " + problem
    else:
        report["state"] = "on" if data["enabled"] else "off (enabled: false)"
        report["repos"] = [dict(e, thisSpace=bool(space) and e["space"] == space) for e in data["repos"]]
    return report


# ---- git (every call goes through git_call) ------------------------------------------------------

def clean_env(env=None):
    """The environment for every git call: no GIT_* variable (in any case), and no prompt for a password."""
    env = dict(os.environ if env is None else env)
    for k in list(env):
        if k.upper().startswith("GIT_"):
            del env[k]
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def git_call(argv, timeout, cwd=None, input=None):
    """(exit code, stdout, stderr) of one program: an argv list, never a shell, with clean_env(). A call past
    its timeout gives 124; a program that cannot start gives 127. input (bytes, t-104): given on stdin instead of
    an empty one; stdout is then the answer's bytes, stderr text as without it."""
    raw = input is not None
    how = {"input": input} if raw else {"stdin": subprocess.DEVNULL, "universal_newlines": True}
    try:
        r = subprocess.run(list(argv), cwd=cwd, env=clean_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=max(1, timeout), **how)
    except subprocess.TimeoutExpired:
        return 124, b"" if raw else "", "{} timed out after {} s".format(" ".join(argv[:4]), int(timeout))
    except OSError as e:
        return 127, b"" if raw else "", str(e)
    return r.returncode, r.stdout, pa_hooks.as_text(r.stderr, "replace") if raw else r.stderr


def base(path, hooks):
    """git -C <path> with the repository's hooks and fsmonitor switched off."""
    return ["git", "-C", path, "-c", "core.hooksPath=" + hooks, "-c", "core.fsmonitor=false"]


# ---- who is calling (C0) -------------------------------------------------------------------------

AGENT_VARS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_SANDBOX", "CODEX_HOME_SESSION")
AGENT_PREFIXES = ("CODEX_", "CLAUDE_CODE_")
AGENT_NAMES = ("claude", "codex")
# d-069: check and run belong to the panel, so one of these must be among the caller's ancestors. The only place the
# names live: the Herdr app (Herdr.app/Contents/MacOS/Herdr on macOS) and its core (herdr-core, also inside
# "Herdr Core.app"), compared with the basename of each ancestor's command, lowercased. No variable or flag adds to
# them (plan 5: nothing turns C0 off); a name that does not match refuses, it never lets a caller through.
PANEL_NAMES = ("herdr", "herdr-core")
DEPTH = 40
PS = "/bin/ps" if os.path.exists("/bin/ps") else "ps"


def process_table():
    """{pid: (ppid, comm, args)} of every process, from two ps calls; None when ps fails."""
    try:
        a = subprocess.run([PS, "-A", "-o", "pid=", "-o", "ppid=", "-o", "comm="], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, universal_newlines=True, timeout=10)
        b = subprocess.run([PS, "-A", "-o", "pid=", "-o", "args="], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, universal_newlines=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if a.returncode or b.returncode:
        return None
    args = {}
    for line in b.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if parts and parts[0].isdigit():
            args[int(parts[0])] = parts[1] if len(parts) > 1 else ""
    table = {}
    for line in a.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            pid = int(parts[0])
            table[pid] = (int(parts[1]), parts[2] if len(parts) > 2 else "", args.get(pid, ""))
    return table or None


def ancestors(pid=None, table=None):
    """[{pid, comm, args}] of the parent of pid (default: this process) and its parents, at most DEPTH.
    table: {pid: (ppid, comm, args)}, the test seam; read with ps when None. None when it cannot be read."""
    table = process_table() if table is None else table
    if not table:
        return None
    pid = os.getpid() if pid is None else pid
    if pid not in table:
        return None
    out, seen = [], set()
    ppid = table[pid][0]
    while ppid and ppid not in seen and len(out) < DEPTH:
        seen.add(ppid)
        if ppid not in table:
            break
        parent, comm, args = table[ppid]
        out.append({"pid": ppid, "comm": comm, "args": args})
        ppid = parent
    if not out:
        return None
    return out


def caller_refusal(env, ancs, strict=True):
    """Why this caller is not the person (C0), or None. env: the environment; ancs: ancestors() (None when ps
    failed: refused). strict (check and run, the panel's) adds C0.3, a Herdr terminal, and C0.4, the Herdr app or
    its core (PANEL_NAMES) must be among the ancestors. Not strict (pa-push-allow, typed in the person's own
    terminal): no agent among the ancestors, and not an orphan."""
    for k in AGENT_VARS:
        if k in env:
            return "{} is set: an agent's shell".format(k)
    for k in sorted(env):
        if k.upper().startswith(AGENT_PREFIXES):
            return "{} is set: an agent's shell".format(k)
    if ancs is None:
        return "the process tree could not be read (ps failed), so the caller is unknown"
    if not strict and all(a.get("pid") == 1 for a in ancs):
        return "the caller is unknown (an orphaned process): its only ancestor is the system's first process"
    for a in ancs:
        name = os.path.basename(str(a.get("comm") or "")).lower()
        words = str(a.get("args") or "").lower()
        if name in AGENT_NAMES:
            return "an ancestor process is {} (pid {})".format(name, a.get("pid"))
        if name == "node" and any(n in words for n in AGENT_NAMES):
            return "an ancestor process is node running {} (pid {})".format(
                next(n for n in AGENT_NAMES if n in words), a.get("pid"))
    if strict and "HC_TERMINAL_ID" in env:
        return "HC_TERMINAL_ID is set: a Herdr terminal; pa-push-main run belongs to the panel"
    if strict and not any(os.path.basename(str(a.get("comm") or "")).lower() in PANEL_NAMES for a in ancs):
        return "the caller is not the panel: no Herdr app or core process ({}) among its ancestors ({})".format(
            ", ".join(PANEL_NAMES), ", ".join(ancestor_names(ancs)) or "none")
    return None


def ancestor_names(ancs, n=8):
    return [os.path.basename(str(a.get("comm") or "")) for a in (ancs or [])[:n]]


# ---- pa-push-allow -------------------------------------------------------------------------------

def allow_parser():
    parser = argparse.ArgumentParser(prog="pa-push-allow", description="The person's push allowlist ({}).".format(allow_path()))
    sub = parser.add_subparsers(dest="action")
    sub.required = True
    show = sub.add_parser("show", help="print the allowlist (anyone)")
    show.add_argument("--json", action="store_true")
    show.add_argument("--space")
    add = sub.add_parser("add", help="add an entry (the person's command)")
    add.add_argument("--path", required=True)
    add.add_argument("--space", required=True)
    add.add_argument("--url")
    add.add_argument("--branch")
    add.add_argument("--note")
    add.add_argument("--pre-push", nargs="+", dest="pre_push")
    add.add_argument("--standing", help="bind the person's standing rule to merge and push (its id in the space's rules.json)")
    bind = sub.add_parser("set", help="bind or unbind the person's standing rule on an entry (the person's command)")
    bind.add_argument("--path", required=True)
    bind.add_argument("--space", required=True)
    how = bind.add_mutually_exclusive_group(required=True)
    how.add_argument("--standing", help="the rule's id in the space's rules.json; its text's SHA-256 is stored with it")
    how.add_argument("--unbind", action="store_true", help="remove the binding")
    remove = sub.add_parser("remove", help="remove an entry (the person's command)")
    remove.add_argument("--path", required=True)
    remove.add_argument("--space", required=True)
    for name in ("on", "off"):
        p = sub.add_parser(name, help="turn the file, or one entry, {} (the person's command)".format(name))
        p.add_argument("--path")
        p.add_argument("--space")
    return parser


def cmd_allow_show(args):
    report = allow_report(pa.local_id(args.space or "") or None)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    print("Push allowlist {}: {}".format(report["file"], report["state"]))
    if report["exists"]:
        print("  changed {}  sha256 {}".format(report["mtime"], report["sha"]))
    for e in report["repos"]:
        print("  {} {} {} -> {} {} (space {}){}{}".format("*" if e["thisSpace"] else "-", "on " if e["enabled"] else "off",
                                                        e["path"], e["url"], e["branch"], e["space"],
                                                        "  prePush: " + " ".join(e["prePush"]) if e.get("prePush") else "",
                                                        "  note: " + e["note"] if e.get("note") else ""))
    return 0


def find_entries(data, path, space):
    real = os.path.realpath(path)
    return [e for e in data["repos"] if os.path.realpath(e["path"]) == real and e["space"] == space]


def allow_change(args, env=None, ancestors=None):
    """add, remove, on, off: the person's own changes. Refused for agents (C0.1-C0.2: a plain terminal of the
    person's, which may be a Herdr terminal, is allowed). The result is validated before it is written; the
    old file is kept as push-allow.json.bak-pa-push-allow[-n]."""
    env = os.environ if env is None else env
    why = caller_refusal(env, ancestors, strict=False)
    if why:
        print("pa-push-allow: refused: pa-push-allow {} is the person's command; run it in your own terminal. ({})".format(
            args.action, why), file=sys.stderr)
        return 1
    data, problem, _sha, _mtime = load_allow()
    if problem:
        print("pa-push-allow: the push allowlist is not valid: {}; fix it by hand".format(problem), file=sys.stderr)
        return 1
    data = data or {"version": 1, "enabled": True, "repos": []}
    if args.action in ("on", "off") and not (args.path or args.space):
        data["enabled"] = args.action == "on"
        what = "the push allowlist is {}".format(args.action)
    else:
        if not (args.path and args.space):
            print("pa-push-allow: give both --path and --space, or neither (on/off for the whole file)", file=sys.stderr)
            return 1
        space = pa.local_id(args.space)
        found = find_entries(data, args.path, space)
        if args.action == "add":
            if found:
                print("pa-push-allow: {} is already on the allowlist for space {}".format(args.path, space), file=sys.stderr)
                return 1
            path = os.path.realpath(args.path)
            entry = {"path": path, "url": args.url or default_url(path), "branch": args.branch or default_branch(path),
                     "space": space, "enabled": True}
            if args.pre_push:
                entry["prePush"] = args.pre_push
            if args.note:
                entry["note"] = args.note
            if getattr(args, "standing", None):
                binding, why = binding_for(space, args.standing)
                if why:
                    print("pa-push-allow: not changed: " + why, file=sys.stderr)
                    return 1
                entry["standing"] = binding
            data["repos"].append(entry)
            what = "added: " + json.dumps(entry)
        elif not found:
            print("pa-push-allow: no entry for {} in space {}".format(args.path, space), file=sys.stderr)
            return 1
        elif args.action == "set":
            if getattr(args, "unbind", False):
                for e in found:
                    e.pop("standing", None)
                what = "the entry for {} in space {} has no standing rule".format(args.path, space)
            else:
                binding, why = binding_for(space, args.standing)
                if why:
                    print("pa-push-allow: not changed: " + why, file=sys.stderr)
                    return 1
                for e in found:
                    e["standing"] = binding
                what = "the entry for {} in space {} is bound to rule {} (text sha256 {})".format(
                    args.path, space, binding["rule"], binding["textSha"])
        elif args.action == "remove":
            data["repos"] = [e for e in data["repos"] if e not in found]
            what = "removed the entry for {} in space {}".format(args.path, space)
        else:
            for e in found:
                e["enabled"] = args.action == "on"
            what = "the entry for {} in space {} is {}".format(args.path, space, args.action)
    problem = validate(data)
    if problem:
        print("pa-push-allow: not changed: the result would not be valid: {}".format(problem), file=sys.stderr)
        return 1
    pa.backup(allow_path(), "pa-push-allow")
    pa.write_json(allow_path(), data)
    print("{} ({}).".format(what, allow_path()))
    return 0


def binding_for(space, rule_id):
    """({rule, textSha}, None) for the rule of that id in the space's rules.json, or (None, why not): the person binds
    the rule as its text stands now (t-140 R10 (1))."""
    if not isinstance(rule_id, str) or not pa.ID.match(rule_id):
        return None, "--standing needs a rule id"
    directory = os.path.join(pa.ROOT, "spaces", space)
    rules = space_rules(directory)
    if rules is None:
        return None, "space {} has no valid rules.json".format(space)
    found = [r for r in rules if r.get("id") == rule_id]
    if len(found) != 1 or not isinstance(found[0].get("text"), str):
        return None, "space {} has {} rule(s) with the id {} (and a text); bind one".format(space, len(found), rule_id)
    return {"rule": rule_id, "textSha": text_sha(found[0]["text"])}, None


def text_sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def space_rules(directory):
    data = pa.read_json_safe(os.path.join(directory, "rules.json"))
    rules = data.get("rules") if isinstance(data, dict) else None
    return [r for r in rules if isinstance(r, dict)] if isinstance(rules, list) else None


def default_url(path):
    hooks = os.devnull
    code, out, _err = git_call(base(path, hooks) + ["remote", "get-url", "origin"], 30)
    return out.strip() if not code else ""


def default_branch(path):
    code, out, _err = git_call(base(path, os.devnull) + ["symbolic-ref", "--short", "-q", "HEAD"], 30)
    return out.strip() if not code else ""


# ---- facts (C6-C11), shared by plan, check and run --------------------------------------------------

FACT_KEYS = ("path", "url", "branch", "originUrl", "remoteNow", "localNow", "commit", "taskBranch", "branchTip", "merge",
             "gate", "commits", "count", "allow", "commands", "checkedAt")
COMMITS_MAX = 200  # commits recorded on a card
CONTEXT_COMMITS = 20  # commits written into the card's context
HEX = re.compile(r"^([0-9a-f]{40}|[0-9a-f]{64})$")
RISKY = re.compile(r"^(core\.(hookspath|sshcommand|fsmonitor|gitproxy|askpass|editor|pager)|url\..*|credential\..*|include(if)?\..*"
                   r"|remote\..*\.(receivepack|uploadpack|pushurl|vcs)|push\..*|gpg\..*|filter\..*|protocol\..*|http\..*\.proxy|http\.proxy)$",
                   re.I)
SHOWN_HOOKS = "<an empty folder>"
SHOWN_CLONE = "<a temporary clone of the commit>"


def short(sha):
    return (sha or "")[:7] or "none"


class TakesInput:
    """True while git_call takes stdin (t-104 D8): Session.gate_git's takes_input. A git_call put in its place
    without an input parameter (a test's recorder) keeps the content path on today's per-call calls."""

    def __bool__(self):
        try:
            return "input" in inspect.signature(git_call).parameters
        except (TypeError, ValueError):
            return False


class Session:
    """The git context of one plan, check or run: an empty hooks folder (removed by close), a deadline, and the run's
    cache of object-addressed content answers (t-094 R3, R6: the gate it evaluates; dropped by close). Its own
    push-time reads (self.git) never go through it."""

    def __init__(self, deadline=None):
        self.deadline = deadline
        self.hooks = tempfile.mkdtemp(prefix="pa-push-hooks-")
        self.cache = pa_hooks.ContentCache()

    def left(self):
        return float("inf") if self.deadline is None else self.deadline - time.monotonic()

    def git(self, path, args, timeout=30, input=None):
        left = self.left()
        if left < 2:
            return 124, "" if input is None else b"", "ran out of time"
        if input is None:
            return git_call(base(path, self.hooks) + list(args), min(timeout, left))
        return git_call(base(path, self.hooks) + list(args), min(timeout, left), input=input)

    def gate_git(self, path, *args, timeout=None, input=None):
        """pa_hooks.git()'s signature and stripped result, run as self.git runs (hooks and fsmonitor off, no
        GIT_*): the runner the gate evaluation is given, so it runs nothing the task worktree names. With input
        (t-104), as pa_hooks.git(): stdout is the answer's bytes, unstripped (a strip would cut a batch's last object)."""
        args = list(args)
        if "status" in args:  # never look inside a nested repository (its own settings could name a program)
            args.insert(args.index("status") + 1, "--ignore-submodules=all")
        code, out, err = self.git(path, args, timeout or pa_hooks.GIT_TIMEOUT, input=input)
        return code, out if input is not None else out.strip(), err.strip()
    gate_git.takes_input = TakesInput()  # read through the bound method: content_record batches with it (t-104 R1)

    def sha(self, path, ref):
        code, out, _err = self.git(path, ["rev-parse", "--verify", "-q", ref + "^{commit}"])
        out = out.strip()
        return out if not code and HEX.match(out) else None

    def is_ancestor(self, path, a, b):
        return self.git(path, ["merge-base", "--is-ancestor", a, b])[0] == 0

    def close(self):
        shutil.rmtree(self.hooks, ignore_errors=True)
        if self.cache is not None:
            self.cache.close()  # t-104 R11: a runner still holding it (Ctx.run) caches nothing after this
        self.cache = None


def tracked_changes(s, path):
    """Lines of `git status --porcelain --untracked-files=no`, without .claude/ (as the gate counts), or None. It
    never looks inside a nested repository (--ignore-submodules=all), whose own settings could name a program."""
    code, out, _err = s.git(path, ["--no-optional-locks", "status", "--porcelain", "--untracked-files=no",
                                   "--ignore-submodules=all"])
    if code:
        return None
    return [l for l in out.splitlines() if l.strip() and not l[3:].strip('"').startswith(".claude/")]


def task_places(worktree):
    """The folders of the task's worktree that the gate and live_pin look in: the worktree, and its src/<name>
    children (a folder of packages). [] when there is no worktree."""
    if not worktree or not os.path.isdir(worktree):
        return []
    root = os.path.realpath(worktree)
    places = [root]
    src = os.path.join(root, "src")
    if os.path.isdir(src):
        places += [os.path.join(src, n) for n in sorted(os.listdir(src)) if os.path.isdir(os.path.join(src, n))]
    return places


def task_settings_refusal(s, worktree):
    """C8a, before the gate is evaluated: the git settings of every repository in the task's worktree that the gate
    or live_pin runs git status in, read as C7b reads the main checkout's (the local scope, and the worktree scope
    when extensions.worktreeConfig is on). The worktree is the agent's, so a key that can run a program refuses, and
    so does a nested repository entry (a submodule, staged or committed): git would read that repository's own
    settings. The refusal message, or None."""
    for place in task_places(worktree):
        code, top, _ = s.git(place, ["rev-parse", "--show-toplevel"])
        if code or os.path.realpath(top.strip()) != place:
            continue
        code, wt_config, _ = s.git(place, ["config", "--local", "--bool", "--get", "extensions.worktreeConfig"])
        for scope in ("local", "worktree") if wt_config.strip() == "true" else ("local",):
            code, out, err = s.git(place, ["config", "--" + scope, "--list", "--name-only"])
            if code:
                return "could not read the {} git settings of the task's worktree {} ({}); nothing was pushed".format(
                    scope, place, err.strip()[-200:])
            for key in out.split():
                if RISKY.match(key):
                    return ("the task's worktree {} has git setting {} ({}), which can run a program or redirect git; "
                            "remove it, run pa-gate again and ask for a new card").format(place, key, scope)
        why = nested_repository(s, place)
        if why:
            return why
    return None


def nested_repository(s, place):
    """The refusal for a nested repository entry (mode 160000, a submodule) in place's index, or None."""
    code, out, err = s.git(place, ["ls-files", "--stage", "-z"])
    if code:
        return "could not list the entries of the task's worktree {} ({}); nothing was pushed".format(place, err.strip()[-200:])
    for record in out.split("\0"):
        meta, _tab, name = record.partition("\t")
        if not meta.startswith("160000 "):
            continue
        code, tree, _ = s.git(place, ["--literal-pathspecs", "ls-tree", "-z", "HEAD", "--", name])
        how = "committed" if not code and tree.startswith("160000 ") else "staged"
        return ("the task's worktree {} holds a nested repository {} ({}, a submodule entry); git would look inside it "
                "with that repository's own settings; remove it, run pa-gate again and ask for a new card").format(
                    place, pa_hooks.cut(pa_hooks.clean(name), 200), how)
    return None


def live_pin(s, worktree, common):
    """The task worktree's repository whose common dir is common, read now: {repo, commit, uncommitted}, or None.
    The same places the gate looks: the worktree itself, else its src/<name> children."""
    if not worktree or not os.path.isdir(worktree):
        return None
    root = os.path.realpath(worktree)
    places = [root]
    src = os.path.join(root, "src")
    if os.path.isdir(src):
        places += [os.path.join(src, n) for n in sorted(os.listdir(src)) if os.path.isdir(os.path.join(src, n))]
    for place in places:
        code, top, _ = s.git(place, ["rev-parse", "--show-toplevel"])
        if code or os.path.realpath(top.strip()) != place:
            continue
        code, here, _ = s.git(place, ["rev-parse", "--path-format=absolute", "--git-common-dir"])
        if code or os.path.realpath(here.strip()) != common:
            continue
        changed = tracked_changes(s, place)
        return {"repo": place, "commit": s.sha(place, "HEAD"), "uncommitted": None if changed is None else len(changed)}
    return None


def main_settings_refusal(s, path):
    """C7b: the git settings of the main checkout path (the local scope, and the worktree scope when
    extensions.worktreeConfig is on) hold no key that can run a program or redirect the push. The refusal message,
    or None. Shared with pa-queue's merge (t-052 J5, P5)."""
    real = os.path.realpath(path)
    # The worktree scope exists only with extensions.worktreeConfig (git refuses --worktree otherwise, and then
    # the local scope is the whole story).
    code, wt_config, _ = s.git(real, ["config", "--local", "--bool", "--get", "extensions.worktreeConfig"])
    for scope in ("local", "worktree") if wt_config.strip() == "true" else ("local",):
        code, out, err = s.git(real, ["config", "--" + scope, "--list", "--name-only"])
        if code:
            return "could not read the {} git settings of {} ({}); nothing was pushed".format(scope, path, err.strip()[-200:])
        for key in out.split():
            if RISKY.match(key):
                return "{} has git setting {} ({}), which can run a program or redirect the push; remove it, or push by hand".format(
                    path, key, scope)
    return None


def push_job(directory, task, common):
    """t-192 R2: the recorded job (other than main) whose gate a push of the repository `common` reads, or None (the
    task's own GATE.json, as before): when GATE.json pins no commit of this repository and exactly one recorded job's
    GATE-<job>.json does. Reads the gate files only (no git: C8a's settings check comes first)."""
    def pins_here(name):
        g = pa.read_json_safe(os.path.join(directory, "evidence", task["id"], name))
        return isinstance(g, dict) and any(isinstance(c, dict) and isinstance(c.get("common"), str)
                                           and os.path.realpath(c["common"]) == common for c in g.get("commits") or [])
    if pins_here("GATE.json"):
        return None
    found = []
    for w in task.get("worktrees") or [] if isinstance(task.get("worktrees"), list) else []:
        job = w.get("job") if isinstance(w, dict) else None
        try:
            if not isinstance(job, str) or job in found or not pa_hooks.files_job(task, job):
                continue
        except pa.Problem:
            continue
        if pins_here(pa_hooks.check_files(job)[2]):
            found.append(job)
    return found[0] if len(found) == 1 else None


def gather(s, directory, space, push, facts, plan=False, ontos=None):
    """Checks C6-C11 in order, filling facts as it goes. Raises Refused at the first that fails. plan: the card does
    not exist yet, so its fields are taken from what is read now (and push is filled in), instead of compared.
    ontos (t-140 R2, the check of a queue card that pushes, which writes nothing): C8h also takes the branch of an
    entry ahead that the card's own merge would put on main first. Returns "already" when the remote branch is at
    the commit already, else None."""
    path = push["path"]
    real = os.path.realpath(path)
    facts["path"] = path
    # C6: the allowlist
    data, problem, sha, mtime = load_allow()
    facts["allow"] = {"file": allow_path(), "sha": sha, "mtime": mtime,
                      "changedSinceCard": bool(not plan and sha != push.get("allowSha")), "entry": None}
    if data is None and problem is None:
        raise Refused("C6a", "no push allowlist ({}); main-branch pushes are off".format(allow_path()))
    if problem:
        raise Refused("C6b", "the push allowlist is not valid: {}; fix it by hand".format(problem))
    if not data["enabled"]:
        raise Refused("C6c", "the push allowlist is turned off (enabled: false)")
    mine = [e for e in data["repos"] if os.path.realpath(e["path"]) == real and e["space"] == space]
    others = [e for e in data["repos"] if os.path.realpath(e["path"]) == real and e["space"] != space]
    if not mine:
        if others:
            raise Refused("C6g", "the allowlist entry for {} names space {}, not this space {}".format(path, others[0]["space"], space))
        raise Refused("C6d", "no allowlist entry for {}".format(path))
    if len(mine) > 1:
        raise Refused("C6d", "{} allowlist entries match {} for this space; keep one".format(len(mine), path))
    entry = mine[0]
    facts["allow"]["entry"] = entry
    if plan:
        push.update(url=entry["url"], branch=entry["branch"])
    if entry["url"] != push["url"]:
        raise Refused("C6e", "the allowlist entry for {} has URL {}, the card says {}".format(path, entry["url"], push["url"]))
    if entry["branch"] != push["branch"]:
        raise Refused("C6f", "the allowlist entry for {} has branch {}, the card says {}".format(path, entry["branch"], push["branch"]))
    if not entry["enabled"]:
        raise Refused("C6h", "the allowlist entry for {} is turned off".format(path))
    if plan:
        push.update(allowSha=sha, allowMtime=mtime)
    if sha != push.get("allowSha"):
        raise Refused("C6i", "the push allowlist changed since this card was made; ask for a new card")
    url, branch = entry["url"], entry["branch"]
    facts.update(url=url, branch=branch)
    # C7: the main checkout and its settings
    code, top, _ = s.git(real, ["rev-parse", "--show-toplevel"])
    code2, common, _ = s.git(real, ["rev-parse", "--path-format=absolute", "--git-common-dir"])
    common = os.path.realpath(common.strip()) if not code2 else None
    if code or code2 or os.path.realpath(top.strip()) != real or common != os.path.realpath(os.path.join(real, ".git")):
        raise Refused("C7a", "{} is not the main checkout of a git repository".format(path))
    why = main_settings_refusal(s, path)
    if why:
        raise Refused("C7b", why)
    code, out, err = s.git(real, ["config", "--get-regexp", r"^url\..*\.(insteadof|pushinsteadof)$"])
    if code not in (0, 1):
        raise Refused("C7c", "could not read the URL rewrites of {} ({}); nothing was pushed".format(path, err.strip()[-200:]))
    for line in out.splitlines() if code == 0 else []:
        key, _sep, value = line.partition(" ")
        if value and url.startswith(value):
            raise Refused("C7c", "a URL rewrite ({} = {}) applies to {}; remove it, or put the rewritten URL on the allowlist".format(key, value, url))
    code, origin, _ = s.git(real, ["config", "--get", "remote.origin.url"])
    facts["originUrl"] = origin.strip() if not code else None
    # C8: the gate, now and as recorded
    task_id = push["task"]
    ledger = pa.read_json(os.path.join(directory, "ledger.json"), {}) or {}
    task = next((t for t in ledger.get("tasks", []) if isinstance(t, dict) and t.get("id") == task_id), None)
    if not task:
        raise Refused("C8a", "no task {} in this space's ledger".format(task_id))
    # t-192 R2: a repository only a recorded job's worktree holds is gated as that job (pa-gate --job: its worktree,
    # its GATE-<job>.json); read from the gate files alone, so the main job's path runs exactly as before
    job = push_job(directory, task, common)
    tree = pa_hooks.gate_job(task, job) if job else task.get("worktree")
    gname, again = pa_hooks.check_files(job)[2], task_id + (" --job " + job if job else "")
    why = task_settings_refusal(s, tree)
    if why:
        raise Refused("C8a", why)
    now_gate = pa_hooks.evaluate_gate(directory, task, "review", runner=s.gate_git, cache=s.cache, job=job)
    if not now_gate["passed"]:
        failed = ["{} ({})".format(c["check"], c.get("why")) for c in now_gate["checks"] if not c["ok"]]
        raise Refused("C8a", "the full gate for {} does not pass now: {}".format(task_id, "; ".join(failed)))
    gate = pa.read_json(os.path.join(directory, "evidence", task_id, gname))
    if not isinstance(gate, dict):
        raise Refused("C8b", "there is no {} for {}; run pa-gate {}".format(gname, task_id, again))
    facts["gate"] = {"at": gate.get("at"), "phase": gate.get("phase"), "passed": gate.get("passed"), "commit": None, "uncommitted": None}
    if gate.get("phase") != "review":
        raise Refused("C8b", "{} for {} is from phase {}; run pa-gate {} again".format(gname, task_id, gate.get("phase"), again))
    if gate.get("passed") is not True:
        raise Refused("C8b", "{} for {} did not pass".format(gname, task_id))
    pins = [c for c in gate.get("commits") or [] if isinstance(c, dict) and isinstance(c.get("common"), str)
            and os.path.realpath(c["common"]) == common]
    if len(pins) != 1:
        raise Refused("C8c", "{} pins {} for this repository; run pa-gate {} again".format(
            gname, "no commit" if not pins else "{} commits".format(len(pins)), again))
    pin = pins[0]
    facts["gate"].update(commit=pin.get("commit"), uncommitted=pin.get("uncommitted"))
    if pin.get("uncommitted") != 0:
        raise Refused("C8d", "the gate ran with {} uncommitted file(s) in the worktree; commit them and run the gate again".format(pin.get("uncommitted")))
    if plan:
        push.update(commit=pin.get("commit"), taskBranch=pin.get("branch"), gateAt=gate.get("at"))
    commit = push.get("commit")
    if pin.get("commit") != commit:
        raise Refused("C8e", "the gate passed {}, the card says {}".format(short(pin.get("commit")), short(commit)))
    task_branch = push.get("taskBranch")
    facts.update(commit=commit, taskBranch=task_branch)
    if not isinstance(task_branch, str) or branch_problem(task_branch):
        raise Refused("C8f", "the gate ran on no branch in the worktree (a detached HEAD); check out the task branch and run pa-gate {} again".format(task_id))
    tip = s.sha(real, "refs/heads/" + task_branch)
    facts["branchTip"] = tip
    if tip != commit:
        raise Refused("C8f", "commits were added to {} after the gate (gate {}, branch now {}); run pa-gate again and ask for a new card".format(
            task_branch, short(commit), short(tip)))
    live = live_pin(s, tree, common)
    if not live or live["commit"] != commit:
        raise Refused("C8g", "the task's worktree is not at the gated commit now ({}); run pa-gate {} again and ask for a new card".format(
            "not found" if not live else "it is at " + short(live["commit"]), task_id))
    if live["uncommitted"] != 0:
        raise Refused("C8g", "the task's worktree has {} uncommitted change(s) to tracked files now; commit them and run the gate again".format(live["uncommitted"]))
    # C8h (t-052 J5, RL-J2-R2-1): the live gate compared this repository's commit against the push branch; a commit
    # checked on a queued entry's branch merges through pa-queue after that entry, never by its own push card.
    onto = [e.get("onto") for e in now_gate.get("content") or [] if isinstance(e, dict) and isinstance(e.get("common"), str)
            and os.path.realpath(e["common"]) == common]
    if onto != [branch] and not (ontos and len(onto) == 1 and onto[0] in ontos):
        raise Refused("C8h", "the full gate for {} {}; it merges through pa-queue after the task of that branch".format(
            task_id, "did not compare this repository's commit against {}".format(branch) if not onto or not isinstance(onto[0], str)
            else "says its checks compared it against {}".format(pa_hooks.clean(onto[0])) if len(onto) == 1
            else "compared {} commits of this repository".format(len(onto))))
    # C9: the commit itself
    if not isinstance(commit, str) or not HEX.match(commit):
        raise Refused("C9", "{} is not a full commit id".format(commit))
    if s.sha(real, commit) != commit:
        raise Refused("C9", "commit {} is not in {}".format(short(commit), path))
    # C10: local branch
    local = s.sha(real, "refs/heads/" + branch)
    facts["localNow"] = local
    if not local:
        raise Refused("C10a", "{} has no local branch {}".format(path, branch))
    if plan:
        push["localBefore"] = local
    if local == commit:
        merge = False
    elif local != push.get("localBefore"):
        raise Refused("C10a", "local {} moved since the card (was {}, now {}); ask for a new card".format(branch, short(push.get("localBefore")), short(local)))
    elif s.is_ancestor(real, local, commit):
        merge = True
    else:
        raise Refused("C10a", "local {} ({}) is not an ancestor of {}; only a fast-forward is allowed".format(branch, short(local), short(commit)))
    facts["merge"] = merge
    if plan:
        push["merge"] = merge
    if merge:
        code, head, _ = s.git(real, ["symbolic-ref", "-q", "HEAD"])
        head = head.strip() if not code else ""
        if head != "refs/heads/" + branch:
            raise Refused("C10b", "{} is on {}, not {}; check out {} there first".format(
                path, head[len("refs/heads/"):] if head.startswith("refs/heads/") else "a detached HEAD", branch, branch))
        changed = tracked_changes(s, real)
        if changed is None or changed:
            raise Refused("C10b", "{} has {} uncommitted change(s) to tracked files; the local fast-forward needs a clean checkout".format(
                path, "some" if changed is None else len(changed)))
    # C11: the remote branch, read now
    code, out, err = s.git(real, ["ls-remote", url, "refs/heads/" + branch], 30)
    if code:
        raise Refused("C11a", "could not read {} ({}); nothing was pushed".format(url, (err.strip().splitlines() or ["exit {}".format(code)])[-1][-200:]))
    remote = next((l.split()[0] for l in out.splitlines() if l.split()[1:2] == ["refs/heads/" + branch]), None)
    facts["remoteNow"] = remote
    if not remote or not HEX.match(remote):
        raise Refused("C11a", "{} has no branch {}; creating a main branch is not done here".format(url, branch))
    if remote == commit:
        if plan:
            raise Refused("C11b", "nothing to push: {} {} is already at {}".format(url, branch, short(commit)))
        facts.update(commits=[], count=0)
        return "already"
    if plan:
        push["remoteBefore"] = remote
    if remote != push.get("remoteBefore"):
        raise Refused("C11b", "remote {} moved since the card (was {}, now {}); ask for a new card".format(branch, short(push.get("remoteBefore")), short(remote)))
    if s.sha(real, remote) != remote:
        code, _o, err = s.git(real, ["fetch", "--no-tags", url, "refs/heads/" + branch], 60)
        if code or s.sha(real, remote) != remote:
            raise Refused("C11c", "could not fetch {} {} to compare it ({}); nothing was pushed".format(url, branch, err.strip()[-200:]))
    if not s.is_ancestor(real, remote, commit):
        raise Refused("C11c", "remote {} ({}) is not an ancestor of {}; only a fast-forward is allowed, never a force push".format(
            branch, short(remote), short(commit)))
    facts["commits"], facts["count"] = commit_list(s, real, remote, commit, task_branch)
    if facts["count"] > COMMITS_MAX or len(facts["commits"]) != facts["count"]:
        raise Refused("C11d", "{} commits to push ({}..{}); a card lists at most {} commits, and it never pushes a commit it does "
                              "not list: push fewer at a time".format(facts["count"], short(remote), short(commit), COMMITS_MAX))
    labels, batch = queue_labels(s, real, remote, commit, queue_merged(directory, common))
    for c in facts["commits"]:
        if c["sha"] in labels:
            c["queueTask"], c["queueJob"] = labels[c["sha"]]
    if plan:
        push["commits"] = facts["commits"]
        if batch:
            push["batch"] = batch
    return None


def queue_merged(directory, common):
    """queue.json's merged[] records of the repository whose common dir is common, in merge order (pa-queue writes
    them; t-052 J2, J3b). [] when there is no queue, or no record of this repository."""
    data = pa.read_json_safe(os.path.join(directory, "queue.json"))
    repos = data.get("repos") if isinstance(data, dict) else None
    out = []
    for rec in repos.values() if isinstance(repos, dict) else []:
        if isinstance(rec, dict) and isinstance(rec.get("common"), str) and os.path.realpath(rec["common"]) == common:
            out += [m for m in rec.get("merged") or [] if isinstance(m, dict)]
    return out


def queue_labels(s, path, remote, commit, merged):
    """({sha: (task, job)}, batch) for the commits of remote..commit that the queue merged (R20, t-052 J5): in merge
    order, each merged entry whose tip commit holds takes the commits of `<tip> --not <remote> <from>` not taken by
    an entry before it. batch: one {task, job, tip, count[, suiteTree]} per entry that took a commit."""
    labels, batch = {}, []
    if not merged:
        return labels, batch
    code, out, _ = s.git(path, ["rev-list", remote + ".." + commit])
    in_range = set(out.split()) if not code else set()
    for m in merged:
        task, job, tip, frm = m.get("task"), m.get("job"), m.get("tip"), m.get("from")
        if not (isinstance(task, str) and pa.ID.match(task) and isinstance(tip, str) and HEX.match(tip)):
            continue
        job = job if isinstance(job, str) and pa.ID.match(job) else None
        if not s.is_ancestor(path, tip, commit):
            continue
        argv = ["rev-list", tip, "--not", remote] + ([frm] if isinstance(frm, str) and HEX.match(frm) else [])
        code, out, _ = s.git(path, argv)
        mine = [x for x in out.split() if x in in_range and x not in labels] if not code else []
        if not mine:
            continue
        for x in mine:
            labels[x] = (task, job)
        item = {"task": task, "job": job, "tip": tip, "count": len(mine)}
        tree = (m.get("suite") or {}).get("tree") if isinstance(m.get("suite"), dict) else None
        if isinstance(tree, str) and HEX.match(tree):
            item["suiteTree"] = tree
        batch.append(item)
    return labels, batch


REBASED = re.compile(r"^rebase\b.*\(finish\): refs/heads/(\S+) onto ([0-9a-f]{40}|[0-9a-f]{64})$")
# Reflog entries of the task branch. A move onto main: where main stood then is a base point. The branch's own move:
# its own commit (or a merge into it), passed over. Any other entry newer than the newest base point: unknown.
MOVED_ONTO = re.compile(r"^(branch: Created from |reset: moving to |branch: Reset to |(merge|pull)\b[^:]*: Fast-forward$)")
OWN_MOVE = re.compile(r"^(commit( \([a-z ]+\))?: |(merge|pull)\b[^:]*: Merge made |cherry-pick: |revert: |branch: renamed )")


def task_base(s, path, task_branch):
    """(base, points) from the task branch's reflog in path. points: where local main stood each time the branch
    was put onto it (created from it, reset onto it, fast-forwarded to it: the entry's commit; rebased onto it: the
    `onto` commit); base: the newest of them, where the branch's own line starts. (None, []) when the reflog does
    not say: no base point, or an entry newer than the base that is neither a move onto main nor the branch's own."""
    if branch_problem(task_branch):
        return None, []
    code, out, _ = s.git(path, ["reflog", "show", "--format=%H%x1f%gs", "refs/heads/" + task_branch, "--"])
    entries = [l.split("\x1f", 1) for l in out.splitlines() if "\x1f" in l] if not code else []
    points = []
    for sha, subject in entries:  # newest first
        m = REBASED.match(subject)
        if m and m.group(1) == task_branch:
            points.append(m.group(2))
        elif MOVED_ONTO.match(subject) and HEX.match(sha):
            points.append(sha)
        elif not points and not OWN_MOVE.match(subject):
            return None, []
    return (points[0], points) if points else (None, [])


def commit_list(s, path, remote, commit, task_branch):
    """([{sha, subject, author, fromTask}] of remote..commit, newest first, at most COMMITS_MAX; the full count).
    fromTask (spec 6.1) is true for a commit on the task branch's own first-parent line since it was last put onto
    main (task_base) and not reachable from any point where main stood when the branch was put onto it; false for
    any other (on local main before this task, or merged in); null for every commit when task_base cannot say."""
    code, out, _ = s.git(path, ["log", "-n", str(COMMITS_MAX), "--format=%H%x1f%s%x1f%an <%ae>", remote + ".." + commit])
    rows = [l.split("\x1f") for l in out.splitlines() if l.count("\x1f") == 2] if not code else []
    own = None
    base, points = task_base(s, path, task_branch)
    if base and s.is_ancestor(path, base, commit):
        code, mine, _ = s.git(path, ["rev-list", "--first-parent", base + ".." + commit])
        code2, outside, _ = s.git(path, ["rev-list", commit, "--not"] + points)
        own = set(mine.split()) & set(outside.split()) if not code and not code2 else None
    code, count, _ = s.git(path, ["rev-list", "--count", remote + ".." + commit])
    commits = [{"sha": r[0], "subject": pa_hooks.cut(pa_hooks.clean(r[1]), 200), "author": pa_hooks.cut(pa_hooks.clean(r[2]), 120),
                "fromTask": None if own is None else r[0] in own} for r in rows]
    return commits, int(count.strip()) if not code and count.strip().isdigit() else len(commits)


def commands(facts, entry):
    """The argv lists run would start, in order, as the card shows them."""
    path, url, branch, commit = facts["path"], facts["url"], facts["branch"], facts["commit"]
    b = base(path, SHOWN_HOOKS)
    out = []
    if entry.get("prePush"):
        out.append(["git", "-c", "core.hooksPath=" + SHOWN_HOOKS, "clone", "--quiet", "--shared", "--no-checkout", path, SHOWN_CLONE])
        out.append(["git", "-C", SHOWN_CLONE, "-c", "core.hooksPath=" + SHOWN_HOOKS, "checkout", "--quiet", "--detach", commit])
        out.append(fill(entry["prePush"], path=SHOWN_CLONE, repo=path, commit=commit, url=url, branch=branch))
    if facts.get("merge"):
        out.append(b + ["merge", "--ff-only", "--no-edit", commit])
    out.append(b + ["push", "--porcelain", "--no-follow-tags", "--recurse-submodules=no", url, commit + ":refs/heads/" + branch])
    out.append(b + ["ls-remote", url, "refs/heads/" + branch])
    return out


def seen_token(card_id, facts):
    """What the person saw, as one SHA-256: run refuses when it differs (C12)."""
    shas = [c["sha"] for c in facts.get("commits") or []]
    value = [card_id, facts.get("path"), facts.get("url"), facts.get("branch"), facts.get("commit"), facts.get("localNow"),
             facts.get("remoteNow"), (facts.get("allow") or {}).get("sha"), (facts.get("gate") or {}).get("at"), shas]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


# ---- cards ----------------------------------------------------------------------------------------

def card_file(directory, card_id):
    """Where the card is: decisions/, or decisions-archive/ when only there (t-054). Writes go through pa.put_card (under the archive lock, t-131)."""
    return pa.card_file(directory, card_id)


def load_card(directory, space, card_id):
    """The push card (C3), or Refused."""
    if not isinstance(card_id, str) or not pa.ID.match(card_id):
        raise Refused("C3", "no decision card {}".format(card_id))
    card = pa.read_json(card_file(directory, card_id))
    if not isinstance(card, dict):
        raise Refused("C3", "no decision card {}".format(card_id))
    push = card.get("push")
    if not isinstance(push, dict):
        raise Refused("C3", "{} is not a push card".format(card_id))
    if push.get("version") != 1:
        raise Refused("C3", "{} has push version {}; this pa-push-main knows version 1".format(card_id, push.get("version")))
    if push.get("space") != space:
        raise Refused("C3", "{} belongs to space {}, not {}".format(card_id, push.get("space"), space))
    for key in ("path", "url", "branch", "task", "commit"):
        if not isinstance(push.get(key), str):
            raise Refused("C3", "{} is not a push card (its {} is missing)".format(card_id, key))
    if not pa.ID.match(push["task"]) or not os.path.isabs(push["path"]):
        raise Refused("C3", "{} is not a push card (bad task or path)".format(card_id))
    return card


@contextlib.contextmanager
def push_lock(directory, wait=5):
    """<space>/.push.lock (C2): one plan or run at a time in a space."""
    with open(os.path.join(directory, ".push.lock"), "a") as f:
        end = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > end:
                    raise Refused("C2", "another push for this space is running; try again in a minute")
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def blank_facts():
    return {k: None for k in FACT_KEYS}


def check_card(directory, space, card_id, env, ancs):
    """pa-push-main check: the facts, the first failing check and the seen token. Reads only: no lock, no write
    in the space (git may fetch the remote branch into FETCH_HEAD to compare it)."""
    if is_queue_card(directory, card_id):
        return check_queue_card(directory, space, card_id, env, ancs)
    why = caller_refusal(env, ancs)
    report = {"card": card_id, "ok": False, "refusal": None, "caller": {"ok": why is None, "why": why, "ancestors": ancestor_names(ancs)},
              "status": None, "done": False, "result": None, "facts": blank_facts(), "seen": None}
    s = Session(deadline=time.monotonic() + 55)
    try:
        card = load_card(directory, space, card_id)
        push = card["push"]
        report.update(status=card.get("status"), done=bool(push.get("done")), result=push.get("result"))
        if push.get("done"):
            r = push.get("result") or {}
            raise Refused("C4", "{} was already pushed at {} ({}..{}); nothing to do".format(card_id, r.get("at"), short(r.get("from")), short(r.get("to"))))
        if card.get("status") != "open":
            raise Refused("C4", "{} is {}; nothing was pushed".format(card_id, card.get("status")))
        gather(s, directory, space, push, report["facts"])
        report["facts"]["commands"] = commands(report["facts"], report["facts"]["allow"]["entry"])
        report["ok"] = True
        report["seen"] = seen_token(card_id, report["facts"])
    except Refused as r:
        report["refusal"] = {"check": r.check, "message": r.message}
    finally:
        s.close()
        report["facts"]["checkedAt"] = pa.now()
    return report


def card_context(push, facts, entry, replaced=None):
    """The card's context, written by the tool from what it read (never by an agent). replaced: the open card this
    one replaces (t-052 J5)."""
    branch, c7 = push["branch"], short(push["commit"])
    lines = ["Repository: {}".format(push["path"]),
             "Push to: {} branch {} (on your push allowlist{}). The remote {} is at {} now.".format(
                 push["url"], branch, ": " + pa_hooks.clean(entry["note"]) if entry.get("note") else "", branch, short(push["remoteBefore"])),
             "Local {} is at {}; {}.".format(branch, short(push["localBefore"]),
                                             "this card fast-forwards it to {} first".format(c7) if push["merge"] else "it is the gated commit"),
             "Gated commit: {} on {} (pa-gate review passed at {}).".format(c7, push["taskBranch"], push["gateAt"])]
    if replaced:
        n = len(replaced["push"].get("commits") or [])
        lines.append("Replaces {}: this card also pushes its {} commit{}.".format(replaced["id"], n, "" if n == 1 else "s"))
    if push.get("batch"):
        lines.append("Merged into local {} by the queue ({}), in merge order:".format(branch, len(push["batch"])))
        for b in push["batch"]:
            lines.append("- {}{}: {} commit{}, tip {}{}".format(b["task"], " job " + b["job"] if b.get("job") else "", b["count"],
                                                              "" if b["count"] == 1 else "s", short(b["tip"]),
                                                              ", suite passed on tree " + short(b["suiteTree"]) if b.get("suiteTree") else ""))
    lines.append("Commits to push ({}):".format(facts["count"]))
    queued = any(c.get("queueTask") for c in push["commits"])

    def label(c):
        if c.get("queueTask"):
            return " (from {}{})".format(c["queueTask"], " job " + c["queueJob"] if c.get("queueJob") else "")
        if c["fromTask"] is False:
            return " (not from a queued task)" if queued else " (not from this task)"
        return ""
    for c in push["commits"][:CONTEXT_COMMITS]:
        lines.append("- {} {} ({}){}".format(short(c["sha"]), c["subject"], c["author"], label(c)))
    if facts["count"] > CONTEXT_COMMITS:
        lines.append("- and {} more".format(facts["count"] - CONTEXT_COMMITS))
    if any(c["fromTask"] is None and not c.get("queueTask") for c in push["commits"]):
        lines.append("Note: which of these commits are this task's cannot be told: the record of where {} started "
                     "(its reflog) is missing; read the list yourself.".format(push["taskBranch"]))
    if facts.get("originUrl") != push["url"]:
        lines.append("Note: origin of {} is {}, not the allowlist URL; the push goes to the allowlist URL.".format(
            push["path"], pa_hooks.clean(facts.get("originUrl") or "not set")))
    lines.append("Will run: " + "; ".join(" ".join(a) for a in commands(facts, entry)))
    lines.append("The panel checks every fact again when you look, and the push runs only when you answer Push.")
    return pa_hooks.cut("\n".join(lines), pa_hooks.CONTEXT_MAX)


def next_card_id(directory, task_id):
    n = 1
    while os.path.exists(card_file(directory, "d-push-{}-{}".format(task_id, n))):
        n += 1
    return "d-push-{}-{}".format(task_id, n)


def open_push_cards(directory, path):
    """[(id, card)] of the open push cards (any task) for the main checkout path, by id."""
    folder = os.path.join(directory, "decisions")
    out = []
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if not (name.startswith("d-push-") and name.endswith(".json")):
            continue
        card = pa.read_json(os.path.join(folder, name))
        push = card.get("push") if isinstance(card, dict) else None
        if isinstance(push, dict) and card.get("status") == "open" and \
                isinstance(push.get("path"), str) and os.path.realpath(push["path"]) == path:
            out.append((name[:-5], card))
    return out


def contains(s, old, push):
    """True when the new card's push holds every commit the open card old stands for (t-052 J5, P3): same remote and
    branch, its commit a proper ancestor of the new commit, and every sha it lists listed by the new card."""
    op = old.get("push") or {}
    shas = [c.get("sha") for c in op.get("commits") or [] if isinstance(c, dict)]
    new = {c["sha"] for c in push.get("commits") or []}
    return bool(op.get("url") == push["url"] and op.get("branch") == push["branch"] and isinstance(op.get("commit"), str)
                and HEX.match(op["commit"]) and op["commit"] != push["commit"] and not op.get("done")
                and s.is_ancestor(push["path"], op["commit"], push["commit"]) and shas and all(x in new for x in shas))


def space_of(args):
    directory, agent = pa.space_for_command(args.space)
    return directory, pa.local_id(agent.get("space") or os.path.basename(directory))


def cmd_plan(args):
    """Opens a push card for a task that passed its full gate, for a repository on the allowlist. The tool writes
    every fact on the card. Workers are refused (a task record above this folder)."""
    record = pa_hooks.find_task(os.getcwd())
    if record:
        print("pa-push-main: refused: workers do not open push cards (this folder is the worktree of task {}); "
              "say in your report that the work is ready to release".format(record.get("task")), file=sys.stderr)
        return 1
    directory, space = space_of(args)
    task_id = pa.ident(args.task, "task id")
    path = os.path.realpath(args.repo)
    try:
        with push_lock(directory):
            card, replaced = plan_card(directory, space, task_id, path)
    except Refused as r:
        print("pa-push-main: refused: " + r.message, file=sys.stderr)
        return 1
    print("{} opened: {}.{} The person answers it in the panel.".format(
        card["id"], card["question"], " It replaces {}, now withdrawn.".format(replaced) if replaced else ""))
    return 0


def plan_card(directory, space, task_id, path):
    """The push card for task_id and the main checkout path, written; the caller holds the push lock (cmd_plan, and
    pa-queue's merge in the same hold as its merges). An open push card of the same repository whose commits the new
    one lists is withdrawn as replaced, after every check of the new card passed and the new card is written (t-052
    J5); any other open card of it refuses. (card, replaced id or None), or Refused with nothing written."""
    push = {"version": 1, "space": space, "task": task_id, "path": path}
    facts = blank_facts()
    s = Session(deadline=time.monotonic() + 120)
    try:
        opened = open_push_cards(directory, path)
        if len(opened) > 1:
            raise Refused("plan", "{} open push cards exist for {} ({}); withdraw all but one first, with pa-decide --withdraw".format(
                len(opened), path, ", ".join(i for i, _c in opened)))
        gather(s, directory, space, push, facts, plan=True)
        old = None
        for other, card in opened:
            if not contains(s, card, push):
                raise Refused("plan", "an open push card {} exists for {} and {}; withdraw {} first, with pa-decide --withdraw".format(
                    other, card["push"].get("task"), path, other))
            old = dict(card, id=other)
        entry = facts["allow"]["entry"]
        card_id = next_card_id(directory, task_id)
        push.update(done=False, result=None, attempts=[])
        if old:
            push["replaces"] = old["id"]
        name, count = os.path.basename(path), facts["count"]
        card = {"id": card_id, "task": task_id, "kind": "push",
                "question": "Push {} {} to {} ({} commit{})?".format(name, push["branch"], push["url"], count, "" if count == 1 else "s"),
                "context": card_context(push, facts, entry, old),
                "options": [{"label": "Push", "detail": "Fast-forward local {} to {} if needed, then push {}..{}. No force.".format(
                                push["branch"], short(push["commit"]), short(push["remoteBefore"]), short(push["commit"]))},
                            {"label": "Not now", "detail": "Nothing is pushed. The card closes."}],
                "evidence": ["GATE.json", "REVIEW.md"], "recommendation": "Push", "status": "open", "answer": None, "note": None,
                "created": pa.now(), "answered": None, "push": push}
        pa.put_card(directory, card_id, card)
        by = "agent" if pa_hooks.by_an_agent() else "person"
        pa_hooks.append_journal(directory, {"by": by, "trigger": "push-plan", "task": task_id,
                                            "decision": card_id, "why": "push card opened: {} {} {}..{} ({} commits){}".format(
                                                push["url"], push["branch"], short(push["remoteBefore"]), short(push["commit"]), count,
                                                "; replaces " + old["id"] if old else ""),
                                            "push": journal_push(push, push["remoteBefore"], push["merge"])})
        if old:
            note = "replaced by {}: {}, which holds its commits".format(
                card_id, "local {} moved to {}".format(push["branch"], short(push["commit"])) if not push["merge"]
                else "it pushes {}".format(short(push["commit"])))
            old.update(status="withdrawn", answer=None, note=note, answered=pa.now(), answeredBy="pa-push-main: replaced",
                       replacedBy=card_id)
            save_card(directory, old)
            pa_hooks.append_journal(directory, {"by": by, "trigger": "push-plan", "task": old.get("task"), "decision": old["id"],
                                                "why": "withdrawn: " + note,
                                                "push": journal_push(old["push"], old["push"].get("remoteBefore"), False)})
        return card, old["id"] if old else None
    finally:
        s.close()


def journal_push(push, frm, merged):
    return {"path": push.get("path"), "url": push.get("url"), "branch": push.get("branch"), "from": frm, "to": push.get("commit"),
            "merged": merged, "allowSha": push.get("allowSha"), "allowMtime": push.get("allowMtime"), "gateAt": push.get("gateAt")}


def cmd_check(args):
    directory, space = space_of(args)
    report = check_card(directory, space, args.card, os.environ, ancestors())
    if args.json:
        print(json.dumps(report, indent=2))
    elif report.get("kind") == "queue":
        print("{}: {}".format(args.card, "ready" if report["ok"] else "refused ({}): {}".format(report["refusal"]["check"], report["refusal"]["message"])))
        for o in report["options"]:
            p = o["push"]
            print("  {}: merges now {}{}; {}".format(o["label"], ", ".join(o["mergeNow"]) or "nothing",
                                                    " (waits: {}: {})".format(o["waits"]["task"], o["waits"]["why"]) if o["waits"] else "",
                                                    "pushes {} commit(s) to {}".format(p["count"], short(p["commit"])) if p["ok"]
                                                    else "not pushed now: " + (p["refusal"] or {}).get("message", "")))
        print("caller: {}".format("this caller may answer" if report["caller"]["ok"] else "refused: " + report["caller"]["why"]))
        if report["seen"]:
            print("seen: " + report["seen"])
    else:
        f = report["facts"]
        print("{}: {}".format(args.card, "ready: Push would push {} {}..{} to {}".format(f["branch"], short(f["remoteNow"]), short(f["commit"]), f["url"])
                              if report["ok"] else "refused ({}): {}".format(report["refusal"]["check"], report["refusal"]["message"])))
        print("caller: {}".format("this caller may push" if report["caller"]["ok"] else "refused: " + report["caller"]["why"]))
        if report["seen"]:
            print("seen: " + report["seen"])
    return 0 if report["ok"] else 1


# ---- run (C0-C13, then the act) ------------------------------------------------------------------

RUN_MSG = ("pa-push-main run is the person's command; it runs when they answer a push card in the Project panel. "
           "An agent opens the card with pa-push-main plan and stops. ({})")
ANSWERS = ("Push", "Not now", "Other")  # Other: the person's own words, in --note; it pushes nothing
BUDGET = 540  # seconds for one run (the panel's exec allows 600)
PUSH_RESERVE = 150  # seconds that must be left before the push starts
ATTEMPTS_KEPT = 20
TAIL = 15  # lines of output kept


def outcome(ok, done=False, answer=None, refusal=None, result=None, message=""):
    return {"ok": ok, "done": done, "answer": answer, "refusal": refusal, "result": result, "message": message}


def refused(r, answer=None):
    return outcome(False, answer=answer, refusal={"check": r.check, "message": r.message}, message="pa-push-main: refused: " + r.message)


def tail(text, lines=TAIL):
    return "\n".join([l for l in str(text or "").splitlines() if l.strip()][-lines:])


def run_push(space, card_id, answer, note, seen, env, ancs, deadline=None):
    """pa-push-main run, as (exit code, outcome). env and ancs (ancestors()) decide C0; there is no other way past it.
    deadline: time.monotonic() by which the run must end (default BUDGET from now)."""
    deadline = deadline or time.monotonic() + BUDGET
    why = caller_refusal(env, ancs)
    if why:
        return 1, refused(Refused("C0", RUN_MSG.format(why)), answer)
    if not space:
        return 1, refused(Refused("C1", "pa-push-main run needs --space <id>"), answer)
    space = pa.local_id(space)
    directory = os.path.join(pa.ROOT, "spaces", space) if pa.ID.match(space) else None
    if not directory or not os.path.isfile(os.path.join(directory, "agent.json")):
        return 1, refused(Refused("C1", "no Project Agent state for space {}".format(space)), answer)
    if is_queue_card(directory, card_id):  # t-140 R3: the queue lock, then the push lock
        try:
            return run_queue(directory, space, card_id, answer, note, seen, deadline)
        except Refused as r:
            return 1, refused(r, answer)
    try:
        with push_lock(directory):
            return run_locked(directory, space, card_id, answer, note, seen, deadline)
    except Refused as r:
        return 1, refused(r, answer)


def run_locked(directory, space, card_id, answer, note, seen, deadline):
    card = load_card(directory, space, card_id)  # C3 (Refused goes to run_push)
    push = card["push"]
    if push.get("done"):
        r = push.get("result") or {}
        return 0, outcome(True, True, card.get("answer"), result=r, message="{} was already pushed at {} ({}..{}); nothing to do.".format(
            card_id, r.get("at"), short(r.get("from")), short(r.get("to"))))
    if card.get("status") != "open":
        raise Refused("C4", "{} is {}; nothing was pushed".format(card_id, card.get("status")))
    if answer not in ANSWERS:
        raise Refused("C5", "--answer must be one of: " + ", ".join(ANSWERS))
    if answer == "Not now":
        return answer_not_now(directory, card, note)
    if answer == "Other":
        return answer_other(directory, card, note)
    if answer != "Push":  # only Push reaches the push path
        raise Refused("C5", "--answer must be one of: " + ", ".join(ANSWERS))
    s = Session(deadline)
    facts = blank_facts()
    try:
        state = gather(s, directory, space, push, facts)
        if state == "already":
            return already_there(s, directory, card, note, facts)
        if not seen:
            raise Refused("C12", "--seen is required (the token of the facts the person looked at)")
        if seen != seen_token(card_id, facts):
            raise Refused("C12", "the facts changed since you looked; look at the card again")
        entry = facts["allow"]["entry"]
        if entry.get("prePush"):
            pre_push(s, entry, facts)
        if s.left() < PUSH_RESERVE:
            raise Refused("deadline", "ran out of time before pushing; nothing was pushed")
        return act(s, directory, card, note, facts)
    except Refused as r:
        record_attempt(directory, card, r, facts)
        return 1, refused(r, "Push")
    finally:
        s.close()


def pre_push(s, entry, facts):
    """C13: the entry's prePush, in a pristine temporary clone of the commit, before anything changes."""
    path, commit = facts["path"], facts["commit"]
    clone = tempfile.mkdtemp(prefix="pa-push-clone-")
    try:
        code, out, err = git_call(["git", "-c", "core.hooksPath=" + s.hooks, "clone", "--quiet", "--shared", "--no-checkout", path, clone],
                                  min(60, s.left()))
        if not code:
            code, out, err = git_call(["git", "-C", clone, "-c", "core.hooksPath=" + s.hooks, "checkout", "--quiet", "--detach", commit],
                                      min(60, s.left()))
        if code:
            raise Refused("C13", "could not make the temporary clone for the prePush check: {}; nothing was pushed".format(tail(err, 3)))
        argv = fill(entry["prePush"], path=clone, repo=path, commit=commit, url=facts["url"], branch=facts["branch"])
        code, out, err = git_call(argv, min(180, s.left()), cwd=clone)
        if code:
            raise Refused("C13", "the allowlist's prePush check failed (exit {}):\n{}".format(code, tail(out + "\n" + err)))
    finally:
        shutil.rmtree(clone, ignore_errors=True)


def save_card(directory, card):
    pa.put_card(directory, card["id"], card)


def record_attempt(directory, card, r, facts):
    """A refusal or failure after C5: kept on the card (which stays open) and journaled."""
    push = card["push"]
    push["attempts"] = (list(push.get("attempts") or []) + [{"at": pa.now(), "check": r.check, "message": r.message}])[-ATTEMPTS_KEPT:]
    save_card(directory, card)
    pa_hooks.append_journal(directory, {"by": "person", "trigger": "panel", "task": card.get("task"), "decision": card["id"],
                                        "why": "push refused: " + r.message, "push": journal_push(push, facts.get("remoteNow"), facts.get("merge"))})


def answered(card, label, note):
    card.update(status="answered", answer=label, note=(note or "").strip() or None, answered=pa.now(), answeredBy="person, in the panel")


def answer_not_now(directory, card, note):
    """C5 Not now: the card closes, nothing is read or pushed."""
    push = card["push"]
    answered(card, "Not now", note)
    save_card(directory, card)
    pa_hooks.append_journal(directory, {"by": "person", "trigger": "panel", "task": card.get("task"), "decision": card["id"],
                                        "why": "answered Not now", "push": journal_push(push, push.get("remoteBefore"), False)})
    return 0, outcome(True, False, "Not now", message="{} answered Not now; nothing was pushed.".format(card["id"]))


def answer_other(directory, card, note):
    """C5 Other: the person's own words close the card as Not now does; nothing is read or pushed, --seen is not looked at."""
    words = (note or "").strip()
    if not words:
        raise Refused("C5", "an Other answer needs --note with the person's own words")
    if len(words) > pa.OTHER_MAX:
        raise Refused("C5", pa.OTHER_TOO_LONG)
    push = card["push"]
    answered(card, pa.OTHER, words)
    card["other"] = True
    save_card(directory, card)
    pa_hooks.append_journal(directory, {"by": "person", "trigger": "panel", "task": card.get("task"), "decision": card["id"],
                                        "why": "answered Other: " + pa_hooks.cut(pa_hooks.clean(words), 200),
                                        "push": journal_push(push, push.get("remoteBefore"), False)})
    return 0, outcome(True, False, pa.OTHER, message="{} answered Other; nothing was pushed.".format(card["id"]))


def local_fast_forward(s, facts):
    """git merge --ff-only of the commit in the main checkout (C10 made sure it is a clean fast-forward)."""
    path, branch, commit = facts["path"], facts["branch"], facts["commit"]
    code, out, err = s.git(path, ["merge", "--ff-only", "--no-edit", commit], 30)
    if code or s.sha(path, "refs/heads/" + branch) != commit:
        raise Refused("merge", "failed: local fast-forward did not run: {}; nothing was pushed".format(tail(err + "\n" + out, 5) or "exit {}".format(code)))
    return out


def finish(s, directory, card, note, facts, how, merged, output):
    """Success: the card is answered and done, the journal has the line, and remote-tracking refs are refreshed."""
    push = card["push"]
    result = {"at": pa.now(), "from": push.get("remoteBefore"), "to": facts["commit"], "url": facts["url"], "branch": facts["branch"],
              "merged": merged, "how": how, "output": tail(output)}
    push.update(done=True, result=result)
    answered(card, "Push", note)
    save_card(directory, card)
    if how == "pushed":
        why = "pushed {} {} {}..{} ({} commits)".format(facts["url"], facts["branch"], short(result["from"]), short(result["to"]), facts.get("count"))
    else:
        why = "already on the remote: {} {} is at {}".format(facts["url"], facts["branch"], short(result["to"]))
    pa_hooks.append_journal(directory, {"by": "person", "trigger": "panel", "task": card.get("task"), "decision": card["id"],
                                        "why": why, "push": journal_push(push, result["from"], merged)})
    refresh_tracking(s, facts)
    return 0, outcome(True, True, "Push", result=result, message="{}: {}{}.".format(
        card["id"], why, "; local {} fast-forwarded".format(facts["branch"]) if merged else ""))


def refresh_tracking(s, facts):
    """Courtesy, failures ignored: fetch the branch for each remote whose URL is the allowlist URL, so that
    refs/remotes/<name>/<branch> shows the push."""
    code, out, _ = s.git(facts["path"], ["config", "--get-regexp", r"^remote\..*\.url$"])
    for line in out.splitlines() if not code else []:
        key, _sep, value = line.partition(" ")
        name = key[len("remote."):-len(".url")]
        if value.strip() == facts["url"] and name and not name.startswith("-"):
            s.git(facts["path"], ["fetch", "--no-tags", name, facts["branch"]], 30)


def already_there(s, directory, card, note, facts):
    """C11b: the remote branch is at the commit already (a push by hand, or a run killed after its push)."""
    output = local_fast_forward(s, facts) if facts.get("merge") else ""
    return finish(s, directory, card, note, facts, "already on the remote", bool(facts.get("merge")), output)


def act(s, directory, card, note, facts):
    """Every check passed: the local fast-forward if needed, the push of exactly the commit, and a read back."""
    path, url, branch, commit = facts["path"], facts["url"], facts["branch"], facts["commit"]
    merged = bool(facts.get("merge"))
    output = local_fast_forward(s, facts) if merged else ""
    code, out, err = s.git(path, ["push", "--porcelain", "--no-follow-tags", "--recurse-submodules=no", url, commit + ":refs/heads/" + branch], 120)
    output += out + err
    if code:
        _c, now_remote, _e = s.git(path, ["ls-remote", url, "refs/heads/" + branch], 30)
        raise Refused("push", "failed: git push was refused: {}. Local {} is at {}{}; the remote {} is at {}.".format(
            tail(err + "\n" + out, 5) or "exit {}".format(code), branch, short(s.sha(path, "refs/heads/" + branch)),
            ", fast-forwarded by this run" if merged else "", branch, short((now_remote.split() or [""])[0])))
    code, now_remote, err = s.git(path, ["ls-remote", url, "refs/heads/" + branch], 30)
    remote = next((l.split()[0] for l in now_remote.splitlines() if l.split()[1:2] == ["refs/heads/" + branch]), None)
    if code or remote != commit:
        raise Refused("verify", "failed: the push reported success but {} {} is {}, not {}; look at the remote".format(
            url, branch, short(remote) if remote else "unreadable", short(commit)))
    return finish(s, directory, card, note, facts, "pushed", merged, output)


def read_remote(s, path, url, branch):
    """The remote branch's commit, read now with ls-remote, or None."""
    code, out, _err = s.git(path, ["ls-remote", url, "refs/heads/" + branch], 30)
    remote = next((l.split()[0] for l in out.splitlines() if l.split()[1:2] == ["refs/heads/" + branch]), None) if not code else None
    return remote if remote and HEX.match(remote) else None


def push_exact(s, facts):
    """The push of exactly facts' commit to the allowlist URL and branch (--porcelain, no force, no tags), then a read
    back: ("pushed", output) or ("unknown", output); a refused push raises Refused("push") with the remote read again.
    Never a local fast-forward: the queue merged local main already (t-140 R3 (7))."""
    path, url, branch, commit = facts["path"], facts["url"], facts["branch"], facts["commit"]
    code, out, err = s.git(path, ["push", "--porcelain", "--no-follow-tags", "--recurse-submodules=no", url, commit + ":refs/heads/" + branch], 120)
    if code:
        raise Refused("push", "failed: git push was refused: {}. The remote {} is at {}.".format(
            tail(err + "\n" + out, 5) or "exit {}".format(code), branch, short(read_remote(s, path, url, branch))))
    remote = read_remote(s, path, url, branch)
    if remote != commit:
        return "unknown", out + err, "the push reported success but {} {} is {}, not {}; look at the remote".format(
            url, branch, short(remote) if remote else "unreadable", short(commit))
    return "pushed", out + err, "pushed {} {} {}..{} ({} commits)".format(url, branch, short(facts.get("remoteNow")), short(commit), facts.get("count"))


# ---- t-140 Part A: a queue card that also pushes (R1-R9) ---------------------------------------------------

QUEUE_PUSH_SUFFIX = " and push"
QUEUE_CONTEXT = "Your answer also pushes the merged commits; the panel shows what it would push, read now."


def tool_installed():
    return shutil.which("pa-push-main") is not None


def queue_card_push(directory, repo):
    """For pa-queue card (R1): where pushing is set up for the main checkout repo in this space (the allowlist valid
    and on, exactly one enabled entry for it and this space, and the push tool on PATH), the card's push fields; else
    None, and the card is today's. The fields are for display and routing only: check and run read everything again."""
    data, problem, _sha, _mtime = load_allow()
    if problem or not isinstance(data, dict) or data.get("enabled") is not True or not tool_installed():
        return None
    agent = pa.read_json_safe(os.path.join(directory, "agent.json")) or {}
    space = pa.local_id(agent.get("space") or os.path.basename(directory))
    real = os.path.realpath(repo)
    mine = [e for e in data["repos"] if os.path.realpath(e["path"]) == real and e["space"] == space]
    if len(mine) != 1 or mine[0].get("enabled") is not True:
        return None
    e = mine[0]
    return {"fields": {"push": {"version": 1, "path": real, "url": e["url"], "branch": e["branch"]}},
            "suffix": QUEUE_PUSH_SUFFIX, "contextLine": QUEUE_CONTEXT, "url": e["url"]}


def is_queue_card(directory, card_id):
    if not isinstance(card_id, str) or not pa.ID.match(card_id):
        return False
    card = pa.read_json_safe(card_file(directory, card_id))
    return isinstance(card, dict) and card.get("kind") == "queue" and not isinstance(card.get("push"), dict)


def pushing_queue(card):
    """True for a queue card that pushes (its queue has a push object): pa-decide and the panel route it here."""
    q = card.get("queue") if isinstance(card, dict) else None
    return isinstance(card, dict) and card.get("kind") == "queue" and isinstance(q, dict) and isinstance(q.get("push"), dict)


def load_queue_card(directory, card_id):
    """The pushing queue card (C3), or Refused."""
    card = pa.read_json_safe(card_file(directory, card_id))
    if not isinstance(card, dict) or card.get("kind") != "queue" or not isinstance(card.get("queue"), dict):
        raise Refused("C3", "{} is not a queue card".format(card_id))
    q = card["queue"]
    if not isinstance(q.get("push"), dict):
        raise Refused("C3", "{} is not a pushing queue card (it was made where pushing was not set up); it is answered as "
                            "a queue card".format(card_id))
    if q["push"].get("version") != 1:
        raise Refused("C3", "{} has queue push version {}; this pa-push-main knows version 1".format(card_id, q["push"].get("version")))
    if not isinstance(q.get("repo"), str) or not os.path.isabs(q["repo"]) or not isinstance(q.get("entries"), list):
        raise Refused("C3", "{} is not a pushing queue card (its repo or entries are missing)".format(card_id))
    return card


def merge_options(card):
    return [o for o in card.get("options") or [] if isinstance(o, dict) and isinstance(o.get("count"), int)
            and o["count"] > 0 and isinstance(o.get("label"), str)]


def dry_merge(Q, ctx, rec, card, option, now_at):
    """R2: the longest front prefix of the option's entries that would merge now, each read as if the ones ahead of it
    had merged (merge_check with the tip and branch of the entry ahead), and the first that would not, with why.
    Writes nothing: each entry is probed on a copy with the approval the answer would record."""
    suites, reviews = pa_hooks.suite_runs(ctx.directory), pa_hooks.review_runs(ctx.directory)
    real = ctx.tip(rec["main"])
    prev_tip, prev_branch, merged = real, None, []
    listed = [x for x in card["queue"]["entries"] if isinstance(x, dict)][:option["count"]]
    for k, x in enumerate(listed):
        e, why = Q.listed_entry(rec, x)
        if e is None:
            return merged, {"task": x.get("task"), "why": why}
        if rec["entries"].index(e) != k:
            ahead = rec["entries"][k]["task"] if k < len(rec["entries"]) else "?"
            return merged, {"task": e["task"], "why": "an entry ahead of it ({}) is not on this card; it merges first".format(ahead)}
        probe = dict(e, approval=Q.approval_for(card.get("id"), dict(card, answered=now_at, answer=option["label"]), e))
        why, _void, _suite = Q.merge_check(ctx, rec, probe, prev_tip, suites, reviews, onto=prev_branch, expected=real)
        if why:
            return merged, {"task": e["task"], "why": why}
        merged.append(e)
        prev_tip, prev_branch = e["tip"], e["branch"]
    return merged, None


def queue_facts(s, Q, ctx, data, rec, directory, space, card):
    """The facts of a pushing queue card, read now (R2): {facts, options, seen}; Refused when the card's queue cannot
    be read. Shared by check and run, so the token the person saw is the one the run compares."""
    q, repo = card["queue"], ctx.repo
    now_at = pa.now()
    adata, problem, asha, amtime = load_allow()
    entry = None
    if adata and not problem:
        mine = [e for e in adata["repos"] if os.path.realpath(e["path"]) == repo and e["space"] == space]
        entry = mine[0] if len(mine) == 1 else None
    url, branch = (entry or q["push"]).get("url"), (entry or q["push"]).get("branch")
    local = ctx.tip(rec["main"])
    remote = read_remote(s, repo, url, branch) if isinstance(url, str) and isinstance(branch, str) and not branch_problem(branch) else None
    facts = {"path": repo, "url": url, "branch": branch, "localNow": local, "remoteNow": remote, "main": rec["main"],
             "allow": {"file": allow_path(), "sha": asha, "mtime": amtime, "entry": entry}, "checkedAt": None}
    extra = []
    if remote and local and remote != local and s.sha(repo, remote) == remote and s.is_ancestor(repo, remote, local):
        code, out, _ = s.git(repo, ["rev-list", remote + ".." + local])
        extra = out.split() if not code else []
    options = []
    for o in merge_options(card):
        merged, waits = dry_merge(Q, ctx, rec, card, o, now_at)
        listed = [x for x in q["entries"] if isinstance(x, dict)][:o["count"]]
        item = {"label": o["label"], "count": o["count"], "entries": [{"task": x.get("task"), "job": x.get("job"), "instance": x.get("instance"),
                                                                        "tip": x.get("tip")} for x in listed],
                "mergeNow": [e["task"] for e in merged], "waits": waits, "extra": extra,
                "push": {"ok": False, "refusal": None, "commit": None, "commits": [], "count": 0}}
        if merged:
            push = {"version": 1, "space": space, "task": merged[-1]["task"], "path": repo}
            pf = blank_facts()
            ahead = {e["branch"] for e in merged[:-1]}
            try:
                gather(s, directory, space, push, pf, plan=True, ontos=ahead)
                if extra:
                    raise Refused("R6", extra_message(rec["main"], len(extra)))
                item["push"].update(ok=True, commit=pf["commit"], commits=pf["commits"], count=pf["count"])
            except Refused as r:
                item["push"].update(refusal={"check": r.check, "message": r.message}, commit=pf.get("commit"),
                                    commits=pf.get("commits") or [], count=pf.get("count") or 0)
        else:
            item["push"]["refusal"] = {"check": "none", "message": "nothing would merge now, so nothing would be pushed"}
        options.append(item)
    value = [card.get("id"), repo, url, branch, asha, local, remote,
             [[o["label"], [[x["task"], x["job"], x["instance"], x["tip"]] for x in o["entries"]], o["mergeNow"],
               [c.get("sha") for c in o["push"]["commits"]], (o["push"]["refusal"] or {}).get("check")] for o in options]]
    return {"facts": facts, "options": options, "seen": hashlib.sha256(json.dumps(value).encode()).hexdigest()}


def extra_message(main, n):
    return "local {} has commits that are not from this card: {}; a push card lists them".format(main, n)


def queue_context(directory, card):
    """(Q, ctx, data, rec) for the card's repository; the caller closes ctx. sync_main runs in memory only."""
    Q = queue_tool()
    ctx = Q.Ctx(directory, card["queue"]["repo"])
    try:
        data = Q.load_queue(directory)
        rec = data["repos"].get(ctx.repo)
        if not isinstance(rec, dict) or rec.get("common") != card["queue"].get("common"):
            raise Refused("C3", "the queue of {} is not the one this card was made for".format(ctx.repo))
        for k in ("planned", "entries", "left", "merged"):
            if not isinstance(rec.get(k), list):
                rec[k] = []
        Q.sync_main(ctx, rec)
    except Q.Refused as r:
        ctx.close()
        raise Refused("C10a", str(r))
    except Refused:
        ctx.close()
        raise
    return Q, ctx, data, rec


def check_queue_card(directory, space, card_id, env, ancs):
    """pa-push-main check of a pushing queue card (R2): read only, no lock, nothing written in the space."""
    why = caller_refusal(env, ancs)
    report = {"card": card_id, "kind": "queue", "ok": False, "refusal": None,
              "caller": {"ok": why is None, "why": why, "ancestors": ancestor_names(ancs)},
              "status": None, "done": False, "result": None, "facts": {}, "options": [], "seen": None}
    s = Session(deadline=time.monotonic() + 55)
    ctx = None
    try:
        card = load_queue_card(directory, card_id)
        q = card["queue"]
        report.update(status=card.get("status"), result=q["push"].get("result"))
        if card.get("status") != "open" or q.get("applied"):
            raise Refused("C4", "{} is {}; nothing to do".format(card_id, card.get("status") if card.get("status") != "open" else "applied"))
        Q, ctx, data, rec = queue_context(directory, card)
        got = queue_facts(s, Q, ctx, data, rec, directory, space, card)
        report.update(facts=got["facts"], options=got["options"], seen=got["seen"], ok=True)
    except Refused as r:
        report["refusal"] = {"check": r.check, "message": r.message}
    except pa_hooks.ContentError as e:
        report["refusal"] = {"check": "C10a", "message": str(e)}
    finally:
        if ctx is not None:
            ctx.close()
        s.close()
        report["facts"]["checkedAt"] = pa.now()
    return report


def run_queue(directory, space, card_id, answer, note, seen, deadline):
    """R3: the queue lock, then the push lock (pa-queue merge's order), then the card's answer."""
    Q = queue_tool()
    hold = Q.queue_lock(directory)
    try:
        hold.__enter__()
    except Q.Refused:
        raise Refused("C2", "another queue command is running; try again")
    try:
        with push_lock(directory):
            return run_queue_locked(Q, directory, space, card_id, answer, note, seen, deadline)
    finally:
        hold.__exit__(None, None, None)


def queue_journal(directory, card, why, push=None):
    line = {"by": "person", "trigger": "panel", "task": card.get("task"), "decision": card["id"], "why": why}
    if push:
        line["push"] = push
    pa_hooks.append_journal(directory, line)


def run_queue_locked(Q, directory, space, card_id, answer, note, seen, deadline):
    card = load_queue_card(directory, card_id)  # C3
    card["id"] = card_id
    q = card["queue"]
    if card.get("status") != "open" or q.get("applied"):
        raise Refused("C4", "{} is {}; nothing was merged or pushed".format(card_id, card.get("status") if card.get("status") != "open" else "applied"))
    labels = [o.get("label") for o in card.get("options") or [] if isinstance(o, dict)]
    if answer != pa.OTHER and answer not in labels:
        raise Refused("C5", "--answer must be one of: " + ", ".join([str(l) for l in labels] + [pa.OTHER]))
    if answer == pa.OTHER:
        words = (note or "").strip()
        if not words:
            raise Refused("C5", "an Other answer needs --note with the person's own words")
        if len(words) > pa.OTHER_MAX:
            raise Refused("C5", pa.OTHER_TOO_LONG)
        answered(card, pa.OTHER, words)
        card["other"] = True
        save_card(directory, card)
        queue_journal(directory, card, "answered Other: " + pa_hooks.cut(pa_hooks.clean(words), 200))
        return 0, outcome(True, False, pa.OTHER, message="{} answered Other; nothing was merged or pushed.".format(card_id))
    option = next(o for o in card["options"] if isinstance(o, dict) and o.get("label") == answer)
    if not (isinstance(option.get("count"), int) and option["count"] > 0):
        answered(card, answer, note)
        save_card(directory, card)
        queue_journal(directory, card, "answered " + answer)
        return 0, outcome(True, False, answer, message="{} answered {}; nothing was merged or pushed.".format(card_id, answer))
    s = Session(deadline)
    Q_, ctx, data, rec = queue_context(directory, card)
    try:
        got = queue_facts(s, Q, ctx, data, rec, directory, space, card)
        if not seen:
            raise Refused("C12", "--seen is required (the token of the facts the person looked at)")
        if seen != got["seen"]:
            raise Refused("C12", "the facts changed since you looked; look at the card again")
        return queue_merge_and_push(s, Q, ctx, data, rec, directory, space, card, option, note)
    except Refused as r:
        q["attempts"] = (list(q.get("attempts") or []) + [{"at": pa.now(), "check": r.check, "message": r.message}])[-ATTEMPTS_KEPT:]
        save_card(directory, card)
        queue_journal(directory, card, "not answered: " + r.message)
        raise
    finally:
        ctx.close()
        s.close()


def queue_error(Q, ctx, data, rec, directory, card, label, main_before, err):
    """t-163 R10: an error after the person's answer was saved (the queue tool refused, git could not say, a file
    could not be written): the queue is saved if it can be, the card's push result says failed with the message, one
    journal line, exit 1. Nothing is retried and nothing is pushed (the push comes after the merges)."""
    words = pa_hooks.cut(pa_hooks.clean(str(err) or type(err).__name__), 300)
    try:
        Q.save(ctx, data, rec)
    except (Q.Refused, OSError) as again:
        words += "; queue.json not saved: " + pa_hooks.cut(pa_hooks.clean(str(again)), 100)
    card_id = card["id"]
    card = pa.read_json_safe(card_file(directory, card_id)) or card
    card["id"] = card_id
    now_main = ctx.tip(rec["main"])
    done = "local {} {}".format(rec["main"], "unchanged" if now_main == main_before else "moved {}..{}".format(
        short(main_before), short(now_main)))
    message = "{} answered {}: an error after the answer: {} ({}); nothing was pushed".format(card_id, label, words, done)
    result = {"at": pa.now(), "state": "failed", "from": None, "to": None, "commits": 0, "check": "error",
              "message": message, "merged": [], "pushCard": None}
    card["queue"].setdefault("push", {})["result"] = result
    try:
        save_card(directory, card)
    except OSError:
        pass
    queue_journal(directory, card, message)
    return 1, outcome(False, False, label, refusal={"check": "error", "message": message}, result=result, message=message)


def queue_merge_and_push(s, Q, ctx, data, rec, directory, space, card, option, note):
    """R3 (5)-(8), R5, R6: the answer, its approvals, the merges of the option's entries, then the push of exactly the
    merged commits, or why not and the push card that follows. Nothing is retried."""
    card_id, label, repo = card["id"], option["label"], ctx.repo
    answered(card, label, note)
    save_card(directory, card)
    main_before = ctx.tip(rec["main"])
    try:
        Q.apply_cards(ctx, rec)  # the approvals, bound as a card's (task, job, instance, added, checked digests)
        only = [(x.get("task"), x.get("job") or None) for x in card["queue"]["entries"][:option["count"]] if isinstance(x, dict)]
        lines, merged = Q.merge_front(ctx, data, rec, only=only)
        Q.save(ctx, data, rec)
    except (Q.Refused, pa_hooks.ContentError, OSError) as err:
        return queue_error(Q, ctx, data, rec, directory, card, label, main_before, err)
    card = pa.read_json_safe(card_file(directory, card_id)) or card  # apply_cards wrote it again
    card["id"] = card_id
    stop = next((l for l in lines if l.startswith("stopped at")), None)
    result = {"at": pa.now(), "state": "none", "from": None, "to": None, "commits": 0, "check": None, "message": "",
              "merged": [e["task"] for e in merged], "pushCard": None}
    push_line = None
    if not merged:
        result["message"] = "nothing merged now{}; nothing was pushed".format(": " + stop if stop else "")
    else:
        facts = blank_facts()
        push = {"version": 1, "space": space, "task": merged[-1]["task"], "path": repo}
        try:
            try:
                gather(s, directory, space, push, facts, plan=True)
            except Refused as r:
                if r.check != "C11b" or facts.get("remoteNow") != facts.get("commit"):
                    raise
                result.update(state="already", to=facts.get("commit"), message="already on the remote")
            if result["state"] != "already":
                if facts.get("merge") or facts.get("localNow") != facts.get("commit"):
                    raise Refused("C10a", "local {} is at {}, not at the merged commit {}; nothing was pushed".format(
                        facts["branch"], short(facts.get("localNow")), short(facts.get("commit"))))
                if facts["remoteNow"] != main_before:
                    n = s.git(repo, ["rev-list", "--count", "{}..{}".format(facts["remoteNow"], main_before)])[1].strip()
                    raise Refused("R6", extra_message(rec["main"], int(n) if n.isdigit() else "some"))
                entry = facts["allow"]["entry"]
                if entry.get("prePush"):
                    pre_push(s, entry, facts)
                if s.left() < PUSH_RESERVE:
                    raise Refused("deadline", "ran out of time before pushing; nothing was pushed")
                state, output, message = push_exact(s, facts)
                result.update(state=state, message=message, output=tail(output))
                push_line = journal_push(dict(push, url=facts["url"], branch=facts["branch"], commit=facts["commit"]), facts["remoteNow"], False)
        except Refused as r:
            result.update(state="failed" if r.check == "push" else "refused", check=r.check, message=r.message)
        result.update(**{"from": facts.get("remoteNow"), "to": facts.get("commit"), "commits": facts.get("count") or 0})
        if result["state"] in ("refused", "failed"):
            before = {i for i, _c in open_push_cards(directory, repo)}
            said = Q.push_card(ctx, rec, merged[-1]["task"])  # R5 rows 3-4: one more click, never a retry
            new = [i for i, _c in open_push_cards(directory, repo) if i not in before]
            result["pushCard"] = new[-1] if new else None
            result["message"] += "; " + "; ".join(said)
    done = "merged {} ({} {}..{})".format(", ".join(result["merged"]), rec["main"], short(main_before), short(ctx.tip(rec["main"]))) \
        if merged else "merged nothing"
    if result["state"] == "pushed":
        tail_words = "pushed {} {} {}..{}".format(card["queue"]["push"].get("url"), card["queue"]["push"].get("branch"),
                                                   short(result["from"]), short(result["to"]))
    elif result["state"] == "already":
        tail_words = "already on the remote"
    elif result["state"] == "none":
        tail_words = result["message"]
    else:
        tail_words = "not pushed: {}".format(result["message"])
    message = "{} answered {}: {}; {}".format(card_id, label, done, tail_words)
    if stop and merged:
        message += " ({})".format(stop)
    card["queue"]["push"]["result"] = result
    save_card(directory, card)
    queue_journal(directory, card, message, push_line)
    return 0, outcome(True, result["state"] in ("pushed", "already"), label, result=result, message=message)


# ---- t-140 Part B: the person's rule as their standing approval (R10, R13, R14) ----------------------------

def standing_rule(directory, space, repo, rule_id):
    """(rule, allowlist entry) when the person's standing rule rule_id is in force for the main checkout repo in this
    space (R10 1-5), read now; else Refused("standing", why not). The binding is on the allowlist, which only the
    person's command writes; rules.json alone never counts."""
    def no(why):
        raise Refused("standing", why)
    if not isinstance(rule_id, str) or not pa.ID.match(rule_id):
        no("--standing needs the rule's id")
    data, problem, _sha, _mtime = load_allow()
    if data is None and problem is None:
        no("no push allowlist ({}): the rule is not bound".format(allow_path()))
    if problem:
        no("the push allowlist is not valid: {}".format(problem))
    if not data["enabled"]:
        no("the push allowlist is turned off (enabled: false)")
    real = os.path.realpath(repo)
    bound = [e for e in data["repos"] if os.path.realpath(e["path"]) == real and e["space"] == space
             and (e.get("standing") or {}).get("rule") == rule_id]
    if len(bound) != 1:
        no("no allowlist entry for {} in space {} is bound to rule {} (not bound): the person binds it with pa-push-allow "
           "set --path {} --space {} --standing {}".format(repo, space, rule_id, repo, space, rule_id))
    entry = bound[0]
    if not entry["enabled"]:
        no("the allowlist entry for {} is turned off".format(repo))
    rules = space_rules(directory)
    if rules is None:
        no("this space has no valid rules.json")
    found = [r for r in rules if r.get("id") == rule_id]
    if not found:
        no("no rule {} in this space's rules.json".format(rule_id))
    if len(found) > 1:
        no("{} rules have the id {}".format(len(found), rule_id))
    rule = found[0]
    if rule.get("status") != "active":
        no("rule {} is {}, not active".format(rule_id, rule.get("status")))
    if not isinstance(rule.get("text"), str) or text_sha(rule["text"]) != entry["standing"]["textSha"]:
        no("the text of rule {} changed since the person bound it (text changed); they bind it again".format(rule_id))
    why = not_persons(rule)
    if why:
        no("rule {} is not the person's: {}".format(rule_id, why))
    if rule.get("appliesTo") not in ("all", "release"):
        no("rule {} applies to {}, not all or release".format(rule_id, rule.get("appliesTo")))
    return rule, entry


PERSON_APPROVED = ("approved", "edited and approved")  # t-163 R13: the panel's words (rules.js), exactly
RESUMES = ("created", "approved", "edited and approved", "turned on")  # t-163 R6: the person's changes that resume the rule


def not_persons(rule):
    """None when the rule is the person's (R10 (4)): source person and created by them, or source orchestrator and
    approved by them; and every history entry after that is theirs. Else why not."""
    history = [h for h in rule.get("history") or [] if isinstance(h, dict)]
    if not history:
        return "it has no history"
    if rule.get("source") == "person":
        if history[0].get("by") != "person" or history[0].get("change") != "created":
            return "its first history entry is not its creation by the person"
        start = 0
    elif rule.get("source") == "orchestrator":
        start = next((i for i, h in enumerate(history) if h.get("by") == "person" and h.get("change") in PERSON_APPROVED), None)
        if start is None:
            return "the person never approved it"
    else:
        return "its source is {}".format(rule.get("source"))
    later = [h for h in history[start + 1:] if h.get("by") != "person"]
    if later:
        return "a later change by {} ({})".format(later[0].get("by"), later[0].get("change"))
    return None


def since_of(rule):
    """When the person last created, approved or turned on the rule (R13: the Other/note window starts there; a stop
    before it is resumed, Q4: off and on). t-163 R6: only those changes, spelled exactly as the panel writes them, by
    the person, with a time in pa.now()'s form; an edit, a reorder or any other entry does not resume it. None when
    there is none (everything counts)."""
    times = [pa.parse_time(str(h.get("at"))) if isinstance(h.get("at"), str) else None for h in rule.get("history") or []
             if isinstance(h, dict) and h.get("by") == "person" and h.get("change") in RESUMES]
    times = [t for t in times if t is not None]
    return max(times) if times else None


def journal_lines(directory):
    path = os.path.join(directory, "journal.jsonl")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return [], False
    out, doubt = [], False
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError:
            doubt = True
            continue
        if isinstance(value, dict):
            out.append(value)
    return out, doubt


def journal_hold(directory):
    """The hold as pa-places reads it (hold_state below, R13): off | on | unknown. A journal that is there but
    cannot be read is unknown (fail closed: the hold is the person's brake on the one path where an agent pushes)."""
    path = os.path.join(directory, "journal.jsonl")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except FileNotFoundError:
        text = None
    except OSError:
        return "unknown"
    return hold_state(text)


def hold_state(text):
    """pa_places.hold_state, word for word (a copy: the push tool loads only pa and pa_hooks): the last line
    with a "hold" key decides; a line that is not JSON after it (or with none at all) means it cannot be told."""
    state, doubt = "off", False
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError:
            doubt = True
            continue
        if isinstance(value, dict) and "hold" in value:
            v = value["hold"]
            state = "on" if v == "on" or v is True else "off" if v == "off" or v is False else "unknown"
            doubt = False
    return "unknown" if doubt else state


def standing_stops(directory, rule):
    """What stops the rule now (R13), as (why, fresh) or None. fresh is False for a stop already written (it stays until
    the person changes the rule, Q4)."""
    since = since_of(rule)

    def after(at):  # t-163 R5: at or after since; a time that is missing or not pa.now()'s form counts (fail closed)
        t = pa.parse_time(at) if isinstance(at, str) else None
        return since is None or t is None or t >= since

    def when(at):
        return at if isinstance(at, str) and pa.parse_time(at) is not None else "time unreadable"
    lines, _doubt = journal_lines(directory)
    for l in reversed(lines):
        if l.get("standing") == "stopped" and l.get("rule") == rule.get("id") and after(l.get("at")):
            return ("the rule stopped at {}: {}; the person resumes it by turning it off and on in the panel".format(
                when(l.get("at")), pa_hooks.cut(pa_hooks.clean(str(l.get("why"))), 200)), False)
    hold = journal_hold(directory)
    if hold != "off":
        return ("a hold is {} (the journal read as pa-places reads it)".format(hold), True)
    for sub in ("decisions", pa.ARCHIVE):
        folder = os.path.join(directory, sub)
        for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
            if not name.endswith(".json"):
                continue
            card = pa.read_json_safe(os.path.join(folder, name))
            if not isinstance(card, dict):
                continue
            at = card.get("previousAnswered") or card.get("answered")
            if worktree_note(card) and after(at):
                return ("card {} carries a note the person wrote ({}), withdrawn or asked again before it was acted on".format(
                    name[:-5], when(at)), True)
            if card.get("status") != "answered" or not after(card.get("answered")):
                continue
            if pa.is_other(card):
                return ("card {} was answered Other at {}".format(name[:-5], when(card.get("answered"))), True)
            if str(card.get("note") or "").strip() and str(card.get("answeredBy") or "").startswith("person, in the panel"):
                return ("card {} has a note written in the panel ({})".format(name[:-5], when(card.get("answered"))), True)
    return None


def worktree_note(card):
    """A worktree card (single or batch) withdrawn, or asked again and not answered yet, that carries the person's note
    from its last answer (t-168 round 3): pa-worktree remove withdraws a card for its note without reading it, and the
    note may say to keep any of them, so it stops the rule as the answered note did (R13), whoever answered (fail closed)."""
    tree = card.get("kind") == "worktrees" or bool(card.get("worktree"))
    return tree and card.get("status") in ("withdrawn", "open") and bool(str(card.get("previousNote") or "").strip())


def write_stop(directory, rule_id, why, task=None):
    """R13: the stop line. Both standing commands refuse while it stands (until the person changes the rule)."""
    line = {"by": "agent" if pa_hooks.by_an_agent() else "person", "trigger": "standing", "standing": "stopped", "rule": rule_id,
            "why": pa_hooks.cut(pa_hooks.clean(str(why)), 300)}
    if task:
        line["task"] = task
    pa_hooks.append_journal(directory, line)


STANDING_MSG = ("pa-push-main run --standing is the Project Agent's, under the person's standing rule to merge and push in "
                "its own space; without it, the person answers a card in the panel. ({})")


def is_agent(env, ancs):
    if any(k in env for k in AGENT_VARS) or any(k.upper().startswith(AGENT_PREFIXES) for k in env):
        return True
    for a in ancs or []:
        name = os.path.basename(str(a.get("comm") or "")).lower()
        if name in AGENT_NAMES or (name == "node" and any(n in str(a.get("args") or "").lower() for n in AGENT_NAMES)):
            return True
    return False


def standing_caller_refusal(env, ancs, cwd, directory):
    """C0 for the --standing form only (R14): the caller is this space's Project Agent: an agent (its variables or an
    agent ancestor), in one of the space's Project Agent terminals (agent.json), not a ledger worker's terminal, with no
    task record above its folder or its project folder. The card form's C0 is unchanged. None, or why not."""
    if ancs is None:
        return "the process tree could not be read (ps failed), so the caller is unknown"
    if not is_agent(env, ancs):
        return "the caller is not an agent: the person or the panel answers a card instead"
    for folder in (cwd, env.get("CLAUDE_PROJECT_DIR")):
        record = pa_hooks.find_task(folder) if folder else None
        if record:
            return "a worker's folder (the worktree of task {}): workers never push".format(record.get("task"))
    me = pa.local_id(env.get("HC_TERMINAL_ID") or "")
    if not me:
        return "not in a Herdr terminal: the space's Project Agent runs in one"
    ledger = pa.read_json_safe(os.path.join(directory, "ledger.json")) or {}
    for t in ledger.get("tasks") or [] if isinstance(ledger, dict) else []:
        for w in t.get("workers") or [] if isinstance(t, dict) else []:
            if isinstance(w, dict) and pa.local_id(str(w.get("address") or "")) == me:
                return "{} is a worker of {} in the ledger: workers never push".format(me, t.get("id"))
    agent = pa.read_json_safe(os.path.join(directory, "agent.json")) or {}
    # t-163 R8: the current terminal only; `terminals` keeps old addresses (a restart's), which never push
    mine = pa.local_id(str(agent.get("terminal") or "")) if isinstance(agent, dict) else ""
    if not mine or me != mine:
        return "{} is not this space's Project Agent terminal ({})".format(me, mine or "none recorded")
    return None


def run_standing(space, rule_id, repo, env, ancs, cwd=None, deadline=None):
    """pa-push-main run --standing <rule> --repo <main checkout> --space S (R14), as (exit code, outcome): the push of
    the queue's merges made on the person's rule, with every check of today's run but the card's (no seen token: the
    binding stands in for it). A refusal after C0 writes the stop line (R13); the remote is never changed by one."""
    deadline = deadline or time.monotonic() + BUDGET
    if not space:
        return 1, refused(Refused("C1", "pa-push-main run --standing needs --space <id>"))
    space = pa.local_id(space)
    directory = os.path.join(pa.ROOT, "spaces", space) if pa.ID.match(space) else None
    if not directory or not os.path.isfile(os.path.join(directory, "agent.json")):
        return 1, refused(Refused("C1", "no Project Agent state for space {}".format(space)))
    why = standing_caller_refusal(env, ancs, cwd or os.getcwd(), directory)
    if why:
        return 1, refused(Refused("C0", STANDING_MSG.format(why)))
    if not isinstance(repo, str) or not repo:
        return 1, refused(Refused("C1", "pa-push-main run --standing needs --repo <main checkout>"))
    repo = os.path.realpath(repo)
    try:
        with push_lock(directory):
            try:
                rule, binding = standing_rule(directory, space, repo, rule_id)
            except Refused as r:
                raise Refused("C0", STANDING_MSG.format("the rule is not in force: " + r.message))
            stop = standing_stops(directory, rule)
            if stop:
                if stop[1]:
                    write_stop(directory, rule_id, stop[0])
                raise Refused("C0", STANDING_MSG.format("the rule stops here: " + stop[0]))
            s = Session(deadline)
            facts, task = blank_facts(), None
            try:
                common = os.path.realpath(os.path.join(repo, ".git"))
                merged = queue_merged(directory, common)
                last = merged[-1] if merged else None
                if not last:  # t-163 R12: nothing merged is nothing to push, not a stop
                    return 0, outcome(True, False, None, result={"state": "none"},
                                      message="nothing merged on the rule; nothing to push ({})".format(repo))
                task = last.get("task")
                push = {"version": 1, "space": space, "task": task, "path": repo}
                try:
                    gather(s, directory, space, push, facts, plan=True)
                except Refused as r:
                    if r.check == "C11b" and facts.get("remoteNow") and facts.get("remoteNow") == facts.get("commit"):
                        return 0, outcome(True, True, None, result={"state": "already", "to": facts["commit"]},
                                          message="already on the remote: {} {} is at {}".format(facts["url"], facts["branch"], short(facts["commit"])))
                    raise
                if last.get("standing") != rule_id:  # t-163 R12: after gather, so a merge already pushed is not a stop
                    raise Refused("standing", "the last merge of {} was not made on rule {} (approved by {}): commits not "
                                              "merged on the rule; a push card lists them".format(
                                                  repo, rule_id, last.get("card") or last.get("by")))
                if facts.get("merge") or facts.get("localNow") != facts.get("commit"):
                    raise Refused("C10a", "local {} is at {}, not at the merged commit {}; nothing is pushed on this path".format(
                        facts["branch"], short(facts.get("localNow")), short(facts.get("commit"))))
                labels, _batch = queue_labels(s, repo, facts["remoteNow"], facts["commit"],
                                              [m for m in merged if m.get("standing") == rule_id])
                foreign = [c["sha"] for c in facts["commits"] if c["sha"] not in labels]
                if foreign or len(facts["commits"]) != facts["count"]:
                    raise Refused("standing", "{} commit(s) of {}..{} not merged on the rule; a push card lists them".format(
                        len(foreign) or "some", short(facts["remoteNow"]), short(facts["commit"])))
                entry = facts["allow"]["entry"]
                if entry.get("prePush"):
                    pre_push(s, entry, facts)
                if s.left() < PUSH_RESERVE:
                    raise Refused("deadline", "ran out of time before pushing; nothing was pushed")
                state, output, message = push_exact(s, facts)
                jp = journal_push(dict(push, url=facts["url"], branch=facts["branch"], commit=facts["commit"]), facts["remoteNow"], False)
                pa_hooks.append_journal(directory, {"by": "agent", "trigger": "standing", "task": task, "rule": rule_id,
                                                    "textSha": binding["standing"]["textSha"], "why": message, "push": jp})
                if state != "pushed":
                    write_stop(directory, rule_id, message, task)
                    return 1, outcome(False, False, None, refusal={"check": "verify", "message": message},
                                      result={"state": state}, message="pa-push-main: " + message)
                refresh_tracking(s, facts)
                return 0, outcome(True, True, None, result={"state": "pushed", "from": facts["remoteNow"], "to": facts["commit"],
                                                            "count": facts["count"], "output": tail(output)},
                                  message="{} (rule {}); next: pa-install-all as r-pa-installs-2 says".format(message, rule_id))
            except Refused as r:
                write_stop(directory, rule_id, "{} ({})".format(r.message, r.check), task)
                raise
            finally:
                s.close()
    except Refused as r:
        return 1, refused(r)


def cmd_run(args):
    for k in list(os.environ):  # this process's own git reads (the gate's) see no GIT_* either
        if k.upper().startswith("GIT_"):
            del os.environ[k]
    if args.standing:
        if args.card:
            print("pa-push-main: run --standing takes no card", file=sys.stderr)
            return 2
        code, result = run_standing(args.space, args.standing, args.repo, os.environ, ancestors())
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(result["message"], file=sys.stdout if code == 0 else sys.stderr)
        return code
    if not args.card or not args.answer:
        print("pa-push-main: run needs a card and --answer (or --standing <rule id> --repo <main checkout>)", file=sys.stderr)
        return 2
    code, result = run_push(args.space, args.card, args.answer, args.note, args.seen, os.environ, ancestors())
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(result["message"], file=sys.stdout if code == 0 else sys.stderr)
    return code


def main_parser():
    parser = argparse.ArgumentParser(prog="pa-push-main", description="Push a main branch from a push card (the person's answer in the panel).")
    sub = parser.add_subparsers(dest="action")
    sub.required = True
    plan = sub.add_parser("plan", help="open a push card for a task that passed its full gate")
    plan.add_argument("task")
    plan.add_argument("--repo", required=True, help="the repository's main checkout")
    plan.add_argument("--space")
    check = sub.add_parser("check", help="the card's facts, read now (read-only)")
    check.add_argument("card")
    check.add_argument("--space")
    check.add_argument("--json", action="store_true")
    run = sub.add_parser("run", help="the person's answer, from the panel: checks everything again, then pushes")
    run.add_argument("card", nargs="?")
    run.add_argument("--answer")
    run.add_argument("--standing", help="t-140 R14: the Project Agent's push of the queue's merges under the person's rule")
    run.add_argument("--repo", help="with --standing: the repository's main checkout")
    run.add_argument("--space")
    run.add_argument("--note")
    run.add_argument("--seen")
    run.add_argument("--json", action="store_true")
    return parser


# ---- entry point ----------------------------------------------------------------------------------

def main():
    kind = os.path.basename(sys.argv[0])
    if kind in ("pa_push.py", "pa_push") and len(sys.argv) > 1 and sys.argv[1] in ("allow", "main"):
        kind, sys.argv = "pa-push-" + sys.argv[1], [sys.argv[0]] + sys.argv[2:]
    if kind == "pa-push-allow":
        args = allow_parser().parse_args()
        if args.action == "show":
            return cmd_allow_show(args)
        return allow_change(args, os.environ, ancestors())
    if kind == "pa-push-main":
        args = main_parser().parse_args()
        if args.action == "plan":
            return cmd_plan(args)
        if args.action == "run":
            return cmd_run(args)
        return cmd_check(args)
    raise pa.Problem("invoke as pa-push-main or pa-push-allow")


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except pa.Problem as e:
        print("{}: {}".format(os.path.basename(sys.argv[0]), e), file=sys.stderr)
        sys.exit(2)
