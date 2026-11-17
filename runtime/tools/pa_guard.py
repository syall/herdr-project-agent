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
    """The simple commands of a plain line, as lists of words (quotes removed); None when the line is not plain."""
    scanned = split_plain_ops(line)
    return None if scanned is None else scanned[0]


def split_plain_ops(line):
    """(commands, operators) of a plain line: operators[i] is the ; && || | after commands[i] (None after the last).
    None when the line is not plain.

    Plain: simple commands joined only by ; && || |, each a program word and literal words, quoted or not.
    Not plain: $ and backticks (outside single quotes), < >, a lone &, |&, ( ) { } (except the @{...} of
    stash@{0} and HEAD@{1}), unquoted glob characters * ? [, a # or = starting a word, an unquoted line
    break or line continuation, an unterminated quote, an empty command between operators.
    """
    s = line.strip()
    commands, ops, words, word = [], [], [], []
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
            ops.append(op)
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
        ops.append(None)
    elif pending_op not in (None, ";"):
        return None
    return commands, ops


# ---- R2: involves git, gh or hc ------------------------------------------------------------------

def program(word):
    """The last part of a path, in lower case: the Mac's file system finds /usr/bin/git for GIT or Git too."""
    return word.rstrip("/").rsplit("/", 1)[-1].lower()


def involves(words):
    """A word is git, gh or hc, or a path whose last part is one of them, in any case."""
    return any(program(w) in NAMES for w in words)


WHOLE_WORD = re.compile(r"(?<![\w.-])(git|gh|hc)(?![\w.-])", re.IGNORECASE)
# A shell started with text to run, or eval: its quoted text is a command line of its own.
WRAPPERS = ("sh", "bash", "zsh", "dash", "ksh", "eval")


def involves_text(line):
    """git, gh or hc appears as a whole word, in any case, once quotes and backslashes are taken out.
    Used for a line that is not plain, and for each word of a plain line that starts a shell or eval."""
    return bool(WHOLE_WORD.search(re.sub(r"[\"'\\]", "", line)))


def wraps(commands):
    """A plain line runs a shell or eval somewhere (sh -c '...', echo '...' | sh, env bash -c, eval '...')."""
    return any(program(w) in WRAPPERS for words in commands for w in words)


# ---- R6: the table -------------------------------------------------------------------------------
# Each shape gets the words after the row's command and a context (pa_address, own_address, cwd) and says yes or no.

def is_word(a):
    """A <path> or <branch>: a word not starting with -."""
    return bool(a) and not a.startswith("-")


def is_ref(a):
    """A <branch> to fetch or pull: a word, and not a refspec that writes a local ref (a:b, +a)."""
    return is_word(a) and ":" not in a and not a.startswith("+")


def no_pathspec(a):
    """A word, with no wildcard and no pathspec magic: git cannot read it as a set of files."""
    return is_word(a) and not a.startswith(":") and not re.search(r"[*?\[]", a)


def explicit_path(a):
    """A path named explicitly: not . or .., no wildcard, no pathspec magic."""
    return no_pathspec(a) and os.path.normpath(a) not in (".", "..")


def file_path(a, cwd):
    """A path named explicitly that is not a folder: no ~, not only . and .. parts, and not an existing folder in cwd.
    With the folder unknown (None), nothing counts."""
    if not explicit_path(a) or a.startswith("~") or cwd is None:
        return False
    if all(part in ("", ".", "..") for part in os.path.normpath(a).split(os.sep)):
        return False
    return not os.path.isdir(os.path.join(cwd, a))


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
    # --remote with --exec runs a program of the caller's choosing; neither is reading. Abbreviations count (--rem, --e).
    return (no_output(args) and not any(short_cluster(a, "o") for a in args)
            and not any(prefix_of(a, "--remote", 3) or prefix_of(a, "--exec", 3) for a in args))


def grep_args(args, ctx):
    # -O / --open-files-in-pager starts a program of the caller's choosing: not reading.
    return no_output(args) and not any(short_cluster(a, "O") or prefix_of(a, "--open-files-in-pager", 4) for a in args)


BRANCH_LIST = {"--list", "-a", "-r", "-v", "-vv", "--show-current", "--contains", "--merged", "--no-merged"}


def branch_args(args, ctx):
    """Nothing; or listing options with other words; or <new-branch> [<start>].

    With a listing option the other words are patterns (--list) or commits (--contains); with -v / -vv git reads a
    word as a new branch name, which is allowed anyway."""
    options = [a for a in args if a.startswith("-")]
    for a in options:
        if opt_name(a) not in BRANCH_LIST or ("=" in a and opt_name(a) not in ("--contains", "--merged", "--no-merged")):
            return False
    return bool(options) or len(args) <= 2


def new_branch(args, word=is_word):
    """-c|-b <new-branch> [<start>] after the flag."""
    return 1 <= len(args) <= 2 and all(word(a) for a in args)


