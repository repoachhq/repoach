"""Pure audit of chain-head thinking classes (SP-CHAINS-THINKING-CLASS).

Reports every chain whose head model is classified ``reasoner`` or
``unknown``, plus malformed chains with empty model lists.  The caller
(CLI or chainpilot) prints the findings; this module is a pure leaf
with no I/O and no side effects.
"""

from __future__ import annotations

from collections.abc import Mapping

from ferova.llm_proxy.providers.catalog import classify_thinking


def audit_chain_thinking(chains: Mapping[str, list[str]]) -> list[str]:
    """Return one finding line per chain whose head violates rule #1.

    Rule #1 of ``chains.env``: the chain head must be a ``non_thinking``
    model.  A head classified ``reasoner`` or ``unknown`` is reported.
    Chains whose model list is empty are reported as malformed.

    Args:
        chains: Mapping of chain name to an ordered list of model ids
            (as parsed from ``chains.env``).

    Returns:
        A list of human-readable finding strings, one per violation.
        Empty when every head is ``non_thinking`` or ``hybrid`` with a
        known class.
    """
    findings: list[str] = []

    for chain_name, model_list in chains.items():
        if not model_list:
            findings.append(f"chain {chain_name!r}: malformed (empty model list)")
            continue

        head = model_list[0]
        thinking_class = classify_thinking(head)

        if thinking_class in ("reasoner", "unknown"):
            findings.append(
                f"chain {chain_name!r}: head model {head!r} classified {thinking_class}"
            )

    return findings
