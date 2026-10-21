"""``repoach digest``: what the gate refused over a period, read from the ledger (G6).

Three lists, counted at the top:

* **refuted findings** — ``pr_findings`` rows the refuter (or a
  verifier) refuted, with their recorded reason. ``pr_findings`` carries
  no timestamp, so the period selects the pull requests reviewed in it
  (a ``pr_reviews`` row at or after ``since``) and lists their refuted
  findings, whichever head they were raised at.
* **refused LGTMs** — ``pr_merges`` attempts in the period that the
  gate refused (every outcome but ``APPROVE``, ``ALREADY_MERGED`` and
  ``SKIP_STATE``), with the outcome tag and the recorded notes as the
  gate fact that failed. ``pr_merges`` records no head SHA.
* **passed after refusal** — merges in the period of a pull request
  with at least one refused attempt before it.

Read-only on the ledger; a ledger missing a table contributes nothing.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection

from ..review.auto_merge import OUTCOME_ALREADY_MERGED, OUTCOME_MERGED, OUTCOME_SKIP_STATE

NOT_A_REFUSAL: frozenset[str] = frozenset(
    {OUTCOME_MERGED, OUTCOME_ALREADY_MERGED, OUTCOME_SKIP_STATE}
)
"""Merge outcomes that are not the gate saying no (a merge, or a PR no longer open)."""

_CLAIM_CHARS = 200


class RefutedFinding(BaseModel):
    """One refuted finding."""

    pr: int
    pr_url: str
    head_sha: str
    reviewer: str
    claim_type: str
    file: str
    line: int
    claim: str
    reason: str


class RefusedLgtm(BaseModel):
    """One merge attempt the gate refused."""

    pr: int
    pr_url: str
    at: datetime
    outcome: str
    fact: str
    base_ref: str
    head_ref: str


class PassedAfterRefusal(BaseModel):
    """A merge that followed at least one refusal."""

    pr: int
    pr_url: str
    merged_at: datetime
    merged_sha: str | None
    refusals: int


class Digest(BaseModel):
    """The whole digest."""

    since: datetime
    generated_at: datetime
    refuted_findings: list[RefutedFinding]
    refused_lgtms: list[RefusedLgtm]
    passed_after_refusal: list[PassedAfterRefusal]
    counts: dict[str, int]


def web_url(remote: str) -> str:
    """Return the browser URL of a git remote (``git@host:o/r.git`` → ``https://host/o/r``)."""
    url = remote.strip().removesuffix(".git").rstrip("/")
    if url.startswith("git@") and ":" in url:
        host, _, path = url[len("git@") :].partition(":")
        return f"https://{host}/{path}"
    if url.startswith("ssh://"):
        rest = url[len("ssh://") :]
        rest = rest.split("@", 1)[-1]
        host, _, path = rest.partition("/")
        return f"https://{host.split(':')[0]}/{path}"
    if "://" in url and "@" in url.split("://", 1)[1].split("/", 1)[0]:
        scheme, rest = url.split("://", 1)
        return f"{scheme}://{rest.split('@', 1)[1]}"
    return url


def pr_link(repo_url: str, number: int, forge: str) -> str:
    """Return the link of PR / MR ``number``: ``/pull/N`` on GitHub, ``/-/merge_requests/N`` on GitLab."""
    if forge == "gitlab":
        return f"{repo_url}/-/merge_requests/{number}"
    return f"{repo_url}/pull/{number}"


def _when(value: object) -> datetime:
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)


def digest(db_path: Path, since: datetime, *, repo_url: str, forge: str) -> Digest:
    """Read what the gate refused since ``since``.

    Args:
        db_path: The review ledger.
        since: Start of the period (aware; naive is read as UTC).
        repo_url: The repository's browser URL, for the links.
        forge: ``github`` or ``gitlab``, for the link shape.

    Returns:
        The digest; empty lists when the ledger (or a table) is absent.
    """
    since = _when(since)
    refuted: list[RefutedFinding] = []
    refused: list[RefusedLgtm] = []
    passed: list[PassedAfterRefusal] = []
    if db_path.exists():
        engine = create_engine(f"sqlite:///{db_path}")
        try:
            tables = set(inspect(engine).get_table_names())
            with engine.connect() as conn:
                reviewed: set[int] = set()
                if "pr_reviews" in tables:
                    for pr, created in conn.execute(
                        text("SELECT pr_number, created_at FROM pr_reviews")
                    ):
                        if _when(created) >= since:
                            reviewed.add(int(pr))
                if "pr_findings" in tables and reviewed:
                    for row in conn.execute(
                        text(
                            "SELECT pr_number, head_sha, finder, claim_type, file, line_start, "
                            "claim, verification_result FROM pr_findings "
                            "WHERE status = 'refuted' ORDER BY pr_number, id"
                        )
                    ):
                        if int(row[0]) not in reviewed:
                            continue
                        refuted.append(
                            RefutedFinding(
                                pr=int(row[0]),
                                pr_url=pr_link(repo_url, int(row[0]), forge),
                                head_sha=row[1],
                                reviewer=row[2],
                                claim_type=row[3],
                                file=row[4],
                                line=int(row[5]),
                                claim=row[6][:_CLAIM_CHARS],
                                reason=row[7],
                            )
                        )
                if "pr_merges" in tables:
                    refused, passed = _merges(conn, since, repo_url=repo_url, forge=forge)
        finally:
            engine.dispose()
    return Digest(
        since=since,
        generated_at=datetime.now(UTC),
        refuted_findings=refuted,
        refused_lgtms=refused,
        passed_after_refusal=passed,
        counts={
            "refuted_findings": len(refuted),
            "refused_lgtms": len(refused),
            "passed_after_refusal": len(passed),
            "prs": len(
                {item.pr for item in refuted}
                | {item.pr for item in refused}
                | {item.pr for item in passed}
            ),
        },
    )


