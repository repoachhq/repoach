---
id: SP-DEDUP-CLASSIFICATION-CONSTANTS
title: Deduplicate the three remaining independently-redeclared classification constants
version: 0.1
status: approved
author: agent
created: 2026-07-24
updated: 2026-07-24

owns:
  code:
    - src/repoach/review/merge_gate.py
    - src/repoach/review/coder_findings.py
    - src/repoach/review/review_lessons.py
  resources: N/A

depends_on: [SP-FINDINGS-INIT-RACE, SP-CHAINPILOT-REVIEWER-OUTCOMES, SP-CHAINPILOT-CHAIN-REWRITE, SP-NIM-CHAIN-HEALTH]
provides_to: []

constraints: {}
---

# Deduplicate the three remaining independently-redeclared classification constants

## Intent

Three classification constants — the mechanical-claim-type set, the
confirmed-real finding-status set, and the chain-tier ordering — are
each independently redeclared, byte-identical, in two sibling modules.
The exact drift class this produces was already diagnosed and fixed
once, for a fourth constant (`_JUDGED_TYPES`), by importing a single
shared definition instead of keeping a second hand-copied set. Apply
that same, already-proven pattern to the three remaining duplicates so
a future edit to any one of these taxonomies (adding a `ClaimType`,
adding a `FindingStatus`, adding a tier) cannot silently desync one
copy from its twin.

## Context

Finding #7 (code-debt, medium, effort S). Confirmed live at HEAD
(`develop`, `bc4e4e0`) — this problem is still real and unfixed for
all three named pairs.

- `src/repoach/review/merge_gate.py:41-43` declares
  `_MECHANICAL_TYPES = frozenset({ClaimType.MISSING_TEST,
  ClaimType.MISSING_DOCSTRING, ClaimType.LINT_CONVENTION})`.
  `src/repoach/review/coder_findings.py:60-62` declares the identical
  `_MECHANICAL_TYPES` independently.
- `merge_gate.py:44-56` already owns the *proven fix pattern* for the
  sibling constant on the same lines: `_JUDGED_TYPES =
  JUDGED_CLAIM_TYPES` — a plain alias importing the public
  `JUDGED_CLAIM_TYPES` declared once in `findings.py:58`. Its docstring
  states the exact incident this finding warns is still open for
  `_MECHANICAL_TYPES`: "The gate and the refuter used to keep drifting,
  independently-defined copies of this set — the refuter judged
  `spec_gap` while the gate's copy omitted it, so a refuter-VERIFIED
  blocking `spec_gap` fell through both ... and landed in
  `blocking_unverified` as a misleading 'no verifier' reason."
- `src/repoach/review/reviewer_outcomes.py:37-44` declares
  `_CONFIRMED_REAL: frozenset[FindingStatus] = frozenset({
  FindingStatus.VERIFIED, FindingStatus.OPEN, FindingStatus.RESOLVED,
  FindingStatus.STUCK})`, with its own docstring at line 47 admitting
  "Mirrors slice 11's `review_lessons._CONFIRMED_REAL`."
  `src/repoach/review/review_lessons.py:35-42` declares the identical
  `_CONFIRMED_REAL` independently.
- `src/repoach/review/chain_rewrite.py:30` declares `_TIERS =
  ("opus", "sonnet", "haiku")`. `src/repoach/review/chain_health.py:34`
  declares the identical `_TIERS: tuple[str, ...] = ("opus", "sonnet",
  "haiku")` independently; `chain_health.py` uses it to order
  `check_tier_heads`'s per-tier probe fan-out (lines 202/214/231).
- All seven files above live in `src/repoach/review/`; no
  cross-package import boundary is crossed by any of the moves below
  (`reviewer_outcomes.py`'s own docstring explains it lives in
  `review/` rather than `llm_proxy/providers/` specifically to respect
  the one-way `review -> llm_proxy` boundary — unaffected here, since
  both `_TIERS` owners are already inside `review/`).
- No existing test references any of `_MECHANICAL_TYPES`,
  `_CONFIRMED_REAL`, `_TIERS` by name (confirmed by grep across
  `tests/`), so renaming/relocating them cannot break a pinned test.

## Goals

