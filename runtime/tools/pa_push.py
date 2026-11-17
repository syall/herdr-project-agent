#!/usr/bin/env python3
"""The person's push of a main branch, from a push card in the Project panel. Python standard library only (3.7+).

Selected by the name it is invoked as (symlinks in ~/.local/bin):

  pa-push-main plan <task-id> --repo <main checkout> [--space ID]
                                                 open a push card for a task that passed its full gate
  pa-push-main check <card> [--space ID] [--json]
                                                 the card's facts read now, the first failing check, the seen token
  pa-push-main run <card> --answer Push|"Not now" --space ID [--seen TOKEN] [--note T] [--json]
                                                 the person's answer, run by the Project panel on their click:
                                                 every check again under a lock, then a fast-forward push of
                                                 exactly the gated commit to the allowlist URL (never a force)
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
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True  # keep tools/ free of caches
import pa  # noqa: E402
import pa_hooks  # noqa: E402

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
    if all(a.get("pid") == 1 for a in ancs):
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


class Session:
    """The git context of one plan, check or run: an empty hooks folder (removed by close) and a deadline."""

    def __init__(self, deadline=None):
        self.deadline = deadline
        self.hooks = tempfile.mkdtemp(prefix="pa-push-hooks-")

    def left(self):
        return float("inf") if self.deadline is None else self.deadline - time.monotonic()

    def git(self, path, args, timeout=30):
        left = self.left()
        if left < 2:
            return 124, "", "ran out of time"
        return git_call(base(path, self.hooks) + list(args), min(timeout, left))

    def gate_git(self, path, *args, timeout=None):
        """pa_hooks.git()'s signature and stripped result, run as self.git runs (hooks and fsmonitor off, no
        GIT_*): the runner the gate evaluation is given, so it runs nothing the task worktree names."""
        code, out, err = self.git(path, args, timeout or pa_hooks.GIT_TIMEOUT)
        return code, out.strip(), err.strip()

    def sha(self, path, ref):
        code, out, _err = self.git(path, ["rev-parse", "--verify", "-q", ref + "^{commit}"])
        out = out.strip()
        return out if not code and HEX.match(out) else None

    def is_ancestor(self, path, a, b):
        return self.git(path, ["merge-base", "--is-ancestor", a, b])[0] == 0

    def close(self):
        shutil.rmtree(self.hooks, ignore_errors=True)


def tracked_changes(s, path):
    """Lines of `git status --porcelain --untracked-files=no`, without .claude/ (as the gate counts), or None."""
    code, out, _err = s.git(path, ["--no-optional-locks", "status", "--porcelain", "--untracked-files=no"])
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
    when extensions.worktreeConfig is on). The worktree is the agent's, so a key that can run a program refuses.
    The refusal message, or None."""
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


