"""The worker command allowlist, used by `pa-hook guard` (pa_hooks.hook_guard). Python standard library only (3.7+).

A worker may run the ordinary build, file and text tools freely, and git, gh and hc only as the rows of
ALLOWLIST below allow. Anything else that involves git, gh or hc is refused, so that publishing and
repository settings stay with the person and the Project Agent. The person's own tools (PERSON_ONLY:
pa-push-main, pa-push-allow and pa_push.py, t-007 part 7b) are refused for a worker in every form, and no row
can allow them: a main branch is pushed only when the person answers a push card in the panel. Edits inside a .git folder, to the
Project Agent's files and to the person's own git and gh settings are refused too. Everything here runs
in-process: no subprocess, no files read except to resolve links and to see whether a `git checkout` /
`git restore` word names an existing path.

  check_command(line, pa_address, own_address, extra_rows, cwd) -> None (allowed) or the refusal
  check_edit(path, spaces_dir, cwd, home)                       -> None (allowed) or the refusal
  check_writes(line, spaces_dir, cwd, home)                     -> None (allowed) or the refusal
  profile_rows(profile)                                         -> the profile's "guardAllow" rows, checked

This is a fence for agents that follow their rules, not a sandbox: a program the worker writes itself can
still start git. A Bash command that plainly writes (a redirect, or a file command of file_targets' table)
into the paths check_edit protects is refused, evidence folder excepted (t-016); reads, lines that are not
plain, and programs that write files themselves are not judged. The rules it backs are in worker.md.
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
# R12 (fix round 2): a variable that changes what git or gh do.
VARIABLE = ("Refused by the Project Agent's allowlist: {} sets a git or gh variable (GIT_*, GH_*), which changes what the "
            "allowed git and gh commands do. Run the command without it. If it is needed, stop and report this to the "
            "Project Agent with hc send. There is no override.")
# t-007 part 7b: the person's own commands. A word whose last path part is one of these (any case) involves them.
PERSON_ONLY = ("pa-push-main", "pa-push-allow", "pa_push.py")
PERSON_MSG = ("Refused by the Project Agent's allowlist: {}. pa-push-main and pa-push-allow are the person's commands: a "
              "main branch is pushed only when the person answers a push card in the panel. Say in your report that the "
              "work is ready to release. There is no override.")
SHOWN = 300  # longest command or path quoted back in a message
WRITE = ("Refused by the Project Agent's guard: this command writes to {target}, which is inside a .git folder or the Project "
         "Agent's files. Change git state with the allowed git commands only; write reports, logs and notes in your task's "
         "evidence folder (evidence/<task-id>/ in the Project Agent's state folder). If the write is needed, stop and report "
         "to the Project Agent with hc send. There is no override.")  # t-016, check_writes
FOLDER_UNKNOWN = ("Refused by the Project Agent's guard: after {word} the folder is not known, so the write to {target} cannot "
                  "be checked. Write the target as a full path, or run the cd as a command of its own first. There is no "
                  "override.")
HOME_VAR = re.compile(r"\$(?:HOME(?![A-Za-z0-9_])|\{HOME\})")
REDIRECT = re.compile(r"(?:>&(?:\d+|-)|<&\d+)(?=[ \t;&|]|$)|&>>|&>|>>|>|<")  # a duplication first: it has no target


# ---- R1: the plain form --------------------------------------------------------------------------

def split_plain(line):
    """The simple commands of a plain line, as lists of words (quotes removed); None when the line is not plain."""
    scanned = split_plain_ops(line)
    return None if scanned is None else scanned[0]


def split_plain_ops(line, writes=False):
    """(commands, operators) of a plain line: operators[i] is the ; && || | after commands[i] (None after the last).
    None when the line is not plain.

    Plain: simple commands joined only by ; && || |, each a program word and literal words, quoted or not.
    Not plain: $ and backticks (outside single quotes), < >, a lone &, |&, ( ) { } (except the @{...} of
    stash@{0} and HEAD@{1}), unquoted glob characters * ? [, a # or = starting a word, an unquoted line
    break or line continuation, an unterminated quote, an empty command between operators.
    writes=True (t-016): also plain are > >> < &> &>> (an fd's digits before them dropped), each followed by a word, and
    >&N N>&M >&- <&N; $HOME or ${HOME} starting a word (bare or in double quotes) is the home. Returns (commands,
    operators, targets): targets[i] the words after commands[i]'s output redirects (an input's word is a read).
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
                if writes and c == "$" and not word and HOME_VAR.match(s, i):
                    word.append(os.path.expanduser("~"))
                    i = HOME_VAR.match(s, i).end()
                    continue
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
        elif writes and (c in "<>" or s.startswith("&>", i)):
            if has_word and not (c != "&" and "".join(word).isdigit() and s[i - 1].isdigit()):
                words.append("".join(word))  # else the word is the fd: dropped
            word, has_word = [], False
            m = REDIRECT.match(s, i)
            i = m.end()
            if m.group(0)[-1] not in "-0123456789":  # a duplication has no word
                if i < n and s[i] in "<>|&":
                    return None  # << <<< <> >| >&word
                words.append(("<",) if m.group(0) == "<" else (">",))  # a mark: the next word is its target
        elif writes and c == "$" and not word and HOME_VAR.match(s, i):
            word.append(os.path.expanduser("~"))
            has_word, i = True, HOME_VAR.match(s, i).end()
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
    if not writes:
        return commands, ops
    targets = []
    for k, words in enumerate(commands):  # each mark takes the word after it out of the command's words
        marks = [j for j, w in enumerate(words) if isinstance(w, tuple)]
        if any(j + 1 == len(words) or j + 1 in marks for j in marks) or len(words) == 2 * len(marks):
            return None  # a redirect without its word, or a command of redirects only
        targets.append([words[j + 1] for j in marks if words[j] == (">",)])
        commands[k] = [w for j, w in enumerate(words) if j not in marks and j - 1 not in marks]
    return commands, ops, targets


# ---- R2: involves git, gh or hc ------------------------------------------------------------------

def program(word):
    """The last part of a path, in lower case: the Mac's file system finds /usr/bin/git for GIT or Git too."""
    return word.rstrip("/").rsplit("/", 1)[-1].lower()


DASHED = re.compile(r"^git-[a-z0-9][a-z0-9-]*$")  # git-push, git-lfs: git's own programs, run without git


def person_only(word):
    """The word names one of the person's own tools (PERSON_ONLY): its last path part, in any case."""
    return program(word) in PERSON_ONLY


def is_name(word):
    p = program(word)
    return p in NAMES or bool(DASHED.match(p)) or p in PERSON_ONLY


def involves(words):
    """A word is git, gh or hc, or a path whose last part is one of them or a dashed git program, in any case.
    A NAME=value word whose value names git, gh or hc counts too (PAGER='git push')."""
    return any(is_name(w) or (ASSIGN.match(w) and involves_text(w.split("=", 1)[1])) for w in words)


WHOLE_WORD = re.compile(r"(?<![\w.-])(git|gh|hc|git-[a-z0-9-]*[a-z0-9]|pa-push-main|pa-push-allow|pa_push\.py)(?![\w.-])",
                        re.IGNORECASE)
PERSON_WORD = re.compile(r"(?<![\w.-])(pa-push-main|pa-push-allow|pa_push\.py)(?![\w.-])", re.IGNORECASE)
# A shell started with text to run, or eval: its quoted text is a command line of its own. env -S splits its text too.
WRAPPERS = ("sh", "bash", "zsh", "dash", "ksh", "csh", "tcsh", "fish", "watch", "eval")
SPLIT_STRING = re.compile(r"^(-[A-Za-z]*S|--sp)")
ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(\[[^\]]*\])?\+?=")


