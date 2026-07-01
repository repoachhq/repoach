"""Architecture layer — derive the system dependency graph from specs.

The deriver (`SP-ARCH-GRAPH`) parses governed spec frontmatter into a
:class:`Registry` (the spec-id-to-artifact ownership map), builds the
:class:`Graph` over ``depends_on``, and renders it. It reads spec files
only — no code, no database, no network — and is the resolution core the
edge-honesty gate (`SP-ARCH-EDGE-GATE`) reuses via :meth:`Registry.owner_of`.
"""

from __future__ import annotations

from ferova.arch.graph import Graph
from ferova.arch.registry import (
    MalformedFrontmatterError,
    OwnershipConflict,
    OwnershipConflictError,
    Registry,
    SpecNode,
    has_frontmatter,
    load_registry,
)
from ferova.arch.render import render

__all__ = [
    "Graph",
    "MalformedFrontmatterError",
    "OwnershipConflict",
    "OwnershipConflictError",
    "Registry",
    "SpecNode",
    "has_frontmatter",
    "load_registry",
    "render",
]
