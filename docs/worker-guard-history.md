# The worker guard before t-025: the allowlist, the plain-lines rule and the Bash write rule

## 1. Summary

Task t-025 replaced three rules of the worker guard (`runtime/tools/pa_guard.py`, called by `pa-hook guard`) with
a short denylist: a worker's Bash line is refused only when it pushes, publishes or merges a change request,
deletes a branch or a worktree, or runs the person's push tools. The removed rules were the allowlist of git, gh
and hc commands, the rule that a line naming them must be plain, and the Bash write rule. This file records what
they did, why they were built, what they cost, why they went, and where their code is. To bring them back,
restore the files named in part 8 from commit `af47aa1` and install.

Task and card ids (t-007, d-034, ...) refer to the Project Agent's own records for this repository, which are not
part of the repository.

## 2. The allowlist

Built in t-007 part 7a (cards d-034 and d-036; later d-038, d-048 and d-067), with the person-only names of
t-007 part 7b.

**What it did.** git, gh and hc ran only as rows of a table (`ALLOWLIST`, about 40 rows). Each row named a
command (`git status`, `git checkout`, `gh pr view`, `hc send`, ...) and a function that judged its arguments:
`git checkout` only of a branch name (a word naming an existing path was refused, so a checkout could not discard
a file), `git config` only to read a key, `git branch` only to list or create, `git rebase` only onto one branch
or with `--continue`/`--abort`/`--skip`, `hc send` only to the Project Agent's own address, `hc read` only of the
worker's own address, and read-only rows such as `git archive`, `git grep` and `git cat-file` without an
`--output` option. Anything else that involved git, gh or hc was refused with one message ("Refused by the
Project Agent's allowlist: ... Workers run only the git, gh and hc commands on the allowlist"). A profile could
add rows (`guardAllow` in its `profile.json`, args `any` or `none`) but never one in `NEVER_ADD` (push, remote
and config, credentials, submodules, publishing gh commands, `hc send`, `hc run` and the like). A word setting a
`GIT_*` or `GH_*` variable was refused, because such a variable changes what an allowed command does. The
person's push tools (`pa-push-main`, `pa-push-allow`, `pa_push.py`) were refused in every form, with their own
message; an allowed git command could still name the tool's file as a path (`git add`), unless its output was
piped into anything but a text filter.

**Why it was built.** The investigation for t-007 found that the older push pattern in `pa_hooks.py` could be
passed by ordinary forms (`git -C dir push`, quoting, `--all`, a `cd` first). A table that tried to block every
bypass was itself stopped by a safety classifier as too close to an evasion list, so the person chose an
allowlist (d-034) and accepted it with read-only additions (d-036). A guard that could not load refused
everything (d-038).

**What it cost.** About 600 lines of the guard's 1025, and the largest part of `tests/test_pa_guard.py`. In
review, t-007's records hold eight review rounds (REVIEW to REVIEW-8) and seven verify rounds (VERIFY to
VERIFY-7) for the allowlist and its fixes, then three split and pipe fix rounds, then the part 7b rounds for the
person-only names. In use: every ordinary git form without a row was refused (part 5).