def _merges(
    conn: Connection, since: datetime, *, repo_url: str, forge: str
) -> tuple[list[RefusedLgtm], list[PassedAfterRefusal]]:
    rows = conn.execute(
        text(
            "SELECT pr_number, outcome, base_ref, head_ref, merged_sha, notes, created_at "
            "FROM pr_merges ORDER BY created_at, id"
        )
    ).all()
    refused: list[RefusedLgtm] = []
    passed: list[PassedAfterRefusal] = []
    refusals_before: dict[int, int] = defaultdict(int)
    for pr, outcome, base_ref, head_ref, merged_sha, notes, created in rows:
        number, at = int(pr), _when(created)
        if outcome == OUTCOME_MERGED:
            if at >= since and refusals_before[number]:
                passed.append(
                    PassedAfterRefusal(
                        pr=number,
                        pr_url=pr_link(repo_url, number, forge),
                        merged_at=at,
                        merged_sha=merged_sha,
                        refusals=refusals_before[number],
                    )
                )
            continue
        if outcome in NOT_A_REFUSAL:
            continue
        refusals_before[number] += 1
        if at >= since:
            refused.append(
                RefusedLgtm(
                    pr=number,
                    pr_url=pr_link(repo_url, number, forge),
                    at=at,
                    outcome=outcome,
                    fact=notes,
                    base_ref=base_ref,
                    head_ref=head_ref,
                )
            )
    return refused, passed


def _cell(value: str) -> str:
    return " ".join(value.replace("|", "\\|").split())


def render_markdown(report: Digest) -> str:
    """Return the digest as Markdown: counts, then one table per list."""
    c = report.counts
    lines = [
        f"# What the gate refused since {report.since.date().isoformat()}",
        "",
        f"- refuted findings: {c['refuted_findings']}",
        f"- refused LGTMs: {c['refused_lgtms']}",
        f"- merges that passed after a refusal: {c['passed_after_refusal']}",
        f"- pull requests: {c['prs']}",
        "",
        "## Refuted findings",
        "",
    ]
    if report.refuted_findings:
        lines += [
            "| PR | reviewer | claim | where | why refuted |",
            "|---|---|---|---|---|",
        ]
        lines += [
            f"| [#{f.pr}]({f.pr_url}) | {f.reviewer} | {_cell(f.claim)} | "
            f"`{f.file}:{f.line}` | {_cell(f.reason) or 'no reason recorded'} |"
            for f in report.refuted_findings
        ]
    else:
        lines.append("None.")
    lines += ["", "## Refused LGTMs", ""]
    if report.refused_lgtms:
        lines += ["| PR | when | outcome | gate fact |", "|---|---|---|---|"]
        lines += [
            f"| [#{r.pr}]({r.pr_url}) | {r.at.strftime('%Y-%m-%d %H:%M')} | {r.outcome} | "
            f"{_cell(r.fact) or 'no fact recorded'} |"
            for r in report.refused_lgtms
        ]
    else:
        lines.append("None.")
    lines += ["", "## Passed after a refusal", ""]
    if report.passed_after_refusal:
        lines += ["| PR | merged | refusals before |", "|---|---|---:|"]
        lines += [
            f"| [#{p.pr}]({p.pr_url}) | {p.merged_at.strftime('%Y-%m-%d %H:%M')} | {p.refusals} |"
            for p in report.passed_after_refusal
        ]
    else:
        lines.append("None.")
    return "\n".join(lines) + "\n"


def render_json(report: Digest) -> str:
    """Return the digest as one JSON document."""
    return json.dumps(json.loads(report.model_dump_json()), indent=2)


__all__ = [
    "NOT_A_REFUSAL",
    "Digest",
    "PassedAfterRefusal",
    "RefusedLgtm",
    "RefutedFinding",
    "digest",
    "pr_link",
    "render_json",
    "render_markdown",
    "web_url",
]
