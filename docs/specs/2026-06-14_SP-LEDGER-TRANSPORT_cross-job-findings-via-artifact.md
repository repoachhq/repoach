# SP-LEDGER-TRANSPORT — carry the findings ledger between CI jobs via a run artifact

## Metadata

- **Status**: OPEN
- **Priority**: P1 — prerequisite #1 for the merge-gate flip (slice 7b),
  surfaced by SP-MERGE-GATE-SHADOW (#387)
- **Owner**: operator
- **Executor**: hand-implemented (`.github/workflows/*` — bot whitelist
  forbids it; force-majeure)
- **Opened**: 2026-06-14

## Why

The pure merge gate (slice 7a, shadow) re-verifies the findings ledger
at head. In CI each job has its own ephemeral
`${{ runner.temp }}/ferova_review.db`, so the findings written by
the `review` job never reach the `auto_merge` job — its gate sees an
empty ledger. The three jobs (`review` → `auto_fix` → `auto_merge`)
share one workflow run, so the standard, secure way to pass a file
between them is a **run-scoped artifact**: written by the trusted
base-ref `review` job (not forgeable by PR code), read by the
owner-gated `auto_merge` job. The pure gate then re-verifies the
transported findings at head — stale or tampered state cannot decide
the merge (mechanical findings re-run on disk; judged findings count
only when fresh at this head).

## What

In `.github/workflows/auto-review.yml`:

1. **review job** — after the review step, `actions/upload-artifact`
   the db as `findings-ledger-<pr>` (`overwrite: true`,
   `if-no-files-found: ignore`).
2. **auto_fix job** — after the in-run re-review, upload the same
   artifact (overwrite) gated on `steps.coder.outputs.pushed ==
   'true'`, so post-fix findings supersede the pre-fix ones.
3. **auto_merge job** — before `Run auto-merge`,
   `actions/download-artifact` `findings-ledger-<pr>` into
   `${{ runner.temp }}` (step-level `continue-on-error: true` so a
   missing artifact leaves an empty db and the merge path stays
   alive). The `review merge` gate then reads the hydrated db.

No Python change: `run_auto_merge` already reads `FEROVA_DB_PATH`, and
`gather_merge_facts` already re-verifies at head.

## Files in scope

- `.github/workflows/auto-review.yml`

## Out of scope

- Deciding on the gate (still shadow until slice 7b).
- Proxy-in-auto_merge for re-judging design/security at head (the
  other 7b prerequisite).
- Any Python / `gh_client` change (artifacts beat the forgeable
  comment-transport the architecture doc floated).

## Smoke scenario

A real PR through the runner's auto-review pipeline. The `review` job
uploads `findings-ledger-<pr>`; the `auto_merge` job downloads it; the
`auto_merge.shadow_gate` log shows non-zero finding awareness
(`spec_coverage_known`, `open_blocking` reflecting the hydrated
ledger) instead of the empty-db default.

## Definition of Done

- All three artifact steps present and YAML-valid.
- One real pipeline run shows the auto_merge shadow gate reading the
  transported ledger (non-empty) rather than an empty db.
- actionlint / YAML clean.

## Commit plan

1. `feat(ci): transport the findings ledger between jobs via a run artifact`

## Risks

- **Stale ledger after a fix**: the post-fix auto_fix upload
  supersedes it, and the gate's head_sha freshness check degrades any
  remaining staleness gracefully (toward permissive — mechanical
  re-verified at head, judged require fresh sha).
- **Artifact write trust**: written by the trusted base-ref review job
  within the run; not reachable by PR-authored code.
