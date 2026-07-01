# SP-FINDER-OUTPUT — Dual-run findings bridge: every reviewer comment becomes a persisted Finding

Add a pure-derivation bridge module (findings_bridge.py) that maps BotRole → ClaimType and severity strings → Severity, converts ReviewComment objects to Finding records, and bulk-persists them for all valid ReviewerOutcomes. Wire the bridge into the orchestrator's review_pr method right before _build_team_outcome, wrapped in try/except so a bridge failure never breaks the verdict flow. The two steps match the spec's commit plan exactly: step 1 delivers the bridge + all unit tests; step 2 wires the orchestrator and adds the wiring-specific unit tests.

## Step 1 — Add findings_bridge module with unit tests

- **Files**: `src/ferova/review/findings_bridge.py`, `tests/unit/test_findings_bridge.py`, `tests/integration/test_findings_bridge.py`
- **Action**: Create src/ferova/review/findings_bridge.py with the following exact content (copy imports verbatim — do NOT invent other modules):

```python
"""Bridge: derive Finding records from ReviewerOutcome comments (dual-run, SP-FINDER-OUTPUT).

Provisional claim_type defaults map each lens to a ClaimType bucket.
Verifier slices 4-5 own the refinement; proposed status signals unverified.
"""
from __future__ import annotations

from pathlib import Path

from .consensus import is_unparsed_outcome
from .findings import ClaimType, Finding, Severity, init_findings_schema, record_finding
from .reviewer import BotRole, ReviewComment, ReviewerOutcome

LENS_DEFAULT_CLAIM_TYPE: dict[BotRole, ClaimType] = {
    BotRole.ARCHITECT: ClaimType.DESIGN,
    BotRole.SENTINEL: ClaimType.SECURITY,
    BotRole.TESTER: ClaimType.MISSING_TEST,
    BotRole.SCRIBE: ClaimType.MISSING_DOCSTRING,
}

SEVERITY_MAP: dict[str, Severity] = {
    "blocker": Severity.BLOCKING,
    "major": Severity.BLOCKING,
    "minor": Severity.ADVISORY,
    "nit": Severity.ADVISORY,
}


def comment_to_finding(
    comment: ReviewComment,
    *,
    role: BotRole,
    pr_number: int,
    head_sha: str,
    round_n: int,
) -> Finding:
    """Derive a Finding from one ReviewComment.

    Args:
        comment: The reviewer comment to convert.
        role: BotRole that produced the comment.
        pr_number: GitHub PR number.
        head_sha: Commit SHA at review time.
        round_n: Review round index.

    Returns:
        A Finding with status=PROPOSED and claim_type from LENS_DEFAULT_CLAIM_TYPE.
    """
    return Finding(
        pr_number=pr_number,
        head_sha=head_sha,
        round=round_n,
        finder=role.value,
        claim_type=LENS_DEFAULT_CLAIM_TYPE.get(role, ClaimType.DESIGN),
        severity=SEVERITY_MAP.get(comment.severity, Severity.ADVISORY),
        file=comment.file,
        line_start=comment.line,
        line_end=comment.line,
        claim=comment.body[:500],
        evidence_pointer=f"{comment.file}:{comment.line} \u2014 {comment.body[:200]}",
    )


def record_findings_for_outcomes(
    db_path: Path,
    *,
    pr_number: int,
    head_sha: str | None,
    outcomes: list[ReviewerOutcome],
    round_n: int,
) -> int:
    """Persist one Finding per comment across all valid ReviewerOutcomes.

    Calls init_findings_schema once; skips any outcome where
    is_unparsed_outcome returns True; returns the recorded count.
    head_sha=None is stored as empty string.

    Args:
        db_path: Path to the SQLite database file.
        pr_number: GitHub PR number.
        head_sha: Commit SHA at review time (None becomes "").
        outcomes: All reviewer outcomes from the run.
        round_n: Review round index (1 or 2).

    Returns:
        Number of findings recorded.
    """
    init_findings_schema(db_path)
    effective_sha = head_sha or ""
    count = 0
    for outcome in outcomes:
        if is_unparsed_outcome(outcome):
            continue
        for comment in outcome.comments:
            finding = comment_to_finding(
                comment,
                role=outcome.role,
                pr_number=pr_number,
                head_sha=effective_sha,
                round_n=round_n,
            )
            record_finding(db_path, finding)
            count += 1
    return count
```

