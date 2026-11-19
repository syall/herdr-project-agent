#!/usr/bin/env python3
"""The merge queue of a space (t-052 J2). Python standard library only (3.7+).

  pa-queue plan <task> [--job J] --repo <main checkout> --files F...   record the plan's files; say where to start
  pa-queue add <task> [--job J] --repo R        join the queue at the end, already stacked on the last entry
  pa-queue show [--repo R] [--json]             the queue with every state read from git now (writes nothing)
  pa-queue out <task> [--job J] --repo R --why T  take an entry out (with its dependents); those behind it are invalidated
  pa-queue restack --repo R [--dry-run]         rebase the invalidated entries as a chain, in merge order
  pa-queue verify <task> [--job J] --repo R     decide whether the branch is clean against the content checked
                                                (clean, moved or union: a short review; changed; stale)
  pa-queue refresh --repo R                     record short reviews, set passed or failed, restack after a failure

queue.json, in the space's state folder, is written by this tool only, under <state>/.queue.lock; each state change
appends one journal line ("trigger": "queue"). Workers' shells are refused. Clean is decided here and nowhere else:
from git, never from a worker's report. The tool rebases only branches of queue entries, in their own clean worktrees
with no open worker, with the old tip journalled first; on a conflict it aborts. It never pushes, fetches, merges, or
touches main or a remote. The queue card and the merge are J3's.

d-166 (t-052 J3c): a rebase conflict a rebase worker resolved by keeping both sides unchanged is `union` (the tool
proves it with git merge-file --union on the blobs) and needs a doc-only short review (only docstrings or comments?),
not a new check; an entry that leaves on a restack conflict keeps its approval as resumable, and add restores it once
that review passes.
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import sys
import time

sys.dont_write_bytecode = True  # keep tools/ free of caches
import pa  # noqa: E402
import pa_hooks  # noqa: E402
import pa_push  # noqa: E402

QUEUE_FILE = "queue.json"
LOCK_FILE = ".queue.lock"
LOCK_WAIT = 30  # seconds a write command waits for another one
NEAR = 30  # lines: main's changes this close to the change's own are named for the short reviewer
REBASE_TIMEOUT = 300
REVIEWS_FILE = "reviews.jsonl"
# Local or worktree settings that can run a program or resolve a conflict during a rebase (besides pa_push.RISKY).
REBASE_RISKY = re.compile(r"^(merge\..*|rerere\..*|rebase\..*|sequence\..*|commit\.gpgsign|core\.attributesfile|diff\..*)$", re.I)
IN_PROGRESS = ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD")
REBASE_OPTS = ["-c", "rerere.enabled=false", "-c", "rerere.autoUpdate=false", "-c", "commit.gpgSign=false",
               "-c", "core.editor=true", "-c", "sequence.editor=true"]
REBASE_ARGS = ["rebase", "--no-autostash", "--no-autosquash", "--no-update-refs", "--no-verify"]
START_LINE = re.compile(r"^Start: (\S+)[ \t]*$", re.M)
END_LINE = re.compile(r"^End: (\S+)[ \t]*$", re.M)
REVIEWER_LINE = re.compile(r"^Reviewer: (\S+)", re.M)
VERDICT_LINE = re.compile(r"^Verdict: (pass|must fix(?: \(\d+\))?)[ \t]*$", re.M)
DOC_QUESTION = ("is every conflict region only docstrings or comments, both sides kept word for word? 'yes' or 'no' per "
                "region with one line of reason")
QUESTION = ("for each place where the base's change lies within about 30 lines of the change, or where one calls, defines "
            "or shadows something the other uses (names, imports, constants, a test class or method), does the change "
            "still do what it did when it was reviewed? 'yes' or 'no' per place with one line of reason")


class Refused(Exception):
    pass


short = pa_push.short


def now():
    return pa.now()


# ---- the queue file, the lock, the journal ------------------------------------------------------

def queue_path(directory):
    return os.path.join(directory, QUEUE_FILE)


def load_queue(directory):
    path = queue_path(directory)
    if not os.path.exists(path):
        return {"version": 1, "repos": {}}
    data = pa.read_json_safe(path)
    if not isinstance(data, dict) or not isinstance(data.get("repos"), dict):
        raise Refused("{} is not a valid queue file; nothing was changed (the person decides what to do with it)".format(path))
    return data


@contextlib.contextmanager
def queue_lock(directory):
    """<state>/.queue.lock: one write command at a time in a space."""
    with open(os.path.join(directory, LOCK_FILE), "a") as f:
        end = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > end:
                    raise Refused("another pa-queue is running in this space; try again in a minute")
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def refuse_worker(directory):
    """Workers never run a write command: a task record above this folder, or this terminal is a ledger worker."""
    record = pa_hooks.find_task(os.getcwd())
    if record:
        raise Refused("workers do not run pa-queue (this folder is the worktree of task {}); say in your report what "
                      "is ready".format(record.get("task")))
    me = pa.local_id(os.environ.get("HC_TERMINAL_ID") or "")
    if me:
        for t in pa_hooks.read_ledger(directory).get("tasks", []):
            for w in t.get("workers") or []:
                if isinstance(w, dict) and w.get("address") == me:
                    raise Refused("workers do not run pa-queue ({} is a worker of {} in the ledger)".format(me, t.get("id")))


class Ctx:
    """One command's context: the space, the main checkout, a git session (empty hooks folder, no GIT_*), and the
    journal lines to write once queue.json is saved."""

    def __init__(self, directory, repo):
        self.directory = directory
        self.repo = os.path.realpath(repo) if repo else None
        self.s = pa_push.Session()
        self.run = pa_hooks.content_git(self.s.gate_git)
        self.ledger = pa_hooks.read_ledger(directory)
        self.events = []
        self.notes = []
        agent = pa.read_json_safe(os.path.join(directory, "agent.json")) or {}
        try:
            self.profile = pa.load_profile(agent.get("profile")) if agent.get("profile") else None
        except (pa.Problem, OSError, ValueError):
            self.profile = None

    def close(self):
        self.s.close()

    # git, read only, through the content path's runner (no replace refs, no commit-graph, no grafts)
    def out(self, *args):
        code, out, _err = self.run(self.repo, *args)
        return out if not code else None

    def tip(self, branch):
        if not isinstance(branch, str) or not branch:
            return None
        out = self.out("rev-parse", "--verify", "-q", "refs/heads/" + branch + "^{commit}")
        return out if out and pa_push.HEX.match(out) else None

    def merge_base(self, a, b):
        return self.out("merge-base", a, b) if a and b else None

    def is_ancestor(self, a, b):
        return bool(a and b) and self.run(self.repo, "merge-base", "--is-ancestor", a, b)[0] == 0

    def tree(self, sha):
        return self.out("rev-parse", "--verify", "-q", sha + "^{tree}") if sha else None

    def task(self, tid):
        return next((t for t in self.ledger.get("tasks", []) if t.get("id") == tid), None)

    def event(self, event, entry=None, **kw):
        line = {"by": "agent" if pa_hooks.by_an_agent() else "person", "trigger": "queue", "event": event, "repo": self.repo}
        if entry is not None:
            line.update(task=entry.get("task"), job=entry.get("job"))
        line.update(kw)
        self.events.append(line)

    def journal_now(self, event, entry=None, **kw):
        self.event(event, entry, **kw)
        pa_hooks.append_journal(self.directory, self.events.pop())

    def flush(self):
        for line in self.events:
            pa_hooks.append_journal(self.directory, line)
        self.events = []


def repo_rec(ctx, data):
    """The queue of the main checkout ctx.repo, made when missing (not saved until the command saves)."""
    code, top, _ = ctx.run(ctx.repo, "rev-parse", "--show-toplevel")
    if code or os.path.realpath(top) != ctx.repo:
        raise Refused("{} is not the top folder of a git repository".format(ctx.repo))
    code, common, _ = ctx.run(ctx.repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    common = os.path.realpath(common) if not code and common else None
    if common != os.path.realpath(os.path.join(ctx.repo, ".git")):
        raise Refused("{} is not a main checkout (it is a linked worktree); pass the repository's main checkout".format(ctx.repo))
    rec = data["repos"].get(ctx.repo)
    if not isinstance(rec, dict):
        main = pa_hooks.content_main(ctx.run, ctx.repo, ctx.profile)
        if not main:
            raise Refused("{} has no main branch (main, master, trunk or the profile's)".format(ctx.repo))
        rec = {"common": common, "main": main, "mainAt": None, "planned": [], "entries": [], "left": [], "merged": []}
        data["repos"][ctx.repo] = rec
    for k in ("planned", "entries", "left", "merged"):
        if not isinstance(rec.get(k), list):
            rec[k] = []
    if rec.get("common") != common:
        raise Refused("queue.json names another repository for {}".format(ctx.repo))
    return rec


def save(ctx, data, rec):
    rec["mainAt"] = ctx.tip(rec["main"]) or rec.get("mainAt")
    pa.write_json(queue_path(ctx.directory), data)
    ctx.flush()


def same(e, task, job):
    return e.get("task") == task and (e.get("job") or None) == (job or None)


# ---- tasks and worktrees -------------------------------------------------------------------------

def task_tree(task, job):
    trees = pa.task_worktrees(task)
    t = next((t for t in trees if t["job"] == job), None) if job else next((t for t in trees if t["main"]), None)
    return t["path"] if t else None


def pin_in(ctx, rec, tree):
    for pin in pa_hooks.gate_commits(tree, ctx.s.gate_git):
        if pin.get("common") == rec["common"]:
            return pin
    return None


def open_workers(task):
    return [w.get("address") for w in task.get("workers") or []
            if isinstance(w, dict) and not w.get("finished") and not w.get("closed")]


def git_dir(ctx, tree):
    code, out, _ = ctx.s.gate_git(tree, "rev-parse", "--absolute-git-dir")
    return out if not code and out else None


def in_progress(ctx, tree):
    folder = git_dir(ctx, tree)
    if not folder:
        return "its git folder cannot be read"
    found = [n for n in IN_PROGRESS if os.path.exists(os.path.join(folder, n))]
    return ", ".join(found) if found else None


def checked_out(ctx, branch):
    """Worktrees of the repository with branch checked out, from git worktree list."""
    code, out, _ = ctx.s.gate_git(ctx.repo, "worktree", "list", "--porcelain", "-z")
    if code:
        return None
    found, path = [], None
    for field in out.split("\0"):
        if field.startswith("worktree "):
            path = os.path.realpath(field[len("worktree "):])
        elif field == "branch refs/heads/" + branch and path:
            found.append(path)
    return found


def may_rebase(ctx, entry, task):
    """Why the tool may not rebase this entry's branch now (R18), or None."""
    tree = entry.get("worktree")
    if not task:
        return "it is not in the ledger"
    if not tree or not os.path.isdir(tree):
        return "its worktree {} is missing".format(tree)
    where = checked_out(ctx, entry["branch"])
    if where is None:
        return "git worktree list failed"
    if where != [tree] or tree == ctx.repo:
        return "its branch {} is checked out in {}, not only in its own worktree {}".format(entry["branch"], ", ".join(where) or "no worktree", tree)
    if tree not in [t["path"] for t in pa.task_worktrees(task)]:
        return "the ledger does not record {} as its worktree".format(tree)
    code, head, _ = ctx.s.gate_git(tree, "symbolic-ref", "--short", "-q", "HEAD")
    if code or head != entry["branch"]:
        return "its worktree is not on {}".format(entry["branch"])
    busy = open_workers(task)
    if busy:
        return "an open worker ({}) is on the task; restack after it is closed".format(", ".join(str(b) for b in busy))
    changed = pa_push.tracked_changes(ctx.s, tree)
    if changed is None:
        return "its worktree's status cannot be read"
    if changed:
        return "its worktree has uncommitted tracked changes ({})".format(pa_hooks.clean(changed[0])[:80])
    doing = in_progress(ctx, tree)
    if doing:
        return "an operation is in progress in its worktree ({})".format(doing)
    why = pa_push.task_settings_refusal(ctx.s, tree)
    if why:
        return why
    code, wt_config, _ = ctx.s.git(tree, ["config", "--local", "--bool", "--get", "extensions.worktreeConfig"])
    for scope in ("local", "worktree") if wt_config.strip() == "true" else ("local",):
        code, out, err = ctx.s.git(tree, ["config", "--" + scope, "--list", "--name-only"])
        if code:
            return "its {} git settings cannot be read ({})".format(scope, err.strip()[-200:])
        for key in out.split():
            if REBASE_RISKY.match(key):
                return ("its git setting {} ({}) can run a program or resolve a conflict during a rebase; remove it "
                        "and restack again").format(key, scope)
    tip = ctx.tip(entry["branch"])
    if not ctx.is_ancestor(entry.get("base"), tip):
        return "its recorded base {} is not below its branch (moved by hand?)".format(short(entry.get("base")))
    return None


