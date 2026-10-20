"""Truthful boundary fakes for the direct providers (SP-UNIVERSAL-PROVIDERS).

* :func:`write_fake_claude` writes an executable that answers exactly
  the two invocations repoach makes of the Claude Code CLI:
  ``claude auth status --json`` and ``claude --print --output-format json
  --model <m> [--system-prompt-file f]`` with the prompt on stdin.
* :class:`FakeAnthropicServer` is a real HTTP server on the loopback that
  answers ``POST /v1/messages`` with a Messages API SSE stream, or with
  an error status.

Neither touches repoach's own code: the transports under test spawn the
executable and speak HTTP to the server exactly as they do in production.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

APPROVE_JSON = json.dumps(
    {"verdict": "APPROVE", "summary": "fake reviewer: nothing to report", "comments": []}
)
"""A reviewer answer every persona parses as APPROVE with no finding."""

_CLAUDE_SCRIPT = """#!{python}
import json
import os
import time
import sys

LOG = {log!r}
args = sys.argv[1:]
if args[:2] == ["auth", "status"]:
    logged_in = {logged_in!r}
    print(json.dumps({{
        "loggedIn": logged_in,
        "authMethod": "claude.ai" if logged_in else "none",
        "email": "someone@example.invalid",
    }}))
    sys.exit(0 if logged_in else 1)
prompt = sys.stdin.read()
system = ""
if "--system-prompt-file" in args:
    with open(args[args.index("--system-prompt-file") + 1], encoding="utf-8") as handle:
        system = handle.read()
with open(LOG, "a", encoding="utf-8") as handle:
    handle.write(json.dumps({{
        "argv": args,
        "prompt_chars": len(prompt),
        "system_chars": len(system),
        "cwd": os.getcwd(),
        "session_leader": os.getsid(0) == os.getpid(),
        "pid": os.getpid(),
        "env": dict(os.environ),
    }}) + "\\n")
time.sleep({sleep_s!r})
exit_code = {exit_code!r}
if exit_code:
    sys.stderr.write({stderr!r})
    sys.exit(exit_code)
print(json.dumps({{
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": {result!r},
    "usage": {{"input_tokens": 11, "output_tokens": 7}},
}}))
"""


def write_fake_claude(
    directory: Path,
    *,
    logged_in: bool = True,
    result: str = APPROVE_JSON,
    exit_code: int = 0,
    stderr: str = "",
    sleep_s: float = 0.0,
) -> tuple[Path, Path]:
    """Write a fake ``claude`` executable into ``directory``.

    Args:
        directory: Where to write it (put it first on ``PATH``).
        logged_in: What ``auth status`` reports.
        result: The ``result`` text every ``--print`` call returns.
        exit_code: Non-zero makes every ``--print`` call fail with ``stderr``.
        stderr: The failure text.
        sleep_s: Seconds every ``--print`` call sleeps before answering.

    Returns:
        ``(executable, call log)``; the log holds one JSON line per
        ``--print`` call.
    """
    directory.mkdir(parents=True, exist_ok=True)
    log = directory / "claude_calls.jsonl"
    executable = directory / "claude"
    executable.write_text(
        _CLAUDE_SCRIPT.format(
            python=sys.executable,
            log=str(log),
            logged_in=logged_in,
            result=result,
            exit_code=exit_code,
            stderr=stderr,
            sleep_s=sleep_s,
        ),
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable, log


def read_calls(log: Path) -> list[dict[str, Any]]:
    """Return the ``--print`` calls a fake ``claude`` logged."""
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def messages_stream(text: str, model: str) -> bytes:
    """Return the SSE body the Messages API streams for one text answer."""
    return b"".join(
        [
            _sse(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_fake",
                        "type": "message",
                        "role": "assistant",
                        "model": model,
                        "content": [],
                        "stop_reason": None,
                        "usage": {"input_tokens": 12, "output_tokens": 1},
                    },
                },
            ),
            _sse(
                "content_block_start",
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
            ),
            _sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": text},
                },
            ),
            _sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 9},
                },
            ),
            _sse("message_stop", {"type": "message_stop"}),
        ]
    )


class FakeAnthropicServer:
    """A loopback HTTP server standing in for the Messages API.

    Attributes:
        base_url: ``http://127.0.0.1:<port>/v1`` — what
            ``REPOACH_ANTHROPIC_BASE_URL`` is set to.
        requests: One ``{"path", "headers", "body"}`` per request received.
    """

    def __init__(self, *, status: int = 200, text: str = APPROVE_JSON) -> None:
        self.status = status
        self.text = text
        self.requests: list[dict[str, Any]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                owner.requests.append(
                    {"path": self.path, "headers": dict(self.headers), "body": body}
                )
                if owner.status != 200:
                    payload = json.dumps(
                        {"type": "error", "error": {"type": "error", "message": "fake refusal"}}
                    ).encode()
                    self.send_response(owner.status)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                stream = messages_stream(owner.text, str(body.get("model") or ""))
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(stream)))
                self.end_headers()
                self.wfile.write(stream)

            def log_message(self, format: str, *args: Any) -> None:
                return None

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}/v1"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> FakeAnthropicServer:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
