"""The usage ledger: one row per model call, summed per review and per period (SP-USAGE-LEDGER).

Every provider call the agent loop makes passes through
:func:`record_call`, the one write site (A1): one ``usage_ledger`` row in
the review database (``settings.db_path``) with the provider, the model,
the token counts, the duration, the cost estimate
(:func:`~repoach.providers.prices.price_call`, None when unpriced or on a
subscription) and the error when the call failed.

Attribution travels in context variables, not arguments:
:func:`usage_run` opens a run (``review pr``) whose id every row carries
and whose rows are kept in memory for the end-of-review summary;
:func:`reviewer_scope` / :func:`bind_reviewer` name the reviewer a call
belongs to, including across the orchestrator's worker threads.  A call
outside any run is still recorded, with no run id.

An unwritable ledger never fails a call: ``usage_ledger.write_failed`` is
logged once per process and the run's summary says ``usage not recorded``.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, ParamSpec, TypeVar

from pydantic import BaseModel, ConfigDict
from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    insert,
    select,
)
from sqlalchemy.exc import SQLAlchemyError

from ..core.config import get_settings
from ..core.logging import get_logger
from ..core.sqlite_schema_init import ensure_schema_created
from ..llm.capability import CapabilityTier
from ..llm_proxy.api.models.agent_v1 import AgentResponse
from .prices import (
    PRICES_READ_ON,
    SUBSCRIPTION_PROVIDERS,
    CallCost,
    price_call,
    prices_stale,
)

_log = get_logger(__name__)

P = ParamSpec("P")
T = TypeVar("T")

_metadata = MetaData()

usage_ledger = Table(
    "usage_ledger",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String, nullable=True),
    Column("command", String, nullable=True),
    Column("pr_number", Integer, nullable=True),
    Column("reviewer", String, nullable=True),
    Column("provider", String, nullable=False),
    Column("model", String, nullable=False),
    Column("prompt_tokens", Integer, nullable=False),
    Column("completion_tokens", Integer, nullable=False),
    Column("cache_read_tokens", Integer, nullable=False),
    Column("cache_write_tokens", Integer, nullable=False),
    Column("duration_ms", Integer, nullable=False),
    Column("cost_usd", Float, nullable=True),
    Column("price_source", String, nullable=True),
    Column("error", String, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Index("ix_usage_ledger_created_at", "created_at"),
    Index("ix_usage_ledger_run_id", "run_id"),
)
"""``db:table:usage_ledger`` (owned by SP-UNIVERSAL-PROVIDERS)."""

SUBSCRIPTION_LABEL = "subscription (tokens only)"


class UsageRow(BaseModel):
    """One model call as the ledger records it.

    Attributes:
        run_id: The :func:`usage_run` id, None outside a run.
        command: The command of the run (``review pr``), None outside one.
        pr_number: The PR / MR the run reviews, None when none.
        reviewer: ``Architect``, ``Sentinel``, ``Tester``, ``Scribe``,
            ``Refuter``…, None when the call was not attributed.
        provider: The route label (``claude_code``, ``anthropic``,
            ``ollama``, ``gemini``, ``gateway``…).
        model: The model id as sent (the gateway: the upstream it reported).
        prompt_tokens: Uncached prompt tokens.
        completion_tokens: Completion tokens (reasoning included).
        cache_read_tokens: Prompt-cache hits.
        cache_write_tokens: Prompt-cache writes.
        duration_ms: Wall time of the call.
        cost_usd: Estimate in USD; None when unpriced, on a subscription, or
            when the call failed (no answer, no token counts).
        price_source: The price row used, ``self-hosted``, or None.
        subscription: The provider bills a subscription (never stored:
            derived from ``provider``).
        error: The error type and message when the call failed.
        created_at: When the call ended (UTC).
    """

    model_config = ConfigDict(frozen=True)

    run_id: str | None = None
    command: str | None = None
    pr_number: int | None = None
    reviewer: str | None = None
    provider: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    duration_ms: int = 0
    cost_usd: float | None = None
    price_source: str | None = None
    subscription: bool = False
    error: str | None = None
    created_at: datetime


@dataclass
class UsageRun:
    """One command's calls, kept in memory for its end-of-run summary.

    Attributes:
        run_id: Carried by every row of the run.
        command: ``review pr``…
        pr_number: The PR / MR, None when the command has none.
        started: ``time.monotonic()`` at the start of the run.
        rows: The rows recorded so far, in completion order.
        recorded: False once a row could not be written to the ledger.
    """

    run_id: str
    command: str
    pr_number: int | None
    started: float
    rows: list[UsageRow] = field(default_factory=list)
    recorded: bool = True
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, row: UsageRow) -> None:
        """Append ``row`` (called from every reviewer thread)."""
        with self._lock:
            self.rows.append(row)

    def snapshot(self) -> list[UsageRow]:
        """Return the rows recorded so far."""
        with self._lock:
            return list(self.rows)

    def wall_s(self) -> float:
        """Seconds since the run started."""
        return time.monotonic() - self.started


_RUN: ContextVar[UsageRun | None] = ContextVar("repoach_usage_run", default=None)
_REVIEWER: ContextVar[str | None] = ContextVar("repoach_usage_reviewer", default=None)

_SCHEMA_READY: set[Path] = set()
_SCHEMA_LOCK = threading.Lock()
_WRITE_FAILED_LOGGED = threading.Event()


@contextmanager
def usage_run(command: str, pr_number: int | None = None) -> Iterator[UsageRun]:
    """Open a run: every call made inside it (and in threads bound to it) carries its id.

    Args:
        command: The command name (``review pr``).
        pr_number: The PR / MR the command works on.

    Yields:
        The run, whose rows the caller summarises at the end.
    """
    run = UsageRun(
        run_id=uuid.uuid4().hex[:12],
        command=command,
        pr_number=pr_number,
        started=time.monotonic(),
    )
    token = _RUN.set(run)
    try:
        yield run
    finally:
        _RUN.reset(token)


@contextmanager
def reviewer_scope(name: str) -> Iterator[None]:
    """Attribute every call made inside the block to reviewer ``name``."""
    token = _REVIEWER.set(name)
    try:
        yield
    finally:
        _REVIEWER.reset(token)


def bind_reviewer(name: str, fn: Callable[P, T]) -> Callable[P, T]:
    """Return ``fn`` bound to the current run and to reviewer ``name``, for a worker thread.

    A ``ThreadPoolExecutor`` worker does not inherit the submitting
    thread's context variables; the wrapper runs ``fn`` in a copy of the
    context taken now, with the reviewer set.
    """
    context = copy_context()

    def run_in_context(*args: P.args, **kwargs: P.kwargs) -> T:
        def attributed() -> T:
            with reviewer_scope(name):
                return fn(*args, **kwargs)

        return context.run(attributed)

    return run_in_context


def _model_of(client: Any, capability: CapabilityTier, response: AgentResponse | None) -> str:
    """Return the model id a call used: the tier's model, else what the response reports."""
    name = str(getattr(client, "name", "") or "")
    models = getattr(client, "models", None)
    configured = models.get(capability) if isinstance(models, dict) else None
    if response is not None and response.model_used:
        reported = response.model_used
        prefix = f"{name}/"
        if name != "gateway" and reported.startswith(prefix):
            return reported[len(prefix) :]
        if name == "gateway":
            return reported
    return str(configured or "unknown")