# ---- what the checks saw, and the decision ---------------------------------------------------------

def stored_base(ctx, task_id, common, sha):
    """The stored record of the checked sha the gate compares on (pa_hooks.first_checked): the gate and add agree."""
    return pa_hooks.first_checked([r for r in pa_hooks.stored_records(ctx.directory, task_id)
                                   if r.get("common") == common and r.get("commit") == sha and not r.get("error")
                                   and isinstance(r.get("base"), str) and r["base"] and r["base"] != sha])


def checked_at_add(ctx, rec, task, branch):
    """The record of VERIFY.md's last Checked: commit, against its first stored base (or its merge base with onto); onto
    as the gate takes it for that commit (pa_hooks.content_onto, else main): the gate and add agree (MF-J2-1)."""
    evidence = os.path.join(ctx.directory, "evidence", task["id"])
    verify = pa_hooks.checked_commits(pa.read_text(os.path.join(evidence, "VERIFY.md")))
    review = pa_hooks.checked_commits(pa.read_text(os.path.join(evidence, "REVIEW.md")))
    is_c = lambda x: pa_hooks.is_commit(ctx.run, ctx.repo, x)
    x = next((c for c in reversed(verify) if is_c(c)), None)
    rv = next((c for c in reversed(review) if is_c(c)), None)
    if not x:
        raise Refused("VERIFY.md has no Checked: line naming a commit here; the queue takes only checks recorded as "
                      "content (R5 AC5.2): a fresh verifier writes it")
    onto = pa_hooks.content_onto(ctx.directory, task["id"], rec["common"], branch, ctx.run, ctx.repo, x) or rec["main"]
    saved = stored_base(ctx, task["id"], rec["common"], x)
    if saved:
        r = pa_hooks.content_record(ctx.run, ctx.repo, x, onto, saved["base"])
        if not r.get("error") and (r["change"], r["sequence"]) != (saved.get("change"), saved.get("sequence")):
            raise Refused("the stored record of the checked {} differs from what git says now".format(short(x)))
    else:
        r = pa_hooks.content_record(ctx.run, ctx.repo, x, onto)
    if r.get("error"):
        raise Refused("the checked {}: {}".format(short(x), r["error"]))
    if r["base"] == x:
        raise Refused("the checked {} is already on {}: nothing to queue".format(short(x), onto))
    return {"commit": x, "base": r["base"], "change": r["change"], "sequence": r["sequence"], "verify": x, "review": rv}


def full_checked(ctx, checked, onto):
    """checked's whole record, computed now against its own base; must give the stored digests."""
    r = pa_hooks.content_record(ctx.run, ctx.repo, checked["commit"], onto or "main", checked["base"])
    if r.get("error"):
        return r
    if (r["change"], r["sequence"]) != (checked.get("change"), checked.get("sequence")):
        return {"error": "the record of the checked {} computed now differs from the one queue.json holds".format(short(checked["commit"]))}
    return r


def file_paths(record):
    return {f.get("path") for f in record.get("files") or [] if isinstance(f, dict)}


def differing(a, b):
    x = {f.get("path"): f for f in a.get("files") or [] if isinstance(f, dict)}
    y = {f.get("path"): f for f in b.get("files") or [] if isinstance(f, dict)}
    return sorted(p for p in set(x) | set(y) if x.get(p) != y.get(p) and isinstance(p, str))


