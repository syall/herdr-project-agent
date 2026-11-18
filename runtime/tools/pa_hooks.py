#!/usr/bin/env python3
"""Project Agent gates and worker hooks. Python standard library only (3.7+).

Selected by the name it is invoked as (symlinks in ~/.local/bin):

  pa-gate <task-id> [--phase P] [--space ID]    deterministic evidence check (exit 0 pass, 1 fail)
  pa-hooks install <worktree> --task ID [--fix] install the hooks below into a worktree's local settings
  pa-hooks remove <worktree>                    take them out again
  pa-full-suite                                 run the full test suite once, output into build.log
  pa-worktree check [--open-cards]              worktrees whose tasks are all done or parked: list them, open decision cards
  pa-worktree remove <task-id>                  delete a task's worktree, only with an answered "Delete" card
  pa-worker info <address>                      what deleting a worker would remove, as JSON
  pa-worker delete <address> [--keep-worktree] [--discard] [--with-task]
                                                the person's command: end a worker and remove what belongs to it
  pa-worker close <address> --evidence F         end a worker whose report is read (agents may run it; guarded)
  pa-worker close --leftovers [--dry-run]        list the workers from before one-worker-per-job on one decision card
  pa-worker close [<address>] --card ID          close the workers the person approved on that card (same guards)
  pa-worker keep <address> --quote TEXT [--off]  record that the person wants a worker kept open
  pa-trust <address> [--space ID]               a blocked worker's Claude Code folder trust prompt: accept it only for
                                                the project folder or a task's evidence folder (see cmd_trust); one
                                                JSON line, exit 0 accepted, 1 not, 2 usage or hc error
  pa-hook <name>                                one hook, called by the agent harness (reads its JSON on stdin):
      lint-after-edit   after each edit: lint the changed file
      affected-tests    when a task is marked complete, and (--stop) before the session ends
      test-file-lock    before an edit: no test-file edits during a fix task
      guard             before a command or an edit: nothing that cannot be taken back,
                        and the short denylist in pa_guard.py

A failing hook exits with code 2, which sends the agent back to fix it.
"""
import argparse
import contextlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True  # keep tools/ free of caches
import pa  # noqa: E402


def _load_guard():
    """pa_guard.py from this file's own folder, found the same way when run through a symlink or loaded by a test.

    Loaded by hook_guard each time it is needed, not at import, so that a missing or broken pa_guard.py stops only
    the guard (which then refuses the call) and the other hooks keep working.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("pa_guard", os.path.join(os.path.dirname(os.path.realpath(__file__)), "pa_guard.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Card d-038 ("Block"): when the guard cannot load or its check fails, the call is refused, not let through.
GUARD_UNAVAILABLE = ("Refused by the Project Agent's guard: guard unavailable ({}). Commands and edits cannot be checked, "
                     "so they do not run: hc send is refused too. Stop and report this to the Project Agent by saying so as your "
                     "last message; it repairs the guard. There is no override.")
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")

TASK_FILE = os.path.join(".claude", "pa-task.json")
SETTINGS = os.path.join(".claude", "settings.local.json")
MARK = "pa-hook "  # every hook command we install contains this
PHASES = ["intent", "design", "plan", "build", "test", "review", "release", "maintain"]
# What each phase must leave behind before a task may leave it.
ARTIFACTS = {
    "intent": ["intent.md"],
    "design": ["spec.md"],
    "plan": ["plan.md"],
    "build": ["FINDINGS.md", "DECISIONS.md"],
    "test": ["VERIFY.md"],
    "review": ["REVIEW.md"],
}
TEST_FILE = re.compile(r"(^|/)(tests?|__tests__|spec|specs)/|(^|/)test_[^/]+$|[._-](test|tests|spec)\.[^/]+$|Tests?\.[A-Za-z]+$")
# Never, whatever the handoff says. Each is (pattern, what it is).
DENY_COMMANDS = [
    (r"\bgit\s+push\b[^;&|]*(\s--force\b|\s-f\b|\s--force-with-lease\b|\s\+\S)", "a force push"),
    (r"\bgit\s+reset\s+--hard\b", "discarding uncommitted work (git reset --hard)"),
    (r"\bgit\s+clean\b[^;&|]*\s-[a-zA-Z]*f", "deleting untracked work (git clean -f)"),
    (r"\bgit\s+(checkout|restore)\s+(--\s+)?\.(\s|$)", "discarding uncommitted work"),
    (r"\bgit\s+stash\s+(drop|clear)\b", "deleting stashed work"),
    (r"\bgit\s+worktree\s+remove\b[^;&|]*(\s--force\b|\s-f\b)", "force-removing a worktree (uncommitted work is lost)"),
    (r"\bgit\s+branch\s+[^;&|]*-D\b", "force-deleting a branch"),
    (r"\bgh\s+pr\s+merge\b", "merging a change request"),
    (r"\bgh\s+release\s+create\b", "publishing a release"),
    (r"\b(npm|pnpm|yarn)\s+publish\b|\bcargo\s+publish\b|\btwine\s+upload\b", "publishing a package"),
    (r"\bterraform\s+(apply|destroy)\b|\bkubectl\s+(apply|delete|rollout)\b|\bcdk\s+(deploy|destroy)\b|\bhelm\s+(install|upgrade|uninstall)\b", "a deploy or infrastructure change"),
    (r"(^|[\s'\"=/])\.env(\.[\w.-]+)?(\s|$|['\"])|~/\.ssh\b|/\.ssh/|\bid_(rsa|ed25519|ecdsa)\b", "reading secrets"),
    # t-009 R15: billed amounts come only from the person (quotes and backslashes between the words do not hide it)
    (r"\bpa-costs['\"\s\\]+billed['\"\s\\]+(fetch|enable|import)\b", "the person's billing command (pa-costs billed fetch, enable or import)"),
]
DENY_PATHS = [(r"(^|/)\.env(\.[^/]*)?$", "an environment file with secrets"), (r"(^|/)\.ssh(/|$)", "SSH keys"),
              (r"(^|/)(credentials|\.netrc|\.npmrc|\.pypirc)$", "a credentials file")]
ROUTE = "This needs the person's approval. Stop, and report it to the Project Agent; it opens a decision card."


def find_task(start):
    """The worktree's task record ({task, space, fix, root}), walking up from start."""
    folder = os.path.realpath(start)
    while True:
        record = pa.read_json(os.path.join(folder, TASK_FILE))
        if record:
            return dict(record, root=folder)
        parent = os.path.dirname(folder)
        if parent == folder:
            return None
        folder = parent


def space_from_terminal():
    seen, address = set(), os.environ.get("HC_TERMINAL_ID")
    while address and address not in seen and len(seen) < 6:
        seen.add(address)
        info = pa.hc_get(address)
        if (info.get("labels") or {}).get("herdr.pa"):
            return info["labels"]["herdr.pa"]
        address = info.get("parent")
    return None


def load_context(space):
    directory = pa.space_dir(space)
    agent = pa.read_json(os.path.join(directory, "agent.json"))
    if not agent:
        raise pa.Problem("no Project Agent state for space " + space)
    return directory, agent, pa.load_profile(agent.get("profile"))


def command_for(kind, agent, profile):
    """The space's own command (agent.json "commands"), else the profile's. kind: lint | affected | full."""
    own = (agent.get("commands") or {}).get(kind)
    if own:
        return own
    value = profile.get("lint") if kind == "lint" else (profile.get("tests") or {}).get(kind)
    if value and value.startswith("hooks/"):
        value = os.path.join(profile["_folder"], value)
    return value or None


def changed_files(root):
    try:
        out = subprocess.run(["git", "status", "--porcelain"], cwd=root, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, universal_newlines=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    files = []
    for line in out.splitlines():
        path = line[3:].split(" -> ")[-1].strip().strip('"')
        if path and not path.startswith(".claude/"):
            files.append(path)
    return files


def run_shell(command, cwd, files, timeout):
    """Runs a configured command. {file}/{files} are filled in; PA_CHANGED_FILES lists them, one per line."""
    quoted = " ".join(shlex.quote(f) for f in files)
    line = command.replace("{files}", quoted).replace("{file}", quoted)
    env = dict(os.environ, PA_CHANGED_FILES="\n".join(files))
    try:
        r = subprocess.run(["sh", "-c", line], cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True, timeout=timeout)
        return r.returncode, r.stdout
    except subprocess.TimeoutExpired as e:
        return 124, (e.stdout or "") + "\n(timed out after {} s)".format(timeout)


def tail(text, lines=40):
    rows = text.rstrip().splitlines()
    return "\n".join((["... ({} more lines above)".format(len(rows) - lines)] if len(rows) > lines else []) + rows[-lines:])


def block(message):
    print(message, file=sys.stderr)
    sys.exit(2)


# ---- hooks ------------------------------------------------------------------------------------

def hook_input():
    try:
        return json.load(sys.stdin) if not sys.stdin.isatty() else {}
    except ValueError:
        return {}


def hook_context(data):
    record = find_task(data.get("cwd") or os.getcwd())
    if not record:
        return None, None, None
    try:
        _dir, agent, profile = load_context(record["space"])
    except pa.Problem:
        return record, {}, {}
    return record, agent, profile


def hook_lint(data, _args):
    record, agent, profile = hook_context(data)
    path = (data.get("tool_input") or {}).get("file_path")
    command = record and command_for("lint", agent, profile)
    if not command or not path or not os.path.isfile(path):
        return 0
    code, out = run_shell(command, record["root"], [os.path.relpath(path, record["root"])], 120)
    if code:
        block("Lint failed for {} (task {}). Fix it before going on.\n{}".format(path, record["task"], tail(out)))
    return 0


def hook_affected(data, args):
    record, agent, profile = hook_context(data)
    if not record:
        return 0
    files = changed_files(record["root"])
    command = command_for("affected", agent, profile)
    if not files or not command:
        return 0
    code, out = run_shell(command, record["root"], files, 900)
    if not code:
        return 0
    message = "The tests affected by this session's changes fail (task {}). Fix the code and run them again.\n{}".format(record["task"], tail(out))
    if "--stop" in args:
        # Hold the session open a few times, then let it end and say so, rather than loop forever.
        counter = os.path.join(tempfile.gettempdir(), "pa-hook-stop-{}".format(re.sub(r"[^A-Za-z0-9_-]", "", str(data.get("session_id", "none")))))
        count = int(pa.read_text(counter).strip() or 0) + 1
        with open(counter, "w") as f:
            f.write(str(count))
        if count > 3:
            print(message + "\n(Held this session open 3 times already; letting it stop. Report the failure.)", file=sys.stderr)
            return 1
    block(message)


def hook_test_lock(data, _args):
    record = find_task(data.get("cwd") or os.getcwd())
    path = (data.get("tool_input") or {}).get("file_path")
    if not record or not record.get("fix") or not path:
        return 0
    rel = os.path.relpath(os.path.realpath(path), record["root"])
    if TEST_FILE.search(rel):
        block("Test files are locked during a fix task ({}): {} was not changed. Fix the code so the existing "
              "failing test passes; if the test itself is wrong, stop and report that.".format(record["task"], rel))
    return 0


def hook_guard(data, args):
    """The guard fails closed: anything that goes wrong in it refuses the call (block() exits through SystemExit)."""
    try:
        if not isinstance(data, dict) or not data.get("tool_name"):
            # Malformed or empty hook input (hook_input gives {}): nothing can be checked, so nothing runs.
            raise ValueError("no tool call in the hook input")
        return _hook_guard(data, args)
    except Exception as e:  # noqa: BLE001 - card d-038: every failure refuses
        block(GUARD_UNAVAILABLE.format("{}: {}".format(type(e).__name__, e)[:200]))


def guard_folders(data):
    """The folders a tool call may run in, and the task record (REVIEW-3 M5: the hook does not assume that its cwd is
    the Bash tool's folder). Tried in order: the reported cwd, the project folder, the hook's own folder; the first
    task record found wins, and its worktree root is one more folder."""
    candidates = [data.get("cwd"), os.environ.get("CLAUDE_PROJECT_DIR"), os.getcwd()]
    candidates = [c for c in dict.fromkeys(candidates) if c]
    for folder in candidates:
        record, agent, profile = hook_context(dict(data, cwd=folder))
        if record:
            return candidates + [record["root"]], (record, agent, profile)
    return candidates, hook_context(data)


def _hook_guard(data, _args):
    starts, (record, agent, profile) = guard_folders(data)
    tool, tool_input = data.get("tool_name"), data.get("tool_input") or {}
    extra_commands = [(p, "a release tool ({} profile)".format(profile.get("name"))) for p in (profile or {}).get("releaseTools", [])]
    extra_paths = [(p, "a secret ({} profile)".format(profile.get("name"))) for p in (profile or {}).get("secretPaths", [])]
    if tool == "Bash":
        command = tool_input.get("command") or ""
        for pattern, what in DENY_COMMANDS + extra_commands:
            if re.search(pattern, command):
                block("Blocked by the Project Agent safety hook: {}. {}".format(what, ROUTE))
        mains = ["main", "master", "trunk"] + list((profile or {}).get("mainBranches", []))
        if re.search(r"\bgit\s+push\b[^;&|]*[\s:/](" + "|".join(re.escape(b) for b in mains) + r")(\s|$)", command):
            block("Blocked by the Project Agent safety hook: a push to the main branch. " + ROUTE)
        if re.search(r"\bgit\s+push\b", command) and record:
            branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=record["root"], stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, universal_newlines=True).stdout.strip()
            if branch in mains:
                block("Blocked by the Project Agent safety hook: a push from the main branch ({}). {}".format(branch, ROUTE))
        # After the refusals above (unchanged): the denylist.
        refusal = _load_guard().check_command(command)
        if refusal:
            block(refusal)
    else:
        path = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("notebook_path") or ""
        for pattern, what in DENY_PATHS + extra_paths:
            if path and re.search(pattern, path):
                block("Blocked by the Project Agent safety hook: {} ({}). {}".format(what, path, ROUTE))
        if tool in EDIT_TOOLS:
            refusal = _load_guard().check_edit(path, os.path.join(pa.ROOT, "spaces"), cwd=starts, home=pa.ROOT)
            if refusal:
                block(refusal)
    return 0


HOOKS = {"lint-after-edit": hook_lint, "affected-tests": hook_affected, "test-file-lock": hook_test_lock, "guard": hook_guard}


# ---- pa-hooks install / remove ------------------------------------------------------------------

def hook_settings():
    run = lambda name: {"type": "command", "command": os.path.join("$HOME", ".local", "bin", "pa-hook") + " " + name}
    return {
        "PreToolUse": [{"matcher": "Edit|Write|MultiEdit", "hooks": [dict(run("test-file-lock"), timeout=10)]},
                       {"matcher": "Bash|Read|Edit|Write|MultiEdit|NotebookEdit", "hooks": [dict(run("guard"), timeout=10)]}],
        "PostToolUse": [{"matcher": "Edit|Write|MultiEdit", "hooks": [dict(run("lint-after-edit"), timeout=150)]}],
        "TaskCompleted": [{"hooks": [dict(run("affected-tests"), timeout=930)]}],
        "Stop": [{"hooks": [dict(run("affected-tests --stop"), timeout=930)]}],
    }


DENY_RULES = ["Read(.env)", "Read(.env.*)", "Read(**/.env)", "Read(**/.env.*)", "Read(~/.ssh/**)",
              "Bash(git push --force*)", "Bash(git push -f*)", "Bash(git reset --hard*)", "Bash(git clean -f*)"]


def ours(entry):
    return any(MARK in (h.get("command") or "") for h in entry.get("hooks", []))


def cmd_hooks(args):
    root = os.path.realpath(os.path.expanduser(args.worktree))
    if not os.path.isdir(root):
        raise pa.Problem("not a folder: " + root)
    path = os.path.join(root, SETTINGS)
    settings = pa.read_json(path, {}) or {}
    hooks = settings.setdefault("hooks", {})
    for event in list(hooks):
        hooks[event] = [e for e in hooks[event] if not ours(e)]
        if not hooks[event]:
            del hooks[event]
    task_path = os.path.join(root, TASK_FILE)
    if args.action == "remove":
        if not hooks:
            settings.pop("hooks", None)
        pa.backup(path, "pa-hooks")
        pa.write_json(path, settings)
        if os.path.exists(task_path):
            os.unlink(task_path)
        print("Project Agent hooks removed from " + root)
        return
    if not args.task:
        raise pa.Problem("pa-hooks install needs --task <id>")
    space = pa.local_id(args.space or space_from_terminal() or "")
    if not space:
        raise pa.Problem("pass --space <id>, or run this from the Project Agent's terminal")
    load_context(space)
    for event, entries in hook_settings().items():
        hooks.setdefault(event, []).extend(entries)
    deny = settings.setdefault("permissions", {}).setdefault("deny", [])
    deny.extend(r for r in DENY_RULES if r not in deny)
    pa.backup(path, "pa-hooks")
    pa.write_json(path, settings)
    pa.write_json(task_path, {"task": args.task, "space": space, "fix": bool(args.fix), "installed": pa.now()})
    # Keep both files out of commits without touching the repo's tracked ignore file.
    try:
        exclude = subprocess.run(["git", "rev-parse", "--git-path", "info/exclude"], cwd=root, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, universal_newlines=True).stdout.strip()
        if exclude:
            exclude = os.path.join(root, exclude)
            have = pa.read_text(exclude)
            # .playwright-mcp/: page snapshots a browser tool leaves in the folder it runs in.
            missing = [p for p in (SETTINGS, TASK_FILE, ".playwright-mcp/") if p not in have]
            if missing:
                os.makedirs(os.path.dirname(exclude), exist_ok=True)
                with open(exclude, "a") as f:
                    f.write(("" if have.endswith("\n") or not have else "\n") + "\n".join(missing) + "\n")
    except OSError:
        pass
    print("Project Agent hooks installed in {} for task {}{}.".format(root, args.task, " (fix task: test files locked)" if args.fix else ""))


