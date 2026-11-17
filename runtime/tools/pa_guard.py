"""The worker command allowlist, used by `pa-hook guard` (pa_hooks.hook_guard). Python standard library only (3.7+).

A worker may run the ordinary build, file and text tools freely, and git, gh and hc only as the rows of
ALLOWLIST below allow. Anything else that involves git, gh or hc is refused, so that publishing and
repository settings stay with the person and the Project Agent. Edits inside a .git folder and to the
Project Agent's decision files are refused too. Everything here runs in-process: no subprocess, no files
read except to resolve links and to see whether a `git checkout` word names an existing path.

  check_command(line, pa_address, own_address, extra_rows, cwd)  -> None (allowed) or the refusal message
  check_edit(path, spaces_dir, cwd)                               -> None (allowed) or the refusal message
  profile_rows(profile)                                           -> the profile's "guardAllow" rows, checked

This is a fence for agents that follow their rules, not a sandbox: a program the worker writes itself can
still start git. The rules it backs are in worker.md.
"""
import os
import re
import shlex
import sys

NAMES = ("git", "gh", "hc")
OFF_TABLE = ("Refused by the Project Agent's allowlist: {}. Workers run only the git, gh and hc commands on the "
             "allowlist. Stop and report this to the Project Agent with hc send; it handles publishing and "
             "repository settings. There is no override.")
NOT_PLAIN = ("Refused by the Project Agent's allowlist: this line uses git, gh or hc beyond plain commands joined by "
             "; && || |. Write the command plainly and run it again. If it cannot be written plainly, stop and "
             "report to the Project Agent with hc send. There is no override.")
SHOWN = 300  # longest command or path quoted back in a message


# ---- R1: the plain form --------------------------------------------------------------------------

def split_plain(line):
    """The simple commands of a plain line, as lists of words (quotes removed); None when the line is not plain.

    Plain: simple commands joined only by ; && || |, each a program word and literal words, quoted or not.
    Not plain: $ and backticks (outside single quotes), < >, a lone &, |&, ( ) { } (except the @{...} of
    stash@{0} and HEAD@{1}), unquoted glob characters * ? [, a # or = starting a word, an unquoted line
    break or line continuation, an unterminated quote, an empty command between operators.
    """
    s = line.strip()
    commands, words, word = [], [], []
    has_word, pending_op = False, None
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "'":
            j = s.find("'", i + 1)
            if j < 0:
                return None
            word.append(s[i + 1:j])
            has_word, i = True, j + 1
        elif c == '"':
            i += 1
            while True:
                if i >= n:
                    return None
                c = s[i]
                if c == '"':
                    i += 1
                    break
                if c in "$`":
                    return None
                if c == "\\" and i + 1 < n and s[i + 1] in '$`"\\\n':
                    if s[i + 1] != "\n":
                        word.append(s[i + 1])
                    i += 2
                    continue
                word.append(c)
                i += 1
            has_word = True
        elif c == "\\":
            if i + 1 >= n or s[i + 1] in "\n\r":
                return None
            word.append(s[i + 1])
            has_word, i = True, i + 2
        elif c in " \t":
            if has_word:
                words.append("".join(word))
            word, has_word = [], False
            i += 1
        elif c in ";&|":
            if has_word:
                words.append("".join(word))
            word, has_word = [], False
            if c == ";":
                op, i = ";", i + 1
                if i < n and s[i] in ";&":
                    return None
            elif s.startswith("&&", i):
                op, i = "&&", i + 2
            elif c == "&" or s.startswith("|&", i):
                return None
            elif s.startswith("||", i):
                op, i = "||", i + 2
            else:
                op, i = "|", i + 1
            if not words:
                return None
            commands.append(words)
            words, pending_op = [], op
        elif c == "{" and word and word[-1].endswith("@"):
            m = re.match(r"\{[^{}\s,'\"$`\\;&|<>()*?\[]*\}", s[i:])
            if not m or ".." in m.group(0):
                return None
            word.append(m.group(0))
            has_word, i = True, i + len(m.group(0))
        elif c in "$`<>(){}*?[\n\r":
            return None
        elif c in "#=" and not has_word:
            return None
        else:
            word.append(c)
            has_word, i = True, i + 1
    if has_word:
        words.append("".join(word))
    if words:
        commands.append(words)
    elif pending_op not in (None, ";"):
        return None
    return commands


