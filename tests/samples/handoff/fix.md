# t-1 fix (build role, low tier, round 2)

Task t-1: Widget: one thing. The check at <C2> found 1 Must fix: read <EV>/REVIEW.md '## Must fix' (and VERIFY.md). Also read plan.md, FINDINGS.md, DECISIONS.md.
Worktree <WT>, branch t-1-widget, HEAD <C2>. Hooks installed.
Fix only the Must fix findings. Recorded limits and Nits stay as they are. M1 only, in widget.py.
1. A test first that fails at <C2>: commit it while failing. 2. The fix. 3. Affected tests (pa-run, --local only where a test needs this Mac; say where each ran). No pa-full-suite. 4. Append a 'Round 2' section to FINDINGS.md, DECISIONS.md and build.log with real output. Commit; report commit ids and files touched.
Evidence folder: <EV>.
Rules: public repo, nothing work-specific; no tool against the real ~/.config/herdr-pa or a real space; open no file under ~/.claude, ~/.codex or ~/.config/herdr-app; start no Claude session; do not reload Herdr; no push, merge or install; git commands as plain separate commands. Multi-line scripts in a file under $T, run with sh "$T"/x.sh. No rm of a path built by command substitution. Leave no background process; commit your work; worktree clean at the end. Report with `hc send t9pa` when done or stuck.
