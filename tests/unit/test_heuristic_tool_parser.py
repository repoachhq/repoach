"""SP-CC-EMUL-HARDEN — pin every HeuristicToolParser form, old and new.

The claude_code provider's tools appendix teaches the model to emit
``<tool_use>{"name", "args"}</tool_use>`` text blocks, but until this
slice the parser only understood the ``● <function=...>`` and
WebFetch/WebSearch heuristics — a protocol mismatch that left the tool
emulation dead on arrival (a compliant model's calls passed through as
prose, ``stop_reason: end_turn``). These tests pin the new tag form
(complete, streamed across arbitrary chunk boundaries, salvaged at
flush, loud on malformed payloads), the round-trip with the provider's
own history serialization, and the pre-existing forms so NIM/OR
streaming consumers keep working unchanged.
"""

from __future__ import annotations

import json

from repoach.llm_proxy.core.anthropic.tools import HeuristicToolParser
from repoach.llm_proxy.providers.claude_code.client import ClaudeCodeProvider


def _feed_all(parser: HeuristicToolParser, *chunks: str) -> tuple[str, list[dict]]:
    text_parts: list[str] = []
    tools: list[dict] = []
    for chunk in chunks:
        text, detected = parser.feed(chunk)
        text_parts.append(text)
        tools.extend(detected)
    return "".join(text_parts), tools


class TestToolUseTagForm:
    def test_complete_tag_extracted_with_surrounding_text_preserved(self) -> None:
        text, tools = _feed_all(
            HeuristicToolParser(),
            "Plan: read the file first.\n"
            '<tool_use>{"name": "read_file", "args": {"path": "src/x.py"}}</tool_use>\n'
            "Then I will summarise.",
        )
        assert "Plan: read the file first." in text
        assert "Then I will summarise." in text
        assert "<tool_use>" not in text
        assert len(tools) == 1
        assert tools[0]["name"] == "read_file"
        assert tools[0]["input"] == {"path": "src/x.py"}
        assert tools[0]["type"] == "tool_use"
        assert tools[0]["id"].startswith("toolu_heuristic_")

    def test_input_key_accepted_as_args_alias(self) -> None:
        _, tools = _feed_all(
            HeuristicToolParser(),
            '<tool_use>{"name": "grep_repo", "input": {"pattern": "Planner"}}</tool_use>',
        )
        assert tools[0]["input"] == {"pattern": "Planner"}

    def test_missing_args_defaults_to_empty_object(self) -> None:
        _, tools = _feed_all(
            HeuristicToolParser(),
            '<tool_use>{"name": "git_status"}</tool_use>',
        )
        assert tools[0]["input"] == {}

    def test_nested_brace_args_survive(self) -> None:
        payload = {"name": "apply_fix", "args": {"meta": {"depth": {"level": 3}}}}
        _, tools = _feed_all(
            HeuristicToolParser(),
            f"<tool_use>{json.dumps(payload)}</tool_use>",
        )
        assert tools[0]["input"] == {"meta": {"depth": {"level": 3}}}

    def test_multiple_tags_in_one_feed(self) -> None:
        text, tools = _feed_all(
            HeuristicToolParser(),
            'a <tool_use>{"name": "t1", "args": {}}</tool_use> b '
            '<tool_use>{"name": "t2", "args": {"k": "v"}}</tool_use> c',
        )
        assert [t["name"] for t in tools] == ["t1", "t2"]
        assert "a " in text
        assert " b " in text
        assert " c" in text

    def test_malformed_json_passes_through_as_text_no_tool(self) -> None:
        raw = "<tool_use>{not valid json}</tool_use> trailing"
        text, tools = _feed_all(HeuristicToolParser(), raw)
        assert tools == []
        assert "<tool_use>{not valid json}</tool_use>" in text

    def test_payload_without_name_is_rejected_loudly(self) -> None:
        raw = '<tool_use>{"args": {"path": "x"}}</tool_use>'
        text, tools = _feed_all(HeuristicToolParser(), raw)
        assert tools == []
        assert raw in text

    def test_non_object_args_rejected(self) -> None:
        raw = '<tool_use>{"name": "t", "args": [1, 2]}</tool_use>'
        text, tools = _feed_all(HeuristicToolParser(), raw)
        assert tools == []
        assert raw in text


class TestToolUseTagStreaming:
    def test_tag_split_across_three_feeds(self) -> None:
        text, tools = _feed_all(
            HeuristicToolParser(),
            "Before <tool_us",
            'e>{"name": "grep_repo", "args": {"pattern": "Planner"}}</tool',
            "_use> after",
        )
        assert len(tools) == 1
        assert tools[0]["name"] == "grep_repo"
        assert "Before " in text
        assert " after" in text
        assert "<tool_us" not in text

    def test_partial_open_tag_prefix_not_leaked_as_text(self) -> None:
        parser = HeuristicToolParser()
        text1, tools1 = parser.feed("thinking... <tool_")
        assert tools1 == []
        assert "<tool_" not in text1
        text2, tools2 = parser.feed('use>{"name": "t", "args": {}}</tool_use>')
        assert len(tools2) == 1
        assert tools2[0]["name"] == "t"
        assert "<tool_" not in text2

    def test_payload_split_mid_json(self) -> None:
        text, tools = _feed_all(
            HeuristicToolParser(),
            '<tool_use>{"name": "read_file", "ar',
            'gs": {"path": "a/b.py"}}</tool_use>',
        )
        assert len(tools) == 1
        assert tools[0]["input"] == {"path": "a/b.py"}
        assert text == ""


