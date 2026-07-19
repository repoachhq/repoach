"""Artificial Analysis free-API capability ingest (SP-MFC-AA-INGEST).

Slice 1 of the model-first chains arc (``docs/model_first_chains_architecture.md``).
Pulls live per-model capability from the Artificial Analysis free endpoint
(``GET /api/v2/language/models/free``) and reduces it to one capability number
per model, so the model-first chain builder ranks models off the real market
instead of the hand-curated ``benchmark_prior.json`` snapshot.

A model is benchmarked by Artificial Analysis as several rows — one per
reasoning/effort variant, whose intelligence indices can differ by double
digits. Selection is variant-agnostic by design: the variants of a model
collapse to a single capability equal to the MAX intelligence index across
them, keyed on the normalized model name (the ``slug`` does not unify a
reasoning row with its non-reasoning sibling). The same collapse later anchors
the Claude reference models, so the eligibility comparison stays like-for-like.

The resource gates routing, so a bad scrape must fail loud rather than degrade:
a missing key, a failed page request, or an out-of-range value raises
:class:`AaIngestError` and yields no partial ranking. Network I/O is injected
(``fetch_page``) so the unit suite runs offline against captured payloads.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

import httpx
from pydantic import BaseModel, ConfigDict

from repoach.core.logging import get_logger
from repoach.llm_proxy.config.settings import Settings

_log = get_logger(__name__)

AA_DEFAULT_BASE = "https://artificialanalysis.ai"
AA_FREE_PATH = "/api/v2/language/models/free"
INTELLIGENCE_INDEX_MIN = 0.0
INTELLIGENCE_INDEX_MAX = 100.0
MAX_PAGES = 50

_VARIANT_SUFFIX = re.compile(r"\s*\(.*$")
_NON_ALNUM = re.compile(r"[^a-z0-9]")

__all__ = [
    "AA_DEFAULT_BASE",
    "AA_FREE_PATH",
    "AaIngestError",
    "AaRanking",
    "ModelCapability",
    "ModelVariant",
    "fetch_aa_ranking",
    "normalize_model_name",
    "parse_aa_models",
]


class AaIngestError(Exception):
    """Raised when the ingest cannot produce a trustworthy ranking.

    Covers a missing API key, a failed or malformed page response, and any
    value that violates the sanity bounds (intelligence index outside
    ``[0, 100]`` or a negative price/performance figure).
    """


class ModelVariant(BaseModel):
    """One Artificial Analysis row — a single model variant's measured values.

    Attributes:
        name: The model display name verbatim, including its variant suffix.
        slug: The Artificial Analysis slug for the row.
        creator: The model creator name.
        intelligence_index: The headline intelligence index (in ``[0, 100]``).
        coding_index: The coding index, or ``None`` when not measured.
        agentic_index: The agentic index, or ``None`` when not measured.
        price_1m_input: USD per 1M input tokens, or ``None``.
        price_1m_output: USD per 1M output tokens, or ``None``.
        median_output_tps: Median output tokens/second, or ``None``.
        median_ttft_s: Median time-to-first-token in seconds, or ``None``.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    slug: str
    creator: str
    intelligence_index: float
    coding_index: float | None
    agentic_index: float | None
    price_1m_input: float | None
    price_1m_output: float | None
    median_output_tps: float | None
    median_ttft_s: float | None


