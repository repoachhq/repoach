"""An unknown ledger value is a named refusal, not a traceback (SP-LEDGER-FORMAT-IS-VERSIONED).

The ledger a review run writes is read by an older release's merge gate.
Run 43995248874 crashed on ``ValueError: 'note' is not a valid Severity``.
These tests feed ledgers of a newer shape to this reader: a newer format,
an enum value it does not define, a declared column the file lacks. Each
must be ``SKIP_LEDGER_SKEW`` (exit 5) with the writer and reader versions,
and the committed snapshot must hold ``LEDGER_FORMAT`` to the live shape.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from enum import StrEnum
from pathlib import Path
from typing import get_args

import pytest
from typer.testing import CliRunner

import repoach
from repoach.cli import review_cmds
from repoach.core.config import get_settings
from repoach.review import auto_merge, ledger_format
from repoach.review.auto_merge import (
    OUTCOME_SKIP_LEDGER_SKEW,
    evaluate_merge_gate,
    merge_exit_code,
    run_auto_merge,
)
from repoach.review.coder_findings import CoderFindingsResult
from repoach.review.findings import (
    ClaimType,
    Finding,
    FindingStatus,
    Severity,
    fetch_findings,
    record_finding,
)
from repoach.review.generated_store import fetch_generated_checks
from repoach.review.generated_types import GeneratedState, ScriptRef
from repoach.review.ledger_format import (
    LEDGER_FORMAT,
    LedgerSkewError,
    _of_enum,
    check,
    demo_ledger,
    snapshot,
    stamp,
)
from repoach.review.merge_wait import MERGE_ATTEMPTS_MARKER, MergeAttemptsComment
from repoach.review.orchestrator import ReviewTeamOrchestrator, TeamOutcome
from tests.merge_wait_fakes import (
    HEAD_259,
    FixturePR,
    MergeWaitFixtureForge,
    ci_runs,
    seed_review,
)
from tests.unit.test_review_team import _StubGitHubForge
from tests.unit.test_review_transport_error_verdict import _install_unreachable_reviewers

REPO_ROOT = Path(__file__).resolve().parents[2]
FORMATS = REPO_ROOT / "tests" / "fixtures" / "ledger_format"
SKEWED_SQL = REPO_ROOT / "tests" / "fixtures" / "ledger_skew" / "format_2_with_later_severity.sql"
READER = repoach.__version__
N_DEMO = len(ClaimType) + len(Severity) + len(FindingStatus)

PR_FINDINGS_AT_5CEA5A7 = [
    "id INTEGER",
    "pr_number INTEGER",
    "head_sha VARCHAR",
    "round INTEGER",
    "finder VARCHAR",
    "claim_type VARCHAR",
    "severity VARCHAR",
    "file VARCHAR",
    "line_start INTEGER",
    "line_end INTEGER",
    "claim VARCHAR",
    "evidence_pointer VARCHAR",
    "status VARCHAR",
    "verification_method VARCHAR",
    "verification_result VARCHAR",
    "checked_at_sha VARCHAR",
    "verify_attempts INTEGER",
    "last_seen_at VARCHAR",
    "last_run_id VARCHAR",
    "budget_used INTEGER",
]
"""``pr_findings`` on ``origin/develop`` ``5cea5a7``: the id and 19 data columns."""

OTHER_TABLES_AT_5CEA5A7 = {
    "pr_review_integrity": [
        "id INTEGER",
        "pr_number INTEGER",
        "head_sha VARCHAR",
        "n_reviewers INTEGER",
        "n_unparsed INTEGER",
    ],
    "pr_spec_coverage": [
        "id INTEGER",
        "pr_number INTEGER",
        "head_sha VARCHAR",
        "spec_id VARCHAR",
        "n_promised INTEGER",
        "n_present INTEGER",
        "missing VARCHAR",
        "covered INTEGER",
        "planned VARCHAR",
        "draft_refusals VARCHAR",
        "rule_version INTEGER",
        "contract VARCHAR",
        "origins VARCHAR",
        "report VARCHAR",
        "resolution VARCHAR",
        "batch VARCHAR",
    ],
    "pr_merges": [
        "id INTEGER",
        "pr_number INTEGER",
        "outcome VARCHAR",
        "base_ref VARCHAR",
        "head_ref VARCHAR",
        "merged_sha VARCHAR",
        "notes VARCHAR",
        "created_at DATETIME",
        "head_sha VARCHAR",
        "pending_checks VARCHAR",
        "trigger VARCHAR",
        "rule_version VARCHAR",
    ],
    "generated_checks": [
        "id INTEGER",
        "pr_number INTEGER",
        "head_sha VARCHAR",
        "path VARCHAR",
        "argv TEXT",
        "exit_code INTEGER",
        "duration_ms INTEGER",
        "output_tail TEXT",
        "state VARCHAR",
        "script_ref VARCHAR",
        "runs_for_path INTEGER",
        "sources_changed TEXT",
        "producer_inputs_changed TEXT",
        "artefact_sha256 VARCHAR",
        "checks_ms INTEGER",
        "overhead_ms INTEGER",
        "recorded_at VARCHAR",
    ],
}
"""The four other tables the gate reads, as ``origin/develop`` declares them.

