"""Claude Code subprocess provider.

Routes Anthropic ``Messages`` requests through the local ``claude``
CLI in ``--print`` mode, which authenticates via OAuth and bills
against the user's Claude MAX subscription rather than API credits.

Tradeoffs vs the native Anthropic API:
- One subprocess per request (latency overhead ~0.5-2 s).
- No mid-stream cancellation; the CLI returns a single JSON object.
- Output is a final assistant text — emitted as a single SSE
  ``content_block`` to the caller. ``<tool_use>`` text blocks
  produced by the model are converted to native ``tool_use`` SSE
  blocks via :class:`HeuristicToolParser`, matching what the rest
  of the proxy expects.
- No token counts from the upstream model are echoed (the CLI
  reports its own usage which differs from API token semantics).
"""

from __future__ import annotations

import asyncio
import json
import shlex
import shutil
import tempfile
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from loguru import logger

from ferova.llm_proxy.core.anthropic import (
    HeuristicToolParser,
    SSEBuilder,
    append_request_id,
)
from ferova.llm_proxy.providers.base import BaseProvider, ProviderConfig
from ferova.llm_proxy.providers.exceptions import ProviderError
from ferova.llm_proxy.providers.rate_limit import GlobalRateLimiter


class ClaudeCodeProvider(BaseProvider):
    """Wraps ``claude -p`` to expose MAX-billed completions.

    ``claude --print`` does not accept the Anthropic ``tools`` field as
    API input, so the provider strips ``tools`` from the request and
    renders the tool contract as a text appendix on the system prompt,
    then parses the model's ``<tool_use>`` text back into native
    ``tool_use`` blocks. It is therefore a first-class member of the one
    chain — the last-resort backstop that serves tool requests via
    emulation when the native providers ahead of it are all down.
    """

    def __init__(
        self,
        config: ProviderConfig,
        *,
        cli_path: str = "claude",
        default_model: str = "sonnet",
        subprocess_timeout: float = 600.0,
    ) -> None:
        """Initialise the provider.

        Args:
            config: Common provider config (timeouts, rate limit).
            cli_path: Path or name of the ``claude`` executable.
            default_model: Fallback model alias when the request
                does not name an explicit Claude alias.
            subprocess_timeout: Hard cap (seconds) on a single
                ``claude -p`` invocation. Set higher than the proxy's
                generic HTTP read timeout because cold starts +
                cache-creation on the first turn can exceed 2 min.

        The provider runs ``claude`` from a clean tempdir so the CLI
        never auto-loads the proxy's own ``CLAUDE.md``, project
        sessions, or auto-memory; without that isolation the CLI
        would behave like an in-project assistant rather than a
        stateless LLM and contaminate responses.
        """
        super().__init__(config)
        if shutil.which(cli_path) is None:
            logger.warning(
                "CLAUDE_CODE_CLI_UNRESOLVABLE: cli_path={!r} not found on PATH; "
                "subprocess spawns will fail with OSError",
                cli_path,
            )
        resolved_cli = shutil.which(cli_path) or cli_path
        self._cli_path = resolved_cli
        self._default_model = default_model
        self._subprocess_timeout = subprocess_timeout
        self._workdir = Path(tempfile.mkdtemp(prefix="ferova_claude_code_"))
        self._global_rate_limiter = GlobalRateLimiter.get_scoped_instance(
            "claude_code",
            rate_limit=config.rate_limit,
            rate_window=config.rate_window,
            max_concurrency=config.max_concurrency,
        )

    async def cleanup(self) -> None:
        """No persistent resources held."""
        return None

    async def stream_response(
        self,
        request: Any,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
    ) -> AsyncIterator[str]:
        """Run ``claude -p`` and yield Anthropic SSE events.

        The prompt travels via STDIN, never argv: a Developer-session
        conversation serialized into one prompt exceeded the kernel's
        ARG_MAX and the spawn died with ``OSError: Argument list too
        long`` — surfaced as a naked 500 on the backstop hop
        (SP-DEV-PROMISE-DELIVERY step 2, 2026-07-05). The bounded
        system prompt stays in argv; spawn-time ``OSError`` maps to
        :class:`ProviderError` like every other provider failure.
        """
        message_id = f"msg_{uuid.uuid4()}"
        sse = SSEBuilder(message_id, request.model, input_tokens)

        prompt, system_prompt = self._build_prompt(request)
        cli_model = self._cli_model_for(request.model)
        cmd = [
            self._cli_path,
            "--print",
            "--disable-slash-commands",
            "--output-format",
            "json",
            "--model",
            cli_model,
        ]
        sysprompt_path: Path | None = None
        if system_prompt:
            sysprompt_path = self._workdir / f"sysprompt_{uuid.uuid4().hex}.txt"
            sysprompt_path.write_text(system_prompt, encoding="utf-8")
            cmd += ["--system-prompt-file", str(sysprompt_path)]

        req_tag = f" request_id={request_id}" if request_id else ""
        logger.info(
            "CLAUDE_CODE_STREAM:{} model={} prompt_chars={} system_prompt_chars={} cmd={}",
            req_tag,
            cli_model,
            len(prompt),
            len(system_prompt),
            shlex.join(cmd),
        )

        yield sse.message_start()

        async with self._global_rate_limiter.concurrency_slot():
            try:
                await self._global_rate_limiter.wait_if_blocked()
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(self._workdir),
                )
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(input=prompt.encode("utf-8")),
                    timeout=self._subprocess_timeout,
                )
                stdout = stdout_b.decode("utf-8", errors="replace")
                stderr = stderr_b.decode("utf-8", errors="replace")

                if proc.returncode != 0:
                    err = (
                        f"claude CLI exited {proc.returncode}: "
                        f"{stderr.strip() or stdout.strip()[:200]}"
                    )
                    raise ProviderError(append_request_id(err, request_id))

                payload = self._parse_cli_json(stdout, request_id)
                if payload.get("is_error"):
                    err = (
                        payload.get("result")
                        or payload.get("error")
                        or "claude CLI returned is_error"
                    )
                    raise ProviderError(append_request_id(str(err), request_id))

                result_text = payload.get("result") or ""

                heuristic = HeuristicToolParser()
                filtered_text, detected_tools = heuristic.feed(result_text)

                tool_use_emitted = False
                text_chars_emitted = 0

                if filtered_text:
                    for event in sse.ensure_text_block():
                        yield event
                    yield sse.emit_text_delta(filtered_text)
                    text_chars_emitted += len(filtered_text)

                for tool_use in detected_tools:
                    for event in sse.close_content_blocks():
                        yield event
                    block_idx = sse.blocks.allocate_index()
                    yield sse.content_block_start(
                        block_idx,
                        "tool_use",
                        id=tool_use["id"],
                        name=tool_use["name"],
                    )
                    yield sse.content_block_delta(
                        block_idx,
                        "input_json_delta",
                        json.dumps(tool_use["input"]),
                    )
                    yield sse.content_block_stop(block_idx)
                    tool_use_emitted = True

                for tool_use in heuristic.flush():
                    for event in sse.close_content_blocks():
                        yield event
                    block_idx = sse.blocks.allocate_index()
                    yield sse.content_block_start(
                        block_idx,
                        "tool_use",
                        id=tool_use["id"],
                        name=tool_use["name"],
                    )
                    yield sse.content_block_delta(
                        block_idx,
                        "input_json_delta",
                        json.dumps(tool_use["input"]),
                    )
                    yield sse.content_block_stop(block_idx)
                    tool_use_emitted = True

                stop_reason = "tool_use" if tool_use_emitted else "end_turn"
                output_tokens = _resolve_output_tokens(payload, text_chars_emitted)

            except (asyncio.CancelledError, GeneratorExit):
                raise
            except asyncio.TimeoutError as exc:
                err = f"claude CLI timed out after {self._subprocess_timeout}s"
                logger.error("CLAUDE_CODE_TIMEOUT:{} {}", req_tag, err)
                for event in sse.close_content_blocks():
                    yield event
                for event in sse.emit_error(append_request_id(err, request_id)):
                    yield event
                raise ProviderError(err) from exc
            except OSError as exc:
                err = f"claude CLI spawn failed: {exc}"
                logger.error("CLAUDE_CODE_SPAWN_FAILED:{} {}", req_tag, err)
                for event in sse.close_content_blocks():
                    yield event
                for event in sse.emit_error(append_request_id(err, request_id)):
                    yield event
                raise ProviderError(err) from exc
            except ProviderError as exc:
                logger.error("CLAUDE_CODE_ERROR:{} {}", req_tag, exc)
                for event in sse.close_content_blocks():
                    yield event
                for event in sse.emit_error(str(exc)):
                    yield event
                raise
            finally:
                if sysprompt_path is not None:
                    try:
                        sysprompt_path.unlink(missing_ok=True)
                    except OSError:
                        logger.warning(
                            "CLAUDE_CODE_SYSPROMPT_CLEANUP_FAILED:{} "
                            "could not unlink sysprompt file {}",
                            req_tag,
                            sysprompt_path,
                        )

        for event in sse.close_content_blocks():
            yield event
        yield sse.message_delta(stop_reason, output_tokens)
        yield sse.message_stop()

    def _cli_model_for(self, requested: str) -> str:
        """Pick a CLI model alias from the upstream Claude name."""
        lower = (requested or "").lower()
        if "haiku" in lower:
            return "haiku"
        if "opus" in lower:
            return "opus"
        if "sonnet" in lower:
            return "sonnet"
        return self._default_model

    def _build_prompt(self, request: Any) -> tuple[str, str]:
        """Flatten an Anthropic ``MessagesRequest`` into one prompt.

        The CLI does not accept structured chat history, so we stitch
        every prior turn into a single user prompt with explicit
        markers, leaving the genuine system prompt for
        ``--system-prompt``.

        The Anthropic ``tools`` field cannot be forwarded to ``claude
        -p`` (it would only be honoured via an MCP server). We instead
        render the schema as a text appendix that explicitly tells
        the model to emit text-based ``<tool_use>{...}</tool_use>``
        blocks — the harness's :class:`HeuristicToolParser` parses
        those out of the streamed response.
        """
        system_text = self._extract_system_prompt(request)
        tools_appendix = self._render_tools_appendix(getattr(request, "tools", None))
        if tools_appendix:
            system_text = system_text + "\n\n" + tools_appendix if system_text else tools_appendix
        messages = getattr(request, "messages", []) or []

        lines: list[str] = []
        for idx, msg in enumerate(messages):
            role = self._role_of(msg)
            text = self._content_text(msg)
            if not text.strip():
                continue
            if idx == len(messages) - 1 and role == "user":
                lines.append(text)
            else:
                lines.append(f"[{role}]\n{text}")
        prompt = "\n\n".join(lines).strip() or "(empty)"
        return prompt, system_text

    @staticmethod
    def _render_tools_appendix(tools: Any) -> str:
        """Render the ``tools`` schema as a text contract appended to the system prompt.

        ``claude -p`` cannot natively call structured tools without an
        MCP server, so without this appendix Claude Sonnet will see the
        prompt mention tool names but find no mounted tool, then refuse
        with a hallucinated MCP-not-mounted error instead of emitting
        the text-based ``<tool_use>`` blocks the harness expects.
        """
        if not tools:
            return ""
        lines = [
            "TOOL CALL PROTOCOL — READ CAREFULLY",
            "",
            "You do NOT have native (MCP) tool execution in this run. The",
            "harness expects you to emit tool calls as plain TEXT blocks",
            "in this exact format (one block per turn unless explicitly",
            "asked to chain):",
            "",
            '    <tool_use>{"name": "<tool_name>", "args": {<json args>}}</tool_use>',
            "",
            "Each tool call must be a single self-closed XML-like tag",
            "containing a JSON object with EXACTLY two keys: ``name`` and",
            "``args``. The harness parses these tags out of your text",
            "response, executes the tool, and re-injects the result on",
            "the next turn as a ``<tool_result>...</tool_result>`` block.",
            "",
            "Available tools:",
            "",
        ]
        for tool in tools:
            name, description, schema = ClaudeCodeProvider._extract_tool_meta(tool)
            if not name:
                continue
            lines.append(f"- **{name}**: {description or '(no description)'}")
            if schema:
                params = schema.get("properties") if isinstance(schema, dict) else None
                if isinstance(params, dict) and params:
                    arg_keys = ", ".join(sorted(params))
                    required = (
                        ", ".join(schema.get("required", [])) if isinstance(schema, dict) else ""
                    )
                    lines.append(
                        f"  args: {{{arg_keys}}}" + (f" required={required}" if required else "")
                    )
        lines += [
            "",
            "DO NOT call any other tool, search the file system, or write",
            "to disk. The harness only listens for ``<tool_use>`` text",
            "blocks. End your turn with the next required tool call,",
            "until the mission's `finish` tool is the appropriate next step.",
        ]
        return "\n".join(lines)

    @staticmethod
    def _extract_tool_meta(tool: Any) -> tuple[str, str, dict[str, Any] | None]:
        """Pull (name, description, input_schema) from an Anthropic / OpenAI tool entry.

        Supports three input shapes:

        * OpenAI dict form
          ``{"type": "function", "function": {"name": ..., "parameters": ...}}``
        * Anthropic dict form
          ``{"name": ..., "description": ..., "input_schema": ...}``
        * Pydantic-like object exposing ``name`` / ``description`` /
          ``input_schema`` (or a nested ``function`` attribute for
          OpenAI ergonomics).
        """
        if isinstance(tool, dict):
            if isinstance(tool.get("function"), dict):
                fn = tool["function"]
                return (
                    str(fn.get("name") or ""),
                    str(fn.get("description") or ""),
                    fn.get("parameters") if isinstance(fn.get("parameters"), dict) else None,
                )
            return (
                str(tool.get("name") or ""),
                str(tool.get("description") or ""),
                tool.get("input_schema") if isinstance(tool.get("input_schema"), dict) else None,
            )
        name = getattr(tool, "name", None)
        if name is None and hasattr(tool, "function"):
            fn = tool.function
            return (
                str(getattr(fn, "name", "") or ""),
                str(getattr(fn, "description", "") or ""),
                getattr(fn, "parameters", None),
            )
        return (
            str(name or ""),
            str(getattr(tool, "description", "") or ""),
            getattr(tool, "input_schema", None),
        )

    @staticmethod
    def _extract_system_prompt(request: Any) -> str:
        """Extract a flat system prompt string from the request."""
        system = getattr(request, "system", None)
        if system is None:
            return ""
        if isinstance(system, str):
            return system
        if isinstance(system, list):
            chunks: list[str] = []
            for entry in system:
                if isinstance(entry, str):
                    chunks.append(entry)
                elif isinstance(entry, dict):
                    text = entry.get("text") or entry.get("content") or ""
                    if isinstance(text, str):
                        chunks.append(text)
                else:
                    text = getattr(entry, "text", None)
                    if isinstance(text, str):
                        chunks.append(text)
            return "\n\n".join(chunks)
        return str(system)

    @staticmethod
    def _role_of(msg: Any) -> str:
        if isinstance(msg, dict):
            return str(msg.get("role") or "user")
        return str(getattr(msg, "role", "user"))

    @staticmethod
    def _content_text(msg: Any) -> str:
        """Extract the human-readable text from a message content payload."""
        content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks: list[str] = []
            for entry in content:
                if isinstance(entry, str):
                    chunks.append(entry)
                    continue
                if isinstance(entry, dict):
                    etype = entry.get("type")
                    if etype == "text":
                        chunks.append(str(entry.get("text") or ""))
                    elif etype == "tool_use":
                        chunks.append(
                            f"<tool_use>{json.dumps({'name': entry.get('name'), 'args': entry.get('input')})}</tool_use>"
                        )
                    elif etype == "tool_result":
                        result = entry.get("content")
                        if isinstance(result, list):
                            inner = "".join(
                                str(item.get("text", "")) if isinstance(item, dict) else str(item)
                                for item in result
                            )
                        else:
                            inner = str(result or "")
                        chunks.append(f"<tool_result>{inner}</tool_result>")
                    elif "text" in entry:
                        chunks.append(str(entry.get("text") or ""))
                    continue
                etype = getattr(entry, "type", None)
                if etype == "text":
                    chunks.append(str(getattr(entry, "text", "") or ""))
                elif etype == "tool_use":
                    chunks.append(
                        f"<tool_use>{json.dumps({'name': getattr(entry, 'name', None), 'args': getattr(entry, 'input', None)})}</tool_use>"
                    )
                elif etype == "tool_result":
                    chunks.append(f"<tool_result>{getattr(entry, 'content', '')}</tool_result>")
            return "\n".join(c for c in chunks if c)
        return str(content)

    @staticmethod
    def _parse_cli_json(stdout: str, request_id: str | None) -> dict[str, Any]:
        """Parse the JSON object emitted by ``claude -p --output-format json``."""
        text = stdout.strip()
        if not text:
            raise ProviderError(append_request_id("claude CLI returned empty stdout", request_id))
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                append_request_id(f"claude CLI emitted non-JSON: {text[:200]}", request_id)
            ) from exc


