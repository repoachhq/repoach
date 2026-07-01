"""Reasoning-effort wire fragment + single-pass probe value (SP-CHAINPILOT-EFFORT-KNOB).

Phase 2a-3-i of the Chain Autopilot arc (`docs/chain_autopilot_architecture.md`),
the first of four segments closing the ``reasoning_effort`` deferral from 0b-3.

``plan_reasoning`` (``providers/reasoning.py``) already decides that an
``EFFORT`` provider (groq, cerebras, deepseek) should request its ``min_effort``,
but the value is never turned into a request field: the generic transport omits
``plan.effort`` and the probe sweep runs with no reasoning knob. This module is
the one source of the effort wire format. Both downstream consumers land
``reasoning_effort`` at the **top level** of the request JSON but through
different in-memory shapes — the probe merges its ``extra_body=`` argument into
the POST body, the transport splices the openai-python SDK ``extra_body`` kwarg —
so a flat fragment serves both and each consumer owns its own merge.

The single-pass policy (one probe per cell at the provider's declared
``min_effort``) lives here as a thin resolver over
:data:`~ferova.llm_proxy.providers.reasoning.REASONING_CONTROLS`, keeping the
sweep policy-free; a future multi-pass slice changes one function.
"""

from __future__ import annotations

from .reasoning import REASONING_CONTROLS, KnobType, ReasoningControl

REASONING_EFFORT_FIELD = "reasoning_effort"
"""The top-level OpenAI-compatible request field that carries the effort level."""

_NONE_CONTROL = ReasoningControl(KnobType.NONE)


def _effort_control(provider_id: str) -> ReasoningControl | None:
    """Return *provider_id*'s control when it exposes an ``EFFORT`` knob.

    Args:
        provider_id: The catalog provider id (e.g. ``"groq"``).

    Returns:
        The :class:`ReasoningControl` when the provider's knob is
        :attr:`KnobType.EFFORT`, else ``None`` (an unknown provider resolves to
        the conservative ``NONE`` control and yields ``None``).
    """
    control = REASONING_CONTROLS.get(provider_id, _NONE_CONTROL)
    return control if control.knob is KnobType.EFFORT else None


def effort_extra_body(provider_id: str, effort: str | None) -> dict[str, str]:
    """Build the flat wire fragment carrying ``reasoning_effort``.

    The empty dict is the merge identity, so ``{**body, **fragment}`` is always
    safe regardless of provider. A fresh dict is returned on every call.

    Args:
        provider_id: The catalog provider id; resolved against
            :data:`REASONING_CONTROLS`.
        effort: The effort level to encode (e.g. ``"low"``), or ``None``.

    Returns:
        ``{"reasoning_effort": effort}`` when *provider_id* exposes an ``EFFORT``
        knob and *effort* is a non-empty string; ``{}`` otherwise (non-``EFFORT``
        provider, unknown provider, or empty/``None`` *effort*).
    """
    if _effort_control(provider_id) is None or not effort:
        return {}
    return {REASONING_EFFORT_FIELD: effort}


def probe_effort_for(provider_id: str) -> str | None:
    """Return the single-pass effort value to probe *provider_id*'s cells with.

    The single-pass policy for this arc: one probe per cell at the provider's
    declared ``min_effort``.

    Args:
        provider_id: The catalog provider id; resolved against
            :data:`REASONING_CONTROLS`.

    Returns:
        ``control.min_effort`` for an ``EFFORT`` provider, else ``None`` (no
        effort knob to probe — the sweep then probes those cells with no effort
        fragment).
    """
    control = _effort_control(provider_id)
    return control.min_effort if control is not None else None
