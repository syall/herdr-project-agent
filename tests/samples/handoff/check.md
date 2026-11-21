# t-1 check (check role, low tier, round 1, check: full)

Fresh context; you are not an author of t-1. Task t-1: Widget: one thing. Read <EV>/intent.md, plan.md, FINDINGS.md, DECISIONS.md, build.log.
Worktree <WT>, branch t-1-widget, HEAD <C2> (tests <C1>; base main <C0>). Full check: the whole diff <C0>..<C2>.
You report; you never fix. The verifier's and reviewer's job in one:
1. pa-full-suite once at HEAD (never under a timeout), output to full-suite-check.out. Known flakes: test_a test_x1; test_b test_y (a race). If only those fail, rerun each failing one alone and say so.
2. Failing-first: the tests of <C1> fail at <C0> with the tests overlay and pass at HEAD (pa-run, --local only where a test needs this Mac; say where each ran) -> check-failing-first.out.
3. No mutations: this handoff names none (the default). A test gap you find is a Recorded limit, never a Must fix, unless the spec or plan names it.
4. Review against each intent item. Judge D2 (the cache).
Must-fix bar: breaks an intent item, a verification point, safety or security. Else Recorded limit (name a follow-up) or Nit.
Write VERIFY.md and REVIEW.md, each ending 'Checked: <full HEAD id>' and 'Verdict: pass' or 'Verdict: must fix (<n>)'; REVIEW.md has 'Reviewer:' your own address (echo $HC_TERMINAL_ID).
Evidence folder: <EV>.
Space rules for this job (<SP>/rules.json, active, highest order first; last change unknown):
- r-test-runs: Where test runs go
- r-few-mutations: Few mutations
Read each one's text in rules.json before you start and follow it; where this handoff disagrees with an active rule, follow the rule and say so in your report.
Rules: public repo; no tool against the real ~/.config/herdr-pa or a real space other than pa-full-suite's own run; open no file under ~/.claude, ~/.codex or ~/.config/herdr-app; start no Claude session; do not reload Herdr; no push, merge or install; edit and commit nothing (mutations reverted, worktree clean at the end); git commands as plain separate commands. Scripts in a file under $T, run with sh "$T"/x.sh. No rm of a path built by command substitution. Leave no background process. Report with `hc send t9pa` when done or stuck.
