# SP-REFUTED-FEEDBACK — Refutations feed the finders — lens track record in prompts

Add a pure render_lens_track_record helper to review_lessons.py that surfaces a lens's verified/refuted precision plus its last N refuted claims (claim + refuter reasoning, truncated), and wire it into Reviewer.review_diff so each finder prompt carries a fixed-heading section listing its own refuted history. The section is bounded (1200 chars), degrades to '' on any DB error, and never touches prompts/review/.

## Step 1 — Add render_lens_track_record helper + unit tests

- **Files**: `src/ferova/review/review_lessons.py`, `tests/unit/test_review_lessons.py`
- **Action**: In src/ferova/review/review_lessons.py add a pure function render_lens_track_record(db_path: Path, role: str, limit: int = 3, *, max_chars: int = 1200) -> str. It opens the ledger via fetch_all_findings, filters to status==REFUTED and finder==role, computes precision = confirmed/(confirmed+refuted) for that lens (using compute_lens_precision on the full ledger), and renders a section under the fixed heading 'Your recent refuted claims — do not re-raise without new evidence' containing the precision figure (e.g. 'precision 0/25 (0.00)') and the latest `limit` refuted findings newest-first as '- <file>: <claim summary> — refuter: <verification_result truncated to ~200 chars>'. Truncate the whole section to max_chars with an ellipsis. Return '' when the lens has no refutations. Wrap the DB access in try/except Exception: log a single warning via _log.warning('review.track_record.db_error', role=role, error=...) and return ''. Skip malformed rows (missing file/claim) row-by-row. Add three unit tests in tests/unit/test_review_lessons.py: test_track_record_renders_precision_and_recent_refutations (seeded ledger with 2 verified + 3 refuted for 'scribe' across PRs; assert heading present, precision '0/3' present, three refuted claims newest-first), test_track_record_empty_without_refutations (clean lens → ''), test_track_record_caps_length (one refuted with a 5KB verification_result → returned string length <= max_chars + small slack, ends with ellipsis).
- **Commit**: `feat(review): render per-lens refuted track record`
- **Done when**: pytest tests/unit/test_review_lessons.py::test_track_record_renders_precision_and_recent_refutations tests/unit/test_review_lessons.py::test_track_record_empty_without_refutations tests/unit/test_review_lessons.py::test_track_record_caps_length passes
- **Unit tests**: `tests/unit/test_review_lessons.py::test_track_record_renders_precision_and_recent_refutations`, `tests/unit/test_review_lessons.py::test_track_record_empty_without_refutations`, `tests/unit/test_review_lessons.py::test_track_record_caps_length`

## Step 2 — Append track record to finder prompts in review_diff

- **Files**: `src/ferova/review/reviewer.py`, `tests/unit/test_reviewer_prompts.py`
- **Action**: In src/ferova/review/reviewer.py, inside Reviewer.review_diff, after the existing prompt = self._render_prompt(...) + extra_prompt_section line, append the track record section: from .review_lessons import render_lens_track_record; track = render_lens_track_record(self._db_path, self.role.value) where self._db_path is a new optional __init__ kw (default None). When track is non-empty, append '\n\n' + track to the prompt before passing to _call_with_retry. Add a __init__ kw db_path: Path | None = None stored as self._db_path. Create tests/unit/test_reviewer_prompts.py with test_finder_prompt_carries_its_track_record: instantiate a Reviewer subclass with a tmp ledger seeded with 2 refuted findings for finder='sentinel', monkeypatch _call_with_retry to capture the prompt arg, call review_diff('diff'), assert the captured prompt contains 'Your recent refuted claims — do not re-raise without new evidence' and the refuted claim text. Add a second case in the same test: a Reviewer with an empty ledger → captured prompt does NOT contain the heading. Use a tiny Reviewer subclass that overrides _call_with_retry to return (ReviewVerdict.APPROVE, 'ok', [], _FailedRunResult(error='')) and overrides _render_prompt to return 'PROMPT'. Do not modify any file under prompts/review/.
- **Commit**: `feat(review): append lens track record to finder prompts`
- **Done when**: pytest tests/unit/test_reviewer_prompts.py::test_finder_prompt_carries_its_track_record passes and pytest tests/unit/test_review_lessons.py passes
- **Unit tests**: `tests/unit/test_reviewer_prompts.py::test_finder_prompt_carries_its_track_record`

