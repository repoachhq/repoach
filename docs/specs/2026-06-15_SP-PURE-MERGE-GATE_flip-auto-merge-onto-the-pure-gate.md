# SP-PURE-MERGE-GATE — flip auto_merge onto the pure evidence-first gate

## Metadata

- **Status**: OPEN
- **Priority**: P1 — review redesign slice 7b of 11
  (docs/review_redesign_architecture.md); the flip itself
- **Owner**: operator
- **Executor**: hand-implemented (touches `auto_merge` merge safety —
  above the autonomous Developer's risk envelope)
- **Opened**: 2026-06-15

## Why

Slice 7a built the pure gate (`compute_merge_decision` +
`gather_merge_facts`) and ran it in shadow next to the live 4/4 archive
gate. The shadow backtest over PRs #378–#389 reached **8/8 agreement**
with the 4/4 gate (after SP-VERIFIER-DOCSTRING-EXEMPT fixed the one
divergence — the docstring verifier counting undocumented test
functions). The prerequisites are met: the ledger now travels between
jobs (SP-LEDGER-TRANSPORT) and blocking findings are re-judged fresh at
head. So this slice **flips**: `auto_merge` stops gating on the archive
verdict and decides on the pure gate.

This closes two audit CRITICALs by construction:

- **CRITICAL #1 (forgeable archive verdict)** — the archive's
  self-reported 4/4 is no longer a merge gate. It is read for report
  context only. Forging it changes nothing.
- **CRITICAL #2 (parse_failed → APPROVE promote)** — an unparsed
  reviewer no longer promotes a PR. The merge requires a
  review-integrity fact recorded at the exact head: every reviewer
  parsed, zero unparsed. A parse_failed review fails that fact.

## What

1. **`src/ferova/review/findings.py`** — new `pr_review_integrity`
   table + `record_review_integrity(db_path, *, pr_number, head_sha,
   n_reviewers, n_unparsed)` and `fetch_review_integrity(db_path,
   pr_number) -> list[dict]`. `init_findings_schema` creates both the
   findings and the integrity tables.
2. **`src/ferova/review/orchestrator.py`** — after recording
   findings, record the review-integrity fact for the run:
   `n_reviewers = len(outcomes)`, `n_unparsed = sum(1 for o in outcomes
   if _is_unparsed(o))`, at the run's `head_sha`.
3. **`src/ferova/review/merge_gate.py`** — `MergeFacts` gains
   `review_complete` + `review_integrity_known`.
   `compute_merge_decision` refuses when no integrity record exists at
   head, or the review is incomplete (unparsed > 0 or fewer than the
   bench's four reviewers). `gather_merge_facts` derives those from the
   integrity records fresh at `head_sha`, and now calls
   `init_findings_schema` before `fetch_findings` so a PR with no
   findings recorded does not crash on a missing table.
4. **`src/ferova/review/auto_merge.py`** — the flip:
   - Remove the archive-verdict gate (the
     `verdict != "APPROVE" -> SKIP_NOT_APPROVED` block) and the
     `_shadow_pure_gate` helper. Keep `fetch_archive_comment` +
     `parse_archive_verdict` for report context only.
   - After the base + idempotency + CI gates, compute
     `gather_merge_facts` at `gh.pr_head_sha(pr_number)` and decide on
     `compute_merge_decision`. A refused decision persists the new
     `OUTCOME_SKIP_GATE` with the failing reasons; a permitted decision
     proceeds to `squash_merge`.
5. **`src/ferova/cli/review_cmds.py`** — map `OUTCOME_SKIP_GATE`
   into the skip bucket (exit code 5). `OUTCOME_SKIP_NOT_APPROVED`
   stays defined for historical L4 back-compat.

## Files in scope

- `src/ferova/review/findings.py`
- `src/ferova/review/orchestrator.py`
- `src/ferova/review/merge_gate.py`
- `src/ferova/review/auto_merge.py`
- `src/ferova/cli/review_cmds.py`
- `tests/unit/test_merge_gate.py`
- `tests/unit/test_review_auto_merge.py`

## Out of scope

- Executing the spec acceptance selectors at merge / re-running the
  refuter at merge — the orchestrator already re-reviews and records
  fresh facts; the gate consumes them.
- The reviewer/coder findings rewiring of later slices (8–11).

## Smoke scenario

- `compute_merge_decision`: all-green facts → merge;
  `review_integrity_known=False` → no-merge; `review_complete=False` →
  no-merge; each pre-existing condition still blocks.
- `gather_merge_facts`: an integrity record fresh at head with 4
  reviewers + 0 unparsed → `review_complete`; 2 unparsed → not
  complete; a record at a stale head → integrity unknown at this head.
- `run_auto_merge`: no integrity record → `SKIP_GATE`; an unparsed
  reviewer → `SKIP_GATE`; a complete record + green CI + no blocking
  findings → `APPROVE` (merged); merge command failure → `FAILED`.

## Definition of Done

- The five flipped/added `test_merge_gate` cases + the rewritten
  `test_review_auto_merge` cases green.
- The merge no longer depends on the archive verdict anywhere in
  `run_auto_merge`'s control flow.
- `ruff` + `ruff format --check` + `pytest tests/unit` green; zero
  inline comments; no `# noqa`.

## Commit plan

1. `feat(review): review-integrity fact + record it in the orchestrator`
2. `feat(review): merge_gate requires a fresh complete review at head`
3. `feat(review): flip auto_merge onto the pure gate (drop archive verdict)`

## Risks

- **A real PR whose review legitimately ran but recorded < 4 reviewers**
  (e.g. a transport drop on one bot) now refuses to merge where the old
  gate's nit-only auto-promote would have merged. This is the intended
  tightening — a degraded review is no longer a merge. The operator
  re-runs the bench rather than overriding.
- **Cross-job ledger freshness**: the integrity + findings the gate
  reads must be the ones written by the review job at this head
  (SP-LEDGER-TRANSPORT). A stale-head record is treated as
  integrity-unknown and blocks — fail-closed.