def _engine(db_path: Path) -> Any:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path}",
        future=True,
        connect_args={"timeout": 30, "check_same_thread": False},
    )
    with _SCHEMA_LOCK:
        if db_path not in _SCHEMA_READY:
            ensure_schema_created(engine, _metadata)
            _SCHEMA_READY.add(db_path)
    return engine


def _ledger_path() -> Path:
    return Path(get_settings().db_path)


def write_row(row: UsageRow, db_path: Path) -> None:
    """Insert ``row`` into ``usage_ledger`` at ``db_path`` (the table is created on first use)."""
    engine = _engine(db_path)
    values = row.model_dump(exclude={"subscription"})
    with engine.begin() as conn:
        conn.execute(insert(usage_ledger).values(**values))


def _build_row(
    client: Any,
    capability: CapabilityTier,
    response: AgentResponse | None,
    *,
    duration_s: float,
    error: BaseException | None,
    run: UsageRun | None,
) -> UsageRow:
    """Price one call and return its row."""
    provider = str(getattr(client, "name", "") or "unknown")
    model = _model_of(client, capability, response)
    usage = response.usage if response is not None else None
    prompt = int(usage.input_tokens) if usage is not None else 0
    completion = int(usage.output_tokens) if usage is not None else 0
    cache_read = int(usage.cache_read_tokens) if usage is not None else 0
    cache_write = int(usage.cache_write_tokens) if usage is not None else 0
    now = datetime.now(UTC)
    cost = price_call(
        provider,
        model,
        input_tokens=prompt,
        output_tokens=completion,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        on=now.date(),
    )
    if response is None:
        cost = CallCost(cost_usd=None, price_source=None, subscription=cost.subscription)
    return UsageRow(
        run_id=run.run_id if run is not None else None,
        command=run.command if run is not None else None,
        pr_number=run.pr_number if run is not None else None,
        reviewer=_REVIEWER.get(),
        provider=provider,
        model=model,
        prompt_tokens=prompt,
        completion_tokens=completion,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        duration_ms=int(duration_s * 1000),
        cost_usd=float(cost.cost_usd) if cost.cost_usd is not None else None,
        price_source=cost.price_source,
        subscription=cost.subscription,
        error=f"{type(error).__name__}: {str(error)[:300]}" if error is not None else None,
        created_at=now,
    )