# ---- R2: involves git, gh or hc ------------------------------------------------------------------

def involves(words):
    """A word is git, gh or hc, or a path whose last part is one of them."""
    return any(w in NAMES or w.rstrip("/").rsplit("/", 1)[-1] in NAMES for w in words)


WHOLE_WORD = re.compile(r"(?<![\w.-])(git|gh|hc)(?![\w.-])")


def involves_text(line):
    """For a line that is not plain: git, gh or hc appears as a whole word, once quotes and backslashes are taken out."""
    return bool(WHOLE_WORD.search(re.sub(r"[\"'\\]", "", line)))


# ---- R6: the table -------------------------------------------------------------------------------
# Each shape gets the words after the row's command and a context (pa_address, own_address, cwd) and says yes or no.

def is_word(a):
    """A <path> or <branch>: a word not starting with -."""
    return bool(a) and not a.startswith("-")


def is_ref(a):
    """A <branch> to fetch or pull: a word, and not a refspec that writes a local ref (a:b, +a)."""
    return is_word(a) and ":" not in a and not a.startswith("+")


def explicit_path(a):
    """A path named explicitly: not . or .., no wildcard, no pathspec magic."""
    return is_word(a) and not a.startswith(":") and not re.search(r"[*?\[]", a) and os.path.normpath(a) not in (".", "..")


def opt_name(a):
    return a.split("=", 1)[0]


def prefix_of(a, option, shortest):
    """git accepts any unique abbreviation of a long option: --outp is --output."""
    name = opt_name(a)
    return len(name) >= shortest and option.startswith(name)


def short_cluster(a, letter):
    """-vo: short options run together, one of them `letter`."""
    return a.startswith("-") and not a.startswith("--") and letter in a[1:]


def any_args(args, ctx):
    return True


def no_args(args, ctx):
    return not args


def no_output(args, ctx=None):
    """Any arguments except --output (or an abbreviation of it): reading, never writing a file."""
    return not any(opt_name(a).startswith("--output") or prefix_of(a, "--output", 3) for a in args)


def archive_args(args, ctx):
    return no_output(args) and not any(short_cluster(a, "o") for a in args)


def grep_args(args, ctx):
    # -O / --open-files-in-pager starts a program of the caller's choosing: not reading.
    return no_output(args) and not any(short_cluster(a, "O") or prefix_of(a, "--open-files-in-pager", 4) for a in args)


BRANCH_LIST = {"--list", "-a", "-r", "-v", "-vv", "--show-current", "--contains", "--merged", "--no-merged"}


def branch_args(args, ctx):
    """Nothing; or listing options and patterns; or <new-branch> [<start>]."""
    options = [a for a in args if a.startswith("-")]
    for a in options:
        if opt_name(a) not in BRANCH_LIST or ("=" in a and opt_name(a) not in ("--contains", "--merged", "--no-merged")):
            return False
    return bool(options) or len(args) <= 2


def new_branch(args):
    """-c|-b <new-branch> [<start>] after the flag."""
    return 1 <= len(args) <= 2 and all(is_word(a) for a in args)


def switch_args(args, ctx):
    return (len(args) == 1 and is_word(args[0])) or (args[:1] == ["-c"] and new_branch(args[1:]))


def checkout_args(args, ctx):
    """<branch>, or -b <new-branch> [<start>]. A word that names an existing path is a file restore: refused."""
    if args[:1] == ["-b"]:
        return new_branch(args[1:])
    if len(args) != 1 or not is_word(args[0]):
        return False
    cwd = ctx.get("cwd")
    return cwd is not None and not os.path.lexists(os.path.join(cwd, os.path.expanduser(args[0])))


