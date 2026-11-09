"""Antigravity CLI (``agy``) subprocess provider.

Routes Anthropic ``Messages`` requests through the local ``agy`` CLI in
headless ``stream-json`` mode, authenticating via the user's Antigravity
login, Application Default Credentials, or Gemini API key.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import signal
import tempfile
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import Any

from loguru import logger

from repoach.llm_proxy.cli.process_registry import register_pid, unregister_pid
from repoach.llm_proxy.core.anthropic import (
    HeuristicToolParser,
    SSEBuilder,
    append_request_id,
)
from repoach.llm_proxy.providers.base import BaseProvider, ProviderConfig
from repoach.llm_proxy.providers.exceptions import OutputCapExceededError, ProviderError
from repoach.llm_proxy.providers.rate_limit import GlobalRateLimiter

_SUBPROCESS_KILL_GRACE_S: float = 2.0

CLI_OUTPUT_CAP_ERROR = "max_output_tokens"
"""The ``api_error`` token reported when a CLI reply exceeds its output token cap."""

CLI_ERROR_TEXT_MAX_CHARS = 2000
"""Maximum characters of CLI result/error text preserved in a :class:`ProviderError`."""

AGY_DEFAULT_MODEL = "gemini-3.8-flash-medium"
"""Default ``agy`` model slug when no explicit model or tier alias is supplied."""

AGY_TIER_MODELS: dict[str, str] = {
    "opus": "gemini-4-argon-high",
    "sonnet": "gemini-4-argon-medium",
    "haiku": AGY_DEFAULT_MODEL,
}
"""Default ``agy`` model slugs for the three capability tiers."""

AGY_SECRET_SUFFIXES: tuple[str, ...] = ("_API_KEY", "_AUTH_TOKEN", "_TOKEN")
"""Variable-name endings stripped from the ``agy`` child environment."""

AGY_KEPT_SECRETS: frozenset[str] = frozenset({"GEMINI_API_KEY"})
"""The one key ``agy`` keeps for its ``modelProvider: "gemini"`` mode."""


def scrub_antigravity_env(environ: Mapping[str, str]) -> dict[str, str]:
    """Return ``environ`` minus unrelated keys and tokens, preserving ``GEMINI_API_KEY``.

    Every variable ending in :data:`AGY_SECRET_SUFFIXES` (such as
    ``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``, ``REPOACH_ANTHROPIC_AUTH_TOKEN``)
    is removed except :data:`AGY_KEPT_SECRETS`, so a gateway or direct
    ``agy`` subprocess never inherits foreign credentials.
    """
    return {
        name: value
        for name, value in environ.items()
        if name in AGY_KEPT_SECRETS or not name.upper().endswith(AGY_SECRET_SUFFIXES)
    }


def _thinking_disabled(request: Any) -> bool:
    """Return True when the request turns thinking off (``{"type": "disabled"}``)."""
    thinking = getattr(request, "thinking", None)
    kind = thinking.get("type") if isinstance(thinking, dict) else getattr(thinking, "type", None)
    enabled = (
        thinking.get("enabled")
        if isinstance(thinking, dict)
        else getattr(thinking, "enabled", None)
    )
    return kind == "disabled" or enabled is False


def _send_signal(proc: asyncio.subprocess.Process, sig: int, *, group: bool) -> None:
    """Send ``sig`` to ``proc`` or to its process group when ``group`` is True."""
    if group:
        os.killpg(proc.pid, sig)
    elif sig == signal.SIGKILL:
        proc.kill()
    else:
        proc.terminate()


async def _kill_subprocess_on_exit(
    proc: asyncio.subprocess.Process, req_tag: str, *, group: bool = False
) -> None:
    """Ensure ``proc`` is not left running past the end of a stream call."""
    if proc.returncode is not None:
        return
    try:
        _send_signal(proc, signal.SIGTERM, group=group)
    except ProcessLookupError as exc:
        logger.debug(
            "ANTIGRAVITY_SUBPROCESS_KILL_RACE:{} pid={} terminate raced with exit ({})",
            req_tag,
            proc.pid,
            exc,
        )
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=_SUBPROCESS_KILL_GRACE_S)
        return
    except asyncio.TimeoutError:
        logger.warning(
            "ANTIGRAVITY_SUBPROCESS_KILL_ESCALATE:{} pid={} still alive {}s after SIGTERM, "
            "sending SIGKILL",
            req_tag,
            proc.pid,
            _SUBPROCESS_KILL_GRACE_S,
        )
    try:
        _send_signal(proc, signal.SIGKILL, group=group)
    except ProcessLookupError as exc:
        logger.debug(
            "ANTIGRAVITY_SUBPROCESS_KILL_RACE:{} pid={} kill raced with exit ({})",
            req_tag,
            proc.pid,
            exc,
        )
        return
    await proc.wait()


def _resolve_output_tokens(payload: Mapping[str, Any], text_chars: int) -> int:
    """Pick the output-token count for the terminal ``message_delta``."""
    usage = payload.get("usage")
    if isinstance(usage, dict):
        candidate = usage.get("output_tokens")
        if isinstance(candidate, int) and candidate > 0:
            return candidate
    if text_chars > 0:
        return max(1, (text_chars + 3) // 4)
    return 1


def _extract_agy_result(stdout: str) -> dict[str, Any] | None:
    """Return the terminal ``result`` payload from ``agy`` NDJSON or JSON stdout."""
    fallback: dict[str, Any] | None = None
    for raw_line in reversed(stdout.splitlines()):
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.debug("ANTIGRAVITY_STDOUT_LINE_NOT_JSON: {}", exc)
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("event") == "result" and isinstance(obj.get("result"), dict):
            return obj["result"]
        if "status" in obj and ("response" in obj or "error" in obj) and fallback is None:
            fallback = obj
    return fallback


def agy_result_error(
    payload: Mapping[str, Any], request_id: str | None, *, prefix: str = ""
) -> ProviderError:
    """Turn an ``agy`` non-SUCCESS result object into a :class:`ProviderError`.

    Args:
        payload: The terminal ``result`` dict emitted by ``agy``.
        request_id: Optional request id appended to the error message.
        prefix: Optional prefix prepended before the CLI error text.
    """
    status = payload.get("status") or "ERROR"
    text = str(
        payload.get("error")
        or payload.get("response")
        or payload.get("result")
        or f"agy CLI returned status={status}"
    )
    message = append_request_id(f"{prefix}{text[:CLI_ERROR_TEXT_MAX_CHARS]}", request_id)
    if (
        payload.get("api_error") == CLI_OUTPUT_CAP_ERROR
        or payload.get("error") == CLI_OUTPUT_CAP_ERROR
        or CLI_OUTPUT_CAP_ERROR in text.lower()
    ):
        return OutputCapExceededError(message, raw_error=dict(payload))
    return ProviderError(message)


def agy_exit_error(
    returncode: int | None, stdout: str, stderr: str, request_id: str | None
) -> ProviderError:
    """Turn a non-zero ``agy`` exit into the error the failover reads.

    Args:
        returncode: The CLI's exit status.
        stdout: What it printed on stdout.
        stderr: What it printed on stderr.
        request_id: Optional request id appended to the message.
    """
    prefix = f"agy CLI exited {returncode}: "
    payload = _extract_agy_result(stdout)
    if payload is not None and (
        payload.get("status") not in (None, "SUCCESS") or payload.get("is_error")
    ):
        return agy_result_error(payload, request_id, prefix=prefix)
    err = f"{prefix}{stderr.strip() or stdout.strip()[:200]}"
    return ProviderError(append_request_id(err, request_id))


def _apply_agy_input_usage(sse: SSEBuilder, payload: Mapping[str, Any]) -> None:
    """Copy ``agy`` token counts onto ``sse``.

    ``agy`` reports ``usage.input_tokens`` as total input tokens (including
    ``cache_read_tokens``), so the uncached portion stored on ``sse.input_tokens``
    is ``max(0, input_tokens - cache_read_tokens)``.
    """
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return
    cache_read = usage.get("cache_read_tokens")
    if not isinstance(cache_read, int):
        cache_read = usage.get("cache_read_input_tokens")
    if isinstance(cache_read, int) and cache_read >= 0:
        sse.cache_read_tokens = cache_read
    else:
        cache_read = 0
    cache_write = usage.get("cache_creation_input_tokens")
    if isinstance(cache_write, int) and cache_write >= 0:
        sse.cache_write_tokens = cache_write
    reported = usage.get("input_tokens")
    if isinstance(reported, int) and reported >= 0:
        sse.input_tokens = max(0, reported - cache_read)


class AntigravityProvider(BaseProvider):
    """Wraps headless ``agy`` in ``stream-json`` mode to expose completions."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        cli_path: str = "agy",
        default_model: str = AGY_DEFAULT_MODEL,
        subprocess_timeout: float = 600.0,
        own_process_group: bool = True,
        child_env: Callable[[Mapping[str, str]], dict[str, str]] | None = scrub_antigravity_env,
    ) -> None:
        """Initialise the Antigravity CLI subprocess provider.

        Args:
            config: Common provider config (timeouts, rate limit).
            cli_path: Path or name of the ``agy`` executable.
            default_model: Fallback ``agy`` model slug when the request
                does not name an explicit model.
            subprocess_timeout: Hard cap in seconds on a single ``agy``
                invocation.
            own_process_group: Spawn each ``agy`` child in its own process
                group and signal the group on cleanup.
            child_env: Callable that builds the scrubbed child environment
                from ``os.environ`` at spawn time (defaults to
                :func:`scrub_antigravity_env`).
        """
        super().__init__(config)
        if shutil.which(cli_path) is None:
            logger.warning(
                "ANTIGRAVITY_CLI_UNRESOLVABLE: cli_path={!r} not found on PATH; "
                "subprocess spawns will fail with OSError",
                cli_path,
            )
        resolved_cli = shutil.which(cli_path) or cli_path
        self._cli_path = resolved_cli
        self._default_model = default_model
        self._subprocess_timeout = subprocess_timeout
        self._own_process_group = own_process_group
        self._child_env = child_env
        self._workdir = Path(tempfile.mkdtemp(prefix="repoach_antigravity_"))
        self._global_rate_limiter = GlobalRateLimiter.get_scoped_instance(
            "antigravity",
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
        """Run ``agy`` in ``stream-json`` mode and yield Anthropic SSE events.

        The subprocess runs inside an empty isolated temporary directory
        (``self._workdir``), with a scrubbed environment
        (:func:`scrub_antigravity_env`) that strips all foreign
        ``_API_KEY`` / ``_AUTH_TOKEN`` / ``_TOKEN`` secrets, and never
        passes ``--dangerously-skip-permissions``.
        """
        message_id = f"msg_{uuid.uuid4()}"
        sse = SSEBuilder(
            message_id,
            request.model,
            input_tokens,
            log_full_content=self._config.log_full_content,
        )

        full_prompt = self._build_full_prompt(request)
        cli_model = self._cli_model_for(request.model)
        cmd = [
            self._cli_path,
            "--disable-slash-commands",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--model",
            cli_model,
        ]
        if _thinking_disabled(request):
            cmd += ["--effort", "low"]

        stdin_line = (
            json.dumps(
                {"event": "user", "message": {"content": full_prompt}},
                ensure_ascii=False,
            )
            + "\n"
        )

        req_tag = f" request_id={request_id}" if request_id else ""
        logger.info(
            "ANTIGRAVITY_STREAM:{} model={} prompt_chars={} cmd={}",
            req_tag,
            cli_model,
            len(full_prompt),
            shlex.join(cmd),
        )

        yield sse.message_start()

        proc: asyncio.subprocess.Process | None = None
        async with self._global_rate_limiter.concurrency_slot():
            try:
                await self._global_rate_limiter.wait_if_blocked()
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(self._workdir),
                    start_new_session=self._own_process_group,
                    env=self._spawn_env(),
                )
                register_pid(proc.pid)
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(input=stdin_line.encode("utf-8")),
                    timeout=self._subprocess_timeout,
                )
                stdout = stdout_b.decode("utf-8", errors="replace")
                stderr = stderr_b.decode("utf-8", errors="replace")

                if proc.returncode != 0:
                    raise agy_exit_error(proc.returncode, stdout, stderr, request_id)

                payload = self._parse_agy_stdout(stdout, request_id)
                if payload.get("status") not in (None, "SUCCESS") or payload.get("is_error"):
                    raise agy_result_error(payload, request_id)

                result_text = str(payload.get("response") or payload.get("result") or "")

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
                _apply_agy_input_usage(sse, payload)

            except (asyncio.CancelledError, GeneratorExit):
                raise
            except asyncio.TimeoutError as exc:
                err = f"agy CLI timed out after {self._subprocess_timeout}s"
                logger.error("ANTIGRAVITY_TIMEOUT:{} {}", req_tag, err)
                for event in sse.close_content_blocks():
                    yield event
                for event in sse.emit_error(append_request_id(err, request_id)):
                    yield event
                raise ProviderError(err) from exc
            except OSError as exc:
                err = f"agy CLI spawn failed: {exc}"
                logger.error("ANTIGRAVITY_SPAWN_FAILED:{} {}", req_tag, err)
                for event in sse.close_content_blocks():
                    yield event
                for event in sse.emit_error(append_request_id(err, request_id)):
                    yield event
                raise ProviderError(err) from exc
            except ProviderError as exc:
                logger.error("ANTIGRAVITY_ERROR:{} {}", req_tag, exc)
                for event in sse.close_content_blocks():
                    yield event
                for event in sse.emit_error(str(exc)):
                    yield event
                raise
            finally:
                if proc is not None:
                    await _kill_subprocess_on_exit(proc, req_tag, group=self._own_process_group)
                    unregister_pid(proc.pid)

        for event in sse.close_content_blocks():
            yield event
        yield sse.message_delta(stop_reason, output_tokens)
        yield sse.message_stop()

    def _spawn_env(self) -> dict[str, str] | None:
        """Return the scrubbed child environment when ``child_env`` is configured."""
        if self._child_env is None:
            return None
        return self._child_env(os.environ)

    def _cli_model_for(self, requested: str) -> str:
        """Resolve an ``agy`` model slug from the requested model or tier name."""
        slug = (requested or "").removeprefix("antigravity/").strip()
        if not slug:
            return self._default_model
        lower = slug.lower()
        if lower in AGY_TIER_MODELS:
            return AGY_TIER_MODELS[lower]
        if lower.endswith(("-low", "-medium", "-high")) or lower.startswith(("gemini-", "gpt-")):
            return slug
        if "haiku" in lower:
            return AGY_TIER_MODELS["haiku"]
        if "opus" in lower:
            return AGY_TIER_MODELS["opus"]
        if "sonnet" in lower:
            return AGY_TIER_MODELS["sonnet"]
        return slug

    @classmethod
    def _build_full_prompt(cls, request: Any) -> str:
        """Flatten system prompt, tool contract appendix, and chat history into one prompt."""
        system_text = cls._extract_system_prompt(request)
        tools_appendix = cls._render_tools_appendix(getattr(request, "tools", None))
        if tools_appendix:
            system_text = f"{system_text}\n\n{tools_appendix}" if system_text else tools_appendix
        messages = getattr(request, "messages", []) or []

        lines: list[str] = []
        if system_text.strip():
            lines.append(f"[system]\n{system_text.strip()}")
        for idx, msg in enumerate(messages):
            role = cls._role_of(msg)
            text = cls._content_text(msg)
            if not text.strip():
                continue
            if idx == len(messages) - 1 and role == "user" and not lines:
                lines.append(text)
            else:
                lines.append(f"[{role}]\n{text}")
        return "\n\n".join(lines).strip() or "(empty)"

    @classmethod
    def _render_tools_appendix(cls, tools: Any) -> str:
        """Render the ``tools`` schema as a text contract appended to the system prompt."""
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
            name, description, schema = cls._extract_tool_meta(tool)
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
        """Pull ``(name, description, input_schema)`` from an Anthropic / OpenAI tool entry."""
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
    def _parse_agy_stdout(stdout: str, request_id: str | None) -> dict[str, Any]:
        """Extract the terminal result object from ``agy`` stdout or raise :class:`ProviderError`."""
        text = stdout.strip()
        if not text:
            raise ProviderError(append_request_id("agy CLI returned empty stdout", request_id))
        payload = _extract_agy_result(text)
        if payload is None:
            raise ProviderError(
                append_request_id(f"agy CLI emitted no result event: {text[:200]}", request_id)
            )
        return payload
