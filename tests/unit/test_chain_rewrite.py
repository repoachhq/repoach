"""Tests for SP-CHAINPILOT-CHAIN-REWRITE (Phase 3d-1a).

Covers the mechanical rewriter: evict / drop-provider / demote / promote /
insert, the safety invariants (backstop immovable, chain never empty, one-step
shifts, insert above the backstop), and that untouched slots + all comments are
preserved verbatim.
"""

from __future__ import annotations

from repoach.review.chain_rewrite import (
    ChainEdit,
    EditOp,
    RewriteResult,
    rewrite_chains,
)

_FIXTURE = """# Chain config header comment.
FEROVA_SOMETHING=keep-me

# OPUS comment line.
MODEL_OPUS=nvidia_nim/mistralai/mistral-medium-3.5-128b,nvidia_nim/deepseek-ai/deepseek-v4-pro,claude_code/opus

# SONNET comment line.
MODEL_SONNET=nvidia_nim/mistralai/mistral-medium-3.5-128b,open_router/mistralai/mistral-small-2603,claude_code/sonnet

# HAIKU comment line.
MODEL_HAIKU=nvidia_nim/mistralai/mistral-small-4-119b-2603,claude_code/haiku

# Trailing comment.
"""


def _slot(content: str, key: str) -> str:
    for line in content.split("\n"):
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1]
    raise AssertionError(f"{key} not found")


def _apply(edits: list[ChainEdit]) -> RewriteResult:
    return rewrite_chains(_FIXTURE, edits)


def test_no_edits_is_byte_identical() -> None:
    result = _apply([])
    assert result.new_content == _FIXTURE
    assert result.applied == ()
    assert result.skipped == ()


def test_evict_removes_model_from_every_slot() -> None:
    content = (
        "MODEL_OPUS=nvidia_nim/x/dup,claude_code/opus\n"
        "MODEL_SONNET=nvidia_nim/x/dup,claude_code/sonnet\n"
    )
    result = rewrite_chains(content, [ChainEdit(op=EditOp.EVICT_MODEL, model="x/dup")])

    assert "x/dup" not in _slot(result.new_content, "MODEL_OPUS")
    assert "x/dup" not in _slot(result.new_content, "MODEL_SONNET")
    assert len(result.applied) == 1
    assert result.skipped == ()


def test_evict_preserves_comments_and_other_keys() -> None:
    result = _apply([ChainEdit(op=EditOp.EVICT_MODEL, model="deepseek-ai/deepseek-v4-pro")])

    for line in (
        "# Chain config header comment.",
        "FEROVA_SOMETHING=keep-me",
        "# OPUS comment line.",
        "# Trailing comment.",
    ):
        assert line in result.new_content.split("\n")
    assert _slot(result.new_content, "MODEL_HAIKU") == _slot(_FIXTURE, "MODEL_HAIKU")


def test_evict_refuses_to_empty_a_chain() -> None:
    result = rewrite_chains(
        "MODEL_HAIKU=nvidia_nim/only/model\n",
        [ChainEdit(op=EditOp.EVICT_MODEL, model="only/model")],
    )
    assert result.new_content == "MODEL_HAIKU=nvidia_nim/only/model\n"
    assert len(result.skipped) == 1
    assert "empty" in result.skipped[0].reason


def test_evict_never_touches_the_backstop() -> None:
    result = _apply([ChainEdit(op=EditOp.EVICT_MODEL, model="opus")])
    assert result.new_content == _FIXTURE
    assert len(result.skipped) == 1


def test_drop_provider_removes_one_ref_only() -> None:
    result = _apply(
        [
            ChainEdit(
                op=EditOp.DROP_PROVIDER,
                model="mistralai/mistral-small-2603",
                provider="open_router",
            )
        ]
    )
    sonnet = _slot(result.new_content, "MODEL_SONNET")
    assert "open_router/mistralai/mistral-small-2603" not in sonnet
    assert "nvidia_nim/mistralai/mistral-medium-3.5-128b" in sonnet
    assert "claude_code/sonnet" in sonnet


def test_demote_shifts_one_step_down() -> None:
    content = "MODEL_OPUS=nvidia_nim/a/first,nvidia_nim/a/second,claude_code/opus\n"
    result = rewrite_chains(content, [ChainEdit(op=EditOp.DEMOTE, model="a/first", tier="opus")])
    opus = _slot(result.new_content, "MODEL_OPUS").split(",")
    assert opus[0] == "nvidia_nim/a/second"
    assert opus[1] == "nvidia_nim/a/first"


def test_promote_shifts_one_step_up() -> None:
    content = "MODEL_OPUS=nvidia_nim/a/first,nvidia_nim/a/second,claude_code/opus\n"
    result = rewrite_chains(content, [ChainEdit(op=EditOp.PROMOTE, model="a/second", tier="opus")])
    opus = _slot(result.new_content, "MODEL_OPUS").split(",")
    assert opus[0] == "nvidia_nim/a/second"
    assert opus[1] == "nvidia_nim/a/first"


def test_demote_skips_when_next_is_backstop() -> None:
    result = _apply(
        [ChainEdit(op=EditOp.DEMOTE, model="mistralai/mistral-small-4-119b-2603", tier="haiku")]
    )
    assert result.new_content == _FIXTURE
    assert len(result.skipped) == 1
    assert "backstop" in result.skipped[0].reason


def test_promote_skips_at_head() -> None:
    result = _apply(
        [ChainEdit(op=EditOp.PROMOTE, model="mistralai/mistral-medium-3.5-128b", tier="opus")]
    )
    assert result.new_content == _FIXTURE
    assert "head" in result.skipped[0].reason