``generated_checks`` joined in 0.4: ``generated_store.py`` is unchanged by
this branch against ``origin/develop`` ``92cd241``, so these are the
columns ``develop`` already writes.
"""

OTHER_ENUMS_AT_DEVELOP = {
    "pr_spec_coverage.contract": ["plan", "acceptance-criteria", "none", "unavailable", ""],
    "generated_checks.state": [
        "generated_artefact_verified",
        "generated_artefact_stale",
        "generated_artefact_no_source_changed",
        "generated_check_absent",
        "skipped (cost)",
        "generated_check_not_run",
    ],
    "generated_checks.script_ref": ["head", "base"],
}
"""The vocabularies outside ``pr_findings`` the gate decodes, as ``origin/develop`` defines them."""

JSON_AT_DEVELOP = {
    "generated_checks.argv": "list[str]",
    "generated_checks.sources_changed": "list[str]",
    "generated_checks.producer_inputs_changed": "list[str]",
    "pr_merges.pending_checks": "list[str] or empty",
    "pr_spec_coverage.origins": "dict[str, str] or empty",
}
"""The JSON encodings ``origin/develop``'s writers produce.

``record_generated_checks`` writes ``json.dumps(list(...))`` of str tuples,
``record_merge`` ``json.dumps(list(pending_checks))`` into a column added
nullable, ``record_spec_coverages`` ``json.dumps(origins)`` of a
``dict[str, str]`` or ``""``.
"""


def _sql(db: Path, *statements: str) -> None:
    with sqlite3.connect(db) as conn:
        for statement in statements:
            conn.execute(statement)


def _load_skewed(tmp_path: Path) -> Path:
    db = tmp_path / "skewed.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(SKEWED_SQL.read_text(encoding="utf-8"))
    return db


_DEMO_CACHE: dict[str, Path] = {}


@pytest.fixture(scope="module", autouse=True)
def _demo_once(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Write the demo ledger once; each test works on its own copy."""
    db = tmp_path_factory.mktemp("demo") / "demo.db"
    demo_ledger(db, 259, written_by="0.2.0.dev14+gabc1234")
    _DEMO_CACHE["db"] = db


def _demo(tmp_path: Path) -> Path:
    db = tmp_path / "demo.db"
    shutil.copyfile(_DEMO_CACHE["db"], db)
    return db


def _unstamped(db: Path) -> None:
    _sql(db, "DROP TABLE ledger_meta")


def test_live_snapshot_matches_the_committed_format() -> None:
    committed = json.loads((FORMATS / f"{LEDGER_FORMAT}.json").read_text(encoding="utf-8"))
    assert snapshot() == committed
    assert sorted(p.name for p in FORMATS.glob("*.json")) == [
        f"{n}.json" for n in range(1, LEDGER_FORMAT + 1)
    ]


class _SeverityWithLater(StrEnum):
    BLOCKING = "blocking"
    ADVISORY = "advisory"
    NOTE = "note"
    LATER = "later"


def test_t1_an_unbumped_enum_value_turns_the_snapshot_red(monkeypatch: pytest.MonkeyPatch) -> None:
    claim_type, _severity, *others = ledger_format.ENUM_COLUMNS
    widened = (claim_type, _of_enum(claim_type.table, "severity", _SeverityWithLater), *others)
    monkeypatch.setattr(ledger_format, "ENUM_COLUMNS", widened)
    committed = json.loads((FORMATS / f"{LEDGER_FORMAT}.json").read_text(encoding="utf-8"))
    assert snapshot() != committed


def test_snapshot_cli_prints_the_committed_file() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "repoach.review.ledger_format", "snapshot"],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    committed = (FORMATS / f"{LEDGER_FORMAT}.json").read_text(encoding="utf-8")
    assert json.loads(completed.stdout) == json.loads(committed)


def test_t2_a_newer_format_is_refused_with_both_versions(tmp_path: Path) -> None:
    db = _load_skewed(tmp_path)
    with pytest.raises(LedgerSkewError) as raised:
        check(db, 259)
    assert str(raised.value) == (
        f"format 2 is newer than this reader's 1 "
        f"(written by repoach 9.9.9; this gate is repoach {READER})"
    )
    assert raised.value.needed_format == 2
    assert raised.value.reason == (
        f"ledger skew: format 2 is newer than this reader's 1 "
        f"(written by repoach 9.9.9; this gate is repoach {READER}); "
        "a release carrying format 2 is needed before this lane can judge this head"
    )