ANSI_ESCAPE = re.compile(r"\\(x[0-9A-Fa-f]{1,2}|u[0-9A-Fa-f]{1,4}|U[0-9A-Fa-f]{1,8}|[0-7]{1,3}|.)", re.DOTALL)
# An expansion that may be empty: ${...}, $NAME, $1, $@, an empty $( ) or ` `.
MAY_BE_EMPTY = re.compile(r"\$\{[^{}]*\}|\$\(\s*\)|`\s*`|\$[A-Za-z_][A-Za-z0-9_]*|\$[0-9@*#?$!-]")
# REVIEW-7 I2: a default or alternative value the guard can see (${X:-git}, ${X-git}, ${X:=gi}, ${X:+gh}) is that value;
# a command substitution with no substitution inside it ($(:), $(true), `:`) is taken out whatever it prints.
DEFAULT_VALUE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:?[-=+]([^{}]*)\}")
SUBSTITUTION = re.compile(r"\$\([^()]*\)|`[^`]*`")


def ansi_c(m):
    """The text of one $'...' quote, its escapes decoded (\\x67, \\147 and \\u0067 are g)."""
    def one(e):
        code = e.group(1)
        if code[0] in "xuU" and len(code) > 1:
            return chr(int(code[1:], 16) % 0x110000)
        if code[0] in "01234567":
            return chr(int(code, 8))
        return code
    return ANSI_ESCAPE.sub(one, m.group(1))