def decide(ctx, checked, tip, onto, onto_tip):
    """R6, the shared-file rule and d-166: {result: clean|moved|union|changed|stale, files, why, record, shared,
    unions, checked}."""
    out = {"result": "changed", "files": [], "why": "", "record": None, "shared": [], "unions": [], "checked": checked.get("commit")}
    if not tip or not onto_tip or ctx.merge_base(tip, onto_tip) != onto_tip:
        return dict(out, result="stale", why="it does not sit on the tip of {}".format(onto))
    now_rec = pa_hooks.content_record(ctx.run, ctx.repo, tip, onto)
    if now_rec.get("error"):
        return dict(out, why=now_rec["error"])
    out["record"] = now_rec
    seen = full_checked(ctx, checked, onto)
    if seen.get("error"):
        return dict(out, why=seen["error"])
    if (seen["change"], seen["sequence"]) == (now_rec["change"], now_rec["sequence"]):
        return dict(out, result="clean")
    moved, why, shared = pa_hooks.moved_only(ctx.run, ctx.repo, seen, now_rec)
    files = differing(seen, now_rec)
    if moved:
        return dict(out, result="moved", files=[s["path"] for s in shared], shared=shared)
    union, uwhy, unions, ushared = pa_hooks.union_only(ctx.run, ctx.s.gate_git, ctx.repo, seen, now_rec)
    if union:  # d-166 (t-052 J3c): never clean alone, a doc-only short review decides
        return dict(out, result="union", files=sorted([u["path"] for u in unions] + [s["path"] for s in ushared]),
                    shared=ushared, unions=unions)
    return dict(out, files=files, why=pa_hooks.compare_records(seen, now_rec) or why)


def ranges(hunks, side):
    out = []
    for h in hunks:
        start, count = h[side]
        out.append([start, start + max(count, 1) - 1])
    return out


def tip_line(line, hunks):
    """Line `line` of the new base as it lies in the tip, by the -U0 hunks new base -> tip; None when the change
    rewrote it."""
    shift = 0
    for h in hunks:
        (start, count), (_s, added) = h["old"], h["new"]
        if count == 0:
            shift += added if start < line else 0
        elif start + count - 1 < line:
            shift += added - count
        elif start <= line:
            return None
    return line + shift


def region_of(first, last, regions):
    """The index of the conflict region holding tip lines first..last whole, else None."""
    if first is None or last is None:
        return None
    return next((i for i, (a, b) in enumerate(regions) if a <= first and last <= b), None)


def shared_file(ctx, s, regions=None):
    """The shared-file item of one file (t-052 J2): main's hunks, the change's, and the pairs within NEAR lines. For a
    union file (J3c fix round 2) a pair whose two hunks both lie in one conflict region is left out: the doc-only
    question covers it; every other pair stays. `outside` (J3c fix round 3, Q-J2-1): main has a hunk that does not lie
    whole in a conflict region (one the mapping cannot place counts), near the change or not."""
    main_h = pa_hooks.diff_hunks(ctx.run, ctx.repo, s["oldBase"], s["newBase"])
    change_h = pa_hooks.diff_hunks(ctx.run, ctx.repo, s["newBase"], s["tip"])
    main, change = ranges(main_h, "new"), ranges(change_h, "old")
    near, outside = [], False
    for mh, m in zip(main_h, main):
        start, count = mh["new"]
        mr = region_of(tip_line(start, change_h), tip_line(start + count - 1, change_h), regions or []) if count else None
        outside = outside or mr is None
        for ch, c in zip(change_h, change):
            if max(0, max(m[0], c[0]) - min(m[1], c[1])) > NEAR:
                continue
            first, added = ch["new"]
            if mr is not None and added and region_of(first, first + added - 1, regions) == mr:
                continue
            near.append([m, c])
    item = {"path": s["path"], "mainHunks": main, "changeHunks": change, "near": near}
    if regions is not None:
        item.update(union=True, outside=outside)
    return item


def needs_for(ctx, entry, tip, decision, old_base, new_base, since=None):
    files = [shared_file(ctx, s) for s in decision["shared"]]
    # a union file is a shared file too (REVIEW J3c Must fix 1, Q-J2-1): main's hunks outside its regions are asked about
    files += [shared_file(ctx, u, u["regions"]) for u in decision.get("unions") or []]
    files.sort(key=lambda f: f["path"])
    n = max(1, len(entry.get("rebases") or []))
    name = "SHORT-REVIEW-{}{}-{}.md".format(entry["task"], "-" + entry["job"] if entry.get("job") else "", n)
    need = {"tip": tip, "tree": ctx.tree(tip), "file": name, "since": since or now(), "oldBase": old_base, "newBase": new_base,
            "files": files, "kind": "moved"}
    if decision.get("unions"):  # d-166 (t-052 J3c): the reviewer judges the conflict regions, lines of the tip's file
        need.update(kind="doc-only", checked=decision.get("checked"),
                    unions=[{"path": u["path"], "regions": u["regions"], "order": u["order"]} for u in decision["unions"]])
    return need


def handoff_lines(entry, need):
    doc = need.get("kind") == "doc-only"
    lines = ["{}: a short review is needed ({}): a fresh reviewer writes evidence/{}/{}".format(
        entry["task"], "doc-only: a conflict kept on both sides, d-166" if doc else "the shared-file rule", entry["task"], need["file"])]
    for u in need.get("unions") or []:
        lines.append("  {}: conflict regions at lines {} of {}:{} ({}); git diff {} {} -- {} (what the resolution added to "
                     "the checked change)".format(u["path"], ", ".join("{}-{}".format(a, b) for a, b in u["regions"]),
                                                  need["tip"], u["path"], u["order"], need.get("checked"), need["tip"], u["path"]))
    if doc:
        lines.append("  question: " + DOC_QUESTION + "; then 'Verdict: pass' or 'Verdict: must fix (<n>)'.")
    for f in need["files"]:
        near = "; ".join("main {}-{} near change {}-{}".format(m[0], m[1], c[0], c[1]) for m, c in f["near"]) or "none within {} lines".format(NEAR)
        lines.append("  {}: git diff {} {} -- {} (the base's change); git diff {} {} -- {} (the change); {}".format(
            f["path"], need["oldBase"], need["newBase"], f["path"], need["newBase"], need["tip"], f["path"], near))
    # the shared-file question (Q-J2-1): every moved file; a union file when main changed it outside the conflict
    # regions (near the change or far from it) or a near pair is listed; not when the conflict is main's only change
    if not doc or any(not f.get("union") or f["near"] or f.get("outside", True) for f in need["files"]):
        lines.append("  question: " + QUESTION + "; then 'Verdict: pass' or 'Verdict: must fix (<n>)'.")
    lines.append("  the file has the lines Start: <UTC time>, End: <UTC time>, Reviewer: <its address>, "
                 "Checked: {}, and its Verdict: line".format(need["tip"]))
    return lines


# ---- the tool rebase (R18) ------------------------------------------------------------------------

def tool_rebase(ctx, entry, new_tip, onto, data=None, rec=None):
    """Rebase entry's branch onto new_tip from its recorded base: 'done', 'conflict' (aborted: back at the old tip) or
    'stuck' (it could not be undone). The old tip is journalled, and recorded in queue.json when the entry is in it,
    before git runs."""
    tree, tip, base = entry["worktree"], ctx.tip(entry["branch"]), entry["base"]
    r = pa_hooks.content_record(ctx.run, ctx.repo, tip, entry.get("onto") or onto, base)
    if not r.get("error") and r["base"] != tip:
        pa_hooks.store_records(ctx.directory, entry["task"], [dict(r, source="queue", by="pa-queue")])
    entry.setdefault("rebases", []).append({"at": now(), "from": tip, "to": None, "onto": onto, "ontoTip": new_tip,
                                            "result": None, "files": []})
    if data is not None:
        save(ctx, data, rec)
    ctx.journal_now("rebase", entry, **{"from": tip, "onto": onto, "ontoTip": new_tip, "base": base, "worktree": tree})
    code, _out, _err = ctx.s.git(tree, REBASE_OPTS + REBASE_ARGS + ["--onto", new_tip, base, entry["branch"]], REBASE_TIMEOUT)
    if not code:
        return "done"
    ctx.s.git(tree, REBASE_OPTS + ["rebase", "--abort"], 60)
    if ctx.tip(entry["branch"]) != tip or in_progress(ctx, tree):
        return "stuck"
    return "conflict"


