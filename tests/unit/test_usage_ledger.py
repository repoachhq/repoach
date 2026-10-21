"""The usage ledger: one row per call, priced from a dated table, summed per run and period.

SP-USAGE-LEDGER (slice 2 of SP-UNIVERSAL-PROVIDERS).  Calls go through
the real transports against the loopback fakes of
:mod:`tests.provider_fakes`; the ledger is a real SQLite file.
"""

from __future__ import annotations

import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from structlog.testing import capture_logs
from typer.testing import CliRunner

from repoach.agent_engine.agent_loop import AgentLoop
from repoach.cli.main import app
from repoach.core import config as core_config
from repoach.llm.capability import CapabilityTier
from repoach.providers import prices as prices_module
from repoach.providers import usage as usage_module
from repoach.providers.prices import (
    ANTHROPIC_HOST,
    PRICES,
    PRICES_READ_ON,
    price_call,
    price_row,
    prices_stale,
)
from repoach.providers.usage import (
    UsageRow,
    bind_reviewer,
    cost_text,
    parse_since,
    read_rows,
    reviewer_scope,
    run_summary_line,
    run_usage_json,
    totals,
    usage_run,
    write_row,
)
from tests.provider_fakes import FakeAnthropicServer, write_fake_claude
from tests.unit.test_provider_select import PROVIDER_ENV
from tests.unit.test_universal_install import _repo


@pytest.fixture
def clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg_config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg_data"))
    monkeypatch.setenv("REPOACH_DB_PATH", str(tmp_path / "review.db"))
    for name in PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("REPOACH_LLM_PROXY_BASE_URL", raising=False)
    repo = _repo(tmp_path / "clone")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(core_config, "_settings", None)
    return repo


def _row(**fields: object) -> UsageRow:
    base: dict[str, object] = {
        "provider": "anthropic",
        "model": "claude-sonnet-5",
        "created_at": datetime(2026, 10, 21, 12, tzinfo=UTC),
    }
    base.update(fields)
    return UsageRow.model_validate(base)


def _price_the_fake_host(monkeypatch: pytest.MonkeyPatch, *models: str) -> None:
    """Two-row price fixture: the shipped Anthropic rows, keyed on the loopback host."""
    for model in models:
        row = price_row(ANTHROPIC_HOST, model, on=PRICES_READ_ON)
        assert row is not None
        monkeypatch.setitem(
            prices_module._BY_KEY,
            ("127.0.0.1", model),
            row.model_copy(update={"host": "127.0.0.1"}),
        )


