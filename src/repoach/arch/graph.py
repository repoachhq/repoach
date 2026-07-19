"""The derived dependency graph and its cycle detector.

A :class:`Graph` is the directed ``spec-id -> {depends_on}`` map built by
:meth:`repoach.arch.registry.Registry.graph`. Cycle detection only
follows edges whose target is a known node, so a dangling ``depends_on``
(reported separately as a warning) never fabricates a cycle.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Graph:
    """An immutable directed graph of spec-ids over their ``depends_on``."""

    edges: Mapping[str, frozenset[str]]

    def cycles(self) -> list[list[str]]:
        """Return every dependency cycle as an ordered list of spec-ids.

        Uses a depth-first three-colour walk; on revisiting a node still on
        the active path, the path suffix from that node is recorded as a
        cycle. Each distinct cycle is returned once, normalised to start at
        its lexicographically smallest member.

        Returns:
            The cycles found, each a list of ids; empty when acyclic.
        """
        active: list[str] = []
        on_path: set[str] = set()
        visited: set[str] = set()
        found: set[tuple[str, ...]] = set()

        def walk(node: str) -> None:
            visited.add(node)
            on_path.add(node)
            active.append(node)
            for target in sorted(self.edges.get(node, frozenset())):
                if target not in self.edges:
                    continue
                if target in on_path:
                    start = active.index(target)
                    found.add(_normalise(active[start:]))
                elif target not in visited:
                    walk(target)
            on_path.discard(node)
            active.pop()

        for node in sorted(self.edges):
            if node not in visited:
                walk(node)
        return [list(cycle) for cycle in sorted(found)]


def _normalise(cycle: list[str]) -> tuple[str, ...]:
    """Rotate a cycle to start at its smallest id, for stable de-duplication."""
    pivot = cycle.index(min(cycle))
    return tuple(cycle[pivot:] + cycle[:pivot])
