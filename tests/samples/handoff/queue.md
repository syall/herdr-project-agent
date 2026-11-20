t-1 (position 1 of 1, on main at <C0>, tip <C2>, state checking)
worktree: <WT>
report with hc send t9pa when done: what ran, its result, and the review file's path

Look at the README change.

Space rules for this job (<SP>/rules.json, active, highest order first; last change unknown):
- r-test-runs: Where test runs go
- r-few-mutations: Few mutations
Read each one's text in rules.json before you start and follow it; where this handoff disagrees with an active rule, follow the rule and say so in your report.
Known flakes: test_a test_x1; test_b test_y (a race). If only those fail, rerun each failing one alone and say so. The Reviewer: line must be your own terminal address exactly as `echo $HC_TERMINAL_ID` prints it, nothing else. Rules: edit and commit nothing; no push, merge or install; open no file under ~/.claude, ~/.codex or ~/.config/herdr-app; start no Claude session; git commands as plain separate commands. Leave no background process.