- G1: `_MECHANICAL_TYPES` is declared exactly once, as a public
  `MECHANICAL_CLAIM_TYPES` constant in `findings.py` (alongside
  `JUDGED_CLAIM_TYPES`, the same taxonomy's other partition).
  `merge_gate.py` and `coder_findings.py` both bind their local
  `_MECHANICAL_TYPES` name to an import of that shared constant rather
  than redeclaring the frozenset.
- G2: `_CONFIRMED_REAL` is declared exactly once, as a public
  `CONFIRMED_REAL_STATUSES` constant in `findings.py` (alongside
  `FindingStatus`, the enum it partitions). `reviewer_outcomes.py` and
  `review_lessons.py` both bind their local `_CONFIRMED_REAL` name to
  an import of that shared constant rather than redeclaring the
  frozenset.
- G3: `_TIERS` is declared exactly once, as a public `CHAIN_TIERS`
  constant in `chain_health.py` (the module already probing per-tier
  chain health). `chain_rewrite.py` binds its local `_TIERS` name to an
  import of that shared constant rather than redeclaring the tuple.
- G4: values are unchanged — this is a pure declaration-site move; the
  three frozensets/tuple contain the exact same members after the
  change as before.

## Non-Goals

- NG1: no behavior change beyond the declaration-site move — no new
  `ClaimType`, `FindingStatus`, or tier is added or removed; no
  call-site logic (`merge_gate.compute_merge_decision`,
  `coder_findings.fetch_open_blocking_findings`,
  `reviewer_outcomes.harvest_reviewer_outcomes`,
  `review_lessons`'s insights aggregation, `chain_health.check_tier_heads`,
  `chain_rewrite`'s slot rewriter) changes its classification outcome
  for any input.
- NG2: no change to `_JUDGED_TYPES` / `JUDGED_CLAIM_TYPES` — that pair
  is already deduplicated (SP-CLAIM-TYPE-PARTITION-ALIGN); this spec
  only touches the three still-duplicated siblings.
- NG3: no new shared module — the two owning modules (`findings.py`,
  `chain_health.py`) already exist and already own the sibling
  constants (`JUDGED_CLAIM_TYPES`, per-tier probing) these three join.
- NG4: no change to `llm_proxy/api/routes.py`'s unrelated
  `_MODEL_ENDPOINT_TIERS` or `llm_proxy/routing/chain_expand.py`'s
  `NIM_HEAD_TIERS` — those are different constants for a different
  purpose (endpoint routing / NIM head selection), not part of this
  finding's named duplicate pairs.

## Interface

`src/repoach/review/findings.py` (new public constants, no signature
changes to existing functions):

```python
MECHANICAL_CLAIM_TYPES = frozenset(
    {ClaimType.MISSING_TEST, ClaimType.MISSING_DOCSTRING, ClaimType.LINT_CONVENTION}
)
"""Claim types resolved by a mechanical on-disk re-check rather than
adversarial judging. The single source of truth (mirrors
JUDGED_CLAIM_TYPES): merge_gate and coder_findings both import this
constant rather than keeping their own copies."""

CONFIRMED_REAL_STATUSES: frozenset[FindingStatus] = frozenset(
    {FindingStatus.VERIFIED, FindingStatus.OPEN, FindingStatus.RESOLVED, FindingStatus.STUCK}
)
"""Statuses downstream of VERIFIED — the finding was confirmed a real
problem. The single source of truth: reviewer_outcomes and
review_lessons both import this constant rather than keeping their own
copies."""
```

`src/repoach/review/merge_gate.py`:
- `_MECHANICAL_TYPES = MECHANICAL_CLAIM_TYPES` (imported from
  `.findings`), replacing the inline `frozenset({...})` literal.

`src/repoach/review/coder_findings.py`:
- `_MECHANICAL_TYPES = MECHANICAL_CLAIM_TYPES` (imported from
  `.findings`), replacing the inline `frozenset({...})` literal.

`src/repoach/review/reviewer_outcomes.py`:
- `_CONFIRMED_REAL = CONFIRMED_REAL_STATUSES` (imported from
  `.findings`), replacing the inline `frozenset({...})` literal.

`src/repoach/review/review_lessons.py`:
- `_CONFIRMED_REAL = CONFIRMED_REAL_STATUSES` (imported from
  `.findings`), replacing the inline `frozenset({...})` literal.

`src/repoach/review/chain_health.py`:
- Rename the module-level `_TIERS` to public `CHAIN_TIERS: tuple[str,
  ...] = ("opus", "sonnet", "haiku")`; update the module's own internal
  uses (`check_tier_heads`, its docstrings referencing ``_TIERS``) to
  the new name.

`src/repoach/review/chain_rewrite.py`:
- `_TIERS = CHAIN_TIERS` (imported from `.chain_health`), replacing the
  inline tuple literal.

## Behavior

### Nominal

- `merge_gate.compute_merge_decision` and
  `coder_findings.fetch_open_blocking_findings` classify a
  `missing_test` / `missing_docstring` / `lint_convention` finding as
  mechanical exactly as before (value unchanged, only the declaration
  site moved).
- `reviewer_outcomes.harvest_reviewer_outcomes` and `review_lessons`'s
  precision aggregation classify a `verified` / `open` / `resolved` /
  `stuck` finding as confirmed-real exactly as before.
- `chain_health.check_tier_heads` probes tiers in `("opus", "sonnet",
  "haiku")` order exactly as before; `chain_rewrite`'s slot rewriter
  iterates the same three tiers in the same order.

### Edge cases

- A future PR adds a new `ClaimType` member and updates
  `MECHANICAL_CLAIM_TYPES` in `findings.py` only — both `merge_gate.py`
  and `coder_findings.py` see the updated set on their next import,
  with no second edit required (this is the property the finding
  demands; AC2 below proves it holds).
- Same for a future `FindingStatus` addition to
  `CONFIRMED_REAL_STATUSES`, and a future tier addition to
  `CHAIN_TIERS`.

### Failure scenarios

- N/A — this is a pure refactor with no new failure mode; the only
  regression risk is a value drift introduced by the move itself,
  which AC1 (value-equality) and AC2 (identity) both guard against.

## Acceptance Criteria

- [ ] AC1: unit — `tests/unit/test_taxonomy_constants_dedup.py::test_mechanical_claim_types_value_unchanged`
  asserts `findings.MECHANICAL_CLAIM_TYPES == frozenset({ClaimType.MISSING_TEST,
  ClaimType.MISSING_DOCSTRING, ClaimType.LINT_CONVENTION})`;
  `::test_confirmed_real_statuses_value_unchanged` asserts
  `findings.CONFIRMED_REAL_STATUSES == frozenset({FindingStatus.VERIFIED,
  FindingStatus.OPEN, FindingStatus.RESOLVED, FindingStatus.STUCK})`;
  `::test_chain_tiers_value_unchanged` asserts
  `chain_health.CHAIN_TIERS == ("opus", "sonnet", "haiku")`.
- [ ] AC2 (discriminating identity test — FAILS on pre-change code):
  `tests/unit/test_taxonomy_constants_dedup.py::test_mechanical_claim_types_deduplicated`
  asserts `merge_gate._MECHANICAL_TYPES is findings.MECHANICAL_CLAIM_TYPES`
  AND `coder_findings._MECHANICAL_TYPES is findings.MECHANICAL_CLAIM_TYPES`;
  `::test_confirmed_real_statuses_deduplicated` asserts
  `reviewer_outcomes._CONFIRMED_REAL is findings.CONFIRMED_REAL_STATUSES`
  AND `review_lessons._CONFIRMED_REAL is findings.CONFIRMED_REAL_STATUSES`;
  `::test_chain_tiers_deduplicated` asserts
  `chain_rewrite._TIERS is chain_health.CHAIN_TIERS`. On pre-change code
  `findings.MECHANICAL_CLAIM_TYPES`, `findings.CONFIRMED_REAL_STATUSES`,
  and `chain_health.CHAIN_TIERS` do not exist at all (each pair is
  today two independently-constructed frozensets/tuple, not an import),
  so this test module fails at collection with `AttributeError` before
  the fix lands, and only passes once each pair is import-bound to the
  same object.
- [ ] AC3: promised tests —
  `tests/unit/test_taxonomy_constants_dedup.py::test_mechanical_claim_types_value_unchanged`,
  `::test_confirmed_real_statuses_value_unchanged`,
  `::test_chain_tiers_value_unchanged`,
  `::test_mechanical_claim_types_deduplicated`,
  `::test_confirmed_real_statuses_deduplicated`,
  `::test_chain_tiers_deduplicated`.
- [ ] AC4: regression — the full existing suites for the seven touched
  modules stay green with no assertion changes:
  `tests/unit/test_merge_gate.py`, `tests/unit/test_coder_findings*.py`,
  any `reviewer_outcomes` / `review_lessons` / `chain_health` /
  `chain_rewrite` unit tests already in `tests/unit/` — none of them
  reference `_MECHANICAL_TYPES`, `_CONFIRMED_REAL`, or `_TIERS` by name
  (confirmed by grep at spec-authoring time), so no rename touches a
  pinned assertion.
- [ ] AC5: `ruff check` + `ruff format --check` + `pytest tests/unit`
  green; zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no
  `# noqa` anywhere in the diff.

## Architecture Impact

- Adds/Removes dependency: `merge_gate.py` and `coder_findings.py`
  gain no NEW import edge (both already import from `.findings`, e.g.
  `JUDGED_CLAIM_TYPES` in `merge_gate.py`, `ClaimType`/`FindingStatus`
  in `coder_findings.py`) — this only adds two more names to an
  existing import line. `reviewer_outcomes.py` and `review_lessons.py`
  both already import `FindingStatus` from `.findings` — same,
  additive-only import-line change. `chain_rewrite.py` gains one new
  intra-package import edge, `chain_rewrite -> chain_health`; both
  already live in `src/repoach/review/`, so no new cross-package
  boundary is introduced (the one-way `review -> llm_proxy` boundary
  `reviewer_outcomes.py`'s docstring protects is untouched).
- New / changed coupling, cycles, or shared state: net REDUCES
  coupling — three duplicated constants collapse to three single
  sources of truth, matching the already-established
  `JUDGED_CLAIM_TYPES` pattern. No new cycle: `chain_rewrite` importing
  from `chain_health` does not create a cycle (`chain_health.py` has no
  import back to `chain_rewrite.py`, confirmed by grep).

## Open Questions

(none)