# ---- states ------------------------------------------------------------------------------------

def content_only(checks):
    """True when the only failing checks are the content check: what add's own decision may still accept (d-166)."""
    fails = [c for c in checks if isinstance(c, dict) and not c.get("ok")]
    return bool(fails) and all(c.get("check") == pa_hooks.CONTENT_CHECK for c in fails)


def gate_now(ctx, task, detail=False):
    result = pa_hooks.evaluate_gate(ctx.directory, task, "review", runner=ctx.s.gate_git)
    fails = [c for c in result["checks"] if not c["ok"]]
    pending = bool(fails) and all(c["check"] == pa_hooks.CONTENT_CHECK and "a short review is needed" in str(c.get("why")) for c in fails)
    why = "; ".join("{} ({})".format(c["check"], c.get("why")) for c in fails)
    if detail:
        return result["passed"], pending, why, content_only(result["checks"])
    return result["passed"], pending, why


def gate_pin(ctx, rec, tid, tip, content_failed=False):
    """GATE.json passed at tip; content_failed: or failed only on the content check there (add, a union, d-166)."""
    g = pa.read_json_safe(os.path.join(ctx.directory, "evidence", tid, "GATE.json"))
    ok = isinstance(g, dict) and (g.get("passed") is True or (content_failed and content_only(g.get("checks") or [])))
    return ok and g.get("phase") == "review" and \
        any(isinstance(c, dict) and c.get("common") == rec["common"] and c.get("commit") == tip for c in g.get("commits") or [])


def latest(items, test):
    found = [x for x in items if test(x)]
    return found[-1] if found else None


def span(run):
    return {k: run.get(k) for k in ("started", "ended", "seconds")} if run else None


def derive(ctx, rec):
    """[(entry, now)] with every state read from git and the files now (R1 AC1.2): nothing is written."""
    suites, reviews = pa_hooks.suite_runs(ctx.directory), pa_hooks.review_runs(ctx.directory)
    out, ahead, prev = [], None, rec["main"]
    for e in rec["entries"]:
        n = {"tip": ctx.tip(e.get("branch")), "ontoTip": ctx.tip(e.get("onto")), "content": None, "files": [], "suite": None,
             "review": None, "gate": None, "why": "", "state": "checking", "timing": None}
        if not n["tip"] or not n["ontoTip"]:
            n.update(state="invalidated", why="its branch or its onto branch is missing")
        elif ahead == "invalidated":
            n.update(state="invalidated", why="the entry ahead is invalidated")
        elif e.get("onto") != prev:
            n.update(state="invalidated", why="its onto {} is no longer the entry ahead ({})".format(e.get("onto"), prev))
        elif ctx.merge_base(n["tip"], n["ontoTip"]) != n["ontoTip"]:
            n.update(state="invalidated", why="it does not sit on the tip of {}".format(e.get("onto")))
        else:
            d = decide(ctx, e["checked"], n["tip"], e["onto"], n["ontoTip"])
            n.update(content=d["result"], files=d["files"])
            if d["result"] not in ("clean", "moved", "union"):
                n.update(state="changed", why="content changed: {}".format(d["why"] or ", ".join(d["files"])))
            else:
                tree = ctx.tree(n["tip"])
                s = latest(suites, lambda r: r.get("tree") == tree and tree and r.get("uncommitted") == 0 and r.get("reusable"))
                n["suite"] = s
                r = None
                if d["result"] in ("moved", "union"):
                    # RL-J2-V1, Nit-J2-1 (t-052 J3a): the keys short_review_passed checks, so show matches the gate;
                    # a union counts only a doc-only review (t-052 J3c)
                    r = latest(reviews, lambda x: x.get("task") == e["task"] and (x.get("job") or None) == (e.get("job") or None)
                               and x.get("common") == rec["common"] and x.get("tip") == n["tip"] and tree and x.get("tree") == tree
                               and (d["result"] != "union" or x.get("about") == "doc-only"))
                    n["review"] = r
                task = ctx.task(e["task"])
                if task is None:
                    n.update(state="failed", why="the task is not in the ledger")
                elif not (s and s.get("exit") == 0):
                    # The gate is read once its suite passed on this tree (it then decides passed or failed); before,
                    # the entry waits for its suite either way, or failed on it.
                    n["gate"] = {"passed": None, "pending": None, "pin": gate_pin(ctx, rec, e["task"], n["tip"]), "why": ""}
                    if s:
                        n.update(state="failed", why="the full suite exited {} on its tree ({}, {})".format(s.get("exit"), s.get("worker"), s.get("ended")))
                    elif r and r.get("verdict") != "pass":
                        n.update(state="failed", why="short review: {}".format(r.get("verdict")))
                    else:
                        n["why"] = "waiting for its suite"
                else:
                    ok, pending, why = gate_now(ctx, task)
                    n["gate"] = {"passed": ok, "pending": pending, "pin": gate_pin(ctx, rec, e["task"], n["tip"]), "why": why}
                    if s and s.get("exit") != 0:
                        n.update(state="failed", why="the full suite exited {} on its tree ({}, {})".format(s.get("exit"), s.get("worker"), s.get("ended")))
                    elif r and r.get("verdict") != "pass":
                        n.update(state="failed", why="short review: {}".format(r.get("verdict")))
                    elif not ok and not pending:
                        n.update(state="failed", why="the gate fails at its tip: " + why)
                    elif s and ok and n["gate"]["pin"] and (d["result"] == "clean" or (r and r.get("verdict") == "pass")):
                        n["state"] = "passed"
                    else:
                        waits = [w for w, c in (("its suite", not s), ("pa-gate at its tip", not n["gate"]["pin"]),
                                                ("its short review", d["result"] in ("moved", "union") and not r)) if c]
                        n["why"] = "waiting for " + ", ".join(waits) if waits else ""
                if s and r:
                    a, b = s.get("seconds") or 0, r.get("seconds") or 0
                    n["timing"] = {"suite": span(s), "review": span(r), "bottleneck": "suite" if a >= b else "review",
                                   "by": round(abs(a - b), 1)}
        out.append((e, n))
        ahead, prev = n["state"], e.get("branch")
    return out


def set_state(ctx, entry, state, why=""):
    if entry.get("state") != state:
        ctx.event("state", entry, **{"from": entry.get("state"), "to": state, "why": why})
        entry["state"] = state


def leave(ctx, rec, entry, why):
    """entry leaves the queue; its approval is void. Only a restack conflict keeps it resumable (d-166, t-052 J3c):
    with the checked digests, so add can restore it once for a conflict kept on both sides. An approval still held
    for its doc-only review is void for good."""
    rec["entries"].remove(entry)
    approval, held = entry.get("approval"), entry.get("heldApproval")
    resumable = why == "conflict" and isinstance(approval, dict)
    if isinstance(approval, dict):
        approval = dict(approval, void=why, **({"resumable": True} if resumable else {}))
    elif isinstance(held, dict) and isinstance(held.get("approval"), dict):
        approval = dict(held["approval"], void=why)
    left = {"task": entry["task"], "job": entry.get("job"), "branch": entry["branch"], "state": entry.get("state"),
            "why": why, "at": now(), "tip": ctx.tip(entry["branch"]), "approval": approval}
    if resumable:
        left["checked"] = {k: (entry.get("checked") or {}).get(k) for k in ("commit", "change", "sequence")}
    rec["left"].append(left)
    ctx.event("left", entry, why=why, tip=ctx.tip(entry["branch"]))


