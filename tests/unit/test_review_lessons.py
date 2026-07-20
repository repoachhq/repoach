"""Unit tests for SP-REVIEW-LESSONS (slice 11) — distil + insights.

The distil side learns only from confirmed-real findings (never refuted
ones); the write is gated + injectable so no live network is touched. The
insights side is pinned on the per-lens precision metric and the
status/claim-type distributions. The CLI is exercised via ``CliRunner``
with a tmp ledger.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

import repoach.core.config as config
from repoach.review import review_lessons
from repoach.review.findings import (
    ClaimType,
    Finding,
    FindingStatus,
    Severity,
    init_findings_schema,
    record_finding,
)


@pytest.fixture()
def fresh_settings() -> Iterator[None]:
    config._settings = None
    try:
        yield
    finally:
        config._settings = None


def _rec(
    db: Path,
    *,
    finder: str = "architect",
    claim_type: ClaimType = ClaimType.DESIGN,
    status: FindingStatus = FindingStatus.VERIFIED,
    file: str = "src/m.py",
    claim: str = "smell",
    pr_number: int = 1,
    verification_result: str = "",
) -> int:
    return record_finding(
        db,
        Finding(
            pr_number=pr_number,
            head_sha="head123",
            round=1,
            finder=finder,
            claim_type=claim_type,
            severity=Severity.BLOCKING,
            file=file,
            line_start=1,
            line_end=1,
            claim=claim,
            evidence_pointer=f"{file}:1",
            status=status,
            verification_result=verification_result,
        ),
    )


def test_distill_keeps_confirmed_excludes_refuted_and_proposed(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    _rec(db, status=FindingStatus.VERIFIED, claim="verified one", file="src/a.py")
    _rec(db, status=FindingStatus.RESOLVED, claim="resolved one", file="src/b.py")
    _rec(db, status=FindingStatus.REFUTED, claim="hallucinated", file="src/c.py")
    _rec(db, status=FindingStatus.PROPOSED, claim="unjudged", file="src/d.py")

    lessons = review_lessons.distill_verified_lessons(db, 1)

    assert len(lessons) == 2
    joined = "\n".join(lessons)
    assert "verified one" in joined and "resolved one" in joined
    assert "hallucinated" not in joined and "unjudged" not in joined


def test_distill_dedupes_repeats(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    _rec(db, status=FindingStatus.VERIFIED, claim="same smell", file="src/a.py")
    _rec(db, status=FindingStatus.OPEN, claim="same smell", file="src/a.py")

    assert len(review_lessons.distill_verified_lessons(db, 1)) == 1


def test_remember_disabled_returns_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fresh_settings: None
) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    _rec(db, status=FindingStatus.VERIFIED)
    monkeypatch.setenv("REVIEW_LESSONS_ENABLED", "false")
    calls = {"n": 0}

    def _spy(*a: Any, **k: Any) -> bool:
        calls["n"] += 1
        return True

    assert review_lessons.remember_verified_findings(db, 1, remember_fn=_spy) == 0
    assert calls["n"] == 0


def test_remember_writes_to_builder_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fresh_settings: None
) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    _rec(db, status=FindingStatus.VERIFIED, claim="real blocker")
    monkeypatch.setenv("REVIEW_LESSONS_ENABLED", "true")
    seen: list[tuple[str, str]] = []

    def _fake_remember(content: str, *, project: str, base_url: str, **k: Any) -> bool:
        seen.append((project, content))
        return True

    written = review_lessons.remember_verified_findings(db, 1, remember_fn=_fake_remember)

    assert written == 1
    assert seen[0][0] == "ferova-builder"
    assert "real blocker" in seen[0][1]


def test_lens_precision_confirmed_vs_refuted(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    for i in range(3):
        _rec(db, finder="architect", status=FindingStatus.VERIFIED, claim=f"a{i}", file=f"a{i}.py")
    _rec(db, finder="architect", status=FindingStatus.REFUTED, claim="wrong", file="z.py")
    _rec(db, finder="tester", status=FindingStatus.PROPOSED, claim="pending", file="t.py")

    precision = {
        lp.finder: lp
        for lp in review_lessons.compute_lens_precision(review_lessons.fetch_all_findings(db))
    }

    assert precision["architect"].confirmed == 3
    assert precision["architect"].refuted == 1
    assert precision["architect"].precision == 0.75
    assert "tester" not in precision


def test_compute_insights_distribution(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    _rec(db, status=FindingStatus.VERIFIED, claim_type=ClaimType.DESIGN, file="a.py")
    _rec(db, status=FindingStatus.REFUTED, claim_type=ClaimType.SECURITY, file="b.py")
    _rec(db, status=FindingStatus.VERIFIED, claim_type=ClaimType.DESIGN, file="c.py")

    insights = review_lessons.gather_insights(db)

    assert insights.total == 3
    assert insights.by_status["verified"] == 2
    assert insights.by_status["refuted"] == 1
    assert insights.by_claim_type["design"] == 2


def test_gather_insights_scopes_to_pr(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    _rec(db, pr_number=1, status=FindingStatus.VERIFIED, file="a.py")
    _rec(db, pr_number=2, status=FindingStatus.VERIFIED, file="b.py")

    assert review_lessons.gather_insights(db, pr_number=1).total == 1
    assert review_lessons.gather_insights(db).total == 2


def test_cli_insights_outputs_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from repoach.cli import review_cmds

    db = tmp_path / "f.db"
    init_findings_schema(db)
    _rec(db, finder="sentinel", status=FindingStatus.VERIFIED, claim="vuln", file="a.py")
    _rec(db, finder="sentinel", status=FindingStatus.REFUTED, claim="false", file="b.py")
    monkeypatch.setattr(review_cmds, "get_settings", lambda: SimpleNamespace(db_path=str(db)))

    runner = CliRunner()
    result = runner.invoke(review_cmds.review_app, ["insights"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["total"] == 2
    sentinel = next(lp for lp in payload["lens_precision"] if lp["finder"] == "sentinel")
    assert sentinel["confirmed"] == 1 and sentinel["refuted"] == 1
    assert sentinel["precision"] == 0.5


def test_track_record_renders_precision_and_recent_refutations(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    for i in range(2):
        _rec(
            db,
            finder="scribe",
            status=FindingStatus.VERIFIED,
            claim=f"verified {i}",
            file=f"v{i}.py",
        )
    for i in range(3):
        _rec(
            db,
            finder="scribe",
            status=FindingStatus.REFUTED,
            claim=f"refuted {i}",
            file=f"r{i}.py",
            verification_result=f"reasoning {i}",
        )

    section = review_lessons.render_lens_track_record(db, "scribe")

    assert "Your recent refuted claims" in section
    assert "precision 2/5" in section
    assert "refuted 2" in section
    assert "refuted 1" in section
    assert "refuted 0" in section
    lines = section.splitlines()
    refuted_lines = [line for line in lines if line.startswith("-")]
    assert len(refuted_lines) == 3


def test_track_record_empty_without_refutations(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    _rec(db, finder="scribe", status=FindingStatus.VERIFIED, claim="ok", file="a.py")

    assert review_lessons.render_lens_track_record(db, "scribe") == ""


def test_track_record_caps_length(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    _rec(
        db,
        finder="scribe",
        status=FindingStatus.REFUTED,
        claim="bad",
        file="a.py",
        verification_result="x" * 5000,
    )

    section = review_lessons.render_lens_track_record(db, "scribe", max_chars=80)

    assert len(section) <= 83
    assert section.endswith("...")