Create tests/unit/test_findings_bridge.py with tests: test_lens_default_claim_types (verify all four BotRole keys map to the right ClaimType), test_severity_mapping (blocker→BLOCKING, major→BLOCKING, minor→ADVISORY, nit→ADVISORY, unknown→ADVISORY), test_record_findings_round_trip (two Architect comments, record, fetch_findings, verify count=2 and fields), test_unparsed_outcomes_skipped (Scribe outcome with summary='[parse_failed:TRANSPORT] …' and zero comments is skipped; returns 0), test_none_head_sha_empty (head_sha=None stored as '' in the ledger).

Create tests/integration/test_findings_bridge.py with the smoke scenario: tmp db, Architect outcome with one 'blocker' comment and one 'nit' comment + Scribe outcome with summary='[parse_failed:TRANSPORT] ...' and no comments; call record_findings_for_outcomes; assert return value is 2; fetch_findings returns exactly 2 findings both with finder='architect', claim_type=ClaimType.DESIGN, status=FindingStatus.PROPOSED; severities are BLOCKING and ADVISORY.
- **Commit**: `feat(review): findings bridge — comments become proposed findings (dual-run)`
- **Done when**: pytest tests/unit/test_findings_bridge.py::test_lens_default_claim_types tests/unit/test_findings_bridge.py::test_severity_mapping tests/unit/test_findings_bridge.py::test_record_findings_round_trip tests/unit/test_findings_bridge.py::test_unparsed_outcomes_skipped tests/unit/test_findings_bridge.py::test_none_head_sha_empty tests/integration/test_findings_bridge.py -v exits 0
- **Unit tests**: `tests/unit/test_findings_bridge.py::test_lens_default_claim_types`, `tests/unit/test_findings_bridge.py::test_severity_mapping`, `tests/unit/test_findings_bridge.py::test_record_findings_round_trip`, `tests/unit/test_findings_bridge.py::test_unparsed_outcomes_skipped`, `tests/unit/test_findings_bridge.py::test_none_head_sha_empty`

## Step 2 — Wire orchestrator to record findings on every review run

- **Files**: `src/ferova/review/orchestrator.py`, `tests/unit/test_findings_bridge.py`
- **Action**: Edit src/ferova/review/orchestrator.py:

1. Add this import to the existing import block (after the .consensus import line):
   `from .findings_bridge import record_findings_for_outcomes`

2. In review_pr, locate the block that calls `self._build_team_outcome(...)`. Insert the following block IMMEDIATELY BEFORE that call (after the n_blockers/n_majors sums and evaluate_consensus call):

```python
        try:
            n = record_findings_for_outcomes(
                self._db_path,
                pr_number=pr_number,
                head_sha=head_sha,
                outcomes=outcomes,
                round_n=2 if round2_ran else 1,
            )
            _log.info("review_team.findings_recorded", pr_number=pr_number, n_findings=n)
        except Exception:
            _log.warning("review_team.findings_record_failed", pr_number=pr_number)
```

Add two tests to tests/unit/test_findings_bridge.py:

- test_orchestrator_records_findings: build a ReviewOrchestrator with dry_run=True and a tmp db; patch _run_round1 to return one Architect outcome with one 'minor' comment; patch _run_round2 to return (same_outcomes, False); patch record_findings_for_outcomes in ferova.review.orchestrator to a MagicMock that returns 1; call review_pr(pr_number=7, diff_override='diff'); assert the mock was called once with the correct keyword args (pr_number=7, round_n=1) and that the returned dict has 'verdict'.

- test_findings_failure_never_breaks_review: same setup but patch record_findings_for_outcomes to raise RuntimeError('boom'); call review_pr; assert the returned dict still has 'verdict' (TeamOutcome was produced despite the exception).
- **Commit**: `feat(review): orchestrator records findings on every review run`
- **Done when**: pytest tests/unit/test_findings_bridge.py::test_orchestrator_records_findings tests/unit/test_findings_bridge.py::test_findings_failure_never_breaks_review -v exits 0 AND ruff check src/ferova/review/findings_bridge.py src/ferova/review/orchestrator.py exits 0 AND ruff format --check src/ferova/review/findings_bridge.py src/ferova/review/orchestrator.py exits 0
- **Unit tests**: `tests/unit/test_findings_bridge.py::test_orchestrator_records_findings`, `tests/unit/test_findings_bridge.py::test_findings_failure_never_breaks_review`

## Integration tests

