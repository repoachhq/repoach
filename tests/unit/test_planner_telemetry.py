"""SP-PLAN-QUALITY step 4/6 — per-attempt planner telemetry persistence.

Pins the ``planner_attempts`` ledger: two rejected attempts recorded
with correct ``spec_id``/``attempt`` and fetched back ordered by id,
and the never-raise failure policy — a telemetry write that cannot
reach its database returns ``False`` and logs a warning instead of
letting an exception escape into the planner's refine loop.

Step 5/6 adds the insights side: ``gather_insights`` aggregates
``violated_rule -> count`` across every planner attempt in the
telemetry ledger (deliberately ignoring PR scoping — planner attempts
are keyed by spec_id, not PR), and the ``ferova review insights`` CLI
command renders the aggregate under a ``planner_rule_violations`` key.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from structlog.testing import capture_logs
from typer.testing import CliRunner

from repoach.review.findings import init_findings_schema
from repoach.review.planner_telemetry import (
    fetch_planner_attempts,
    init_planner_telemetry_schema,
    record_planner_attempt,
)
from repoach.review.review_lessons import gather_insights

_SPEC_ID = "SP-TEST-TELEMETRY"


def test_attempt_rows_persisted(tmp_path: Path) -> None:
    """Two rejected attempts are recorded with correct spec_id/attempt; fetch is ordered."""
    db_path = tmp_path / "test.db"
    init_planner_telemetry_schema(db_path)

    first_ok = record_planner_attempt(
        db_path,
        spec_id=_SPEC_ID,
        attempt=1,
        violated_rule="plan payload failed validation: missing commit_message",
    )
    second_ok = record_planner_attempt(
        db_path,
        spec_id=_SPEC_ID,
        attempt=2,
        violated_rule="step 1 ('Add module') touches 4 files, exceeding cap",
    )
    assert first_ok is True
    assert second_ok is True

    rows = fetch_planner_attempts(db_path, spec_id=_SPEC_ID)
    assert len(rows) == 2
    assert rows[0]["spec_id"] == _SPEC_ID
    assert rows[0]["attempt"] == 1
    assert rows[0]["violated_rule"] == "plan payload failed validation: missing commit_message"
    assert rows[1]["spec_id"] == _SPEC_ID
    assert rows[1]["attempt"] == 2
    assert rows[1]["violated_rule"] == "step 1 ('Add module') touches 4 files, exceeding cap"
    assert rows[0]["recorded_at"] is not None

    all_rows = fetch_planner_attempts(db_path)
    assert len(all_rows) == 2


def test_telemetry_failure_never_breaks_planning(tmp_path: Path) -> None:
    """A db path under a FILE used as a directory returns False, logs, never raises."""
    blocking_file = tmp_path / "not_a_directory"
    blocking_file.write_text("i am a file, not a directory", encoding="utf-8")
    uncreatable_db_path = blocking_file / "nested" / "telemetry.db"

    with capture_logs() as captured:
        result = record_planner_attempt(
            uncreatable_db_path,
            spec_id=_SPEC_ID,
            attempt=1,
            violated_rule="plan payload failed validation: missing commit_message",
        )

    assert result is False
    assert any(
        log.get("log_level") == "warning" and log.get("event") == "planner_telemetry.record_failed"
        for log in captured
    )


def test_insights_reports_rule_violations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """gather_insights aggregates violated_rule counts across ALL sessions, ignoring pr_number.

    Seeds three attempts across two spec_ids (one rule repeated twice, one
    rule once) and asserts both the raw ``gather_insights`` aggregation and
    the rendered CLI dict (driven through the ``review insights`` command
    body) carry the ``planner_rule_violations`` key with the right counts.
    """
    import json

    from repoach.cli import review_cmds

    db_path = tmp_path / "ledger.db"
    init_planner_telemetry_schema(db_path)
    init_findings_schema(db_path)

    record_planner_attempt(
        db_path,
        spec_id="SP-ALPHA",
        attempt=1,
        violated_rule="step touches 4 files, exceeding cap",
    )
    record_planner_attempt(
        db_path,
        spec_id="SP-ALPHA",
        attempt=2,
        violated_rule="step touches 4 files, exceeding cap",
    )
    record_planner_attempt(
        db_path,
        spec_id="SP-BETA",
        attempt=1,
        violated_rule="no-stub lint: monkeypatch of plan-touched symbol",
    )

    insights = gather_insights(db_path, pr_number=42)

    assert insights.planner_rule_violations == {
        "step touches 4 files, exceeding cap": 2,
        "no-stub lint: monkeypatch of plan-touched symbol": 1,
    }

    monkeypatch.setattr(review_cmds, "get_settings", lambda: SimpleNamespace(db_path=str(db_path)))

    runner = CliRunner()
    result = runner.invoke(review_cmds.review_app, ["insights"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert "planner_rule_violations" in payload
    assert payload["planner_rule_violations"] == {
        "step touches 4 files, exceeding cap": 2,
        "no-stub lint: monkeypatch of plan-touched symbol": 1,
    }
