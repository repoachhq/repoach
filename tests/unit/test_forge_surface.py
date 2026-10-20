"""The public surface of both forges is the ``Forge`` protocol, nothing more.

The ``gh`` / ``glab`` transport helpers are private, so no caller can
reach a write that skips :func:`~repoach.forge.protocol.write_barrier`
(the historical ``GhCli`` alias is :class:`GitHubForge` and has the same
surface).
"""

from __future__ import annotations

import inspect

import pytest

from repoach.forge.github import GhCli, GitHubForge
from repoach.forge.gitlab import GitLabForge
from repoach.forge.protocol import Forge


def _public(cls: type) -> set[str]:
    return {name for name, _ in inspect.getmembers(cls) if not name.startswith("_")}


PROTOCOL = _public(Forge) | set(Forge.__annotations__)


@pytest.mark.parametrize("cls", [GitHubForge, GitLabForge, GhCli])
def test_public_surface_is_the_protocol(cls: type) -> None:
    assert _public(cls) == PROTOCOL | {"create_pull_request"}


@pytest.mark.parametrize("cls", [GitHubForge, GitLabForge])
def test_every_write_goes_through_the_barrier(cls: type) -> None:
    for name in ("post_comment", "post_review", "reply", "upsert_marked_comment", "merge"):
        assert getattr(getattr(cls, name), "__wrapped__", None) is not None, name
