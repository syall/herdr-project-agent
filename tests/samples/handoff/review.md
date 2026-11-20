# t-2 j2 reviewer (reviewer role, normal tier, round 1, check: full)

Fresh context; you are not an author of t-2 j2. Task t-2 j2: Gadget: two things. Read <EV>/plan.md, FINDINGS.md, DECISIONS.md.
Worktree <WT>, branch t-1-widget, HEAD <C2> (base main <C0>). Review the whole diff <C0>..<C2> against the plan. The guard is not weakened.
Run no suite (the verifier runs it); single tests only if you need them.
Must-fix bar: breaks a requirement, the plan, a verification point, safety or security. Else Recorded limit or Nit; say for each whether it is small and inside this task's files (fixed before merge) or accepted (no follow-up task).
Append '## j2 review (full, <C0>..<C2>)' to REVIEW.md with 'Reviewer:' your own address (echo $HC_TERMINAL_ID), ending 'Checked: <full HEAD id>' and 'Verdict: pass' or 'Verdict: must fix (<n>)'.
Evidence folder: <EV>.
Rules: no tool against the real ~/.config/herdr-pa or a real space other than pa-full-suite's own run; open no file under ~/.claude, ~/.codex or ~/.config/herdr-app; start no Claude session; do not reload Herdr; no push, merge or install; edit and commit nothing (worktree clean at the end); git commands as plain separate commands. Scripts in a file under $T, run with sh "$T"/x.sh. No rm of a path built by command substitution. Leave no background process. Report with `hc send t9pa` when done or stuck.