@pytest.mark.parametrize("writer_format", [3, 4, 17])
def test_t2_a_newer_format_is_refused_even_when_every_value_is_known(
    tmp_path: Path, writer_format: int
) -> None:
    db = _demo(tmp_path)
    _sql(db, f"UPDATE ledger_meta SET value = '{writer_format}' WHERE key = 'format'")
    with pytest.raises(LedgerSkewError) as raised:
        check(db, 259)
    assert str(raised.value).startswith(f"format {writer_format} is newer than this reader's 1 ")
    assert raised.value.needed_format == writer_format
    assert raised.value.reason.endswith(
        f"; a release carrying format {writer_format} is needed before this lane can judge this head"
    )


@pytest.mark.parametrize(
    ("column", "enum_name"),
    [("severity", "Severity"), ("status", "FindingStatus"), ("claim_type", "ClaimType")],
)
def test_t3_an_unknown_enum_value_at_the_same_format_is_named(
    tmp_path: Path, column: str, enum_name: str
) -> None:
    db = _demo(tmp_path)
    _sql(
        db,
        f"UPDATE pr_findings SET {column} = 'later' WHERE id = (SELECT max(id) FROM pr_findings)",
    )
    with pytest.raises(LedgerSkewError) as raised:
        check(db, 259)
    assert str(raised.value) == (
        f"pr_findings.{column}='later' is not a {enum_name} this reader knows "
        f"(written by repoach 0.2.0.dev14+gabc1234; this gate is repoach {READER})"
    )
    assert raised.value.needed_format == LEDGER_FORMAT + 1


@pytest.mark.parametrize(
    ("table", "column", "vocabulary"),
    [
        ("pr_spec_coverage", "contract", "ContractKind"),
        ("generated_checks", "state", "GeneratedState"),
        ("generated_checks", "script_ref", "ScriptRef"),
    ],
)
def test_t3_an_unknown_value_of_another_gate_table_is_named(
    tmp_path: Path, table: str, column: str, vocabulary: str
) -> None:
    db = _demo(tmp_path)
    _sql(db, f"UPDATE {table} SET {column} = 'later' WHERE id = (SELECT max(id) FROM {table})")
    with pytest.raises(LedgerSkewError) as raised:
        check(db, 259)
    assert str(raised.value) == (
        f"{table}.{column}='later' is not a {vocabulary} this reader knows "
        f"(written by repoach 0.2.0.dev14+gabc1234; this gate is repoach {READER})"
    )
    _sql(db, f"UPDATE {table} SET pr_number = 7")
    assert check(db, 259).rows_checked == N_DEMO


def test_t3_an_unknown_generated_state_is_named_where_its_decoder_raises(tmp_path: Path) -> None:
    db = _demo(tmp_path)
    _sql(db, "UPDATE generated_checks SET state = 'generated_artefact_later' WHERE id = 1")
    with pytest.raises(
        ValueError, match="'generated_artefact_later' is not a valid GeneratedState"
    ):
        fetch_generated_checks(db, 259, "0" * 40)
    with pytest.raises(
        LedgerSkewError, match=r"^generated_checks\.state='generated_artefact_later' "
    ):
        check(db, 259)


