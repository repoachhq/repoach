---
id: SP-REFUTED-FEEDBACK
title: Refutations feed the finders — lens track record in prompts
version: 0.1
status: approved
author: jfaye + Claude (review-side evidence sweep, 2026-07-06)
created: 2026-07-06
updated: 2026-07-06

owns:
  code: []
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Refutations feed the finders — lens track record in prompts

## Intent

Close the cross-PR learning loop the refuter leaves open. Scribe has
a 100% refutation rate (25 refuted, 0 verified, ever) and keeps
producing the same docstring claims the AST verifier kills; Architect
repeated the same "make this constant configurable" design claim
across three PRs, refuted each time. The within-PR sentinel loop
exists; across PRs, nothing tells a finder its last N identical
claims were hallucinations.

## Context

`compute_lens_precision` (`src/ferova/review/review_lessons.py:140-171`)
already computes per-lens precision from the ledger but surfaces only
in a CLI insights JSON — no agent consumes it. Refuted findings are
deliberately excluded from builder memory (`review_lessons.py:94-104`)
and review-memory's remember side is seed/manual only. Reviewer
prompts are assembled in `reviewer.py` (code — allowed) from
operator-owned templates under `prompts/review/` (forbidden to bots):
the track record must therefore be APPENDED by the assembly code, not
templated.

## Goals

- G1: A pure `render_lens_track_record(db, role, limit=3) -> str`
  produces a compact section: the lens's ledger precision
  (verified / (verified+refuted)) and its last `limit` refuted
  findings (claim summary + refuter reasoning, truncated), empty
  string when the lens has no refutations.
- G2: The reviewer prompt assembly appends that section to each
  finder's prompt under a fixed heading ("Your recent refuted
  claims — do not re-raise without new evidence"), without touching
  any file under `prompts/review/`.
- G3: The section is capped (e.g. 1 200 chars) so prompt budgets are
  unaffected, and it degrades to the empty string on any DB error
  (never blocks a review).

## Non-Goals

- NG1: No change to the REFUTED terminal state or the state machine.
- NG2: No automatic review-memory writes (a later slice; this one is
  prompt-side only).
- NG3: No change to the refuter itself.

## Assumptions

- A1: Touched files (`review_lessons.py`, `reviewer.py`) are frontier
  or covered; this spec owns nothing.
- A2: Per-lens refutation history is small (74 findings total today);
  a single indexed query per review run is negligible.

## Interface

Inputs:
- `render_lens_track_record(db_path, role, limit=3)` — pure read.

Outputs:
- A bounded text section appended to finder prompts.

Errors: none raised — DB errors log and return "".

## Behavior

### Nominal

Scribe's next review prompt carries: precision 0/25 and its three
most recent refuted docstring claims with the AST verifier's
reasoning — the refute-or-retract rule then applies to its own
history, not just the current PR's threads.

### Edge cases

- Lens with no refutations → empty section, prompt unchanged.
- Reasoning longer than the cap → truncated with an ellipsis.
- DB locked/missing → empty section, one warning log.

### Failure scenarios

- Malformed ledger rows → skipped row-by-row, section built from the
  rest.

## Architecture Impact

- No edge added or removed; no ownership change.

## Diagram

N/A (one query-and-render helper + one append site).

## Acceptance Criteria

- [ ] AC1: `tests/unit/test_review_lessons.py::test_track_record_renders_precision_and_recent_refutations`
  — a seeded ledger yields a section with the precision figure and
  the latest refuted claims, newest first.
- [ ] AC2: `tests/unit/test_review_lessons.py::test_track_record_empty_without_refutations`
  — a clean lens yields "".
- [ ] AC3: `tests/unit/test_review_lessons.py::test_track_record_caps_length`
  — long reasonings truncate under the cap.
- [ ] AC4: `tests/unit/test_reviewer_prompts.py::test_finder_prompt_carries_its_track_record`
  — the assembled finder prompt contains the fixed heading when the
  lens has refutations and omits it otherwise.
- [ ] AC5: The full unit suite passes.

## Open Questions

(none)
