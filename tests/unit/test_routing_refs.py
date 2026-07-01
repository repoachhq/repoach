"""Tests for the ModelRef value object (SP-ROUTING-DOMAIN-TYPES / SP-PROVIDER-CATALOG).

Pins :class:`ModelRef` to the live ``chains.env`` spelling and the legacy
``ResolvedModel`` boundary. Provider-id validation now derives from the
provider catalog (via :data:`SUPPORTED_PROVIDER_IDS`), so an unregistered
provider is rejected without a hand-maintained enum.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ferova.llm_proxy.api.model_router import ResolvedModel
from ferova.llm_proxy.routing.refs import ModelRef

_CHAINS_ENV = Path(__file__).resolve().parents[2] / "chains.env"


def _chain_refs() -> list[str]:
    """Every comma-separated ``provider/model`` ref declared in chains.env."""
    refs: list[str] = []
    for line in _CHAINS_ENV.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("MODEL_") or "=" not in stripped:
            continue
        _, _, value = stripped.partition("=")
        refs.extend(part.strip() for part in value.split(",") if part.strip())
    return refs


def test_chain_refs_round_trip() -> None:
    refs = _chain_refs()
    assert refs, "expected at least one MODEL_* chain entry in chains.env"
    for ref in refs:
        assert str(ModelRef.parse(ref)) == ref


@pytest.mark.parametrize(
    ("ref", "provider", "model"),
    [
        (
            "nvidia_nim/mistralai/mistral-medium-3.5-128b",
            "nvidia_nim",
            "mistralai/mistral-medium-3.5-128b",
        ),
        ("open_router/mistralai/mistral-small-2603", "open_router", "mistralai/mistral-small-2603"),
        ("claude_code/opus", "claude_code", "opus"),
    ],
)
def test_parse_splits_on_first_slash(ref: str, provider: str, model: str) -> None:
    parsed = ModelRef.parse(ref)
    assert parsed.provider_id == provider
    assert parsed.model == model


@pytest.mark.parametrize(
    "bad_ref",
    [
        "",
        "claude_code",
        "no-slash-here",
        "unknown_provider/whatever",
        "not_a_provider/x",
        "nvidia_nim/",
    ],
)
def test_parse_rejects_invalid(bad_ref: str) -> None:
    with pytest.raises(ValueError):
        ModelRef.parse(bad_ref)


def test_model_ref_is_frozen() -> None:
    ref = ModelRef.parse("claude_code/opus")
    with pytest.raises(ValidationError):
        ref.model = "sonnet"


def test_to_resolved_matches_resolved_model() -> None:
    ref = "nvidia_nim/mistralai/mistral-medium-3.5-128b"
    name = "claude-opus-4-7"
    expected = ResolvedModel(
        original_model=name,
        provider_id="nvidia_nim",
        provider_model="mistralai/mistral-medium-3.5-128b",
        provider_model_ref=ref,
    )
    actual = ModelRef.parse(ref).to_resolved(name)
    assert actual == expected
