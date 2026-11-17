#!/usr/bin/env python3
"""The person's push of a main branch, from a push card in the Project panel. Python standard library only (3.7+).

Selected by the name it is invoked as (symlinks in ~/.local/bin):

  pa-push-allow show [--json] [--space ID]       the push allowlist (<PA home>/push-allow.json): valid or why not,
                                                 on or off, its SHA-256 and time, each entry
  pa-push-allow add --path P --space S [--url U] [--branch B] [--note T] [--pre-push ARG...]
  pa-push-allow remove --path P --space S
  pa-push-allow on|off [--path P --space S]      the person's commands: change the allowlist (refused for agents)

Off by default: with no push-allow.json, or "enabled": false, nothing here pushes. Agents never push: the
person-only commands refuse an agent's shell (its variables, a claude or codex ancestor process, or a failed
ps). That fence stops agents that follow their rules; it does not prove a human.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys

sys.dont_write_bytecode = True  # keep tools/ free of caches
import pa  # noqa: E402

ALLOW_FILE = "push-allow.json"
MAX_REPOS = 50
NOTE_MAX = 200
TOP_KEYS = ("version", "enabled", "repos")
ENTRY_KEYS = ("path", "url", "branch", "space", "enabled", "prePush", "note")
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


def git_call(argv, timeout, cwd=None):
    """(exit code, stdout, stderr) of one program: an argv list, never a shell, with clean_env(). A call past
    its timeout gives 124; a program that cannot start gives 127."""
    try:
        r = subprocess.run(list(argv), cwd=cwd, env=clean_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True, timeout=max(1, timeout))
    except subprocess.TimeoutExpired:
        return 124, "", "{} timed out after {} s".format(" ".join(argv[:4]), int(timeout))
    except OSError as e:
        return 127, "", str(e)
    return r.returncode, r.stdout, r.stderr


def base(path, hooks):
    """git -C <path> with the repository's hooks and fsmonitor switched off."""
    return ["git", "-C", path, "-c", "core.hooksPath=" + hooks, "-c", "core.fsmonitor=false"]


# ---- who is calling (C0) -------------------------------------------------------------------------

AGENT_VARS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_SANDBOX", "CODEX_HOME_SESSION")
AGENT_PREFIXES = ("CODEX_", "CLAUDE_CODE_")
AGENT_NAMES = ("claude", "codex")
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
    failed: refused). strict adds C0.3, a Herdr terminal (pa-push-main run belongs to the panel)."""
    for k in AGENT_VARS:
        if k in env:
            return "{} is set: an agent's shell".format(k)
    for k in sorted(env):
        if k.upper().startswith(AGENT_PREFIXES):
            return "{} is set: an agent's shell".format(k)
    if ancs is None:
        return "the process tree could not be read (ps failed), so the caller is unknown"
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
            data["repos"].append(entry)
            what = "added: " + json.dumps(entry)
        elif not found:
            print("pa-push-allow: no entry for {} in space {}".format(args.path, space), file=sys.stderr)
            return 1
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


def default_url(path):
    hooks = os.devnull
    code, out, _err = git_call(base(path, hooks) + ["remote", "get-url", "origin"], 30)
    return out.strip() if not code else ""


def default_branch(path):
    code, out, _err = git_call(base(path, os.devnull) + ["symbolic-ref", "--short", "-q", "HEAD"], 30)
    return out.strip() if not code else ""


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
    raise pa.Problem("invoke as pa-push-main or pa-push-allow")


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except pa.Problem as e:
        print("{}: {}".format(os.path.basename(sys.argv[0]), e), file=sys.stderr)
        sys.exit(2)