def involves_text(line):
    """git, gh or hc appears as a whole word, in any case, once quotes and backslashes are taken out: as written, and
    with $'...' decoded and each expansion that may be empty taken out (gi$''t, $'\\x67it', gi${X}t are git), and with a
    visible default value put in and command substitutions taken out (${X:-git}, gi$(:)t, gi`:`t are git).
    Used for a line that is not plain, and for each word of a plain line that starts a shell or eval."""
    return names_in_text(line, WHOLE_WORD)


def person_text(line):
    """One of the person's own tools (PERSON_ONLY) appears in the text, read the way involves_text reads it."""
    return names_in_text(line, PERSON_WORD)


def names_in_text(line, pattern):
    decoded = re.sub(r"\$'((?:[^'\\]|\\.)*)'", ansi_c, line, flags=re.DOTALL).replace('$"', '"')
    valued = DEFAULT_VALUE.sub(lambda m: m.group(1), decoded)
    for _ in range(8):  # innermost first: gi$(echo $(true))t
        valued, found = SUBSTITUTION.subn("", valued)
        if not found:
            break
    texts = (line, decoded, MAY_BE_EMPTY.sub("", decoded), MAY_BE_EMPTY.sub("", valued))
    return any(pattern.search(re.sub(r"[\"'\\]", "", t)) for t in texts)


def wraps(commands):
    """A plain line runs a shell or eval somewhere (sh -c '...', echo '...' | sh, env bash -c, eval '...', env -S '...')."""
    for words in commands:
        for k, w in enumerate(words):
            if program(w) in WRAPPERS or (program(w) == "env" and any(SPLIT_STRING.match(a) for a in words[k + 1:])):
                return True
    return False


def wrapped_text(word):
    """The text a wrapper runs, without an env -S prefix (-S'git push' is the text 'git push')."""
    return re.sub(r"^(-[A-Za-z]*S|--split-string=)", "", word)


# REVIEW-7 M1, REVIEW-8 I-1, I-2, VERIFY-6 V6-1: after a |, a shell, eval, source or . runs what it reads; a known text
# filter does not, whatever its arguments; any other program (a launcher too) does when a later word names a shell or
# is eval, or its first later word not starting with - is source or . (command . x, time source x, xargs -n1 .).
PIPE_READERS = WRAPPERS + ("source", ".")
FILTERS = ("grep", "rg", "head", "tail", "wc", "sort", "uniq", "cut", "tr", "tee", "cat", "less", "jq", "column")


def runs_input(words):
    """A command after a | runs what it reads. Leading NAME=value words are skipped. Launcher options are not parsed."""
    k = 0
    while k < len(words) - 1 and ASSIGN.match(words[k]):
        k += 1
    first = program(words[k])
    if first in FILTERS:
        return False
    named = [w for w in words[k + 1:] if not w.startswith("-")]
    return first in PIPE_READERS or wraps([words[k:]]) or bool(named) and program(named[0]) in ("source", ".")


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
    """A path named explicitly that is not a folder: no ~, not only . and .. parts, and not an existing folder in any
    folder the command may run in (cwd: a tuple). With the folder unknown (None), nothing counts."""
    if not explicit_path(a) or a.startswith("~") or cwd is None:
        return False
    if all(part in ("", ".", "..") for part in os.path.normpath(a).split(os.sep)):
        return False
    return not any(os.path.isdir(os.path.join(c, a)) for c in cwd)


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
    return cwd is not None and not any(os.path.lexists(os.path.join(c, os.path.expanduser(args[0]))) for c in cwd)


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


