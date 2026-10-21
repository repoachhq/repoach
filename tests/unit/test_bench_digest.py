"""``repoach digest`` on a seeded ledger (SP-PRECISION-MEASUREMENT G6, AC4)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from repoach.bench.digest import digest, render_markdown, web_url
from repoach.cli.main import app
from repoach.review.findings import (
    ClaimType,
    Finding,
    FindingStatus,
    Severity,
    init_findings_schema,
    record_finding,
)
from repoach.review.persistence import init_schema, record_merge, record_review
from repoach.review.reviewer import BotRole, ReviewerOutcome, ReviewVerdict

REPO = "https://github.com/o/r"
OLD = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
NEW = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
LATER = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)


def _review(db: Path, pr: int, at: datetime) -> None:
    record_review(
        db,
        pr_number=pr,
        outcome=ReviewerOutcome(role=BotRole.SENTINEL, verdict=ReviewVerdict.COMMENT, summary=""),
        recorded_at=at,
    )


def _finding(db: Path, pr: int, status: FindingStatus, claim: str) -> None:
    record_finding(
        db,
        Finding(
            pr_number=pr,
            head_sha="b" * 40,
            round=1,
            finder="sentinel",
            claim_type=ClaimType.SECURITY,
            severity=Severity.BLOCKING,
            file="src/app.py",
            line_start=7,
            line_end=7,
            claim=claim,
            evidence_pointer="src/app.py:7",
            status=status,
            verification_result="the input is a constant | not user data",
        ),
    )


def _merge(db: Path, pr: int, outcome: str, at: datetime, notes: str = "") -> None:
    record_merge(
        db,
        pr_number=pr,
        outcome=outcome,
        base_ref="develop",
        head_ref=f"feat/{pr}",
        merged_sha="c" * 40 if outcome == "APPROVE" else None,
        notes=notes,
        recorded_at=at,
    )


def seeded(tmp_path: Path) -> Path:
    """Three PRs: 10 reviewed this week, 11 reviewed long ago, 12 refused then merged."""
    db = tmp_path / "ledger.db"
    init_schema(db)
    init_findings_schema(db)
    _review(db, 10, NEW)
    _review(db, 11, OLD)
    _finding(db, 10, FindingStatus.REFUTED, "SQL injection in the lookup")
    _finding(db, 10, FindingStatus.VERIFIED, "real one")
    _finding(db, 11, FindingStatus.REFUTED, "old refutation")
    _merge(db, 12, "SKIP_GATE", OLD, "open blocking finding #4")
    _merge(db, 12, "SKIP_CI_RED", NEW, "ci red: test")
    _merge(db, 12, "APPROVE", LATER)
    _merge(db, 13, "SKIP_STATE", NEW, "closed")
    _merge(db, 14, "APPROVE", NEW)
    return db


def test_digest_lists_what_the_gate_refused_in_the_period(tmp_path: Path) -> None:
    report = digest(
        seeded(tmp_path), datetime(2026, 10, 13, tzinfo=UTC), repo_url=REPO, forge="github"
    )
    assert [(f.pr, f.claim) for f in report.refuted_findings] == [
        (10, "SQL injection in the lookup")
    ]
    assert report.refuted_findings[0].pr_url == "https://github.com/o/r/pull/10"
    assert [(r.pr, r.outcome) for r in report.refused_lgtms] == [(12, "SKIP_CI_RED")]
    assert [(p.pr, p.refusals) for p in report.passed_after_refusal] == [(12, 2)]
    assert report.counts == {
        "refuted_findings": 1,
        "refused_lgtms": 1,
        "passed_after_refusal": 1,
        "prs": 2,
    }


def test_markdown_has_counts_links_and_escaped_cells(tmp_path: Path) -> None:
    text = render_markdown(
        digest(seeded(tmp_path), datetime(2026, 10, 13, tzinfo=UTC), repo_url=REPO, forge="github")
    )
    assert text.startswith("# What the gate refused since 2026-10-13\n")
    assert "- refuted findings: 1" in text
    assert (
        "| [#10](https://github.com/o/r/pull/10) | sentinel | SQL injection in the lookup |" in text
    )
    assert "the input is a constant \\| not user data" in text
    assert (
        "| [#12](https://github.com/o/r/pull/12) | 2026-10-15 09:00 | SKIP_CI_RED | ci red: test |"
        in text
    )
    assert "| [#12](https://github.com/o/r/pull/12) | 2026-10-16 09:00 | 2 |" in text


def test_empty_or_missing_ledger_renders_none(tmp_path: Path) -> None:
    report = digest(
        tmp_path / "absent.db", datetime(2026, 10, 13, tzinfo=UTC), repo_url=REPO, forge="gitlab"
    )
    assert report.counts["refuted_findings"] == 0
    assert render_markdown(report).count("None.") == 3


def test_gitlab_links(tmp_path: Path) -> None:
    report = digest(
        seeded(tmp_path), datetime(2026, 10, 13, tzinfo=UTC), repo_url=REPO, forge="gitlab"
    )
    assert report.refuted_findings[0].pr_url == "https://github.com/o/r/-/merge_requests/10"


def test_web_url_normalises_remotes() -> None:
    assert web_url("git@gitlab.com:o/r.git\n") == "https://gitlab.com/o/r"
    assert web_url("ssh://git@github.com:22/o/r.git") == "https://github.com/o/r"
    assert web_url("https://user:tok@github.com/o/r.git") == "https://github.com/o/r"
    assert web_url("https://github.com/o/r/") == "https://github.com/o/r"


def test_cli_json_has_the_spec_keys_and_links(tmp_path: Path) -> None:
    db = seeded(tmp_path)
    result = CliRunner().invoke(
        app,
        [
            "digest",
            "--since",
            "2026-10-13",
            "--format",
            "json",
            "--db",
            str(db),
            "--repo-url",
            REPO,
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert {"refuted_findings", "refused_lgtms", "passed_after_refusal", "counts"} <= set(data)
    for key in ("refuted_findings", "refused_lgtms", "passed_after_refusal"):
        assert data[key]
        assert all(item["pr_url"].startswith(REPO) for item in data[key])


def test_cli_rejects_a_bad_format_and_a_bad_since(tmp_path: Path) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["digest", "--format", "html", "--repo-url", REPO]).exit_code == 2
    assert runner.invoke(app, ["digest", "--since", "lately", "--repo-url", REPO]).exit_code == 2
