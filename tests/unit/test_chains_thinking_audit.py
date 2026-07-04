"""Unit tests for the chain-head thinking audit (SP-CHAINS-THINKING-CLASS).

Covers reasoner-head reporting, non_thinking-head silence, unknown-model
reporting, empty-chain no-findings, and malformed-chain reporting.
"""

from __future__ import annotations

from ferova.llm_proxy.providers.thinking_audit import audit_chain_thinking


def test_reasoner_head_is_reported() -> None:
    """AC2: a chain led by a reasoner model yields a finding naming chain,
    model, and class."""
    chains = {
        "minimax_chain": ["minimaxai/minimax-m3", "claude_code/sonnet"],
    }
    findings = audit_chain_thinking(chains)
    assert len(findings) == 1
    assert "minimax_chain" in findings[0]
    assert "minimaxai/minimax-m3" in findings[0]
    assert "reasoner" in findings[0]


def test_non_thinking_head_is_clean() -> None:
    """AC3: a chain led by a non_thinking model yields no finding."""
    chains = {
        "mistral_chain": [
            "nvidia_nim/mistralai/mistral-medium-3.5",
            "claude_code/sonnet",
        ],
    }
    findings = audit_chain_thinking(chains)
    assert findings == []


def test_unknown_model_is_reported_not_guessed() -> None:
    """AC4: an unlisted model classifies as unknown and is reported."""
    chains = {
        "mystery_chain": ["nvidia_nim/some/unlisted-model"],
    }
    findings = audit_chain_thinking(chains)
    assert len(findings) == 1
    assert "mystery_chain" in findings[0]
    assert "unlisted-model" in findings[0]
    assert "unknown" in findings[0]


def test_empty_chain_list_no_findings() -> None:
    """An empty chains mapping produces no findings."""
    findings = audit_chain_thinking({})
    assert findings == []


def test_malformed_chain_reported() -> None:
    """A chain with an empty model list is reported as malformed."""
    chains = {
        "ok_chain": ["nvidia_nim/mistralai/mistral-medium-3.5"],
        "bad_chain": [],
    }
    findings = audit_chain_thinking(chains)
    assert len(findings) == 1
    assert "bad_chain" in findings[0]
    assert "malformed" in findings[0]
