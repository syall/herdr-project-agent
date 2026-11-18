"""The worker guard's checks for `pa-hook guard` (pa_hooks.hook_guard). Python standard library only (3.7+).

check_command(line) refuses a Bash line only when a simple command in it (also inside $(...), `...`, <(...), >(...),
sh -c or eval, to depth 4) pushes (D1), publishes or merges a change request (D2), deletes a branch or a worktree
(D3), or runs the person's push tools (D4); spec t-025. When unsure, it lets the line run. check_edit(path,
spaces_dir, cwd, home) refuses editor writes into .git, the Project Agent's files (evidence excepted) and the
person's git and gh settings. A fence for workers that follow their rules, not a sandbox: scripts, aliases and names
built at run time are not caught. The rules this replaced are described in docs/worker-guard-history.md.
"""
import os
import re
import shlex

SHOWN = 300  # longest command or path quoted back in a message
TEXT_HINT = ("If this is text, not a command (a heredoc line or an unquoted argument), quote it or write it with the "
             "editor tool.")
refusal = lambda middle, hint=True: ("Refused by the Project Agent's guard: {}" + middle + (" " + TEXT_HINT if hint else "")
                                     + " There is no override.")

DENY_PUSH = refusal(" pushes to a remote. Agents never push (worker.md, Safety): commit your work and say in your report "
                    "that it is ready; the person pushes.")
DENY_PR = refusal(" publishes or merges a change request. That is the person's decision (worker.md, Safety): say in your "
                  "report that the work is ready, and the Project Agent asks them on a decision card.")
DENY_DELETE = refusal(" deletes a branch or a worktree. Deleting is the person's decision (worker.md, Safety): leave it, "
                      "and name it in your report if it should go.")
DENY_TOOLS = refusal(". pa-push-main and pa-push-allow are the person's commands: a main branch is pushed only when the "
                     "person answers a push card in the panel. Say in your report that the work is ready to release.", False)
EDIT = refusal(" is inside a .git folder, the Project Agent's files or the person's git and gh settings, which workers do "
               "not edit. Write reports, logs and notes in your task's evidence folder (evidence/<task-id>/ in the Project "
               "Agent's state folder). If the edit is needed, stop and report it to the Project Agent with hc send.", False)

SUBSTITUTION = re.compile(r"\$\(([^()]*)\)|`([^`]*)`|[<>]\(([^()]*)\)")  # no ( ) inside: innermost first
ARROWS = re.compile(r"[<>&|]*")  # a redirect operator: > >> >& &> >| < << <<<
SHELLS, SHELL_C = ("sh", "bash", "zsh", "dash", "ksh", "fish"), re.compile(r"^-[A-Za-z]*c[A-Za-z]*$")  # -c -lc -ec
GIT_VALUE = ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--super-prefix")
KEYWORDS = ("!", "{", "if", "then", "else", "elif", "do", "while", "until")
LAUNCHERS = ("sudo", "env", "command", "builtin", "exec", "nohup", "nice", "time", "timeout", "xargs", "caffeinate")
ASSIGNMENT, NUMBER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*="), re.compile(r"^\d+(\.\d+)?[smhd]?$")  # 30, 2.5s, 5m
PYTHON = re.compile(r"^(python|python3|python3\.\d+|pypy.*)$")
BRANCH_DELETE = re.compile(r"^(--delete|-[A-Za-z]*[dD][A-Za-z]*)$")  # -d, -D, -dr, -Df


def program(word):
    """The last part of a path, in lower case: the Mac's file system finds /usr/bin/git for GIT or Git too."""
    return word.rstrip("/").rsplit("/", 1)[-1].lower()


def shown(text):
    return text if len(text) <= SHOWN else text[:SHOWN - 3] + "..."


def simple_commands(line):
    """The simple commands of a line, as lists of words: quote marks, escapes and redirect targets taken out."""
    commands, words, word, drop, i, n = [], [], None, False, 0, len(line)  # drop: the next word is a redirect target
    while i < n:
        c = line[i]
        if c in "'\"" or line.startswith("$'", i):  # up to the closing quote, or the end of the text
            quote, escapes = ("'", True) if c == "$" else (c, c == '"')
            i, word = i + (2 if c == "$" else 1), word if word is not None else []
            while i < n and line[i] != quote:
                i += 2 if escapes and line[i] == "\\" and i + 1 < n else 1
                word.append(line[i - 1])
            i += 1
        elif c not in " \t;&|()\n\r<>":  # a character of the word; a backslash escapes the next one
            char, i = (line[i + 1:i + 2], i + 2) if c == "\\" else (c, i + 1)
            if char not in ("", "\n"):  # a backslash before a line break joins the lines
                word = word if word is not None else []
                word.append(char)
        else:
            redirect = c in "<>" or line.startswith("&>", i)
            if c in "<>" and word and "".join(word).isdigit() and line[i - 1].isdigit():
                word = None  # 2>: the file descriptor
            if word is not None and not drop:
                words.append("".join(word))
            drop, word = drop and word is None, None
            if redirect:  # > >> >& &> < << <<< and the next word, its target
                i, drop = ARROWS.match(line, i).end(), True
                continue
            if c not in " \t":  # the end of a simple command
                commands += [words] if words else []
                words, drop = [], False
            i += 1
    if word is not None and not drop:
        words.append("".join(word))
    return commands + [words] if words else commands


