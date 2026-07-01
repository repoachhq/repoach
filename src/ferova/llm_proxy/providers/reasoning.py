"""Per-provider reasoning-control policy (SP-CHAINPILOT-REASONING-CONTROL).

The headroom framework for Phase 0b of the Chain Autopilot arc
(`docs/chain_autopilot_architecture.md`). A reasoning model must leave room
for its visible answer; *how* to guarantee that differs per provider. A
live, doc-verified audit (2026-06-22) of all six providers established the
matrix below — and that only NIM and OpenRouter expose a reasoning-*token*
budget. The four direct OpenAI-compatible providers share ``max_tokens``
between reasoning and answer and can only toggle thinking off or lower a
coarse effort level; they rely on a generous budget plus the existing
budget-retry (`api/_failover.py` / `api/services.py`).

| Provider     | Knob                                   | max_tokens shared? |
|--------------|----------------------------------------|--------------------|
| nvidia_nim   | token budget (chat_template_kwargs)    | no (separate)      |
| open_router  | token budget (reasoning.max_tokens)    | no                 |
| kimi         | toggle (thinking.type)                 | yes                |
| groq         | effort (reasoning_effort)              | yes                |
| cerebras     | effort (reasoning_effort)              | yes                |
| deepseek     | toggle + effort (high/max only)        | yes                |

This module is a pure policy: it returns an abstract :class:`ReasoningPlan`
that the transports translate into their own wire fields (0b-2/0b-3). It is
the single place the matrix lives, so a provider-API change is one edit.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

_DEFAULT_REASONING_BUDGET_CAP = 2048
_REASONING_BUDGET_FLOOR = 256
_COMBINED_ANSWER_HEADROOM_FLOOR = 4096
"""Minimum ``max_tokens`` to request from a combined-budget provider when
thinking is on.

These providers cannot fence reasoning from the answer, so we give the
request enough total room that a typical answer survives a moderate
chain-of-thought; the budget-retry escalates further if the stream still
comes back starved.
"""


def bounded_reasoning_budget(
    max_tokens: Any, *, cap: int = _DEFAULT_REASONING_BUDGET_CAP
) -> int | None:
    """Bound a reasoning-token budget so visible output is never starved.

    Half the completion budget, capped at *cap* and floored at 256. Lifted
    from the original NIM-only helper so every transport with a real
    token-budget knob reuses one definition.

    Args:
        max_tokens: The request's completion budget (may be unusable).
        cap: The hard ceiling on the reasoning budget.

    Returns:
        The bounded budget, or ``None`` when *max_tokens* is unusable (the
        caller then omits the field).
    """
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
        return None
    return max(_REASONING_BUDGET_FLOOR, min(cap, max_tokens // 2))


class KnobType(StrEnum):
    """The per-request reasoning lever a provider exposes.

    Attributes:
        TOKEN_BUDGET: a real reasoning-token cap (NIM, OpenRouter) — headroom
            is guaranteed because reasoning is bounded separately.
        EFFORT: a coarse effort level (groq, cerebras, deepseek) — shrinks but
            does not fence reasoning.
        TOGGLE: on/off only (kimi) — no shrinking lever.
        NONE: no per-request control (unknown provider — conservative default).
    """

    TOKEN_BUDGET = "token_budget"
    EFFORT = "effort"
    TOGGLE = "toggle"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class ReasoningControl:
    """How one provider lets a request shape reasoning.

    Attributes:
        knob: The lever kind.
        min_effort: For ``EFFORT`` providers, the lowest value that still
            keeps reasoning useful while leaving answer room (provider
            dependent: ``"low"`` for groq/cerebras, ``"high"`` for deepseek
            whose floor is high).
        combined_budget: ``True`` when ``max_tokens`` is shared between
            reasoning and answer — the provider cannot reserve answer
            headroom, so a floor + the budget-retry carry it.
    """

    knob: KnobType
    min_effort: str | None = None
    combined_budget: bool = True


REASONING_CONTROLS: dict[str, ReasoningControl] = {
    "nvidia_nim": ReasoningControl(KnobType.TOKEN_BUDGET, combined_budget=False),
    "open_router": ReasoningControl(KnobType.TOKEN_BUDGET, combined_budget=False),
    "kimi": ReasoningControl(KnobType.TOGGLE),
    "groq": ReasoningControl(KnobType.EFFORT, min_effort="low"),
    "cerebras": ReasoningControl(KnobType.EFFORT, min_effort="low"),
    "deepseek": ReasoningControl(KnobType.EFFORT, min_effort="high"),
}

_NONE_CONTROL = ReasoningControl(KnobType.NONE)


@dataclass(frozen=True, slots=True)
class ReasoningPlan:
    """The abstract reasoning decision a transport then translates to wire fields.

    Attributes:
        token_budget: The reasoning-token cap to apply, for ``TOKEN_BUDGET``
            providers; ``None`` otherwise or when ``max_tokens`` was unusable.
        effort: The effort level to request, for ``EFFORT`` providers; ``None``
            otherwise.
        thinking_enabled: Whether the transport should leave reasoning on. When
            ``False`` the transport disables it however its API allows.
        answer_headroom_floor: A minimum ``max_tokens`` to request so the answer
            survives on a combined-budget provider; ``None`` when the provider
            fences reasoning itself or ``max_tokens`` is unusable.
    """

    token_budget: int | None = None
    effort: str | None = None
    thinking_enabled: bool = True
    answer_headroom_floor: int | None = None


def plan_reasoning(
    provider_id: str, *, max_tokens: int | None, thinking_enabled: bool
) -> ReasoningPlan:
    """Return the reasoning plan for a request to *provider_id*.

    Pure policy over :data:`REASONING_CONTROLS`. An unknown provider resolves
    to the conservative ``NONE`` control, yielding a plan that applies no
    reasoning fields.

    Args:
        provider_id: The catalog provider id (e.g. ``"nvidia_nim"``).
        max_tokens: The request's completion budget, or ``None``.
        thinking_enabled: Whether reasoning is wanted for this request.

    Returns:
        A :class:`ReasoningPlan`. With ``thinking_enabled=False`` the plan
        carries no budget/effort and signals the transport to disable
        reasoning.
    """
    control = REASONING_CONTROLS.get(provider_id, _NONE_CONTROL)

    if not thinking_enabled:
        return ReasoningPlan(thinking_enabled=False)

    floor = (
        _COMBINED_ANSWER_HEADROOM_FLOOR
        if control.combined_budget and control.knob is not KnobType.NONE
        else None
    )

    if control.knob is KnobType.TOKEN_BUDGET:
        return ReasoningPlan(token_budget=bounded_reasoning_budget(max_tokens))
    if control.knob is KnobType.EFFORT:
        return ReasoningPlan(effort=control.min_effort, answer_headroom_floor=floor)
    if control.knob is KnobType.TOGGLE:
        return ReasoningPlan(answer_headroom_floor=floor)
    return ReasoningPlan()