def test_anthropic_calls_are_priced_from_the_table_and_attributed(
    clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _price_the_fake_host(monkeypatch, "claude-sonnet-5", "claude-opus-5")
    monkeypatch.setenv("REPOACH_PROVIDER", "anthropic")
    monkeypatch.setenv("REPOACH_ANTHROPIC_API_KEY", "some-key-value")
    with FakeAnthropicServer(text="ok") as server:
        monkeypatch.setenv("REPOACH_ANTHROPIC_BASE_URL", server.base_url)
        with usage_run("review pr", 7) as run:
            with reviewer_scope("Architect"):
                AgentLoop(capability=CapabilityTier.SONNET).run_oneshot("x")
            AgentLoop(capability=CapabilityTier.OPUS).run_oneshot("y")
    rows = read_rows(tmp_path / "review.db")
    assert [(r.reviewer, r.model) for r in rows] == [
        ("Architect", "claude-sonnet-5"),
        (None, "claude-opus-5"),
    ]
    assert {(r.run_id, r.pr_number, r.command) for r in rows} == {(run.run_id, 7, "review pr")}
    assert rows[0].cost_usd == pytest.approx((12 * 2 + 9 * 10) / 1_000_000)
    assert rows[1].cost_usd == pytest.approx((12 * 5 + 9 * 25) / 1_000_000)
    assert rows[0].price_source == f"anthropic/claude-sonnet-5 read {PRICES_READ_ON}"
    line = run_summary_line(run)
    assert line.startswith(
        "usage: 2 calls, 42 tokens (in 24, out 18, cache_read 0, cache_write 0), "
    )
    assert "$0.0004 (prices read 2026-10-21)" in line
    document = run_usage_json(run)
    assert [entry["reviewer"] for entry in document["per_reviewer"]] == [
        "(unattributed)",
        "Architect",
    ]
    assert document["total"]["calls"] == 2


def test_a_call_at_another_host_is_never_priced_at_list_price(
    clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An Anthropic model reached through a proxy host is unpriced, not billed at the API's list price."""
    monkeypatch.setenv("REPOACH_PROVIDER", "anthropic")
    monkeypatch.setenv("REPOACH_ANTHROPIC_API_KEY", "some-key-value")
    with FakeAnthropicServer(text="ok") as server:
        monkeypatch.setenv("REPOACH_ANTHROPIC_BASE_URL", server.base_url)
        with usage_run("review pr", 7) as run:
            AgentLoop(capability=CapabilityTier.SONNET).run_oneshot("x")
    (row,) = read_rows(tmp_path / "review.db")
    assert (row.model, row.cost_usd, row.price_source) == ("claude-sonnet-5", None, None)
    assert run_summary_line(run).endswith("cannot say (1 unpriced calls)")


def test_claude_code_is_tokens_only_never_a_dollar_figure(
    clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPOACH_PROVIDER", "claude_code")
    cli, _log = write_fake_claude(tmp_path / "bin", result="ok")
    monkeypatch.setenv("PATH", f"{cli.parent}:{os.environ['PATH']}")
    with usage_run("review pr", 9) as run:
        AgentLoop(capability=CapabilityTier.HAIKU).run_oneshot("x")
    (row,) = read_rows(tmp_path / "review.db")
    assert (row.provider, row.model) == ("claude_code", "haiku")
    assert (row.prompt_tokens, row.completion_tokens) == (11, 7)
    assert (row.cost_usd, row.price_source, row.subscription) == (None, None, True)
    line = run_summary_line(run)
    assert line.endswith("subscription (tokens only)")
    assert "$" not in line


def test_rejected_key_is_a_row_with_its_error_and_no_cost(
    clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPOACH_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "rejected-key-value")
    with FakeAnthropicServer(status=401) as server:
        monkeypatch.setenv("REPOACH_ANTHROPIC_BASE_URL", server.base_url)
        with pytest.raises(Exception, match="401"):
            AgentLoop(capability=CapabilityTier.SONNET).run_oneshot("x")
    (row,) = read_rows(tmp_path / "review.db")
    assert row.error is not None and row.error.startswith("ProviderAuthError: ")
    assert "rejected-key-value" not in row.error
    assert (row.cost_usd, row.prompt_tokens) == (None, 0)
    assert run_summary_line_for([row]).startswith(
        "usage: 1 calls, all failed (ProviderAuthError), "
    )
    assert totals([row]).unpriced_calls == 0
    assert totals([row]).errors == 1


def test_unwritable_ledger_never_fails_the_call_and_says_so(
    clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setenv("REPOACH_DB_PATH", str(blocker / "review.db"))
    monkeypatch.setenv("REPOACH_PROVIDER", "anthropic")
    monkeypatch.setenv("REPOACH_ANTHROPIC_API_KEY", "some-key-value")
    monkeypatch.setattr(
        usage_module, "_WRITE_FAILED_LOGGED", type(usage_module._WRITE_FAILED_LOGGED)()
    )
    with FakeAnthropicServer(text="ok") as server:
        monkeypatch.setenv("REPOACH_ANTHROPIC_BASE_URL", server.base_url)
        with capture_logs() as logs, usage_run("review pr", 1) as run:
            out = AgentLoop(capability=CapabilityTier.SONNET).run_oneshot("x")
            AgentLoop(capability=CapabilityTier.SONNET).run_oneshot("x")
    assert out.text == "ok"
    warnings = [e for e in logs if e["event"] == "usage_ledger.write_failed"]
    assert [e["log_level"] for e in warnings if e["log_level"] == "warning"] == ["warning"]
    assert run_summary_line(run).endswith("; usage not recorded")
    assert len(run.snapshot()) == 2


def test_bound_reviewers_keep_their_names_across_worker_threads(clone: Path) -> None:
    seen: list[tuple[str | None, str | None]] = []

    def work() -> None:
        current = usage_module._RUN.get()
        seen.append((usage_module._REVIEWER.get(), current.run_id if current else None))

    with usage_run("review pr", 3) as run, ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(bind_reviewer(name, work)) for name in ("Sentinel", "Scribe")]
        for future in futures:
            future.result()
    assert sorted(seen) == [("Scribe", run.run_id), ("Sentinel", run.run_id)]


def test_every_price_row_names_its_source_and_day() -> None:
    assert PRICES
    for row in PRICES:
        assert row.source_url.startswith("https://")
        assert row.read_on == date(2026, 10, 21)
    assert {(r.provider, r.model) for r in PRICES} >= {
        ("anthropic", "claude-opus-5"),
        ("anthropic", "claude-sonnet-5"),
        ("anthropic", "claude-haiku-4-5"),
    }


def test_price_call_hand_computed_with_cache_rows() -> None:
    cost = price_call(
        "anthropic",
        "claude-opus-5",
        host="api.anthropic.com",
        input_tokens=1_000_000,
        output_tokens=100_000,
        cache_read_tokens=200_000,
        cache_write_tokens=40_000,
        on=date(2026, 10, 21),
    )
    assert cost.cost_usd == Decimal("5") + Decimal("2.5") + Decimal("0.1") + Decimal("0.25")
    assert price_call(
        "ollama",
        "anything",
        host="localhost",
        input_tokens=9,
        output_tokens=9,
        cache_read_tokens=0,
        cache_write_tokens=0,
        on=date(2026, 10, 21),
    ).cost_usd == Decimal(0)
    unpriced = price_call(
        "openrouter",
        "google/gemini-3.8-flash",
        host="openrouter.ai",
        input_tokens=9,
        output_tokens=9,
        cache_read_tokens=0,
        cache_write_tokens=0,
        on=date(2026, 10, 21),
    )
    assert (unpriced.cost_usd, unpriced.price_source) == (None, None)
    for host in ("127.0.0.1", "api.groq.com", "openrouter.ai"):
        elsewhere = price_call(
            "openai_compatible",
            "gpt-6-luna",
            host=host,
            input_tokens=9,
            output_tokens=9,
            cache_read_tokens=0,
            cache_write_tokens=0,
            on=date(2026, 10, 21),
        )
        assert elsewhere.cost_usd is None
    gemini = price_call(
        "gemini",
        "gemini-2.5-flash",
        host="generativelanguage.googleapis.com",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        on=date(2026, 10, 21),
    )
    assert gemini.cost_usd == Decimal("0.30")


def test_a_temporary_price_ends_on_its_announced_day() -> None:
    host = "generativelanguage.googleapis.com"
    assert price_row(host, "gemini-3.8-flash", on=date(2026, 12, 31)) is not None
    assert price_row(host, "gemini-3.8-flash", on=date(2027, 1, 1)) is None


def test_cost_text_mixes_priced_subscription_and_unpriced() -> None:
    priced = _row(cost_usd=0.5)
    unpriced = _row(provider="gateway", model="nvidia_nim/x")
    subscription = _row(provider="claude_code", model="opus", subscription=True)
    assert cost_text(totals([priced, unpriced])).endswith("+ 1 unpriced calls")
    assert cost_text(totals([unpriced])) == "cannot say (1 unpriced calls)"
    assert cost_text(totals([subscription])) == "subscription (tokens only)"
    assert cost_text(totals([])) == "no calls"
    assert prices_stale(PRICES_READ_ON + timedelta(days=91))
    assert not prices_stale(PRICES_READ_ON + timedelta(days=90))


def test_parse_since_durations_and_dates() -> None:
    now = datetime(2026, 10, 21, 12, tzinfo=UTC)
    assert parse_since("7d", now=now) == now - timedelta(days=7)
    assert parse_since("24h", now=now) == now - timedelta(hours=24)
    assert parse_since("2026-10-01", now=now) == datetime(2026, 10, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="7d, 24h, 2w"):
        parse_since("last week", now=now)


def test_usage_command_groups_the_period(clone: Path, tmp_path: Path) -> None:
    db = tmp_path / "review.db"
    now = datetime.now(UTC)
    write_row(_row(reviewer="Architect", cost_usd=0.25, prompt_tokens=10, created_at=now), db)
    write_row(_row(reviewer="Sentinel", cost_usd=0.5, prompt_tokens=20, created_at=now), db)
    write_row(_row(provider="claude_code", model="opus", reviewer="Architect", created_at=now), db)
    write_row(_row(reviewer="Old", created_at=now - timedelta(days=30)), db)
    result = CliRunner().invoke(app, ["usage", "--since", "7d", "--by", "reviewer", "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert {"rows", "totals"} <= report.keys()
    assert [row["key"] for row in report["rows"]] == ["Architect", "Sentinel"]
    assert report["totals"]["calls"] == 3
    assert report["totals"]["cost_usd"] == pytest.approx(0.75)
    assert "subscription (tokens only)" in report["totals"]["cost"]
    table = CliRunner().invoke(app, ["usage", "--since", "7d"])
    assert table.exit_code == 0
    lines = table.stdout.splitlines()
    assert lines[1].split() == [
        "key",
        "calls",
        "in",
        "out",
        "cache_read",
        "cache_write",
        "wall_s",
        "cost",
    ]
    assert lines[-1].startswith("total")
    with sqlite3.connect(db) as conn:
        assert conn.execute("select count(*) from usage_ledger").fetchone() == (4,)


def test_usage_command_rejects_a_bad_period(clone: Path) -> None:
    result = CliRunner().invoke(app, ["usage", "--since", "soon"])
    assert result.exit_code == 2
    assert "7d, 24h, 2w" in result.stderr


def run_summary_line_for(rows: list[UsageRow]) -> str:
    """The summary line of a run holding exactly ``rows``."""
    with usage_run("review pr", 1) as run:
        for row in rows:
            run.add(row)
    return run_summary_line(run)


def test_summary_splits_cache_read_and_write(clone: Path) -> None:
    line = run_summary_line_for(
        [_row(prompt_tokens=2, completion_tokens=3, cache_read_tokens=5, cache_write_tokens=7)]
    )
    assert "(in 2, out 3, cache_read 5, cache_write 7)" in line