def gather(s, directory, space, push, facts, plan=False):
    """Checks C6-C11 in order, filling facts as it goes. Raises Refused at the first that fails. plan: the card does
    not exist yet, so its fields are taken from what is read now (and push is filled in), instead of compared.
    Returns "already" when the remote branch is at the commit already, else None."""
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
    # The worktree scope exists only with extensions.worktreeConfig (git refuses --worktree otherwise, and then
    # the local scope is the whole story).
    code, wt_config, _ = s.git(real, ["config", "--local", "--bool", "--get", "extensions.worktreeConfig"])
    for scope in ("local", "worktree") if wt_config.strip() == "true" else ("local",):
        code, out, err = s.git(real, ["config", "--" + scope, "--list", "--name-only"])
        if code:
            raise Refused("C7b", "could not read the {} git settings of {} ({}); nothing was pushed".format(scope, path, err.strip()[-200:]))
        for key in out.split():
            if RISKY.match(key):
                raise Refused("C7b", "{} has git setting {} ({}), which can run a program or redirect the push; remove it, "
                                     "or push by hand".format(path, key, scope))
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
    why = task_settings_refusal(s, task.get("worktree"))
    if why:
        raise Refused("C8a", why)
    now_gate = pa_hooks.evaluate_gate(directory, task, "review", runner=s.gate_git)
    if not now_gate["passed"]:
        failed = ["{} ({})".format(c["check"], c.get("why")) for c in now_gate["checks"] if not c["ok"]]
        raise Refused("C8a", "the full gate for {} does not pass now: {}".format(task_id, "; ".join(failed)))
    gate = pa.read_json(os.path.join(directory, "evidence", task_id, "GATE.json"))
    if not isinstance(gate, dict):
        raise Refused("C8b", "there is no GATE.json for {}; run pa-gate {}".format(task_id, task_id))
    facts["gate"] = {"at": gate.get("at"), "phase": gate.get("phase"), "passed": gate.get("passed"), "commit": None, "uncommitted": None}
    if gate.get("phase") != "review":
        raise Refused("C8b", "GATE.json for {} is from phase {}; run pa-gate {} again".format(task_id, gate.get("phase"), task_id))
    if gate.get("passed") is not True:
        raise Refused("C8b", "GATE.json for {} did not pass".format(task_id))
    pins = [c for c in gate.get("commits") or [] if isinstance(c, dict) and isinstance(c.get("common"), str)
            and os.path.realpath(c["common"]) == common]
    if len(pins) != 1:
        raise Refused("C8c", "GATE.json pins {} for this repository; run pa-gate {} again".format(
            "no commit" if not pins else "{} commits".format(len(pins)), task_id))
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
    live = live_pin(s, task.get("worktree"), common)
    if not live or live["commit"] != commit:
        raise Refused("C8g", "the task's worktree is not at the gated commit now ({}); run pa-gate {} again and ask for a new card".format(
            "not found" if not live else "it is at " + short(live["commit"]), task_id))
    if live["uncommitted"] != 0:
        raise Refused("C8g", "the task's worktree has {} uncommitted change(s) to tracked files now; commit them and run the gate again".format(live["uncommitted"]))
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
    if plan:
        push["commits"] = facts["commits"]
    return None


REBASED = re.compile(r"^rebase\b.*\(finish\): refs/heads/(\S+) onto ([0-9a-f]{40}|[0-9a-f]{64})$")


def task_base(s, path, task_branch):
    """Where the task branch started, from its reflog in path: the newest `rebase (finish): refs/heads/<b> onto
    <sha>`, else the oldest entry when it is `branch: Created from ...`. None when the reflog does not say."""
    if branch_problem(task_branch):
        return None
    code, out, _ = s.git(path, ["reflog", "show", "--format=%H%x1f%gs", "refs/heads/" + task_branch, "--"])
    entries = [l.split("\x1f", 1) for l in out.splitlines() if "\x1f" in l] if not code else []
    for _sha, subject in entries:
        m = REBASED.match(subject)
        if m and m.group(1) == task_branch:
            return m.group(2)
    if entries and entries[-1][1].startswith("branch: Created from ") and HEX.match(entries[-1][0]):
        return entries[-1][0]
    return None


def commit_list(s, path, remote, commit, task_branch):
    """([{sha, subject, author, fromTask}] of remote..commit, newest first, at most COMMITS_MAX; the full count).
    fromTask (spec 6.1) is true for a commit on the task branch's own first-parent line since the branch started
    (task_base), false for any other (on local main before this task, or merged in); null for every commit when
    where the branch started cannot be read."""
    code, out, _ = s.git(path, ["log", "-n", str(COMMITS_MAX), "--format=%H%x1f%s%x1f%an <%ae>", remote + ".." + commit])
    rows = [l.split("\x1f") for l in out.splitlines() if l.count("\x1f") == 2] if not code else []
    own = None
    base = task_base(s, path, task_branch)
    if base and s.is_ancestor(path, base, commit):
        code, mine, _ = s.git(path, ["rev-list", "--first-parent", base + ".." + commit])
        own = set(mine.split()) if not code else None
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
    return os.path.join(directory, "decisions", card_id + ".json")


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


