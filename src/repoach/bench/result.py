"""The recorded run: what each case's review produced, before any label is applied.

A result file (``bench/results/<date>_<corpus>_<provider>.json``) holds
the findings of every case exactly as the ledger recorded them, plus
tokens and wall time. Labels are not copied in: :mod:`.score` reads
them from the corpus at report time, so labelling after a run never
needs the run again.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

RESULT_SCHEMA = 1


class ResultError(ValueError):
    """A result file is missing or invalid; the message names the file."""


class RecordedFinding(BaseModel):
    """One ``pr_findings`` row of a case, with its reviewer comment's severity.

    Attributes:
        fingerprint: :func:`~repoach.bench.corpus.fingerprint` of the row.
        reviewer: The finder (``architect``, ``sentinel``, ``tester``, ``scribe``).
        claim_type: The claim type.
        severity: The reviewer comment's severity (``blocker``, ``major``,
            ``minor``, ``nit``); the ledger's (``blocking`` /
            ``advisory``) for a row no comment produced.
        file: The cited file.
        line_start: First cited line.
        line_end: Last cited line.
        claim: The claim text (at most 500 characters, as recorded).
        status: The final lifecycle status (``refuted`` is scored as a refutation).
        reason: The verifier's or the refuter's recorded reason.
    """

    fingerprint: str
    reviewer: str
    claim_type: str
    severity: str
    file: str
    line_start: int
    line_end: int
    claim: str
    status: str
    reason: str = ""

    @property
    def refuted(self) -> bool:
        """Return True when the refuter (or a verifier) refuted the finding."""
        return self.status == "refuted"


class CaseRun(BaseModel):
    """What one case produced.

    Attributes:
        case: The case id.
        kind: The case's source kind.
        head_sha: The head reviewed (or scored, for a ledger case).
        pr_url: The PR / MR link, None for a local case.
        skipped: Why the case was not reviewed, None when it was.
        findings: The recorded findings.
        tokens: Total tokens of the case's model calls.
        calls: Model calls made.
        errors: Model calls that failed.
        wall_s: Wall time of the case.
        verdict: The team verdict, None when skipped or not reviewed.
    """

    case: str
    kind: str
    head_sha: str | None = None
    pr_url: str | None = None
    skipped: str | None = None
    findings: list[RecordedFinding] = Field(default_factory=list)
    tokens: int = 0
    calls: int = 0
    errors: int = 0
    wall_s: float = 0.0
    verdict: str | None = None


class BenchResult(BaseModel):
    """A whole run over a corpus.

    Attributes:
        schema_version: :data:`RESULT_SCHEMA`.
        corpus: The corpus name.
        corpus_root: The corpus path the labels are read back from.
        corpus_sha: The corpus version at run time.
        provider: The provider every review ran on.
        reviewers: The reviewers kept in the result (all four by default).
        measured_on: The run date.
        started_at: When the run started (UTC).
        partial: True when the run stopped before its last case.
        dry_run: True when no model was called (cases materialised only).
        cases: One entry per case, in corpus order.
    """

    schema_version: int = RESULT_SCHEMA
    corpus: str
    corpus_root: str
    corpus_sha: str
    provider: str
    reviewers: list[str]
    measured_on: date
    started_at: datetime
    partial: bool = False
    dry_run: bool = False
    cases: list[CaseRun] = Field(default_factory=list)

    def file_name(self) -> str:
        """Return ``<date>_<corpus>_<provider>.json``."""
        return f"{self.measured_on.isoformat()}_{self.corpus}_{self.provider}.json"


def save_result(result: BenchResult, results_dir: Path) -> Path:
    """Write ``result`` under ``results_dir`` and return the file."""
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / result.file_name()
    path.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def load_result(path: Path) -> BenchResult:
    """Read a result file.

    Raises:
        ResultError: The file is missing, not JSON, or not a result.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ResultError(f"{path}: unreadable: {exc.strerror}") from exc
    except json.JSONDecodeError as exc:
        raise ResultError(f"{path}: not JSON: {exc.msg}") from exc
    try:
        return BenchResult.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        raise ResultError(f"{path}: {where}: {first['msg']}") from exc


__all__ = [
    "RESULT_SCHEMA",
    "BenchResult",
    "CaseRun",
    "RecordedFinding",
    "ResultError",
    "load_result",
    "save_result",
]
