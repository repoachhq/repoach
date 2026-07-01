# SP-DIFF-SCOPER — Diff scoper — whole-file units with explicit omission announcement

Replace the blind mid-line character cut in Reviewer.review_diff with a two-step solution: first, create a pure-function module diff_scoper.py that splits a raw diff into complete FileDiff units and greedily packs them under a char cap while recording omissions and oversized files; second, wire scope_diff into reviewer.py so the prompt always carries either a complete view of every selected file or an explicit announcement naming everything left out, eliminating the convergent-hallucination failure mode caused by truncated half-units.

## Step 1 — Add diff_scoper module with FileDiff, ScopedDiff, split_diff, scope_diff

- **Files**: `src/ferova/review/diff_scoper.py`, `tests/unit/test_diff_scoper.py`
- **Action**: Create src/ferova/review/diff_scoper.py with: (a) FileDiff(BaseModel) fields path:str, text:str, chars:int; (b) split_diff(diff:str)->list[FileDiff] splitting on lines starting with 'diff --git ', parsing path from the 'b/<path>' side of the header, attaching any preamble text before the first marker to the first unit's text, returning [] for empty input; (c) ScopedDiff(BaseModel) fields prompt_diff:str, included:list[str], omitted:list[str], oversized:list[str]; (d) scope_diff(diff:str, cap_chars:int)->ScopedDiff that greedily appends complete FileDiff units in original order while the running total stays under cap_chars, records oversized single-unit diffs (chars>cap_chars) in both omitted and oversized, appends the announcement block when anything is omitted, and returns the input unchanged with full inclusion lists when everything fits. Only import: from pydantic import BaseModel. Create tests/unit/test_diff_scoper.py with a realistic three-unit fixture where each unit is a real diff --git block (header + index + ---/+++ + one hunk); measure unit chars from the fixture, compute cap_one=units[0].chars+10 and cap_all=sum(u.chars for u in units)+100; implement test_split_diff_units (three units split correctly), test_split_diff_empty (empty string returns []), test_split_diff_preamble_attached (preamble text present in units[0].text), test_scope_diff_omits_whole_units (cap_one: one included, two omitted, no unit cut mid-text), test_scope_diff_oversized_unit_never_cut (cap smaller than any single unit: all land in oversized, prompt_diff is empty or announcement-only), test_scope_diff_passthrough_when_under_cap (cap_all: prompt_diff equals input exactly, omitted=[], oversized=[]), test_scope_diff_announcement_lists_omitted (cap_one: announcement in prompt_diff names both omitted paths).
- **Commit**: `feat(review): diff scoper — complete file units with explicit omission`
- **Done when**: pytest tests/unit/test_diff_scoper.py::test_split_diff_units tests/unit/test_diff_scoper.py::test_split_diff_empty tests/unit/test_diff_scoper.py::test_split_diff_preamble_attached tests/unit/test_diff_scoper.py::test_scope_diff_omits_whole_units tests/unit/test_diff_scoper.py::test_scope_diff_oversized_unit_never_cut tests/unit/test_diff_scoper.py::test_scope_diff_passthrough_when_under_cap tests/unit/test_diff_scoper.py::test_scope_diff_announcement_lists_omitted all pass and ruff check src/ferova/review/diff_scoper.py exits 0
- **Unit tests**: `tests/unit/test_diff_scoper.py::test_split_diff_units`, `tests/unit/test_diff_scoper.py::test_split_diff_empty`, `tests/unit/test_diff_scoper.py::test_split_diff_preamble_attached`, `tests/unit/test_diff_scoper.py::test_scope_diff_omits_whole_units`, `tests/unit/test_diff_scoper.py::test_scope_diff_oversized_unit_never_cut`, `tests/unit/test_diff_scoper.py::test_scope_diff_passthrough_when_under_cap`, `tests/unit/test_diff_scoper.py::test_scope_diff_announcement_lists_omitted`

## Step 2 — Wire scope_diff into reviewer.py, replacing blind truncation

