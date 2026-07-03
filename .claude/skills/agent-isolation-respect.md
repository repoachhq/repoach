---
name: agent-isolation-respect
description: Refuse to commit on protected branches when running as an isolated sub-agent. Verify worktree branch via git rev-parse before any git operation.
---

# agent-isolation-respect

When running as a sub-agent inside a worktree (Agent tool with
`isolation: "worktree"`), you must commit on **the worktree branch only**,
never on `main` / `master`. The 2026-04-28 autonomous run revealed that
2 of 4 sonnets bypassed worktree isolation and committed directly to
main — likely because they `cd`'d outside the worktree before commit.

## Before any git operation

Run this check:

```bash
git rev-parse --abbrev-ref HEAD
```

The output **must** be `worktree-agent-XXX` (or another non-protected
feature branch). If it is `main` or `master`:

1. **STOP** — do not commit, do not merge, do not push.
2. Print `ISOLATION FAILED — current branch is main` and report.
3. Re-check that you are inside the expected worktree directory:
   `pwd` should match `.claude/worktrees/agent-XXX`.
4. If you have already left the worktree, `cd` back into it before
   continuing.

## Never `cd` outside the worktree

The worktree is your isolated copy. The parent directory has its own
working tree (which may have user WIP). Operating in the parent breaks
isolation and risks committing the user's uncommitted changes under the
agent's name.

## When in doubt, ask

If the parent task instructed you to "commit your work" and the branch
check says you are on main, **ask before proceeding** rather than
silently committing.
