"""Deprecated import path of :mod:`repoach.forge.github`, kept for one release.

SP-UNIVERSAL-FORGE moved the GitHub wrapper behind the ``Forge``
protocol. Attribute access on this module is forwarded to
:mod:`repoach.forge.github`; new code imports from :mod:`repoach.forge`.
"""

from __future__ import annotations

from importlib import import_module


def __getattr__(name: str) -> object:
    """Forward every attribute to :mod:`repoach.forge.github`."""
    return getattr(import_module("repoach.forge.github"), name)