def add_args(args, ctx):
    if args in (["-A"], ["-u"]):
        return True
    rest = args[1:] if args[:1] in (["-A"], ["-u"], ["--"]) else args
    return bool(rest) and all(is_word(a) for a in rest)


def restore_args(args, ctx):
    rest = args[1:] if args[:1] == ["--staged"] else args
    rest = rest[1:] if rest[:1] == ["--"] else rest
    return bool(rest) and all(explicit_path(a) for a in rest)


COMMIT_FLAGS = {"--amend", "--no-edit", "--allow-empty"}


def commit_args(args, ctx):
    """Options from -m <msg>, -F <file>, -a, --amend, --no-edit, --allow-empty, -s (short ones may run together: -am)."""
    i = 0
    while i < len(args):
        a = args[i]
        if a in COMMIT_FLAGS:
            i += 1
        elif a != "-" and re.match(r"^-[as]*[mF]?$", a):
            if a[-1] in "mF":
                if i + 1 >= len(args):
                    return False
                i += 2
            else:
                i += 1
        else:
            return False
    return True


def stash_args(args, ctx):
    if not args:
        return True
    sub, rest = args[0], args[1:]
    if sub == "list":
        return not rest
    if sub in ("show", "apply", "pop"):
        return len(rest) <= 1 and all(is_word(a) for a in rest)
    if sub != "push":
        return False
    if rest[:1] == ["-m"]:
        if len(rest) < 2:
            return False
        rest = rest[2:]
    return not rest or (rest[0] == "--" and len(rest) > 1 and all(is_word(a) for a in rest[1:]))


def fetch_args(args, ctx):
    rest = args[1:] if args[:1] == ["--prune"] else args
    return len(rest) <= 2 and all(is_ref(a) for a in rest)


def pull_args(args, ctx):
    rest = args[1:] if args[:1] in (["--rebase"], ["--ff-only"]) else args
    return len(rest) in (0, 2) and all(is_ref(a) for a in rest)


def merge_args(args, ctx):
    if args in (["--abort"], ["--continue"]):
        return True
    ff = message = False
    branches = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--no-ff", "--ff-only") and not ff:
            ff, i = True, i + 1
        elif a == "-m" and not message and i + 1 < len(args):
            message, i = True, i + 2
        elif is_word(a):
            branches.append(a)
            i += 1
        else:
            return False
    return len(branches) == 1


def rebase_args(args, ctx):
    return (len(args) == 1 and is_word(args[0])) or args in (["--continue"], ["--abort"], ["--skip"])


def worktree_list_args(args, ctx):
    return args in ([], ["--porcelain"])


def config_args(args, ctx):
    return (args in (["--list"], ["-l"]) or (len(args) == 2 and args[0] in ("--get", "--get-all") and is_word(args[1]))
            or (len(args) == 1 and is_word(args[0])))


def reflog_args(args, ctx):
    """Show and list only: `git reflog`, `git reflog show ...`, `git reflog -n 5 ...`, `git reflog list`."""
    if not args:
        return True
    if args[0] == "list":
        return len(args) == 1
    return (args[0] == "show" or args[0].startswith("-")) and no_output(args)


TAG_LIST = {"-l", "--list", "--contains", "--merged"}


def tag_args(args, ctx):
    """Listing only: nothing; or -l / --list with patterns; or --contains / --merged."""
    if not args:
        return True
    options = [a for a in args if a.startswith("-")]
    for a in options:
        if opt_name(a) not in TAG_LIST or ("=" in a and opt_name(a) not in ("--contains", "--merged")):
            return False
    return bool(options)


def one_of(*subcommands):
    def shape(args, ctx):
        return bool(args) and args[0] in subcommands
    shape.__name__ = "one_of_" + "_".join(subcommands)
    return shape


def hc_send_args(args, ctx):
    """<Project Agent address> <text>. With no known Project Agent (no task record), one address."""
    pa_address = ctx.get("pa_address")
    return len(args) == 2 and is_word(args[0]) and (not pa_address or args[0] == pa_address)