def switch_args(args, ctx):
    return (len(args) == 1 and is_word(args[0])) or (args[:1] == ["-c"] and new_branch(args[1:]))


def checkout_args(args, ctx):
    """<branch>, or -b <new-branch> [<start>]: branch names only. A word with * ? [ or a leading : is a pathspec, and
    a word that names an existing path is a file: both would restore files (losing uncommitted work), so refused."""
    if args[:1] == ["-b"]:
        return new_branch(args[1:], no_pathspec)
    if len(args) != 1 or not no_pathspec(args[0]):
        return False
    cwd = ctx.get("cwd")
    return cwd is not None and not os.path.lexists(os.path.join(cwd, os.path.expanduser(args[0])))


def add_args(args, ctx):
    if args in (["-A"], ["-u"]):
        return True
    rest = args[1:] if args[:1] in (["-A"], ["-u"], ["--"]) else args
    return bool(rest) and all(is_word(a) for a in rest)


def restore_args(args, ctx):
    """[--staged] [--] <path>...: each a file named explicitly, never a folder (a folder restores all under it)."""
    rest = args[1:] if args[:1] == ["--staged"] else args
    rest = rest[1:] if rest[:1] == ["--"] else rest
    return bool(rest) and all(file_path(a, ctx.get("cwd")) for a in rest)


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


CONFIG_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*\.(.*\.)?[A-Za-z][A-Za-z0-9-]*$")  # section[.subsection].name


def config_args(args, ctx):
    """Reading only: --list / -l / list; --get / --get-all / get <key>; or <key> alone. A key contains a dot, so a
    subcommand word (edit, set, unset, rename-section, remove-section) is never read as one."""
    return (args in (["--list"], ["-l"], ["list"])
            or (len(args) == 2 and args[0] in ("--get", "--get-all", "get") and bool(CONFIG_KEY.match(args[1])))
            or (len(args) == 1 and bool(CONFIG_KEY.match(args[0]))))


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
    """<Project Agent address> <text>, the text not an option. With no known Project Agent (no task record), one address."""
    pa_address = ctx.get("pa_address")
    return len(args) == 2 and is_word(args[0]) and is_word(args[1]) and (not pa_address or args[0] == pa_address)


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
    # publishing to another server or system, or serving the repository
    ("git", "lfs", "push"), ("git", "svn"), ("git", "p4"), ("git", "cvsexportcommit"), ("git", "send-pack"),
    ("git", "http-push"), ("git", "imap-send"), ("git", "daemon"), ("git", "instaweb"),
    ("gh", "pr", "create"), ("gh", "pr", "merge"), ("gh", "pr", "edit"), ("gh", "pr", "ready"), ("gh", "pr", "comment"),
    ("gh", "pr", "review"), ("gh", "pr", "close"), ("gh", "pr", "reopen"), ("gh", "pr", "update-branch"),
    ("gh", "pr", "revert"), ("gh", "pr", "lock"), ("gh", "pr", "unlock"),
    ("gh", "issue", "create"), ("gh", "issue", "comment"), ("gh", "issue", "edit"), ("gh", "issue", "close"),
    ("gh", "issue", "reopen"), ("gh", "issue", "delete"), ("gh", "issue", "transfer"), ("gh", "issue", "develop"),
    ("gh", "issue", "lock"), ("gh", "issue", "unlock"), ("gh", "issue", "pin"), ("gh", "issue", "unpin"),
    ("gh", "run", "rerun"), ("gh", "run", "cancel"), ("gh", "run", "delete"),
    ("gh", "repo"), ("gh", "release"), ("gh", "api"), ("gh", "auth"), ("gh", "secret"), ("gh", "variable"),
    ("gh", "workflow"), ("gh", "ruleset"), ("gh", "gist"), ("gh", "config"), ("gh", "extension"), ("gh", "alias"),
    ("gh", "label"), ("gh", "cache"), ("gh", "project"), ("gh", "ssh-key"), ("gh", "gpg-key"), ("gh", "codespace"),
    ("gh", "org"), ("gh", "agent-task"),
    ("hc", "send"),
]
ADDED_SHAPES = {"any": any_args, "none": no_args}