@pytest.mark.parametrize(
    ("table", "column", "stored", "shape"),
    [
        ("generated_checks", "argv", "make", "list[str]"),
        ("generated_checks", "argv", '"make"', "list[str]"),
        ("generated_checks", "argv", "[1]", "list[str]"),
        ("generated_checks", "argv", "", "list[str]"),
        ("generated_checks", "sources_changed", "{}", "list[str]"),
        ("generated_checks", "producer_inputs_changed", "a,b", "list[str]"),
        ("pr_merges", "pending_checks", "ci", "list[str] or empty"),
        ("pr_merges", "pending_checks", '{"ci": 1}', "list[str] or empty"),
        ("pr_spec_coverage", "origins", "[]", "dict[str, str] or empty"),
        ("pr_spec_coverage", "origins", '{"t": 1}', "dict[str, str] or empty"),
    ],
)
def test_t3_a_json_column_value_off_its_declared_shape_is_named(
    tmp_path: Path, table: str, column: str, stored: str, shape: str
) -> None:
    db = _demo(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute(
            f"UPDATE {table} SET {column} = ? WHERE id = (SELECT min(id) FROM {table})", (stored,)
        )
    with pytest.raises(LedgerSkewError) as raised:
        check(db, 259)
    assert str(raised.value) == (
        f"{table}.{column}={stored!r} is not JSON {shape} this reader knows "
        f"(written by repoach 0.2.0.dev14+gabc1234; this gate is repoach {READER})"
    )
    assert raised.value.needed_format == LEDGER_FORMAT + 1
    _sql(db, f"UPDATE {table} SET pr_number = 7")
    assert check(db, 259).rows_checked == N_DEMO


@pytest.mark.parametrize(
    ("table", "column", "stored"),
    [
        ("pr_merges", "pending_checks", None),
        ("pr_merges", "pending_checks", ""),
        ("pr_spec_coverage", "origins", ""),
    ],
)
def test_t3_an_empty_value_where_the_column_allows_none_proceeds(
    tmp_path: Path, table: str, column: str, stored: str | None
) -> None:
    db = _demo(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute(f"UPDATE {table} SET {column} = ?", (stored,))
    assert check(db, 259).rows_checked == N_DEMO


def test_t3_an_argv_that_is_not_json_is_named_where_its_decoder_raises(tmp_path: Path) -> None:
    db = _demo(tmp_path)
    _sql(db, "UPDATE generated_checks SET argv = 'make' WHERE id = 1")
    with pytest.raises(json.JSONDecodeError):
        fetch_generated_checks(db, 259, "0" * 40)
    with pytest.raises(LedgerSkewError, match=r"^generated_checks\.argv='make' is not JSON "):
        check(db, 259)


def test_t3_an_unknown_value_of_another_pr_is_not_this_pr_s(tmp_path: Path) -> None:
    db = _demo(tmp_path)
    _sql(
        db,
        "UPDATE pr_findings SET severity = 'later' WHERE id = 1",
        "UPDATE pr_findings SET pr_number = 7 WHERE id = 1",
    )
    assert check(db, 259).rows_checked == N_DEMO - 1
    with pytest.raises(LedgerSkewError, match="severity='later'"):
        check(db, 7)


def test_t4_a_declared_column_the_file_lacks_is_named_before_any_row_is_read(
    tmp_path: Path,
) -> None:
    db = _demo(tmp_path)
    _sql(db, "ALTER TABLE pr_findings DROP COLUMN budget_used")
    with pytest.raises(LedgerSkewError) as raised:
        check(db, 259)
    assert str(raised.value) == (
        "pr_findings.budget_used missing "
        f"(written by repoach 0.2.0.dev14+gabc1234; this gate is repoach {READER})"
    )
    columns = {row[1] for row in sqlite3.connect(db).execute("PRAGMA table_info(pr_findings)")}
    assert "budget_used" not in columns


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("pr_review_integrity", "n_unparsed"),
        ("pr_spec_coverage", "batch"),
        ("pr_merges", "notes"),
        ("generated_checks", "checks_ms"),
    ],
)
def test_t4_every_gate_table_is_checked(tmp_path: Path, table: str, column: str) -> None:
    db = _demo(tmp_path)
    _sql(db, f"ALTER TABLE {table} DROP COLUMN {column}")
    with pytest.raises(LedgerSkewError, match=rf"^{table}\.{column} missing "):
        check(db, 259)


def test_t4_an_unstamped_ledger_lacking_a_column_this_reader_heals_proceeds(
    tmp_path: Path,
) -> None:
    db = _demo(tmp_path)
    _unstamped(db)
    _sql(db, "ALTER TABLE pr_findings DROP COLUMN budget_used")
    assert check(db, 259).rows_checked == N_DEMO

    _sql(db, "ALTER TABLE pr_findings DROP COLUMN claim")
    with pytest.raises(LedgerSkewError) as raised:
        check(db, 259)
    assert str(raised.value) == (
        f"pr_findings.claim missing (written by repoach (unstamped); this gate is repoach {READER})"
    )


def test_t4_an_unstamped_ledger_lacking_the_healed_contract_column_proceeds(
    tmp_path: Path,
) -> None:
    db = _demo(tmp_path)
    _unstamped(db)
    _sql(db, "ALTER TABLE pr_spec_coverage DROP COLUMN contract")
    assert check(db, 259).rows_checked == N_DEMO


def _skewed_gate_fixture(tmp_path: Path) -> tuple[MergeWaitFixtureForge, Path]:
    fx = MergeWaitFixtureForge(
        [FixturePR(259, HEAD_259, "docs/sp-approved-spec-without-plan", check_runs=ci_runs())]
    )
    db = tmp_path / "review.db"
    seed_review(db, 259, HEAD_259)
    stamp(db, written_by="0.2.0.dev14+gabc1234")
    record_finding(
        db,
        Finding(
            pr_number=259,
            head_sha=HEAD_259,
            round=1,
            finder="sentinel",
            claim_type=ClaimType.BROKEN_BEHAVIOR,
            severity=Severity.ADVISORY,
            file="README.md",
            line_start=1,
            line_end=1,
            claim="a severity a newer writer defines",
            evidence_pointer="README.md:1",
        ),
    )
    _sql(db, "UPDATE pr_findings SET severity = 'later'")
    return fx, db


SKEW_REASON = (
    "ledger skew: pr_findings.severity='later' is not a Severity this reader knows "
    f"(written by repoach 0.2.0.dev14+gabc1234; this gate is repoach {READER}); "
    "a release carrying format 2 is needed before this lane can judge this head"
)