class TestToolUseTagFlush:
    def test_unclosed_tag_with_complete_json_salvaged_at_flush(self) -> None:
        parser = HeuristicToolParser()
        _text, tools = parser.feed('<tool_use>{"name": "list_dir", "args": {"path": "src"}}')
        assert tools == []
        flushed = parser.flush()
        assert len(flushed) == 1
        assert flushed[0]["name"] == "list_dir"

    def test_unclosed_tag_with_garbage_payload_flushes_nothing(self) -> None:
        parser = HeuristicToolParser()
        parser.feed("<tool_use>this never becomes json")
        assert parser.flush() == []

    def test_flush_idempotent_after_clean_feed(self) -> None:
        parser = HeuristicToolParser()
        parser.feed('<tool_use>{"name": "t", "args": {}}</tool_use>')
        assert parser.flush() == []


class TestHistoryRoundTrip:
    """The provider re-serializes tool_use/tool_result turns into the
    flattened prompt; a serialized turn fed back to the parser must
    yield the identical call (the protocol must close on itself)."""

    def test_serialized_tool_use_reparses_identically(self) -> None:
        message = {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Reading now."},
                {
                    "type": "tool_use",
                    "id": "toolu_x",
                    "name": "read_file",
                    "input": {"path": "src/y.py"},
                },
            ],
        }
        serialized = ClaudeCodeProvider._content_text(message)
        _, tools = HeuristicToolParser().feed(serialized)
        assert len(tools) == 1
        assert tools[0]["name"] == "read_file"
        assert tools[0]["input"] == {"path": "src/y.py"}

    def test_serialized_tool_result_stays_text(self) -> None:
        message = {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_x",
                    "content": [{"type": "text", "text": "line1\nline2"}],
                }
            ],
        }
        serialized = ClaudeCodeProvider._content_text(message)
        text, tools = HeuristicToolParser().feed(serialized)
        assert tools == []
        assert "line1" in text


class TestPreexistingFormsStillWork:
    def test_function_marker_form(self) -> None:
        parser = HeuristicToolParser()
        _text, tools = parser.feed("● <function=send_msg><parameter=text>hello</parameter>\ndone\n")
        assert len(tools) == 1
        assert tools[0]["name"] == "send_msg"
        assert tools[0]["input"] == {"text": "hello"}

    def test_web_tool_json_form(self) -> None:
        _, tools = _feed_all(
            HeuristicToolParser(),
            'Use WebFetch {"url": "https://example.com"} to read the page.',
        )
        assert len(tools) == 1
        assert tools[0]["name"] == "WebFetch"

    def test_control_tokens_stripped(self) -> None:
        text, tools = _feed_all(
            HeuristicToolParser(),
            "hello <|im_end|>world",
        )
        assert tools == []
        assert text == "hello world"

    def test_plain_text_passthrough(self) -> None:
        text, tools = _feed_all(HeuristicToolParser(), "just prose, nothing else")
        assert tools == []
        assert text == "just prose, nothing else"

    def test_tag_and_function_marker_coexist(self) -> None:
        _text, tools = _feed_all(
            HeuristicToolParser(),
            '<tool_use>{"name": "t1", "args": {}}</tool_use>\n'
            "● <function=t2><parameter=k>v</parameter>\nrest\n",
        )
        assert [t["name"] for t in tools] == ["t1", "t2"]


class TestLiveCapturedShape:
    """Regression pin on the exact response shape a real ``claude`` CLI
    produced through the provider on 2026-06-07 (live probe, haiku via
    the Max subscription): a one-line preamble, a blank line, then the
    protocol tag. Synthetic fixtures lie; this one did not."""

    _LIVE_RESULT_TEXT = (
        "Let me use the correct tool call format for this environment:\n\n"
        '<tool_use>{"name": "read_file", '
        '"args": {"path": "src/repoach/llm/__init__.py"}}</tool_use>'
    )

    def test_live_captured_response_parses_to_single_tool_call(self) -> None:
        parser = HeuristicToolParser()
        text, tools = parser.feed(self._LIVE_RESULT_TEXT)
        assert len(tools) == 1
        assert tools[0]["name"] == "read_file"
        assert tools[0]["input"] == {"path": "src/repoach/llm/__init__.py"}
        assert "<tool_use>" not in text
        assert parser.flush() == []