# ---- pa-gate ----------------------------------------------------------------------------------

def applies(rule, task, phase):
    target = str(rule.get("appliesTo") or "all").lower()
    return target in ("all", phase, str(task.get("type", "")).lower(), str(task.get("tier", "")).lower() + " tier")


def gate_commits(worktree, runner=None):
    """The pin: the commit(s) a gate run saw in a task's worktree. One entry when the folder is a git top level,
    one for each src/<name> child that is (a folder of packages), else []. Never raises; records, never fails.
    runner: how to run git, with git()'s signature and result (default: git(), the module's own)."""
    git = globals()["git"] if runner is None else runner
    if not worktree or not os.path.isdir(worktree):
        return []
    root = os.path.realpath(worktree)

    def entry(path):
        code, top, _ = git(path, "rev-parse", "--show-toplevel")
        if code or not top or os.path.realpath(top) != path:
            return None
        code, common, _ = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if code or not common:  # git older than 2.31: no --path-format
            code, common, _ = git(path, "rev-parse", "--git-common-dir")
            common = os.path.join(path, common) if not code and common else None
        _c, branch, _ = git(path, "symbolic-ref", "--short", "-q", "HEAD")
        code, commit, _ = git(path, "rev-parse", "--verify", "-q", "HEAD")
        _c, status, _ = git(path, "--no-optional-locks", "status", "--porcelain", "--untracked-files=no")
        # "XY path" (git() strips the output, so the first line may have lost a leading space)
        changed = [l for l in status.splitlines() if l.strip() and not l.split(None, 1)[-1].strip('"').startswith(".claude/")]
        return {"repo": path, "common": os.path.realpath(common) if common else None, "branch": branch or None,
                "commit": commit if not code and commit else None, "uncommitted": len(changed)}

    one = entry(root)
    if one:
        return [one]
    src = os.path.join(root, "src")
    if not os.path.isdir(src):
        return []
    found = []
    for name in sorted(os.listdir(src)):
        path = os.path.join(src, name)
        if os.path.isdir(path) and not os.path.islink(path):
            e = entry(path)
            if e:
                found.append(e)
    return found


def evaluate_gate(directory, task, phase, rules=None, runner=None):
    """The gate's verdict for one ledger task, as GATE.json holds it. Reads only: writes and prints nothing.
    rules: the space's rules list (default: read from rules.json in directory). runner: how gate_commits runs
    git (default: git())."""
    tier = str(task.get("tier") or "normal").lower()
    code = bool(task.get("worktree")) or str(task.get("type", "")).lower() in ("feature", "bug", "chore", "code")
    # Leaving `review` (the default) needs every earlier phase's artifact too.
    needed = []
    for p in PHASES[:PHASES.index(phase) + 1] if phase == "review" else [phase]:
        for name in ARTIFACTS.get(p, []):
            if name == "spec.md" and tier == "low":
                continue  # low-tier tasks skip design
            needed.append(name)
    if code and phase in ("test", "review"):
        needed.append("build.log")
    if rules is None:
        rules = (pa.read_json(os.path.join(directory, "rules.json"), {}) or {}).get("rules", [])
    for rule in rules:
        if rule.get("status") == "active" and applies(rule, task, phase):
            needed.extend(str(x) for x in rule.get("evidence", []) if isinstance(x, str))
    evidence = os.path.join(directory, "evidence", task.get("id"))
    checks, ok = [], True
    for name in dict.fromkeys(needed):
        path = os.path.normpath(os.path.join(evidence, name))
        if not path.startswith(evidence + os.sep):
            checks.append({"check": name, "ok": False, "why": "outside the evidence folder"})
        elif not os.path.isfile(path):
            checks.append({"check": name, "ok": False, "why": "missing"})
        elif not pa.read_text(path).strip():
            checks.append({"check": name, "ok": False, "why": "empty"})
        else:
            checks.append({"check": name, "ok": True})
    if phase == "review":
        # Only a worker with an address is a reviewing worker or an author; roles are compared in lower case (card d-077).
        workers = [w for w in task.get("workers") or [] if isinstance(w, dict) and isinstance(w.get("address"), str) and w["address"].strip()]
        role = lambda w: str(w.get("role") or "").lower()
        authors = {w["address"] for w in workers if role(w) in ("build", "design")}
        # A `check` worker (verifier and reviewer in one) counts as the reviewer for low tier only (t-017 R4,
        # AC4.3); at any tier, a reviewer or check worker that is also an author fails.
        counted = ("reviewer", "check") if tier == "low" else ("reviewer",)
        reviewers = {w["address"] for w in workers if role(w) in counted}
        both = [w for w in workers if role(w) in ("reviewer", "check") and w["address"] in authors]
        separate = bool(reviewers) and not both
        if both:
            why = "{} is recorded as {} and as a build or design worker".format(both[0].get("address"), both[0].get("role"))
        elif tier == "low":
            why = "the ledger lists no reviewer or check worker separate from the build workers"
        else:
            why = "the ledger lists no reviewer worker separate from the build workers (a check worker counts for low tier only)"
        checks.append({"check": "reviewer is not the author", "ok": separate, **({} if separate else {"why": why})})
    ok = all(c["ok"] for c in checks)
    return {"task": task.get("id"), "phase": phase, "tier": tier, "passed": ok, "at": pa.now(), "checks": checks,
            "commits": gate_commits(task.get("worktree"), runner)}


def cmd_gate(args):
    record = find_task(os.getcwd())
    space = pa.local_id(args.space or (record or {}).get("space") or space_from_terminal() or "")
    if not space:
        base = os.path.join(pa.ROOT, "spaces")
        owners = [n for n in (os.listdir(base) if os.path.isdir(base) else [])
                  if any(t.get("id") == args.task for t in (pa.read_json(os.path.join(base, n, "ledger.json"), {}) or {}).get("tasks", []))]
        if len(owners) != 1:
            raise pa.Problem("pass --space <id> ({} spaces have a task {})".format(len(owners), args.task))
        space = owners[0]
    directory, _agent, _profile = load_context(space)
    ledger = pa.read_json(os.path.join(directory, "ledger.json"), {}) or {}
    task = next((t for t in ledger.get("tasks", []) if t.get("id") == args.task), None)
    if not task:
        raise pa.Problem("no task {} in {}".format(args.task, os.path.join(directory, "ledger.json")))
    phase = (args.phase or "review").lower()
    if phase not in PHASES:
        raise pa.Problem("unknown phase {!r}; one of {}".format(phase, ", ".join(PHASES)))
    result = evaluate_gate(directory, task, phase)
    evidence, checks, ok = os.path.join(directory, "evidence", args.task), result["checks"], result["passed"]
    if os.path.isdir(evidence):
        pa.write_json(os.path.join(evidence, "GATE.json"), result)
    for c in checks:
        print("{}  {}{}".format("ok  " if c["ok"] else "FAIL", c["check"], "" if c["ok"] else "  ({})".format(c["why"])))
    print("pa-gate {} ({} phase, {} tier): {}".format(args.task, phase, result["tier"], "PASSED" if ok else "FAILED. The task does not move."))
    sys.exit(0 if ok else 1)


# ---- pa-worktree ------------------------------------------------------------------------------

GIT_TIMEOUT = 20  # seconds, for each git call made while checking or describing a worktree