def sync_main(ctx, rec):
    """Main moved by another route: entries main now holds are merged by hand, in queue order (RL-R2-2); a rewound
    main, or main holding an entry but not one ahead of it, is refused before anything is written."""
    main_tip = ctx.tip(rec["main"])
    if not main_tip:
        raise Refused("the main branch {} does not exist".format(rec["main"]))
    at = rec.get("mainAt")
    if at and at != main_tip and not ctx.is_ancestor(at, main_tip):
        raise Refused("local {} was moved by hand to {}, which is not ahead of {} where pa-queue last saw it (rewound or "
                      "rewritten); nothing was written: the person decides".format(rec["main"], short(main_tip), short(at)))
    held = [ctx.is_ancestor(ctx.tip(e.get("branch")) or e.get("tip"), main_tip) for e in rec["entries"]]
    n = 0
    while n < len(held) and held[n]:
        n += 1
    if any(held[n:]):
        late = rec["entries"][held.index(True, n)]
        raise Refused("local {} holds {} but not {} ahead of it: the queue's order was broken by hand; nothing was "
                      "written".format(rec["main"], late["task"], rec["entries"][n]["task"]))
    gone = set()
    for e in rec["entries"][:n]:
        rec["merged"].append({"task": e["task"], "job": e.get("job"), "tip": ctx.tip(e["branch"]), "from": at, "to": main_tip,
                              "at": now(), "by": "hand"})
        ctx.event("hand-merge", e, tip=ctx.tip(e["branch"]), main=main_tip)
        gone.add(e["branch"])
    rec["entries"] = rec["entries"][n:]
    for e in rec["entries"]:
        if e.get("onto") in gone:
            e["onto"] = rec["main"]


# ---- restack ------------------------------------------------------------------------------------

def touched(ctx, a, b):
    try:
        return {f["path"] for f in pa_hooks.raw_files(ctx.run, ctx.repo, a, b)}
    except pa_hooks.ContentError:
        return set()


def restack(ctx, data, rec, dry=False):
    """The chain, in merge order (R13, as the person confirmed it): from the first entry not on the tip of the entry
    ahead, each entry is rebased onto the entry ahead as it is after that one's own restack (main for the first).
    Returns (lines, why it stopped or None)."""
    lines, stop = [], None
    prev, prev_tip = rec["main"], ctx.tip(rec["main"])
    anchor, between, started = prev_tip, set(), False
    for e in list(rec["entries"]):
        tip = ctx.tip(e["branch"])
        if not started and e.get("onto") == prev and tip and ctx.merge_base(tip, prev_tip) == prev_tip:
            prev, prev_tip, anchor = e["branch"], tip, tip
            continue
        started = True
        if dry:
            mine = touched(ctx, e["base"], tip) if tip else set()
            under = touched(ctx, e["checked"]["base"], anchor) | between
            shared = sorted(mine & under)
            guess = "shared: {}; clean only if the change lines are only moved (then a short review), else a recheck".format(
                ", ".join(shared)) if shared else "clean"
            lines.append("{}: git rebase --onto {} {} {}  ({})".format(e["task"], prev_tip, e["base"], e["branch"], guess))
            between |= mine
            prev, prev_tip = e["branch"], "<{} after its restack>".format(e["branch"])
            continue
        task = ctx.task(e["task"])
        why = may_rebase(ctx, e, task)
        if why:
            stop = "{}: {}; it and every entry behind it stay invalidated".format(e["task"], why)
            for later in rec["entries"][rec["entries"].index(e):]:
                set_state(ctx, later, "invalidated", "restack stopped at {}".format(e["task"]))
            break
        old = tip
        result = tool_rebase(ctx, e, prev_tip, prev, data, rec)
        last = e["rebases"][-1]
        if result == "stuck":
            last.update(result="conflict", to=ctx.tip(e["branch"]))
            leave(ctx, rec, e, "the rebase could not be undone: {} needs a person".format(e["worktree"]))
            stop = "{}: the rebase onto {} could not be undone; {} needs a person".format(e["task"], prev, e["worktree"])
            break
        if result == "conflict":
            last.update(result="conflict", to=old)
            leave(ctx, rec, e, "conflict")
            lines.append("{}: conflict rebasing onto {}; it left the queue (back at {}): a rebase worker, then the "
                         "check Rounds asks for, then pa-queue add".format(e["task"], prev, short(old)))
            continue
        new = ctx.tip(e["branch"])
        d = decide(ctx, e["checked"], new, prev, prev_tip)
        last.update(to=new, result=d["result"], files=d["files"])
        if d["result"] in ("clean", "moved", "union"):
            e.update(onto=prev, base=prev_tip, tip=new, suite=None, review=None)
            set_state(ctx, e, "checking", "restacked onto {} ({})".format(prev, d["result"]))
            if d["result"] in ("moved", "union"):
                e["needs"] = {"shortReview": needs_for(ctx, e, new, d, e["checked"]["base"], prev_tip)}
                lines += handoff_lines(e, e["needs"]["shortReview"])
            else:
                e["needs"] = None
            lines.append("{}: rebased onto {} at {}: {} (run pa-gate {} again; its suite runs on the new tree)".format(
                e["task"], prev, short(prev_tip), d["result"], e["task"]))
            prev, prev_tip = e["branch"], new
        else:
            leave(ctx, rec, e, "content changed: " + (", ".join(d["files"]) or d["why"]))
            lines.append("{}: its content changed in the rebase ({}); it left the queue for the R8 check".format(
                e["task"], ", ".join(d["files"]) or d["why"]))
    return lines, stop


# ---- short reviews --------------------------------------------------------------------------------

def record_reviews(ctx, rec):
    """refresh (a): a short review file the tool named, valid, becomes one line of content/reviews.jsonl."""
    have = pa_hooks.review_runs(ctx.directory)
    for e in rec["entries"]:
        need = (e.get("needs") or {}).get("shortReview")
        tip = ctx.tip(e["branch"])
        if not need or need.get("tip") != tip or any(r.get("task") == e["task"] and r.get("tip") == tip for r in have):
            continue
        path = os.path.join(ctx.directory, "evidence", e["task"], need["file"])
        if not os.path.isfile(path):
            ctx.notes.append("{}: waiting for its short review ({})".format(e["task"], need["file"]))
            continue
        text = pa.read_text(path)
        problems = []
        start, end = START_LINE.search(text), END_LINE.search(text)
        t0 = pa.parse_time(start.group(1)) if start else None
        t1 = pa.parse_time(end.group(1)) if end else None
        if t0 is None or t1 is None or t1 < t0:
            problems.append("it needs Start: and End: lines with UTC times (End not before Start)")
        if tip not in pa_hooks.checked_commits(text):
            problems.append("its Checked: line must name the tip {}".format(tip))
        verdict = VERDICT_LINE.findall(text)
        if not verdict:
            problems.append("it has no Verdict: line")
        who = REVIEWER_LINE.search(text)
        address = pa.local_id(who.group(1)) if who else None
        task = ctx.task(e["task"]) or {}
        workers = [w for w in task.get("workers") or [] if isinstance(w, dict)]
        authors = {w.get("address") for w in workers if str(w.get("role") or "").lower() in ("build", "design")}
        since = pa.parse_time(str(need.get("since")))
        fresh = [w for w in workers if w.get("address") == address and str(w.get("role") or "").lower() == "reviewer"
                 and pa.parse_time(str(w.get("started"))) is not None and since is not None and pa.parse_time(str(w["started"])) >= since]
        if not address:
            problems.append("it has no Reviewer: line")
        elif address in authors:
            problems.append("its reviewer {} is an author of the task".format(address))
        elif not fresh:
            problems.append("its reviewer {} is not a reviewer of the task started after the rebase".format(address))
        if problems:
            ctx.notes.append("{}: {} not recorded: {}".format(e["task"], need["file"], "; ".join(problems)))
            continue
        with open(path, "rb") as f:
            sha = hashlib.sha256(f.read()).hexdigest()
        line = {"kind": "short-review", "at": now(), "task": e["task"], "job": e.get("job"), "repo": ctx.repo, "common": rec["common"],
                "branch": e["branch"], "tip": tip, "tree": ctx.tree(tip), "file": need["file"], "fileSha256": sha, "worker": address,
                "started": start.group(1), "ended": end.group(1), "seconds": round(t1 - t0, 1),
                "verdict": "pass" if verdict[-1] == "pass" else "must fix", "about": need.get("kind") or "moved"}
        os.makedirs(pa_hooks.content_dir(ctx.directory), exist_ok=True)
        with open(os.path.join(pa_hooks.content_dir(ctx.directory), REVIEWS_FILE), "a") as f:
            f.write(json.dumps(line) + "\n")
        have.append(line)
        ctx.event("review", e, file=need["file"], verdict=line["verdict"], started=line["started"], ended=line["ended"],
                  seconds=line["seconds"], worker=address)


