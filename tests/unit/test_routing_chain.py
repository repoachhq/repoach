"""Tests for the Chain value object (SP-ROUTING-TABLE).

Pins :class:`Chain` parsing to the live ``chains.env`` slot spelling and
the ``without`` filter to the legacy semantic-failover fallback.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repoach.llm_proxy.routing.chain import Chain
from repoach.llm_proxy.routing.refs import ModelRef

_CHAINS_ENV = Path(__file__).resolve().parents[2] / "chains.env"


def _slot_specs() -> list[str]:
    """Every ``MODEL_*=`` slot value declared in chains.env."""
    specs: list[str] = []
    for line in _CHAINS_ENV.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("MODEL_") or "=" not in stripped:
            continue
        _, _, value = stripped.partition("=")
        if value.strip():
            specs.append(value)
    return specs


def test_parse_round_trips_chains_env_slots() -> None:
    specs = _slot_specs()
    assert specs, "expected at least one MODEL_* slot in chains.env"
    for spec in specs:
        expected = [part.strip() for part in spec.split(",") if part.strip()]
        parsed = [str(ref) for ref in Chain.parse(spec).refs]
        assert parsed == expected


@pytest.mark.parametrize("spec", ["", "   ", ",", " , "])
def test_parse_rejects_empty(spec: str) -> None:
    with pytest.raises(ValueError):
        Chain.parse(spec)


def test_parse_dedups_preserving_order() -> None:
    chain = Chain.parse("nvidia_nim/a/b, claude_code/opus, nvidia_nim/a/b")
    assert [str(ref) for ref in chain.refs] == ["nvidia_nim/a/b", "claude_code/opus"]


def test_chain_rejects_empty_refs() -> None:
    with pytest.raises(ValueError):
        Chain(refs=())


def test_without_removes_skipped() -> None:
    chain = Chain.parse("nvidia_nim/a/b,claude_code/opus")
    filtered = chain.without(frozenset({ModelRef.parse("nvidia_nim/a/b")}))
    assert [str(ref) for ref in filtered.refs] == ["claude_code/opus"]


def test_without_all_skipped_falls_back_to_head() -> None:
    chain = Chain.parse("nvidia_nim/a/b,claude_code/opus")
    skip = frozenset({ModelRef.parse("nvidia_nim/a/b"), ModelRef.parse("claude_code/opus")})
    filtered = chain.without(skip)
    assert [str(ref) for ref in filtered.refs] == ["nvidia_nim/a/b"]