def card_context(push, facts, entry):
    """The card's context, written by the tool from what it read (never by an agent)."""
    branch, c7 = push["branch"], short(push["commit"])
    lines = ["Repository: {}".format(push["path"]),
             "Push to: {} branch {} (on your push allowlist{}). The remote {} is at {} now.".format(
                 push["url"], branch, ": " + pa_hooks.clean(entry["note"]) if entry.get("note") else "", branch, short(push["remoteBefore"])),
             "Local {} is at {}; {}.".format(branch, short(push["localBefore"]),
                                             "this card fast-forwards it to {} first".format(c7) if push["merge"] else "it is the gated commit"),
             "Gated commit: {} on {} (pa-gate review passed at {}).".format(c7, push["taskBranch"], push["gateAt"]),
             "Commits to push ({}):".format(facts["count"])]
    for c in push["commits"][:CONTEXT_COMMITS]:
        lines.append("- {} {} ({}){}".format(short(c["sha"]), c["subject"], c["author"], " (not from this task)" if c["fromTask"] is False else ""))
    if facts["count"] > CONTEXT_COMMITS:
        lines.append("- and {} more".format(facts["count"] - CONTEXT_COMMITS))
    if any(c["fromTask"] is None for c in push["commits"]):
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


def open_push_card(directory, task_id, path):
    folder = os.path.join(directory, "decisions")
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if not (name.startswith("d-push-") and name.endswith(".json")):
            continue
        card = pa.read_json(os.path.join(folder, name))
        push = card.get("push") if isinstance(card, dict) else None
        if isinstance(push, dict) and card.get("status") == "open" and push.get("task") == task_id and \
                isinstance(push.get("path"), str) and os.path.realpath(push["path"]) == path:
            return name[:-5]
    return None


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
    push = {"version": 1, "space": space, "task": task_id, "path": path}
    facts = blank_facts()
    s = Session(deadline=time.monotonic() + 120)
    try:
        with push_lock(directory):
            other = open_push_card(directory, task_id, path)
            if other:
                raise Refused("plan", "an open push card {} exists for {} and {}; withdraw {} first, with pa-decide --withdraw".format(
                    other, task_id, path, other))
            gather(s, directory, space, push, facts, plan=True)
            entry = facts["allow"]["entry"]
            card_id = next_card_id(directory, task_id)
            push.update(done=False, result=None, attempts=[])
            name, count = os.path.basename(path), facts["count"]
            card = {"id": card_id, "task": task_id, "kind": "push",
                    "question": "Push {} {} to {} ({} commit{})?".format(name, push["branch"], push["url"], count, "" if count == 1 else "s"),
                    "context": card_context(push, facts, entry),
                    "options": [{"label": "Push", "detail": "Fast-forward local {} to {} if needed, then push {}..{}. No force.".format(
                                    push["branch"], short(push["commit"]), short(push["remoteBefore"]), short(push["commit"]))},
                                {"label": "Not now", "detail": "Nothing is pushed. The card closes."}],
                    "evidence": ["GATE.json", "REVIEW.md"], "recommendation": "Push", "status": "open", "answer": None, "note": None,
                    "created": pa.now(), "answered": None, "push": push}
            pa.write_json(card_file(directory, card_id), card)
            pa_hooks.append_journal(directory, {"by": "agent" if pa_hooks.by_an_agent() else "person", "trigger": "push-plan", "task": task_id,
                                                "decision": card_id, "why": "push card opened: {} {} {}..{} ({} commits)".format(
                                                    push["url"], push["branch"], short(push["remoteBefore"]), short(push["commit"]), count),
                                                "push": journal_push(push, push["remoteBefore"], push["merge"])})
    except Refused as r:
        print("pa-push-main: refused: " + r.message, file=sys.stderr)
        return 1
    finally:
        s.close()
    print("{} opened: {}. The person answers it in the panel.".format(card_id, card["question"]))
    return 0


def journal_push(push, frm, merged):
    return {"path": push.get("path"), "url": push.get("url"), "branch": push.get("branch"), "from": frm, "to": push.get("commit"),
            "merged": merged, "allowSha": push.get("allowSha"), "allowMtime": push.get("allowMtime"), "gateAt": push.get("gateAt")}


def cmd_check(args):
    directory, space = space_of(args)
    report = check_card(directory, space, args.card, os.environ, ancestors())
    if args.json:
        print(json.dumps(report, indent=2))
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
ANSWERS = ("Push", "Not now")
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
    pa.write_json(card_file(directory, card["id"]), card)


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


def cmd_run(args):
    for k in list(os.environ):  # this process's own git reads (the gate's) see no GIT_* either
        if k.upper().startswith("GIT_"):
            del os.environ[k]
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
    run.add_argument("card")
    run.add_argument("--answer", required=True)
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
