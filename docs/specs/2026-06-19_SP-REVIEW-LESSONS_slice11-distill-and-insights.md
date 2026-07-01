# SP-REVIEW-LESSONS — the review learning loop (slice 11)

**Status:** OPEN
**Redesign slice:** 11 (the final slice of the evidence-first arc — the
learning loop SP-REVIEW-MEMORY explicitly deferred).
**Touches forbidden paths:** no.

## Why

SP-REVIEW-MEMORY shipped the *recall* side: the bench recalls curated trap
lessons before reviewing (`project=review`), and its docstring parked the
*write* side for this slice — "the automatic *remember* stays gated on the
verified findings ledger (slice 11), because learning from unverified
reviewer comments would teach the bench its own hallucinations."

Now that findings carry a verify/refute verdict, slice 11 closes the loop:

- **Short loop (per build):** at a review cycle's end, the findings that
  survived verification — real problems the builder shipped — are
  distilled into **builder-scoped** lessons, so the Planner recalls them
  before the next build.
- **Aggregate loop (insights):** a CLI report over the whole ledger — the
  status / claim-type distribution and a **per-lens precision** metric
  (what fraction of each reviewer's findings that reached a verdict were
  confirmed real vs refuted), surfacing which lenses hallucinate.

Refuted findings are never learned from; they only lower a lens's
precision in the insights view.

## What

### New module `src/ferova/review/review_lessons.py`
- `distill_verified_lessons(db_path, pr_number) -> list[str]` — keep
  findings whose status is downstream of VERIFIED (`_CONFIRMED_REAL` =
  {verified, open, resolved, stuck}); drop refuted / still-proposed; dedupe
  by claim-type + file + claim-prefix; format `"[claim_type] file — claim"`.
- `remember_verified_findings(db_path, pr_number, *, remember_fn=...) -> int`
  — gated on `review_lessons_enabled`; writes each lesson to
  `project=builder` via the agentmemory client (injected for tests;
  degrades gracefully — the client never raises). Returns the count.
- `compute_lens_precision(findings) -> list[LensPrecision]` — per-finder
  `confirmed / (confirmed + refuted)`; lenses with no settled finding are
  omitted. Pure over `list[Finding]`.
- `compute_insights(findings) -> FindingsInsights` — `total`, `by_status`,
  `by_claim_type`, `lens_precision`. Pure.
- `gather_insights(db_path, *, pr_number=None) -> FindingsInsights` — loads
  one PR or the whole ledger, then `compute_insights`.

### `findings.py`
- Add `fetch_all_findings(db_path) -> list[Finding]` (cross-PR read for the
  aggregate report); factor the row→model mapping into `_rows_to_findings`
  shared with `fetch_findings` (no behaviour change).

### `core/config.py`
- Add `review_lessons_enabled: bool = True`
  (`FEROVA_REVIEW_LESSONS_ENABLED`) — the kill-switch for the write side,
  mirroring `review_memory_enabled`.

### `orchestrator.py`
- At cycle end, inside the `if self._post:` block after `_fire_routine`,
  call `remember_verified_findings(self._db_path, pr_number=pr_number)`.
  One line; the function is gated + graceful so it never blocks a run.

### `cli/review_cmds.py`
- New `review insights [PR]` command: read-only, echoes the
  `gather_insights` payload as JSON (status mix, claim-type mix, per-lens
  precision). Omitting the PR aggregates the whole ledger. Always exits 0.

## Acceptance

- Distil keeps only confirmed-real findings, dedupes repeats; refuted /
  proposed never become lessons.
- The write is gated (`review_lessons_enabled=false` → 0, no client call)
  and targets `project=builder`.
- Per-lens precision = confirmed/(confirmed+refuted), ignoring proposed;
  `ferova review insights` emits valid JSON with the metric.
- Full `tests/unit` green; ruff + format + no-inline-comments +
  no-silent-except clean.

## Out of scope

- Synthesising *generalised* lessons from clusters of findings (the raw
  per-finding lesson + agentmemory smart-search recall is the v1).
- A markdown/HTML insights dashboard — JSON is the v1 surface.
- Writing to the `review` scope — verified findings teach the *builder*;
  the bench's own traps stay curated/seeded.
</content>