REFLOG_CHANGES = ("expire", "delete", "drop", "exists")


def reflog_args(args, ctx):
    """Show and list only: `git reflog`, `git reflog show ...`, `git reflog -n 5 ...`, `git reflog list`."""
    if not args:
        return True
    if args[0] == "list":
        return len(args) == 1
    if args[0].startswith("-") and any(a in REFLOG_CHANGES for a in args):
        return False  # git reflog -n 5 expire ...: a subcommand after the options
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
    # fix round 2 (REVIEW-3 M1): publishing a subtree, stored secrets, global config and schedules, lock state on the
    # server, serving a repository, and starting or stopping other agents. Any git credential-<helper> is refused too.
    ("git", "subtree"), ("git", "maintenance"), ("git", "lfs", "install"), ("git", "lfs", "uninstall"),
    ("git", "lfs", "lock"), ("git", "lfs", "unlock"), ("git", "receive-pack"), ("git", "upload-pack"), ("git", "shell"),
    ("git", "cvsserver"),
    ("hc", "run"), ("hc", "kill"), ("hc", "rm"), ("hc", "interrupt"),
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
        low = tuple(w.lower() for w in command)
        if (any(low[:len(never)] == never or never[:len(low)] == low for never in NEVER_ADD)
                or (low[0] == "git" and len(low) > 1 and low[1].startswith("credential"))):
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


# ---- folders: where each command of a line may run (for git checkout and git restore) ---------------

def starts_of(cwd):
    """The folders the line may start in, as a tuple: the hook passes several (it does not assume its folder)."""
    if cwd is None:
        return (os.getcwd(),)
    folders = [cwd] if isinstance(cwd, str) else [c for c in cwd if c]
    return tuple(dict.fromkeys(os.path.normpath(os.path.abspath(c)) for c in folders)) or (os.getcwd(),)


# A word that may change the shell's folder, other than a `cd <dir> &&` that the tracking follows.
CHANGERS = ("cd", "pushd", "popd", "chdir", "eval", "source")
CHANGER_FIRST = (".", "exec", "builtin", "command")


def changes_folder(words):
    return any(program(w) in CHANGERS for w in words) or words[0] in CHANGER_FIRST


def cd_target(words, heres, cdpath=False):
    """Where `cd` goes, as a tuple (one folder per starting folder); None: not known. Not known either: a ~ form that
    is not a home folder (~-, ~+, ~nobody: the shell reads them otherwise), and, when CDPATH may be set, a relative
    target not starting with . (cd looks along CDPATH first)."""
    if len(words) == 1:
        return (os.path.expanduser("~"),)
    if len(words) != 2 or words[1] == "-":
        return None
    target = os.path.expanduser(words[1])
    if target.startswith("~"):
        return None
    if os.path.isabs(target):
        return (os.path.normpath(target),)
    if heres is None or (cdpath and not target.startswith(".")):
        return None
    return tuple(dict.fromkeys(os.path.normpath(os.path.join(h, target)) for h in heres))


def folders(commands, ops, start):
    """The folders each simple command may run in (a tuple; None: not known).

    A `cd` moves the next commands only when it surely ran and they run only if it did: it comes first, or after ; or
    &&, and is followed by &&. From there the folder is known while commands are joined by &&; after any other
    operator a failed cd could have left the shell where it was. Any other word that may change folder (pushd, popd,
    cd -, builtin cd, eval, source, ...) makes the folder unknown from there on."""
    here = starts_of(start)
    cdpath = bool(os.environ.get("CDPATH")) or any("cdpath" in w.lower() for words in commands for w in words)
    moved, prev, result = False, None, []
    for words, op in zip(commands, ops):
        result.append(here)
        if words[0] == "cd" and prev in (None, ";", "&&") and op == "&&":
            here, moved = cd_target(words, here, cdpath), True
        elif changes_folder(words):
            here = None
        elif moved and op not in ("&&", None):
            here = None
        prev = op
    return result