def _first_failure() -> bool:
    """True the first time a ledger failure is seen in this process (warn once, then debug)."""
    if _WRITE_FAILED_LOGGED.is_set():
        return False
    _WRITE_FAILED_LOGGED.set()
    return True


def record_call(
    client: Any,
    capability: CapabilityTier,
    response: AgentResponse | None,
    *,
    duration_s: float,
    error: BaseException | None = None,
    db_path: Path | None = None,
) -> UsageRow | None:
    """Record one provider call: price it, attach it to the current run, write it.

    Never raises into the call path: a response the ledger cannot read
    (``usage_ledger.record_failed``) or a ledger it cannot write
    (``usage_ledger.write_failed``) is logged as a warning once per
    process and marks the run ``usage not recorded``.

    Args:
        client: The provider (its ``name`` and ``models`` are read).
        capability: The tier the call asked for.
        response: The answer, None when the call raised.
        duration_s: Wall time of the call.
        error: The exception the call raised, None on success.
        db_path: The ledger; ``settings.db_path`` when omitted.

    Returns:
        The row (also kept by the current run, if any), None when it
        could not be built.
    """
    run = _RUN.get()
    try:
        row = _build_row(client, capability, response, duration_s=duration_s, error=error, run=run)
    except (AttributeError, TypeError, ValueError) as exc:
        if run is not None:
            run.recorded = False
        if _first_failure():
            _log.warning(
                "usage_ledger.record_failed", exc=type(exc).__name__, message=str(exc)[:200]
            )
        else:
            _log.debug("usage_ledger.record_failed", exc=type(exc).__name__, message=str(exc)[:200])
        return None
    if run is not None:
        run.add(row)
    try:
        write_row(row, db_path or _ledger_path())
    except (OSError, SQLAlchemyError, AttributeError, TypeError, ValueError) as exc:
        if run is not None:
            run.recorded = False
        if _first_failure():
            _log.warning(
                "usage_ledger.write_failed", exc=type(exc).__name__, message=str(exc)[:200]
            )
        else:
            _log.debug("usage_ledger.write_failed", exc=type(exc).__name__, message=str(exc)[:200])
    return row


class UsageTotals(BaseModel):
    """Sums over a set of rows.

    Attributes:
        calls: Rows summed.
        prompt_tokens: Uncached prompt tokens.
        completion_tokens: Completion tokens.
        cache_read_tokens: Prompt-cache hits.
        cache_write_tokens: Prompt-cache writes.
        total_tokens: The four token counts summed.
        duration_ms: Call durations summed.
        cost_usd: Sum of the priced rows, None when no row is priced.
        unpriced_calls: Answered rows with no price that are not on a subscription.
        subscription_calls: Rows on a subscription.
        errors: Rows whose call failed.
    """

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0
    duration_ms: int = 0
    cost_usd: float | None = None
    unpriced_calls: int = 0
    subscription_calls: int = 0
    errors: int = 0