def git(path, *args, timeout=None):
    """(exit code, stdout, stderr), stripped. A call that runs past the timeout gives code 124."""
    limit = timeout or GIT_TIMEOUT
    try:
        r = subprocess.run(["git", "-C", path] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=limit)
    except subprocess.TimeoutExpired:
        return 124, "", "git {} timed out after {} s".format(args[0] if args else "", limit)
    except OSError as e:
        return 127, "", str(e)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def worktree_facts(path, profile=None):
    """What deleting this folder would cost. None if it is not a worktree we know how to remove:
    a linked git worktree, or the profile's own kind (a marker file plus status and remove scripts).
    {"unreadable": <why>} when it is one but its state cannot be read (git status or the profile's
    status script failed): never to be taken for gone or clean."""
    if not os.path.isdir(path):
        return None
    custom = (profile or {}).get("worktree") or {}
    if custom.get("marker") and custom.get("status") and custom.get("remove") and os.path.exists(os.path.join(path, custom["marker"])):
        script = lambda name: custom[name] if os.path.isabs(custom[name]) else os.path.join(profile["_folder"], custom[name])
        r = subprocess.run([script("status"), path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        try:
            facts = json.loads(r.stdout) if r.returncode == 0 else None
        except ValueError:
            facts = None
        if not facts:
            return {"unreadable": "the profile's worktree status script failed"}
        # "repos" (optional): the git repositories inside it, so its card can describe each one.
        repos = [os.path.realpath(r) for r in facts.get("repos") or [] if isinstance(r, str) and os.path.isabs(r)
                 and os.path.isdir(r) and inside(r, path)]
        return {"main": False, "branch": facts.get("branch", ""), "uncommitted": int(facts.get("uncommitted", 0)),
                "unpushed": int(facts.get("unpushed", 0)), "removesBranches": bool(facts.get("removesBranches")),
                "kind": facts.get("kind", "worktree"), "remove": script("remove"), "repos": repos}
    code, top, _ = git(path, "rev-parse", "--path-format=absolute", "--show-toplevel")
    code2, common, _ = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if code or code2 or os.path.realpath(top) != os.path.realpath(path):
        return None
    if os.path.realpath(common) == os.path.realpath(os.path.join(top, ".git")):
        return {"main": True}
    files = status_files(path)
    _c, branch, _ = git(path, "rev-parse", "--abbrev-ref", "HEAD")
    _c, ahead, _ = git(path, "rev-list", "--count", "HEAD", "--not", "--remotes")
    if files is None:
        return {"unreadable": "git status failed"}
    return {"main": False, "repo": os.path.dirname(os.path.realpath(common)), "branch": branch,
            "uncommitted": len(files), "unpushed": int(ahead) if ahead.isdigit() else 0}


def inside(path, folder):
    path, folder = os.path.realpath(path or "/nonexistent"), os.path.realpath(folder)
    return path == folder or path.startswith(folder + os.sep)


def worker_tree(task, entry=None):
    """The realpath of the worktree a task's worker works in, always one its own task records (pa.task_worktrees):
    the one its ledger entry's `worktree` (the folder it was started in) equals or lies inside, the deepest if
    several do. A field that names no worktree of the task (another task's, stale, mistyped, relative) counts as
    absent: the task's main worktree, else None."""
    trees = pa.task_worktrees(task)
    given = (entry or {}).get("worktree") if isinstance(entry, dict) else None
    if isinstance(given, str) and given.strip():
        given = os.path.realpath(os.path.expanduser(given))
        holding = [t["path"] for t in trees if inside(given, t["path"])]
        if holding:
            return max(holding, key=len)
    main = next((t for t in trees if t["main"]), None)
    return main["path"] if main else None


def worktree_state(directory, agent, profile=None):
    """Per worktree in the ledger (every one a task records, pa.task_worktrees): its tasks (with their states,
    titles and the job it is for each), its workers' fates, and anyone else working inside it. A task's worker
    belongs to the row of its own worktree (worker_tree). A worker's fate: live, closed (its ledger entry is
    finished), killed, removed or exited."""
    ledger = pa.read_json(os.path.join(directory, "ledger.json"), {}) or {}
    live = pa.live_terminals() or {}
    rows = {}
    for task in ledger.get("tasks", []):
        if not task.get("id"):
            continue
        for tree in pa.task_worktrees(task):
            path = tree["path"]
            row = rows.setdefault(path, {"path": path, "given": tree["given"], "tasks": [], "states": {}, "titles": {}, "jobs": {}, "mains": {},
                                         "jobRecorded": {}, "keep": False, "workers": []})
            row["tasks"].append(task["id"])
            row["states"][task["id"]] = task.get("state")
            row["titles"][task["id"]] = task.get("title") or ""
            row["jobs"][task["id"]], row["mains"][task["id"]] = tree["job"], tree["main"]
            row["jobRecorded"][task["id"]] = tree["jobRecorded"]
            row["keep"] = row["keep"] or tree["keep"]
            for w in task.get("workers") or []:
                if worker_tree(task, w) != path:
                    continue
                info = pa.hc_get(w.get("address"))
                fate = "live" if info.get("state") == "live" else ("closed" if w.get("finished") else
                                                                  "killed" if info.get("reason") == "killed" else
                                                                  "removed" if not info else "exited")
                row["workers"].append({"address": w.get("address"), "name": w.get("name") or info.get("name"), "fate": fate})
    for row in rows.values():
        mine = {w["address"] for w in row["workers"]}
        row["others"] = sorted(a for a, info in live.items() if a not in mine and a != agent.get("terminal") and (
            inside(info.get("cwd"), row["path"]) or inside((info.get("foreground") or {}).get("cwd"), row["path"])))
        row["facts"] = worktree_facts(row["path"], profile)
    return rows


# ---- what a worktree holds, for its Delete/Keep card (and WORKTREE.md) ----------------------------
# Read-only git calls, each with GIT_TIMEOUT. Any failure goes into "error"; the card opens anyway.

CARD_REPOS = 6  # repositories shown on the card
CARD_COMMITS = 8  # commits shown per repository
CARD_FILES = 10  # changed files shown per repository
CARD_UNCOMMITTED = 10  # uncommitted files shown per repository
SUBJECT = 72  # characters of a commit subject
PATH = 80  # characters of a file path (cut from the left)
CONTEXT_MAX = 2000  # characters of the card's context, a hard cap
MD_COMMITS, MD_FILES = 200, 500  # what WORKTREE.md lists per repository
DESCRIBE_REPOS = 20  # repositories described at all (each costs about 8 git calls)
NO_REPOS = "this kind of worktree lists no repositories; counts only"
SQUASH = "(a squash merge shows as not merged)"
DESCRIBE_DEADLINE = 60  # seconds for describing a whole worktree; repositories past it are named, not described
CONTROL = re.compile("[\x00-\x1f\x7f\x85\u2028\u2029]")


def clean(text):
    """Repository and ledger text as one visible line: control characters (a newline in a file name, a
    terminal escape) become escapes, so they cannot add lines or styling to a card or WORKTREE.md."""
    named = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
    return CONTROL.sub(lambda m: named.get(m.group(), "\\x{:02x}".format(ord(m.group())) if ord(m.group()) < 256
                                           else "\\u{:04x}".format(ord(m.group()))), str(text if text is not None else ""))


def md(text, limit=300):
    """clean() for WORKTREE.md, also safe inside a code span or a table cell."""
    return cut(clean(text), limit).replace("`", "'").replace("|", "\\|")


def cut(text, limit):
    text = str(text or "")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def cut_path(path, limit=PATH):
    path = str(path or "")
    return path if len(path) <= limit else "…" + path[-(limit - 1):]


def describe_repo(path, profile=None, name=None, deadline=None):
    """One git repository: its branch, the main branch it came from, the commits on the branch (merged
    into the main branch? pushed?), the files they change, and what is uncommitted (spec 4.3)."""
    repo = {"name": name or os.path.basename(path.rstrip(os.sep)), "path": path, "branch": None, "base": None, "fork": None,
            "commits": [], "commitsTotal": 0, "merged": 0, "pushed": 0, "files": [], "filesTotal": 0, "added": 0, "removed": 0,
            "uncommitted": [], "uncommittedTotal": 0, "error": None, "head": None, "fetched": None}

    def g(*args):  # every call within GIT_TIMEOUT, and within the whole description's deadline
        left = GIT_TIMEOUT if deadline is None else min(GIT_TIMEOUT, max(0.2, deadline - time.time()))
        return git(path, *args, timeout=left)

    def fail(err):
        repo["error"] = repo["error"] or (clean(str(err or "git failed").strip())[:200] or "git failed")

    code, branch, err = g("rev-parse", "--abbrev-ref", "HEAD")
    if code:
        fail(err)
        return repo
    repo["branch"] = clean(branch)
    repo["head"] = g("rev-parse", "HEAD")[1] or None
    # Merged and pushed come from local refs: say when they were last fetched.
    code, common, _ = g("rev-parse", "--path-format=absolute", "--git-common-dir")
    fetch_head = os.path.join(common, "FETCH_HEAD") if not code and common else None
    if fetch_head and os.path.isfile(fetch_head):
        repo["fetched"] = iso(os.path.getmtime(fetch_head))
    # The main branch: origin/HEAD's target, then main, master, trunk, then the profile's main branches.
    names = []
    code, target, _ = g("symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if not code and target:
        names.append(target)
    names += ["main", "master", "trunk"]
    for b in (profile or {}).get("mainBranches") or []:
        names += [b, "origin/" + b]
    bases = [n for n in dict.fromkeys(names) if n != branch and g("rev-parse", "--verify", "--quiet", n + "^{commit}")[0] == 0]
    repo["base"] = bases[0] if bases else None
    # Where the branch began: its creation in the reflog (still right after a fast-forward merge), else the merge-base.
    fork = None
    if branch != "HEAD":
        code, out, _ = g("reflog", "show", "--format=%H", "refs/heads/" + branch)
        oldest = out.splitlines()[-1].strip() if not code and out else None
        if oldest and g("merge-base", "--is-ancestor", oldest, "HEAD")[0] == 0:
            fork = oldest
    if not fork and bases:
        code, out, err = g("merge-base", "HEAD", bases[0])
        fork = out if not code and out else None
        if code:
            fail(err)
    if fork:
        repo["fork"] = g("rev-parse", "--short", fork)[1] or fork[:7]
        code, count, _ = g("rev-list", "--count", fork + "..HEAD")
        code, out, err = g("log", "-n", str(MD_COMMITS + 1), "--format=%H%x1f%h%x1f%s", fork + "..HEAD")
        if code:
            fail(err)
        rows = [line.split("\x1f", 2) for line in out.splitlines() if line.count("\x1f") >= 2] if not code else []
        repo["commitsTotal"] = max(int(count) if count.isdigit() else 0, len(rows))
        unmerged = []  # per main-branch candidate: the branch commits it does not contain
        for b in bases:
            code, out, _ = g("rev-list", "HEAD", "--not", fork, b)
            if not code:
                unmerged.append(set(out.split()))
        picked = set()  # commits whose patch is already in the main branch (rebased or picked)
        if repo["base"] and repo["commitsTotal"] <= MD_COMMITS:
            code, out, _ = g("cherry", repo["base"], "HEAD", fork)
            picked = {line[2:].strip() for line in out.splitlines() if line.startswith("- ")} if not code else set()
        code, out, err = g("rev-list", "HEAD", "--not", "--remotes")
        if code:
            fail(err)
        unpushed = set(out.split())
        for full, short, subject in rows:
            merged = (any(full not in u for u in unmerged) or full in picked) if unmerged else None
            repo["commits"].append({"sha": short, "subject": clean(subject), "merged": merged, "pushed": not code and full not in unpushed})
        repo["merged"] = sum(1 for c in repo["commits"] if c["merged"])
        repo["pushed"] = sum(1 for c in repo["commits"] if c["pushed"])
        code, out, err = g("diff", "--numstat", "-z", "-M", fork, "HEAD")
        if code:
            fail(err)
        parts, i = (out.split("\x00") if not code else []), 0
        while i < len(parts):
            bits = parts[i].split("\t", 2)
            i += 1
            if len(bits) < 3:
                continue
            added, removed, name_ = bits
            if not name_:  # a rename: the old and the new path follow
                name_ = parts[i + 1] if i + 1 < len(parts) else ""
                i += 2
            added = int(added) if added.isdigit() else None
            removed = int(removed) if removed.isdigit() else None
            repo["filesTotal"] += 1
            repo["added"] += added or 0
            repo["removed"] += removed or 0
            if len(repo["files"]) < MD_FILES:
                repo["files"].append({"path": clean(name_), "added": added, "removed": removed})
    elif not repo["error"]:
        fail("no main branch found")
    files = status_files(path)
    if files is None:
        fail("git status failed")
    else:
        repo["uncommittedTotal"] = len(files)
        repo["uncommitted"] = [{"path": clean(f), "status": st} for st, f in files[:MD_FILES]]
    return repo


def describe_worktree(row, facts, profile):
    """The tasks using the worktree and each repository in it (spec 4.2, 4.4)."""
    desc = {"tasks": [{"id": clean(t), "title": clean(row.get("titles", {}).get(t, "")), "state": clean(row.get("states", {}).get(t))}
                      for t in row["tasks"]],
            "repos": [], "notDescribed": [], "notDescribedWhy": {}, "note": None, "error": None, "kind": clean(facts.get("kind") or "git worktree")}
    deadline = time.time() + DESCRIBE_DEADLINE
    if facts.get("remove"):  # the profile's own kind: its status script names the repositories, if it can
        paths = facts.get("repos") or []
        if not paths:
            desc["note"] = NO_REPOS
        names = [None] * len(paths)
    else:
        paths, names = [row["path"]], [os.path.basename(facts.get("repo") or row["path"])]
    for i, path in enumerate(paths):
        why = ("more than {} repositories".format(DESCRIBE_REPOS) if i >= DESCRIBE_REPOS else
               "time limit of {:g} s".format(DESCRIBE_DEADLINE) if time.time() >= deadline else None)
        if why:
            desc["notDescribed"].append(clean(os.path.basename(path)))
            desc["notDescribedWhy"].setdefault(why, []).append(clean(os.path.basename(path)))
            continue
        try:
            desc["repos"].append(describe_repo(path, profile, names[i], deadline))
        except Exception as e:  # noqa: BLE001 - a description never stops the card
            desc["repos"].append(dict(name=names[i] or os.path.basename(path), path=path, branch=None, base=None, fork=None,
                                      commits=[], commitsTotal=0, merged=0, pushed=0, files=[], filesTotal=0, added=0, removed=0,
                                      uncommitted=[], uncommittedTotal=0, error=clean(e)[:200], head=None, fetched=None))
    return desc


def counts(added, removed):
    text = " ".join(x for x in ("+{}".format(added) if added else "", "−{}".format(removed) if removed else "") if x)
    return text


def unchanged(repo):
    return not repo["commitsTotal"] and not repo["filesTotal"] and not repo["uncommittedTotal"] and not repo["error"]


def merged_word(c):
    return "merged: unknown" if c["merged"] is None else "merged" if c["merged"] else "not merged"


def repo_lines(r):
    head = "{}: branch {}".format(r["name"], r["branch"] or "?")
    if r["fork"]:
        n = r["commitsTotal"]
        head += ", from {} at {}: {} commit{}, {}, {} pushed".format(
            r["base"] or "its start", r["fork"], n, "" if n == 1 else "s",
            "{} merged into {}".format(r["merged"], r["base"]) if r["base"] else "merged: unknown", r["pushed"])
    lines = [head]
    if r["commitsTotal"]:
        lines.append("  merged and pushed are from local refs; last fetch: {}".format(r.get("fetched") or "never"))
    shown = r["commits"][:CARD_COMMITS]
    for c in shown:
        lines.append("  {} {}  ({}, {})".format(c["sha"], cut(c["subject"], SUBJECT), merged_word(c), "pushed" if c["pushed"] else "not pushed"))
    if r["commitsTotal"] > len(shown):
        lines.append("  … {} shown, {} more".format(len(shown), r["commitsTotal"] - len(shown)))
    if r["filesTotal"]:
        listed = ["{} (binary)".format(cut_path(f["path"])) if f["added"] is None else
                  " ".join(x for x in (cut_path(f["path"]), counts(f["added"], f["removed"])) if x) for f in r["files"][:CARD_FILES]]
        more = r["filesTotal"] - len(listed)
        lines.append("  Files changed: {} ({}): {}{}".format(r["filesTotal"], counts(r["added"], r["removed"]) or "+0",
                                                          ", ".join(listed), ", … {} more".format(more) if more > 0 else ""))
    if r["uncommittedTotal"]:
        listed = ["{} {}".format(u["status"], cut_path(u["path"])) for u in r["uncommitted"][:CARD_UNCOMMITTED]]
        more = r["uncommittedTotal"] - len(listed)
        lines.append("  Uncommitted: {}: {}{}".format(r["uncommittedTotal"], ", ".join(listed), ", … {} more".format(more) if more > 0 else ""))
    if r["error"]:
        lines.append("  could not read the changes: {}".format(r["error"]))
    return lines


def branch_loss(desc, facts):
    """When deleting also deletes the branches (removesBranches) and the repositories were described: what the
    branches' commits would come to (AC3.3). A commit merged into a main branch (the local one counts: it keeps
    the commit) or on a remote is kept. {"lost": n or None (unknown), "lostIn": [names], "lines": {index: line}};
    None when deleting keeps the branches or only counts are known."""
    if not facts.get("removesBranches") or not desc.get("repos"):
        return None
    out = {"lost": 0, "lostIn": [], "lines": {}}
    unknown = bool(desc.get("error") or desc.get("notDescribed"))
    for i, r in enumerate(desc["repos"]):
        n = r["commitsTotal"]
        lost = sum(1 for c in r["commits"] if c["merged"] is not True and not c["pushed"])
        if r["error"]:
            why = r["error"]
        elif len(r["commits"]) < n or n > MD_COMMITS:
            why = "more commits than described"
        elif any(c["merged"] is None and not c["pushed"] for c in r["commits"]):
            why = "merged unknown"
        else:
            why = None
        if why:
            unknown = True
            out["lines"][i] = "  unknown if deleted: {}; Keep is recommended".format(why)
        elif lost:
            out["lost"] += lost
            out["lostIn"].append(r["name"])
            out["lines"][i] = "  LOST if deleted: {} of {} commits neither merged nor on a remote".format(lost, n)
        elif n:
            out["lines"][i] = "  kept if deleted: all {} commits merged or pushed".format(n)
    if unknown:
        out["lost"] = None
    return out


def deleting_text(facts, loss=None):
    loses_commits = facts.get("removesBranches") and facts.get("unpushed", 0) > 0
    n = facts.get("uncommitted", 0)
    lost = "; the {} uncommitted files are DISCARDED".format(n) if n else ""
    if facts.get("removesBranches") and loss is not None:
        said = ("; whether every commit is merged or pushed is unknown" if loss["lost"] is None else
                "; {} commits ({}) are neither merged nor on a remote and would be LOST".format(loss["lost"], ", ".join(loss["lostIn"]))
                if loss["lost"] else "; all their commits are merged or pushed")
        return "Deleting also deletes its branches ({}){}{}.".format(facts.get("branch"), said, lost)
    if facts.get("removesBranches"):
        return "Deleting also deletes its branches ({}){}{}.".format(
            facts.get("branch"), "; the commits that are on no remote would be LOST" if loses_commits else "", lost)
    return "Deleting: the branch {} and its commits are kept{}.".format(facts.get("branch"), lost or "; nothing uncommitted is lost")


def card_context(desc, row, facts, gone, extra=(), md_file="WORKTREE.md"):
    """The card's context: one fact per line, at most CONTEXT_MAX characters (spec 4.5). extra: lines after the
    Tasks line (the job, rule (b)); md_file: the evidence file with the full list."""
    loss = branch_loss(desc, facts)
    lines = ["{} ({})".format(row["path"], desc.get("kind") or facts.get("kind") or "git worktree"),
             "Tasks: " + ", ".join('{} "{}" ({})'.format(t["id"], cut(t["title"], 60), t["state"]) for t in desc["tasks"])] + list(extra)
    if desc.get("error"):
        lines.append("could not read the changes: {}".format(desc["error"]))
    if desc.get("note") or desc.get("error"):
        lines += [desc["note"]] if desc.get("note") else []
        lines.append("Uncommitted files: {}. Commits not on any remote: {}.".format(facts.get("uncommitted", 0), facts.get("unpushed", 0)))
    changed = [(i, r) for i, r in enumerate(desc["repos"]) if not unchanged(r)]
    for i, r in changed[:CARD_REPOS]:
        lines += repo_lines(r)
        if loss and i in loss["lines"]:
            lines.append(loss["lines"][i])
    more = max(0, len(changed) - CARD_REPOS) + len(desc.get("notDescribed") or [])
    if more:
        lines.append("… and {} more repositories ({})".format(more, md_file))
    still = [r["name"] for r in desc["repos"] if unchanged(r)]
    if still:
        lines.append("Unchanged: " + ", ".join(still))
    lines += ["Workers: " + gone, "No other terminal is working in it.", deleting_text(facts, loss), "Full list: " + md_file]
    text = "\n".join(lines)
    if len(text) > CONTEXT_MAX:
        tail_ = "… (full list in {})".format(md_file)
        keep = text[:CONTEXT_MAX - len(tail_) - 1]
        text = (keep[:keep.rfind("\n")] if "\n" in keep else keep) + "\n" + tail_
    return text


def card_changes(desc):
    """The structured description on the card, with the card's caps; truncated when anything was cut."""
    truncated = len(desc["repos"]) > CARD_REPOS or bool(desc.get("notDescribed"))
    repos = []
    for r in desc["repos"][:CARD_REPOS]:
        truncated = truncated or r["commitsTotal"] > CARD_COMMITS or r["filesTotal"] > CARD_FILES or r["uncommittedTotal"] > CARD_UNCOMMITTED \
            or any(len(c["subject"]) > SUBJECT for c in r["commits"][:CARD_COMMITS])
        repos.append(dict(r, commits=[dict(c, subject=cut(c["subject"], SUBJECT)) for c in r["commits"][:CARD_COMMITS]],
                          files=r["files"][:CARD_FILES], uncommitted=r["uncommitted"][:CARD_UNCOMMITTED]))
    return {"tasks": desc["tasks"], "repos": repos, "note": desc.get("note"), "error": desc.get("error"), "truncated": truncated, "at": pa.now()}


def worktree_md(desc, row, facts, gone, card_id, extra=()):
    """WORKTREE.md (WORKTREE-<job>.md for a job's card): the same sections as the card, with the larger caps
    (spec 4.6). Every piece of repository or ledger text goes through md(), so it stays inside its own line,
    code span or cell."""
    loss = branch_loss(desc, facts)
    out = ["# Worktree {}".format(md(row["path"], 1000)), "", "{}. Written {} with decision card {}.".format(
        md(desc.get("kind") or "git worktree"), pa.now(), card_id), "", "## Tasks", ""]
    out += ['- {} "{}" ({})'.format(md(t["id"]), md(t["title"]), md(t["state"])) for t in desc["tasks"]]
    out += ([""] + [md(line, 1000) for line in extra]) if extra else []
    if desc.get("note") or desc.get("error"):
        out += [""] + ([desc["note"]] if desc.get("note") else []) + (["Could not read the changes: " + md(desc["error"])] if desc.get("error") else [])
        out.append("Uncommitted files: {}. Commits not on any remote: {}.".format(facts.get("uncommitted", 0), facts.get("unpushed", 0)))
    for i, r in enumerate(desc["repos"]):
        out += ["", "## {}".format(md(r["name"])), "", "`{}`: branch `{}`{}.".format(md(r["path"], 1000), md(r["branch"] or "?"),
                ", from `{}` at `{}`".format(md(r["base"] or "its start"), md(r["fork"])) if r["fork"] else "")]
        if loss and i in loss["lines"]:
            out += ["", md(loss["lines"][i].strip(), 1000) + "."]
        if r["commitsTotal"]:
            out += ["", "Merged and pushed are from local refs; last fetch: {}.".format(r.get("fetched") or "never")]
        if r["error"]:
            out += ["", "Could not read the changes: {}".format(md(r["error"]))]
        out += ["", "### Commits ({}, {} merged, {} pushed)".format(r["commitsTotal"], r["merged"], r["pushed"]), ""]
        out += ["- `{}` {} ({}, {})".format(md(c["sha"]), md(c["subject"]), merged_word(c), "pushed" if c["pushed"] else "not pushed")
                for c in r["commits"][:MD_COMMITS]] or ["- none"]
        if r["commitsTotal"] > MD_COMMITS:
            out.append("- … {} more".format(r["commitsTotal"] - MD_COMMITS))
        out += ["", "### Files changed ({}, {})".format(r["filesTotal"], counts(r["added"], r["removed"]) or "+0"), ""]
        if r["files"]:
            out += ["| File | Added | Removed |", "|---|---|---|"] + ["| `{}` | {} | {} |".format(
                md(f["path"]), "binary" if f["added"] is None else f["added"], "" if f["removed"] is None else f["removed"]) for f in r["files"]]
            if r["filesTotal"] > len(r["files"]):
                out += ["", "… {} more".format(r["filesTotal"] - len(r["files"]))]
        else:
            out.append("None.")
        out += ["", "### Uncommitted ({})".format(r["uncommittedTotal"]), ""]
        out += ["- `{}` {}".format(md(u["status"]), md(u["path"])) for u in r["uncommitted"]] or ["None."]
    for why, names in (desc.get("notDescribedWhy") or {}).items():
        out += ["", "Not described ({}): {}".format(why, ", ".join(md(n) for n in names))]
    out += ["", "## Workers", "", md(gone, 2000), "", "## Deleting", "", md(deleting_text(facts, loss), 2000), ""]
    return "\n".join(out)


def card_id(task, job, main=True):
    """The Delete/Keep card of a worktree: d-wt-<task> for the task's main one (cards from before t-020 stay valid),
    d-wt-<task>-<job> for any other."""
    return "d-wt-{}".format(task) if main else "d-wt-{}-{}".format(task, job)


def md_name(job, main=True):
    """The evidence file with a worktree card's full list: WORKTREE.md, or WORKTREE-<job>.md for a job's card."""
    return "WORKTREE.md" if main else "WORKTREE-{}.md".format(job)


def card_path(directory, task, job=None, main=True):
    return os.path.join(directory, "decisions", card_id(task, job, main) + ".json")


def card_owner(row):
    """(task, job, main) whose card a worktree row gets: the first task whose main worktree it is, else the row's
    first task with the job it records it for. check and remove both use this, so they agree on the card."""
    task = next((t for t in row["tasks"] if row["mains"].get(t)), None)
    if task:
        return task, row["jobs"][task], True
    return row["tasks"][0], row["jobs"][row["tasks"][0]], False


def in_worktrees_folder(path):
    """True when the worktree sits in a folder named `worktrees`: <project>/worktrees/<container>/<repo> or
    <workspace>/worktrees/<name>. A job's worktree elsewhere is neither offered nor removed (D4)."""
    parent = os.path.dirname(path)
    return os.path.basename(parent) == "worktrees" or os.path.basename(os.path.dirname(parent)) == "worktrees"


def not_over(desc):
    """Why a described worktree's job is not over under rule (b), or None when it is: every repository has at least
    one commit since its fork, all of them merged into a main branch (pushed is not enough while the task is in
    progress), nothing uncommitted, and the whole of it described (spec R2, AC2.1, AC2.2)."""
    if desc.get("error"):
        return "cannot tell ({})".format(desc["error"])
    if desc.get("note"):
        return "cannot tell ({})".format(desc["note"])
    if desc.get("notDescribed"):
        return "cannot tell (not every repository was described)"
    if not desc.get("repos"):
        return "cannot tell (no repository in it)"
    for r in desc["repos"]:
        n = r["commitsTotal"]
        if r["error"]:
            return "cannot tell ({})".format(r["error"])
        if not n:
            return "no commits since its fork"
        if len(r["commits"]) < n or n > MD_COMMITS:
            return "cannot tell (more commits than described)"
        if r["uncommittedTotal"]:
            return "{} uncommitted file(s)".format(r["uncommittedTotal"])
        if any(c["merged"] is None for c in r["commits"]):
            return "merged unknown"
        k = sum(1 for c in r["commits"] if c["merged"] is not True)
        if k:
            return "{} of {} commits not merged into {}".format(k, n, r["base"] or "the main branch")
    return None


def job_over(row, facts, profile):
    """Rule (b): (True, None, desc) when the worktree's job is over although a task using it is not done or parked;
    else (False, why, desc or None). The cheap checks come first; describing (git calls) only when they pass."""
    live = [w["address"] for w in row["workers"] if w["fate"] == "live"]
    if live:
        return False, "in use by " + ", ".join(live), None
    if row["others"]:
        return False, "another terminal is working in it: " + ", ".join(row["others"]), None
    if facts.get("uncommitted"):
        return False, "{} uncommitted file(s)".format(facts["uncommitted"]), None
    try:
        desc = describe_worktree(row, facts, profile)
    except Exception as e:  # noqa: BLE001 - unknown is never over
        return False, "cannot tell ({})".format(clean(e)[:200]), None
    why = not_over(desc)
    return why is None, why, desc


def withdraw_card(directory, path, card, task, why):
    card.update(status="withdrawn", answer=None, note="Withdrawn by pa-worktree check: " + why, answered=pa.now(),
                answeredBy="pa-worktree check: withdrawn")
    pa.write_json(path, card)
    append_journal(directory, {"by": "agent" if by_an_agent() else "person", "trigger": "worktree", "task": task,
                               "why": "withdrew card {}: {}".format(card["id"], why)})
    print("{}: withdrew card {}: {}".format(task, card["id"], why))


def open_worktree_card(directory, row, facts, profile, owner, rule, gone, desc=None):
    """Writes the Delete/Keep card of one worktree and its evidence file; returns the recommendation."""
    task, job, main = owner
    named = rule == "b" or not main or job != "main"
    extra = []
    if named:
        extra.append("Job: {}{}".format(clean(job), "" if row["jobRecorded"].get(task, True) else " (named from its folder; not recorded)"))
    if rule == "b":
        extra.append("{}; this job's commits are all merged".format(
            "Task still {}".format(clean(row["states"].get(task))) if len(row["tasks"]) == 1 else "Tasks still " + tasks_still(row)))
    if desc is None:
        try:
            desc = describe_worktree(row, facts, profile)
        except Exception as e:  # noqa: BLE001 - describing never stops the card (AC7.5)
            desc = {"tasks": [{"id": clean(t), "title": clean(row["titles"].get(t, "")), "state": clean(row["states"].get(t))} for t in row["tasks"]],
                    "repos": [], "notDescribed": [], "notDescribedWhy": {}, "note": None, "error": clean(e)[:200],
                    "kind": clean(facts.get("kind") or "git worktree")}
    loss = branch_loss(desc, facts)
    loses_commits = facts.get("removesBranches") and (loss["lost"] != 0 if loss else facts["unpushed"] > 0)
    dirty = facts["uncommitted"] > 0 or loses_commits
    cid, md_file = card_id(task, job, main), md_name(job, main)
    evidence = []
    try:
        pa.write_text(os.path.join(directory, "evidence", task, md_file), worktree_md(desc, row, facts, gone, cid, extra))
        evidence = [md_file]
    except OSError:
        pass
    squash = (" " + SQUASH if any(c["merged"] is False for r in desc["repos"] for c in r["commits"]) else "")
    titles = ", ".join('{} "{}"'.format(clean(t), cut(clean(row["titles"].get(t, "")), 60)) for t in row["tasks"])
    pa.write_json(card_path(directory, task, job, main), {
        "id": cid, "task": task, "worktree": row["path"], "job": job, "main": main, "rule": rule,
        "question": "Delete the worktree for {}{}?".format(titles, ", job {}".format(clean(job)) if named else ""),
        "context": card_context(desc, row, facts, gone, extra, md_file),
        "evidence": evidence,
        "changes": card_changes(desc),
        "options": [{"label": "Delete", "detail": "Remove the {}{}.{}".format(facts.get("kind", "worktree folder"),
                     " with everything listed above that is not saved elsewhere" if dirty else "", squash)},
                    {"label": "Keep", "detail": "Leave the worktree. It will not be asked about again."}],
        "recommendation": "Keep" if dirty else "Delete", "status": "open", "answer": None, "note": None,
        "snapshot": {"uncommitted": facts["uncommitted"], "unpushed": facts["unpushed"],
                     "commits": sum(r["commitsTotal"] for r in desc["repos"]),
                     # what pa-worktree remove compares: each repository's HEAD and its uncommitted files
                     "heads": {r["path"]: r["head"] for r in desc["repos"] if r.get("head")},
                     "uncommittedFiles": {r["path"]: [u["path"] for u in r["uncommitted"]] for r in desc["repos"]
                                          if r.get("head") and r["uncommittedTotal"] == len(r["uncommitted"])}},
        "created": pa.now(), "answered": None})
    print("{}: opened card {} (recommends {})".format(task, cid, "Keep" if dirty else "Delete"))


def remove_command(task, job, main):
    return "pa-worktree remove {}{}".format(task, "" if main else " --job " + job)


def cmd_worktree(args):
    record = find_task(os.getcwd())
    space = pa.local_id(args.space or (record or {}).get("space") or space_from_terminal() or "")
    if not space:
        raise pa.Problem("pass --space <id>, or run this from the Project Agent's terminal")
    directory, agent, profile = load_context(space)
    rows = worktree_state(directory, agent, profile)
    if args.action == "check":
        for row in rows.values():
            owner = card_owner(row)
            task, job, main = owner
            facts, path = row["facts"], card_path(directory, task, job, main)
            card = pa.read_json(path)
            if card and card.get("status") == "withdrawn":
                card = None  # asked again once its job is over or every task is done or parked
            fates = [w["fate"] for w in row["workers"]]
            still = tasks_still(row)
            gone = ", ".join("{} ({})".format(clean(w["name"] or w["address"]), w["fate"]) for w in row["workers"]) or "no workers recorded"
            if not facts:
                print("{}: gone or not a git worktree ({})".format(task, row["path"]))
            elif facts.get("unreadable"):
                print("{}: could not read {} ({}); asked once it can be read".format(task, row["path"], facts["unreadable"]))
            elif facts.get("main"):
                print("{}: {} is the project's main checkout; never removed".format(task, row["path"]))
            elif row["keep"]:
                print("{}: kept (keepWorktree is set)".format(task))
            elif not main and not in_worktrees_folder(row["path"]):
                print("{}: {} is not under a worktrees folder; not offered".format(task, row["path"]))
            elif card and card.get("status") == "answered":
                delete = str(card.get("answer", "")).lower().startswith("delete")
                if delete and still and card.get("rule") == "b":
                    print("{}: card {} answered {!r}: run `{}`; it re-checks".format(task, card["id"], card.get("answer"), remove_command(*owner)))
                elif delete and still:
                    print("{}: card {} answered {!r}, but tasks still {}; pa-worktree remove refuses until every task using it is "
                          "done or parked".format(task, card["id"], card.get("answer"), still))
                else:
                    print("{}: card {} answered {!r}{}".format(task, card["id"], card.get("answer"),
                          ": run `{}`".format(remove_command(*owner)) if delete else ": kept, not asked again"))
            elif card and still and card.get("rule") == "b":
                over, why, _desc = job_over(row, facts, profile)
                if over:
                    print("{}: card {} is open".format(task, card["id"]))
                elif not args.open_cards:
                    print("{}: card {} is open, but the job is no longer over ({}); --open-cards withdraws it".format(task, card["id"], why))
                else:
                    withdraw_card(directory, path, card, task, "the job is no longer over: " + why)
            elif card and still and not args.open_cards:
                print("{}: card {} is open, but tasks still {}; --open-cards withdraws it".format(task, card["id"], still))
            elif card and still:
                # A task was reopened after the card opened: the question no longer stands.
                withdraw_card(directory, path, card, task,
                              "tasks still {}; it is asked again once every task using the worktree is done or parked".format(still))
            elif card:
                print("{}: card {} is open".format(task, card["id"]))
            elif still:
                over, why, desc = job_over(row, facts, profile)
                if not over:
                    print("{}: tasks still {}; asked once every task using it is done or parked; job {} not over yet ({})".format(
                        task, still, clean(job), why))
                elif not args.open_cards:
                    print("{}: candidate, job {} is over (every commit merged); --open-cards asks the person".format(task, clean(job)))
                else:
                    open_worktree_card(directory, row, facts, profile, owner, "b", gone, desc)
            elif "live" in fates:
                print("{}: in use by {}".format(task, ", ".join(w["address"] for w in row["workers"] if w["fate"] == "live")))
            elif row["others"]:
                print("{}: another terminal is working in it ({})".format(task, ", ".join(row["others"])))
            elif not args.open_cards:
                # Every task is done or parked and nothing runs inside: a candidate, however its workers ended.
                print("{}: candidate, every task done or parked ({}); --open-cards asks the person".format(task, gone))
            else:
                open_worktree_card(directory, row, facts, profile, owner, "a", gone)
        if not rows:
            print("No task in the ledger has a worktree.")
        return
    # remove: only what an answered card allows, re-checked now.
    row = pick_row(rows, args.task, args.job)
    owner = card_owner(row)
    task, job, main = owner
    path = card_path(directory, task, job, main)
    card = pa.read_json(path) or {}
    if card.get("status") != "answered" or not str(card.get("answer", "")).lower().startswith("delete"):
        raise pa.Problem("no answered \"Delete\" decision card for this worktree (run `pa-worktree check --open-cards`; the person answers it)")
    if os.path.realpath(card.get("worktree") or "") != row["path"]:
        raise pa.Problem("the card is for {}, not {}".format(card.get("worktree"), row["path"]))
    facts = row["facts"]
    if not facts:
        print("Already gone: " + row["path"])
        return
    if facts.get("unreadable"):
        raise pa.Problem("could not read {} ({}); nothing was removed".format(row["path"], facts["unreadable"]))
    if facts.get("main"):
        raise pa.Problem("{} is the project's main checkout; it is never removed".format(row["path"]))
    if row["keep"]:
        raise pa.Problem("keepWorktree is set on the task")
    if not main and not in_worktrees_folder(row["path"]):
        raise pa.Problem("{} is not under a worktrees folder; nothing was removed".format(row["path"]))
    still = tasks_still(row)
    if still and card.get("rule") != "b":
        raise pa.Problem("tasks still {}; a worktree is removed only once every task using it is done or parked. Nothing was removed".format(still))
    if still:
        over, why, _desc = job_over(row, facts, profile)
        if not over:
            raise pa.Problem("tasks still {} and job {} is not over ({}); nothing was removed".format(still, clean(job), why))
    busy = [w["address"] for w in row["workers"] if w["fate"] == "live"] + row["others"]
    if busy:
        raise pa.Problem("still in use by {}; nothing was removed".format(", ".join(busy)))
    snapshot = card.get("snapshot") or {}
    changed = None
    if facts["uncommitted"] > snapshot.get("uncommitted", 0):
        changed = "there are more uncommitted files now ({}) than when the person answered ({})".format(
            facts["uncommitted"], snapshot.get("uncommitted", 0))
    elif facts.get("removesBranches") and facts["unpushed"] > snapshot.get("unpushed", 0):
        changed = "there are more unpushed commits now ({}) than when the person answered ({})".format(
            facts["unpushed"], snapshot.get("unpushed", 0))
    else:
        changed = changed_since_card(snapshot)
    if changed:
        # The person answered about a worktree that is no longer the same: ask again with a fresh card.
        why = "the worktree changed since the person answered {!r}: {}".format(card.get("answer"), changed)
        card.update(status="withdrawn", previousAnswer=card.get("answer"), answer=None, answered=pa.now(),
                    answeredBy="pa-worktree remove: withdrawn", note="Withdrawn by pa-worktree remove: " + why)
        pa.write_json(path, card)
        append_journal(directory, {"by": "agent" if by_an_agent() else "person", "trigger": "worktree", "task": task,
                                   "why": "withdrew card {}: {}".format(card["id"], why)})
        raise pa.Problem(changed + "; the card is withdrawn and the next `pa-worktree check --open-cards` asks again. Nothing was removed")
    try:
        done = remove_worktree(row["path"], facts)
    except pa.Problem:
        if not os.path.lexists(row["path"]):  # it failed, but the folder is gone: the ledger must not name it
            mark_removed_everywhere(directory, row, "the worktree {} is gone after a failed removal (card {})".format(row["path"], card["id"]))
        raise
    mark_removed_everywhere(directory, row, "removed the worktree {} (card {})".format(row["path"], card["id"]))
    print(done + " (card {})".format(card["id"]))
    tidy = remove_container(row["given"])
    if tidy:
        print(tidy)


def pick_row(rows, task, job=None):
    """The row `pa-worktree remove <task> [--job <job>]` means: the job's worktree, else the task's main one, else its
    only one. Refuses (nothing removed) when there is none, or several and no main one."""
    mine = [r for r in rows.values() if task in r["tasks"]]
    if job:
        hit = [r for r in mine if r["jobs"][task] in (job, pa.job_slug(job))]
        if not hit:
            raise pa.Problem("task {} has no worktree for job {}; nothing was removed".format(task, clean(job)))
        return hit[0]
    row = next((r for r in mine if r["mains"].get(task)), None)
    if row:
        return row
    if not mine:
        raise pa.Problem("task {} has no worktree in the ledger".format(task))
    if len(mine) > 1:
        raise pa.Problem("task {} has no main worktree; name one: {}; nothing was removed".format(
            task, ", ".join("--job " + clean(r["jobs"][task]) for r in mine)))
    return mine[0]


def state_word(state):
    """A task's state as said on a line: `no state` when the ledger gives none (it counts as unfinished)."""
    return clean(state) if state not in (None, "") else "no state"


def tasks_still(row):
    """'t-003 in-progress, t-004 review' for the tasks using a worktree that are not done or parked."""
    return ", ".join("{} {}".format(clean(i), state_word(s)) for i, s in row["states"].items() if s not in ("done", "parked"))


def mark_removed_everywhere(directory, row, why):
    """After a worktree was removed (or is gone after a failed removal): every task that records it gets it marked
    removed (mark_removed), under the ledger lock, and the journal says so. No entry is left naming the folder."""
    with ledger_lock(directory):
        ledger = read_ledger(directory)
        for t in ledger.get("tasks", []):
            if t.get("id") in row["tasks"]:
                mark_removed(t, row["path"], pa.now())
                t["updated"] = pa.now()
        if ledger:
            pa.write_json(os.path.join(directory, "ledger.json"), ledger)
    append_journal(directory, {"by": "agent" if by_an_agent() else "person", "trigger": "worktree", "task": row["tasks"][0], "why": why})


def changed_since_card(snapshot):
    """What changed in the worktree since its card's snapshot, in a way that matters: a repository's HEAD
    moved (new commits), or an uncommitted file that the card did not list. None when nothing did."""
    for path, head in (snapshot.get("heads") or {}).items():
        code, now_head, err = git(path, "rev-parse", "HEAD")
        if code:
            return "cannot read {} ({})".format(path, clean(err) or "git failed")
        if now_head != head:
            return "{} has new commits since the card (HEAD was {}, now {})".format(path, head[:7], now_head[:7])
    for path, listed in (snapshot.get("uncommittedFiles") or {}).items():
        files = status_files(path)
        if files is None:
            return "cannot read {} (git status failed)".format(path)
        new = sorted({clean(f) for _s, f in files} - set(listed))
        if new:
            return "{} has uncommitted files that were not on the card: {}".format(path, ", ".join(new[:5]) + (", +{} more".format(len(new) - 5) if len(new) > 5 else ""))
    return None


def remove_worktree(path, facts):
    """Removes a worktree whose removal the person has approved. Forced only as far as there is
    something to lose (the caller has shown the person those counts). Returns what it did."""
    if facts.get("remove"):  # the profile's kind of worktree: its own remove script
        force = facts["uncommitted"] > 0 or (facts.get("removesBranches") and facts["unpushed"] > 0)
        r = subprocess.run([facts["remove"], path] + (["--force"] if force else []), stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, universal_newlines=True)
        if r.returncode or os.path.isdir(path):
            raise pa.Problem("the profile's remove script did not remove it:\n{}".format(tail(r.stdout, 15)))
        return "Removed the {} {}.".format(facts.get("kind", "worktree"), path)
    for extra in (TASK_FILE, SETTINGS, SETTINGS + ".bak-pa-hooks"):  # our own files must not count as changes
        target = os.path.join(path, extra)
        if os.path.isfile(target):
            os.unlink(target)
    try:
        os.rmdir(os.path.join(path, ".claude"))
    except OSError:
        pass
    code, out, err = git(facts["repo"], "worktree", "remove", *(["--force"] if facts["uncommitted"] else []), path)
    if code:
        raise pa.Problem("git would not remove it: {}".format(err or out))
    return "Removed the worktree {}. Branch {} is kept.".format(path, facts["branch"])


def remove_container(given):
    """After a worktree was removed: removes the folder that held it when that is a container,
    <...>/worktrees/<container>/<repo>, now empty. given: the worktree's path as recorded (so a symlinked container is
    seen). Never the `worktrees` folder, a project or workspace, a symlink, a non-empty folder or anything else;
    a workspace's worktree (<ws>/worktrees/<name>) has no container. One os.rmdir, no recursion; never raises.
    Returns what it did, or None when there is no container to look at."""
    try:
        parent = os.path.dirname(os.path.abspath(os.path.expanduser(given)))
        holder = os.path.dirname(parent)
        if (os.path.basename(holder) != "worktrees" or os.path.basename(parent) == "worktrees" or os.path.islink(parent)
                or not os.path.isdir(parent) or os.path.basename(os.path.realpath(holder)) != "worktrees"
                or os.path.dirname(os.path.realpath(parent)) != os.path.realpath(holder)):
            return None
        names = sorted(os.listdir(parent))
        if names:
            return "kept {}: not empty ({}{})".format(parent, ", ".join(clean(n) for n in names[:3]), ", …" if len(names) > 3 else "")
        os.rmdir(parent)
        return "Removed the empty folder {}.".format(parent)
    except OSError as e:
        return "could not remove the empty folder {}: {}".format(os.path.dirname(os.path.abspath(str(given))), clean(e))


def by_an_agent():
    """True inside an agent's own shell. The person's destructive commands refuse to run there:
    an agent asks with a decision card instead."""
    return any(os.environ.get(k) for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_SANDBOX", "CODEX_HOME_SESSION"))


def worker_facts(directory, agent, profile, address):
    """What belongs to one worker: its task, and the worktree it would take with it."""
    address = pa.local_id(address)
    ledger = pa.read_json(os.path.join(directory, "ledger.json"), {}) or {}
    task = next((t for t in ledger.get("tasks", []) if any(w.get("address") == address for w in t.get("workers") or [])), None)
    info = pa.hc_get(address)
    out = {"address": address, "name": info.get("name"), "state": info.get("state") or "gone", "task": None, "worktree": None}
    if not task:
        return out, None
    crew = task.get("workers") or []
    entry = next(w for w in crew if w.get("address") == address)
    out.update(task=task.get("id"), title=task.get("title"), taskState=task.get("state"), role=entry.get("role"),
               onlyWorker=len(crew) == 1, keep=bool(entry.get("keep")), finished=bool(entry.get("finished")), closed=entry.get("closed"))
    row = worktree_state(directory, agent, profile).get(worker_tree(task, entry))  # its own worktree only, never a sibling job's
    if row and task["id"] not in row["tasks"]:  # never another task's worktree (worker_tree gives only its own; a second guard)
        row = None
    if row and row["facts"] and not row["facts"].get("main") and not row["facts"].get("unreadable"):
        # Another task that records this worktree and is not done or parked keeps it (rule (a), every ledger shape: card d-094).
        held = [t for t in row["tasks"] if t != task["id"] and row["states"].get(t) not in ("done", "parked")]
        others = [w["address"] for w in row["workers"] if w["fate"] == "live" and w["address"] != address] + row["others"]
        others += ["task {} ({})".format(clean(t), state_word(row["states"].get(t))) for t in held]
        out["worktree"] = {"path": row["path"], "kind": row["facts"].get("kind", "worktree"), "uncommitted": row["facts"]["uncommitted"],
                           "unpushed": row["facts"]["unpushed"], "removesBranches": bool(row["facts"].get("removesBranches")),
                           "branch": row["facts"]["branch"], "sharedWith": others, "keep": row["keep"], "heldBy": held,
                           "tasks": list(row["tasks"])}  # whose ledger entries a removal marks
    return out, row


# ---- pa-worker close ------------------------------------------------------------------------
# The agent ends a worker once it has read its report. Every check below only reads; a refusal
# changes nothing. Only a terminal recorded in this space's ledger is ever ended.

CLOSABLE = ("idle", "done")  # hc statuses a worker may be closed in (also: exited, or already gone)
ORCHESTRATOR_FILES = ("intent.md",)  # the orchestrator's own artefact (sdlc.md); spec.md and plan.md are workers'
TOOL_FILES = ("GATE.json", "WORKTREE.md")  # written by pa-gate and pa-worktree, never a worker's report


def tool_file(name):
    """True for a file the tools write into a task's evidence folder: TOOL_FILES, and a job's WORKTREE-<job>.md."""
    return name in TOOL_FILES or re.fullmatch(r"WORKTREE-[a-z0-9-]+\.md", name) is not None


@contextlib.contextmanager
def ledger_lock(directory):
    """The lock pa-task and pa-worker take around a read-modify-write of ledger.json."""
    import fcntl
    with open(os.path.join(directory, ".ledger.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def read_ledger(directory):
    return pa.read_json(os.path.join(directory, "ledger.json"), {}) or {}


def append_journal(directory, line):
    with open(os.path.join(directory, "journal.jsonl"), "a") as journal:
        journal.write(json.dumps(dict({"at": pa.now()}, **line)) + "\n")


def recorded(ledger, address, info=None):
    """(task, entry) of the worker the ledger records at this address, or (None, None). When the address
    is recorded more than once (a core restart can hand an old address out again), an entry not yet
    finished comes first, and among those the one whose name is the live terminal's name."""
    found = [(t, w) for t in ledger.get("tasks", []) for w in t.get("workers") or [] if w.get("address") == address]
    live_name = (info or {}).get("name")
    found.sort(key=lambda tw: (bool(tw[1].get("finished")), not (live_name and tw[1].get("name") == live_name)))
    return found[0] if found else (None, None)


def read_terminal(address):
    """(hc get record, None); ({}, None) when hc says there is no such terminal; (None, why) when hc
    cannot tell. `pa.hc_get` gives {} for any hc error, so an empty answer is checked with `hc ls --all`."""
    info = pa.hc_get(address)
    if info:
        return info, None
    try:
        listed = json.loads(pa.call(["hc", "ls", "--all", "--json"])).get("terminals", [])
    except (pa.Problem, ValueError, AttributeError):
        return None, "hc did not answer"
    if any(t.get("address") == address for t in listed):
        return None, "hc get failed, but hc ls lists it"
    return {}, None


def worker_label(address, entry, info):
    return "{} ({})".format(address, entry.get("name") or (info or {}).get("name") or "unnamed")


def worker_start(entry, info):
    """When the worker started (epoch seconds): the later of the ledger's `started` (the agent writes it) and
    hc's created_ms (hc records it), so a wrong early `started` cannot make an older file count; None if neither."""
    known = []
    if entry.get("started"):
        stamp = pa.parse_time(str(entry["started"]))
        if stamp:
            known.append(stamp)
    created = (info or {}).get("created_ms")
    if isinstance(created, (int, float)) and created > 0:
        known.append(created / 1000.0)
    return max(known) if known else None


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def identity_refusal(address, task, entry, info):
    """The terminal at this address is no longer the recorded worker, or the person said to keep it."""
    if info:
        got, want = info.get("name") or "", entry.get("name")
        if (got != want) if want else not got.startswith(str(task.get("id")) + "-"):
            return "name", "{} is now a different terminal ({}), not {}; nothing was closed".format(
                address, got or "unnamed", want or "a worker of " + str(task.get("id")))
    if entry.get("keep"):
        return "keep", '{} is marked keep ({}: "{}"); only the person closes it, with the × in the panel'.format(
            worker_label(address, entry, info), entry.get("keptAt"), entry.get("keepQuote"))
    return None


status_word = pa.status_word  # one reading of hc status, shared with pa-pass


def status_refusal(address, entry, info):
    """Only an idle, done, exited or gone worker is closed."""
    if not info:
        return None
    status = status_word(info)
    if status == "working":
        return "working", "{} is working; close it after its report".format(worker_label(address, entry, info))
    if status == "blocked":
        return "blocked", "{} is blocked on a question or a permission; bring that to the person, do not close it".format(
            worker_label(address, entry, info))
    if status in CLOSABLE or (not status and info.get("state") == "finished"):
        return None
    return "status", "{} has status {}; nothing was closed".format(address, status or "none")


def own_file(name):
    """The files pa-hooks install writes into a worktree: never counted as work."""
    return name in (SETTINGS, TASK_FILE) or name.startswith(SETTINGS + ".bak-pa-hooks")


def status_files(path):
    """[(XY status, path)] of the uncommitted files in a git checkout, every untracked file listed
    whatever the repository's own config, without pa-hooks' own files; None if git cannot tell.
    Ignored files are not seen."""
    try:
        r = subprocess.run(["git", "-C", path, "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode:
        return None
    files = []
    for line in r.stdout.splitlines():
        name = line[3:].split(" -> ")[-1].strip().strip('"')
        if name and not own_file(name):
            files.append((line[:2].strip(), name))
    return files


def uncommitted_refusal(path, names_known=True, count=None):
    """A refusal for a checkout with uncommitted files, or None."""
    files = status_files(path)
    if files is None:
        return "unreadable", "cannot tell whether {} holds uncommitted work (git status failed); nothing was closed".format(path)
    names = [f for _s, f in files]
    count = max(count or 0, len(names))
    if count:
        listed = names[:5] + (["+{} more".format(len(names) - 5)] if len(names) > 5 else [])
        return "uncommitted", "{} has {} uncommitted file(s){}; have them committed or reported first. Nothing was closed".format(
            path, count, " ({})".format(", ".join(listed)) if listed else "")
    return None


def worktree_refusal(task, profile, info=None, entry=None):
    """(refusal or None, what was checked, what could not be seen). It looks at the worker's own worktree
    (worker_tree: its entry's `worktree`, else the task's main one; never a sibling job's), each repository a
    profile kind lists, too, and at the git checkout the worker's terminal runs in.
    A worktree that is gone (or none) is fine: there is nothing in it to lose."""
    checked, unseen = [], []
    path = worker_tree(task, entry)
    if path and os.path.exists(path):
        facts = worktree_facts(path, profile)
        custom = (profile or {}).get("worktree") or {}
        if not facts:
            why = ("the profile's worktree status script failed" if custom.get("marker") and os.path.exists(os.path.join(path, custom["marker"]))
                   else "not a git worktree")
            return ("unreadable", "cannot tell whether {} holds uncommitted work ({}); nothing was closed".format(path, why)), checked, unseen
        if facts.get("unreadable"):
            return ("unreadable", "cannot tell whether {} holds uncommitted work ({}); nothing was closed".format(
                path, facts["unreadable"])), checked, unseen
        if facts.get("remove"):  # the profile's own kind: its script's count, and each repository it lists
            for repo in facts.get("repos") or []:
                refusal = uncommitted_refusal(repo)
                if refusal:
                    return refusal, checked, unseen
                checked.append(repo)
            if facts["uncommitted"]:
                return ("uncommitted", "{} has {} uncommitted file(s); have them committed or reported first. Nothing was closed".format(
                    path, facts["uncommitted"])), checked, unseen
            if not facts.get("repos"):
                unseen.append("{} is checked by its profile's script only (it lists no repositories)".format(path))
            checked.append(path)
        else:
            refusal = uncommitted_refusal(path)
            if refusal:
                return refusal, checked, unseen
            checked.append(path)
    cwd = (info or {}).get("cwd") or ((info or {}).get("foreground") or {}).get("cwd")
    if cwd and not any(inside(cwd, c) for c in checked):
        cwd = os.path.realpath(cwd)
        code, top, _ = git(cwd, "rev-parse", "--show-toplevel") if os.path.isdir(cwd) else (1, "", "")
        if code or not top:
            unseen.append("{} is not in a git checkout".format(cwd))
        elif not any(inside(top, c) for c in checked):
            refusal = uncommitted_refusal(os.path.realpath(top))
            if refusal:
                return refusal, checked, unseen
            checked.append(os.path.realpath(top))
    return None, checked, unseen


def checked_note(checked, unseen):
    return "Checked for uncommitted work: {}; ignored files are not seen{}.".format(
        ", ".join(checked) or "none (no worktree, and no git checkout where it ran)",
        "; not seen: " + "; ".join(unseen) if unseen else "")


def report_problem(folder, rel, given, address, start):
    """Why a file in the evidence folder cannot stand for this worker's report, or None."""
    if rel in ORCHESTRATOR_FILES:
        return "{} is the orchestrator's own file, not a worker's report; name the file that holds this worker's report".format(given)
    if tool_file(os.path.basename(rel)):
        return "{} is written by the Project Agent's tools, not a worker's report; name the file that holds this worker's report".format(given)
    if start is not None:
        written = os.path.getmtime(os.path.join(folder, rel))
        if written < start:
            return "evidence {} was last written before {} started ({} < {}); name the file that holds this worker's report".format(
                given, address, iso(written), iso(start))
    return None


def report_path(directory, task_id, address, given, start=None):
    """The report file the agent read, relative to evidence/<task>/. It must be inside that folder, not
    empty, not the orchestrator's or the tools' own file, and written after the worker started."""
    if not given:
        raise pa.Problem("pa-worker close needs --evidence <file>: the report you read, in evidence/{}/".format(task_id))
    folder = os.path.realpath(os.path.join(directory, "evidence", task_id))
    path = os.path.realpath(given if os.path.isabs(given) else os.path.join(folder, given))
    if not path.startswith(folder + os.sep):
        raise pa.Problem("{} is not in {}'s task evidence folder evidence/{}/".format(given, address, task_id))
    if not os.path.isfile(path):
        raise pa.Problem("evidence {} is missing".format(given))
    with open(path, "rb") as f:
        if not f.read().strip():
            raise pa.Problem("evidence {} is empty; read the report before closing".format(given))
    rel = os.path.relpath(path, folder)
    problem = report_problem(folder, rel, given, address, start)
    if problem:
        raise pa.Problem(problem)
    return rel


def end_worker(directory, ledger, task, entry, address, info, report, by, trigger, actor=None, why=None):
    """Ends one worker whose checks passed: kill if live, rm, then mark its ledger entry and journal
    it. The caller holds the ledger lock, re-read the ledger and re-checked the status (`info`, the
    `hc get` it checked; empty when the terminal is already gone). A kill that leaves the terminal
    live raises, and nothing is recorded. Returns the line to print. Never touches the worktree, the
    branch, the task or the evidence. `by` goes into the entry's closedBy; `actor` (default: `by`) is
    who the journal line says ran it."""
    note = ""
    if info:
        if info.get("state") == "live":
            pa.call(["hc", "kill", address], check=False)
            if pa.hc_get(address).get("state") == "live":
                raise pa.Problem("hc kill did not end {} (it is still live); nothing was recorded".format(address))
        pa.call(["hc", "rm", address], check=False)
        if pa.hc_get(address):
            note = " (ended {} but hc rm did not remove its record)".format(address)
    stamp = pa.now()
    entry.update(finished=True, closed=stamp, closedBy=by, report=report)
    task["updated"] = stamp
    pa.write_json(os.path.join(directory, "ledger.json"), ledger)
    name = entry.get("name") or "unnamed"
    append_journal(directory, {"by": actor or by, "trigger": trigger, "task": task.get("id"),
                               "why": why or "closed worker {} ({}) after reading its report {}".format(address, name, report)})
    costs = pa.costs_module()  # t-009: one closed event for the space's costs; never changes what close does
    if costs:
        try:
            costs.record_closed(directory, address, "pa-worker close" + (" --card" if trigger == "leftovers" else ""))
        except Exception:  # noqa: BLE001
            pass
    return "Closed worker {} ({}, task {}); its worktree and branch are untouched.{}".format(address, name, task.get("id"), note)


def unrecorded(address):
    return pa.Problem("{} is not a recorded worker of this space; nothing was closed (if it is yours, close it in Herdr)".format(address))


def close_worker(directory, agent, profile, address, evidence, card_id=None):
    """pa-worker close <address> --evidence F (spec 3.2 and 3.3), or --card <id>: the person's answer on a
    leftovers card stands in for the evidence file (card d-031). Returns the lines to print."""
    if "/" in address:
        raise pa.Problem("{} is on another machine; pa-worker close runs on the machine the space runs on".format(address))
    if address == agent.get("terminal"):
        raise pa.Problem("that is the Project Agent itself; it ends when its space is closed")
    info, why = read_terminal(address)
    task, entry = recorded(read_ledger(directory), address, info)
    if not entry:
        raise unrecorded(address)
    if info is None:
        raise pa.Problem("cannot read {}'s state from hc ({}); nothing was closed".format(address, why))
    refusal = identity_refusal(address, task, entry, info)
    if refusal:
        raise pa.Problem(refusal[1])
    if entry.get("finished") and not info:
        return ["{} is already closed.".format(address)]
    card = leftovers_card(directory, card_id) if card_id else None
    if card:
        problem = card_refusal(card, address, task, entry, info)
        if problem:
            raise pa.Problem(problem)
        report = "card " + card_id
    else:
        report = report_path(directory, task["id"], address, evidence, worker_start(entry, info))
    refusal = status_refusal(address, entry, info)
    if refusal:
        raise pa.Problem(refusal[1])
    refusal, checked, unseen = worktree_refusal(task, profile, info, entry)
    if refusal:
        raise pa.Problem(refusal[1])
    by = "agent" if by_an_agent() else "person"
    with ledger_lock(directory):
        ledger = read_ledger(directory)
        info, why = read_terminal(address)
        task, entry = recorded(ledger, address, info)
        if not entry:
            raise unrecorded(address)
        if info is None:
            raise pa.Problem("cannot read {}'s state from hc ({}); nothing was closed".format(address, why))
        refusal = identity_refusal(address, task, entry, info) or status_refusal(address, entry, info)
        if refusal:
            raise pa.Problem(refusal[1])
        if card:
            problem = card_refusal(card, address, task, entry, info)
            if problem:
                raise pa.Problem(problem)
        if entry.get("finished") and not info:  # closed meanwhile (another pa-worker close)
            return ["{} is already closed.".format(address)]
        if card:
            line = end_worker(directory, ledger, task, entry, address, info, report, by, "leftovers",
                              why="closed worker {} ({}) on the person's answer to card {}".format(address, entry.get("name") or "unnamed", card_id))
        else:
            line = end_worker(directory, ledger, task, entry, address, info, report, by, "close")
    return [line, checked_note(checked, unseen)]


def keep_worker(directory, agent, address, quote, off):
    """pa-worker keep <address> --quote "<the person's words>" [--off] (spec 3.5). Both ways need the
    person's own words, stored and journaled as given. It only marks the ledger entry; it reopens nothing."""
    if "/" in address:
        raise pa.Problem("{} is on another machine; pa-worker keep runs on the machine the space runs on".format(address))
    if address == agent.get("terminal"):
        raise pa.Problem("that is the Project Agent itself; it ends when its space is closed")
    if not (quote or "").strip():
        raise pa.Problem('pa-worker keep{} needs --quote "<the person\'s own words>"'.format(" --off" if off else ""))
    quote = quote.strip()
    with ledger_lock(directory):
        ledger = read_ledger(directory)
        task, entry = recorded(ledger, address)
        if not entry:
            raise pa.Problem("{} is not a recorded worker of this space; nothing was marked".format(address))
        name = entry.get("name") or "unnamed"
        if off and not entry.get("keep"):
            print("{} is not marked keep.".format(address))
            return
        stamp = pa.now()
        if off:
            entry.update(keep=False, unkeptAt=stamp, unkeepQuote=quote)
            why = 'the person said {} may be closed again: "{}"'.format(address, quote)
        else:
            entry.update(keep=True, keptAt=stamp, keepQuote=quote)
            why = 'the person asked to keep {} open: "{}"'.format(address, quote)
        task["updated"] = stamp
        pa.write_json(os.path.join(directory, "ledger.json"), ledger)
        append_journal(directory, {"by": "agent" if by_an_agent() else "person", "trigger": "keep", "task": task.get("id"), "why": why})
    print("{} ({}, task {}) {}.".format(address, name, task.get("id"), "is no longer kept" if off else "is marked keep: it is not closed by the agent"))


LEFTOVERS_CARD = "d-leftovers"
LEFTOVERS_PER_CARD = 10  # workers on one leftovers card, at most; fewer when their lines pass CONTEXT_MAX
CLOSE_ALL, CLOSE_NONE = "Close all listed", "Close none"


def worker_words(address, name):
    """Patterns for the worker's address and name, as whole words."""
    words = [r"(?<![A-Za-z0-9]){}(?![A-Za-z0-9])".format(re.escape(address))]
    if name:
        words.append(r"(?<![A-Za-z0-9-]){}(?![A-Za-z0-9-])".format(re.escape(name)))
    return words


def line_text(line):
    return " ".join(str(v) for k, v in sorted(line.items()) if k != "at" and isinstance(v, (str, int, float)))


def read_journal(directory):
    lines = []
    for raw_line in pa.read_text(os.path.join(directory, "journal.jsonl")).splitlines():
        try:
            line = json.loads(raw_line)
        except ValueError:
            continue
        if isinstance(line, dict):
            lines.append(line)
    return lines


def idle_for(info, now=None):
    """'3 h' since hc last saw the status change (status_ms), or 'unknown'."""
    changed = (info or {}).get("status_ms")
    if not isinstance(changed, (int, float)) or changed <= 0:
        return "unknown"
    seconds = max(0, int((now or time.time()) - changed / 1000.0))
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if seconds >= size:
            return "{} {}".format(seconds // size, unit)
    return "{} s".format(seconds)


def uncommitted_in(task, profile, entry=None):
    """(worktree path or None, uncommitted count or None when it cannot be read, file names), for the worker's
    own worktree (worker_tree)."""
    path = worker_tree(task, entry)
    if not path:
        return None, 0, []
    if not os.path.exists(path):
        return path, 0, []
    facts = worktree_facts(path, profile)
    if not facts or facts.get("unreadable"):
        return path, None, []
    if facts.get("remove"):
        return path, facts["uncommitted"], []
    files = status_files(path)
    return (path, None, []) if files is None else (path, len(files), [clean(f) for _s, f in files])


def leftover_list(directory, agent, profile, ledger):
    """The old-style workers to put on the card, and the ones held back with why. It reads only: hc get,
    git status, the journal. The last journal line naming a worker is quoted for the person, never read."""
    journal = read_journal(directory)
    listed, held = [], []
    for task in ledger.get("tasks", []):
        for entry in task.get("workers") or []:
            address = entry.get("address") or ""
            if entry.get("finished") or not address or address == agent.get("terminal"):
                continue
            base = {"address": address, "name": entry.get("name"), "task": task.get("id")}
            if "/" in address:
                held.append(dict(base, why="on another machine"))
                continue
            info = pa.hc_get(address)
            if entry.get("keep"):
                held.append(dict(base, why="marked keep"))
                continue
            refusal = identity_refusal(address, task, entry, info) or status_refusal(address, entry, info)
            if refusal:
                why = {"name": "the address now names another terminal ({})".format(clean(info.get("name") or "unnamed")),
                       "working": "working", "blocked": "blocked"}.get(refusal[0], "status {}".format(clean(status_word(info) or "none")))
                held.append(dict(base, why=why))
                continue
            path, count, files = uncommitted_in(task, profile, entry)
            last = None
            for line in journal:
                if any(re.search(w, line_text(line)) for w in worker_words(address, entry.get("name"))):
                    last = line
            listed.append(dict(base, name=clean(entry.get("name")), title=clean(task.get("title")), taskState=clean(task.get("state")),
                               role=clean(entry.get("role") or "worker"), state="gone" if not info else clean(status_word(info) or info.get("state")),
                               idle="gone" if not info else idle_for(info), worktree=path, uncommitted=count, uncommittedFiles=files[:50],
                               lastLine={k: cut(clean(last.get(k)), 300) for k in ("at", "trigger", "why") if last.get(k) is not None} if last else None))
    return listed, held


def leftover_line(w):
    tree = ("; uncommitted: cannot read {}".format(w["worktree"]) if w["uncommitted"] is None else
            "; uncommitted: {} in {}{}".format(w["uncommitted"], w["worktree"], " ({})".format(", ".join(cut_path(f) for f in w["uncommittedFiles"][:5])
                                                                                       + (", …" if w["uncommitted"] > 5 else "")) if w["uncommittedFiles"] else "")
            if w["uncommitted"] else "; nothing uncommitted" if w["worktree"] else "; no worktree")
    head = '{} {} ({}, {} "{}" {}): {}, idle {}{}'.format(w["address"], w["name"] or "unnamed", w["role"], w["task"], cut(w["title"], 60),
                                                         w["taskState"], w["state"], w["idle"], tree)
    last = w.get("lastLine")
    quoted = ('  last journal line naming it (quoted, not read): "{}" ({}, {})'.format(cut(last.get("why", ""), 160), last.get("at", "?"),
                                                                                       last.get("trigger", "?")) if last else "  no journal line names it")
    return [head, quoted]


def leftovers_card_text(listed, held):
    """(the workers on the card, its context). A card holds only the workers whose lines it shows in full:
    at most LEFTOVERS_PER_CARD, and no more than fit in CONTEXT_MAX with the header and the footer. The
    panel shows the whole context, so what "Close all listed" can close is exactly what the person saw."""
    header = "Workers recorded before one-worker-per-job, listed by `pa-worker close --leftovers`. Nothing has been closed."
    held_line = ("Not listed (the agent closes them later, after their reports): " + ", ".join(
        "{} ({})".format(h["address"], h["why"]) for h in held)) if held else ""
    held_line = cut(held_line, 400)
    shown, body = [], []
    for w in listed[:LEFTOVERS_PER_CARD]:
        lines = leftover_line(w)
        more = len(listed) - len(shown) - 1
        footer = ([("{} more old-style workers did not fit on this card; they come on the next card, once this one is "
                    "answered.").format(more)] if more else []) + ([held_line] if held_line else [])
        if len("\n".join([header] + body + lines + footer)) > CONTEXT_MAX and shown:
            break
        shown.append(w)
        body += lines
    more = len(listed) - len(shown)
    footer = ([("{} more old-style workers did not fit on this card; they come on the next card, once this one is "
                "answered.").format(more)] if more else []) + ([held_line] if held_line else [])
    return shown, "\n".join([header] + body + footer)[:CONTEXT_MAX]


def shown_before(directory, policy):
    """Workers on earlier leftovers cards that the person answered: they are not offered again."""
    seen = set()
    for card_id in (policy or {}).get("cards") or []:
        card = pa.read_json(os.path.join(directory, "decisions", card_id + ".json")) or {}
        if card.get("status") == "answered":
            seen |= {(w.get("address"), w.get("task"), w.get("name")) for w in card.get("workers") or []}
    return seen


def close_leftovers(directory, agent, profile, dry_run):
    """pa-worker close --leftovers [--dry-run], as the person decided on card d-031: put the recorded workers
    from before one-worker-per-job that are idle and not finished on a decision card, one card at a time, and
    close nothing; `pa-worker close --card` does that after the person answers it in the panel. The first run
    sets workerPolicy. A later run opens the next card only when the last one is answered and some workers did
    not fit on it, or when it was withdrawn (a note); else it says it already ran."""
    ledger = read_ledger(directory)
    policy = ledger.get("workerPolicy")
    if policy:
        last_id = policy.get("card")
        last = pa.read_json(os.path.join(directory, "decisions", last_id + ".json")) if last_id else None
        again = last and (last.get("status") == "withdrawn" or (last.get("status") == "answered" and last.get("more")))
        if not again:
            print("--leftovers already ran (workerPolicy since {}; card {}{}); nothing was done.".format(
                policy.get("since"), last_id or "none",
                " is open: the person answers it first" if last and last.get("status") == "open" else ""))
            return
    seen = shown_before(directory, policy)
    listed, held = leftover_list(directory, agent, profile, ledger)
    listed = [w for w in listed if (w["address"], w["task"], w["name"]) not in seen]
    on_card, context = leftovers_card_text(listed, held)
    cards = list((policy or {}).get("cards") or [])
    card_id = LEFTOVERS_CARD if not cards else "{}-{}".format(LEFTOVERS_CARD, len(cards) + 1)
    if dry_run:
        for w in on_card:
            print("would list {} ({}, {}): {}".format(w["address"], w["name"] or "unnamed", w["task"], leftover_line(w)[0].split(": ", 1)[1]))
        if len(listed) > len(on_card):
            print("{} more would wait for the next card".format(len(listed) - len(on_card)))
        for h in held:
            print("not listed {} ({}, {}): {}".format(h["address"], h.get("name") or "unnamed", h["task"], h["why"]))
        if not listed:
            print("No old-style workers to list.")
        return
    by = "agent" if by_an_agent() else "person"
    path = os.path.join(directory, "decisions", card_id + ".json")
    with ledger_lock(directory):
        ledger = read_ledger(directory)
        opened = bool(on_card) and not os.path.exists(path)
        if opened:
            pa.write_json(path, {
                "id": card_id, "kind": "leftovers", "task": None,
                "question": "Close the {} worker(s) left from before one-worker-per-job that are listed here?".format(len(on_card)),
                "context": context,
                "options": [{"label": CLOSE_ALL, "detail": "The agent closes each worker listed here with `pa-worker close --card {}`. It skips "
                             "one marked keep (tell the agent \"keep tNN\" first), and one that is working, blocked, has uncommitted files, or "
                             "became active or changed since this card. Leave the note empty: a note means nothing on this card is closed. "
                             "Worktrees, branches, tasks and evidence stay.".format(card_id)},
                            {"label": CLOSE_NONE, "detail": "Leave them. The agent closes each later, once it has read its report."}],
                "evidence": [], "recommendation": None, "status": "open", "answer": None, "note": None,
                "workers": on_card, "more": len(listed) - len(on_card), "notListed": held, "created": pa.now(), "answered": None})
        ledger["workerPolicy"] = dict(policy or {"style": "per-job", "since": pa.now()},
                                      card=card_id if opened else (policy or {}).get("card"), cards=cards + ([card_id] if opened else []))
        pa.write_json(os.path.join(directory, "ledger.json"), ledger)
        append_journal(directory, {"by": by, "trigger": "leftovers", "why": "listed {} old-style worker(s) on card {} for the person: {}; {} wait for "
                                   "the next card; not listed: {}; nothing was closed".format(
                                       len(on_card), card_id if opened else "(none)", ", ".join(w["address"] for w in on_card) or "none",
                                       len(listed) - len(on_card), ", ".join("{} ({})".format(h["address"], h["why"]) for h in held) or "none")})
    for w in on_card:
        print("listed {} ({}, {}): {}".format(w["address"], w["name"] or "unnamed", w["task"], leftover_line(w)[0].split(": ", 1)[1]))
    if len(listed) > len(on_card):
        print("{} more wait for the next card: once {} is answered, run pa-worker close --leftovers again".format(len(listed) - len(on_card), card_id))
    for h in held:
        print("not listed {} ({}, {}): {}".format(h["address"], h.get("name") or "unnamed", h["task"], h["why"]))
    if not on_card:
        print("No old-style workers to list; no card opened. workerPolicy is set (per-job).")
    elif opened:
        print("opened card {} for the person; nothing was closed. workerPolicy is set (per-job).".format(card_id))
    else:
        print("card {} exists already; nothing was closed.".format(card_id))


def leftovers_card(directory, card_id):
    """The answered leftovers card that lets `pa-worker close --card` close workers, or a refusal."""
    card = pa.read_json(os.path.join(directory, "decisions", card_id + ".json")) if pa.ID.match(card_id or "") else None
    if not card or card.get("kind") != "leftovers":
        raise pa.Problem("no leftovers card {}; nothing was closed".format(card_id))
    if card.get("status") == "open":
        raise pa.Problem("card {} is not answered yet; the person answers it in the panel. Nothing was closed".format(card_id))
    if card.get("status") != "answered":
        raise pa.Problem("card {} is {}; nothing was closed".format(card_id, card.get("status")))
    if "in conversation" in str(card.get("answeredBy") or ""):
        raise pa.Problem("card {} is answered in the panel only; nothing was closed".format(card_id))
    if card.get("answer") != CLOSE_ALL:
        raise pa.Problem("card {} was answered {!r}; nothing was closed".format(card_id, card.get("answer")))
    if str(card.get("note") or "").strip():
        # A note is never parsed: it could name a worker to keep in any words. Close nothing and ask again.
        card.update(status="withdrawn", previousAnswer=card.get("answer"), answer=None, answeredBy="pa-worker close: withdrawn",
                    withdrawnAt=pa.now())
        pa.write_json(os.path.join(directory, "decisions", card_id + ".json"), card)
        append_journal(directory, {"by": "agent" if by_an_agent() else "person", "trigger": "leftovers",
                                   "why": "withdrew card {}: it was answered with a note ({}); nothing was closed".format(card_id, cut(clean(card["note"]), 200))})
        raise pa.Problem('card {} has a note, so nothing was closed: "{}". The note is not read. Ask the person to say "keep tNN" for each '
                         'worker to keep (pa-worker keep), then run pa-worker close --leftovers to ask again on a new card'.format(
                             card_id, cut(clean(card["note"]), 200)))
    return card


def card_refusal(card, address, task, entry, info):
    """Why this worker is not closed on the card's answer: not on it, named in its note, or changed since."""
    listed = next((w for w in card.get("workers") or [] if w.get("address") == address and w.get("task") == task.get("id")
                   and w.get("name") == clean(entry.get("name"))), None)
    if not listed:
        return "{} is not on card {}; nothing was closed".format(address, card["id"])
    created = pa.parse_time(str(card.get("created") or ""))
    changed = (info or {}).get("status_ms")
    if info and not isinstance(changed, (int, float)):
        return "activity since card {} opened is unknown (hc gives no status time); not closed".format(card["id"])
    if (listed.get("state") == "gone" and info) or (created and isinstance(changed, (int, float)) and changed / 1000.0 > created):
        return "{} has been active since card {} opened; not closed".format(address, card["id"])
    return None


def close_by_card(directory, agent, profile, card_id):
    """pa-worker close --card <id>: every worker on the answered leftovers card, each through the normal
    guards. A worker that fails one is skipped and listed; the others are closed."""
    card = leftovers_card(directory, card_id)
    for w in card.get("workers") or []:
        try:
            for line in close_worker(directory, agent, profile, w.get("address") or "", None, card_id):
                print(line)
        except pa.Problem as e:
            print("skipped {} ({}, {}): {}".format(w.get("address"), w.get("name") or "unnamed", w.get("task"), e))


def mark_removed(task, path, when):
    """Records in a ledger task that the worktree at path (a realpath) was removed: its `worktrees` entry gets
    `removed`, or one is added (path as recorded, its job) when it came from `worktree` or `extraWorktrees`;
    `worktree` becomes null only if it was that one. History is kept; nothing else changes."""
    tree = next((w for w in pa.task_worktrees(task) if w["path"] == path), None)
    real = lambda p: os.path.realpath(os.path.expanduser(p))
    entries = task.get("worktrees")
    marked = False
    if isinstance(entries, list):
        for e in entries:
            if isinstance(e, dict) and isinstance(e.get("path"), str) and e["path"].strip() and real(e["path"]) == path:
                e["removed"] = e.get("removed") or when
                marked = True
    if not marked and tree and (entries is None or isinstance(entries, list)):
        task["worktrees"] = (entries or []) + [{"path": tree["given"], "job": tree["job"], "removed": when}]
    if isinstance(task.get("worktree"), str) and task["worktree"].strip() and real(task["worktree"]) == path:
        task["worktree"] = None


def cmd_worker(args):
    given = args.space or space_from_terminal() or ""
    space = pa.local_id(given)
    if not space:
        raise pa.Problem("pass --space <id>")
    if args.action in ("close", "keep") and "/" in given and not os.path.isdir(pa.space_dir(space)):
        raise pa.Problem("space {} lives on {}; run pa-worker there (its Project Agent does)".format(given, given.split("/")[0]))
    directory, agent, profile = load_context(space)
    if args.action == "close":
        if args.leftovers:
            return close_leftovers(directory, agent, profile, args.dry_run)
        if args.card and not args.address:
            return close_by_card(directory, agent, profile, args.card)
        for line in close_worker(directory, agent, profile, args.address, args.evidence, args.card):
            print(line)
        return
    if args.action == "keep":
        return keep_worker(directory, agent, args.address, args.quote, args.off)
    facts, row = worker_facts(directory, agent, profile, args.address)
    if args.action == "info":
        print(json.dumps(facts, indent=2))
        return
    if by_an_agent():  # delete only: close is the agent's
        raise pa.Problem("pa-worker delete is the person's command. To remove a worker or a worktree, ask the person.")
    if facts["address"] == agent.get("terminal"):
        raise pa.Problem("that is the Project Agent itself; it ends when its space is closed")
    did, failed = [], None
    if facts["state"] == "live":
        pa.call(["hc", "kill", facts["address"]], check=False)
    if facts["state"] != "gone":
        pa.call(["hc", "rm", facts["address"]], check=False)
        did.append("Ended and removed worker {} ({}).".format(facts["address"], facts.get("name") or "unnamed"))
    tree = facts.get("worktree")
    if tree and not args.keep_worktree:
        if tree["keep"]:
            did.append("Kept the worktree (keepWorktree is set on the task).")
        elif tree["sharedWith"]:
            did.append("Kept the worktree: still used by {}.".format(", ".join(tree["sharedWith"])))
        elif (tree["uncommitted"] or (tree["removesBranches"] and tree["unpushed"])) and not args.discard:
            did.append("Kept the worktree: it has {} uncommitted file(s) and {} unpushed commit(s); delete again with --discard to lose them.".format(
                tree["uncommitted"], tree["unpushed"]))
        else:
            try:
                did.append(remove_worktree(tree["path"], row["facts"]))
                tree["removed"] = True
                tidy = remove_container(row["given"])
                if tidy:
                    did.append(tidy)
            except pa.Problem as e:  # the worker is ended already: the ledger is still written, the failure said
                failed = "Could not remove the worktree {}: {}".format(tree["path"], e)
                did.append(failed)
                tree["removed"] = not os.path.lexists(tree["path"])  # gone anyway: marked, so no entry names it
    if facts.get("task"):
        with open(os.path.join(directory, ".ledger.lock"), "a") as lock:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = os.path.join(directory, "ledger.json")
            ledger = pa.read_json(path)
            for t in (ledger or {}).get("tasks", []):
                if t.get("id") == facts["task"]:
                    t["workers"] = [w for w in t.get("workers") or [] if w.get("address") != facts["address"]]
                    t["updated"] = pa.now()
                if tree and tree.get("removed") and t.get("id") in tree["tasks"]:  # every task that recorded it
                    mark_removed(t, tree["path"], pa.now())
                    t["updated"] = pa.now()
            if ledger:
                pa.write_json(path, ledger)
            with open(os.path.join(directory, "journal.jsonl"), "a") as journal:
                journal.write(json.dumps({"at": pa.now(), "by": "person", "trigger": "panel", "task": facts["task"],
                                          "why": "the person deleted worker {}: {}".format(facts["address"], " ".join(did))}) + "\n")
    for line in did:
        print(line)
    if args.with_task and facts.get("task"):
        subprocess.call([os.path.join(os.path.dirname(os.path.realpath(__file__)), "pa.py"), "task", "delete", facts["task"], "--space", space])
    elif facts.get("task"):
        term = pa.hc_get(agent.get("terminal"))
        if term.get("state") == "live" and pa.launcher_alive(directory, agent.get("terminal")):
            pa.call(["hc", "send", agent["terminal"], "The person deleted worker {} of task {}. {} Re-read ledger.json; do not start a replacement "
                     "unless they ask.".format(facts["address"], facts["task"], " ".join(did[1:]))], check=False)
        else:
            print("(The Project Agent is not running; it reads the ledger when it starts.)")
    if failed:
        raise pa.Problem("the worktree was not removed ({}); the worker is ended and the ledger updated".format(tree["path"]))


# ---- pa-trust: Claude Code's folder trust prompt (t-035) ---------------------------------------------------
#
# The marker table is built from a real screen (evidence/t-035/trust-screen.txt, kept as
# tests/trust_screens/claude-real.txt) and, as a second set, the older wording the spec remembers. Anything else
# is not recognised and goes to the person (fail closed). Nothing here writes a file or opens Claude Code's or
# Codex's own state.

BOX = "│╭╮╰╯─┃┏┓┗┛━"
CURSOR = "❯"
TRUST_WORDINGS = [
    {"name": "saved 2026-11-18", "markers": ["Accessing workspace:", "Is this a project you created or one you trust?"],
     "yes": "Yes, I trust this folder", "no": "No, exit",
     "text": "Accessing workspace: Quick safety check: Is this a project you created or one you trust? (Like your own "
             "code, a well-known open source project, or work from your team). If not, take a moment to review what's "
             "in this folder first. Claude Code'll be able to read, edit, and execute files here. Security guide",
     "footer": ["Enter to confirm · Esc to cancel"]},
    {"name": "older, remembered", "markers": ["Do you trust the files in this folder?"],
     "yes": "1. Yes, proceed", "no": "2. No, exit",
     "text": "Do you trust the files in this folder? Claude Code may read files in this folder. Reading untrusted files "
             "may lead Claude Code to behave in unexpected ways. With your permission Claude Code may execute files in "
             "this folder. Executing untrusted code is unsafe. Learn more",
     "footer": ["Enter to confirm · Esc to exit", "Enter to confirm · Esc to cancel"]},
]
# Another dialog anywhere on the screen: a tool permission prompt, an MCP server approval, the bypass-permissions
# confirmation, an auto-mode notice, Codex.
OTHER_DIALOG_MARKERS = ["Do you want to", "don't ask again", "MCP server", ".mcp.json", "Bypass Permissions",
                        "bypass permissions", "Auto mode", "auto mode", "Codex", "Yes, I accept", "esc to interrupt"]
GRANT_WORDS = ("allow", "permission", "director", "mcp", "server", "env", "helper", "apikey")
PERMISSION_RULE = re.compile(r"\b(Bash|Read|Edit|Write|MultiEdit|NotebookEdit|WebFetch|WebSearch|Glob|Grep|Task|Agent|Skill)\(|\bmcp__")
PA_HOOK_COMMAND = re.compile(r"^(\$HOME/\.local/bin/|~/\.local/bin/|/[^\s;&|`$<>()]*/)?pa-hook [a-z][a-z-]*( --[a-z][a-z-]*)*$")
PA_HOOK_LISTED = re.compile(r"(^|\s)(\S*/)?pa-hook [a-z][a-z-]*( --[a-z][a-z-]*)*$")
TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
CONFIRM_TRIES, CONFIRM_DELAY = 10, 0.5


def screen_line(line):
    return re.sub(r"\s+", " ", line.strip().strip(BOX).strip())


def trust_marks(text):
    """The wording whose markers are all on the screen, or None (none, or more than one)."""
    flat = re.sub(r"\s+", " ", " ".join(screen_line(l) for l in text.split("\n")))
    found = [w for w in TRUST_WORDINGS if all(m in flat for m in w["markers"])]
    return found[0] if len(found) == 1 else None


def pa_hook_line(text):
    """A listed hook that is a pa-hook command and nothing more."""
    if any(ch in text.replace("$HOME", "") for ch in ";&|`$<>()"):
        return False
    return bool(PA_HOOK_LISTED.search(text))


def trust_read(text, width=None):
    """Read a screen: (result or None, why, facts). facts: wording, shown (the folder as shown), grants (what the
    dialog lists that would be granted), cursor ("yes", "no" or None) and move (the key from No to Yes, or None)."""
    lines = [l.rstrip("\r") for l in (text or "").split("\n")]
    for m in OTHER_DIALOG_MARKERS:
        if m in (text or ""):
            return "not-this-prompt", "the screen shows another dialog ({!r})".format(m), {}
    wording = trust_marks(text or "")
    if not wording:
        return "not-this-prompt", "no folder trust prompt this tool knows is on the screen", {}
    known = re.sub(r"\s+", " ", wording["text"])
    options = {}
    for i, line in enumerate(lines):
        c = screen_line(line)
        mark = c.startswith(CURSOR)
        bare = c[len(CURSOR):].strip() if mark else c
        for key in ("yes", "no"):
            if bare == wording[key]:
                options.setdefault(key, []).append((i, mark))
    if len(options.get("yes", [])) != 1 or len(options.get("no", [])) != 1:
        return "not-this-prompt", "the yes and no choices are not each on the screen once", {}
    (yes_i, on_yes), (no_i, on_no) = options["yes"][0], options["no"][0]
    first = next(i for i, l in enumerate(lines) if wording["markers"][0] in screen_line(l))
    if first > min(yes_i, no_i):
        return "not-this-prompt", "the choices come before the dialog's text", {}
    end = max(yes_i, no_i)
    for line in lines[end + 1:]:
        c = screen_line(line)
        if c and c not in wording["footer"]:
            return "not-this-prompt", "text after the dialog it does not know: {!r}".format(c[:80]), {}
    paths, grants, unknown, section = [], [], [], None
    for i in range(first, end + 1):
        if i in (yes_i, no_i):
            continue
        c = screen_line(lines[i])
        if not c:
            section = None
            continue
        if c in wording["footer"] or (len(c) >= 3 and c in known):
            continue
        if section == "grant":
            grants.append(c)
            continue
        if section == "hooks":
            if not pa_hook_line(c):
                grants.append("a hook that is not the Project Agent's: " + c)
            continue
        low = c.lower()
        if c.endswith(":"):
            if "hook" in low:
                section = "hooks"
            elif any(w in low for w in GRANT_WORDS):
                section = "grant"
                grants.append(c)
            else:
                unknown.append(c)
            continue
        if c.startswith(("/", "~", "…", "...")):
            paths.append(i)
        elif PERMISSION_RULE.search(c) or any(w in low for w in GRANT_WORDS):
            grants.append(c)
        elif not pa_hook_line(c):
            unknown.append(c)
    if len(paths) != 1:
        return "uncertain", "{} folder lines on the dialog, not exactly one".format(len(paths)), {}
    i = paths[0]
    shown = screen_line(lines[i])
    raw = lines[i].rstrip()
    width = width or max(len(l.rstrip()) for l in lines)
    if "…" in shown or "..." in shown:
        return "uncertain", "the folder is shown cut: " + shown, {}
    if i + 1 >= len(lines) or screen_line(lines[i + 1]):
        return "uncertain", "the line under the folder is not blank: the path may go on", {}
    if raw and raw[-1] in BOX:
        if not raw[:-1].endswith("  "):
            return "uncertain", "the folder reaches the dialog's right edge: it may be wrapped", {}
    elif len(raw) >= width:
        return "uncertain", "the folder reaches the screen's right edge: it may be wrapped", {}
    if shown.startswith("~") and not shown.startswith("~/"):
        return "uncertain", "the folder starts with ~ but not ~/: " + shown, {}
    if unknown:
        return "not-this-prompt", "text on the dialog it does not know: {!r}".format(unknown[0][:80]), {}
    cursor = "yes" if on_yes and not on_no else ("no" if on_no and not on_yes else None)
    # The saved screen has Yes on the line right under No: one `down`. Any other layout is not one seen; no key.
    move = "down" if cursor == "no" and yes_i == no_i + 1 else None
    return None, "", {"wording": wording["name"], "shown": shown, "grants": grants, "cursor": cursor, "move": move}


def trust_project(project, home, pa_home=None):
    """(real project folder, None), or (None, why) when it cannot be the base of a trust: also when the home folder
    is inside it (every folder in it, the Claude home among them, would be inside), or when it overlaps the Project
    Agent's home or its spaces folder either way, on real paths (holds it, is it, or lies inside it: its state
    folders would be inside it), or holds the real path of any space folder (<pa home>/spaces/<id>, a symlink)."""
    if not isinstance(project, str) or not project.strip():
        return None, "agent.json names no project folder"
    if not os.path.isabs(project):
        return None, "the project folder is not an absolute path: " + project
    real = os.path.realpath(project)
    if not os.path.isdir(real):
        return None, "the project folder is not a folder: " + project
    if real == os.path.realpath(os.sep) or trust_inside(home, real):
        return None, "the project folder is /, the home folder or a folder holding it: every folder in the home " \
                     "folder would be inside it"
    if pa_home and any(trust_inside(x, real) or trust_inside(real, x) for x in (pa_home, os.path.join(pa_home, "spaces"))):
        return None, "the project folder overlaps the Project Agent's home (holds it or its spaces folder, or lies " \
                     "inside it): its state would be inside it"
    if pa_home:
        spaces = os.path.join(pa_home, "spaces")
        try:
            names = os.listdir(spaces)
        except OSError:
            names = []
        if any(trust_inside(os.path.join(spaces, n), real) for n in names):
            return None, "the project folder holds a space folder of the Project Agent's home (through a symlink): " \
                         "its state would be inside it"
    return real, None


def trust_inside(folder, base):
    """folder is base or inside it, on real paths (symlinks and '..' resolved, separator checked)."""
    f, b = os.path.realpath(folder), os.path.realpath(base)
    return f == b or f.startswith(b.rstrip(os.sep) + os.sep)


def trust_evidence(folder, state_dir, task_ids):
    """folder is <state_dir>/evidence/<id>/ or inside it, for one of task_ids (plain names only), on real paths;
    never the state folder, evidence/ itself or the rest of the state folder."""
    base = os.path.realpath(os.path.join(state_dir, "evidence"))
    for task_id in task_ids:
        if not isinstance(task_id, str) or not TASK_ID.match(task_id) or task_id in (".", ".."):
            continue
        own = os.path.realpath(os.path.join(base, task_id))
        if os.path.dirname(own) != base or not os.path.isdir(own):
            continue  # a task folder that is a symlink elsewhere counts for nothing
        if trust_inside(folder, own):
            return True
    return False


TRUST_GIT_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_CEILING_DIRECTORIES",
                  "GIT_DISCOVERY_ACROSS_FILESYSTEM")


def trust_git_env(environ):
    """pa-trust's git environment: without the variables that move git's repository discovery, and LC_ALL=C so its
    messages are matched in one language."""
    env = {k: v for k, v in environ.items() if k not in TRUST_GIT_DROP}
    env["LC_ALL"] = "C"
    return env


def trust_git(path, *args):
    """git() for pa-trust: the same (exit code, stdout, stderr), run with trust_git_env."""
    try:
        r = subprocess.run(["git", "-C", path] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=GIT_TIMEOUT, env=trust_git_env(os.environ))
    except subprocess.TimeoutExpired:
        return 124, "", "git {} timed out after {} s".format(args[0] if args else "", GIT_TIMEOUT)
    except OSError as e:
        return 127, "", str(e)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def git_top(folder):
    code, out, _ = trust_git(folder, "rev-parse", "--show-toplevel")
    return os.path.realpath(out) if code == 0 and out else None


def trust_key_why(folder):
    """(the folder Claude Code records the trust for (spec D2), as a real path, None), or (None, why). In a git
    repository its root, and in a worktree its main checkout's root; outside git the folder itself. Outside git
    only when git finds no repository at all (exit 128, 'not a git repository (or any ...'): a broken .git file
    ('not a git repository: <gitdir>'), a timeout, 'dubious ownership' or any other failure, and a repository whose
    main checkout git cannot tell (a bare repository's worktree, a submodule), give a why."""
    real = os.path.realpath(folder)
    code, out, err = trust_git(real, "rev-parse", "--show-toplevel")
    if code == 128 and "not a git repository (or any" in err:
        return real, None
    if code != 0 or not out:
        return None, "git could not say whether the folder is in a repository (exit {}: {})".format(
            code, (err or "no output").splitlines()[0][:200])
    code, common, _ = trust_git(real, "rev-parse", "--git-common-dir")
    if code == 0 and common:
        common = os.path.realpath(os.path.join(real, common))
        if os.path.basename(common) == ".git":
            return os.path.dirname(common), None
    return None, "git names a repository for the folder but not its main checkout"


def trust_key(folder):
    return trust_key_why(folder)[0]


def trust_area(folder, project, state_dir, task_ids):
    """Where folder lies, or None: in the state folder only a ledger task's evidence folder counts (whatever holds
    the state folder), else the project folder or inside it."""
    if trust_inside(folder, state_dir):
        return "a task's evidence folder in the state folder" if trust_evidence(folder, state_dir, task_ids) else None
    return "inside the project folder" if trust_inside(folder, project) else None


def worker_dirs(folder):
    """The worker's folder, its git top level and its main checkout root, as real paths."""
    if not isinstance(folder, str) or not folder or not os.path.isdir(folder):
        return []
    real = os.path.realpath(folder)
    out = [real]
    top = git_top(real)
    if top:
        out.append(top)
        code, common, _ = trust_git(real, "rev-parse", "--git-common-dir")
        if code == 0 and common:
            common = os.path.realpath(os.path.join(real, common))
            if os.path.basename(common) == ".git":
                out.append(os.path.dirname(common))
    return list(dict.fromkeys(out))


def trust_own(folder, dirs):
    return os.path.realpath(folder) in dirs


def local_settings_grants(path):
    """What an untracked .claude/settings.local.json grants beyond the Project Agent's own (pa-hooks)."""
    try:
        data = json.loads(pa.read_text(path))
    except ValueError:
        return ["{} is not valid JSON".format(path)]
    if not isinstance(data, dict):
        return ["{} is not a settings object".format(path)]
    found = ["{} sets {}".format(path, k) for k in sorted(set(data) - {"hooks", "permissions"})]
    perms = data.get("permissions", {})
    if not isinstance(perms, dict) or set(perms) - {"deny"} or not isinstance(perms.get("deny", []), list):
        found.append("{} has permissions other than deny rules".format(path))
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        return found + ["{} has hooks it cannot read".format(path)]
    for event, entries in hooks.items():
        for entry in entries if isinstance(entries, list) else [None]:
            inner = entry.get("hooks") if isinstance(entry, dict) and not set(entry) - {"matcher", "hooks"} else None
            for h in inner if isinstance(inner, list) else [None]:
                ok = (isinstance(h, dict) and not set(h) - {"type", "command", "timeout"} and h.get("type") == "command"
                      and isinstance(h.get("command"), str) and PA_HOOK_COMMAND.match(h["command"]))
                if not ok:
                    found.append("{} has a {} hook that is not a pa-hook command".format(path, event))
    return found


def folder_grants(dirs):
    """What the folders' own files would grant once trusted (spec R5). Repository files only."""
    found = []
    for d in dict.fromkeys(os.path.realpath(x) for x in dirs if x):
        claude = os.path.join(d, ".claude")
        if os.path.lexists(os.path.join(d, ".mcp.json")):
            found.append("{} has a .mcp.json".format(d))
        if os.path.lexists(os.path.join(claude, "settings.json")):
            found.append("{} has a .claude/settings.json".format(d))
        local = os.path.join(claude, "settings.local.json")
        if not os.path.lexists(local):
            continue
        if os.path.islink(claude) or os.path.islink(local):
            found.append("{} reaches its settings.local.json through a symlink".format(d))
            continue
        code, _, _ = trust_git(d, "ls-files", "--error-unmatch", "--", os.path.join(".claude", "settings.local.json"))
        if code == 0:
            found.append("{} tracks its .claude/settings.local.json in git".format(d))
            continue
        found += local_settings_grants(local)
    return found


def cmd_trust(args):
    if not args.address:
        raise pa.Problem("pa-trust needs a terminal address")
    address = args.address
    space = pa.local_id(args.space or space_from_terminal() or "")
    if not space:
        raise pa.Problem("pass --space <id>")
    directory, agent, _profile = load_context(space)
    home = os.path.expanduser("~")
    out = {"worker": address, "folder": None, "real": None, "project": None, "result": None, "why": ""}

    def done(result, why):
        out.update(result=result, why=why)
        print(json.dumps(out))
        sys.exit(0 if result == "accepted" else 1)

    project, why = trust_project(agent.get("project"), home, pa.ROOT)
    if why:
        done("refused", why)
    out["project"] = project
    if address == agent.get("terminal") or address in (agent.get("terminals") or []):
        done("refused", "that is the Project Agent's own terminal: its trust prompt is the person's")
    ledger = read_ledger(directory)
    _task, entry = recorded(ledger, address)
    if not entry:
        done("refused", "not a worker this space's ledger records: its prompts are the person's")

    def get():
        info = pa.hc_get(address)
        if not info:
            raise pa.Problem("hc gave no record for " + address)
        return info

    def read():
        return pa.call(["hc", "read", address, "--lines", "0"])

    info = get()
    status = info.get("status") if isinstance(info.get("status"), dict) else {}
    if "codex" in str(status.get("app") or "").lower():
        done("not-this-prompt", "a Codex worker: its trust prompt goes to the person")
    if pa.status_word(info) != "blocked":
        done("not-this-prompt", "its status is {!r}, not blocked".format(pa.status_word(info)))
    size = info.get("size") if isinstance(info.get("size"), dict) else {}
    width = size.get("cols") if isinstance(size.get("cols"), int) and size.get("cols") > 0 else None
    screen = read()
    result, why, facts = trust_read(screen, width)
    if result:
        done(result, why)
    shown = facts["shown"]
    out["folder"] = shown
    path = os.path.join(home, shown[2:]) if shown.startswith("~/") else shown
    if not os.path.isdir(path):
        done("uncertain", "the folder on the screen is not a folder here (it may be cut): " + shown)
    real = os.path.realpath(path)
    out["real"] = real
    ids = [t.get("id") for t in ledger.get("tasks", []) if isinstance(t, dict)]
    area = trust_area(real, project, directory, ids)
    if not area:
        done("outside", "on real paths the folder is neither the project folder or inside it (outside the state "
             "folder), nor a task's evidence folder")
    key, why = trust_key_why(real)
    if not key:
        done("uncertain", why + ", so where the trust would be recorded is not known")
    if key != real and not trust_area(key, project, directory, ids):
        done("outside", "Claude Code would record the trust for {}, the folder's repository (or its main checkout), "
             "which is neither the project folder or inside it nor a task's evidence folder".format(key))
    cwd = (info.get("foreground") or {}).get("cwd") or info.get("cwd")
    dirs = worker_dirs(cwd)
    if not dirs:
        done("uncertain", "hc does not say which folder the worker runs in")
    if not trust_own(real, dirs):
        done("uncertain", "the folder is not the worker's own folder or its repository's")
    if facts["grants"]:
        done("grants", "the dialog lists what the folder would grant: " + "; ".join(facts["grants"])[:300])
    found = folder_grants([real, dirs[0], git_top(real), git_top(dirs[0])])
    if found:
        done("grants", "; ".join(found)[:300])
    if facts["cursor"] is None:
        done("uncertain", "the cursor is not on one choice")
    if facts["cursor"] == "no" and not facts["move"]:
        done("uncertain", "the cursor is on the no choice and the yes choice is not the line right under it")
    if read() != screen:
        done("uncertain", "the screen changed before any key was pressed")
    if pa.status_word(get()) != "blocked":
        done("uncertain", "the worker is no longer blocked")
    if facts["cursor"] == "no":
        pa.call(["hc", "keys", address, facts["move"]])
        moved = read()
        r2, _w2, f2 = trust_read(moved, width)
        if r2 or f2.get("cursor") != "yes" or moved.replace(CURSOR, " ") != screen.replace(CURSOR, " ") \
                or pa.status_word(get()) != "blocked":
            done("uncertain", "pressed {} once; the screen then did not show the cursor on the yes choice with nothing "
                 "else changed, so Enter was not pressed (the dialog is left as it is now)".format(facts["move"]))
    pa.call(["hc", "keys", address, "enter"])
    for n in range(CONFIRM_TRIES):
        if n:
            time.sleep(CONFIRM_DELAY)
        try:
            after, text = pa.hc_get(address), read()
        except pa.Problem:
            continue
        if after and pa.status_word(after) != "blocked" and not trust_marks(text):
            done("accepted", "the folder is {}, the worker's own, with no grants of its own; the prompt moved on".format(area))
    done("unconfirmed", "pressed Enter once; the screen still shows the trust prompt or the worker is still blocked")


def cmd_full_suite(_args):
    record = find_task(os.getcwd())
    if not record:
        raise pa.Problem("run pa-full-suite inside a task's worktree (pa-hooks install marks it)")
    directory, agent, profile = load_context(record["space"])
    command = command_for("full", agent, profile)
    if not command:
        raise pa.Problem("no full-suite command: set tests.full in the profile, or commands.full in the space's agent.json")
    began = time.time()
    code, out = run_shell(command, record["root"], [], 3600)
    evidence = os.path.join(directory, "evidence", record["task"])
    os.makedirs(evidence, exist_ok=True)
    with open(os.path.join(evidence, "build.log"), "a") as f:
        f.write("=== {} full suite: `{}` in {} -> exit {} after {:.0f}s ===\n{}\n".format(pa.now(), command, record["root"], code, time.time() - began, tail(out, 200)))
    print(tail(out, 40))
    print("full suite exit {} (appended to {})".format(code, os.path.join(evidence, "build.log")))
    sys.exit(code)


def main():
    kind = os.path.basename(sys.argv[0])
    parser = argparse.ArgumentParser(prog=kind)
    if kind == "pa-hook":
        name, rest = (sys.argv[1] if len(sys.argv) > 1 else ""), sys.argv[2:]
        if name not in HOOKS:
            raise pa.Problem("unknown hook {!r}; one of {}".format(name, ", ".join(sorted(HOOKS))))
        try:
            sys.exit(HOOKS[name](hook_input(), rest) or 0)
        except pa.Problem as e:  # a broken hook must not wedge the agent: report, don't block
            print("pa-hook {}: {}".format(name, e), file=sys.stderr)
            sys.exit(1)
    if kind == "pa-gate":
        parser.add_argument("task")
        parser.add_argument("--phase", help="the phase the task wants to leave (default: review)")
        parser.add_argument("--space")
        return cmd_gate(parser.parse_args())
    if kind == "pa-hooks":
        parser.add_argument("action", choices=("install", "remove"))
        parser.add_argument("worktree")
        parser.add_argument("--task")
        parser.add_argument("--space")
        parser.add_argument("--fix", action="store_true", help="a bug-fix task: lock test files")
        return cmd_hooks(parser.parse_args())
    if kind == "pa-worktree":
        parser.add_argument("action", choices=("check", "remove"))
        parser.add_argument("task", nargs="?")
        parser.add_argument("--open-cards", action="store_true", help="open a decision card for each candidate")
        parser.add_argument("--job", help="remove: the job whose worktree to remove (default: the task's main worktree)")
        parser.add_argument("--space")
        args = parser.parse_args()
        if args.action == "remove" and not args.task:
            raise pa.Problem("pa-worktree remove needs a task id")
        return cmd_worktree(args)
    if kind == "pa-worker":
        parser.add_argument("action", choices=("info", "delete", "close", "keep"))
        parser.add_argument("address", nargs="?")
        parser.add_argument("--keep-worktree", action="store_true", help="delete: leave its worktree in place")
        parser.add_argument("--discard", action="store_true", help="delete: delete the worktree even with uncommitted files or unpushed commits")
        parser.add_argument("--with-task", action="store_true", help="delete: also delete its task, with the task's evidence and cards (to the trash)")
        parser.add_argument("--evidence", help="close: the report you read, a file in evidence/<task>/")
        parser.add_argument("--leftovers", action="store_true", help="close: list the workers from before one-worker-per-job on one decision card (closes nothing)")
        parser.add_argument("--card", help="close: an answered leftovers card, in place of --evidence (without an address: every worker on it)")
        parser.add_argument("--dry-run", action="store_true", help="close --leftovers: only list what it would do")
        parser.add_argument("--quote", help="keep: the person's own words")
        parser.add_argument("--off", action="store_true", help="keep: the person says it may be closed again")
        parser.add_argument("--space")
        args = parser.parse_args()
        if args.action == "close" and args.evidence and args.card:
            raise pa.Problem("pa-worker close takes --evidence or --card, not both")
        if args.action == "close" and (sum(map(bool, (args.address, args.leftovers))) != 1 if not args.card else bool(args.leftovers)):
            raise pa.Problem("pa-worker close needs an address and --evidence, or --leftovers, or --card <id>")
        if args.action != "close" and not args.address:
            raise pa.Problem("pa-worker {} needs an address".format(args.action))
        return cmd_worker(args)
    if kind == "pa-trust":
        parser.add_argument("address")
        parser.add_argument("--space")
        return cmd_trust(parser.parse_args())
    if kind == "pa-full-suite":
        return cmd_full_suite(parser.parse_args())
    raise pa.Problem("invoke as pa-gate, pa-hooks, pa-hook, pa-worktree, pa-worker, pa-trust or pa-full-suite")


if __name__ == "__main__":
    try:
        main()
    except pa.Problem as e:
        print("{}: {}".format(os.path.basename(sys.argv[0]), e), file=sys.stderr)
        sys.exit(2)
