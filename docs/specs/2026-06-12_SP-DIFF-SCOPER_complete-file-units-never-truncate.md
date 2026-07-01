# SP-DIFF-SCOPER — the bench reviews complete file units, never a mid-line truncation

## Metadata

- **Status**: OPEN
- **Priority**: P1 — review redesign slice 2 of 11
  (docs/review_redesign_architecture.md)
- **Owner**: operator
- **Executor**: `ferova develop`
- **Opened**: 2026-06-12

## Why

`Reviewer.review_diff` caps the diff with a blind character cut
(`diff[:_DIFF_HARD_CAP_CHARS]`, reviewer.py:600) — the prompt can end
in the middle of a line of code. The documented consequence is the
worst reviewer failure mode on record: convergent hallucination, where
all four bots fabricate identical false blockers about the code they
cannot see. The redesign principle: **never review a truncated diff**
— a reviewer either sees a complete file unit or is told, precisely,
that the file is not visible.

This slice keeps one review call per reviewer (the per-slice fan-out
and the coverage gate are slices 3 and 6); it only replaces the blind
cut with whole-file scoping plus an explicit omission announcement.

## What

1. **New module `src/ferova/review/diff_scoper.py`**, pure
   functions, no I/O:
   - `FileDiff` (pydantic BaseModel): `path: str`, `text: str` (the
     complete `diff --git` unit including its header lines),
     `chars: int`.
   - `split_diff(diff: str) -> list[FileDiff]` — split on lines
     starting with `diff --git `; `path` parsed from the
     `b/<path>` side; tolerate a leading preamble before the first
     marker (attach it to the first unit) and an empty diff (`[]`).
   - `ScopedDiff` (pydantic BaseModel): `prompt_diff: str`,
     `included: list[str]`, `omitted: list[str]`,
     `oversized: list[str]` (single units larger than the cap —
     always omitted whole, never cut).
   - `scope_diff(diff: str, cap_chars: int) -> ScopedDiff` — greedy
     in original order: append complete units while the running total
     stays under `cap_chars`; a unit that does not fit is omitted
     (oversized ones also recorded in `oversized`). When anything is
     omitted, `prompt_diff` ends with the announcement block:
     ```
     [diff scoped: <I> of <T> files shown COMPLETE.
      NOT visible (do not guess about them): <p1>, <p2>, …
      Review ONLY the code you can see.]
     ```
     When everything fits, `prompt_diff` is the input unchanged and
     the lists reflect full inclusion.
2. **Wiring in `src/ferova/review/reviewer.py`** — replace the
   four lines of the blind cut in `review_diff` (lines 600-605 area)
   with a call to `scope_diff(diff, _DIFF_HARD_CAP_CHARS)`; use
   `scoped.prompt_diff` where `truncated_diff` was used; when
   `scoped.omitted` is non-empty emit
   `_log.warning("review.diff_scoped", role=self.role.value,
   n_included=len(scoped.included), n_omitted=len(scoped.omitted),
   omitted=scoped.omitted[:10])`. Add the import next to the other
   `.diff_scoper` — i.e.
   `from .diff_scoper import scope_diff` beside the existing
   relative imports. NOTHING else in reviewer.py changes (the
   `{SPEC_PLAN}` cap at line ~695 stays as is).

Required imports for the new module (verified — copy, do not
improvise): `from pydantic import BaseModel`. Nothing else is needed.

## Files in scope

- `src/ferova/review/diff_scoper.py` (new)
- `tests/unit/test_diff_scoper.py` (new)
- `src/ferova/review/reviewer.py` (wiring only)

## Plan-shaping constraints

- Step 1 contracts ONLY the two NEW files (module + its tests).
- Step 2 contracts `reviewer.py` (the single big existing file of its
  step — nothing else big) plus `tests/unit/test_diff_scoper.py` for
  its promised wiring tests, re-emitting that test file complete.
- Two steps maximum.

## Out of scope

- Per-slice fan-out of finders, coverage matrix, coverage gate
  (slices 3 and 6).
- The `{SPEC_PLAN}` block cap in `_render_prompt`.
- Changing `_DIFF_HARD_CAP_CHARS`.
- `hallucination_guard.py`, `orchestrator.py`.

## Smoke scenario

### Setup

A synthetic diff of three realistic `diff --git` units (header +
index + ---/+++ + one hunk each; a realistic unit is 110-130 chars —
measure, do not assume).

### Execute

`scope_diff` twice with caps **computed from the measured fixture**:
`cap_one = units[0].chars + 10` (fits exactly one unit) and
`cap_all = sum(u.chars for u in units) + 100` (fits everything); then
`Reviewer.review_diff` on a >`_DIFF_HARD_CAP_CHARS` diff with a
stubbed `_call_with_retry` capturing the prompt.

### Expected

`cap_one`: exactly one unit included, two omitted, the announcement
names both omitted paths, no unit cut mid-text. `cap_all`: input
unchanged, no announcement. The captured reviewer prompt contains the
announcement block and zero half-units.

### Test-arithmetic law (post-mortem of the first dispatch)

Every size threshold in the tests MUST be derived from the measured
fixture (`cap = unit.chars + margin`) — never a hardcoded number.
The first dispatch died twice because a prescribed "cap=80" could
never fit a realistic 117-char unit: the assertions were impossible
regardless of the implementation. Also pin preamble semantics
explicitly: text before the first `diff --git` marker must be
PRESERVED at the start of the first unit's ``text`` (the natural bug
is overwriting the accumulator when the first marker arrives — test
for the preamble substring in ``units[0].text``).

## Definition of Done

- Units split correctly incl. preamble and empty input —
  `test_split_diff_units`, `test_split_diff_empty`,
  `test_split_diff_preamble_attached`.
- Greedy packing never cuts a unit; omitted + oversized recorded —
  `test_scope_diff_omits_whole_units`,
  `test_scope_diff_oversized_unit_never_cut`.
- Under-cap input passes through byte-identical —
  `test_scope_diff_passthrough_when_under_cap`.
- Announcement lists every omitted path —
  `test_scope_diff_announcement_lists_omitted`.
- Wiring: prompt carries the announcement, `review.diff_scoped`
  warning emitted, no `[... diff truncated` legacy marker remains in
  reviewer.py — `test_review_diff_uses_scoper`,
  `test_review_diff_logs_scoping`.
- `ruff` + `ruff format --check` + `pytest tests/unit` green; zero
  inline comments; no `# noqa`.

## Commit plan

1. `feat(review): diff scoper — complete file units with explicit omission`
2. `feat(review): review_diff scopes instead of truncating mid-line`

## Risks

- **Reviewers see fewer files on huge PRs than the old cut** (whole
  units instead of a partial extra file): intended — a complete view
  of less beats a corrupted view of more, and the omission is
  announced instead of silent.
- **reviewer.py full-file re-emission (1 718 lines)** in step 2: the
  single-big-file-per-step rule applies; if the dispatch trips on
  output truncation, root-cause protocol — do not hand-ship before
  the autopsy.