def _resolve_output_tokens(payload: dict[str, Any], text_chars: int) -> int:
    """Pick the best output-token estimate for the terminal ``message_delta``.

    The proxy's :func:`peek_for_content` failover oracle keys off
    ``message_delta.usage.output_tokens > 0`` ; emitting ``0`` (or never
    emitting ``message_delta`` at all) makes the proxy mark this
    candidate dead and skip to the next chain entry — an outright
    breakage of the Claude Max code path observed via
    ``tests/smoke/test_provider_chains_reachability.py`` on 2026-05-09.

    Order of preference :

    1. ``payload["usage"]["output_tokens"]`` — what the CLI itself reports
       when the upstream session populates it (newer Claude Code releases
       expose this).
    2. ``ceil(text_chars / 4)`` — a conservative chars-to-tokens estimate
       that yields a non-zero positive integer whenever the model emitted
       any text or tool_use payload.
    3. ``1`` — a sentinel for empty completions ; better than ``0`` so
       the proxy doesn't classify a clean ``end_turn`` with no output
       as a transport failure.

    Args:
        payload: Decoded JSON object returned by the Claude Code CLI.
        text_chars: Total number of text characters streamed to the
            client (excludes tool_use payload, which is bookkept by the
            tool blocks themselves).

    Returns:
        A positive integer suitable for ``message_delta.usage.output_tokens``.
    """
    usage = payload.get("usage")
    if isinstance(usage, dict):
        candidate = usage.get("output_tokens")
        if isinstance(candidate, int) and candidate > 0:
            return candidate
    if text_chars > 0:
        return max(1, (text_chars + 3) // 4)
    return 1
