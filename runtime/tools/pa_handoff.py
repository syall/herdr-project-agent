#!/usr/bin/env python3
"""pa-handoff: write a worker's handoff from the ledger, git, the active rules and the known-flake list (t-169).
Python standard library only (3.7+).

  pa-handoff check|recheck|fix|verify|review|queue <task> [--job J] [--base B] [--round N] [--diff] [--logic]
             [--no-mutations] [--public] [--items TEXT | --items-file PATH] [--repo R] [--space S] [--force]

It writes <space>/evidence/<task>/handoff-<kind>[-<job>][-r<round>].md and prints its path; the Project Agent adds
only its judgement lines (--items). What it reads:
  - the ledger task: title, tier, evidence folder, the job's worktree (its `worktrees` entry, `main` the task's
    `worktree`); the round is the highest build round of its workers (a fix: one more), unless --round;
  - git in that worktree: the branch, HEAD, the base (--base; else the merge-base with main; a recheck, a --diff
    verify or review and a fix: the last `Checked:` commit in REVIEW.md, else VERIFY.md) and the failing-test commits
    in base..HEAD (a subject with "failing");
  - rules.json: r-few-mutations (its `mutations` field {low, normal, high, recheck}, else the numbers in its text) and
    r-test-runs (pa-run, --local only where a test needs this Mac) when active; no number is ever copied from an
    inactive rule or from an earlier handoff;
  - <space>/flakes.txt: the known flakes, one per line (blank lines and lines starting with # skipped);
  - agent.json: the Project Agent's address (`terminal`) and profile (the --public line).
queue wraps `pa-queue brief <task> --repo <main checkout>` (repo: --repo, else the worktree's main checkout).
It never starts a worker and never writes the ledger or any file but the handoff; an existing handoff is kept
unless --force. check and recheck are the low tier's (a normal or high tier task gets verify and review).
"""
import argparse
import contextlib
import io
import os
import re
import sys

sys.dont_write_bytecode = True  # keep tools/ free of caches
import pa  # noqa: E402
import pa_hooks  # noqa: E402

KINDS = ("check", "recheck", "fix", "verify", "review", "queue")
TIERS = ("low", "normal", "high")
FLAKES_FILE = "flakes.txt"
MAIN = "main"
RUNS = "pa-run, --local only where a test needs this Mac; say where each ran"
RUNS_LOCAL = "pa-run --local; say so"
READS = {"check": ("intent.md", "spec.md", "plan.md", "FINDINGS.md", "DECISIONS.md", "build.log"),
         "verify": ("intent.md", "spec.md", "plan.md", "FINDINGS.md", "DECISIONS.md", "build.log"),
         "review": ("intent.md", "spec.md", "plan.md", "FINDINGS.md", "DECISIONS.md")}
CHECKS_SUFFIX = ("Scripts in a file under $T, run with sh \"$T\"/x.sh. No rm of a path built by command substitution. "
                 "Leave no background process.")
HOME_RULES = ("no tool against the real ~/.config/herdr-pa or a real space{}; open no file under ~/.claude, ~/.codex "
              "or ~/.config/herdr-app; start no Claude session; do not reload Herdr; no push, merge or install; ")
BAR_BUILD = ("Must-fix bar: breaks a requirement, the plan, a verification point, safety or security. Else Recorded "
             "limit or Nit; say for each whether it is small and inside this task's files (fixed before merge) or "
             "accepted (no follow-up task).")
ENDING = "'Checked: <full HEAD id>' and 'Verdict: pass' or 'Verdict: must fix (<n>)'"
REVIEWER = "'Reviewer:' your own address (echo $HC_TERMINAL_ID)"


class Refused(Exception):
    pass


# ---- readers -----------------------------------------------------------------------------------------------------

def parse_flakes(text):
    return [l.strip() for l in (text or "").splitlines() if l.strip() and not l.strip().startswith("#")]


def read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, ValueError):
        return None


def active_rule(doc, rid):
    rules = doc.get("rules") if isinstance(doc, dict) else None
    for r in rules if isinstance(rules, list) else []:
        if isinstance(r, dict) and r.get("id") == rid and r.get("status") == "active":
            return r
    return None