- **Files**: `src/ferova/review/reviewer.py`, `tests/unit/test_diff_scoper.py`, `tests/integration/test_diff_scoper_integration.py`
- **Action**: In src/ferova/review/reviewer.py: add 'from .diff_scoper import scope_diff' next to the existing relative imports near the top of the file. In the review_diff method (around lines 600-605), remove the four lines that assign truncated_diff via diff[:_DIFF_HARD_CAP_CHARS] and append the '[... diff truncated ...]' suffix. Replace them with: scoped = scope_diff(diff, _DIFF_HARD_CAP_CHARS); if scoped.omitted: _log.warning('review.diff_scoped', role=self.role.value, n_included=len(scoped.included), n_omitted=len(scoped.omitted), omitted=scoped.omitted[:10]). Replace every subsequent use of truncated_diff in that method with scoped.prompt_diff. Nothing else in reviewer.py changes. Re-emit tests/unit/test_diff_scoper.py in full, adding at the end: test_review_diff_uses_scoper (build an Architect with a stubbed _call_with_retry; construct a diff whose length exceeds _DIFF_HARD_CAP_CHARS by adding enough file units; call review_diff; assert the captured prompt argument contains the announcement block '[diff scoped:' and does not contain '[... diff truncated'); test_review_diff_logs_scoping (same stub; assert _log.warning was called with event='review.diff_scoped' and n_omitted>0 when the diff is oversized). Create tests/integration/test_diff_scoper_integration.py: one test that imports scope_diff from ferova.review.diff_scoper, calls split_diff on a three-unit synthetic diff, then calls scope_diff with cap=units[0].chars+10, and asserts len(scoped.included)==1 and len(scoped.omitted)==2 end-to-end.
- **Commit**: `feat(review): review_diff scopes instead of truncating mid-line`
- **Done when**: pytest tests/unit/test_diff_scoper.py::test_review_diff_uses_scoper tests/unit/test_diff_scoper.py::test_review_diff_logs_scoping tests/integration/test_diff_scoper_integration.py all pass; grep -n 'diff truncated' src/ferova/review/reviewer.py exits non-zero (no legacy marker remains); ruff check src/ferova/review/reviewer.py exits 0
- **Unit tests**: `tests/unit/test_diff_scoper.py::test_review_diff_uses_scoper`, `tests/unit/test_diff_scoper.py::test_review_diff_logs_scoping`

## Integration tests

