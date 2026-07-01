# SP-CI-FIX-LOOP-CLOSURE — close the fix→re-review→merge loop inside the workflow run

## Metadata

- **Status**: OPEN
- **Priority**: P0 — blocks full spec→ship autonomy (found by the
  first end-to-end traversal, PR #368, 2026-06-12)
- **Owner**: operator
- **Executor**: hand-implemented (touches `.github/workflows/` — bot
  whitelist forbids it)
- **Opened**: 2026-06-12

## Why

The first unassisted traversal proved the chain works until the Coder
pushes: build → PR → review (REQUEST_CHANGES) → Coder fix pushed by
`coder-bot[bot]` — then everything stops. The synchronize event from a
bot-authored push produces workflow runs held at `action_required`
(GitHub first-contributor approval — the fork-PR approve API refuses
them with 403), and even approved, the SP-CI-SECRETS-ISOLATION actor
gate (`github.actor == github.repository_owner`) would skip every job.
The auto_merge comment "a new synchronize event will retrigger the
whole pipeline" is false for bot pushes. Net effect: the convergence
loop silently dies exactly when the factory does its job best.

The robust shape does not fight GitHub's event model: **the run that
pushed the fix re-reviews and merges itself.** The original run's
actor is the owner (the human or the factory pushed the PR head), so
every gate stays intact and the actor allowlist never widens.

## What

All in `.github/workflows/auto-review.yml`:

1. **auto_fix exposes its outcome** — give the "Run Coder auto-fix"
   step an id and emit `pushed=true` to `$GITHUB_OUTPUT` in the
   `rc == 0` branch (`pushed=false` otherwise); declare
   `outputs.pushed` on the job.
2. **In-job re-review after a push** — new step in auto_fix, gated on
   `steps.<coder>.outputs.pushed == 'true'`: run
   `python -m ferova.cli.main review pr <N>` again (same proxy
   sidecar, already running), map exit 0 → `re_verdict=APPROVE_OR_COMMENT`,
   exit 2 → `re_verdict=REQUEST_CHANGES`, else `re_verdict=ERROR`;
   declare `outputs.re_verdict`. This runs review tooling from the PR
   checkout — acceptable inside auto_fix's documented residual trust
   grant (the job executes PR code by design, actor-gated + `bots`
   environment); note it in the step comment.
3. **Release held CI runs on the pushed head** — auto_fix gains
   `actions: write`; after the push, a step polls (≤6 × 10 s) for
   workflow runs on the PR branch with `conclusion == action_required`
   and POSTs `actions/runs/{id}/rerun` on each (CI + ShellCheck must
   go green on the new head for the merge gate). Log every released
   run id; tolerate zero found (synchronize may not even fire).
4. **auto_merge gates on the freshest verdict** — condition becomes:
   `re_verdict == 'APPROVE_OR_COMMENT'` when auto_fix pushed, else
   `needs.review.outputs.verdict == 'APPROVE_OR_COMMENT'`; keep
   `!failure() && !cancelled()` so a skipped auto_fix path still
   merges round-1 approvals, plus the existing actor gate.
5. **Kill the false comment** — rewrite the auto_merge job header
   comment to describe the in-run loop (bot-push synchronize events
   stay held and irrelevant).

## Files in scope

- `.github/workflows/auto-review.yml`

## Out of scope

- Widening the actor allowlist (explicitly rejected — in-run closure
  makes it unnecessary).
- The GitHub console "require approval" policy (moot for the loop;
  operator may still relax it independently).
- ci.yml (no actor gate; its held runs are released by layer 3).
- Multi-round in-run loops — one fix + one re-review per run; a still
  red re-review leaves the PR for the next owner-actored event
  (matches the ≤3-iteration counter which spans runs).

## Smoke scenario

### Setup

A scratch PR whose first review yields REQUEST_CHANGES with a
trivially fixable finding (or replay PR #368 once chains are warm).

### Execute

Let the pipeline run unattended; inspect the single workflow run.

### Expected

One run shows: review (RC) → auto_fix (Coder pushes) → in-job
re-review (APPROVE) → held CI runs released and green → auto_merge
merges. Zero human action between PR-open and merge.

## Definition of Done

- `pushed` and `re_verdict` outputs visible in the run summary.
- Held CI runs on a bot-pushed head are released automatically and
  reach green.
- auto_merge merges on `re_verdict` after a push, on the round-1
  verdict otherwise; never on a stale verdict when a push happened.
- The false synchronize comment is gone.
- actionlint/shellcheck-in-CI green; YAML parses.
- Validated live on one PR end to end (the spec smoke).

## Commit plan

1. `feat(workflow): auto_fix re-reviews in-run after pushing and reports outputs`
2. `feat(workflow): release action_required CI runs on the pushed head`
3. `fix(workflow): auto_merge gates on the freshest verdict, drop the synchronize myth`

## Risks

- **Review tooling from PR code in auto_fix**: within the existing
  residual trust grant of that job (already executes PR code; gated).
- **Rerun API semantics drift**: the release step tolerates failures
  and logs them; the merge gate then skips (loud, not silent).
- **Double review cost per fixed PR**: one extra bench pass — the
  price of convergence.