def totals(rows: list[UsageRow]) -> UsageTotals:
    """Sum ``rows``; ``cost_usd`` sums the priced rows only."""
    priced = [Decimal(str(r.cost_usd)) for r in rows if r.cost_usd is not None]
    prompt = sum(r.prompt_tokens for r in rows)
    completion = sum(r.completion_tokens for r in rows)
    cache_read = sum(r.cache_read_tokens for r in rows)
    cache_write = sum(r.cache_write_tokens for r in rows)
    return UsageTotals(
        calls=len(rows),
        prompt_tokens=prompt,
        completion_tokens=completion,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        total_tokens=prompt + completion + cache_read + cache_write,
        duration_ms=sum(r.duration_ms for r in rows),
        cost_usd=float(sum(priced, Decimal(0))) if priced else None,
        unpriced_calls=sum(
            1 for r in rows if r.cost_usd is None and not r.subscription and r.error is None
        ),
        subscription_calls=sum(1 for r in rows if r.subscription),
        errors=sum(1 for r in rows if r.error is not None),
    )


def cost_text(total: UsageTotals) -> str:
    """Say what ``total`` cost: a dollar figure, ``subscription (tokens only)`` or ``cannot say``.

    A figure mixing priced and unpriced rows is the priced sum plus
    ``+ N unpriced calls``; a run read more than 90 days after the price
    table adds ``may be stale``.
    """
    parts: list[str] = []
    if total.cost_usd is not None:
        stale = ", may be stale" if prices_stale(datetime.now(UTC).date()) else ""
        parts.append(f"${total.cost_usd:.4f} (prices read {PRICES_READ_ON.isoformat()}{stale})")
    if total.subscription_calls:
        parts.append(SUBSCRIPTION_LABEL)
    if total.unpriced_calls:
        if parts:
            parts.append(f"+ {total.unpriced_calls} unpriced calls")
        else:
            parts.append(f"cannot say ({total.unpriced_calls} unpriced calls)")
    if not parts:
        return "no calls"
    return " ".join(parts)


def run_summary_line(run: UsageRun) -> str:
    """The one line ``review pr`` prints on stderr after its verdict."""
    rows = run.snapshot()
    total = totals(rows)
    line = (
        f"usage: {total.calls} calls, {total.total_tokens} tokens "
        f"(in {total.prompt_tokens}, out {total.completion_tokens}, "
        f"cached {total.cache_read_tokens + total.cache_write_tokens}), "
        f"{run.wall_s():.1f} s, {cost_text(total)}"
    )
    if not run.recorded:
        line += "; usage not recorded"
    return line


def _group(rows: list[UsageRow], key: Callable[[UsageRow], str]) -> list[tuple[str, UsageTotals]]:
    groups: dict[str, list[UsageRow]] = {}
    for row in rows:
        groups.setdefault(key(row), []).append(row)
    return [(name, totals(members)) for name, members in sorted(groups.items())]


def run_usage_json(run: UsageRun) -> dict[str, object]:
    """The ``usage`` object of ``review pr``'s JSON: per reviewer, then the total."""
    rows = run.snapshot()
    total = totals(rows)
    per_reviewer = [
        {"reviewer": name, **t.model_dump()}
        for name, t in _group(rows, lambda r: r.reviewer or "(unattributed)")
    ]
    return {
        "run_id": run.run_id,
        "per_reviewer": per_reviewer,
        "total": {**total.model_dump(), "wall_s": round(run.wall_s(), 2), "cost": cost_text(total)},
        "providers": sorted({r.provider for r in rows}),
        "prices_read_on": PRICES_READ_ON.isoformat(),
        "recorded": run.recorded,
    }


GroupBy = Literal["model", "provider", "reviewer", "pr", "day", "command"]

_GROUP_KEYS: dict[str, Callable[[UsageRow], str]] = {
    "model": lambda r: f"{r.provider}/{r.model}",
    "provider": lambda r: r.provider,
    "reviewer": lambda r: r.reviewer or "(unattributed)",
    "pr": lambda r: f"#{r.pr_number}" if r.pr_number is not None else "(none)",
    "day": lambda r: r.created_at.date().isoformat(),
    "command": lambda r: r.command or "(none)",
}