- `tests/integration/test_diff_scoper_integration.py`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-DIFF-SCOPER",
  "title": "Diff scoper — whole-file units with explicit omission announcement",
  "summary": "Replace the blind mid-line character cut in Reviewer.review_diff with a two-step solution: first, create a pure-function module diff_scoper.py that splits a raw diff into complete FileDiff units and greedily packs them under a char cap while recording omissions and oversized files; second, wire scope_diff into reviewer.py so the prompt always carries either a complete view of every selected file or an explicit announcement naming everything left out, eliminating the convergent-hallucination failure mode caused by truncated half-units.",
  "steps": [
    {
      "index": 1,
      "title": "Add diff_scoper module with FileDiff, ScopedDiff, split_diff, scope_diff",
      "files": [
        "src/ferova/review/diff_scoper.py",
        "tests/unit/test_diff_scoper.py"
      ],
      "action": "Create src/ferova/review/diff_scoper.py with: (a) FileDiff(BaseModel) fields path:str, text:str, chars:int; (b) split_diff(diff:str)->list[FileDiff] splitting on lines starting with 'diff --git ', parsing path from the 'b/<path>' side of the header, attaching any preamble text before the first marker to the first unit's text, returning [] for empty input; (c) ScopedDiff(BaseModel) fields prompt_diff:str, included:list[str], omitted:list[str], oversized:list[str]; (d) scope_diff(diff:str, cap_chars:int)->ScopedDiff that greedily appends complete FileDiff units in original order while the running total stays under cap_chars, records oversized single-unit diffs (chars>cap_chars) in both omitted and oversized, appends the announcement block when anything is omitted, and returns the input unchanged with full inclusion lists when everything fits. Only import: from pydantic import BaseModel. Create tests/unit/test_diff_scoper.py with a realistic three-unit fixture where each unit is a real diff --git block (header + index + ---/+++ + one hunk); measure unit chars from the fixture, compute cap_one=units[0].chars+10 and cap_all=sum(u.chars for u in units)+100; implement test_split_diff_units (three units split correctly), test_split_diff_empty (empty string returns []), test_split_diff_preamble_attached (preamble text present in units[0].text), test_scope_diff_omits_whole_units (cap_one: one included, two omitted, no unit cut mid-text), test_scope_diff_oversized_unit_never_cut (cap smaller than any single unit: all land in oversized, prompt_diff is empty or announcement-only), test_scope_diff_passthrough_when_under_cap (cap_all: prompt_diff equals input exactly, omitted=[], oversized=[]), test_scope_diff_announcement_lists_omitted (cap_one: announcement in prompt_diff names both omitted paths).",
      "commit_message": "feat(review): diff scoper — complete file units with explicit omission",
      "done_when": "pytest tests/unit/test_diff_scoper.py::test_split_diff_units tests/unit/test_diff_scoper.py::test_split_diff_empty tests/unit/test_diff_scoper.py::test_split_diff_preamble_attached tests/unit/test_diff_scoper.py::test_scope_diff_omits_whole_units tests/unit/test_diff_scoper.py::test_scope_diff_oversized_unit_never_cut tests/unit/test_diff_scoper.py::test_scope_diff_passthrough_when_under_cap tests/unit/test_diff_scoper.py::test_scope_diff_announcement_lists_omitted all pass and ruff check src/ferova/review/diff_scoper.py exits 0",
      "unit_tests": [
        "tests/unit/test_diff_scoper.py::test_split_diff_units",
        "tests/unit/test_diff_scoper.py::test_split_diff_empty",
        "tests/unit/test_diff_scoper.py::test_split_diff_preamble_attached",
        "tests/unit/test_diff_scoper.py::test_scope_diff_omits_whole_units",
        "tests/unit/test_diff_scoper.py::test_scope_diff_oversized_unit_never_cut",
        "tests/unit/test_diff_scoper.py::test_scope_diff_passthrough_when_under_cap",
        "tests/unit/test_diff_scoper.py::test_scope_diff_announcement_lists_omitted"
      ]
    },
    {
      "index": 2,
      "title": "Wire scope_diff into reviewer.py, replacing blind truncation",
      "files": [
        "src/ferova/review/reviewer.py",
        "tests/unit/test_diff_scoper.py",
        "tests/integration/test_diff_scoper_integration.py"
      ],
      "action": "In src/ferova/review/reviewer.py: add 'from .diff_scoper import scope_diff' next to the existing relative imports near the top of the file. In the review_diff method (around lines 600-605), remove the four lines that assign truncated_diff via diff[:_DIFF_HARD_CAP_CHARS] and append the '[... diff truncated ...]' suffix. Replace them with: scoped = scope_diff(diff, _DIFF_HARD_CAP_CHARS); if scoped.omitted: _log.warning('review.diff_scoped', role=self.role.value, n_included=len(scoped.included), n_omitted=len(scoped.omitted), omitted=scoped.omitted[:10]). Replace every subsequent use of truncated_diff in that method with scoped.prompt_diff. Nothing else in reviewer.py changes. Re-emit tests/unit/test_diff_scoper.py in full, adding at the end: test_review_diff_uses_scoper (build an Architect with a stubbed _call_with_retry; construct a diff whose length exceeds _DIFF_HARD_CAP_CHARS by adding enough file units; call review_diff; assert the captured prompt argument contains the announcement block '[diff scoped:' and does not contain '[... diff truncated'); test_review_diff_logs_scoping (same stub; assert _log.warning was called with event='review.diff_scoped' and n_omitted>0 when the diff is oversized). Create tests/integration/test_diff_scoper_integration.py: one test that imports scope_diff from ferova.review.diff_scoper, calls split_diff on a three-unit synthetic diff, then calls scope_diff with cap=units[0].chars+10, and asserts len(scoped.included)==1 and len(scoped.omitted)==2 end-to-end.",
      "commit_message": "feat(review): review_diff scopes instead of truncating mid-line",
      "done_when": "pytest tests/unit/test_diff_scoper.py::test_review_diff_uses_scoper tests/unit/test_diff_scoper.py::test_review_diff_logs_scoping tests/integration/test_diff_scoper_integration.py all pass; grep -n 'diff truncated' src/ferova/review/reviewer.py exits non-zero (no legacy marker remains); ruff check src/ferova/review/reviewer.py exits 0",
      "unit_tests": [
        "tests/unit/test_diff_scoper.py::test_review_diff_uses_scoper",
        "tests/unit/test_diff_scoper.py::test_review_diff_logs_scoping"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_diff_scoper_integration.py"
  ]
}
```
