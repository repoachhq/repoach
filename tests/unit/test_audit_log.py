"""Unit tests for the chain mutation journal (SP-CHAINPILOT-AUDIT-LOG)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from repoach.review.audit_log import (
    fetch_mutations,
    record_mutations,
    render_changelog,
)
from repoach.review.decision import MutationKind, PlannedMutation

_T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _mut(
    kind: MutationKind, model: str, *, provider: str | None = None, metric: str | None = None
) -> PlannedMutation:
    return PlannedMutation(
        kind=kind, model=model, provider=provider, metric=metric, reason="because"
    )


def test_record_and_fetch_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "review.db"
    muts = [
        _mut(MutationKind.EVICT_MODEL, "m1"),
        _mut(MutationKind.DROP_PROVIDER, "m2", provider="prov"),
        _mut(MutationKind.PROMOTE, "m3", metric="coder_ci_green"),
    ]
    n = record_mutations(db, muts, applied=True, recorded_at=_T0, detail="ctx")
    assert n == 3

    records = fetch_mutations(db)
    assert len(records) == 3
    by_model = {r.model: r for r in records}
    assert by_model["m1"].provider is None and by_model["m1"].metric is None
    assert by_model["m2"].provider == "prov"
    assert by_model["m3"].metric == "coder_ci_green"
    assert all(r.applied is True and r.detail == "ctx" for r in records)


def test_empty_batch_records_nothing(tmp_path: Path) -> None:
    db = tmp_path / "review.db"
    assert record_mutations(db, [], applied=True, recorded_at=_T0) == 0
    assert fetch_mutations(db) == []


def test_fresh_db_fetch_is_empty(tmp_path: Path) -> None:
    assert fetch_mutations(tmp_path / "never.db") == []


def test_newest_first_ordering(tmp_path: Path) -> None:
    db = tmp_path / "review.db"
    record_mutations(db, [_mut(MutationKind.EVICT_MODEL, "old")], applied=True, recorded_at=_T0)
    record_mutations(
        db,
        [_mut(MutationKind.EVICT_MODEL, "new")],
        applied=True,
        recorded_at=_T0 + timedelta(hours=1),
    )
    assert [r.model for r in fetch_mutations(db)] == ["new", "old"]


def test_since_and_limit_filters(tmp_path: Path) -> None:
    db = tmp_path / "review.db"
    record_mutations(db, [_mut(MutationKind.EVICT_MODEL, "old")], applied=True, recorded_at=_T0)
    record_mutations(
        db,
        [_mut(MutationKind.EVICT_MODEL, "new")],
        applied=True,
        recorded_at=_T0 + timedelta(hours=2),
    )
    recent = fetch_mutations(db, since=_T0 + timedelta(hours=1))
    assert [r.model for r in recent] == ["new"]
    assert len(fetch_mutations(db, limit=1)) == 1


def test_shadow_and_applied_coexist(tmp_path: Path) -> None:
    db = tmp_path / "review.db"
    record_mutations(db, [_mut(MutationKind.PROMOTE, "shadowed")], applied=False, recorded_at=_T0)
    record_mutations(
        db,
        [_mut(MutationKind.PROMOTE, "live")],
        applied=True,
        recorded_at=_T0 + timedelta(minutes=1),
    )
    by_model = {r.model: r for r in fetch_mutations(db)}
    assert by_model["shadowed"].applied is False
    assert by_model["live"].applied is True


def test_render_changelog_empty_is_empty_string() -> None:
    assert render_changelog([]) == ""


def test_render_changelog_lines(tmp_path: Path) -> None:
    db = tmp_path / "review.db"
    record_mutations(
        db, [_mut(MutationKind.DROP_PROVIDER, "m", provider="p")], applied=False, recorded_at=_T0
    )
    text = render_changelog(fetch_mutations(db))
    assert text.count("\n") == 0
    assert "SHADOW" in text
    assert "drop_provider" in text
    assert "m@p" in text
    assert "because" in text
