"""Unit tests for SP-BUILDER-MEMORY — the agentmemory REST client.

The client must NEVER raise on a transport failure (a down service must
not break a build), so these tests monkeypatch ``httpx.post`` with fakes
that return scripted responses or raise ``httpx`` errors.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from ferova.memory import agentmemory_client


class _FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self) -> None:
        if not (200 <= self.status_code < 300):
            raise httpx.HTTPError(f"status {self.status_code}")

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _post_returning(response: _FakeResponse):
    def _post(*args: Any, **kwargs: Any) -> _FakeResponse:
        return response

    return _post


def _post_raising(exc: Exception):
    def _post(*args: Any, **kwargs: Any):
        raise exc

    return _post


def test_recall_extracts_narratives(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "results": [
            {"observation": {"narrative": "lesson A"}},
            {"observation": {"title": "lesson B"}},
            {"observation": {"narrative": "   "}},
            {"not_an_observation": True},
        ]
    }
    monkeypatch.setattr(
        agentmemory_client.httpx, "post", _post_returning(_FakeResponse(200, payload))
    )

    assert agentmemory_client.recall("q", project="builder", base_url="http://x") == [
        "lesson A",
        "lesson B",
    ]


def test_recall_empty_on_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agentmemory_client.httpx, "post", _post_raising(httpx.ConnectError("down")))
    assert agentmemory_client.recall("q", project="builder", base_url="http://x") == []


def test_recall_empty_on_bad_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        agentmemory_client.httpx, "post", _post_returning(_FakeResponse(200, ValueError("no json")))
    )
    assert agentmemory_client.recall("q", project="builder", base_url="http://x") == []


def test_recall_empty_on_non_2xx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        agentmemory_client.httpx, "post", _post_returning(_FakeResponse(500, {"results": []}))
    )
    assert agentmemory_client.recall("q", project="builder", base_url="http://x") == []


def test_remember_returns_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        agentmemory_client.httpx, "post", _post_returning(_FakeResponse(200, {"success": True}))
    )
    assert agentmemory_client.remember("c", project="builder", base_url="http://x") is True


def test_remember_false_on_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agentmemory_client.httpx, "post", _post_raising(httpx.ReadTimeout("slow")))
    assert agentmemory_client.remember("c", project="builder", base_url="http://x") is False


def test_remember_false_when_success_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        agentmemory_client.httpx, "post", _post_returning(_FakeResponse(200, {"success": False}))
    )
    assert agentmemory_client.remember("c", project="builder", base_url="http://x") is False
