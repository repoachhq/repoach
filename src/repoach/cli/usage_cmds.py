"""``repoach usage``: tokens, duration and cost estimate over a period (SP-USAGE-LEDGER G4).

Reads the ``usage_ledger`` table of the review database
(``settings.db_path``) that every model call writes
(:func:`~repoach.providers.usage.record_call`), groups the rows and
prints one line per group plus a total.  The cost column is the sum of
the priced rows, ``subscription (tokens only)`` for ``claude_code``, and
``+ N unpriced calls`` / ``cannot say`` for rows with no price.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import typer

from ..core.config import get_settings
from ..providers.usage import parse_since, read_rows, usage_report, usage_table

GROUPINGS: tuple[str, ...] = ("model", "provider", "reviewer", "pr", "day", "command")


def usage(
    since: str = typer.Option(
        "7d",
        "--since",
        help="Start of the period: a duration (7d, 24h, 2w) or a date (2026-10-01).",
    ),
    until: str | None = typer.Option(
        None, "--until", help="End of the period (exclusive), same forms; default now."
    ),
    by: str = typer.Option("model", "--by", help=f"Grouping: {', '.join(GROUPINGS)}."),
    as_json: bool = typer.Option(False, "--json", help="Print the report as one JSON document."),
) -> None:
    """Print the model usage recorded in the ledger since a date, grouped.

    Exit codes: 0 printed (an empty period prints a zero total), 2 an
    invalid ``--since`` / ``--until`` / ``--by``.
    """
    if by not in GROUPINGS:
        typer.echo(f"repoach: --by {by!r}: use one of {', '.join(GROUPINGS)}", err=True)
        raise typer.Exit(code=2)
    now = datetime.now(UTC)
    try:
        start = parse_since(since, now=now)
        end = parse_since(until, now=now) if until is not None else None
    except ValueError as exc:
        typer.echo(f"repoach: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    rows = read_rows(Path(get_settings().db_path), since=start, until=end)
    report = usage_report(rows, by=by)
    if as_json:
        typer.echo(report.model_dump_json(indent=2))
        return
    typer.echo(f"usage since {start.isoformat(timespec='minutes')} by {by}")
    for line in usage_table(report):
        typer.echo(line)


__all__ = ["GROUPINGS", "usage"]
