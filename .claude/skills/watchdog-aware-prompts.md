---
name: watchdog-aware-prompts
description: Split sonnet work into 2-3 commits with COMMIT-after-each discipline to survive the 600s watchdog timeout.
---

# watchdog-aware-prompts

The Agent tool has a watchdog that may kill long-running sub-agents
around the 600-second mark. The 2026-04-27 run had 2 stalls at this
exact threshold with all work lost. The remediation is the
"split-and-commit" discipline.

## Pattern in the prompt

Every sonnet brief should specify:

```
DISCIPLINE
----------
- N commits expected (typically 2-3):
  1. "feat(scope): ... (SP-XX 1/N)"
  2. "feat(scope): ... (SP-XX 2/N)"
  3. "test(scope): ... (SP-XX N/N)"
- After EACH commit, run pytest -k <scope>; commit only if green.
- Watchdog deadline: 8-12 minutes per commit.
- If stuck: commit WIP with prefix "wip(scope): ..." and report what's
  missing.
```

## Why this works

- The smallest unit-of-progress is a single commit. If the watchdog
  kills the sonnet between commit 2 and 3, you still have commits
  1+2 on the worktree branch — recoverable.
- The `wip(...)` fallback ensures even partial work is preserved.
- Tests-after-each-commit enforce that each step is independently
  green; you can resume from any commit.

## Anti-patterns (observed 2026-04-27)

- "Implement the whole feature, run tests at the end, commit once":
  stall mid-implementation → 0 commits → all work in transcript only.
- "Implement A, B, C in any order, commit when ready": no
  intermediate checkpoint → same risk.