def profile_rows(profile, err=None):
    """The profile's "guardAllow" rows ({"command": [...], "args": "any" | "none"}) that may be added; a note for the rest.
    Total: whatever the profile holds, this returns a list and never raises."""
    err = err or sys.stderr
    if not isinstance(profile, dict) or profile.get("guardAllow") is None:
        return []
    name, allow = profile.get("name"), profile.get("guardAllow")
    if not isinstance(allow, list):
        print("pa-hook guard: ignored guardAllow in profile {}: {!r} (needs a list of rows)".format(name, allow), file=err)
        return []
    rows = []
    for row in allow:
        command = row.get("command") if isinstance(row, dict) else None
        args = row.get("args") if isinstance(row, dict) else None
        shape = ADDED_SHAPES.get(args) if isinstance(args, str) else None
        if not (isinstance(command, list) and command and all(isinstance(w, str) and w for w in command)
                and command[0] in NAMES and shape):
            print("pa-hook guard: ignored a guardAllow row in profile {}: {!r} (needs a git, gh or hc command list and "
                  "args \"any\" or \"none\")".format(name, row), file=err)
            continue
        command = tuple(command)
        if any(w.startswith("-") for w in command[1:]):
            # git -c / -C / --no-pager, gh --repo: an option would let the row reach any subcommand, NEVER_ADD included.
            print("pa-hook guard: ignored a guardAllow row in profile {}: {} names an option; rows name subcommands "
                  "only".format(name, " ".join(command)), file=err)
            continue
        if any(command[:len(never)] == never or never[:len(command)] == command for never in NEVER_ADD):
            print("pa-hook guard: ignored a guardAllow row in profile {}: {} can never be added".format(
                name, " ".join(command)), file=err)
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


def cd_target(words, cwd):
    """Where `cd` goes (None: not known)."""
    if len(words) == 1:
        return os.path.expanduser("~")
    if len(words) != 2 or words[1] == "-" or cwd is None:
        return None
    return os.path.normpath(os.path.join(cwd, os.path.expanduser(words[1])))


def folders(commands, ops, start):
    """The folder each simple command runs in (None: not known).

    A `cd` moves the next commands only when it surely ran and they run only if it did: it comes first, or after ; or
    &&, and is followed by &&. From there the folder is known while commands are joined by &&; after any other
    operator a failed cd could have left the shell where it was. pushd and popd make the folder unknown."""
    here, moved, prev, result = start, False, None, []
    for words, op in zip(commands, ops):
        result.append(here)
        if words[0] == "cd" and prev in (None, ";", "&&") and op == "&&":
            here, moved = cd_target(words, here), True
        elif words[0] in ("cd", "pushd", "popd"):
            here = None
        elif moved and op not in ("&&", None):
            here = None
        prev = op
    return result


def check_command(line, pa_address=None, own_address=None, extra_rows=(), cwd=None):
    """None when the Bash line may run; else the refusal message (spec section 6).

    extra_rows: the profile's added rows, or a function that returns them; it is called only when a
    command involves git, gh or hc and no row of ALLOWLIST allows it (so its notes come only then).
    """
    scanned = split_plain_ops(line or "")
    if scanned is None:
        return NOT_PLAIN if involves_text(line) else None
    commands, ops = scanned
    extra = None
    wrapped = wraps(commands)
    places = folders(commands, ops, cwd if cwd is not None else os.getcwd())
    for words, here in zip(commands, places):
        # With a shell or eval in the line, git, gh or hc named inside any word's text counts too (it may be run).
        if involves(words) or (wrapped and any(involves_text(w) for w in words)):
            ctx = {"pa_address": pa_address, "own_address": own_address, "cwd": here}
            if not allowed(words, ALLOWLIST, ctx):
                if extra is None:
                    extra = list((extra_rows() if callable(extra_rows) else extra_rows) or [])
                if not allowed(words, extra, ctx):
                    return OFF_TABLE.format(shown(" ".join(shlex.quote(w) for w in words)))
    return None


# ---- R8: the editor rule ---------------------------------------------------------------------------

STATE_FILES = ("ledger.json", "journal.jsonl", "rules.json", "agent.json")


def check_edit(path, spaces_dir, cwd=None, home=None):
    """None when Edit/Write/MultiEdit/NotebookEdit may write path; else the refusal message.

    Refused, both for the path as written and after resolving links: anything inside a .git folder or a worktree's
    .git file; anything in <spaces_dir> except a space's evidence folder (<spaces_dir>/<space>/evidence/...), so
    decisions/, ledger.json, journal.jsonl, rules.json, agent.json (STATE_FILES) and the rest of the state; and,
    when home (the Project Agent's home) is given, anything else under it (profiles, core texts, tools).
    Names are compared without case (the Mac's file system ignores it).
    """
    if not path:
        return None
    given = os.path.normpath(os.path.join(cwd or os.getcwd(), os.path.expanduser(path)))
    refused = OFF_TABLE.format(shown(path))

    def forms(folder):
        return {os.path.normpath(os.path.abspath(folder)).lower(), os.path.realpath(folder).lower()}

    spaces, homes = forms(spaces_dir), forms(home) if home else set()
    for low in {given.lower(), os.path.realpath(given).lower()}:
        if ".git" in low.split(os.sep):
            return refused
        for top in spaces:
            if low == top or low.startswith(top + os.sep):
                parts = low[len(top) + 1:].split(os.sep)
                if len(parts) < 3 or parts[1] != "evidence":
                    return refused
                break
        else:
            if any(low == top or low.startswith(top + os.sep) for top in homes):
                return refused
    return None
