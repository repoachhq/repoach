"""Tests for SP-CHAINPILOT-APPLY-WRITE (Phase 3d-2).

Covers the flag-gated atomic write: shadow vs enabled, the .bak backup, file-mode
preservation, the no-change short-circuit, the missing-file case, and that the
journal's per-row applied flag is faithful (advise / refused never applied=True).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ferova.review.audit_log import fetch_mutations
from ferova.review.chain_apply import apply_chain_rewrite
from ferova.review.chain_plan import ChainRewritePlan, mutation_to_edit
from ferova.review.chain_rewrite import ChainEdit, RewriteResult
from ferova.review.decision import MutationKind, PlannedMutation

_NOW = datetime(2026, 6, 26, 12, 0, tzinfo=UTC)


def _plan(
    new_content: str,
    *,
    applied: tuple[ChainEdit, ...] = (),
    cold_starts: tuple[PlannedMutation, ...] = (),
) -> ChainRewritePlan:
    return ChainRewritePlan(
        rewrite=RewriteResult(new_content=new_content, applied=applied, skipped=()),
        cold_starts=cold_starts,
    )


def _mutation(kind: MutationKind = MutationKind.EVICT_MODEL, model: str = "a/b") -> PlannedMutation:
    return PlannedMutation(kind=kind, model=model, provider=None, metric=None, reason="r")


def test_shadow_does_not_write(tmp_path: Path) -> None:
    chains = tmp_path / "chains.env"
    chains.write_text("A\n", encoding="utf-8")
    result = apply_chain_rewrite(
        _plan("B\n"),
        [_mutation()],
        db_path=tmp_path / "audit.db",
        chains_path=chains,
        recorded_at=_NOW,
        enabled=False,
    )
    assert chains.read_text() == "A\n"
    assert not (tmp_path / "chains.env.bak").exists()
    assert result.written is False
    assert result.backup_path is None
    records = fetch_mutations(tmp_path / "audit.db")
    assert records
    assert all(not r.applied for r in records)


def test_enabled_writes_atomically_with_backup(tmp_path: Path) -> None:
    chains = tmp_path / "chains.env"
    chains.write_text("A\n", encoding="utf-8")
    result = apply_chain_rewrite(
        _plan("B\n"),
        [_mutation()],
        db_path=tmp_path / "audit.db",
        chains_path=chains,
        recorded_at=_NOW,
        enabled=True,
    )
    assert chains.read_text() == "B\n"
    assert result.written is True
    assert result.backup_path is not None
    assert result.backup_path.read_text() == "A\n"


def test_enabled_write_preserves_file_mode(tmp_path: Path) -> None:
    chains = tmp_path / "chains.env"
    chains.write_text("A\n", encoding="utf-8")
    chains.chmod(0o644)
    apply_chain_rewrite(
        _plan("B\n"),
        [_mutation()],
        db_path=tmp_path / "audit.db",
        chains_path=chains,
        recorded_at=_NOW,
        enabled=True,
    )
    assert chains.read_text() == "B\n"
    assert (chains.stat().st_mode & 0o777) == 0o644


def test_journal_applied_is_faithful(tmp_path: Path) -> None:
    chains = tmp_path / "chains.env"
    chains.write_text("A\n", encoding="utf-8")
    db = tmp_path / "audit.db"
    landed = _mutation(MutationKind.EVICT_MODEL, "m1/v")
    advise = _mutation(MutationKind.ADVISE, "x/y")
    cold = _mutation(MutationKind.COLD_START, "z-ai/glm-5.2")
    plan = _plan("B\n", applied=(mutation_to_edit(landed),), cold_starts=(cold,))

    apply_chain_rewrite(
        plan, [landed, advise], db_path=db, chains_path=chains, recorded_at=_NOW, enabled=True
    )

    by_model = {r.model: r for r in fetch_mutations(db)}
    assert by_model["m1/v"].applied is True
    assert by_model["x/y"].applied is False
    assert by_model["z-ai/glm-5.2"].applied is True


def test_enabled_no_change_does_not_write(tmp_path: Path) -> None:
    chains = tmp_path / "chains.env"
    chains.write_text("A\n", encoding="utf-8")
    result = apply_chain_rewrite(
        _plan("A\n"),
        [_mutation()],
        db_path=tmp_path / "audit.db",
        chains_path=chains,
        recorded_at=_NOW,
        enabled=True,
    )
    assert result.written is False
    assert result.backup_path is None
    assert not (tmp_path / "chains.env.bak").exists()
    assert all(not r.applied for r in fetch_mutations(tmp_path / "audit.db"))


def test_journaled_count_covers_mutations_and_cold_starts(tmp_path: Path) -> None:
    chains = tmp_path / "chains.env"
    chains.write_text("A\n", encoding="utf-8")
    cold = (_mutation(kind=MutationKind.COLD_START, model="z-ai/glm-5.2"),)
    result = apply_chain_rewrite(
        _plan("B\n", cold_starts=cold),
        [_mutation(), _mutation(model="c/d")],
        db_path=tmp_path / "audit.db",
        chains_path=chains,
        recorded_at=_NOW,
        enabled=False,
    )
    assert result.journaled == 3


def test_missing_chains_file_no_write(tmp_path: Path) -> None:
    chains = tmp_path / "chains.env"
    result = apply_chain_rewrite(
        _plan("B\n"),
        [_mutation()],
        db_path=tmp_path / "audit.db",
        chains_path=chains,
        recorded_at=_NOW,
        enabled=True,
    )
    assert not chains.exists()
    assert result.written is False
    assert all(not r.applied for r in fetch_mutations(tmp_path / "audit.db"))