TEXT_NUMBERS = {"low": r"low tier at most (\d+)", "normal": r"normal(?: and high)? tier at most (\d+)",
                "high": r"(?:normal and )?high tier at most (\d+)", "recheck": r"changed logic \(then at most (\d+)"}


def mutation_numbers(doc):
    """{low, normal, high, recheck: int or None} from the active r-few-mutations ({} when it is not active)."""
    rule = active_rule(doc, "r-few-mutations")
    if not rule:
        return {}
    field = rule.get("mutations") if isinstance(rule.get("mutations"), dict) else {}
    text = rule.get("text") if isinstance(rule.get("text"), str) else ""
    out = {}
    for key, pattern in TEXT_NUMBERS.items():
        value = field.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            out[key] = value
            continue
        m = re.search(pattern, text)
        out[key] = int(m.group(1)) if m else None
    return out


def git(path, *args):
    code, out, err = pa_hooks.git(path, *args)
    if code:
        raise Refused("git {} in {}: {}".format(" ".join(args), path, err or "exit {}".format(code)))
    return out


def short(path, rev):
    return git(path, "rev-parse", "--short=7", rev + "^{commit}")


def checked_commit(evidence):
    """The last 'Checked: <id>' in REVIEW.md, else VERIFY.md, and the last Verdict line of that file."""
    for name in ("REVIEW.md", "VERIFY.md"):
        text = read_text(os.path.join(evidence, name)) or ""
        ids = re.findall(r"^Checked:\s*([0-9a-f]{7,40})\s*$", text, re.M)
        if ids:
            verdicts = re.findall(r"^Verdict:\s*(.+?)\s*$", text, re.M)
            return ids[-1], name, verdicts[-1] if verdicts else ""
    return None, None, ""


def must_count(verdict):
    m = re.match(r"must fix \((\d+)\)", verdict or "")
    return "{} Must fix".format(m.group(1)) if m else "Must fix findings"


def queue_brief(directory, repo, task, job):
    """The text `pa-queue brief` prints for this entry (read only)."""
    import pa_queue
    args = argparse.Namespace(task=task, job=job, repo=repo, space=os.path.basename(directory))
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            pa_queue.cmd_brief(args)
    except pa_queue.Refused as r:
        raise Refused("pa-queue brief: {}".format(r))
    return out.getvalue().rstrip("\n")


def main_checkout(worktree):
    common = git(worktree, "rev-parse", "--git-common-dir")
    if not os.path.isabs(common):
        common = os.path.join(worktree, common)
    return os.path.realpath(os.path.dirname(os.path.realpath(common)))


# ---- the facts of one handoff --------------------------------------------------------------------------------------

class Facts(object):
    pass


