# SP-FINDING-MODEL — the Finding model, its lifecycle, and the pr_findings ledger

## Metadata

- **Status**: OPEN
- **Priority**: P1 — review redesign slice 1 of 11
  (docs/review_redesign_architecture.md)
- **Owner**: operator
- **Executor**: `ferova develop`
- **Opened**: 2026-06-11

## Why

The review redesign replaces the verdict with the **finding** as the
atomic unit: a falsifiable claim carrying an evidence pointer and a
persisted lifecycle. This slice is the foundation every later slice
builds on (finder output, verifiers, the pure merge gate, the learning
loop). It is a **pure addition** — nothing imports it yet, no behavior
changes anywhere.

## What

One NEW module, `src/ferova/review/findings.py`, fully
self-contained (its own table + engine helpers — do NOT modify
`persistence.py`):

1. **Enums** (`str, Enum` style, mirroring `ReviewVerdict` in
   `reviewer.py`):
   - `ClaimType`: `missing_test`, `missing_docstring`,
     `lint_convention`, `broken_behavior`, `spec_gap`, `design`,
     `security`.
   - `Severity`: `blocking`, `advisory`.
   - `FindingStatus`: `proposed`, `verified`, `refuted`, `open`,
     `resolved`, `stuck`.
2. **Lifecycle law** — module constant
   `ALLOWED_TRANSITIONS: dict[FindingStatus, frozenset[FindingStatus]]`:
   `proposed → {verified, refuted}` · `verified → {open}` ·
   `open → {resolved, stuck}` · `refuted/resolved/stuck → {}`
   (terminal). Plus
   `is_valid_transition(src: FindingStatus, dst: FindingStatus) -> bool`.
3. **Pydantic model** `Finding` (BaseModel, strict types):
   `pr_number: int`, `head_sha: str`, `round: int`, `finder: str`
   (lens name), `claim_type: ClaimType`, `severity: Severity`,
   `file: str`, `line_start: int`, `line_end: int`, `claim: str`
   (one falsifiable sentence), `evidence_pointer: str` (what to
   check), `status: FindingStatus = FindingStatus.PROPOSED`,
   `verification_method: str = ""`, `verification_result: str = ""`,
   `checked_at_sha: str = ""`, `id: int | None = None`.
4. **Ledger** — SQLAlchemy Core table `pr_findings` (one column per
   model field; `id` Integer primary key autoincrement; enums stored
   as String values) with module-private `_metadata` and an
   `_engine_for(db_path)` helper, exactly mirroring the established
   pattern in `persistence.py`
   (`create_engine(f"sqlite:///{db_path}")`, `Table`, `Column`,
   `MetaData`, `_metadata.create_all(engine, checkfirst=True)`):
   - `init_findings_schema(db_path: Path) -> None` — idempotent.
   - `record_finding(db_path: Path, finding: Finding) -> int` —
     insert, return the new row id.
   - `update_finding_status(db_path: Path, finding_id: int, new_status:
     FindingStatus, *, verification_method: str = "",
     verification_result: str = "", checked_at_sha: str = "") -> bool`
     — reads the current status, refuses (returns ``False``, emits a
     `findings.invalid_transition` structlog warning) when
     `is_valid_transition` rejects the move, updates and returns
     ``True`` otherwise.
   - `fetch_findings(db_path: Path, pr_number: int, *, status:
     FindingStatus | None = None) -> list[Finding]` — ordered by id.
5. Module docstring states the redesign principle: stored findings are
   a hint — verification at the exact head is the truth.

Required imports (verified to exist — copy these, do not improvise):
`from enum import Enum` · `from pathlib import Path` ·
`from pydantic import BaseModel` ·
`from sqlalchemy import Column, Integer, MetaData, String, Table,
create_engine, select` · `from ..core.logging import get_logger`.

## Files in scope

- `src/ferova/review/findings.py` (new)
- `tests/unit/test_review_findings.py` (new)

## Plan-shaping constraints

- Both files are NEW — no step may contract any existing file.
- Two steps maximum; each step creates its code AND its promised
  tests in the same step.
- Step contracts: step 1 = both files (model + lifecycle + their
  tests); step 2 = both files again (ledger CRUD + their tests,
  re-emitting the full grown files).

## Out of scope

- Wiring into the orchestrator/reviewers (slice 3).
- Any change to `persistence.py`, `consensus.py`, `reviewer.py`.
- Verification logic (slices 4-5) — `update_finding_status` only
  records what a verifier decided, it never verifies.

## Smoke scenario

### Setup

A tmp db path.

### Execute

`init_findings_schema`, `record_finding` of a `proposed` blocking
`missing_test` finding, `update_finding_status` to `verified`, then an
illegal jump `verified → resolved`, then `fetch_findings`.

### Expected

Insert returns an id; the legal transition returns ``True``; the
illegal jump returns ``False`` and the row still reads `verified`;
fetch returns one Finding with the recorded fields.

## Definition of Done

- Lifecycle law pinned — `test_allowed_transitions_law`,
  `test_terminal_states_have_no_exits`.
- Round-trip — `test_record_and_fetch_finding_round_trip`.
- Legal transition updates row — `test_legal_transition_updates`.
- Illegal transition refused, row untouched, warning emitted —
  `test_illegal_transition_refused`.
- Status filter — `test_fetch_findings_filters_by_status`.
- Schema idempotent — `test_init_findings_schema_idempotent`.
- `ruff` + `ruff format --check` + `pytest tests/unit` green; zero
  inline comments; no `# noqa`.

## Commit plan

1. `feat(review): Finding model, claim taxonomy and lifecycle law`
2. `feat(review): pr_findings ledger — record, transition-guarded update, fetch`

## Risks

- **Schema drift vs later slices**: fields mirror
  docs/review_redesign_architecture.md verbatim; later slices may
  ALTER-extend via the same self-heal pattern persistence.py uses.
- **Nothing imports the module yet**: intentional — the vulture-style
  dead-code sweeps must skip redesign-staging modules until slice 3
  wires them (note for reviewers: this is slice 1 of a committed
  11-slice plan, not an orphan).
