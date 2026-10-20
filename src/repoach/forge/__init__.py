"""Forge abstraction: one protocol, GitHub and GitLab implementations (SP-UNIVERSAL-FORGE).

Callers import the protocol models from here and obtain a forge with
:func:`select_forge`; nothing outside this package speaks ``gh`` or
``glab``.
"""

from __future__ import annotations

from .protocol import (
    ARCHIVE_MARKER,
    BranchProtection,
    CheckRun,
    CheckStatus,
    Comment,
    Diff,
    Forge,
    ForgeError,
    ForgeForbiddenError,
    ForgeMergeRefusedError,
    ForgeNotFoundError,
    ForgeStateRefusedError,
    ForgeUnavailableError,
    InlineCommentDraft,
    MergeMethod,
    MergeResult,
    PullRequest,
    Review,
    ReviewDraft,
)
from .runner import CommandResult
from .select import merge_preferences, select_forge

__all__ = [
    "ARCHIVE_MARKER",
    "BranchProtection",
    "CheckRun",
    "CheckStatus",
    "CommandResult",
    "Comment",
    "Diff",
    "Forge",
    "ForgeError",
    "ForgeForbiddenError",
    "ForgeMergeRefusedError",
    "ForgeNotFoundError",
    "ForgeStateRefusedError",
    "ForgeUnavailableError",
    "InlineCommentDraft",
    "MergeMethod",
    "MergeResult",
    "PullRequest",
    "Review",
    "ReviewDraft",
    "merge_preferences",
    "select_forge",
]
