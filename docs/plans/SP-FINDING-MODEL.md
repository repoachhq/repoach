# SP-FINDING-MODEL — Finding model, lifecycle law, and pr_findings ledger

Create the new `src/ferova/review/findings.py` module — a fully self-contained addition that introduces the ClaimType / Severity / FindingStatus enums, the ALLOWED_TRANSITIONS lifecycle constant with its `is_valid_transition` guard, the `Finding` Pydantic model, and the SQLAlchemy Core `pr_findings` ledger (init_findings_schema, record_finding, update_finding_status, fetch_findings). No existing file is touched. Two steps: step 1 ships the enums, lifecycle law, and model with their tests; step 2 re-emits both files grown with the full ledger layer and its tests.

## Step 1 — Finding model, claim taxonomy and lifecycle law

- **Files**: `src/ferova/review/findings.py`, `tests/unit/test_review_findings.py`
- **Action**: Create `src/ferova/review/findings.py` with: (a) module docstring stating the redesign principle; (b) ClaimType(str, Enum) with values missing_test, missing_docstring, lint_convention, broken_behavior, spec_gap, design, security; (c) Severity(str, Enum) with blocking, advisory; (d) FindingStatus(str, Enum) with proposed, verified, refuted, open, resolved, stuck; (e) module constant ALLOWED_TRANSITIONS: dict[FindingStatus, frozenset[FindingStatus]] encoding proposed→{verified,refuted}, verified→{open}, open→{resolved,stuck}, refuted/resolved/stuck→{}; (f) function is_valid_transition(src: FindingStatus, dst: FindingStatus) -> bool; (g) Pydantic BaseModel Finding with all fields from the spec (pr_number, head_sha, round, finder, claim_type, severity, file, line_start, line_end, claim, evidence_pointer, status=FindingStatus.PROPOSED, verification_method='', verification_result='', checked_at_sha='', id: int | None = None). Use required imports: `from enum import Enum`, `from pathlib import Path`, `from pydantic import BaseModel`, `from ..core.logging import get_logger`. Create `tests/unit/test_review_findings.py` with: test_allowed_transitions_law (verifies each entry in ALLOWED_TRANSITIONS matches the spec diagram), test_terminal_states_have_no_exits (refuted/resolved/stuck map to empty frozensets), test_is_valid_transition_accepts_legal_move, test_is_valid_transition_rejects_illegal_move, test_finding_default_status_is_proposed, test_finding_requires_claim_fields (construction with all required fields).
- **Commit**: `feat(review): Finding model, claim taxonomy and lifecycle law`
- **Done when**: pytest tests/unit/test_review_findings.py::test_allowed_transitions_law tests/unit/test_review_findings.py::test_terminal_states_have_no_exits tests/unit/test_review_findings.py::test_is_valid_transition_accepts_legal_move tests/unit/test_review_findings.py::test_is_valid_transition_rejects_illegal_move tests/unit/test_review_findings.py::test_finding_default_status_is_proposed passes and ruff check src/ferova/review/findings.py exits 0
- **Unit tests**: `tests/unit/test_review_findings.py::test_allowed_transitions_law`, `tests/unit/test_review_findings.py::test_terminal_states_have_no_exits`, `tests/unit/test_review_findings.py::test_is_valid_transition_accepts_legal_move`, `tests/unit/test_review_findings.py::test_is_valid_transition_rejects_illegal_move`, `tests/unit/test_review_findings.py::test_finding_default_status_is_proposed`

## Step 2 — pr_findings ledger — record, transition-guarded update, fetch