def test_t5_run_auto_merge_records_a_named_skip(tmp_path: Path) -> None:
    fx, db = _skewed_gate_fixture(tmp_path)
    with pytest.raises(ValueError, match="'later' is not a valid Severity"):
        fetch_findings(db, 259)

    result = run_auto_merge(
        259,
        forge=fx.forge,
        db_path=db,
        repo_root=REPO_ROOT,
        wait_seconds=0,
        trigger="merge-on-ci:1",
        clock=fx.clock,
    )

    assert result.outcome == OUTCOME_SKIP_LEDGER_SKEW
    assert result.notes == SKEW_REASON
    assert merge_exit_code(result.outcome) == 5
    assert fx.merges == []
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT outcome, notes FROM pr_merges WHERE pr_number = 259").fetchall()
    assert rows == [(OUTCOME_SKIP_LEDGER_SKEW, SKEW_REASON)]
    comment = fx.attempts_comment()
    assert comment is not None
    assert MERGE_ATTEMPTS_MARKER in str(comment["body"])
    block = re.search(r"```json\n(.*?)\n```", str(comment["body"]), re.S)
    assert block is not None
    attempts = MergeAttemptsComment.model_validate_json(block.group(1)).attempts
    assert [(a.outcome, a.reason) for a in attempts] == [(OUTCOME_SKIP_LEDGER_SKEW, SKEW_REASON)]


def test_t5_an_unknown_generated_state_is_a_named_skip_of_run_auto_merge(tmp_path: Path) -> None:
    fx = MergeWaitFixtureForge(
        [FixturePR(259, HEAD_259, "docs/sp-approved-spec-without-plan", check_runs=ci_runs())]
    )
    db = tmp_path / "review.db"
    seed_review(db, 259, HEAD_259)
    shutil.copyfile(_DEMO_CACHE["db"], tmp_path / "demo.db")
    _sql(
        tmp_path / "demo.db",
        "UPDATE generated_checks SET state = 'generated_artefact_later' WHERE id = 1",
    )
    with sqlite3.connect(db) as conn:
        conn.execute("ATTACH DATABASE ? AS demo", (str(tmp_path / "demo.db"),))
        conn.execute("CREATE TABLE generated_checks AS SELECT * FROM demo.generated_checks")
        conn.execute("UPDATE generated_checks SET pr_number = 259, head_sha = ?", (HEAD_259,))
    stamp(db, written_by="0.2.0.dev14+gabc1234")
    with pytest.raises(ValueError, match="is not a valid GeneratedState"):
        fetch_generated_checks(db, 259, HEAD_259)

    result = run_auto_merge(
        259,
        forge=fx.forge,
        db_path=db,
        repo_root=REPO_ROOT,
        wait_seconds=0,
        trigger="merge-on-ci:1",
        clock=fx.clock,
    )

    assert result.outcome == OUTCOME_SKIP_LEDGER_SKEW
    assert result.notes.startswith(
        "ledger skew: generated_checks.state='generated_artefact_later' is not a GeneratedState "
    )
    assert merge_exit_code(result.outcome) == 5
    assert fx.merges == []


def _cli_settings(monkeypatch: pytest.MonkeyPatch, fx: MergeWaitFixtureForge, db: Path) -> None:
    settings = get_settings().model_copy(update={"db_path": db})
    monkeypatch.setattr(auto_merge, "get_settings", lambda: settings)
    monkeypatch.setattr(auto_merge, "select_forge", lambda **_k: fx.forge)
    monkeypatch.chdir(REPO_ROOT)


def test_t5_review_merge_exits_5_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx, db = _skewed_gate_fixture(tmp_path)
    _cli_settings(monkeypatch, fx, db)
    result = CliRunner().invoke(review_cmds.review_app, ["merge", "259"])
    assert result.exit_code == 5, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Traceback" not in result.output
    payload = json.loads(result.stdout)
    assert payload["outcome"] == OUTCOME_SKIP_LEDGER_SKEW
    assert payload["notes"] == SKEW_REASON


def test_t5_review_gate_prints_the_line_and_exits_5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx, db = _skewed_gate_fixture(tmp_path)
    _cli_settings(monkeypatch, fx, db)
    result = CliRunner().invoke(review_cmds.review_app, ["gate", "259"])
    assert result.exit_code == 5, result.output
    assert "Traceback" not in result.output
    payload = json.loads(result.stdout)
    assert payload["outcome"] == OUTCOME_SKIP_LEDGER_SKEW
    assert payload["merge"] is False
    assert payload["reasons"] == [SKEW_REASON]
    assert payload["ledger"] is None
    assert f"{OUTCOME_SKIP_LEDGER_SKEW} — {SKEW_REASON}" in result.stderr.splitlines()