# ---- printing -----------------------------------------------------------------------------------

def run_text(r):
    if not r:
        return "none"
    word = "passed" if r.get("exit", 0) == 0 and r.get("verdict", "pass") == "pass" else "failed"
    return "{} ({} to {}, {} s)".format(word, r.get("started"), r.get("ended"), r.get("seconds"))


def show_lines(ctx, rec, pairs):
    lines = ["Queue of {} (main {} at {}):".format(rec_repo_name(ctx), rec["main"], short(ctx.tip(rec["main"])))]
    if not pairs:
        lines.append("  empty")
    mergeable, front = [], True
    for i, (e, n) in enumerate(pairs, 1):
        lines.append("{}. {}{} {} on {} (base {}, tip {}): stored {}, now {}{}".format(
            i, e["task"], " " + e["job"] if e.get("job") else "", e["branch"], e.get("onto"), short(e.get("base")), short(n["tip"]),
            e.get("state"), n["state"], " ({})".format(n["why"]) if n["why"] else ""))
        need = (e.get("needs") or {}).get("shortReview")
        lines.append("   content {}; suite {}; short review {}".format(
            n["content"] or "-", run_text(n["suite"]),
            run_text(n["review"]) if n["review"] else ("needed: " + need["file"] if n["content"] in ("moved", "union") and need else "not needed")))
        if n["timing"]:
            lines.append("   bottleneck: {} by {} s".format(n["timing"]["bottleneck"], n["timing"]["by"]))
        if e.get("approval"):
            lines.append("   approval: {}".format(json.dumps(e["approval"])))
        elif e.get("heldApproval"):
            lines.append("   approval held until its doc-only short review passes: {}".format(json.dumps(e["heldApproval"])))
        front = front and n["state"] == "passed"
        if front:
            mergeable.append(e["task"])
    lines.append("mergeable: {}".format(", ".join(mergeable) or "none"))
    for l in rec["left"][-5:]:
        lines.append("left: {} ({}, {})".format(l["task"], l.get("why"), l.get("at")))
    for m in rec["merged"][-5:]:
        lines.append("merged: {} at {} ({})".format(m["task"], short(m.get("tip")), m.get("by") or "queue"))
    return lines + ctx.notes


def rec_repo_name(ctx):
    return ctx.repo


# ---- commands -----------------------------------------------------------------------------------

def space(args):
    directory, _agent = pa.space_for_command(args.space)
    return directory


@contextlib.contextmanager
def writing(args):
    directory = space(args)
    refuse_worker(directory)
    with queue_lock(directory):
        ctx = Ctx(directory, args.repo)
        try:
            data = load_queue(directory)
            yield ctx, data, repo_rec(ctx, data)
        finally:
            ctx.close()


def cmd_plan(args):
    with writing(args) as (ctx, data, rec):
        files = sorted(set(args.files))
        dep, branch = None, None
        for e in reversed(rec["entries"]):
            if not same(e, args.task, args.job) and set(e.get("files") or []) & set(files):
                dep, branch = {"task": e["task"], "job": e.get("job")}, e["branch"]
                break
        if dep is None:
            for p in reversed(rec["planned"]):
                if not same(p, args.task, args.job) and set(p.get("files") or []) & set(files):
                    dep = {"task": p["task"], "job": p.get("job")}
                    t = ctx.task(p["task"])
                    tree = task_tree(t, p.get("job")) if t else None
                    if tree:
                        code, b, _ = ctx.s.gate_git(tree, "symbolic-ref", "--short", "-q", "HEAD")
                        branch = b if not code and b else None
                    break
        rec["planned"] = [p for p in rec["planned"] if not same(p, args.task, args.job)]
        rec["planned"].append({"task": args.task, "job": args.job, "files": files, "dependsOn": dep, "at": now()})
        start = branch or ("{}'s branch".format(dep["task"]) if dep else rec["main"])
        ctx.event("plan", {"task": args.task, "job": args.job}, files=files, dependsOn=dep, start=start)
        save(ctx, data, rec)
    print("{}: start from {}{}".format(args.task, start, " (it shares {} with {})".format(
        ", ".join(sorted(set(files))), dep["task"]) if dep else ""))
    return 0


def cmd_add(args):
    with writing(args) as (ctx, data, rec):
        lines = add(ctx, data, rec, args.task, args.job)
    print("\n".join(lines))
    return 0


def add(ctx, data, rec, tid, job):
    sync_main(ctx, rec)
    task = ctx.task(tid)
    if not task:
        raise Refused("no task {} in the ledger".format(tid))
    if any(same(e, tid, job) for e in rec["entries"]):
        raise Refused("{} is already in the queue".format(tid))
    tree = task_tree(task, job)
    if job and tree and os.path.realpath(tree) != os.path.realpath(os.path.expanduser(str(task.get("worktree") or ""))):
        # t-052 J3a A5 (Q-J3-7): pa-gate pins task.worktree only, so a job elsewhere could never pass in the queue
        raise Refused("the gate reads {}'s worktree only ({}), not job {}'s ({}); set the task's worktree to the job's "
                      "worktree, run pa-gate {} again, then add".format(tid, task.get("worktree"), job, tree, tid))
    pin = pin_in(ctx, rec, tree) if tree else None
    if not pin or not pin.get("commit") or not pin.get("branch"):
        raise Refused("{} has no worktree on a branch of {}".format(tid, ctx.repo))
    tip, branch = pin["commit"], pin["branch"]
    main_tip = ctx.tip(rec["main"])
    if ctx.is_ancestor(tip, main_tip):
        raise Refused("{} at {} is already in {}: nothing to queue".format(tid, short(tip), rec["main"]))
    ok, _pending, why, only_content = gate_now(ctx, task, detail=True)
    # d-166 (t-052 J3c): a branch a rebase worker put on the last entry, keeping a conflict's both sides, fails the
    # gate's content check until it is queued (the gate compares it against main): add's own decision below must then
    # say union, with no tool rebase in between; any other failure refuses here.
    if not ok and not only_content:
        raise Refused("the gate does not pass now: {}".format(why))
    if not gate_pin(ctx, rec, tid, tip, content_failed=not ok):
        raise Refused("GATE.json's pin is not the branch tip {} (run pa-gate {} again)".format(short(tip), tid))
    if pin.get("uncommitted"):
        raise Refused("its worktree has uncommitted tracked changes ({}); commit or drop them first".format(pin["uncommitted"]))
    planned = next((p for p in rec["planned"] if same(p, tid, job)), None)
    dep = planned.get("dependsOn") if planned else None
    if dep and not any(same(e, dep.get("task"), dep.get("job")) for e in rec["entries"]) and \
            not any(same(m, dep.get("task"), dep.get("job")) for m in rec["merged"]):
        raise Refused("it depends on {} (they share planned files), which is not ahead of it in the queue".format(dep.get("task")))
    onto = rec["entries"][-1]["branch"] if rec["entries"] else rec["main"]
    onto_tip = ctx.tip(onto)
    checked = checked_at_add(ctx, rec, task, branch)
    entry = {"task": tid, "job": job, "branch": branch, "worktree": tree, "onto": onto, "dependsOn": dep, "state": "checking",
             "added": now(), "base": upstream(ctx, rec, tip, onto_tip), "tip": tip, "checked": checked,
             "files": sorted(set((planned or {}).get("files") or []) | touched(ctx, checked["base"], checked["commit"])),
             "needs": None, "suite": None, "review": None, "rebases": [], "approval": None}
    lines = []
    if not ok and entry["base"] != onto_tip:
        raise Refused("the gate does not pass now: {} (a branch whose conflict was kept on both sides joins only from "
                      "the tip of {}: rebase it there first)".format(why, onto))
    if entry["base"] != onto_tip:
        why = may_rebase(ctx, entry, task)
        if why:
            raise Refused("it needs a rebase onto {} and the tool may not rebase it: {}".format(onto, why))
        result = tool_rebase(ctx, entry, onto_tip, onto)
        if result != "done":
            ctx.journal_now("refused", entry, why="conflict rebasing onto {}".format(onto))
            raise Refused("conflict rebasing onto {}{}: it needs a rebase worker onto {}, then the check Rounds asks for, "
                          "then pa-queue add again".format(onto, "" if result == "conflict" else " (it could not be undone: "
                                                           "{} needs a person)".format(tree), onto))
        new = ctx.tip(branch)
        d = decide(ctx, checked, new, onto, onto_tip)
        entry["rebases"][-1].update(to=new, result=d["result"], files=d["files"])
        if d["result"] not in ("clean", "moved", "union"):
            why = ", ".join(d["files"]) or d["why"]
            ctx.journal_now("refused", entry, why="content changed in the rebase: " + why)
            raise Refused("the rebase onto {} changed its content ({}): it needs the R8 check on {}; the branch is kept "
                          "rebased at {}".format(onto, d["why"] or why, why, short(new)))
        entry.update(base=onto_tip, tip=new)
        if d["result"] in ("moved", "union"):
            entry["needs"] = {"shortReview": needs_for(ctx, entry, new, d, checked["base"], onto_tip)}
            lines += handoff_lines(entry, entry["needs"]["shortReview"])
        lines.append("{}: rebased onto {} at {}: {} (run pa-gate {} again)".format(tid, onto, short(onto_tip), d["result"], tid))
    else:
        d = decide(ctx, checked, tip, onto, onto_tip)
        if d["result"] not in ("clean", "moved", "union"):
            raise Refused("its content is not the content checked ({})".format(d["why"] or ", ".join(d["files"])))
        if not ok and d["result"] != "union":
            raise Refused("the gate does not pass now: {}".format(why))
        if d["result"] in ("moved", "union"):
            # the rebase worker's resolution, not the tool's: a review started after its commit counts, as the gate's
            since = committed_at(ctx, tip) if d["result"] == "union" else None
            entry["needs"] = {"shortReview": needs_for(ctx, entry, tip, d, checked["base"], onto_tip, since)}
            lines += handoff_lines(entry, entry["needs"]["shortReview"])
    lines += resume_approval(ctx, rec, entry, d)
    rec["entries"].append(entry)
    rec["planned"] = [p for p in rec["planned"] if not same(p, tid, job)]
    ctx.event("add", entry, onto=onto, tip=entry["tip"], position=len(rec["entries"]),
              rebase=entry["rebases"][-1]["result"] if entry["rebases"] else None)
    if entry.get("heldApproval"):  # a review written before add counts at once (refresh records it otherwise)
        record_reviews(ctx, rec)
        lines += restore_approvals(ctx, rec)
    save(ctx, data, rec)
    lines.append("{} joined the queue at position {}, on {} (state checking)".format(tid, len(rec["entries"]), onto))
    return lines