- **Files**: `src/ferova/review/findings.py`, `tests/unit/test_review_findings.py`
- **Action**: Re-emit the full `src/ferova/review/findings.py` grown with: module-private `_metadata = MetaData()`, `_engine_for(db_path: Path)` that calls `db_path.parent.mkdir(parents=True, exist_ok=True)` then `create_engine(f'sqlite:///{db_path}')`, a `pr_findings` Table with one Integer primary-key autoincrement `id` column and one String column per remaining Finding field (enums stored as String values), `init_findings_schema(db_path: Path) -> None` calling `_metadata.create_all(engine, checkfirst=True)`, `record_finding(db_path: Path, finding: Finding) -> int` that inserts and returns the new row id, `update_finding_status(db_path: Path, finding_id: int, new_status: FindingStatus, *, verification_method: str = '', verification_result: str = '', checked_at_sha: str = '') -> bool` that reads current status, calls is_valid_transition, emits a `findings.invalid_transition` structlog warning and returns False on rejection, otherwise updates and returns True, `fetch_findings(db_path: Path, pr_number: int, *, status: FindingStatus | None = None) -> list[Finding]` ordered by id. Add to sqlalchemy imports: `select`. Re-emit the full `tests/unit/test_review_findings.py` grown with: test_init_findings_schema_idempotent, test_record_and_fetch_finding_round_trip (insert → fetch, verify all scalar fields match), test_legal_transition_updates (proposed→verified returns True, row reads verified), test_illegal_transition_refused (verified→resolved returns False, row still reads verified, warning logged via caplog or structlog capture), test_fetch_findings_filters_by_status (insert proposed + verified finding, filter by each status returns only that one).
- **Commit**: `feat(review): pr_findings ledger — record, transition-guarded update, fetch`
- **Done when**: pytest tests/unit/test_review_findings.py passes (all tests including test_record_and_fetch_finding_round_trip, test_legal_transition_updates, test_illegal_transition_refused, test_fetch_findings_filters_by_status, test_init_findings_schema_idempotent) and ruff check src/ferova/review/findings.py exits 0 and ruff format --check src/ferova/review/findings.py exits 0
- **Unit tests**: `tests/unit/test_review_findings.py::test_init_findings_schema_idempotent`, `tests/unit/test_review_findings.py::test_record_and_fetch_finding_round_trip`, `tests/unit/test_review_findings.py::test_legal_transition_updates`, `tests/unit/test_review_findings.py::test_illegal_transition_refused`, `tests/unit/test_review_findings.py::test_fetch_findings_filters_by_status`

## Integration tests