# ---- R12: GIT_* and GH_* variables ---------------------------------------------------------------------------

GIT_VARIABLE = re.compile(r"^(git|gh)_", re.IGNORECASE)
SETTERS = ("export", "env", "declare", "typeset", "set", "local", "readonly")


def message_skips(words):
    """The indices of words that are message texts, not settings (git commit/merge/stash -m <msg>, hc send's text)."""
    skip = set()
    if words[:2] == ["hc", "send"] and len(words) == 4:
        skip.add(3)
    if words[:1] == ["git"]:
        k = 2 if words[1:2] == ["--no-pager"] else 1
        if words[k:k + 1] and words[k] in ("commit", "merge", "stash"):
            for i in range(k + 1, len(words) - 1):
                if re.match(r"^-[as]*m$", words[i]):
                    skip.add(i + 1)
    return skip


def git_variable(commands):
    """The first word that sets a GIT_* or GH_* variable: NAME=value anywhere (message texts aside), or a bare NAME
    after export, declare, ... ; None if there is none."""
    for words in commands:
        skip, setter = message_skips(words), False
        for i, w in enumerate(words):
            if i in skip:
                continue
            m = ASSIGN.match(w)
            if m and GIT_VARIABLE.match(m.group(1)):
                return w
            if setter and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", w) and GIT_VARIABLE.match(w):
                return w
            if program(w) in SETTERS:
                setter = True
    return None


def text_commands(line):
    """The words of a line that is not plain, roughly: quotes and backslashes taken out, split at blanks and at
    operators, redirections, parentheses, $( and backticks. Used only to look for GIT_* / GH_* settings (R12)."""
    text = re.sub(r"[\"'\\]", "", line or "")
    return [part.split() for part in re.split(r"\$\(|[;&|<>()`\n\r]", text) if part.split()]


# ---- R3, R4: the line ----------------------------------------------------------------------------------------

def check_command(line, pa_address=None, own_address=None, extra_rows=(), cwd=None):
    """None when the Bash line may run; else the refusal message (spec section 6, and R12).

    cwd: the folder the line starts in, or several when that is not certain (pa-hook guard passes the reported folder,
    the worktree root and the project folder); every one of them is checked.
    extra_rows: the profile's added rows, or a function that returns them; it is called only when a
    command involves git, gh or hc and no row of ALLOWLIST allows it (so its notes come only then).
    """
    scanned = split_plain_ops(line or "")
    if scanned is None:
        if person_text(line):
            return PERSON_MSG.format(shown(line))
        variable = git_variable(text_commands(line))
        if variable is not None:
            return VARIABLE.format(shown(variable))
        return NOT_PLAIN if involves_text(line) else None
    commands, ops = scanned
    extra = None
    wrapped = wraps(commands)
    places = folders(commands, ops, starts_of(cwd))
    for words, here in zip(commands, places):
        # The person's own tools first, with their own message; message texts (git commit -m, hc send) aside. A git line
        # that a built-in row allows runs git, not them: its words are paths (git add pa_push.py; card d-067); one that no
        # row allows gets the off-table refusal below. A line only a profile row allows is checked here as before.
        ctx = {"pa_address": pa_address, "own_address": own_address, "cwd": here}
        if words[0] == "git" and not allowed(words, ALLOWLIST, ctx) and extra is None:
            extra = list((extra_rows() if callable(extra_rows) else extra_rows) or [])
        if words[0] == "git" and (allowed(words, ALLOWLIST, ctx) or not allowed(words, extra, ctx)):
            continue
        skip = message_skips(words)
        if any(i not in skip and (person_only(w) or (ASSIGN.match(w) and person_text(w.split("=", 1)[1]))
                                  or (wrapped and (person_text(w) or person_text(wrapped_text(w)))))
               for i, w in enumerate(words)):
            return PERSON_MSG.format(shown(" ".join(shlex.quote(w) for w in words)))
    variable = git_variable(commands)
    if variable is None and wrapped:
        # REVIEW-7 I1: an eval or shell -c text is a line of its own; R12 applies to it (message texts aside).
        for words in commands:
            skip = message_skips(words)
            for i, w in enumerate(words):
                if i not in skip and variable is None:
                    variable = git_variable(text_commands(wrapped_text(w)))
    if variable is not None:
        return VARIABLE.format(shown(variable))
    for words, here in zip(commands, places):
        # With a shell or eval in the line, git, gh or hc named inside any word's text counts too (it may be run).
        if involves(words) or (wrapped and any(involves_text(w) or involves_text(wrapped_text(w)) for w in words)):
            ctx = {"pa_address": pa_address, "own_address": own_address, "cwd": here}
            if not allowed(words, ALLOWLIST, ctx):
                if extra is None:
                    extra = list((extra_rows() if callable(extra_rows) else extra_rows) or [])
                if not allowed(words, extra, ctx):
                    return OFF_TABLE.format(shown(" ".join(shlex.quote(w) for w in words)))
    if any(involves(words) for words in commands):
        # What git, gh or hc printed is not run by a shell reading the pipe (git show HEAD:x.sh | bash).
        for k in range(1, len(commands)):
            if ops[k - 1] == "|" and runs_input(commands[k]):
                return OFF_TABLE.format(shown(" ".join(shlex.quote(w) for w in commands[k])))
    return None