class ModelCapability(BaseModel):
    """A model collapsed across its variants into one capability record.

    Attributes:
        name: The normalized model name used as the collapse key.
        capability: The MAX intelligence index across the model's variants.
        coding: The MAX coding index across variants, or ``None`` if every
            variant left it unmeasured.
        cheapest_input: The lowest measured input price across variants, or
            ``None`` if none priced.
        fastest_tps: The highest measured output tokens/second across variants,
            or ``None`` if none measured.
        variants: The contributing variants, in first-seen order.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    capability: float
    coding: float | None
    cheapest_input: float | None
    fastest_tps: float | None
    variants: tuple[ModelVariant, ...]


class AaRanking(BaseModel):
    """A parsed, collapsed Artificial Analysis ranking.

    Attributes:
        index_version: The ``intelligence_index_version`` from the payload, or
            ``None`` when absent — recorded because the index re-baselines.
        models: One :class:`ModelCapability` per normalized name, sorted by
            descending capability.
    """

    model_config = ConfigDict(frozen=True)

    index_version: str | None
    models: tuple[ModelCapability, ...]


def normalize_model_name(name: str) -> str:
    """Reduce a display name to its variant-agnostic collapse key.

    Strips the trailing variant parenthetical (``" (Reasoning, Max Effort)"``)
    then lowercases and removes every non-alphanumeric character, so a model's
    reasoning and non-reasoning rows map to one key.

    Args:
        name: The model display name as Artificial Analysis lists it.

    Returns:
        The normalized key (may be empty for a degenerate name).
    """
    base = _VARIANT_SUFFIX.sub("", name)
    return _NON_ALNUM.sub("", base.lower())


def _require_bounded_index(value: float, name: str) -> float:
    """Return ``value`` if it sits within the intelligence-index bounds.

    Raises:
        AaIngestError: When ``value`` is outside ``[0, 100]``.
    """
    if not INTELLIGENCE_INDEX_MIN <= value <= INTELLIGENCE_INDEX_MAX:
        raise AaIngestError(f"intelligence index {value} out of bounds for {name!r}")
    return value


def _require_non_negative(value: float | None, label: str, name: str) -> float | None:
    """Return ``value`` if it is absent or non-negative.

    Raises:
        AaIngestError: When ``value`` is present and negative.
    """
    if value is not None and value < 0:
        raise AaIngestError(f"negative {label} {value} for {name!r}")
    return value


def _optional_float(source: object, key: str) -> float | None:
    """Read ``key`` from a mapping as a float, treating absence/null as ``None``.

    Raises:
        AaIngestError: When the value is present but not a number.
    """
    if not isinstance(source, dict):
        return None
    raw = source.get(key)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise AaIngestError(f"non-numeric {key}: {raw!r}")
    return float(raw)


def _variant_from_row(row: object) -> ModelVariant | None:
    """Build a :class:`ModelVariant` from one ``data[]`` row, or skip it.

    Returns ``None`` for a row whose intelligence index is null (no usable
    capability). Validates every numeric against the sanity bounds.

    Raises:
        AaIngestError: When a required field is malformed or a value violates
            the sanity bounds.
    """
    if not isinstance(row, dict):
        raise AaIngestError(f"row is not an object: {type(row).__name__}")
    name = row.get("name")
    slug = row.get("slug")
    if not isinstance(name, str) or not name:
        raise AaIngestError("row missing a 'name'")
    if not isinstance(slug, str) or not slug:
        raise AaIngestError(f"row {name!r} missing a 'slug'")
    creator = row.get("model_creator")
    creator_name = creator.get("name") if isinstance(creator, dict) else None
    evaluations = row.get("evaluations")
    intelligence = _optional_float(evaluations, "artificial_analysis_intelligence_index")
    if intelligence is None:
        _log.info("aa_ingest_row_skipped_no_index", model=name)
        return None
    pricing = row.get("pricing")
    performance = row.get("performance")
    return ModelVariant(
        name=name,
        slug=slug,
        creator=creator_name if isinstance(creator_name, str) else "",
        intelligence_index=_require_bounded_index(intelligence, name),
        coding_index=_optional_float(evaluations, "artificial_analysis_coding_index"),
        agentic_index=_optional_float(evaluations, "artificial_analysis_agentic_index"),
        price_1m_input=_require_non_negative(
            _optional_float(pricing, "price_1m_input_tokens"), "input price", name
        ),
        price_1m_output=_require_non_negative(
            _optional_float(pricing, "price_1m_output_tokens"), "output price", name
        ),
        median_output_tps=_require_non_negative(
            _optional_float(performance, "median_output_tokens_per_second"), "tps", name
        ),
        median_ttft_s=_require_non_negative(
            _optional_float(performance, "median_time_to_first_token_seconds"), "ttft", name
        ),
    )


def _collapse(name: str, variants: Sequence[ModelVariant]) -> ModelCapability:
    """Collapse a model's variants into one capability record."""
    codings = [variant.coding_index for variant in variants if variant.coding_index is not None]
    inputs = [variant.price_1m_input for variant in variants if variant.price_1m_input is not None]
    tps = [
        variant.median_output_tps for variant in variants if variant.median_output_tps is not None
    ]
    return ModelCapability(
        name=name,
        capability=max(variant.intelligence_index for variant in variants),
        coding=max(codings) if codings else None,
        cheapest_input=min(inputs) if inputs else None,
        fastest_tps=max(tps) if tps else None,
        variants=tuple(variants),
    )


