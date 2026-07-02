---
name: parallel-sonnet-dispatch
description: Launch N sonnets in parallel on independent specs, each in an isolated worktree. Useful when specs don't share files.
---

# parallel-sonnet-dispatch

Used 2026-04-28 to land 5 specs in 30 minutes (CH-11b,
Source resilience, CH-08, Sofascore-down test, doc audit). Pattern:

## When to use

- ≥2 specs that touch **disjoint files** (no merge-time conflict).
- Each spec has a clear PLAN, DISCIPLINE block, and DELIVERABLE
  contract.
- The orchestrator (opus) can wait asynchronously without blocking on
  any one sonnet.

## Recipe

1. List the specs, write a 1-line dependency graph between them.
   If any two share files, **don't** parallelize them — sequence
   instead.

2. For each spec, write a self-contained brief (see
   `dispatch-spec` skill). Include the ISOLATION CHECK, watchdog
   discipline, and explicit "DO NOT touch X, Y, Z" lists.

3. Send all `Agent` tool calls in **a single message** with
   `run_in_background: true`. Each sonnet gets its own worktree.

4. Use the wait-for-notification model: do NOT poll. Continue with
   other work (review existing branches, write reports, dispatch the
   next batch) and rely on the completion notifications.

5. When notifications come in, review each sonnet's output:
   - `git log <worktree-branch>` to confirm commits landed there.
   - `git worktree list` to confirm isolation held.
   - If commits landed on `main` instead of the worktree branch
     (isolation failure), see `agent-isolation-respect`.

6. Merge sequentially — handle merge-time conflicts.

## Anti-patterns

- Dispatching 5 sonnets all touching `workflow.py`: their commits will
  ALL conflict at merge time, costing more than serial dispatch.
- Polling sonnet output via `TaskOutput` in a tight loop: the agent's
  own waits cost cache and tokens.