def hc_read_args(args, ctx):
    own = ctx.get("own_address")
    return bool(own) and (args == [own] or (len(args) == 3 and args[:2] == [own, "--lines"] and args[2].isdigit()))


def hc_get_args(args, ctx):
    own = ctx.get("own_address")
    return bool(own) and args == [own]


# The table (spec section 3, and the read-only rows the person added). `git` may be followed by --no-pager.
ALLOWLIST = [
    (("git", "status"), any_args),
    (("git", "diff"), no_output),
    (("git", "log"), no_output),
    (("git", "show"), no_output),
    (("git", "blame"), any_args),
    (("git", "ls-files"), any_args),
    (("git", "rev-parse"), any_args),
    (("git", "branch"), branch_args),
    (("git", "switch"), switch_args),
    (("git", "checkout"), checkout_args),
    (("git", "add"), add_args),
    (("git", "restore"), restore_args),
    (("git", "commit"), commit_args),
    (("git", "stash"), stash_args),
    (("git", "fetch"), fetch_args),
    (("git", "pull"), pull_args),
    (("git", "merge"), merge_args),
    (("git", "rebase"), rebase_args),
    (("git", "worktree", "list"), worktree_list_args),
    (("git", "config"), config_args),
    (("gh", "pr"), one_of("view", "list", "diff", "checks", "status")),
    (("gh", "issue"), one_of("view", "list", "status")),
    (("gh", "run"), one_of("view", "list")),
    (("gh", "release"), one_of("view", "list")),
    (("gh", "repo", "view"), any_args),
    (("hc", "send"), hc_send_args),
    (("hc", "read"), hc_read_args),
    (("hc", "get"), hc_get_args),
    # read-only, any arguments except an --output option
    (("git", "archive"), archive_args),
    (("git", "merge-base"), no_output),
    (("git", "grep"), grep_args),
    (("git", "cat-file"), no_output),
    (("git", "ls-tree"), no_output),
    (("git", "for-each-ref"), no_output),
    (("git", "describe"), no_output),
    (("git", "reflog"), reflog_args),
    (("git", "diff-tree"), no_output),
    (("git", "cherry"), no_output),
    (("git", "tag"), tag_args),
]
# tests/run-all.sh, pa-gate, pa-full-suite and the ordinary tools need no row: they involve no git, gh or hc.

# R10: a profile's "guardAllow" rows can never open these (publishing and configuration), nor anything that contains them.
NEVER_ADD = [
    ("git", "push"), ("git", "remote"), ("git", "config"), ("git", "credential"), ("git", "send-email"),
    ("git", "request-pull"), ("git", "submodule"), ("git", "hook"),
    ("gh", "pr", "create"), ("gh", "pr", "merge"), ("gh", "pr", "edit"), ("gh", "pr", "ready"), ("gh", "pr", "comment"),
    ("gh", "pr", "review"), ("gh", "pr", "close"), ("gh", "pr", "reopen"),
    ("gh", "issue", "create"), ("gh", "issue", "comment"), ("gh", "issue", "edit"), ("gh", "issue", "close"),
    ("gh", "repo"), ("gh", "release"), ("gh", "api"), ("gh", "auth"), ("gh", "secret"), ("gh", "variable"),
    ("gh", "workflow"), ("gh", "ruleset"), ("gh", "gist"), ("gh", "config"), ("gh", "extension"), ("gh", "alias"),
    ("hc", "send"),
]
ADDED_SHAPES = {"any": any_args, "none": no_args}


