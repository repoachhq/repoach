"""The CLI's report of a base ref that does not resolve (Yoob B-221).

``review pr``, ``review gate``, ``review generated``, ``review fix`` and
``release gate`` all refuse a base that does not resolve through
:func:`base_ref_refusal`, so the message, the JSON and the exit code
cannot drift between the two command modules.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

import typer

from ..review.base_ref import BaseRefUnresolvableError

EXIT_BASE_UNRESOLVABLE = 10
"""The exit code of every command refused by :class:`BaseRefUnresolvableError`, before any work."""


def base_ref_refusal(
    exc: BaseRefUnresolvableError, *, payload: Mapping[str, object] | None
) -> typer.Exit:
    """Report a base that does not resolve and return the exit that refuses the command.

    The message goes to stderr. A command that prints JSON (``payload``
    given) prints ``payload`` merged with the keys ``error``
    (``base_ref_unresolvable``), ``ref`` and ``hint`` on stdout.

    Args:
        exc: The refusal.
        payload: The keys the command's JSON leads with (``pr_number``),
            ``None`` for a command printing no JSON.

    Returns:
        The :class:`typer.Exit` with :data:`EXIT_BASE_UNRESOLVABLE` to raise.
    """
    typer.echo(f"repoach: {exc}", err=True)
    if payload is not None:
        typer.echo(json.dumps({**payload, **exc.as_json()}, indent=2, ensure_ascii=False))
    return typer.Exit(code=EXIT_BASE_UNRESOLVABLE)


__all__ = ["EXIT_BASE_UNRESOLVABLE", "base_ref_refusal"]
