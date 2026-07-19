"""Tests for the reasoning-token split surfaced by the Anthropic SSE builder.

Covers ``SSEBuilder.estimate_reasoning_tokens`` (extracted from the shared
reasoning/text estimation logic in ``estimate_output_tokens``) and the
``reasoning_tokens`` sibling field emitted by ``message_delta``, verifying it
never leaks into the Anthropic-shaped ``usage`` dict.
"""

import json

from repoach.llm_proxy.core.anthropic.sse import ENCODER, SSEBuilder


def _expected_token_estimate(text: str) -> int:
    if ENCODER:
        return len(ENCODER.encode(text))
    return len(text) // 4


def test_estimate_reasoning_tokens_matches_accumulated_reasoning_only() -> None:
    builder = SSEBuilder(message_id="msg_1", model="test-model")
    builder.start_thinking_block()
    builder.emit_thinking_delta("Let me think about this problem ")
    builder.emit_thinking_delta("carefully before answering.")
    builder.stop_thinking_block()
    builder.start_text_block()
    builder.emit_text_delta("Here is the final answer.")
    builder.stop_text_block()

    expected = _expected_token_estimate(builder.accumulated_reasoning)

    assert builder.estimate_reasoning_tokens() == expected
    assert builder.estimate_reasoning_tokens() != _expected_token_estimate(builder.accumulated_text)


def test_estimate_reasoning_tokens_zero_when_no_thinking_text() -> None:
    builder = SSEBuilder(message_id="msg_2", model="test-model")
    builder.start_text_block()
    builder.emit_text_delta("No thinking happened for this response.")
    builder.stop_text_block()

    assert builder.estimate_reasoning_tokens() == 0


def test_message_delta_emits_reasoning_tokens_as_sibling_not_in_usage() -> None:
    builder = SSEBuilder(message_id="msg_3", model="test-model", input_tokens=10)

    event = builder.message_delta("end_turn", output_tokens=100, reasoning_tokens=42)

    data_line = next(line for line in event.splitlines() if line.startswith("data: "))
    payload = json.loads(data_line[len("data: ") :])

    assert payload["reasoning_tokens"] == 42
    assert "reasoning_tokens" not in payload["usage"]
    assert payload["usage"] == {"input_tokens": 10, "output_tokens": 100}
