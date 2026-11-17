"""The findings ledger's format number and the check every gate runs before reading it.

`SP-LEDGER-FORMAT-IS-VERSIONED`. The ledger a review run writes is read by
the merge gate of an older release (``merge-on-ci.yml`` runs ``main``'s
code). :data:`LEDGER_FORMAT` names the shape this code writes and can read:
the tables the gate reads, their columns, and the values of the enum
columns. Its definition is the committed snapshot
``tests/fixtures/ledger_format/<LEDGER_FORMAT>.json``, which
:func:`snapshot` must reproduce, so a shape change without a bump is a red
test.

The writer stamps the ledger (:func:`stamp`: ``ledger_meta`` rows
``format``, ``written_by``, ``written_at``) after its last write of a
review run. The reader calls :func:`check` before any row is
materialised: a newer format, a declared column the file lacks, or an
enum value this reader does not define raises :class:`LedgerSkewError`,
which both gate entry points turn into ``SKIP_LEDGER_SKEW`` (exit 5). A
ledger with no ``ledger_meta`` is format 0, unstamped, and is read as it
always was, its values still checked.

``python -m repoach.review.ledger_format`` runs ``snapshot``, ``stamp``,
``check`` and ``demo``; ``scripts/skew_replay.sh`` uses the last two to
feed a ledger written by one ref to the reader of another.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import (
    Column,
    MetaData,
    String,
    Table,
    create_engine,
    distinct,
    func,
    inspect,
    select,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection

from .. import __version__
from .findings import (
    _ADDED_FINDINGS_COLUMNS,
    ClaimType,
    Finding,
    FindingStatus,
    Severity,
    init_findings_schema,
    pr_findings,
    pr_review_integrity,
    record_finding,
    record_review_integrity,
)
from .persistence import _ADDED_REVIEW_COLUMNS, _pr_merges, init_schema
from .spec_gate import _ADDED_COLUMNS as _ADDED_COVERAGE_COLUMNS
from .spec_gate import SpecCoverage, _pr_spec_coverage, record_spec_coverage

LEDGER_FORMAT: int = 1
"""The ledger shape this code writes and reads; bump it with a new snapshot file."""

_metadata = MetaData()

ledger_meta = Table(
    "ledger_meta",
    _metadata,
    Column("key", String, primary_key=True),
    Column("value", String, nullable=False),
)
"""One row per stamp key: ``format``, ``written_by``, ``written_at``."""

GATE_TABLES: tuple[Table, ...] = (pr_findings, pr_review_integrity, _pr_spec_coverage, _pr_merges)
"""The ledger tables the merge gate reads, in the order the check inspects them."""

ENUM_COLUMNS: tuple[tuple[str, type[StrEnum]], ...] = (
    ("claim_type", ClaimType),
    ("severity", Severity),
    ("status", FindingStatus),
)
"""The ``pr_findings`` columns whose values a reader materialises as enums."""

_UNSTAMPED = "(unstamped)"


class LedgerSkewError(RuntimeError):
    """The ledger carries a format, column or value this reader does not define.

    The message names them with both versions.

    Attributes:
        detail: The refusal, ending with the writer and reader versions.
        needed_format: The lowest format a reader able to judge this
            ledger carries: the writer's when it is newer, else the next
            one after this reader's.
    """

    def __init__(self, detail: str, *, writer_format: int) -> None:
        super().__init__(detail)
        self.detail = detail
        self.needed_format = max(writer_format, LEDGER_FORMAT + 1)

    @property
    def reason(self) -> str:
        """The merge attempt reason of ``SKIP_LEDGER_SKEW``."""
        return (
            f"ledger skew: {self.detail}; a release carrying format {self.needed_format} "
            "is needed before this lane can judge this head"
        )


class LedgerCheck(BaseModel):
    """A ledger this reader can judge, as :func:`check` found it.

    Attributes:
        format: The stamped format; 0 when unstamped.
        written_by: The writer's ``repoach`` version; ``""`` when unstamped.
        written_at: The stamp's UTC ISO-8601 time; ``""`` when unstamped.
        reader: This reader's ``repoach`` version.
        rows_checked: ``pr_findings`` rows of the pull request whose enum
            columns were read.
    """

    format: int
    written_by: str
    written_at: str
    reader: str
    rows_checked: int

    def summary_line(self) -> str:
        """The one ``ledger: …`` line ``review gate`` prints."""
        if self.format == 0:
            return "ledger: unstamped (pre-format), values known"
        line = f"ledger: format {self.format}, written by repoach {self.written_by}"
        if self.format < LEDGER_FORMAT:
            line += f" (older than this reader's {LEDGER_FORMAT})"
        return line


def snapshot() -> dict[str, object]:
    """Return the live ledger shape: every table's columns and every enum column's values.

    Returns:
        ``{"tables": {name: ["<column> <TYPE>", ...]}, "enums": {"pr_findings.<column>": [values]}}``,
        columns and values in declaration order.
    """
    tables = {
        table.name: [f"{column.name} {column.type}" for column in table.columns]
        for table in (*GATE_TABLES, ledger_meta)
    }
    enums = {
        f"{pr_findings.name}.{column}": [member.value for member in enum]
        for column, enum in ENUM_COLUMNS
    }
    return {"tables": tables, "enums": enums}


def stamp(db_path: Path, *, written_by: str | None = None) -> None:
    """Create ``ledger_meta`` if absent and upsert the three stamp keys.

    Args:
        db_path: The ledger database.
        written_by: The writer version to record; ``repoach.__version__``
            when omitted.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        _metadata.create_all(engine, checkfirst=True)
        rows = {
            "format": str(LEDGER_FORMAT),
            "written_by": written_by or __version__,
            "written_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        with engine.begin() as conn:
            for key, value in rows.items():
                upsert = sqlite_insert(ledger_meta).values(key=key, value=value)
                conn.execute(
                    upsert.on_conflict_do_update(index_elements=["key"], set_={"value": value})
                )
    finally:
        engine.dispose()


def _healed_columns() -> set[tuple[str, str]]:
    """The ``(table, column)`` pairs this reader's own migrations add on open."""
    added = (*_ADDED_FINDINGS_COLUMNS, *_ADDED_REVIEW_COLUMNS)
    pairs = {(table, column) for table, column, _ddl, _spec in added}
    pairs.update((_pr_spec_coverage.name, column) for column, _ddl in _ADDED_COVERAGE_COLUMNS)
    return pairs


def _read_stamp(conn: Connection, tables: set[str], reader: str) -> tuple[int, str, str]:
    """Return ``(format, written_by, written_at)``; ``(0, "", "")`` without ``ledger_meta``."""
    if ledger_meta.name not in tables:
        return 0, "", ""
    present = {column["name"] for column in inspect(conn).get_columns(ledger_meta.name)}
    for column in ledger_meta.columns:
        if column.name not in present:
            raise LedgerSkewError(
                f"{ledger_meta.name}.{column.name} missing "
                f"(written by repoach (unreadable stamp); this gate is repoach {reader})",
                writer_format=0,
            )
    meta = {
        str(key): str(value)
        for key, value in conn.execute(select(ledger_meta.c.key, ledger_meta.c.value))
    }
    written_by = meta.get("written_by", "")
    versions = f"written by repoach {written_by or _UNSTAMPED}; this gate is repoach {reader}"
    raw = meta.get("format")
    if raw is None:
        raise LedgerSkewError(f"ledger_meta.format missing ({versions})", writer_format=0)
    try:
        stamped = int(raw)
    except ValueError:
        raise LedgerSkewError(
            f"ledger_meta.format={raw!r} is not an integer ({versions})", writer_format=0
        ) from None
    if stamped < 1:
        raise LedgerSkewError(
            f"ledger_meta.format={raw!r} is not a positive integer ({versions})", writer_format=0
        )
    return stamped, written_by, meta.get("written_at", "")


def check(db_path: Path, pr_number: int, *, reader: str | None = None) -> LedgerCheck:
    """Check the ledger's stamp, columns and enum values before any row is read.

    In order, the first of these raises: a format newer than
    :data:`LEDGER_FORMAT`; a declared column of a gate table the file
    lacks (on a ledger older than this reader, the columns this reader's
    own migrations add on open are exempt: it heals them, as it always
    did); a distinct value of an enum column among the pull request's
    ``pr_findings`` rows that the enum does not define.

    Args:
        db_path: The ledger database; an absent file is an empty,
            unstamped ledger and nothing is created.
        pr_number: The pull request whose rows are checked.
        reader: This reader's version, for the messages; ``repoach.__version__``
            when omitted.

    Returns:
        The stamp and the number of rows checked.

    Raises:
        LedgerSkewError: The ledger carries a format, column or value this
            reader does not define.
    """
    reader_version = reader or __version__
    if not db_path.is_file():
        return LedgerCheck(
            format=0, written_by="", written_at="", reader=reader_version, rows_checked=0
        )
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            tables = set(inspect(conn).get_table_names())
            stamped, written_by, written_at = _read_stamp(conn, tables, reader_version)
            versions = (
                f"written by repoach {written_by or _UNSTAMPED}; "
                f"this gate is repoach {reader_version}"
            )
            if stamped > LEDGER_FORMAT:
                raise LedgerSkewError(
                    f"format {stamped} is newer than this reader's {LEDGER_FORMAT} ({versions})",
                    writer_format=stamped,
                )
            _check_columns(conn, tables, stamped, versions)
            rows = _check_enum_values(conn, tables, pr_number, stamped, versions)
    finally:
        engine.dispose()
    return LedgerCheck(
        format=stamped,
        written_by=written_by,
        written_at=written_at,
        reader=reader_version,
        rows_checked=rows,
    )


def _check_columns(conn: Connection, tables: set[str], stamped: int, versions: str) -> None:
    """Raise on the first declared column of a present gate table that the file lacks."""
    healed = _healed_columns() if stamped < LEDGER_FORMAT else set()
    for table in GATE_TABLES:
        if table.name not in tables:
            continue
        present = {column["name"] for column in inspect(conn).get_columns(table.name)}
        for column in table.columns:
            if column.name in present or (table.name, column.name) in healed:
                continue
            raise LedgerSkewError(
                f"{table.name}.{column.name} missing ({versions})", writer_format=stamped
            )


def _check_enum_values(
    conn: Connection, tables: set[str], pr_number: int, stamped: int, versions: str
) -> int:
    """Raise on the first enum value of the PR's rows outside its enum; return the row count."""
    if pr_findings.name not in tables:
        return 0
    of_pr = pr_findings.c.pr_number == pr_number
    for column, enum in ENUM_COLUMNS:
        known = {member.value for member in enum}
        values = conn.execute(select(distinct(pr_findings.c[column])).where(of_pr)).scalars()
        unknown = sorted(str(value) for value in values if value not in known)
        if unknown:
            raise LedgerSkewError(
                f"{pr_findings.name}.{column}={unknown[0]!r} is not a {enum.__name__} "
                f"this reader knows ({versions})",
                writer_format=stamped,
            )
    return int(
        conn.execute(select(func.count()).select_from(pr_findings).where(of_pr)).scalar_one()
    )


def demo_ledger(db_path: Path, pr_number: int = 0, *, written_by: str | None = None) -> None:
    """Write a ledger that carries every value this code defines, then stamp it.

    One finding per value of each enum of :data:`ENUM_COLUMNS`, one
    coverage row and one integrity row, every gate table created.

    Args:
        db_path: The ledger to write.
        pr_number: The pull request the rows belong to.
        written_by: The writer version to stamp; ``repoach.__version__``
            when omitted.
    """
    init_schema(db_path)
    init_findings_schema(db_path)
    head = "0" * 40
    base = {
        "pr_number": pr_number,
        "head_sha": head,
        "round": 1,
        "finder": "demo",
        "file": "README.md",
        "line_start": 1,
        "line_end": 1,
        "claim": "demo",
        "evidence_pointer": "demo",
    }
    findings = [
        *(Finding(**base, claim_type=value, severity=Severity.BLOCKING) for value in ClaimType),
        *(
            Finding(**base, claim_type=ClaimType.BROKEN_BEHAVIOR, severity=value)
            for value in Severity
        ),
        *(
            Finding(
                **base,
                claim_type=ClaimType.BROKEN_BEHAVIOR,
                severity=Severity.ADVISORY,
                status=value,
            )
            for value in FindingStatus
        ),
    ]
    for finding in findings:
        record_finding(db_path, finding)
    record_spec_coverage(
        db_path,
        pr_number=pr_number,
        head_sha=head,
        coverage=SpecCoverage(
            spec_id="SP-DEMO", n_promised=0, n_present=0, missing=[], covered=True
        ),
    )
    record_review_integrity(
        db_path, pr_number=pr_number, head_sha=head, n_reviewers=4, n_unparsed=0
    )
    stamp(db_path, written_by=written_by)


def _parser() -> argparse.ArgumentParser:
    """The ``python -m repoach.review.ledger_format`` argument parser."""
    parser = argparse.ArgumentParser(prog="python -m repoach.review.ledger_format")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("snapshot", help="print the live ledger shape")
    stamp_cmd = commands.add_parser("stamp", help="stamp a ledger with this code's format")
    stamp_cmd.add_argument("--db", type=Path, required=True)
    stamp_cmd.add_argument("--written-by", default=None)
    check_cmd = commands.add_parser("check", help="check a ledger before any row is read")
    check_cmd.add_argument("--db", type=Path, required=True)
    check_cmd.add_argument("--pr", type=int, required=True)
    check_cmd.add_argument("--reader", default=None)
    demo_cmd = commands.add_parser("demo", help="write a stamped ledger with every enum value")
    demo_cmd.add_argument("--db", type=Path, required=True)
    demo_cmd.add_argument("--pr", type=int, default=0)
    demo_cmd.add_argument("--written-by", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one subcommand; print one JSON object on stdout.

    ``check`` prints its ``ledger: …`` line on stderr and exits 5 on a
    :class:`LedgerSkewError`, whose message goes to stderr.

    Args:
        argv: The arguments; ``sys.argv[1:]`` when omitted.

    Returns:
        The exit code: 0, or 5 for a skew.
    """
    args = _parser().parse_args(argv)
    if args.command == "snapshot":
        print(json.dumps(snapshot(), indent=2, sort_keys=True))
        return 0
    if args.command == "stamp":
        stamp(args.db, written_by=args.written_by)
        print(json.dumps({"db": str(args.db), "format": LEDGER_FORMAT}))
        return 0
    if args.command == "demo":
        demo_ledger(args.db, args.pr, written_by=args.written_by)
        print(json.dumps({"db": str(args.db), "format": LEDGER_FORMAT, "pr": args.pr}))
        return 0
    try:
        result = check(args.db, args.pr, reader=args.reader)
    except LedgerSkewError as exc:
        print(
            json.dumps(
                {"error": "ledger_skew", "detail": exc.detail, "needed_format": exc.needed_format}
            )
        )
        print(f"ledger: {exc.detail}", file=sys.stderr)
        return 5
    print(result.model_dump_json())
    print(result.summary_line(), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