# ---- R8: the editor rule ---------------------------------------------------------------------------

STATE_FILES = ("ledger.json", "journal.jsonl", "rules.json", "agent.json")
# The worktree's task record and its hook settings, wherever they are.
GUARDED = ((".claude", "pa-task.json"), (".claude", "settings.local.json"))


def forms(folder):
    """A folder as written and with links resolved, in lower case (the Mac's file system ignores case)."""
    if not folder:
        return set()
    return {os.path.normpath(os.path.abspath(folder)).lower(), os.path.realpath(folder).lower()}


def protected(full, tops):
    """An absolute path is inside a .git folder (or is a worktree's .git file), is a task record or hook settings file
    (GUARDED), or is inside the spaces folder other than <space>/evidence/..., or anywhere else under the home.
    Also the person's own git and gh settings (personal). tops: (forms(spaces_dir), forms(home), personal settings).
    Checked as written and after resolving links."""
    spaces, homes, personal = tops
    for low in {full.lower(), os.path.realpath(full).lower()}:
        parts = low.split(os.sep)
        if ".git" in parts:
            return True
        if tuple(parts[-2:]) in GUARDED:
            return True
        if any(low == top or low.startswith(top + os.sep) for top in personal):
            return True
        for top in spaces:
            if low == top or low.startswith(top + os.sep):
                rest = low[len(top) + 1:].split(os.sep)
                if len(rest) < 3 or rest[1] != "evidence":
                    return True
                break
        else:
            if any(low == top or low.startswith(top + os.sep) for top in homes):
                return True
    return False


def personal_settings():
    """The person's own git and gh settings, as folders/files (forms): ~/.gitconfig, ~/.config/git, ~/.config/gh, and
    the same under $XDG_CONFIG_HOME when it is set. Read at each check, so a test's HOME applies."""
    user = os.path.expanduser("~")
    configs = [os.path.join(user, ".config")] + [c for c in [os.environ.get("XDG_CONFIG_HOME")] if c]
    found = set(forms(os.path.join(user, ".gitconfig")))
    for config in configs:
        found |= forms(os.path.join(config, "git")) | forms(os.path.join(config, "gh"))
    return found


def check_edit(path, spaces_dir, cwd=None, home=None):
    """None when Edit/Write/MultiEdit/NotebookEdit may write path; else the refusal message.

    Refused, both for the path as written and after resolving links: anything inside a .git folder or a worktree's
    .git file; a task record or hook settings file (.claude/pa-task.json, .claude/settings.local.json); anything in
    <spaces_dir> except a space's evidence folder (<spaces_dir>/<space>/evidence/...), so decisions/, ledger.json,
    journal.jsonl, rules.json, agent.json (STATE_FILES) and the rest of the state; and, when home (the Project Agent's
    home) is given, anything else under it (profiles, core texts, tools); and the person's own git and gh settings
    (~/.gitconfig, ~/.config/git/, ~/.config/gh/, card d-048). Names are compared without case (the Mac's
    file system ignores it). cwd may be several folders; a relative path is refused when it is protected from any.
    """
    if not path:
        return None
    tops = (forms(spaces_dir), forms(home), personal_settings())
    for start in starts_of(cwd):
        given = os.path.normpath(os.path.join(start, os.path.expanduser(path)))
        if protected(given, tops):
            return OFF_TABLE.format(shown(path))
    return None