def test_review_gate_names_a_readable_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx = MergeWaitFixtureForge(
        [FixturePR(259, HEAD_259, "docs/sp-approved-spec-without-plan", check_runs=ci_runs())]
    )
    db = tmp_path / "review.db"
    seed_review(db, 259, HEAD_259)
    stamp(db, written_by="0.1.0")
    settings = get_settings().model_copy(update={"db_path": db})
    monkeypatch.setattr(auto_merge, "get_settings", lambda: settings)
    evaluation = evaluate_merge_gate(
        259, forge=fx.forge, db_path=db, repo_root=REPO_ROOT, wait_seconds=0
    )
    assert evaluation.ledger is not None
    assert evaluation.ledger.summary_line() == "ledger: format 1, written by repoach 0.1.0"
    assert evaluation.ci_gate.outcome_tag != OUTCOME_SKIP_LEDGER_SKEW


def test_t6_an_unstamped_ledger_with_known_values_proceeds(tmp_path: Path) -> None:
    db = _demo(tmp_path)
    _unstamped(db)
    result = check(db, 259, reader="0.1.0")
    assert (result.format, result.written_by, result.written_at) == (0, "", "")
    assert result.rows_checked == N_DEMO
    assert result.summary_line() == "ledger: unstamped (pre-format), values known"


def test_t6_a_stamped_older_format_proceeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _demo(tmp_path)
    monkeypatch.setattr(ledger_format, "LEDGER_FORMAT", 2)
    result = check(db, 259)
    assert result.format == 1
    assert result.summary_line() == (
        "ledger: format 1, written by repoach 0.2.0.dev14+gabc1234 (older than this reader's 2)"
    )


def test_t6_rows_checked_counts_this_pr_only_and_an_empty_pr_proceeds(tmp_path: Path) -> None:
    db = _demo(tmp_path)
    _sql(db, "UPDATE pr_findings SET pr_number = 7 WHERE id <= 5")
    assert check(db, 259).rows_checked == N_DEMO - 5
    assert check(db, 7).rows_checked == 5
    assert check(db, 8).rows_checked == 0
    absent = check(tmp_path / "absent.db", 259)
    assert (absent.format, absent.rows_checked) == (0, 0)
    assert not (tmp_path / "absent.db").exists()


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("x", "ledger_meta.format='x' is not a positive integer"),
        ("0", "ledger_meta.format='0' is not a positive integer"),
        ("-1", "ledger_meta.format='-1' is not a positive integer"),
        ("١", "ledger_meta.format='١' is not a positive integer"),
    ],
)
def test_a_stamp_that_is_not_a_format_is_named(tmp_path: Path, value: str, message: str) -> None:
    db = _demo(tmp_path)
    _sql(db, f"UPDATE ledger_meta SET value = '{value}' WHERE key = 'format'")
    with pytest.raises(LedgerSkewError, match=f"^{re.escape(message)} "):
        check(db, 259)
    _sql(db, "DELETE FROM ledger_meta WHERE key = 'format'")
    with pytest.raises(LedgerSkewError, match=r"^ledger_meta\.format missing "):
        check(db, 259)


def test_t7_format_1_is_what_the_ledger_already_was() -> None:
    committed = json.loads((FORMATS / "1.json").read_text(encoding="utf-8"))
    assert committed["enums"] == {
        **OTHER_ENUMS_AT_DEVELOP,
        "pr_findings.claim_type": [m.value for m in ClaimType],
        "pr_findings.severity": ["blocking", "advisory", "note"],
        "pr_findings.status": [
            "proposed",
            "verified",
            "refuted",
            "open",
            "resolved",
            "stuck",
            "not_applicable",
            "resolved_at_head",
            "owner_resolved",
            "noted",
        ],
    }
    assert len(committed["enums"]["pr_findings.claim_type"]) == 8
    assert committed["tables"]["pr_findings"] == PR_FINDINGS_AT_5CEA5A7
    for table, columns in OTHER_TABLES_AT_5CEA5A7.items():
        assert committed["tables"][table] == columns
    assert committed["tables"]["ledger_meta"] == ["key VARCHAR", "value VARCHAR"]
    assert set(committed["tables"]) == {*OTHER_TABLES_AT_5CEA5A7, "pr_findings", "ledger_meta"}
    assert committed["json"] == JSON_AT_DEVELOP