def gather(args, directory, agent):
    f = Facts()
    f.kind, f.job, f.directory = args.command, args.job or MAIN, directory
    ledger = pa_hooks.read_ledger(directory)
    task = next((t for t in ledger.get("tasks") or [] if isinstance(t, dict) and t.get("id") == args.task), None)
    if not task:
        raise Refused("{} is not in the ledger".format(args.task))
    f.task, f.title, f.tier = args.task, task.get("title") or "", task.get("tier")
    if f.tier not in TIERS:
        raise Refused("{} has no tier in the ledger (low, normal or high)".format(args.task))
    if f.kind in ("check", "recheck") and f.tier != "low":
        raise Refused("{} is {} tier: check and recheck are the low tier's; use verify and review".format(args.task, f.tier))
    f.label = args.task if f.job == MAIN else "{} {}".format(args.task, f.job)
    entry = next((w for w in task.get("worktrees") or [] if isinstance(w, dict) and w.get("job") == f.job), None)
    f.wt = (entry or {}).get("path") or (task.get("worktree") if f.job == MAIN else None)
    if not f.wt:
        raise Refused("{} has no worktree for job {} in the ledger".format(args.task, f.job))
    f.ev = os.path.join(directory, task.get("evidence") or os.path.join("evidence", args.task))
    f.pa = agent.get("terminal")
    if not f.pa:
        raise Refused("agent.json has no terminal (the Project Agent's address)")
    f.profile = agent.get("profile") or "profile"
    rounds = [w.get("round") for w in task.get("workers") or [] if isinstance(w, dict) and w.get("role") == "build"
              and isinstance(w.get("round"), int)]
    built = max(rounds) if rounds else 1
    f.round = args.round or (built + 1 if f.kind == "fix" else built)
    f.diff = f.kind == "recheck" or bool(args.diff)
    f.items = items_text(args)
    # git
    f.branch = git(f.wt, "rev-parse", "--abbrev-ref", "HEAD")
    f.head = short(f.wt, "HEAD")
    f.checked, f.checked_in, f.verdict = checked_commit(f.ev)
    if f.kind == "queue":
        f.base = f.base_label = None
    elif args.base:
        f.base, f.base_label = short(f.wt, args.base), "base {}".format(short(f.wt, args.base))
    elif f.diff or f.kind == "fix":
        if not f.checked:
            raise Refused("no 'Checked:' commit in {}/REVIEW.md or VERIFY.md; pass --base".format(f.ev))
        f.base, f.base_label = short(f.wt, f.checked), None
    else:
        f.base = short(f.wt, git(f.wt, "merge-base", "HEAD", MAIN))
        f.base_label = "base {} {}".format(MAIN, f.base)
    if f.checked and f.kind in ("recheck", "fix"):
        f.checked = short(f.wt, f.checked)
    elif f.kind == "fix":
        f.checked = f.base
    f.tests = []
    if f.base and f.kind != "fix":
        log = git(f.wt, "log", "--reverse", "--abbrev=7", "--format=%h %s", "{}..HEAD".format(f.base))
        f.tests = [l.split(" ", 1)[0] for l in log.splitlines() if re.search(r"\bfailing\b", l, re.I)]
    # rules and flakes
    rules = pa.read_json_safe(os.path.join(directory, "rules.json")) or {}
    f.mutations = mutation_numbers(rules)
    f.runs = RUNS if active_rule(rules, "r-test-runs") else RUNS_LOCAL
    f.flakes = parse_flakes(read_text(os.path.join(directory, FLAKES_FILE)))
    f.notes = []
    return f


def items_text(args):
    if args.items_file:
        text = read_text(args.items_file)
        if text is None:
            raise Refused("cannot read {}".format(args.items_file))
        return text.strip()
    return (args.items or "").strip()


# ---- text ---------------------------------------------------------------------------------------------------------

def with_items(line, items):
    return "{} {}".format(line, items) if items else line


def read_line(f, names):
    there = [n for n in names if os.path.isfile(os.path.join(f.ev, n))]
    if not there:
        return "Read the files in {}.".format(f.ev)
    return "Read {}.".format(", ".join([os.path.join(f.ev, there[0])] + there[1:]))


def flakes_sentence(f):
    if not f.flakes:
        return ""
    return " Known flakes: {}. If only those fail, rerun each failing one alone and say so.".format("; ".join(f.flakes))


def where_line(f):
    extra = []
    if f.tests:
        extra.append("tests {}".format(", ".join(f.tests)))
    if f.base_label and not f.diff:
        extra.append(f.base_label)
    return "Worktree {}, branch {}, HEAD {}{}.".format(f.wt, f.branch, f.head, " ({})".format("; ".join(extra)) if extra else "")


def failing_first(f):
    what = "the tests of {}".format(", ".join(f.tests)) if f.tests else "the new tests"
    overlay = "" if f.kind == "recheck" else " with the tests overlay"
    return "Failing-first: {} fail at {}{} and pass at HEAD ({})".format(what, f.base, overlay, f.runs)