def operands(args):  # t-016, the Bash write rule: words not starting with -, every word after --; empty words skipped
    return [a for j, a in enumerate(args) if a and (not a.startswith("-") or "--" in args[:j])]


def file_targets(words):
    """The words a listed file command writes (spec section 3); [] for any other program, a launcher included."""
    p, args = program(words[0]), words[1:]
    if p in ("cp", "ln", "install", "rsync", "mv"):  # rsync: -t is --times, so its last operand only
        named = [a.split("=", 1)[1] for a in args if a.startswith("--target-directory=")]
        if p not in ("rsync", "mv"):
            named += [a[2:] for a in args if a.startswith("-t") and a != "-t"] + [b for a, b in zip(args, args[1:]) if a == "-t"]
        return named + operands(args) if p == "mv" or p == "install" and "-d" in args else named or operands(args)[-1:]
    if p in ("rm", "rmdir", "unlink", "touch", "mkdir", "tee", "truncate", "chmod", "chown", "chgrp"):
        return operands(args)
    if p == "dd":
        return [a[3:] for a in args if a.startswith("of=")]
    sed, script = p == "sed" and any(a.startswith(("-i", "--in-place")) for a in args), ("-e", "-f", "--expression", "--file")
    if not sed and not (p == "perl" and any(short_cluster(a, "i") for a in args)):
        return []  # sed or perl without in-place: reads
    values = [j + 1 for j, a in enumerate(args) if (a in script if sed else a[-1:] in ("e", "E") and short_cluster(a, a[-1]))]
    rest = operands([a for j, a in enumerate(args) if j not in values])
    return rest if values or sed and any(a.startswith(script) for a in args) else rest[1:]  # else the first is the script


def folder_change(words, here, cdpath):
    """A cd or pushd adds its folder to here (it only grows); returns the words that make the folder unknown (R4), or None."""
    p, args, found = program(words[0]), words[1:], operands(words[1:])
    t = found[0] if found else "~"
    if (p == "popd" or p in ("builtin", "command") and args and program(args[0]) in ("cd", "pushd", "popd")
            or p in ("cd", "pushd") and ("-" in args or len(found) > 1 or p == "pushd" and not found
                                         or t.startswith("~") and t != "~" and not t.startswith("~/")
                                         or cdpath and not t.startswith((".", "/", "~")))):
        return " ".join(words)
    if p in ("cd", "pushd"):  # joined to each folder; an absolute one stays as it is
        here[:] = list(dict.fromkeys(here + [os.path.normpath(os.path.join(h, os.path.expanduser(t))) for h in here]))
    return None


def check_writes(line, spaces_dir, cwd=None, home=None):
    """None when a plain Bash line writes nothing check_edit protects; else WRITE or FOLDER_UNKNOWN (spec t-016)."""
    scanned = split_plain_ops(line or "", writes=True) or ([], None, [])  # a line that is not plain is not judged
    tops = (forms(spaces_dir), forms(home), personal_settings())
    here, cdpath, unknown = list(starts_of(cwd)), bool(os.environ.get("CDPATH")), None
    for words, outs in zip(scanned[0], scanned[2]):
        words = words[next(k for k, w in enumerate(words) if k == len(words) - 1 or not ASSIGN.match(w)):]
        for t in [t for t in outs + file_targets(words) if t and (t[:1] != "~" or t == "~" or t.startswith("~/"))]:
            full = os.path.expanduser(t)  # ~x forms other than ~ and ~/ are skipped
            if not os.path.isabs(full) and unknown:
                return FOLDER_UNKNOWN.format(word=shown(unknown), target=shown(t))
            if any(protected(os.path.normpath(os.path.join(h, full)), tops) for h in here):  # absolute: full itself
                return WRITE.format(target=shown(t))
        unknown = unknown or folder_change(words, here, cdpath)
    return None