- `tests/unit/test_review_findings.py::test_record_and_fetch_finding_round_trip`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-FINDING-MODEL",
  "title": "Finding model, lifecycle law, and pr_findings ledger",
  "summary": "Create the new `src/ferova/review/findings.py` module — a fully self-contained addition that introduces the ClaimType / Severity / FindingStatus enums, the ALLOWED_TRANSITIONS lifecycle constant with its `is_valid_transition` guard, the `Finding` Pydantic model, and the SQLAlchemy Core `pr_findings` ledger (init_findings_schema, record_finding, update_finding_status, fetch_findings). No existing file is touched. Two steps: step 1 ships the enums, lifecycle law, and model with their tests; step 2 re-emits both files grown with the full ledger layer and its tests.",
  "steps": [
    {
      "index": 1,
      "title": "Finding model, claim taxonomy and lifecycle law",
      "files": [
        "src/ferova/review/findings.py",
        "tests/unit/test_review_findings.py"
      ],
      "action": "Create `src/ferova/review/findings.py` with: (a) module docstring stating the redesign principle; (b) ClaimType(str, Enum) with values missing_test, missing_docstring, lint_convention, broken_behavior, spec_gap, design, security; (c) Severity(str, Enum) with blocking, advisory; (d) FindingStatus(str, Enum) with proposed, verified, refuted, open, resolved, stuck; (e) module constant ALLOWED_TRANSITIONS: dict[FindingStatus, frozenset[FindingStatus]] encoding proposed→{verified,refuted}, verified→{open}, open→{resolved,stuck}, refuted/resolved/stuck→{}; (f) function is_valid_transition(src: FindingStatus, dst: FindingStatus) -> bool; (g) Pydantic BaseModel Finding with all fields from the spec (pr_number, head_sha, round, finder, claim_type, severity, file, line_start, line_end, claim, evidence_pointer, status=FindingStatus.PROPOSED, verification_method='', verification_result='', checked_at_sha='', id: int | None = None). Use required imports: `from enum import Enum`, `from pathlib import Path`, `from pydantic import BaseModel`, `from ..core.logging import get_logger`. Create `tests/unit/test_review_findings.py` with: test_allowed_transitions_law (verifies each entry in ALLOWED_TRANSITIONS matches the spec diagram), test_terminal_states_have_no_exits (refuted/resolved/stuck map to empty frozensets), test_is_valid_transition_accepts_legal_move, test_is_valid_transition_rejects_illegal_move, test_finding_default_status_is_proposed, test_finding_requires_claim_fields (construction with all required fields).",
      "commit_message": "feat(review): Finding model, claim taxonomy and lifecycle law",
      "done_when": "pytest tests/unit/test_review_findings.py::test_allowed_transitions_law tests/unit/test_review_findings.py::test_terminal_states_have_no_exits tests/unit/test_review_findings.py::test_is_valid_transition_accepts_legal_move tests/unit/test_review_findings.py::test_is_valid_transition_rejects_illegal_move tests/unit/test_review_findings.py::test_finding_default_status_is_proposed passes and ruff check src/ferova/review/findings.py exits 0",
      "unit_tests": [
        "tests/unit/test_review_findings.py::test_allowed_transitions_law",
        "tests/unit/test_review_findings.py::test_terminal_states_have_no_exits",
        "tests/unit/test_review_findings.py::test_is_valid_transition_accepts_legal_move",
        "tests/unit/test_review_findings.py::test_is_valid_transition_rejects_illegal_move",
        "tests/unit/test_review_findings.py::test_finding_default_status_is_proposed"
      ]
    },
    {
      "index": 2,
      "title": "pr_findings ledger — record, transition-guarded update, fetch",
      "files": [
        "src/ferova/review/findings.py",
        "tests/unit/test_review_findings.py"
      ],
      "action": "Re-emit the full `src/ferova/review/findings.py` grown with: module-private `_metadata = MetaData()`, `_engine_for(db_path: Path)` that calls `db_path.parent.mkdir(parents=True, exist_ok=True)` then `create_engine(f'sqlite:///{db_path}')`, a `pr_findings` Table with one Integer primary-key autoincrement `id` column and one String column per remaining Finding field (enums stored as String values), `init_findings_schema(db_path: Path) -> None` calling `_metadata.create_all(engine, checkfirst=True)`, `record_finding(db_path: Path, finding: Finding) -> int` that inserts and returns the new row id, `update_finding_status(db_path: Path, finding_id: int, new_status: FindingStatus, *, verification_method: str = '', verification_result: str = '', checked_at_sha: str = '') -> bool` that reads current status, calls is_valid_transition, emits a `findings.invalid_transition` structlog warning and returns False on rejection, otherwise updates and returns True, `fetch_findings(db_path: Path, pr_number: int, *, status: FindingStatus | None = None) -> list[Finding]` ordered by id. Add to sqlalchemy imports: `select`. Re-emit the full `tests/unit/test_review_findings.py` grown with: test_init_findings_schema_idempotent, test_record_and_fetch_finding_round_trip (insert → fetch, verify all scalar fields match), test_legal_transition_updates (proposed→verified returns True, row reads verified), test_illegal_transition_refused (verified→resolved returns False, row still reads verified, warning logged via caplog or structlog capture), test_fetch_findings_filters_by_status (insert proposed + verified finding, filter by each status returns only that one).",
      "commit_message": "feat(review): pr_findings ledger — record, transition-guarded update, fetch",
      "done_when": "pytest tests/unit/test_review_findings.py passes (all tests including test_record_and_fetch_finding_round_trip, test_legal_transition_updates, test_illegal_transition_refused, test_fetch_findings_filters_by_status, test_init_findings_schema_idempotent) and ruff check src/ferova/review/findings.py exits 0 and ruff format --check src/ferova/review/findings.py exits 0",
      "unit_tests": [
        "tests/unit/test_review_findings.py::test_init_findings_schema_idempotent",
        "tests/unit/test_review_findings.py::test_record_and_fetch_finding_round_trip",
        "tests/unit/test_review_findings.py::test_legal_transition_updates",
        "tests/unit/test_review_findings.py::test_illegal_transition_refused",
        "tests/unit/test_review_findings.py::test_fetch_findings_filters_by_status"
      ]
    }
  ],
  "integration_tests": [
    "tests/unit/test_review_findings.py::test_record_and_fetch_finding_round_trip"
  ]
}
```