def test_t8_demo_ledger_carries_every_enum_value_and_passes(tmp_path: Path) -> None:
    db = _demo(tmp_path)
    with sqlite3.connect(db) as conn:
        values = {
            (entry.table.name, entry.column): {
                row[0]
                for row in conn.execute(f"SELECT DISTINCT {entry.column} FROM {entry.table.name}")
            }
            for entry in ledger_format.ENUM_COLUMNS
        }
        meta = dict(conn.execute("SELECT key, value FROM ledger_meta"))
        coverage = conn.execute("SELECT count(*) FROM pr_spec_coverage").fetchone()[0]
        integrity = conn.execute("SELECT count(*) FROM pr_review_integrity").fetchone()[0]
        generated = conn.execute("SELECT count(*) FROM generated_checks").fetchone()[0]
    assert len(ledger_format.ENUM_COLUMNS) == 6
    for entry in ledger_format.ENUM_COLUMNS:
        assert values[(entry.table.name, entry.column)] == set(entry.values)
    assert (coverage, integrity, generated) == (
        len(OTHER_ENUMS_AT_DEVELOP["pr_spec_coverage.contract"]),
        1,
        len(GeneratedState),
    )
    assert len(get_args(ScriptRef)) == 2
    with sqlite3.connect(db) as conn:
        for entry in ledger_format.JSON_COLUMNS:
            stored = {
                row[0]
                for row in conn.execute(f"SELECT DISTINCT {entry.column} FROM {entry.table.name}")
            }
            assert any(value not in (None, "", "[]", "{}") for value in stored), entry.column
    assert meta["format"] == str(LEDGER_FORMAT)
    assert meta["written_by"] == "0.2.0.dev14+gabc1234"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", meta["written_at"])
    assert check(db, 259).rows_checked == N_DEMO == 21


def test_stamp_defaults_to_this_version_and_replaces_its_keys(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    stamp(db)
    stamp(db)
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT key, value FROM ledger_meta ORDER BY key").fetchall()
    assert [key for key, _ in rows] == ["format", "written_at", "written_by"]
    assert dict(rows)["written_by"] == READER


def test_check_cli_exits_5_with_the_message_on_stderr(tmp_path: Path) -> None:
    db = _load_skewed(tmp_path)
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "repoach.review.ledger_format",
            "check",
            "--db",
            str(db),
            "--pr",
            "259",
            "--reader",
            "0.1.0",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert completed.returncode == 5
    assert completed.stderr.strip() == (
        "ledger: format 2 is newer than this reader's 1 "
        "(written by repoach 9.9.9; this gate is repoach 0.1.0)"
    )
    assert json.loads(completed.stdout)["needed_format"] == 2


def test_review_pr_stamps_the_ledger_it_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_unreachable_reviewers(monkeypatch)
    monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)
    db = tmp_path / "review.db"
    settings = get_settings().model_copy(update={"db_path": db})
    monkeypatch.setattr(review_cmds, "get_settings", lambda: settings)

    def review(pr_number: int, **kwargs: object) -> TeamOutcome:
        return ReviewTeamOrchestrator(
            forge=_StubGitHubForge(),
            db_path=db,
            post_to_github=bool(kwargs["post"]),
            repo_root=tmp_path,
        ).review_pr(pr_number)

    monkeypatch.setattr(review_cmds, "run_review", review)
    monkeypatch.chdir(REPO_ROOT)
    CliRunner().invoke(review_cmds.review_app, ["pr", "42", "--dry-run"])
    with sqlite3.connect(db) as conn:
        meta = dict(conn.execute("SELECT key, value FROM ledger_meta"))
    assert meta["format"] == str(LEDGER_FORMAT)
    assert meta["written_by"] == READER


def test_review_fix_restamps_the_ledger_it_rewrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "review.db"
    seed_review(db, 259, HEAD_259)
    stamp(db, written_by="an older review job")
    settings = get_settings().model_copy(update={"db_path": db})
    monkeypatch.setattr(review_cmds, "get_settings", lambda: settings)

    def fix(pr_number: int, **_kwargs: object) -> CoderFindingsResult:
        record_finding(
            db,
            Finding(
                pr_number=pr_number,
                head_sha=HEAD_259,
                round=2,
                finder="coder",
                claim_type=ClaimType.BROKEN_BEHAVIOR,
                severity=Severity.BLOCKING,
                file="README.md",
                line_start=1,
                line_end=1,
                claim="a row the coder lane writes",
                evidence_pointer="README.md:1",
            ),
        )
        return CoderFindingsResult(pr_number=pr_number, pushed=True)

    monkeypatch.setattr(review_cmds, "run_coder_fix_from_findings", fix)
    monkeypatch.chdir(REPO_ROOT)
    result = CliRunner().invoke(review_cmds.review_app, ["fix", "259"])
    assert result.exit_code == 0, result.output
    with sqlite3.connect(db) as conn:
        meta = dict(conn.execute("SELECT key, value FROM ledger_meta"))
    assert meta["format"] == str(LEDGER_FORMAT)
    assert meta["written_by"] == READER


def _clone(tmp_path: Path) -> Path:
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "--quiet", str(REPO_ROOT), str(clone)], check=True)
    return clone


REPLAY_SCRIPT = REPO_ROOT / "scripts" / "skew_replay.sh"
"""The working-tree script, so an edit not yet committed is the one under test."""


