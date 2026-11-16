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
  pa-worker close --leftovers [--dry-run]        close every recorded worker that meets the same guards
  pa-worker keep <address> --quote TEXT [--off]  record that the person wants a worker kept open
  pa-hook <name>                                one hook, called by the agent harness (reads its JSON on stdin):
      lint-after-edit   after each edit: lint the changed file
      affected-tests    when a task is marked complete, and (--stop) before the session ends
      test-file-lock    before an edit: no test-file edits during a fix task
      guard             before a command or a read: nothing that cannot be taken back

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


def hook_guard(data, _args):
    record, agent, profile = hook_context(data)
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
    else:
        path = tool_input.get("file_path") or tool_input.get("path") or ""
        for pattern, what in DENY_PATHS + extra_paths:
            if path and re.search(pattern, path):
                block("Blocked by the Project Agent safety hook: {} ({}). {}".format(what, path, ROUTE))
    return 0


HOOKS = {"lint-after-edit": hook_lint, "affected-tests": hook_affected, "test-file-lock": hook_test_lock, "guard": hook_guard}


# ---- pa-hooks install / remove ------------------------------------------------------------------

def hook_settings():
    run = lambda name: {"type": "command", "command": os.path.join("$HOME", ".local", "bin", "pa-hook") + " " + name}
    return {
        "PreToolUse": [{"matcher": "Edit|Write|MultiEdit", "hooks": [dict(run("test-file-lock"), timeout=10)]},
                       {"matcher": "Bash|Read|Edit|Write|MultiEdit", "hooks": [dict(run("guard"), timeout=10)]}],
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
    rules = (pa.read_json(os.path.join(directory, "rules.json"), {}) or {}).get("rules", [])
    for rule in rules:
        if rule.get("status") == "active" and applies(rule, task, phase):
            needed.extend(str(x) for x in rule.get("evidence", []) if isinstance(x, str))
    evidence = os.path.join(directory, "evidence", args.task)
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
        workers = task.get("workers") or []
        authors = {w.get("address") for w in workers if w.get("role") in ("build", "design")}
        reviewers = {w.get("address") for w in workers if w.get("role") == "reviewer"}
        separate = bool(reviewers) and not (reviewers & authors)
        checks.append({"check": "reviewer is not the author", "ok": separate,
                       **({} if separate else {"why": "the ledger lists no reviewer worker separate from the build workers"})})
    ok = all(c["ok"] for c in checks)
    result = {"task": args.task, "phase": phase, "tier": tier, "passed": ok, "at": pa.now(), "checks": checks}
    if os.path.isdir(evidence):
        pa.write_json(os.path.join(evidence, "GATE.json"), result)
    for c in checks:
        print("{}  {}{}".format("ok  " if c["ok"] else "FAIL", c["check"], "" if c["ok"] else "  ({})".format(c["why"])))
    print("pa-gate {} ({} phase, {} tier): {}".format(args.task, phase, tier, "PASSED" if ok else "FAILED. The task does not move."))
    sys.exit(0 if ok else 1)


# ---- pa-worktree ------------------------------------------------------------------------------

def git(path, *args):
    r = subprocess.run(["git", "-C", path] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def worktree_facts(path, profile=None):
    """What deleting this folder would cost. None if it is not a worktree we know how to remove:
    a linked git worktree, or the profile's own kind (a marker file plus status and remove scripts)."""
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
            return None
        return {"main": False, "branch": facts.get("branch", ""), "uncommitted": int(facts.get("uncommitted", 0)),
                "unpushed": int(facts.get("unpushed", 0)), "removesBranches": bool(facts.get("removesBranches")),
                "kind": facts.get("kind", "worktree"), "remove": script("remove")}
    code, top, _ = git(path, "rev-parse", "--path-format=absolute", "--show-toplevel")
    code2, common, _ = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if code or code2 or os.path.realpath(top) != os.path.realpath(path):
        return None
    if os.path.realpath(common) == os.path.realpath(os.path.join(top, ".git")):
        return {"main": True}
    _c, status, _ = git(path, "status", "--porcelain")
    _c, branch, _ = git(path, "rev-parse", "--abbrev-ref", "HEAD")
    _c, ahead, _ = git(path, "rev-list", "--count", "HEAD", "--not", "--remotes")
    return {"main": False, "repo": os.path.dirname(os.path.realpath(common)), "branch": branch,
            "uncommitted": len([l for l in status.splitlines() if l.strip() and not l[3:].startswith(".claude/")]),
            "unpushed": int(ahead) if ahead.isdigit() else 0}


def inside(path, folder):
    path, folder = os.path.realpath(path or "/nonexistent"), os.path.realpath(folder)
    return path == folder or path.startswith(folder + os.sep)


def worktree_state(directory, agent, profile=None):
    """Per worktree in the ledger: its tasks (with their states and titles), its workers' fates, and anyone
    else working inside it. A worker's fate: live, closed (its ledger entry is finished), killed, removed or exited."""
    ledger = pa.read_json(os.path.join(directory, "ledger.json"), {}) or {}
    live = pa.live_terminals() or {}
    rows = {}
    for task in ledger.get("tasks", []):
        if not task.get("worktree") or not task.get("id"):
            continue
        path = os.path.realpath(os.path.expanduser(task["worktree"]))
        row = rows.setdefault(path, {"path": path, "tasks": [], "states": {}, "titles": {}, "keep": False, "workers": []})
        row["tasks"].append(task["id"])
        row["states"][task["id"]] = task.get("state")
        row["titles"][task["id"]] = task.get("title") or ""
        row["keep"] = row["keep"] or bool(task.get("keepWorktree"))
        for w in task.get("workers") or []:
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


def card_path(directory, task):
    return os.path.join(directory, "decisions", "d-wt-{}.json".format(task))


def cmd_worktree(args):
    record = find_task(os.getcwd())
    space = pa.local_id(args.space or (record or {}).get("space") or space_from_terminal() or "")
    if not space:
        raise pa.Problem("pass --space <id>, or run this from the Project Agent's terminal")
    directory, agent, profile = load_context(space)
    rows = worktree_state(directory, agent, profile)
    if args.action == "check":
        for row in rows.values():
            task, facts = row["tasks"][0], row["facts"]
            card = pa.read_json(card_path(directory, task))
            fates = [w["fate"] for w in row["workers"]]
            if not facts:
                print("{}: gone or not a git worktree ({})".format(task, row["path"]))
            elif facts.get("main"):
                print("{}: {} is the project's main checkout; never removed".format(task, row["path"]))
            elif row["keep"]:
                print("{}: kept (keepWorktree is set)".format(task))
            elif card and card.get("status") == "answered":
                print("{}: card {} answered {!r}{}".format(task, card["id"], card.get("answer"),
                      ": run `pa-worktree remove {}`".format(task) if str(card.get("answer", "")).lower().startswith("delete") else ": kept, not asked again"))
            elif card:
                print("{}: card {} is open".format(task, card["id"]))
            elif any(s not in ("done", "parked") for s in row["states"].values()):
                print("{}: tasks still {}; asked once every task using it is done or parked".format(
                    task, ", ".join("{} {}".format(i, s) for i, s in row["states"].items() if s not in ("done", "parked"))))
            elif "live" in fates:
                print("{}: in use by {}".format(task, ", ".join(w["address"] for w in row["workers"] if w["fate"] == "live")))
            elif row["others"]:
                print("{}: another terminal is working in it ({})".format(task, ", ".join(row["others"])))
            else:
                # Every task is done or parked and nothing runs inside: a candidate, however its workers ended.
                gone = ", ".join("{} ({})".format(w["name"] or w["address"], w["fate"]) for w in row["workers"]) or "no workers recorded"
                if not args.open_cards:
                    print("{}: candidate, every task done or parked ({}); --open-cards asks the person".format(task, gone))
                    continue
                loses_commits = facts.get("removesBranches") and facts["unpushed"] > 0
                dirty = facts["uncommitted"] > 0 or loses_commits
                kept = ("Deleting also deletes its branches ({}){}".format(facts["branch"], "; the commits that are on no remote would be LOST" if loses_commits else "")
                        if facts.get("removesBranches") else "The branch {} and its commits are kept either way".format(facts["branch"]))
                pa.write_json(card_path(directory, task), {
                    "id": "d-wt-" + task, "task": task, "worktree": row["path"],
                    "question": "Delete the worktree for {}?".format(", ".join(row["tasks"])),
                    "context": "{}. Workers: {}. No other terminal is working in it. Uncommitted files: {}. "
                               "Commits not on any remote: {}. {}{}.".format(
                                   row["path"], gone, facts["uncommitted"], facts["unpushed"], kept,
                                   "; deleting DISCARDS the uncommitted files" if facts["uncommitted"] else ""),
                    "options": [{"label": "Delete", "detail": "Remove the {}{}.".format(facts.get("kind", "worktree folder"),
                                 " with everything listed above that is not saved elsewhere" if dirty else "")},
                                {"label": "Keep", "detail": "Leave the worktree. It will not be asked about again."}],
                    "recommendation": "Keep" if dirty else "Delete", "status": "open", "answer": None, "note": None,
                    "snapshot": {"uncommitted": facts["uncommitted"], "unpushed": facts["unpushed"]}, "created": pa.now(), "answered": None})
                print("{}: opened card d-wt-{} (recommends {})".format(task, task, "Keep" if dirty else "Delete"))
        if not rows:
            print("No task in the ledger has a worktree.")
        return
    # remove: only what an answered card allows, re-checked now.
    row = next((r for r in rows.values() if args.task in r["tasks"]), None)
    if not row:
        raise pa.Problem("task {} has no worktree in the ledger".format(args.task))
    card = pa.read_json(card_path(directory, row["tasks"][0])) or {}
    if card.get("status") != "answered" or not str(card.get("answer", "")).lower().startswith("delete"):
        raise pa.Problem("no answered \"Delete\" decision card for this worktree (run `pa-worktree check --open-cards`; the person answers it)")
    if os.path.realpath(card.get("worktree") or "") != row["path"]:
        raise pa.Problem("the card is for {}, not {}".format(card.get("worktree"), row["path"]))
    facts = row["facts"]
    if not facts:
        print("Already gone: " + row["path"])
        return
    if facts.get("main"):
        raise pa.Problem("{} is the project's main checkout; it is never removed".format(row["path"]))
    if row["keep"]:
        raise pa.Problem("keepWorktree is set on the task")
    busy = [w["address"] for w in row["workers"] if w["fate"] == "live"] + row["others"]
    if busy:
        raise pa.Problem("still in use by {}; nothing was removed".format(", ".join(busy)))
    if facts["uncommitted"] > (card.get("snapshot") or {}).get("uncommitted", 0):
        raise pa.Problem("there are more uncommitted files now ({}) than when the person answered ({}); ask again".format(
            facts["uncommitted"], (card.get("snapshot") or {}).get("uncommitted", 0)))
    snapshot = card.get("snapshot") or {}
    if facts.get("removesBranches") and facts["unpushed"] > snapshot.get("unpushed", 0):
        raise pa.Problem("there are more unpushed commits now ({}) than when the person answered ({}); ask again".format(
            facts["unpushed"], snapshot.get("unpushed", 0)))
    print(remove_worktree(row["path"], facts) + " (card {})".format(card["id"]))


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
    row = next((r for r in worktree_state(directory, agent, profile).values() if task.get("id") in r["tasks"]), None)
    if row and row["facts"] and not row["facts"].get("main"):
        others = [w["address"] for w in row["workers"] if w["fate"] == "live" and w["address"] != address] + row["others"]
        out["worktree"] = {"path": row["path"], "kind": row["facts"].get("kind", "worktree"), "uncommitted": row["facts"]["uncommitted"],
                           "unpushed": row["facts"]["unpushed"], "removesBranches": bool(row["facts"].get("removesBranches")),
                           "branch": row["facts"]["branch"], "sharedWith": others, "keep": row["keep"]}
    return out, row


# ---- pa-worker close ------------------------------------------------------------------------
# The agent ends a worker once it has read its report. Every check below only reads; a refusal
# changes nothing. Only a terminal recorded in this space's ledger is ever ended.

GIT_TIMEOUT = 20  # seconds, for each git call made while checking or describing a worktree
CLOSABLE = ("idle", "done")  # hc statuses a worker may be closed in (also: exited, or already gone)


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


def recorded(ledger, address):
    """(task, entry) of the worker the ledger records at this address, or (None, None). An address
    recorded twice (an old, closed worker and a new one) gives the one not yet finished."""
    found = [(t, w) for t in ledger.get("tasks", []) for w in t.get("workers") or [] if w.get("address") == address]
    found.sort(key=lambda tw: bool(tw[1].get("finished")))
    return found[0] if found else (None, None)


def worker_label(address, entry, info):
    return "{} ({})".format(address, entry.get("name") or info.get("name") or "unnamed")


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


def status_refusal(address, entry, info):
    """Only an idle, done, exited or gone worker is closed."""
    if not info:
        return None
    status = info.get("status") or ""
    if status == "working":
        return "working", "{} is working; close it after its report".format(worker_label(address, entry, info))
    if status == "blocked":
        return "blocked", "{} is blocked on a question or a permission; bring that to the person, do not close it".format(
            worker_label(address, entry, info))
    if status in CLOSABLE or (not status and info.get("state") == "finished"):
        return None
    return "status", "{} has status {}; nothing was closed".format(address, status or "none")


def status_files(path):
    """[(XY status, path)] of the uncommitted files in a git checkout, without .claude/ ones; None if git cannot tell."""
    try:
        r = subprocess.run(["git", "-C", path, "status", "--porcelain"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode:
        return None
    files = []
    for line in r.stdout.splitlines():
        name = line[3:].split(" -> ")[-1].strip().strip('"')
        if name and not name.startswith(".claude/"):
            files.append((line[:2].strip(), name))
    return files


def worktree_refusal(task, profile):
    """The task's worktree holds uncommitted files, or its state cannot be read. A worktree that is
    gone (or none) is fine: there is nothing in it to lose."""
    if not task.get("worktree"):
        return None
    path = os.path.realpath(os.path.expanduser(task["worktree"]))
    if not os.path.exists(path):
        return None
    facts = worktree_facts(path, profile)
    custom = (profile or {}).get("worktree") or {}
    if not facts:
        why = ("the profile's worktree status script failed" if custom.get("marker") and os.path.exists(os.path.join(path, custom["marker"]))
               else "not a git worktree")
        return "unreadable", "cannot tell whether {} holds uncommitted work ({}); nothing was closed".format(path, why)
    if facts.get("remove"):  # the profile's own kind: its status script gives a count
        count, names = facts["uncommitted"], []
    else:
        files = status_files(path)
        if files is None:
            return "unreadable", "cannot tell whether {} holds uncommitted work (git status failed); nothing was closed".format(path)
        count, names = len(files), [f for _s, f in files]
    if count:
        listed = names[:5] + (["+{} more".format(len(names) - 5)] if len(names) > 5 else [])
        return "uncommitted", "{} has {} uncommitted file(s){}; have them committed or reported first. Nothing was closed".format(
            path, count, " ({})".format(", ".join(listed)) if listed else "")
    return None


def report_path(directory, task_id, address, given):
    """The report file the agent read, relative to evidence/<task>/. It must be inside that folder and not empty."""
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
    return os.path.relpath(path, folder)


def end_worker(directory, ledger, task, entry, address, report, by, trigger):
    """Ends one worker whose checks passed: kill if live, rm, then mark its ledger entry and journal
    it. The caller holds the ledger lock, re-read the ledger and re-checked the status. Returns the
    line to print. Never touches the worktree, the branch, the task or the evidence."""
    info = pa.hc_get(address)
    note = ""
    if info:
        if info.get("state") == "live":
            pa.call(["hc", "kill", address], check=False)
        pa.call(["hc", "rm", address], check=False)
        if pa.hc_get(address):
            note = " (ended {} but hc rm did not remove its record)".format(address)
    stamp = pa.now()
    entry.update(finished=True, closed=stamp, closedBy=by, report=report)
    task["updated"] = stamp
    pa.write_json(os.path.join(directory, "ledger.json"), ledger)
    name = entry.get("name") or "unnamed"
    append_journal(directory, {"by": by, "trigger": trigger, "task": task.get("id"),
                               "why": "closed worker {} ({}) after reading its report {}".format(address, name, report)})
    return "Closed worker {} ({}, task {}); its worktree and branch are untouched.{}".format(address, name, task.get("id"), note)


def close_worker(directory, agent, profile, address, evidence):
    """pa-worker close <address> --evidence F (spec 3.2 and 3.3)."""
    if "/" in address:
        raise pa.Problem("{} is on another machine; pa-worker close runs on the machine the space runs on".format(address))
    if address == agent.get("terminal"):
        raise pa.Problem("that is the Project Agent itself; it ends when its space is closed")
    task, entry = recorded(read_ledger(directory), address)
    if not entry:
        raise pa.Problem("{} is not a recorded worker of this space; nothing was closed (if it is yours, close it in Herdr)".format(address))
    info = pa.hc_get(address)
    refusal = identity_refusal(address, task, entry, info)
    if refusal:
        raise pa.Problem(refusal[1])
    if entry.get("finished") and not info:
        print("{} is already closed.".format(address))
        return
    report = report_path(directory, task["id"], address, evidence)
    refusal = status_refusal(address, entry, info) or worktree_refusal(task, profile)
    if refusal:
        raise pa.Problem(refusal[1])
    by = "agent" if by_an_agent() else "person"
    with ledger_lock(directory):
        ledger = read_ledger(directory)
        task, entry = recorded(ledger, address)
        if not entry:
            raise pa.Problem("{} is not a recorded worker of this space; nothing was closed (if it is yours, close it in Herdr)".format(address))
        info = pa.hc_get(address)
        refusal = identity_refusal(address, task, entry, info) or status_refusal(address, entry, info)
        if refusal:
            raise pa.Problem(refusal[1])
        print(end_worker(directory, ledger, task, entry, address, report, by, "close"))


def keep_worker(directory, agent, address, quote, off):
    """pa-worker keep <address> --quote "<the person's words>" [--off] (spec 3.5). It only marks the
    ledger entry; it reopens nothing."""
    if "/" in address:
        raise pa.Problem("{} is on another machine; pa-worker keep runs on the machine the space runs on".format(address))
    if address == agent.get("terminal"):
        raise pa.Problem("that is the Project Agent itself; it ends when its space is closed")
    if not off and not (quote or "").strip():
        raise pa.Problem('pa-worker keep needs --quote "<the person\'s own words>"')
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
            entry.update(keep=False, unkeptAt=stamp)
            why = "{} ({}) is no longer kept, at the person's word".format(address, name)
        else:
            entry.update(keep=True, keptAt=stamp, keepQuote=quote.strip())
            why = 'the person asked to keep {} open: "{}"'.format(address, quote.strip())
        task["updated"] = stamp
        pa.write_json(os.path.join(directory, "ledger.json"), ledger)
        append_journal(directory, {"by": "agent" if by_an_agent() else "person", "trigger": "keep", "task": task.get("id"), "why": why})
    print("{} ({}, task {}) {}.".format(address, name, task.get("id"), "is no longer kept" if off else "is marked keep: it is not closed by the agent"))


def close_leftovers(directory, agent, profile, dry_run):
    raise pa.Problem("pa-worker close --leftovers is not built yet")


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
        return close_worker(directory, agent, profile, args.address, args.evidence)
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
    did = []
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
            did.append(remove_worktree(tree["path"], row["facts"]))
            tree["removed"] = True
    if facts.get("task"):
        with open(os.path.join(directory, ".ledger.lock"), "a") as lock:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = os.path.join(directory, "ledger.json")
            ledger = pa.read_json(path)
            for t in (ledger or {}).get("tasks", []):
                if t.get("id") == facts["task"]:
                    t["workers"] = [w for w in t.get("workers") or [] if w.get("address") != facts["address"]]
                    if tree and tree.get("removed"):
                        t["worktree"] = None
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
        parser.add_argument("--leftovers", action="store_true", help="close: every recorded worker from before one-worker-per-job that meets the guards")
        parser.add_argument("--dry-run", action="store_true", help="close --leftovers: only list what it would do")
        parser.add_argument("--quote", help="keep: the person's own words")
        parser.add_argument("--off", action="store_true", help="keep: the person says it may be closed again")
        parser.add_argument("--space")
        args = parser.parse_args()
        if args.action == "close" and bool(args.address) == bool(args.leftovers):
            raise pa.Problem("pa-worker close needs an address and --evidence, or --leftovers")
        if args.action != "close" and not args.address:
            raise pa.Problem("pa-worker {} needs an address".format(args.action))
        return cmd_worker(args)
    if kind == "pa-full-suite":
        return cmd_full_suite(parser.parse_args())
    raise pa.Problem("invoke as pa-gate, pa-hooks, pa-hook, pa-worktree, pa-worker or pa-full-suite")


if __name__ == "__main__":
    try:
        main()
    except pa.Problem as e:
        print("{}: {}".format(os.path.basename(sys.argv[0]), e), file=sys.stderr)
        sys.exit(2)
