"""Tests for :mod:`ferova.review.notifier`.

We never hit the real Anthropic endpoint here — every external call
goes through ``httpx.MockTransport`` so the assertions can verify the
exact headers, URL, and body shape we send.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from ferova.review import notifier


def _stub_transport(callback) -> httpx.MockTransport:
    """Build an httpx MockTransport that delegates to *callback*."""
    return httpx.MockTransport(callback)


def test_fire_review_routine_success(monkeypatch):
    """Happy path: 200 OK with session_id + url is parsed."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "claude_code_session_id": "sess-abc",
                "claude_code_session_url": "https://claude.ai/code/sess-abc",
            },
        )

    monkeypatch.setattr(notifier, "httpx", _patched_httpx(handler))
    res = notifier.fire_review_routine(
        routine_id="rt-42",
        token="FAKE-TEST-TOKEN-NOT-REAL",
        payload={"pr_number": 1, "final_verdict": "APPROVE"},
    )

    assert res.ok
    assert res.status_code == 200
    assert res.session_id == "sess-abc"
    assert res.session_url == "https://claude.ai/code/sess-abc"
    assert "v1/claude_code/routines/rt-42/fire" in captured["url"]
    assert captured["headers"]["authorization"] == "Bearer FAKE-TEST-TOKEN-NOT-REAL"
    assert captured["headers"]["anthropic-beta"].startswith("experimental-cc-routine-")
    # The body must wrap the payload as JSON inside the "text" field.
    assert "text" in captured["body"]
    inner = json.loads(captured["body"]["text"])
    assert inner["pr_number"] == 1
    assert inner["final_verdict"] == "APPROVE"


def test_fire_review_routine_returns_error_on_4xx(monkeypatch):
    """A 401/403/etc. surfaces as ok=False with the body captured."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorised")

    monkeypatch.setattr(notifier, "httpx", _patched_httpx(handler))
    res = notifier.fire_review_routine(
        routine_id="rt-42",
        token="bad-token",
        payload={"pr_number": 1},
    )
    assert not res.ok
    assert res.status_code == 401
    assert "unauthorised" in (res.error or "")


def test_fire_review_routine_missing_creds_no_network(monkeypatch):
    """Missing routine_id or token returns a clean error without HTTP."""
    called = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["count"] += 1
        return httpx.Response(200, json={})

    monkeypatch.setattr(notifier, "httpx", _patched_httpx(handler))

    r1 = notifier.fire_review_routine(routine_id="", token="x", payload={})
    r2 = notifier.fire_review_routine(routine_id="rt", token="", payload={})
    assert not r1.ok and not r2.ok
    assert called["count"] == 0
    assert "missing" in (r1.error or "").lower()


def test_fire_review_routine_slims_payload_when_oversized(monkeypatch):
    """When payload > 60KB, per-comment bodies are dropped before send."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    monkeypatch.setattr(notifier, "httpx", _patched_httpx(handler))

    # Build a payload above 60KB by stuffing a huge comment body.
    huge_body = "x" * 80_000
    payload = {
        "pr_number": 9,
        "final_verdict": "COMMENT",
        "reviews": [
            {
                "role": "architect",
                "verdict": "COMMENT",
                "comments": [{"body": huge_body}],
            }
        ],
    }
    notifier.fire_review_routine(routine_id="rt", token="FAKE-TEST-TOKEN-NOT-REAL", payload=payload)
    inner = json.loads(captured["body"]["text"])
    assert "_note" in inner
    assert "comments" not in inner["reviews"][0]


def test_fire_review_routine_handles_http_error(monkeypatch):
    """Network exception is captured into the result, never raised."""

    class _BoomHttpx:
        HTTPError = httpx.HTTPError

        @staticmethod
        def post(*args, **kwargs):
            raise httpx.ConnectError("dns blew up")

    monkeypatch.setattr(notifier, "httpx", _BoomHttpx)
    res = notifier.fire_review_routine(
        routine_id="rt",
        token="FAKE-TEST-TOKEN-NOT-REAL",
        payload={"pr_number": 1},
    )
    assert not res.ok
    assert res.status_code == 0
    assert "dns blew up" in (res.error or "")


# ---------- helpers ----------


def _patched_httpx(handler):
    """Build a stand-in for the ``httpx`` module that uses MockTransport."""

    transport = _stub_transport(handler)
    client_factory = lambda **kw: httpx.Client(transport=transport, **kw)

    class _Patched:
        HTTPError = httpx.HTTPError

        @staticmethod
        def post(url, *, headers=None, json=None, timeout=None):
            with client_factory() as c:
                return c.post(url, headers=headers, json=json, timeout=timeout)

    return _Patched


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