def mutations_line(f, out):
    """(the mutation step's text, ending in ' -> out.' or '.')."""
    if f.no_mutations:
        return "No mutations (r-few-mutations: a docs-only or test-only change)."
    if f.diff and not f.logic:
        return "No mutations (r-few-mutations: a recheck whose fix changed no logic)."
    key = "recheck" if f.diff else f.tier
    n = f.mutations.get(key)
    cover = "each running only the tests that cover it -> {}.".format(out)
    if n is None and f.mutations:
        f.notes.append("r-few-mutations is active but gives no {} number (its mutations field or text); the handoff "
                       "names none".format(key))
    if n is None:
        return "A few mutations on changed logic (no active r-few-mutations rule gives a number), " + cover
    if f.diff:
        return "At most {} mutations, on the changed lines only (r-few-mutations, a fix that changed logic), {}".format(n, cover)
    first = "" if f.tier == "low" else ", first check"
    return "At most {} mutations on changed logic (r-few-mutations, {} tier{}), {}".format(n, f.tier, first, cover)


def rules_line(f, builder=False, reviewer=False):
    if builder:
        public = "public repo, nothing {}-specific; ".format(f.profile) if f.public else ""
        return ("Rules: " + public + HOME_RULES.format("") + "git commands as plain separate commands. Multi-line "
                "scripts in a file under $T, run with sh \"$T\"/x.sh. No rm of a path built by command substitution. "
                "Leave no background process; commit your work; worktree clean at the end. "
                "Report with `hc send {}` when done or stuck.".format(f.pa))
    public = "public repo; " if f.public else ""
    clean = "(worktree clean at the end)" if reviewer else "(mutations reverted, worktree clean at the end)"
    return ("Rules: " + public + HOME_RULES.format(" other than pa-full-suite's own run") +
            "edit and commit nothing {}; git commands as plain separate commands. ".format(clean) + CHECKS_SUFFIX +
            " Report with `hc send {}` when done or stuck.".format(f.pa))


def r(f):
    return "-r{}".format(f.round) if f.round > 1 else ""


def section(f, what):
    """The section title a check appends ('' when it writes a new file: job main, round 1, full)."""
    parts = ([f.job] if f.job != MAIN else []) + (["Round {}".format(f.round)] if f.round > 1 else []) + \
        ([what] if what else [])
    if not parts or (f.job == MAIN and f.round == 1 and what == "review"):
        return ""
    return "## {} ({} {}..{})".format(" ".join(parts), "diff" if f.diff else "full,", f.base, f.head)


def text_check(f):
    rr = r(f)
    return [
        "# {} check (check role, {} tier, round {}, check: full)".format(f.label, f.tier, f.round), "",
        "Fresh context; you are not an author of {0}. Task {0}: {1}. {2}".format(f.label, f.title, read_line(f, READS["check"])),
        "{} Full check: the whole diff {}..{}.".format(where_line(f), f.base, f.head),
        "You report; you never fix. The verifier's and reviewer's job in one:",
        "1. pa-full-suite once at HEAD (never under a timeout), output to full-suite-check{}.out.{}".format(rr, flakes_sentence(f)),
        "2. {} -> check-failing-first{}.out.".format(failing_first(f), rr),
        "3. " + mutations_line(f, "check-mut{}.log".format(rr)),
        with_items("4. Review against each intent item.", f.items),
        "Must-fix bar: breaks an intent item, a verification point, safety or security. Else Recorded limit (name a "
        "follow-up) or Nit.",
        "Write VERIFY.md and REVIEW.md, each ending {}; REVIEW.md has {}.".format(ENDING, REVIEWER),
        "Evidence folder: {}.".format(f.ev), rules_line(f)]


