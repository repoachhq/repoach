# SP-MERGE-GATE-SHADOW — the pure evidence-first merge gate, in shadow

## Metadata

- **Status**: OPEN
- **Priority**: P1 — review redesign slice 7a of 11
  (docs/review_redesign_architecture.md); the flip itself is 7b
- **Owner**: operator
- **Executor**: `ferova develop`
- **Opened**: 2026-06-14

## Why

Slice 7 is the flip: the merge stops trusting the archive's
self-reported 4/4 verdict (forgeable + stale, audit CRITICAL #1; fed by
the parse_failed promotion, CRITICAL #2) and decides on facts
re-verified at the exact head. But the findings infrastructure is
hours old. Flipping merge safety onto it in one step is reckless. So
this slice builds the pure gate as a tested function and runs it in
**shadow** — `run_auto_merge` computes and logs the evidence-first
decision next to the live 4/4 + CI gate, **without changing what
merges**. Real PRs then show whether the pure gate agrees with the
4/4, and surface the prerequisites for the flip, before slice 7b drops
the archive gate.

## What

1. **New module `src/ferova/review/merge_gate.py`**:
   - `MergeFacts(BaseModel)`: `head_sha`, `ci_green`,
     `open_blocking_findings`, `spec_covered`, `spec_coverage_known`.
   - `MergeDecision(BaseModel)`: `merge`, `reasons`.
   - `compute_merge_decision(facts) -> MergeDecision` — pure: merge iff
     head known ∧ CI green ∧ no open blocking finding ∧ (coverage
     present when recorded).
   - `gather_merge_facts(db_path, *, pr_number, repo_root, head_sha,
     ci_green) -> MergeFacts` — re-verifies the ledger AT HEAD:
     mechanical blocking findings are re-run on disk
     (`verify_finding`), judged blocking findings count only when
     `verified` AND `checked_at_sha == head_sha`, settled
     (resolved/refuted) and advisory findings never count; reads the
     latest `pr_spec_coverage` record.
2. **`src/ferova/review/auto_merge.py`** — `run_auto_merge` gains
   `repo_root` and, just before `squash_merge`, calls a guarded
   `_shadow_pure_gate` that logs `auto_merge.shadow_gate` with
   `shadow_merge` + reasons. Any failure is swallowed — the shadow can
   NEVER affect the live merge.

## Files in scope

- `src/ferova/review/merge_gate.py` (new)
- `src/ferova/review/auto_merge.py`
- `tests/unit/test_merge_gate.py` (new)

## Known prerequisite for the flip (7b)

In CI each job has its own ephemeral `runner.temp` ledger, so the
findings written by the `review` job do NOT reach the `auto_merge`
job — the shadow gate there sees an empty ledger. The shadow is
therefore meaningful only in the **local** flow (`safe_merge.sh` /
`review merge` share `data/ferova.db`). Before 7b can decide on
findings in CI, the ledger must travel between jobs (the architecture
doc's cross-run-continuity question). This slice surfaces that gap;
7b resolves it.

## Out of scope

- Changing what merges (this is shadow; the flip is 7b).
- Executing the spec selectors at merge / re-running the refuter at
  merge (7b, in the trusted job with the proxy).
- Cross-job ledger persistence (its own slice before 7b).

## Smoke scenario

`compute_merge_decision` on all-green facts → merge; flipping each
condition → no-merge with the matching reason. `gather_merge_facts`
on a tmp ledger: a mechanical blocking finding whose symbol now exists
re-verifies away (does not count); a judged finding counts only when
verified + fresh-sha; settled/advisory never count; a non-covered
spec record makes the decision refuse.

## Definition of Done

- Decision: all-green merges, each condition blocks, unknown coverage
  ignored — `test_decision_*`.
- Gather: mechanical re-verify at head, judged fresh-only, settled +
  advisory skipped, coverage read — `test_gather_*`.
- `run_auto_merge` still merges exactly as before (shadow is
  non-intrusive) — existing auto_merge tests stay green.
- `ruff` + `ruff format --check` + `pytest tests/unit` green; zero
  inline comments; no `# noqa`.

## Commit plan

1. `feat(review): pure evidence-first merge gate (decision + fact gathering)`
2. `feat(review): auto_merge runs the pure gate in shadow alongside the 4/4`

## Risks

- **Shadow noise**: a divergence (pure gate says no on a PR the 4/4
  merged) is a log line to investigate, not a failure — exactly the
  signal this slice exists to produce.
- **Empty CI ledger** makes the shadow trivial in CI: documented above;
  the flip is gated on resolving it.