def committed_at(ctx, sha):
    ts = pa_hooks.committer_time(ctx.run, ctx.repo, sha)
    return pa_hooks.iso_at(ts) if ts is not None else None


def resume_approval(ctx, rec, entry, d):
    """d-166 (t-052 J3c): the task's last left[] record, a restack conflict whose approval is resumable, for the same
    task, job and checked digests and not used yet: on a union the approval is held for the doc-only short review
    (restore_approvals) and the record is used; on anything else it is not carried, for good."""
    last = latest(rec["left"], lambda l: same(l, entry["task"], entry.get("job")))
    approval = (last or {}).get("approval")
    if not isinstance(approval, dict) or not approval.get("resumable") or last.get("resumed"):
        return []
    checked = {k: entry["checked"].get(k) for k in ("commit", "change", "sequence")}
    if d["result"] != "union" or last.get("checked") != checked:
        last["resumed"] = {"at": now(), "carried": False, "why": "not a conflict kept on both sides ({})".format(d["result"])
                           if d["result"] != "union" else "the checked content differs"}
        return ["{}: its approval ({}) is not carried: only a conflict kept on both sides keeps it (d-166)".format(
            entry["task"], approval.get("card") or "the queue card")]
    kept = {k: v for k, v in approval.items() if k not in ("void", "resumable")}
    entry["heldApproval"] = {"approval": kept, "left": last.get("at"), "file": entry["needs"]["shortReview"]["file"]}
    last["resumed"] = {"at": now(), "carried": True}
    ctx.event("approval-held", entry, card=approval.get("card"), file=entry["heldApproval"]["file"])
    return ["{}: its approval ({}) is held until the doc-only short review {} passes".format(
        entry["task"], approval.get("card") or "the queue card", entry["heldApproval"]["file"])]


def restore_approvals(ctx, rec):
    """An approval held for a doc-only short review comes back once that review, recorded for the entry's tip and
    tree, passed (d-166): carried names the review. A failed review makes the entry fail (derive), which voids it."""
    lines, reviews = [], pa_hooks.review_runs(ctx.directory)
    for e in rec["entries"]:
        held = e.get("heldApproval")
        if not isinstance(held, dict):
            continue
        tip = ctx.tip(e["branch"])
        tree = ctx.tree(tip)
        r = latest(reviews, lambda x: x.get("task") == e["task"] and (x.get("job") or None) == (e.get("job") or None)
                   and x.get("common") == rec["common"] and x.get("tip") == tip and tree and x.get("tree") == tree
                   and x.get("about") == "doc-only")
        if not r or r.get("verdict") != "pass":
            continue
        e["approval"] = dict(held["approval"], carried="doc-only, short review {}".format(r.get("file")))
        e["heldApproval"] = None
        ctx.event("approval-restored", e, card=held["approval"].get("card"), file=r.get("file"), tip=tip)
        lines.append("{}: its approval ({}) is restored: {}".format(e["task"], held["approval"].get("card") or "the queue card",
                                                                   e["approval"]["carried"]))
    return lines


def upstream(ctx, rec, tip, onto_tip):
    """Where the branch's own commits begin, for the tool rebase (RL-J2-3, t-052 J3a A2): the newest old tip an entry
    had before a restack (rebases[].from) that the branch holds and the onto tip no longer does: a task started on an
    entry's branch that was restacked before it joined. Else the merge base, as in J2. The rebase then replays only
    the task's own commits; a wrong choice replays too much (a conflict) or drops one (changed): both fail safe."""
    base = ctx.merge_base(tip, onto_tip)
    best = None
    for e in rec["entries"]:
        for r in e.get("rebases") or []:
            old = r.get("from")
            if not old or old in (base, tip) or not ctx.is_ancestor(old, tip) or ctx.is_ancestor(old, onto_tip):
                continue
            if base and not ctx.is_ancestor(base, old):
                continue
            if best is None or ctx.is_ancestor(best, old):
                best = old
    return best or base


def with_dependents(rec, entry):
    """entry and every entry whose dependsOn (pa-queue plan) leads to it: they leave together (Q-B, Q-J3-8)."""
    out, changed = [entry], True
    while changed:
        changed = False
        for other in rec["entries"]:
            dep = other.get("dependsOn") or {}
            if other not in out and any(same(x, dep.get("task"), dep.get("job")) for x in out):
                out.append(other)
                changed = True
    return out


def cmd_show(args):
    directory = space(args)
    data = load_queue(directory)
    repos = [os.path.realpath(args.repo)] if args.repo else sorted(data["repos"])
    shown, text = [], []
    for repo in repos:
        rec = data["repos"].get(repo)
        ctx = Ctx(directory, repo)
        try:
            if not isinstance(rec, dict):
                text.append("Queue of {}: empty".format(repo))
                shown.append({"repo": repo, "entries": [], "left": [], "merged": []})
                continue
            pairs = derive(ctx, rec)
            text += show_lines(ctx, rec, pairs)
            shown.append({"repo": repo, "main": rec["main"], "mainAt": rec.get("mainAt"),
                          "entries": [dict(e, now=n) for e, n in pairs], "left": rec["left"], "merged": rec["merged"]})
        finally:
            ctx.close()
    if args.json:
        print(json.dumps(shown[0] if args.repo else shown, indent=2, default=str))
    else:
        print("\n".join(text) if text else "No queue in this space.")
    return 0