def text_recheck(f):
    rr, prev = r(f), max(f.round - 1, 1)
    return [
        "# {} recheck (check role, {} tier, round {}, check: diff)".format(f.label, f.tier, f.round), "",
        "Fresh context; you are not an author of {0}. Task {0}: {1}. The round {2} check at {3} found {4} (read "
        "{5}/REVIEW.md '## Must fix'); round {6} fixed it: read FINDINGS.md, DECISIONS.md, build.log 'Round {6}'.".format(
            f.label, f.title, prev, f.base, must_count(f.verdict), f.ev, f.round),
        "{} Diff check: {}..{}.".format(where_line(f), f.base, f.head),
        "You report; you never fix.",
        "1. pa-full-suite once at HEAD (never under a timeout), output to full-suite-check{}.out.{}".format(rr, flakes_sentence(f)),
        "2. {} -> check-failing-first{}.out.".format(failing_first(f), rr),
        "3. " + mutations_line(f, "check-mut{}.log".format(rr)),
        with_items("4. Review: each Must fix is closed and nothing else changed.", f.items),
        "Must-fix bar: breaks a Must fix's fix, a verification point, safety or security. Else Recorded limit or Nit; "
        "say whether each is small and inside this task's files (fixed before merge) or accepted (no follow-up task).",
        "Append '## Round {} recheck (diff {}..{})' to VERIFY.md and REVIEW.md, each ending {}; REVIEW.md's section has "
        "{}.".format(f.round, f.base, f.head, ENDING, REVIEWER),
        "Evidence folder: {}.".format(f.ev), rules_line(f)]


def text_fix(f):
    hooks = pa_hooks.find_task(f.wt) or {}
    installed = " Hooks installed." if hooks.get("task") == f.task else ""
    suite = ("pa-full-suite once at the end of the Test phase, before the commit." if f.tier == "high"
             else "No pa-full-suite.")
    found = must_count(f.verdict) if f.checked_in else "Must fix findings"
    where = "{}/{} '## Must fix' (and VERIFY.md)".format(f.ev, f.checked_in or "REVIEW.md")
    return [
        "# {} fix (build role, {} tier, round {})".format(f.label, f.tier, f.round), "",
        "Task {}: {}. The check at {} found {}: read {}. Also read plan.md, FINDINGS.md, DECISIONS.md.".format(
            f.label, f.title, f.checked, found, where),
        "Worktree {}, branch {}, HEAD {}.{}".format(f.wt, f.branch, f.head, installed),
        with_items("Fix only the Must fix findings. Recorded limits and Nits stay as they are.", f.items),
        "1. A test first that fails at {}: commit it while failing. 2. The fix. 3. Affected tests ({}). {} 4. Append a "
        "'Round {}' section to FINDINGS.md, DECISIONS.md and build.log with real output. Commit; report commit ids and "
        "files touched.".format(f.head, f.runs, suite, f.round),
        "Evidence folder: {}.".format(f.ev), rules_line(f, builder=True)]


def text_verify(f):
    sfx = ("-" + f.job if f.job != MAIN else "") + r(f)
    title = section(f, "")
    write = ("Append '{}' to VERIFY.md, ending {}.".format(title, ENDING) if title
             else "Write VERIFY.md, ending {}.".format(ENDING))
    scope = "Diff: {}..{}.".format(f.base, f.head) if f.diff else "Full: the whole diff {}..{}.".format(f.base, f.head)
    return [
        "# {} verifier (verifier role, {} tier, round {}, check: {})".format(f.label, f.tier, f.round, "diff" if f.diff else "full"), "",
        "Fresh context; you are not an author of {0}. Task {0}: {1}. {2}".format(f.label, f.title, read_line(f, READS["verify"])),
        "{} {}".format(where_line(f), scope),
        "You report; you never fix.",
        "1. pa-full-suite once at HEAD (never under a timeout), output to full-suite-verify{}.out.{}".format(sfx, flakes_sentence(f)),
        "2. {} -> verify{}-failing-first.out.".format(failing_first(f), sfx),
        "3. " + mutations_line(f, "verify{}-mut.log".format(sfx)),
        with_items("4. Each verification point in plan.md: met or not, with the proof.", f.items),
        BAR_BUILD, write,
        "Evidence folder: {}.".format(f.ev), rules_line(f)]