- `tests/integration/test_findings_bridge.py`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-FINDER-OUTPUT",
  "title": "Dual-run findings bridge: every reviewer comment becomes a persisted Finding",
  "summary": "Add a pure-derivation bridge module (findings_bridge.py) that maps BotRole → ClaimType and severity strings → Severity, converts ReviewComment objects to Finding records, and bulk-persists them for all valid ReviewerOutcomes. Wire the bridge into the orchestrator's review_pr method right before _build_team_outcome, wrapped in try/except so a bridge failure never breaks the verdict flow. The two steps match the spec's commit plan exactly: step 1 delivers the bridge + all unit tests; step 2 wires the orchestrator and adds the wiring-specific unit tests.",
  "steps": [
    {
      "index": 1,
      "title": "Add findings_bridge module with unit tests",
      "files": [
        "src/ferova/review/findings_bridge.py",
        "tests/unit/test_findings_bridge.py",
        "tests/integration/test_findings_bridge.py"
      ],
      "action": "Create src/ferova/review/findings_bridge.py with the following exact content (copy imports verbatim — do NOT invent other modules):\n\n```python\n\"\"\"Bridge: derive Finding records from ReviewerOutcome comments (dual-run, SP-FINDER-OUTPUT).\n\nProvisional claim_type defaults map each lens to a ClaimType bucket.\nVerifier slices 4-5 own the refinement; proposed status signals unverified.\n\"\"\"\nfrom __future__ import annotations\n\nfrom pathlib import Path\n\nfrom .consensus import is_unparsed_outcome\nfrom .findings import ClaimType, Finding, Severity, init_findings_schema, record_finding\nfrom .reviewer import BotRole, ReviewComment, ReviewerOutcome\n\nLENS_DEFAULT_CLAIM_TYPE: dict[BotRole, ClaimType] = {\n    BotRole.ARCHITECT: ClaimType.DESIGN,\n    BotRole.SENTINEL: ClaimType.SECURITY,\n    BotRole.TESTER: ClaimType.MISSING_TEST,\n    BotRole.SCRIBE: ClaimType.MISSING_DOCSTRING,\n}\n\nSEVERITY_MAP: dict[str, Severity] = {\n    \"blocker\": Severity.BLOCKING,\n    \"major\": Severity.BLOCKING,\n    \"minor\": Severity.ADVISORY,\n    \"nit\": Severity.ADVISORY,\n}\n\n\ndef comment_to_finding(\n    comment: ReviewComment,\n    *,\n    role: BotRole,\n    pr_number: int,\n    head_sha: str,\n    round_n: int,\n) -> Finding:\n    \"\"\"Derive a Finding from one ReviewComment.\n\n    Args:\n        comment: The reviewer comment to convert.\n        role: BotRole that produced the comment.\n        pr_number: GitHub PR number.\n        head_sha: Commit SHA at review time.\n        round_n: Review round index.\n\n    Returns:\n        A Finding with status=PROPOSED and claim_type from LENS_DEFAULT_CLAIM_TYPE.\n    \"\"\"\n    return Finding(\n        pr_number=pr_number,\n        head_sha=head_sha,\n        round=round_n,\n        finder=role.value,\n        claim_type=LENS_DEFAULT_CLAIM_TYPE.get(role, ClaimType.DESIGN),\n        severity=SEVERITY_MAP.get(comment.severity, Severity.ADVISORY),\n        file=comment.file,\n        line_start=comment.line,\n        line_end=comment.line,\n        claim=comment.body[:500],\n        evidence_pointer=f\"{comment.file}:{comment.line} \\u2014 {comment.body[:200]}\",\n    )\n\n\ndef record_findings_for_outcomes(\n    db_path: Path,\n    *,\n    pr_number: int,\n    head_sha: str | None,\n    outcomes: list[ReviewerOutcome],\n    round_n: int,\n) -> int:\n    \"\"\"Persist one Finding per comment across all valid ReviewerOutcomes.\n\n    Calls init_findings_schema once; skips any outcome where\n    is_unparsed_outcome returns True; returns the recorded count.\n    head_sha=None is stored as empty string.\n\n    Args:\n        db_path: Path to the SQLite database file.\n        pr_number: GitHub PR number.\n        head_sha: Commit SHA at review time (None becomes \"\").\n        outcomes: All reviewer outcomes from the run.\n        round_n: Review round index (1 or 2).\n\n    Returns:\n        Number of findings recorded.\n    \"\"\"\n    init_findings_schema(db_path)\n    effective_sha = head_sha or \"\"\n    count = 0\n    for outcome in outcomes:\n        if is_unparsed_outcome(outcome):\n            continue\n        for comment in outcome.comments:\n            finding = comment_to_finding(\n                comment,\n                role=outcome.role,\n                pr_number=pr_number,\n                head_sha=effective_sha,\n                round_n=round_n,\n            )\n            record_finding(db_path, finding)\n            count += 1\n    return count\n```\n\nCreate tests/unit/test_findings_bridge.py with tests: test_lens_default_claim_types (verify all four BotRole keys map to the right ClaimType), test_severity_mapping (blocker→BLOCKING, major→BLOCKING, minor→ADVISORY, nit→ADVISORY, unknown→ADVISORY), test_record_findings_round_trip (two Architect comments, record, fetch_findings, verify count=2 and fields), test_unparsed_outcomes_skipped (Scribe outcome with summary='[parse_failed:TRANSPORT] …' and zero comments is skipped; returns 0), test_none_head_sha_empty (head_sha=None stored as '' in the ledger).\n\nCreate tests/integration/test_findings_bridge.py with the smoke scenario: tmp db, Architect outcome with one 'blocker' comment and one 'nit' comment + Scribe outcome with summary='[parse_failed:TRANSPORT] ...' and no comments; call record_findings_for_outcomes; assert return value is 2; fetch_findings returns exactly 2 findings both with finder='architect', claim_type=ClaimType.DESIGN, status=FindingStatus.PROPOSED; severities are BLOCKING and ADVISORY.",
      "commit_message": "feat(review): findings bridge — comments become proposed findings (dual-run)",
      "done_when": "pytest tests/unit/test_findings_bridge.py::test_lens_default_claim_types tests/unit/test_findings_bridge.py::test_severity_mapping tests/unit/test_findings_bridge.py::test_record_findings_round_trip tests/unit/test_findings_bridge.py::test_unparsed_outcomes_skipped tests/unit/test_findings_bridge.py::test_none_head_sha_empty tests/integration/test_findings_bridge.py -v exits 0",
      "unit_tests": [
        "tests/unit/test_findings_bridge.py::test_lens_default_claim_types",
        "tests/unit/test_findings_bridge.py::test_severity_mapping",
        "tests/unit/test_findings_bridge.py::test_record_findings_round_trip",
        "tests/unit/test_findings_bridge.py::test_unparsed_outcomes_skipped",
        "tests/unit/test_findings_bridge.py::test_none_head_sha_empty"
      ]
    },
    {
      "index": 2,
      "title": "Wire orchestrator to record findings on every review run",
      "files": [
        "src/ferova/review/orchestrator.py",
        "tests/unit/test_findings_bridge.py"
      ],
      "action": "Edit src/ferova/review/orchestrator.py:\n\n1. Add this import to the existing import block (after the .consensus import line):\n   `from .findings_bridge import record_findings_for_outcomes`\n\n2. In review_pr, locate the block that calls `self._build_team_outcome(...)`. Insert the following block IMMEDIATELY BEFORE that call (after the n_blockers/n_majors sums and evaluate_consensus call):\n\n```python\n        try:\n            n = record_findings_for_outcomes(\n                self._db_path,\n                pr_number=pr_number,\n                head_sha=head_sha,\n                outcomes=outcomes,\n                round_n=2 if round2_ran else 1,\n            )\n            _log.info(\"review_team.findings_recorded\", pr_number=pr_number, n_findings=n)\n        except Exception:\n            _log.warning(\"review_team.findings_record_failed\", pr_number=pr_number)\n```\n\nAdd two tests to tests/unit/test_findings_bridge.py:\n\n- test_orchestrator_records_findings: build a ReviewOrchestrator with dry_run=True and a tmp db; patch _run_round1 to return one Architect outcome with one 'minor' comment; patch _run_round2 to return (same_outcomes, False); patch record_findings_for_outcomes in ferova.review.orchestrator to a MagicMock that returns 1; call review_pr(pr_number=7, diff_override='diff'); assert the mock was called once with the correct keyword args (pr_number=7, round_n=1) and that the returned dict has 'verdict'.\n\n- test_findings_failure_never_breaks_review: same setup but patch record_findings_for_outcomes to raise RuntimeError('boom'); call review_pr; assert the returned dict still has 'verdict' (TeamOutcome was produced despite the exception).",
      "commit_message": "feat(review): orchestrator records findings on every review run",
      "done_when": "pytest tests/unit/test_findings_bridge.py::test_orchestrator_records_findings tests/unit/test_findings_bridge.py::test_findings_failure_never_breaks_review -v exits 0 AND ruff check src/ferova/review/findings_bridge.py src/ferova/review/orchestrator.py exits 0 AND ruff format --check src/ferova/review/findings_bridge.py src/ferova/review/orchestrator.py exits 0",
      "unit_tests": [
        "tests/unit/test_findings_bridge.py::test_orchestrator_records_findings",
        "tests/unit/test_findings_bridge.py::test_findings_failure_never_breaks_review"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_findings_bridge.py"
  ]
}
```