def _replay(clone: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run this tree's ``skew_replay.sh`` inside ``clone``, whose refs it replays.

    The script finds its repository with ``git rev-parse --show-toplevel``
    from its working directory, so the refs and ``tmp/skew/`` are the
    clone's while the script is the working tree's.
    """
    env = {
        **os.environ,
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
        "PYTHON": sys.executable,
    }
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        ["bash", str(REPLAY_SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=clone,
        env=env,
        check=False,
    )


def test_t9_skew_replay_head_head_is_ok_and_leaves_nothing(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    completed = _replay(clone, "HEAD", "HEAD")
    assert completed.returncode == 0, completed.stderr
    sha = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "--short=7", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert completed.stdout == (
        f"ledger: ok (format {LEDGER_FORMAT}, writer HEAD ({sha}), reader HEAD ({sha}))\n"
    )
    assert not (clone / "tmp" / "skew").exists()
    worktrees = subprocess.run(
        ["git", "-C", str(clone), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert worktrees.count("worktree ") == 1


def _git(clone: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(clone), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_t9_a_newer_writer_is_refused_and_leaves_nothing(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    findings = clone / "src" / "repoach" / "review" / "findings.py"
    findings.write_text(
        findings.read_text(encoding="utf-8").replace(
            '    NOTE = "note"\n', '    NOTE = "note"\n    LATER = "later"\n', 1
        ),
        encoding="utf-8",
    )
    module = clone / "src" / "repoach" / "review" / "ledger_format.py"
    module.write_text(
        module.read_text(encoding="utf-8").replace(
            "LEDGER_FORMAT: int = 1\n", "LEDGER_FORMAT: int = 2\n", 1
        ),
        encoding="utf-8",
    )
    _git(clone, "commit", "--quiet", "-am", "test: a writer at format 2")
    writer = _git(clone, "rev-parse", "--short=7", "HEAD")
    reader = _git(clone, "rev-parse", "--short=7", "HEAD~1")

    completed = _replay(clone, "HEAD~1", "HEAD")

    assert completed.returncode == 5, completed.stderr
    assert completed.stdout == (
        "ledger: format 2 is newer than this reader's 1 "
        f"(written by repoach HEAD ({writer}); this gate is repoach HEAD~1 ({reader}))\n"
    )
    assert not (clone / "tmp" / "skew").exists()


def test_t9_skew_replay_names_a_ref_that_does_not_resolve(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    completed = _replay(clone, "no-such-ref", "HEAD")
    assert completed.returncode == 2
    assert completed.stderr.strip() == "skew_replay: reader ref no-such-ref does not resolve"
    assert not (clone / "tmp" / "skew").exists()


def test_t9_skew_replay_keep_leaves_the_worktrees(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    completed = _replay(clone, "HEAD", "HEAD", "--keep")
    assert completed.returncode == 0, completed.stderr
    assert (clone / "tmp" / "skew" / "reader" / "src").is_dir()
    assert (clone / "tmp" / "skew" / "writer" / "src").is_dir()


def test_t3_an_untrusted_value_is_clipped_and_inert_in_the_refusal(tmp_path: Path) -> None:
    db = _demo(tmp_path)
    hostile = "@owner ```" + "x" * 500
    _sql(
        db,
        f"UPDATE pr_findings SET severity = '{hostile}' "
        "WHERE id = (SELECT max(id) FROM pr_findings)",
        f"UPDATE ledger_meta SET value = '{hostile}' WHERE key = 'written_by'",
    )
    with pytest.raises(LedgerSkewError) as raised:
        check(db, 259)
    reason = str(raised.value)
    assert "@" not in reason
    assert "`" not in reason
    assert "x" * 100 not in reason
    assert len(reason) < 400


def test_t3_a_hostile_format_stamp_is_clipped_and_inert(tmp_path: Path) -> None:
    db = _demo(tmp_path)
    _sql(db, "UPDATE ledger_meta SET value = '@a ```" + "y" * 500 + "' WHERE key = 'format'")
    with pytest.raises(LedgerSkewError) as raised:
        check(db, 259)
    reason = str(raised.value)
    assert "@" not in reason and "`" not in reason
    assert "y" * 100 not in reason


def test_review_gate_cli_prints_ledger_summary_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx = MergeWaitFixtureForge(
        [FixturePR(259, HEAD_259, "docs/sp-approved-spec-without-plan", check_runs=ci_runs())]
    )
    db = tmp_path / "review.db"
    seed_review(db, 259, HEAD_259)
    stamp(db, written_by="0.1.0")
    _cli_settings(monkeypatch, fx, db)
    result = CliRunner().invoke(review_cmds.review_app, ["gate", "259"])
    assert "Traceback" not in result.output
    payload = json.loads(result.stdout)
    assert payload["ledger"]["format"] == 1
    assert payload["ledger"]["written_by"] == "0.1.0"
    assert "ledger: format 1, written by repoach 0.1.0" in result.stderr.splitlines()