def text_review(f):
    title = section(f, "review")
    write = ("Append '{}' to REVIEW.md with {}, ending {}.".format(title, REVIEWER, ENDING) if title
             else "Write REVIEW.md with {}, ending {}.".format(REVIEWER, ENDING))
    scope = ("Review the diff {}..{} against the plan.".format(f.base, f.head) if f.diff
             else "Review the whole diff {}..{} against the plan.".format(f.base, f.head))
    f.tests = []  # a reviewer runs no failing-first: the header names the base only
    return [
        "# {} reviewer (reviewer role, {} tier, round {}, check: {})".format(f.label, f.tier, f.round, "diff" if f.diff else "full"), "",
        "Fresh context; you are not an author of {0}. Task {0}: {1}. {2}".format(f.label, f.title, read_line(f, READS["review"])),
        with_items("{} {}".format(where_line(f), scope), f.items),
        "Run no suite (the verifier runs it); single tests only if you need them.",
        BAR_BUILD, write,
        "Evidence folder: {}.".format(f.ev), rules_line(f, reviewer=True)]


def text_queue(f, repo):
    lines = [queue_brief(f.directory, repo or main_checkout(f.wt), f.task, None if f.job == MAIN else f.job), ""]
    if f.items:
        lines += [f.items, ""]
    lines.append((flakes_sentence(f).strip() + " " if f.flakes else "") +
                 "The Reviewer: line must be your own terminal address exactly as `echo $HC_TERMINAL_ID` prints it, "
                 "nothing else. Rules: edit and commit nothing; no push, merge or install; open no file under ~/.claude, "
                 "~/.codex or ~/.config/herdr-app; start no Claude session; git commands as plain separate commands. "
                 "Leave no background process.")
    return lines


def file_name(f):
    base = "check" if f.kind == "recheck" else f.kind
    job = "-" + f.job if f.job != MAIN else ""
    rr = "" if f.kind == "queue" or f.round <= (2 if f.kind == "fix" else 1) else "-r{}".format(f.round)
    return "handoff-{}{}{}.md".format(base, job, rr)


# ---- command ------------------------------------------------------------------------------------------------------

def cmd_write(args):
    directory, agent = pa.space_for_command(args.space)
    f = gather(args, directory, agent)
    f.public, f.logic, f.no_mutations = args.public, args.logic, args.no_mutations
    builders = {"check": text_check, "recheck": text_recheck, "fix": text_fix, "verify": text_verify,
                "review": text_review}
    lines = text_queue(f, args.repo) if f.kind == "queue" else builders[f.kind](f)
    path = os.path.join(f.ev, file_name(f))
    if os.path.exists(path) and not args.force:
        raise Refused("{} is there; pass --force to write it again".format(path))
    os.makedirs(f.ev, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as out:
        out.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    for note in f.notes:
        print("pa-handoff: note: {}".format(note), file=sys.stderr)
    print(path)
    return 0


def parser():
    p = argparse.ArgumentParser(prog="pa-handoff", description="Write a worker's handoff (t-169).")
    p.add_argument("command", choices=KINDS)
    p.add_argument("task")
    p.add_argument("--job", help="the job (its worktrees entry); default main")
    p.add_argument("--base", help="the base commit (a fix: the checked commit)")
    p.add_argument("--round", type=int, help="default: the task's highest build round (a fix: one more)")
    p.add_argument("--diff", action="store_true", help="verify or review: a diff check from the checked commit")
    p.add_argument("--logic", action="store_true", help="a diff check whose fix changed logic (r-few-mutations)")
    p.add_argument("--no-mutations", action="store_true", help="a docs-only or test-only change")
    p.add_argument("--public", action="store_true", help="the repository is public (the Rules line says so)")
    items = p.add_mutually_exclusive_group()
    items.add_argument("--items", help="the Project Agent's judgement lines")
    items.add_argument("--items-file", help="a file with them")
    p.add_argument("--repo", help="queue: the repository's main checkout (default: the worktree's)")
    p.add_argument("--space")
    p.add_argument("--force", action="store_true", help="write over an existing handoff")
    return p


def main(argv=None):
    args = parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        pa.ident(args.task, "task id")
        if args.job:
            pa.ident(args.job, "job")
        return cmd_write(args)
    except Refused as r:
        print("pa-handoff: refused: {}".format(r), file=sys.stderr)
        return 1
    except pa.Problem as e:
        print("pa-handoff: {}".format(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
