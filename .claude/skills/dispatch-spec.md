---
name: dispatch-spec
description: Pattern for dispatching a spec to a sonnet sub-agent — open_work.md entry + structured brief + worktree isolation.
---

# dispatch-spec

Used in the 2026-04-28 autonomous run ~12 times. Pattern that yields
reliable sonnet output:

## 1. Document the spec in `docs/open_work.md`

Each spec carries: id (`SP-NN`), priority (`P0..P3`), status
(`PENDING` / `IN_PROGRESS` / `DONE` / `BLOCKED`), owner, executor,
date opened. The plan is a numbered list. Definition-of-done is one
line.

## 2. Brief the sonnet self-contained

The sonnet has no prior context. Include in the prompt:

- **One sentence project context** (repoach, autonomous software factory,
  CLAUDE.md conventions).
- **ISOLATION CHECK** — call out the worktree branch verification (see
  the `agent-isolation-respect` skill).
- **SPEC block** — the spec id + 3-5 sentences of context.
- **GOAL** — single sentence.
- **PLAN** — numbered list of file reads + edits + tests.
- **DISCIPLINE** — N expected commits with specific commit-message
  prefixes; per-step pytest invocation; watchdog deadline (8-12 min
  per step) with a wip(...) commit fallback if stuck.
- **CONSTRAINTS** — explicit list of files NOT to touch (especially
  user WIP).
- **DELIVERABLE** — N commits + branch name + ≤200 word report.

## 3. Worktree isolation

Use `Agent` tool with `isolation: "worktree"`. Verify after the agent
completes that the commits land on the worktree branch (not main!) by
checking `git worktree list`.

## 4. Merge back

After a sonnet completes, the orchestrator (you, opus) reviews the
commits, runs the tests on the worktree branch, then either:
- Cherry-picks individual commits onto the integration branch.
- Or `git merge --no-ff` the worktree branch.
- Or pushes the worktree branch to the remote and opens a PR.

## 5. Cleanup

Once merged: `git worktree remove` + `git branch -d` the
`worktree-agent-XXX` artifacts.
