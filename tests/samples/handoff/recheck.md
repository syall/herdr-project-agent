# t-1 recheck (check role, low tier, round 2, check: diff)

Fresh context; you are not an author of t-1. Task t-1: Widget: one thing. The round 1 check at <C2> found 1 Must fix (read <EV>/REVIEW.md '## Must fix'); round 2 fixed it: read FINDINGS.md, DECISIONS.md, build.log 'Round 2'.
Worktree <WT>, branch t-1-widget, HEAD <C4> (tests <C3>). Diff check: <C2>..<C4>.
You report; you never fix.
1. pa-full-suite once at HEAD (never under a timeout), output to full-suite-check-r2.out. Known flakes: test_a test_x1; test_b test_y (a race). If only those fail, rerun each failing one alone and say so.
2. Failing-first: the tests of <C3> fail at <C2> and pass at HEAD (pa-run, --local only where a test needs this Mac; say where each ran) -> check-failing-first-r2.out.
3. At most 3 mutations, on the changed lines only (r-few-mutations, a fix that changed logic), each running only the tests that cover it -> check-mut-r2.log.
4. Review: each Must fix is closed and nothing else changed. M1: the lock is read first.
Must-fix bar: breaks a Must fix's fix, a verification point, safety or security. Else Recorded limit or Nit; say whether each is small and inside this task's files (fixed before merge) or accepted (no follow-up task).
Append '## Round 2 recheck (diff <C2>..<C4>)' to VERIFY.md and REVIEW.md, each ending 'Checked: <full HEAD id>' and 'Verdict: pass' or 'Verdict: must fix (<n>)'; REVIEW.md's section has 'Reviewer:' your own address (echo $HC_TERMINAL_ID).
Evidence folder: <EV>.
Rules: public repo; no tool against the real ~/.config/herdr-pa or a real space other than pa-full-suite's own run; open no file under ~/.claude, ~/.codex or ~/.config/herdr-app; start no Claude session; do not reload Herdr; no push, merge or install; edit and commit nothing (mutations reverted, worktree clean at the end); git commands as plain separate commands. Scripts in a file under $T, run with sh "$T"/x.sh. No rm of a path built by command substitution. Leave no background process. Report with `hc send t9pa` when done or stuck.
