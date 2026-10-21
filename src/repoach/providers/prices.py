"""List prices per provider and model, read from the vendors' pages on a stated day (SP-USAGE-LEDGER G2).

Every row names the page it was read from and the day it was read
(:data:`PRICES_READ_ON`); nothing is fetched at runtime (NG3).  A row
whose vendor announced an end date carries ``valid_until``: after it the
row prices nothing and the call counts as unpriced, never at a guessed
new price.

Token semantics are the Messages API's, which every route reports
(:class:`~repoach.llm_proxy.api.models.agent_v1.Usage`): ``input`` is the
uncached prompt, ``cache_read`` and ``cache_write`` are apart.

* ``claude_code`` bills a subscription: :data:`SUBSCRIPTION_PROVIDERS`,
  cost always None, shown as ``subscription (tokens only)`` (NG2).
* ``ollama`` runs on the user's machine: :data:`SELF_HOSTED_PROVIDERS`,
  cost 0 whatever the model.
* the gateway and any (provider, model) absent here: cost None, shown
  as unpriced.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

PRICES_READ_ON = date(2026, 10, 21)
"""The day every row below was read from its vendor page."""

STALE_AFTER_DAYS = 90
"""A table read longer ago than this is printed with ``may be stale``."""

ANTHROPIC_PRICING_URL = "https://platform.claude.com/docs/en/about-claude/pricing"
OPENAI_PRICING_URL = "https://developers.openai.com/api/docs/pricing"
GEMINI_PRICING_URL = "https://ai.google.dev/gemini-api/docs/pricing"

SUBSCRIPTION_PROVIDERS: frozenset[str] = frozenset({"claude_code"})
"""Providers billed by a subscription: tokens are recorded, a cost never is."""

SELF_HOSTED_PROVIDERS: frozenset[str] = frozenset({"ollama"})
"""Providers that run on the user's own machine: cost 0 for every model."""

_MILLION = Decimal(1_000_000)


class PriceRow(BaseModel):
    """List price of one model, USD per million tokens.

    Attributes:
        provider: The route label (``anthropic``, ``openai``, ``gemini``…).
        model: The model id exactly as sent.
        input_per_mtok: Uncached prompt tokens.
        output_per_mtok: Completion tokens.
        cache_write_per_mtok: Prompt-cache writes; None when the vendor
            lists none (counted at the input price).
        cache_read_per_mtok: Prompt-cache hits; None when the vendor lists
            none (counted at the input price).
        source_url: The page the row was read from.
        read_on: The day it was read.
        valid_until: Last day of a price the vendor announced as temporary.
    """

    model_config = ConfigDict(frozen=True)

    provider: str
    model: str
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cache_write_per_mtok: Decimal | None = None
    cache_read_per_mtok: Decimal | None = None
    source_url: str
    read_on: date = PRICES_READ_ON
    valid_until: date | None = None

    @property
    def source(self) -> str:
        """``provider/model read <date>``: what a usage row cites as its price."""
        return f"{self.provider}/{self.model} read {self.read_on.isoformat()}"


def _anthropic(model: str, inp: str, cw: str, cr: str, out: str) -> PriceRow:
    return PriceRow(
        provider="anthropic",
        model=model,
        input_per_mtok=Decimal(inp),
        output_per_mtok=Decimal(out),
        cache_write_per_mtok=Decimal(cw),
        cache_read_per_mtok=Decimal(cr),
        source_url=ANTHROPIC_PRICING_URL,
    )


def _openai(model: str, inp: str, cr: str, out: str) -> PriceRow:
    return PriceRow(
        provider="openai",
        model=model,
        input_per_mtok=Decimal(inp),
        output_per_mtok=Decimal(out),
        cache_read_per_mtok=Decimal(cr),
        source_url=OPENAI_PRICING_URL,
    )


def _gemini(model: str, inp: str, cr: str, out: str, until: date | None = None) -> PriceRow:
    return PriceRow(
        provider="gemini",
        model=model,
        input_per_mtok=Decimal(inp),
        output_per_mtok=Decimal(out),
        cache_read_per_mtok=Decimal(cr),
        source_url=GEMINI_PRICING_URL,
        valid_until=until,
    )


