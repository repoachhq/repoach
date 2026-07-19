"""Heuristic parser for text-emitted tool calls."""

import json
import re
import uuid
from enum import Enum
from typing import Any

from loguru import logger

_CONTROL_TOKEN_RE = re.compile(r"<\|[^|>]{1,80}\|>")
_CONTROL_TOKEN_START = "<|"
_CONTROL_TOKEN_END = "|>"

_TOOL_USE_OPEN = "<tool_use>"
_TOOL_USE_CLOSE = "</tool_use>"
_TOOL_USE_TAG_RE = re.compile(r"<tool_use>\s*(\{.*?\})\s*</tool_use>", re.DOTALL)


class ParserState(Enum):
    TEXT = 1
    MATCHING_FUNCTION = 2
    PARSING_PARAMETERS = 3


class HeuristicToolParser:
    """
    Stateful parser for raw text tool calls.

    Some OpenAI-compatible models emit tool calls as text rather than structured
    chunks. This parser converts three text forms into Anthropic-style
    ``tool_use`` blocks:

    * ``<tool_use>{"name": ..., "args": {...}}</tool_use>`` tags — the
      protocol the claude_code provider's tools appendix teaches the
      model (SP-CC-EMUL-HARDEN; before this form was parsed, a model
      that followed the appendix to the letter saw its calls pass
      through as prose and the emulation was dead on arrival).
    * ``● <function=...><parameter=k>v</parameter>`` blocks.
    * ``WebFetch`` / ``WebSearch`` mentions followed by a JSON object.

    A ``<tool_use>`` tag whose JSON payload is malformed is left in the
    text output and logged — a loud failure the caller can see beats a
    silently swallowed call.
    """

    _FUNC_START_PATTERN = re.compile(r"●\s*<function=([^>]+)>")
    _PARAM_PATTERN = re.compile(r"<parameter=([^>]+)>(.*?)(?:</parameter>|$)", re.DOTALL)
    _WEB_TOOL_JSON_PATTERN = re.compile(
        r"(?is)\b(?:use\s+)?(?P<tool>WebFetch|WebSearch)\b.*?(?P<json>\{.*?\})"
    )

    def __init__(self):
        self._state = ParserState.TEXT
        self._buffer = ""
        self._current_tool_id = None
        self._current_function_name = None
        self._current_parameters = {}

    def _extract_web_tool_json_calls(self) -> tuple[str, list[dict[str, Any]]]:
        detected_tools: list[dict[str, Any]] = []

        for match in self._WEB_TOOL_JSON_PATTERN.finditer(self._buffer):
            try:
                tool_input = json.loads(match.group("json"))
            except json.JSONDecodeError as exc:
                logger.debug(
                    "anthropic_tools.web_tool_json_decode_failed",
                    error_type=type(exc).__name__,
                )
                continue
            if not isinstance(tool_input, dict):
                continue

            tool_name = match.group("tool")
            if tool_name == "WebFetch" and "url" not in tool_input:
                continue
            if tool_name == "WebSearch" and "query" not in tool_input:
                continue

            detected_tools.append(
                {
                    "type": "tool_use",
                    "id": f"toolu_heuristic_{uuid.uuid4().hex[:8]}",
                    "name": tool_name,
                    "input": tool_input,
                }
            )
            logger.debug(
                "Heuristic bypass: Detected JSON-style tool call '{}'",
                tool_name,
            )

        if not detected_tools:
            return self._buffer, []

        return "", detected_tools

    @staticmethod
    def _tool_from_tag_payload(raw_json: str) -> dict[str, Any] | None:
        """Build a tool_use dict from a tag's JSON payload, or ``None`` if invalid.

        The payload must be a JSON object with a non-empty string
        ``name``; arguments are read from ``args`` (the documented
        protocol key) with ``input`` accepted as an alias, and must be
        an object when present.
        """
        try:
            payload = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            logger.debug(
                "anthropic_tools.tool_use_tag_json_decode_failed: {}",
                str(exc)[:120],
            )
            return None
        if not isinstance(payload, dict):
            return None
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            return None
        args = payload.get("args", payload.get("input", {}))
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return None
        return {
            "type": "tool_use",
            "id": f"toolu_heuristic_{uuid.uuid4().hex[:8]}",
            "name": name.strip(),
            "input": args,
        }

    def _extract_tool_use_tag_calls(self) -> tuple[str, list[dict[str, Any]]]:
        """Extract complete ``<tool_use>{...}</tool_use>`` tags from the buffer.

        Returns the buffer text with successfully-parsed tags spliced
        out, plus the detected tool calls. Tags whose payload does not
        validate stay in the text untouched and are logged at WARNING —
        the downstream caller sees the raw protocol attempt instead of
        a silent no-op.
        """
        if _TOOL_USE_OPEN not in self._buffer:
            return self._buffer, []
        detected_tools: list[dict[str, Any]] = []
        out_parts: list[str] = []
        cursor = 0
        for match in _TOOL_USE_TAG_RE.finditer(self._buffer):
            tool = self._tool_from_tag_payload(match.group(1))
            if tool is None:
                logger.warning(
                    "anthropic_tools.tool_use_tag_invalid_payload: {}",
                    match.group(1)[:160],
                )
                continue
            out_parts.append(self._buffer[cursor : match.start()])
            cursor = match.end()
            detected_tools.append(tool)
            logger.debug(
                "Heuristic bypass: Detected <tool_use> tag call '{}'",
                tool["name"],
            )
        out_parts.append(self._buffer[cursor:])
        return "".join(out_parts), detected_tools

    def _detach_incomplete_tool_use_tail(self) -> str:
        """Detach a trailing partial ``<tool_use>`` region from the buffer.

        Covers both an opened-but-unclosed tag and a partial prefix of
        the opening tag itself at the very end of the buffer (streaming
        callers split chunks at arbitrary byte boundaries). The
        detached tail is returned so :meth:`feed` can re-append it
        after this pass, keeping it away from the text-flush path and
        from the other extraction heuristics until it completes.
        """
        open_idx = self._buffer.rfind(_TOOL_USE_OPEN)
        if open_idx != -1 and _TOOL_USE_CLOSE not in self._buffer[open_idx:]:
            tail = self._buffer[open_idx:]
            self._buffer = self._buffer[:open_idx]
            return tail
        angle_idx = self._buffer.rfind("<")
        if angle_idx != -1:
            candidate = self._buffer[angle_idx:]
            if len(candidate) < len(_TOOL_USE_OPEN) and _TOOL_USE_OPEN.startswith(candidate):
                self._buffer = self._buffer[:angle_idx]
                return candidate
        return ""

    def _strip_control_tokens(self, text: str) -> str:
        return _CONTROL_TOKEN_RE.sub("", text)

    def _split_incomplete_control_token_tail(self) -> str:
        start = self._buffer.rfind(_CONTROL_TOKEN_START)
        if start == -1:
            return ""
        end = self._buffer.find(_CONTROL_TOKEN_END, start)
        if end != -1:
            return ""

        prefix = self._buffer[:start]
        self._buffer = self._buffer[start:]
        return prefix

    def feed(self, text: str) -> tuple[str, list[dict[str, Any]]]:
        """Feed text and return safe text plus detected tool calls."""
        self._buffer += text
        self._buffer = self._strip_control_tokens(self._buffer)
        pending_tool_use_tail = self._detach_incomplete_tool_use_tail()
        self._buffer, tag_tools = self._extract_tool_use_tag_calls()
        self._buffer, detected_tools = self._extract_web_tool_json_calls()
        detected_tools = tag_tools + detected_tools
        filtered_output_parts: list[str] = []

        while True:
            if self._state == ParserState.TEXT:
                if "●" in self._buffer:
                    idx = self._buffer.find("●")
                    filtered_output_parts.append(self._buffer[:idx])
                    self._buffer = self._buffer[idx:]
                    self._state = ParserState.MATCHING_FUNCTION
                else:
                    safe_prefix = self._split_incomplete_control_token_tail()
                    if safe_prefix:
                        filtered_output_parts.append(safe_prefix)
                        break

                    filtered_output_parts.append(self._buffer)
                    self._buffer = ""
                    break

            if self._state == ParserState.MATCHING_FUNCTION:
                match = self._FUNC_START_PATTERN.search(self._buffer)
                if match:
                    self._current_function_name = match.group(1).strip()
                    self._current_tool_id = f"toolu_heuristic_{uuid.uuid4().hex[:8]}"
                    self._current_parameters = {}
                    self._buffer = self._buffer[match.end() :]
                    self._state = ParserState.PARSING_PARAMETERS
                    logger.debug(
                        "Heuristic bypass: Detected start of tool call '{}'",
                        self._current_function_name,
                    )
                elif len(self._buffer) > 100:
                    filtered_output_parts.append(self._buffer[0])
                    self._buffer = self._buffer[1:]
                    self._state = ParserState.TEXT
                else:
                    break

            if self._state == ParserState.PARSING_PARAMETERS:
                finished_tool_call = False

                while True:
                    param_match = self._PARAM_PATTERN.search(self._buffer)
                    if param_match and "</parameter>" in param_match.group(0):
                        pre_match_text = self._buffer[: param_match.start()]
                        if pre_match_text:
                            filtered_output_parts.append(pre_match_text)

                        key = param_match.group(1).strip()
                        val = param_match.group(2).strip()
                        self._current_parameters[key] = val
                        self._buffer = self._buffer[param_match.end() :]
                    else:
                        break

                if "●" in self._buffer:
                    idx = self._buffer.find("●")
                    if idx > 0:
                        filtered_output_parts.append(self._buffer[:idx])
                        self._buffer = self._buffer[idx:]
                    finished_tool_call = True
                elif len(self._buffer) > 0 and not self._buffer.strip().startswith("<"):
                    if "<parameter=" not in self._buffer:
                        filtered_output_parts.append(self._buffer)
                        self._buffer = ""
                        finished_tool_call = True

                if finished_tool_call:
                    detected_tools.append(
                        {
                            "type": "tool_use",
                            "id": self._current_tool_id,
                            "name": self._current_function_name,
                            "input": self._current_parameters,
                        }
                    )
                    logger.debug(
                        "Heuristic bypass: Emitting tool call '{}' with {} params",
                        self._current_function_name,
                        len(self._current_parameters),
                    )
                    self._state = ParserState.TEXT
                else:
                    break

        self._buffer += pending_tool_use_tail
        return "".join(filtered_output_parts), detected_tools

    def flush(self) -> list[dict[str, Any]]:
        """Flush any remaining tool call in the buffer.

        Salvages an opened-but-never-closed ``<tool_use>`` tag whose
        JSON payload is already complete — models occasionally stop
        right before the closing tag at end of turn.
        """
        self._buffer = self._strip_control_tokens(self._buffer)
        self._buffer, tag_tools = self._extract_tool_use_tag_calls()
        detected_tools = list(tag_tools)
        salvage = re.search(r"<tool_use>\s*(\{.*\})\s*$", self._buffer, re.DOTALL)
        if salvage is not None:
            tool = self._tool_from_tag_payload(salvage.group(1))
            if tool is not None:
                detected_tools.append(tool)
                self._buffer = self._buffer[: salvage.start()]
                logger.debug(
                    "Heuristic bypass: Salvaged unclosed <tool_use> tag call '{}'",
                    tool["name"],
                )
        if self._state == ParserState.PARSING_PARAMETERS:
            partial_matches = re.finditer(r"<parameter=([^>]+)>(.*)$", self._buffer, re.DOTALL)
            for match in partial_matches:
                key = match.group(1).strip()
                val = match.group(2).strip()
                self._current_parameters[key] = val

            detected_tools.append(
                {
                    "type": "tool_use",
                    "id": self._current_tool_id,
                    "name": self._current_function_name,
                    "input": self._current_parameters,
                }
            )
            self._state = ParserState.TEXT
            self._buffer = ""

        return detected_tools
