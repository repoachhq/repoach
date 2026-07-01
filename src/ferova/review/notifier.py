"""Routine notifier — fires a Claude Code routine with the review outcome.

Once the four reviewer bots finish, the GitHub Actions workflow calls
:func:`fire_review_routine` which POSTs the TeamOutcome JSON (or a
short summary) to the public Claude Code routines API.  Anthropic
launches a fresh Claude Code session pre-loaded with the payload as
context, so the user gets an actionable summary delivered directly to
the assistant rather than having to fetch the report manually.

API reference (documented endpoint):
    POST https://api.anthropic.com/v1/claude_code/routines/{routine_id}/fire
    Authorization: Bearer <per-routine-bearer-token>
    anthropic-beta: experimental-cc-routine-2026-04-01
    anthropic-version: 2023-06-01
    body: {"text": "<freeform string up to 65KB>"}

The routine is created out-of-band by the user via
``claude.ai/code/routines`` and yields a ``routine_id`` + a
per-routine bearer token; both go into GitHub Actions repo secrets
(``CLAUDE_CODE_ROUTINE_ID`` + ``CLAUDE_CODE_ROUTINE_TOKEN``) and into
``.env`` for local triggering.

Module-level constants :
    ``_MAX_TEXT_BYTES`` is set at 60 KB to leave headroom under the
    65 KB hard cap of the routine-fire endpoint.  When the payload
    overflows that cap, :func:`fire_review_routine` slims it by
    dropping the heavy ``reviews[].comments`` arrays while keeping
    every other field intact.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

from ..core.logging import get_logger

_log = get_logger(__name__)

ROUTINE_FIRE_URL = "https://api.anthropic.com/v1/claude_code/routines/{routine_id}/fire"
_ROUTINE_BETA_HEADER = "experimental-cc-routine-2026-04-01"
_ANTHROPIC_API_VERSION = "2023-06-01"
_MAX_TEXT_BYTES = 60_000


@dataclass(frozen=True)
class RoutineFireResult:
    """Outcome of a single routine-fire call."""

    ok: bool
    status_code: int
    session_id: str | None
    session_url: str | None
    error: str | None


def fire_review_routine(
    *,
    routine_id: str,
    token: str,
    payload: dict,
    timeout_s: float = 30.0,
) -> RoutineFireResult:
    """POST the review outcome to the routine fire endpoint.

    Args:
        routine_id: The routine UUID copied from claude.ai/code/routines.
        token: Per-routine bearer token issued by claude.ai/code/routines.
        payload: TeamOutcome dict (will be json-serialised into the
            ``text`` field, capped at ~60KB so we stay under the API's
            65KB hard limit).
        timeout_s: HTTP timeout for the call.

    Returns:
        A :class:`RoutineFireResult`.  Success is determined by HTTP
        2xx; on failure the body is captured in ``error`` for logging.
    """
    if not routine_id or not token:
        return RoutineFireResult(
            ok=False,
            status_code=0,
            session_id=None,
            session_url=None,
            error="missing routine_id or token",
        )

    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text.encode("utf-8")) > _MAX_TEXT_BYTES:
        slim = {
            **{k: v for k, v in payload.items() if k != "reviews"},
            "reviews": [
                {**{k: v for k, v in r.items() if k != "comments"}}
                for r in payload.get("reviews", [])
            ],
            "_note": "reviews[].comments dropped (payload exceeded 60KB cap)",
        }
        text = json.dumps(slim, ensure_ascii=False, default=str)

    url = ROUTINE_FIRE_URL.format(routine_id=routine_id)
    try:
        resp = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": _ROUTINE_BETA_HEADER,
                "anthropic-version": _ANTHROPIC_API_VERSION,
                "Content-Type": "application/json",
            },
            json={"text": text},
            timeout=timeout_s,
        )
    except httpx.HTTPError as exc:
        _log.warning(
            "routine_fire.http_error",
            routine_id=routine_id,
            error=type(exc).__name__,
        )
        return RoutineFireResult(
            ok=False,
            status_code=0,
            session_id=None,
            session_url=None,
            error=str(exc)[:200],
        )

    if resp.is_success:
        try:
            data = resp.json()
        except ValueError:
            _log.debug("routine_fire.response_not_json", status_code=resp.status_code)
            data = {}
        return RoutineFireResult(
            ok=True,
            status_code=resp.status_code,
            session_id=data.get("claude_code_session_id"),
            session_url=data.get("claude_code_session_url"),
            error=None,
        )

    body_preview = resp.text[:300]
    _log.warning(
        "routine_fire.api_error",
        routine_id=routine_id,
        status_code=resp.status_code,
        body=body_preview,
    )
    return RoutineFireResult(
        ok=False,
        status_code=resp.status_code,
        session_id=None,
        session_url=None,
        error=body_preview,
    )