PRICES: tuple[PriceRow, ...] = (
    _anthropic("claude-opus-5", "5", "6.25", "0.50", "25"),
    _anthropic("claude-sonnet-5", "2", "2.50", "0.20", "10"),
    _anthropic("claude-haiku-4-5", "1", "1.25", "0.10", "5"),
    _openai("gpt-6-astra", "10.00", "1.00", "50.00"),
    _openai("gpt-6.1-sol", "2.00", "0.10", "10.00"),
    _openai("gpt-6-luna", "0.10", "0.01", "0.50"),
    _gemini("gemini-3.8-flash", "0.75", "0.075", "3.75", until=date(2026, 12, 31)),
    _gemini("gemini-2.5-pro", "1.25", "0.125", "10.00"),
    _gemini("gemini-2.5-flash", "0.30", "0.03", "2.50"),
    _gemini("gemini-2.5-flash-lite", "0.10", "0.01", "0.40"),
)
"""Standard-tier list prices.  Anthropic: base input, 5-minute cache write,
cache hit, output.  OpenAI: input, cached input, output.  Gemini: paid
tier, prompts up to 200k tokens, text input, context caching, output;
``gemini-3.8-flash`` is listed at this price through 2026-12-31."""

_BY_KEY: dict[tuple[str, str], PriceRow] = {(row.provider, row.model): row for row in PRICES}


def price_row(provider: str, model: str, *, on: date) -> PriceRow | None:
    """Return the row pricing ``model`` on ``provider`` on day ``on``, else None."""
    row = _BY_KEY.get((provider, model))
    if row is None or (row.valid_until is not None and on > row.valid_until):
        return None
    return row


class CallCost(BaseModel):
    """The cost estimate of one call.

    Attributes:
        cost_usd: USD, None when the call is unpriced or on a subscription.
        price_source: The row used (:attr:`PriceRow.source`), ``self-hosted``,
            or None.
        subscription: True when the provider bills a subscription.
    """

    model_config = ConfigDict(frozen=True)

    cost_usd: Decimal | None
    price_source: str | None
    subscription: bool = False


def price_call(
    provider: str,
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    on: date,
) -> CallCost:
    """Estimate one call's cost from :data:`PRICES`.

    Args:
        provider: The route label.
        model: The model id as sent.
        input_tokens: Uncached prompt tokens.
        output_tokens: Completion tokens.
        cache_read_tokens: Prompt-cache hits.
        cache_write_tokens: Prompt-cache writes.
        on: The day of the call (a temporary price ends with ``valid_until``).

    Returns:
        The estimate; ``cost_usd`` is None on a subscription and for a
        model with no row.
    """
    if provider in SUBSCRIPTION_PROVIDERS:
        return CallCost(cost_usd=None, price_source=None, subscription=True)
    if provider in SELF_HOSTED_PROVIDERS:
        return CallCost(cost_usd=Decimal(0), price_source="self-hosted")
    row = price_row(provider, model, on=on)
    if row is None:
        return CallCost(cost_usd=None, price_source=None)
    cache_read = (
        row.cache_read_per_mtok if row.cache_read_per_mtok is not None else row.input_per_mtok
    )
    cache_write = (
        row.cache_write_per_mtok if row.cache_write_per_mtok is not None else row.input_per_mtok
    )
    total = (
        Decimal(input_tokens) * row.input_per_mtok
        + Decimal(output_tokens) * row.output_per_mtok
        + Decimal(cache_read_tokens) * cache_read
        + Decimal(cache_write_tokens) * cache_write
    ) / _MILLION
    return CallCost(cost_usd=total, price_source=row.source)


def prices_stale(today: date) -> bool:
    """True when :data:`PRICES_READ_ON` is more than :data:`STALE_AFTER_DAYS` before ``today``."""
    return (today - PRICES_READ_ON).days > STALE_AFTER_DAYS


__all__ = [
    "PRICES",
    "PRICES_READ_ON",
    "SELF_HOSTED_PROVIDERS",
    "STALE_AFTER_DAYS",
    "SUBSCRIPTION_PROVIDERS",
    "CallCost",
    "PriceRow",
    "price_call",
    "price_row",
    "prices_stale",
]