def test_insert_adds_above_backstop() -> None:
    result = _apply(
        [
            ChainEdit(
                op=EditOp.INSERT,
                model="z-ai/glm-5.2",
                provider="open_router",
                tier="opus",
                position=1,
            )
        ]
    )
    opus = _slot(result.new_content, "MODEL_OPUS").split(",")
    assert opus[1] == "open_router/z-ai/glm-5.2"
    assert opus[-1] == "claude_code/opus"


def test_insert_clamps_position_above_backstop() -> None:
    result = _apply(
        [
            ChainEdit(
                op=EditOp.INSERT,
                model="z-ai/glm-5.2",
                provider="open_router",
                tier="haiku",
                position=99,
            )
        ]
    )
    haiku = _slot(result.new_content, "MODEL_HAIKU").split(",")
    assert haiku[-1] == "claude_code/haiku"
    assert haiku[-2] == "open_router/z-ai/glm-5.2"


def test_insert_skips_duplicate() -> None:
    result = _apply(
        [
            ChainEdit(
                op=EditOp.INSERT,
                model="deepseek-ai/deepseek-v4-pro",
                provider="nvidia_nim",
                tier="opus",
            )
        ]
    )
    assert result.new_content == _FIXTURE
    assert "already present" in result.skipped[0].reason


def test_insert_skips_unknown_provider() -> None:
    result = _apply(
        [ChainEdit(op=EditOp.INSERT, model="some/model", provider="not_a_provider", tier="opus")]
    )
    assert result.new_content == _FIXTURE
    assert len(result.skipped) == 1


def test_insert_skips_unknown_tier() -> None:
    result = _apply(
        [ChainEdit(op=EditOp.INSERT, model="some/model", provider="nvidia_nim", tier="ultra")]
    )
    assert result.new_content == _FIXTURE
    assert "unknown tier" in result.skipped[0].reason


def test_tier_scoped_evict_touches_only_that_slot() -> None:
    content = (
        "MODEL_OPUS=nvidia_nim/x/dup,nvidia_nim/o/keep,claude_code/opus\n"
        "MODEL_SONNET=nvidia_nim/x/dup,claude_code/sonnet\n"
    )
    result = rewrite_chains(content, [ChainEdit(op=EditOp.EVICT_MODEL, model="x/dup", tier="opus")])
    assert "x/dup" not in _slot(result.new_content, "MODEL_OPUS")
    assert "x/dup" in _slot(result.new_content, "MODEL_SONNET")


def test_absent_model_is_skipped() -> None:
    result = _apply([ChainEdit(op=EditOp.EVICT_MODEL, model="ghost/model")])
    assert result.new_content == _FIXTURE
    assert len(result.skipped) == 1


def test_insert_rejects_claude_code_backstop() -> None:
    result = _apply(
        [ChainEdit(op=EditOp.INSERT, model="haiku", provider="claude_code", tier="opus")]
    )
    assert result.new_content == _FIXTURE
    assert len(result.applied) == 0
    assert "backstop" in result.skipped[0].reason


def test_promote_refuses_to_cross_a_backstop() -> None:
    content = "MODEL_OPUS=nvidia_nim/a/b,claude_code/opus,nvidia_nim/c/d\n"
    result = rewrite_chains(content, [ChainEdit(op=EditOp.PROMOTE, model="c/d", tier="opus")])
    assert result.new_content == content
    assert len(result.applied) == 0
    assert "backstop" in result.skipped[0].reason


def test_demote_refuses_to_move_the_backstop() -> None:
    result = _apply([ChainEdit(op=EditOp.DEMOTE, model="opus", tier="opus")])
    assert result.new_content == _FIXTURE
    assert "backstop" in result.skipped[0].reason


def test_drop_provider_refuses_the_backstop() -> None:
    result = _apply(
        [ChainEdit(op=EditOp.DROP_PROVIDER, model="opus", provider="claude_code", tier="opus")]
    )
    assert result.new_content == _FIXTURE
    assert "backstop" in result.skipped[0].reason


def test_partial_multitier_edit_records_both_applied_and_refusal() -> None:
    content = (
        "MODEL_OPUS=nvidia_nim/head/m,nvidia_nim/x/dup,claude_code/opus\n"
        "MODEL_SONNET=nvidia_nim/x/dup,nvidia_nim/other/m,claude_code/sonnet\n"
    )
    result = rewrite_chains(content, [ChainEdit(op=EditOp.PROMOTE, model="x/dup")])

    opus = _slot(result.new_content, "MODEL_OPUS").split(",")
    assert opus[0] == "nvidia_nim/x/dup"
    assert len(result.applied) == 1
    assert len(result.skipped) == 1
    assert "sonnet" in result.skipped[0].reason
    assert "head" in result.skipped[0].reason


def test_non_insert_unknown_tier_reason() -> None:
    result = _apply(
        [ChainEdit(op=EditOp.EVICT_MODEL, model="deepseek-ai/deepseek-v4-pro", tier="ultra")]
    )
    assert result.new_content == _FIXTURE
    assert "unknown tier" in result.skipped[0].reason


def test_canceling_edits_net_no_change() -> None:
    content = "MODEL_OPUS=nvidia_nim/a/first,nvidia_nim/a/second,claude_code/opus\n"
    result = rewrite_chains(
        content,
        [
            ChainEdit(op=EditOp.DEMOTE, model="a/first", tier="opus"),
            ChainEdit(op=EditOp.PROMOTE, model="a/first", tier="opus"),
        ],
    )
    assert result.new_content == content
    assert len(result.applied) == 2