## Step 3 — Integration: orchestrator wires db_path into reviewers

- **Files**: `src/ferova/review/orchestrator.py`, `tests/integration/test_review_track_record_integration.py`
- **Action**: In src/ferova/review/orchestrator.py, locate the reviewer construction site (where Reviewer subclasses are instantiated, near line 674 where review_diff is called). Pass db_path=settings.db_path (or the orchestrator's existing db_path) into each Reviewer(...) constructor. Add tests/integration/test_review_track_record_integration.py with test_orchestrator_wires_db_path_to_reviewers: seeds a tmp ledger with 2 refuted findings for 'sentinel', constructs a Sentinel reviewer with db_path=that ledger, monkeypatches _call_with_retry to capture the prompt, calls review_diff('x'), and asserts the captured prompt contains the fixed heading and the refuted claim. Also assert a Scribe reviewer with an empty ledger produces a prompt WITHOUT the heading. This proves the orchestrator wiring end-to-end without touching prompts/review/.
- **Commit**: `feat(review): wire db_path through orchestrator to reviewers`
- **Done when**: pytest tests/integration/test_review_track_record_integration.py::test_orchestrator_wires_db_path_to_reviewers passes
- **Unit tests**: `tests/integration/test_review_track_record_integration.py::test_orchestrator_wires_db_path_to_reviewers`

## Integration tests

- `tests/integration/test_review_track_record_integration.py::test_orchestrator_wires_db_path_to_reviewers`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-REFUTED-FEEDBACK",
  "title": "Refutations feed the finders — lens track record in prompts",
  "summary": "Add a pure render_lens_track_record helper to review_lessons.py that surfaces a lens's verified/refuted precision plus its last N refuted claims (claim + refuter reasoning, truncated), and wire it into Reviewer.review_diff so each finder prompt carries a fixed-heading section listing its own refuted history. The section is bounded (1200 chars), degrades to '' on any DB error, and never touches prompts/review/.",
  "steps": [
    {
      "index": 1,
      "title": "Add render_lens_track_record helper + unit tests",
      "files": [
        "src/ferova/review/review_lessons.py",
        "tests/unit/test_review_lessons.py"
      ],
      "action": "In src/ferova/review/review_lessons.py add a pure function render_lens_track_record(db_path: Path, role: str, limit: int = 3, *, max_chars: int = 1200) -> str. It opens the ledger via fetch_all_findings, filters to status==REFUTED and finder==role, computes precision = confirmed/(confirmed+refuted) for that lens (using compute_lens_precision on the full ledger), and renders a section under the fixed heading 'Your recent refuted claims — do not re-raise without new evidence' containing the precision figure (e.g. 'precision 0/25 (0.00)') and the latest `limit` refuted findings newest-first as '- <file>: <claim summary> — refuter: <verification_result truncated to ~200 chars>'. Truncate the whole section to max_chars with an ellipsis. Return '' when the lens has no refutations. Wrap the DB access in try/except Exception: log a single warning via _log.warning('review.track_record.db_error', role=role, error=...) and return ''. Skip malformed rows (missing file/claim) row-by-row. Add three unit tests in tests/unit/test_review_lessons.py: test_track_record_renders_precision_and_recent_refutations (seeded ledger with 2 verified + 3 refuted for 'scribe' across PRs; assert heading present, precision '0/3' present, three refuted claims newest-first), test_track_record_empty_without_refutations (clean lens → ''), test_track_record_caps_length (one refuted with a 5KB verification_result → returned string length <= max_chars + small slack, ends with ellipsis).",
      "commit_message": "feat(review): render per-lens refuted track record",
      "done_when": "pytest tests/unit/test_review_lessons.py::test_track_record_renders_precision_and_recent_refutations tests/unit/test_review_lessons.py::test_track_record_empty_without_refutations tests/unit/test_review_lessons.py::test_track_record_caps_length passes",
      "unit_tests": [
        "tests/unit/test_review_lessons.py::test_track_record_renders_precision_and_recent_refutations",
        "tests/unit/test_review_lessons.py::test_track_record_empty_without_refutations",
        "tests/unit/test_review_lessons.py::test_track_record_caps_length"
      ]
    },
    {
      "index": 2,
      "title": "Append track record to finder prompts in review_diff",
      "files": [
        "src/ferova/review/reviewer.py",
        "tests/unit/test_reviewer_prompts.py"
      ],
      "action": "In src/ferova/review/reviewer.py, inside Reviewer.review_diff, after the existing prompt = self._render_prompt(...) + extra_prompt_section line, append the track record section: from .review_lessons import render_lens_track_record; track = render_lens_track_record(self._db_path, self.role.value) where self._db_path is a new optional __init__ kw (default None). When track is non-empty, append '\\n\\n' + track to the prompt before passing to _call_with_retry. Add a __init__ kw db_path: Path | None = None stored as self._db_path. Create tests/unit/test_reviewer_prompts.py with test_finder_prompt_carries_its_track_record: instantiate a Reviewer subclass with a tmp ledger seeded with 2 refuted findings for finder='sentinel', monkeypatch _call_with_retry to capture the prompt arg, call review_diff('diff'), assert the captured prompt contains 'Your recent refuted claims — do not re-raise without new evidence' and the refuted claim text. Add a second case in the same test: a Reviewer with an empty ledger → captured prompt does NOT contain the heading. Use a tiny Reviewer subclass that overrides _call_with_retry to return (ReviewVerdict.APPROVE, 'ok', [], _FailedRunResult(error='')) and overrides _render_prompt to return 'PROMPT'. Do not modify any file under prompts/review/.",
      "commit_message": "feat(review): append lens track record to finder prompts",
      "done_when": "pytest tests/unit/test_reviewer_prompts.py::test_finder_prompt_carries_its_track_record passes and pytest tests/unit/test_review_lessons.py passes",
      "unit_tests": [
        "tests/unit/test_reviewer_prompts.py::test_finder_prompt_carries_its_track_record"
      ]
    },
    {
      "index": 3,
      "title": "Integration: orchestrator wires db_path into reviewers",
      "files": [
        "src/ferova/review/orchestrator.py",
        "tests/integration/test_review_track_record_integration.py"
      ],
      "action": "In src/ferova/review/orchestrator.py, locate the reviewer construction site (where Reviewer subclasses are instantiated, near line 674 where review_diff is called). Pass db_path=settings.db_path (or the orchestrator's existing db_path) into each Reviewer(...) constructor. Add tests/integration/test_review_track_record_integration.py with test_orchestrator_wires_db_path_to_reviewers: seeds a tmp ledger with 2 refuted findings for 'sentinel', constructs a Sentinel reviewer with db_path=that ledger, monkeypatches _call_with_retry to capture the prompt, calls review_diff('x'), and asserts the captured prompt contains the fixed heading and the refuted claim. Also assert a Scribe reviewer with an empty ledger produces a prompt WITHOUT the heading. This proves the orchestrator wiring end-to-end without touching prompts/review/.",
      "commit_message": "feat(review): wire db_path through orchestrator to reviewers",
      "done_when": "pytest tests/integration/test_review_track_record_integration.py::test_orchestrator_wires_db_path_to_reviewers passes",
      "unit_tests": [
        "tests/integration/test_review_track_record_integration.py::test_orchestrator_wires_db_path_to_reviewers"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_review_track_record_integration.py::test_orchestrator_wires_db_path_to_reviewers"
  ]
}
```