def cmd_out(args):
    with writing(args) as (ctx, data, rec):
        entry = next((e for e in rec["entries"] if same(e, args.task, args.job)), None)
        if not entry:
            raise Refused("{} is not in the queue".format(args.task))
        i = rec["entries"].index(entry)
        out = with_dependents(rec, entry)  # t-052 J3a A4 (D-J2-7, Q-J3-8): as a failure does
        for o in out:
            leave(ctx, rec, o, "taken out: " + args.why if o is entry else "built on {}, which was taken out".format(args.task))
            rec["planned"] = [p for p in rec["planned"] if not same(p, o["task"], o.get("job"))]
        behind = rec["entries"][i:]
        for e in behind:
            set_state(ctx, e, "invalidated", "{} was taken out".format(args.task))
        save(ctx, data, rec)
    print("{} left the queue{}; {} behind it invalidated: run pa-queue restack".format(
        args.task, "" if len(out) == 1 else " with " + ", ".join(o["task"] for o in out[1:]), len(behind)))
    return 0


def cmd_restack(args):
    if args.dry_run:
        directory = space(args)
        ctx = Ctx(directory, args.repo)
        try:
            data = load_queue(directory)
            rec = data["repos"].get(ctx.repo)
            lines = restack(ctx, data, rec, dry=True)[0] if isinstance(rec, dict) else []
        finally:
            ctx.close()
        print("\n".join(lines) if lines else "nothing to restack")
        return 0
    with writing(args) as (ctx, data, rec):
        sync_main(ctx, rec)
        lines, stop = restack(ctx, data, rec)
        save(ctx, data, rec)
    print("\n".join(lines) if lines else "nothing to restack")
    if stop:
        print("pa-queue: restack stopped: " + stop, file=sys.stderr)
        return 1
    return 0


def cmd_verify(args):
    with writing(args) as (ctx, data, rec):
        entry = next((e for e in rec["entries"] if same(e, args.task, args.job)), None)
        if entry:
            tip, onto = ctx.tip(entry["branch"]), entry["onto"]
            d = decide(ctx, entry["checked"], tip, onto, ctx.tip(onto))
            if tip != entry.get("tip"):
                entry["rebases"].append({"at": now(), "from": entry.get("tip"), "to": tip, "onto": onto, "ontoTip": ctx.tip(onto),
                                         "result": d["result"], "files": d["files"]})
                if d["result"] in ("clean", "moved", "union"):
                    entry.update(tip=tip, base=ctx.tip(onto))
                    entry["needs"] = {"shortReview": needs_for(ctx, entry, tip, d, entry["checked"]["base"], ctx.tip(onto))} \
                        if d["result"] in ("moved", "union") else None
            need = (entry.get("needs") or {}).get("shortReview")
            subject = entry
        else:
            task = ctx.task(args.task)
            if not task:
                raise Refused("no task {} in the ledger".format(args.task))
            tree = task_tree(task, args.job)
            pin = pin_in(ctx, rec, tree) if tree else None
            if not pin or not pin.get("commit"):
                raise Refused("{} has no worktree on a branch of {}".format(args.task, ctx.repo))
            onto = rec["entries"][-1]["branch"] if rec["entries"] else rec["main"]
            tip = pin["commit"]
            checked = checked_at_add(ctx, rec, task, pin.get("branch"))
            d = decide(ctx, checked, tip, onto, ctx.tip(onto))
            subject = {"task": args.task, "job": args.job, "rebases": []}
            need = needs_for(ctx, subject, tip, d, checked["base"], ctx.tip(onto)) if d["result"] in ("moved", "union") else None
        ctx.event("verify", subject, tip=tip, onto=onto, result=d["result"], files=d["files"])
        save(ctx, data, rec)
    print("{} at {} on {}: {}{}".format(args.task, short(tip), onto, d["result"],
                                       " ({})".format(d["why"] or ", ".join(d["files"])) if d["result"] not in ("clean",) else ""))
    if d["result"] in ("moved", "union") and need:
        print("\n".join(handoff_lines(subject, need)))
    return 0 if d["result"] in ("clean", "moved", "union") else 1


def cmd_refresh(args):
    with writing(args) as (ctx, data, rec):
        sync_main(ctx, rec)
        record_reviews(ctx, rec)
        restored = restore_approvals(ctx, rec)
        pairs = derive(ctx, rec)
        first = None
        for e, n in pairs:
            if first is None and n["state"] in ("failed", "changed"):
                first = (e, n)
                continue
            if first is None:
                set_state(ctx, e, n["state"], n["why"])
                e["suite"] = dict(span(n["suite"]), tree=n["suite"].get("tree"), exit=n["suite"].get("exit"), worker=n["suite"].get("worker")) if n["suite"] else None
                e["review"] = dict(span(n["review"]), tip=n["review"].get("tip"), verdict=n["review"].get("verdict"),
                                   worker=n["review"].get("worker"), file=n["review"].get("file")) if n["review"] else None
                if n["timing"] and e.get("timed") != n["tip"]:
                    e["timed"] = n["tip"]
                    ctx.event("timing", e, timing=n["timing"], tip=n["tip"])
        lines = restored
        if first:
            e, n = first
            set_state(ctx, e, "failed", n["why"])
            out = with_dependents(rec, e)
            i = rec["entries"].index(e)
            for o in out:
                leave(ctx, rec, o, n["why"] if o is e else "built on {}, which failed".format(e["task"]))
            for later in rec["entries"][i:]:
                set_state(ctx, later, "invalidated", "{} failed".format(e["task"]))
            lines.append("{} failed ({}) and left the queue{}".format(e["task"], n["why"], "" if len(out) == 1 else
                                                                      " with " + ", ".join(o["task"] for o in out[1:])))
        more, stop = restack(ctx, data, rec)
        lines += more
        save(ctx, data, rec)
        lines += ["{}. {}: {}".format(i, e["task"], e.get("state")) for i, e in enumerate(rec["entries"], 1)] + ctx.notes
        lines.append("pa-queue show --repo {} reads every state now".format(ctx.repo))
    print("\n".join(lines))
    if stop:
        print("pa-queue: restack stopped: " + stop, file=sys.stderr)
        return 1
    return 0


COMMANDS = {"plan": cmd_plan, "add": cmd_add, "show": cmd_show, "out": cmd_out, "restack": cmd_restack,
            "verify": cmd_verify, "refresh": cmd_refresh}


def parser():
    p = argparse.ArgumentParser(prog="pa-queue", description="The merge queue of a space (t-052).")
    sub = p.add_subparsers(dest="command")
    for name in ("plan", "add", "out", "verify"):
        s = sub.add_parser(name)
        s.add_argument("task")
        s.add_argument("--job")
        s.add_argument("--repo", required=True, help="the repository's main checkout")
        s.add_argument("--space")
        if name == "plan":
            s.add_argument("--files", nargs="+", required=True, help="the files the approved plan lists")
        if name == "out":
            s.add_argument("--why", required=True)
    s = sub.add_parser("show")
    s.add_argument("--repo")
    s.add_argument("--json", action="store_true")
    s.add_argument("--space")
    for name in ("restack", "refresh"):
        s = sub.add_parser(name)
        s.add_argument("--repo", required=True)
        s.add_argument("--space")
        if name == "restack":
            s.add_argument("--dry-run", action="store_true", help="print the commands and the predictions; write nothing")
    return p


def main():
    args = parser().parse_args(sys.argv[1:])
    if not args.command:
        parser().print_usage(sys.stderr)
        return 2
    if getattr(args, "task", None):
        pa.ident(args.task, "task id")
    try:
        return COMMANDS[args.command](args)
    except Refused as r:
        print("pa-queue: refused: {}".format(r), file=sys.stderr)
        return 1
    except pa_hooks.ContentError as e:
        print("pa-queue: refused: {}".format(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except pa.Problem as e:
        print("pa-queue: {}".format(e), file=sys.stderr)
        sys.exit(2)