def skip_options(words, j, takes_value):
    """The index of the first word from j on that does not start with -, the values of takes_value skipped too."""
    while j < len(words) and words[j].startswith("-"):
        j += 2 if words[j] in takes_value else 1
    return j


def program_position(words):
    """The index of the program word: after NAME=value words, keywords and launchers with their options."""
    k, launched = 0, False
    while k < len(words) and (ASSIGNMENT.match(words[k]) or words[k] in KEYWORDS or program(words[k]) in LAUNCHERS
                              or launched and (words[k].startswith("-") or NUMBER.match(words[k]))):
        k, launched = k + 1, launched or program(words[k]) in LAUNCHERS
    return k


def row_of(words):
    """The message of the first row (D1, D2, D3, D4) that this simple command matches, or None."""
    n, names, first = len(words), [program(w) for w in words], program_position(words)
    word = lambda j: words[j] if j < n else ""
    after = lambda j: word(skip_options(words, j + 1, ()))  # the next word not starting with -
    subs, actions, i = [], [], 0  # each git word's subcommand index; each gh pr's action
    while i < n:
        if names[i] in ("git", "gh"):
            j = skip_options(words, i + 1, GIT_VALUE if names[i] == "git" else ("-R", "--repo"))
            if names[i] == "git":
                subs.append(j)
            elif word(j) == "pr":
                actions.append(word(skip_options(words, j + 1, ("-R", "--repo"))))
            i = max(i + 1, j)
        else:
            i += 1
    deleting = max([k for k, w in enumerate(words) if BRANCH_DELETE.match(w)], default=-1)  # the last delete option
    removing = max([k for k, w in enumerate(words) if w in ("-d", "--delete")], default=-1)
    if any(word(j) in ("push", "send-pack", "http-push") or word(j) in ("subtree", "lfs") and after(j) == "push"
           for j in subs) or first < n and names[first] in ("git-push", "git-send-pack", "git-http-push"):
        row = DENY_PUSH
    elif any(action in ("create", "new", "ready", "merge") for action in actions):
        row = DENY_PR
    elif any(word(j) == "branch" and deleting > j or word(j) == "worktree" and after(j) == "remove"
             or word(j) == "update-ref" and removing > j for j in subs):
        row = DENY_DELETE
    elif first < n and (names[first] in ("pa-push-main", "pa-push-allow", "pa_push.py")
                        or PYTHON.match(names[first]) and "pa_push.py" in names[first + 1:]):
        row = DENY_TOOLS
    else:
        return None
    return row.format(shown(" ".join(shlex.quote(w) for w in words)))


def judge(line, depth):
    """The verdict on a line met at this depth of nesting (wrappers and substitutions); deeper than 4 is not judged."""
    found = None
    for _ in range(8 if depth <= 4 else 0):  # substitutions, innermost first: each body is a line of its own
        bodies = []
        line = SUBSTITUTION.sub(lambda m: bodies.append(next(g for g in m.groups() if g is not None)) or " ", line)
        found = next((f for f in (judge(body, depth + 1) for body in bodies) if f), None)
        if found or not bodies:
            break
    for words in [] if found or depth > 4 else simple_commands(line):
        found, shell = row_of(words), False
        for k, w in enumerate([] if found else words):
            if program(w) == "eval":  # the words after it are a line of their own
                found = judge(" ".join(words[k + 1:]), depth + 1)
                break
            if shell and SHELL_C.match(w) and k + 1 < len(words):  # sh -c '...': the word after -c
                found, shell = judge(words[k + 1], depth + 1), False
                if found:
                    break
            shell = shell or program(w) in SHELLS
        if found:
            break
    return found


def check_command(line):
    """None when the Bash line may run; else the refusal of the first row its simple commands match (spec 3)."""
    return judge(line if isinstance(line, str) else "", 0)


# ---- The editor rule (t-007 R8, kept by t-025) --------------------------------------------------------------------

def starts_of(cwd):
    """The folders the line may start in, as a tuple: the hook passes several (it does not assume its folder)."""
    if cwd is None:
        return (os.getcwd(),)
    folders = [cwd] if isinstance(cwd, str) else [c for c in cwd if c]
    return tuple(dict.fromkeys(os.path.normpath(os.path.abspath(c)) for c in folders)) or (os.getcwd(),)


STATE_FILES = ("ledger.json", "journal.jsonl", "rules.json", "agent.json", "content/")  # content/: t-052, tool-written only
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
            return EDIT.format(shown(path))
    return None