_DURATION = re.compile(r"^(?P<n>\d+)(?P<unit>[hdw])$")


def parse_since(value: str, *, now: datetime) -> datetime:
    """Parse ``--since``: ``7d``, ``24h``, ``2w`` or an ISO date (``2026-10-01``), UTC.

    Raises:
        ValueError: Neither form; the message shows both.
    """
    text = value.strip()
    match = _DURATION.match(text)
    if match is not None:
        n = int(match.group("n"))
        unit = match.group("unit")
        delta = {"h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]
        return now - delta
    try:
        day = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            f"--since {value!r}: use a duration (7d, 24h, 2w) or a date (2026-10-01)"
        ) from exc
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def read_rows(
    db_path: Path, *, since: datetime | None = None, until: datetime | None = None
) -> list[UsageRow]:
    """Return the ledger rows in ``[since, until)``, oldest first (none when the ledger is absent)."""
    if not db_path.is_file():
        return []
    engine = _engine(db_path)
    query = select(usage_ledger).order_by(usage_ledger.c.created_at)
    with engine.connect() as conn:
        records = conn.execute(query).mappings().all()
    rows: list[UsageRow] = []
    for record in records:
        created = _as_utc(record["created_at"])
        if since is not None and created < since:
            continue
        if until is not None and created >= until:
            continue
        data = dict(record)
        data.pop("id", None)
        data["created_at"] = created
        data["subscription"] = record["provider"] in SUBSCRIPTION_PROVIDERS
        rows.append(UsageRow.model_validate(data))
    return rows


class UsageGroup(UsageTotals):
    """One group of :func:`usage_report`: its key, its sums, its cost in words.

    Attributes:
        key: The group (``provider/model``, a reviewer, ``#42``, a day…).
        cost: :func:`cost_text` of the group.
    """

    key: str
    cost: str


class UsageReport(BaseModel):
    """What ``repoach usage`` prints (``--json``: this model's JSON).

    Attributes:
        by: The grouping.
        rows: One group per key, sorted by key.
        totals: The sums over every row (key ``total``).
        prices_read_on: The day the price table was read.
    """

    by: str
    rows: list[UsageGroup]
    totals: UsageGroup
    prices_read_on: str


def _group_of(key: str, rows: list[UsageRow]) -> UsageGroup:
    total = totals(rows)
    return UsageGroup(key=key, cost=cost_text(total), **total.model_dump())


def usage_report(rows: list[UsageRow], *, by: str) -> UsageReport:
    """Group ``rows`` by ``by`` (:data:`GroupBy`) and sum each group."""
    key = _GROUP_KEYS[by]
    groups: dict[str, list[UsageRow]] = {}
    for row in rows:
        groups.setdefault(key(row), []).append(row)
    return UsageReport(
        by=by,
        rows=[_group_of(name, members) for name, members in sorted(groups.items())],
        totals=_group_of("total", rows),
        prices_read_on=PRICES_READ_ON.isoformat(),
    )


def usage_table(report: UsageReport) -> list[str]:
    """Render ``report`` as aligned text lines, the total last."""
    header = ("key", "calls", "in", "out", "cached", "wall_s", "cost")
    body = [
        (
            group.key,
            str(group.calls),
            str(group.prompt_tokens),
            str(group.completion_tokens),
            str(group.cache_read_tokens + group.cache_write_tokens),
            f"{group.duration_ms / 1000:.1f}",
            group.cost,
        )
        for group in [*report.rows, report.totals]
    ]
    widths = [max(len(header[i]), *(len(line[i]) for line in body)) for i in range(len(header))]
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(header))]
    lines.extend("  ".join(c.ljust(widths[i]) for i, c in enumerate(line)) for line in body)
    return [line.rstrip() for line in lines]


__all__ = [
    "SUBSCRIPTION_LABEL",
    "UsageGroup",
    "UsageReport",
    "UsageRow",
    "UsageRun",
    "UsageTotals",
    "bind_reviewer",
    "cost_text",
    "parse_since",
    "read_rows",
    "record_call",
    "reviewer_scope",
    "run_summary_line",
    "run_usage_json",
    "totals",
    "usage_ledger",
    "usage_report",
    "usage_run",
    "usage_table",
    "write_row",
]