def profile_rows(profile, err=None):
    """The profile's "guardAllow" rows ({"command": [...], "args": "any" | "none"}) that may be added; a note for the rest."""
    err = err or sys.stderr
    rows = []
    for row in (profile or {}).get("guardAllow") or []:
        command = row.get("command") if isinstance(row, dict) else None
        shape = ADDED_SHAPES.get(row.get("args")) if isinstance(row, dict) else None
        if not (isinstance(command, list) and command and all(isinstance(w, str) and w for w in command)
                and command[0] in NAMES and shape):
            print("pa-hook guard: ignored a guardAllow row in profile {}: {!r} (needs a git, gh or hc command list and "
                  "args \"any\" or \"none\")".format((profile or {}).get("name"), row), file=err)
            continue
        command = tuple(command)
        if any(command[:len(never)] == never or never[:len(command)] == command for never in NEVER_ADD):
            print("pa-hook guard: ignored a guardAllow row in profile {}: {} can never be added".format(
                (profile or {}).get("name"), " ".join(command)), file=err)
            continue
        rows.append((command, shape))
    return rows


# ---- R3, R4: the check ----------------------------------------------------------------------------

def shown(text):
    return text if len(text) <= SHOWN else text[:SHOWN - 3] + "..."


def allowed(words, rows, ctx):
    """A simple command that involves git, gh or hc: allowed only as a row of the table."""
    if words[0] not in NAMES:
        return False  # a wrapper (env, xargs, sudo), a path to the program, or a leading VAR=value
    rest = list(words[1:])
    if words[0] == "git" and rest[:1] == ["--no-pager"]:
        rest = rest[1:]
    for command, shape in rows:
        size = len(command) - 1
        if command[0] == words[0] and tuple(rest[:size]) == command[1:] and len(rest) >= size and shape(rest[size:], ctx):
            return True
    return False


def next_cwd(words, cwd):
    """Where a `cd` in the line leaves the next command (None: not known)."""
    if words[0] != "cd":
        return cwd
    if len(words) == 1:
        return os.path.expanduser("~")
    if len(words) != 2 or words[1] == "-" or cwd is None:
        return None
    return os.path.normpath(os.path.join(cwd, os.path.expanduser(words[1])))


def check_command(line, pa_address=None, own_address=None, extra_rows=(), cwd=None):
    """None when the Bash line may run; else the refusal message (spec section 6).

    extra_rows: the profile's added rows, or a function that returns them; it is called only when a
    command involves git, gh or hc and no row of ALLOWLIST allows it (so its notes come only then).
    """
    commands = split_plain(line or "")
    if commands is None:
        return NOT_PLAIN if involves_text(line) else None
    extra = None
    here = cwd if cwd is not None else os.getcwd()
    for words in commands:
        if involves(words):
            ctx = {"pa_address": pa_address, "own_address": own_address, "cwd": here}
            if not allowed(words, ALLOWLIST, ctx):
                if extra is None:
                    extra = list((extra_rows() if callable(extra_rows) else extra_rows) or [])
                if not allowed(words, extra, ctx):
                    return OFF_TABLE.format(shown(" ".join(shlex.quote(w) for w in words)))
        here = next_cwd(words, here)
    return None


# ---- R8: the editor rule ---------------------------------------------------------------------------

STATE_FILES = ("ledger.json", "journal.jsonl", "rules.json", "agent.json")


def check_edit(path, spaces_dir, cwd=None):
    """None when Edit/Write/MultiEdit/NotebookEdit may write path; else the refusal message.

    Refused, after resolving links: anything inside a .git folder or a worktree's .git file, and in any
    space's state folder (<spaces_dir>/<space>/) decisions/, ledger.json, journal.jsonl, rules.json and
    agent.json. Names are compared without case (the Mac's file system ignores it).
    """
    if not path:
        return None
    full = os.path.realpath(os.path.join(cwd or os.getcwd(), os.path.expanduser(path)))
    refused = OFF_TABLE.format(shown(path))
    if ".git" in full.lower().split(os.sep):
        return refused
    spaces = os.path.realpath(spaces_dir).lower() + os.sep
    low = full.lower()
    if low.startswith(spaces):
        parts = low[len(spaces):].split(os.sep)
        if len(parts) >= 2 and (parts[1] == "decisions" or (len(parts) == 2 and parts[1] in STATE_FILES)):
            return refused
    return None