def parse_aa_models(pages: Sequence[dict]) -> AaRanking:
    """Validate captured page payloads and collapse them into a ranking.

    Pure (no I/O): the unit suite drives this directly with captured pages.

    Args:
        pages: The decoded page bodies, each ``{"data": [...], ...}``.

    Returns:
        The collapsed :class:`AaRanking`, models sorted by descending capability.

    Raises:
        AaIngestError: When any page is malformed or a value violates the
            sanity bounds.
    """
    grouped: dict[str, list[ModelVariant]] = {}
    index_version: str | None = None
    for page in pages:
        if not isinstance(page, dict):
            raise AaIngestError(f"page is not an object: {type(page).__name__}")
        if index_version is None:
            raw_version = page.get("intelligence_index_version")
            index_version = str(raw_version) if raw_version is not None else None
        data = page.get("data")
        if not isinstance(data, list):
            raise AaIngestError("page missing a 'data' list")
        for row in data:
            variant = _variant_from_row(row)
            if variant is None:
                continue
            grouped.setdefault(normalize_model_name(variant.name), []).append(variant)
    models = tuple(
        sorted(
            (_collapse(name, variants) for name, variants in grouped.items()),
            key=lambda capability: capability.capability,
            reverse=True,
        )
    )
    _log.info("aa_ingest_parsed", models=len(models), index_version=index_version)
    return AaRanking(index_version=index_version, models=models)


def _default_fetch_page(api_key: str, base_url: str, timeout_s: float) -> Callable[[int], dict]:
    """Build the live page fetcher: an ``x-api-key`` GET against the free endpoint."""

    def fetch(page: int) -> dict:
        url = f"{base_url.rstrip('/')}{AA_FREE_PATH}"
        try:
            resp = httpx.get(
                url,
                headers={"x-api-key": api_key},
                params={"page": page},
                timeout=timeout_s,
            )
        except httpx.HTTPError as exc:
            raise AaIngestError(f"page {page} request failed: {type(exc).__name__}") from exc
        if not 200 <= resp.status_code < 300:
            raise AaIngestError(f"page {page} http={resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise AaIngestError(f"page {page} json decode: {type(exc).__name__}") from exc
        if not isinstance(body, dict):
            raise AaIngestError(f"page {page} body is not an object")
        return body

    return fetch


def _total_pages(first_page: dict) -> int:
    """Resolve how many pages to fetch from the first page's pagination block."""
    pagination = first_page.get("pagination")
    if isinstance(pagination, dict):
        total = pagination.get("total_pages")
        if isinstance(total, int) and total > 0:
            return min(total, MAX_PAGES)
    return 1


def fetch_aa_ranking(
    settings: Settings,
    *,
    fetch_page: Callable[[int], dict] | None = None,
    base_url: str = AA_DEFAULT_BASE,
    timeout_s: float = 30.0,
) -> AaRanking:
    """Walk every page of the free endpoint and return the collapsed ranking.

    Args:
        settings: Supplies ``artificial_analysis_api_key``.
        fetch_page: An injected ``(page) -> body`` fetcher; defaults to the live
            ``x-api-key`` HTTP call. The unit suite injects a captured fetcher.
        base_url: The Artificial Analysis base URL (override for tests).
        timeout_s: Per-request timeout for the default fetcher.

    Returns:
        The collapsed :class:`AaRanking`.

    Raises:
        AaIngestError: When the key is missing, a page request fails, or a value
            violates the sanity bounds.
    """
    key = settings.artificial_analysis_api_key
    if not key:
        raise AaIngestError("artificial_analysis_api_key is not set")
    fetch = fetch_page or _default_fetch_page(key, base_url, timeout_s)
    first = fetch(1)
    if not isinstance(first, dict):
        raise AaIngestError("page 1 body is not an object")
    pages = [first]
    for page in range(2, _total_pages(first) + 1):
        pages.append(fetch(page))
    return parse_aa_models(pages)
