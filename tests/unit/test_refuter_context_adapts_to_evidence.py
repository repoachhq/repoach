"""The refuter's context adapts to the evidence, up to a ceiling (SP-REFUTER-VETO-NAMES-REAL-IDENTIFIERS G5).

``[gates].refuter_context_tokens`` is the floor. A finding's context
grows to what its cited file, the hits of the claim's identifiers and
the diff hunks need, up to ``[gates].refuter_context_ceiling``. Past the
ceiling the finding is deferred with ``evidence exceeds the ceiling (<n>
tokens needed, ceiling <c>)``, never judged on a silent partial, and the
context used is recorded as ``budget_used``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from repoach.gates.config import (
    REFUTER_CONTEXT_CEILING_DEFAULT,
    REFUTER_CONTEXT_TOKENS_FLOOR,
    GatesConfig,
    GatesConfigError,
    load_gates_config,
    resolve_refuter_budget,
    resolve_refuter_context_ceiling,
)
from repoach.review.findings import (
    Finding,
    FindingStatus,
    fetch_findings,
    init_findings_schema,
    record_finding,
    record_verification_attempt,
    update_finding_status,
)
from repoach.review.head_reverify import reverify_older_findings
from repoach.review.refuter import (
    DEFERRAL_REASONS,
    EVIDENCE_EXCEEDS_CEILING,
    judge_finding,
    judge_findings_for_pr,
)
from repoach.review.refuter_evidence import estimate_tokens
from repoach.review.report import _from_ledger
from tests.refuter_fakes import blocking_finding
from tests.refuter_fakes import git_tree as _git_tree
from tests.refuter_fakes import judge_reply as _reply

CITED = "src/big.py"
HEAD_A = "a" * 40
HEAD_B = "b" * 40


def _big_source(tokens: int) -> str:
    rows: list[str] = ["def guarded(value):\n", "    return wanted_symbol(value)\n"]
    n = 0
    while estimate_tokens("".join(rows)) < tokens:
        rows.append(f"row_{n} = '{'x' * 90}'\n")
        n += 1
    return "".join(rows)


def _finding(claim: str = "`wanted_symbol` leaks the value", *, head: str = HEAD_A) -> Finding:
    return blocking_finding(claim, file=CITED, line=2, head=head, pr_number=7)


def _tree(tmp_path: Path, cited_tokens: int) -> Path:
    other = "def wanted_symbol(value):\n    return '***'\n"
    return _git_tree(tmp_path, {CITED: _big_source(cited_tokens), "src/other.py": other})


def test_context_grows_past_the_floor_to_serve_a_large_cited_file(tmp_path: Path) -> None:
    root = _tree(tmp_path, 60_000)
    prompts: list[str] = []
    judgement = judge_finding(
        _finding(),
        repo_root=root,
        judge=lambda p: prompts.append(p) or _reply("refuted", "wanted_symbol masks the value"),
    )
    assert judgement.status is FindingStatus.REFUTED
    assert REFUTER_CONTEXT_TOKENS_FLOOR < judgement.budget_used <= REFUTER_CONTEXT_CEILING_DEFAULT
    assert judgement.truncated == ()
    assert "src/other.py: lines 1-2" in prompts[0]


def test_small_evidence_keeps_the_floor(tmp_path: Path) -> None:
    root = _tree(tmp_path, 200)
    judgement = judge_finding(
        _finding(), repo_root=root, judge=lambda _p: _reply("verified", "it leaks")
    )
    assert judgement.status is FindingStatus.VERIFIED
    assert judgement.budget_used == REFUTER_CONTEXT_TOKENS_FLOOR


def test_cited_file_over_the_ceiling_defers_with_the_named_reason(tmp_path: Path) -> None:
    root = _tree(tmp_path, 60_000)
    calls: list[str] = []
    judgement = judge_finding(
        _finding(),
        repo_root=root,
        judge=lambda p: calls.append(p) or _reply("refuted", "fine"),
        context_ceiling=REFUTER_CONTEXT_TOKENS_FLOOR,
    )
    assert calls == []
    assert judgement.status is FindingStatus.PROPOSED
    prefix = "evidence exceeds the ceiling ("
    assert judgement.reason.startswith(prefix)
    assert judgement.reason.endswith(f" tokens needed, ceiling {REFUTER_CONTEXT_TOKENS_FLOOR})")
    needed = int(judgement.reason[len(prefix) :].split(" ", 1)[0])
    assert needed > 60_000
    assert judgement.budget_used == REFUTER_CONTEXT_TOKENS_FLOOR
    assert EVIDENCE_EXCEEDS_CEILING in DEFERRAL_REASONS


def test_ceiling_deferral_is_named_by_the_gate_and_records_budget_used(tmp_path: Path) -> None:
    root = _tree(tmp_path / "repo", 60_000)
    db = tmp_path / "review.db"
    init_findings_schema(db)
    row_id = record_finding(db, _finding())
    report = reverify_older_findings(
        db,
        pr_number=7,
        repo_root=root,
        head_sha=HEAD_B,
        ci_green=True,
        config=GatesConfig.builtin_default(),
        max_judged=5,
        judge_factory=lambda: lambda _p: _reply("refuted", "fine"),
        context_ceiling=REFUTER_CONTEXT_TOKENS_FLOOR,
    )
    assert report.model_calls == 0
    [reason] = report.reasons()
    assert "unresolved: needs judgement (evidence exceeds the ceiling (" in reason
    row = next(f for f in fetch_findings(db, 7) if f.id == row_id)
    assert row.budget_used == REFUTER_CONTEXT_TOKENS_FLOOR
    assert row.status is FindingStatus.PROPOSED


def test_verdict_records_budget_used_on_the_ledger_row(tmp_path: Path) -> None:
    root = _tree(tmp_path / "repo", 60_000)
    db = tmp_path / "review.db"
    init_findings_schema(db)
    row_id = record_finding(db, _finding(head=HEAD_B))
    report = judge_findings_for_pr(
        db,
        pr_number=7,
        repo_root=root,
        head_sha=HEAD_B,
        judge_factory=lambda: lambda _p: _reply("refuted", "wanted_symbol masks the value"),
        context_tokens=REFUTER_CONTEXT_TOKENS_FLOOR,
        context_ceiling=REFUTER_CONTEXT_CEILING_DEFAULT,
    )
    [outcome] = report.outcomes
    assert outcome.state == "refuted"
    assert outcome.budget_used > REFUTER_CONTEXT_TOKENS_FLOOR
    assert outcome.as_json()["refutation"]["budget_used"] == outcome.budget_used
    row = next(f for f in fetch_findings(db, 7) if f.id == row_id)
    assert row.budget_used == outcome.budget_used


def test_ceiling_setting_resolves_and_refuses_a_ceiling_below_the_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REPOACH_REFUTER_CONTEXT_CEILING", raising=False)
    (tmp_path / "repoach.toml").write_text(
        "[gates]\nrefuter_context_ceiling = 120000\n", encoding="utf-8"
    )
    assert resolve_refuter_context_ceiling(tmp_path) == (120_000, "repoach.toml")
    monkeypatch.setenv("REPOACH_REFUTER_CONTEXT_CEILING", "90000")
    assert resolve_refuter_context_ceiling(tmp_path) == (
        90_000,
        "env REPOACH_REFUTER_CONTEXT_CEILING",
    )
    (tmp_path / "repoach.toml").write_text(
        "[gates]\nrefuter_context_tokens = 80000\nrefuter_context_ceiling = 60000\n",
        encoding="utf-8",
    )
    with pytest.raises(GatesConfigError, match="refuter_context_ceiling"):
        load_gates_config(tmp_path)


def test_report_entry_of_a_ledger_row_carries_its_budget_used() -> None:
    row = _finding(head=HEAD_B).model_copy(
        update={
            "id": 3,
            "status": FindingStatus.REFUTED,
            "verification_method": "refuter",
            "verification_result": "wanted_symbol masks the value",
            "budget_used": 65_829,
        }
    )
    payload = _from_ledger(row).as_json()
    assert payload["judge"] == "refuter"
    assert payload["refutation"]["budget_used"] == 65_829


@pytest.mark.parametrize("value", ["true", '"1"', "1.5"], ids=["bool", "string", "float"])
def test_ceiling_refuses_a_bool_or_a_non_integer(tmp_path: Path, value: str) -> None:
    (tmp_path / "repoach.toml").write_text(
        f"[gates]\nrefuter_context_ceiling = {value}\n", encoding="utf-8"
    )
    with pytest.raises(GatesConfigError, match="refuter_context_ceiling"):
        load_gates_config(tmp_path)


@pytest.mark.parametrize("env_value", ["abc", "100", ""], ids=["non-digit", "below-floor", "empty"])
def test_ceiling_env_invalid_falls_back_to_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_value: str
) -> None:
    (tmp_path / "repoach.toml").write_text(
        "[gates]\nrefuter_context_ceiling = 120000\n", encoding="utf-8"
    )
    monkeypatch.setenv("REPOACH_REFUTER_CONTEXT_CEILING", env_value)
    assert resolve_refuter_context_ceiling(tmp_path) == (120_000, "repoach.toml")


def test_budget_raises_a_ceiling_below_the_floor_and_takes_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REPOACH_REFUTER_CONTEXT_TOKENS", raising=False)
    monkeypatch.setenv("REPOACH_REFUTER_CONTEXT_CEILING", "50000")
    (tmp_path / "repoach.toml").write_text(
        "[gates]\nrefuter_context_tokens = 60000\n", encoding="utf-8"
    )
    assert resolve_refuter_budget(tmp_path) == (60_000, 60_000)
    assert resolve_refuter_budget(tmp_path, floor=45_000, ceiling=90_000) == (45_000, 90_000)


def test_budget_used_column_is_added_to_a_pre_existing_pr_findings_table(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    init_findings_schema(db)
    row_id = record_finding(db, _finding())
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE pr_findings DROP COLUMN budget_used")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(pr_findings)")}
    assert "budget_used" not in columns
    init_findings_schema(db)
    row = next(f for f in fetch_findings(db, 7) if f.id == row_id)
    assert row.budget_used == 0


def test_none_budget_used_keeps_the_recorded_value(tmp_path: Path) -> None:
    db = tmp_path / "review.db"
    init_findings_schema(db)
    row_id = record_finding(db, _finding())
    record_verification_attempt(
        db, row_id, method="m", result="r", checked_at_sha="x", budget_used=512
    )
    record_verification_attempt(db, row_id, method="m", result="r2", checked_at_sha="x")
    assert next(f for f in fetch_findings(db, 7) if f.id == row_id).budget_used == 512
    assert update_finding_status(db, row_id, FindingStatus.VERIFIED, verification_method="m")
    assert next(f for f in fetch_findings(db, 7) if f.id == row_id).budget_used == 512
