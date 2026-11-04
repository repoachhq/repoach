"""The CLI's report of a base ref that does not resolve (Yoob B-221).

The five commands that resolve a base (``review pr``, ``review gate``,
``review generated`` and ``review fix`` in ``review_cmds``, ``release
gate`` in ``release_cmds``) all refuse through :func:`base_ref_refusal`,
so the message, the JSON and the exit code cannot drift between them.
The exit code is a CLI contract, so it lives here, not with the error in
:mod:`repoach.review.base_ref`.
"""

from __future__ import annotations

import json

import typer

from ..review.base_ref import BaseRefUnresolvableError

EXIT_BASE_UNRESOLVABLE = 10
"""The exit code of every command refused by :class:`BaseRefUnresolvableError`, before any work."""


def base_ref_refusal(
    exc: BaseRefUnresolvableError, *, prints_json: bool, pr_number: int | None = None
) -> typer.Exit:
    """Report a base that does not resolve and return the exit that refuses the command.

    The message goes to stderr. A command that prints JSON prints the keys
    ``error`` (``base_ref_unresolvable``), ``ref`` and ``hint`` on stdout,
    after ``pr_number`` when it has one.

    Args:
        exc: The refusal.
        prints_json: Whether the command prints JSON (``review generated`` does not).
        pr_number: The pull request the JSON leads with, ``None`` for none.

    Returns:
        The :class:`typer.Exit` with :data:`EXIT_BASE_UNRESOLVABLE` to raise.
    """
    typer.echo(f"repoach: {exc}", err=True)
    if prints_json:
        lead = {} if pr_number is None else {"pr_number": pr_number}
        typer.echo(json.dumps({**lead, **exc.as_json()}, indent=2, ensure_ascii=False))
    return typer.Exit(code=EXIT_BASE_UNRESOLVABLE)


__all__ = ["EXIT_BASE_UNRESOLVABLE", "base_ref_refusal"]