**Why it was removed.** The person, 2026-11-17: it "is blocking too many things like reset, squash, etc. because
complex statements are used all the time". Ordinary work (reset, squash merges, interactive rebase, `-q`, `git
rm`/`mv`, `archive`) needed rows that were not there, and each new row was a new review.

**History.** First version `be74cdb` (its tests `affb9f2`); fix rounds `ffe8e09` and `ca7adf8`; the split of the
write rule out of it `1818c8a`, `3dfe006`, `ebdad24`; pipe fixes `d37e1f5` and `65b9f77`; the person-only names
`05cff7c`; d-067 (an allowed git command may name the push tool's file as a path, as in `git add`)
`9029f41`; the read rows with output redirects and one `-C` `e07d5c6`.

## 3. The plain-lines rule

Same task (requirements R1 and R4 of the allowlist spec), with the exception added in t-016 job 2.

**What it did.** A line that named git, gh or hc had to be plain: simple commands of literal words, joined only
by `;`, `&&`, `||` and `|`. Anything else was refused ("beyond plain commands"): `$` and backticks, redirects,
heredocs, a lone `&`, parentheses and braces, unquoted globs, line breaks, an unterminated quote. To decide
whether a line that was not plain named git at all, the rule looked for the names hidden by quotes and
backslashes, by `$'...'` escapes (`$'\x67it'`), by expansions that may be empty (`gi${X}t`), by default values
(`${X:-git}`) and by command substitutions, and it read the text run by `sh -c`, `eval`, `watch` and `env -S`.
It also refused piping git output into a shell or into `source`. From t-016 job 2, the five read rows (`git
status`, `log`, `diff`, `show`, `rev-parse`) could take output redirects and one leading `-C <dir>`.

**Why it was built.** Only a plain line can be matched word by word against the rows of a table; the other
choice was to parse shell, which is far larger and still incomplete.

**What it cost.** Most of the friction list in part 5. It was also the largest source of review findings: hidden
names, wrappers and pipes each took fix rounds, and each fix made the scanner larger.

**Why it was removed.** The same words of the person. Agents write compound lines all the time, and a rule that
refuses them sends the worker to a script file or the editor for a one-line task.

**History.** As for the allowlist (part 2), plus `e07d5c6` (the read-row exception), `d81ba97`, `50b288e` and
`a1c8873` (t-016 job 2's scanner rounds).

## 4. The Bash write rule

Cards d-042, d-046, d-048, d-058 and d-078; t-016 job 1. Its four known gaps (t-024) were never closed.

**What it did.** A plain Bash line that wrote, by a redirect or by one of the file commands in its table (`cp`,
`mv`, `ln`, `install`, `rsync`, `rm`, `rmdir`, `unlink`, `touch`, `mkdir`, `tee`, `truncate`, `chmod`, `chown`,
`chgrp`, `dd of=`, `sed -i`, `perl -i`), into a protected place was refused: a `.git` folder, a worktree's task
record or hook settings (`.claude/pa-task.json`, `.claude/settings.local.json`), the Project Agent's state (a
space's evidence folder excepted), the rest of its home, or the person's git and gh settings. A relative target
was judged from every folder a `cd` earlier in the line could have reached, and a `cd` it could not follow
(`cd -`, `pushd`, a `~user` form, `CDPATH`) made later relative writes refused ("the folder is not known").

**Why it was built.** The editor tools were already refused in those places (d-042), and Bash was the open side
door. The first version (`ef0e9b1`, `ca7adf8`) was a second scanner inside the allowlist work and was judged hard
to trust, so it was split out (d-048, `1818c8a`; that full version was kept on a local branch,
`t-007a-guard-with-path-rule`, which may exist only on the person's machine). It was rebuilt on the shared
scanner in t-016 (d-058).

**What it cost.** About 100 lines, at the edge of its size budget; three review rounds in t-016 job 1; four more
in job 2 over how `touch` and `truncate` read their options (d-078 settled it); and four known gaps queued as
t-024.

**Why it was removed.** The person's choice of a short denylist. Because a line that is not plain was never
judged, the rule fenced only plain lines, which the person no longer wanted to require.

**History.** `9d4b195` (its tests `b6e1bd3`), `0e6a17c`, `4198c54`, `d81ba97`, `50b288e`, `a1c8873`.

## 5. What it cost in practice: the friction list

Each line names the task whose records hold it.

- git with a redirect, a heredoc, a brace group or a substitution was refused, even when git was only text in the
  line (t-009, t-016, t-017).
- `git commit -q` and `git commit -qm` were refused (t-011, t-016, t-017).
- `git add ... && git commit ...` was refused when the message held parentheses, and some messages with
  apostrophes were refused (t-017).
- `git diff >> file` was refused before t-016 job 2, and `git diff --output` always (t-017).
- Any line naming the push tool was refused, even heredoc text, a search, or a read of the tool's file (t-009;
  the design of t-025).
- Discarding a file's changes by checkout was refused (t-009).
- `git archive` and `git hash-object` were refused, so blob ids were computed in Python (t-017).
- Reset, squash merges and rebase forms other than onto one branch were refused (the person, 2026-11-17).

The usual way round was a script file, the editor tool, or one plain command per line, each costing extra turns.
The older safety hook's text matching, which stays, also refuses a search or a commit message that contains its
words (for example `git reset --hard`); that is a separate, later question.

## 6. Why it was removed

The person, 2026-11-17: "Remove and cancel the Bash guard rule entirely. It is blocking too many things like
reset, squash, etc. because complex statements are used all the time." Asked how much should go, they chose to
keep a short denylist: push, force push, publishing or merging a change request, deleting branches or worktrees,
and the push tools. "Put the Bash guard rule work in a markdown for documentation, but remove it." The fence is
for workers that follow their rules; the written Safety rules in `runtime/core/worker.md` cover what it does not.

## 7. What replaced it

`check_command(line)` in `pa_guard.py` refuses a simple command, wherever it sits in the line (after `cd ... &&`,
in a subshell, a loop, a substitution, `sh -c '...'` or `eval`), when it:

- D1 pushes: `git push`, `git send-pack`, `git http-push`, `git subtree push`, `git lfs push`, or the dashed
  programs `git-push`, `git-send-pack`, `git-http-push`, with any git global options before the subcommand;
- D2 publishes or merges a change request: `gh pr create`, `new`, `ready` or `merge`;
- D3 deletes a branch or a worktree: `git branch -d`/`-D`/`--delete`, `git worktree remove`, `git update-ref -d`;
- D4 runs the person's push tools: `pa-push-main`, `pa-push-allow`, `pa_push.py` in program position, or
  `pa_push.py` run by Python.

Everything else runs: reset, squash, rebase, stash, redirects, substitutions, pipes, heredocs, `-C`, `-q`,
compound lines, `hc send` to any address. It does not promise that a worker cannot do these things: a script the
worker writes, a git alias, a name built at run time, a program that runs text other than a shell, an API call or
a file deletion are not caught. Behind it: the written Safety rules, `pa_push.py`'s own refusal of agent
processes, the remote's credentials, and the push card's check of the push allowlist's SHA and time.

What stayed: the older refusals in `pa_hooks.py` (force pushes, pushes to or from a main branch, discarding
uncommitted work with `git reset --hard`, `git clean -f`, `git checkout .`, `git restore .`, `git stash
drop|clear`, reading secrets, publishing packages, deploys), a profile's `releaseTools`, the editor rule (Edit and
Write into `.git`, the Project Agent's state and the person's git and gh settings), and fail closed: a guard
that cannot load refuses every Bash and edit call.

## 8. How to bring it back

The code as it last was is at `a1c8873` (the last change to `pa_guard.py`) and unchanged at every later main
commit up to `926bb73`, the commit this change was built on (the design was written at `af47aa1`, which holds the
same files). To restore it:

1. `git show af47aa1:runtime/tools/pa_guard.py` for the guard, and `git show af47aa1:tests/test_pa_guard.py` for
   its tests.
2. From `git show af47aa1:runtime/tools/pa_hooks.py`: the Bash branch of `_hook_guard` (the `check_command` call
   with `pa_address`, `own_address`, `extra_rows` and `cwd`, then `check_writes`) and the `guard` line of the
   module docstring.
3. From `git show af47aa1:runtime/core/worker.md`: the guard bullet of "Rules of the job"; and from `git show
   af47aa1:tests/test_core_text.py`: the class `T016J2WorkerSentence`.
4. Install (`install.sh` copies `pa_guard.py` and `pa_hooks.py` together). Profiles' `guardAllow` key would be
   read again.

## 9. Records

Tasks t-007 (parts 7a and 7b), t-016 (jobs 1 and 2), t-024 (moot) and t-025; cards d-034, d-036, d-038, d-042,
d-046, d-048, d-058, d-067 and d-078, all in the Project Agent's state for this repository.
