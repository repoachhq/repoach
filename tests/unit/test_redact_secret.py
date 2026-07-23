"""Unit tests for the shared redact-then-truncate helper (SP-REDACT-UNIFY).

``redact_secret`` is the single owner of the redact-before-truncate contract
that used to be copy-pasted (and truncate-first, leak-prone) across
``chain_health.py``, ``model_catalog.py`` and ``cell_probe.py``.
"""

from __future__ import annotations

from repoach.llm_proxy.providers.model_catalog import redact_secret


def _slices(text: str, size: int) -> list[str]:
    return [text[index : index + size] for index in range(len(text) - size + 1)]


def test_redacts_before_truncating() -> None:
    full_key = "sk-" + ("x" * 40) + "-secret"
    text = ("prefix-" * 15) + full_key + ("suffix-" * 15)
    key_start = text.index(full_key)
    key_end = key_start + len(full_key)
    assert key_start < 120 < key_end

    result = redact_secret(text, full_key)

    assert full_key not in result
    for chunk in _slices(full_key, 8):
        assert chunk not in result
    assert len(result) <= 120


def test_empty_secret_is_truncate_only_noop() -> None:
    text = "y" * 200
    assert redact_secret(text, "") == text[:120]


def test_multiple_occurrences_all_masked() -> None:
    secret = "topsecret-value"
    text = f"{secret}-middle-{secret}-end-{secret}"

    result = redact_secret(text, secret)

    assert secret not in result
    assert result.count("***") == 3


def test_limit_is_configurable() -> None:
    secret = "k"
    text = "a" * 10 + secret + "b" * 10

    result = redact_secret(text, secret, limit=5)

    assert result == "aaaaa"
