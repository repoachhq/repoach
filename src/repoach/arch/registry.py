"""Spec registry — parse governed spec frontmatter into an ownership map.

Loads every spec under a directory into a :class:`Registry`, the
spec-id-to-artifact map the architecture graph and the edge-honesty gate
resolve edges through (`SP-ARCH-GRAPH`). A spec with no frontmatter fence
is a "frontier" node — legacy, un-owned — tolerated rather than an error.
Ownership is disjoint: a code path or a resource belongs to at most one
spec, and overlaps are surfaced by :meth:`Registry.disjointness_violations`
rather than resolved silently.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from repoach.arch.graph import Graph

_RESOURCE_PREFIXES = ("db:table:", "queue:topic:", "format:")
_LEGACY_CODE_PREFIX = "src/ferova/"
_CODE_PREFIX = "src/repoach/"
_REQUIRED_KEYS = ("id", "title", "version", "status", "owns", "depends_on")


class MalformedFrontmatterError(ValueError):
    """A governed spec opens a frontmatter fence but its content is invalid."""


class OwnershipConflictError(ValueError):
    """Two or more specs claim the same artifact — disjointness is broken."""


@dataclass(frozen=True)
class OwnershipConflict:
    """Two or more specs declare ownership of the same artifact.

    Attributes:
        artifact: The code path or resource key owned more than once.
        spec_ids: The conflicting spec-ids, sorted.
    """

    artifact: str
    spec_ids: tuple[str, ...]


@dataclass(frozen=True)
class SpecNode:
    """One spec parsed from its frontmatter, or a frontier placeholder.

    Attributes:
        id: The SP-ID (frontmatter ``id``, or parsed from the filename for
            a frontier node).
        path: The spec file.
        owns_code: Owned code/artifact paths.
        owns_resources: Owned resource keys (``db:table:`` / ``queue:topic:``
            / ``format:``).
        depends_on: Declared architecture edges, by SP-ID.
        frontier: True when the file carried no frontmatter (un-governed).
    """

    id: str
    path: Path
    owns_code: tuple[str, ...] = ()
    owns_resources: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    frontier: bool = False


def _split_frontmatter(text: str) -> str | None:
    """Return the YAML frontmatter block, or ``None`` when there is none.

    The opening and closing fences are lines that are exactly ``---``; a
    ``# --- ... ---`` guidance comment inside the block is not a fence. A
    file that does not open with a fence, or opens one it never closes, is
    treated as having no frontmatter (a frontier node) rather than as an
    error — this tolerates legacy markdown.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[1:index])
    return None


def has_frontmatter(text: str) -> bool:
    """Return whether *text* opens with a closed ``---`` frontmatter fence.

    The single frontmatter authority — the spec-presence gate
    (`SP-ARCH-SPEC-PRESENCE`) reuses this rather than re-implementing fence
    detection, so a governed spec and the gate agree on what "has
    frontmatter" means.
    """
    return _split_frontmatter(text) is not None


def _as_tuple(value: object) -> tuple[str, ...]:
    """Normalise a frontmatter list field to a tuple of strings.

    ``None`` and the ``N/A`` sentinel both mean "empty"; a bare scalar is
    wrapped into a one-element tuple.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        if value.strip().upper() == "N/A":
            return ()
        return (value,)
    if isinstance(value, Iterable):
        return tuple(str(item) for item in value)
    return ()


def _normalise_owned_code(value: object) -> tuple[str, ...]:
    """Coerce ``owns.code`` entries, mapping the pre-rename package prefix.

    Governed specs written before the ferova→repoach rename own paths
    under the legacy ``src/ferova/`` prefix; the artifacts they govern
    now live under ``src/repoach/``. Rewriting at load time keeps the
    historical spec
    documents untouched while the ownership map keeps resolving against
    the real tree.
    """
    return tuple(
        _CODE_PREFIX + path[len(_LEGACY_CODE_PREFIX) :]
        if path.startswith(_LEGACY_CODE_PREFIX)
        else path
        for path in _as_tuple(value)
    )


def _frontier_id(path: Path) -> str:
    """Derive a frontier node's id from its ``<date>_<SP-ID>_<slug>`` name."""
    for part in path.stem.split("_"):
        if part.startswith("SP-"):
            return part
    return path.stem


def _parse_node(path: Path) -> SpecNode:
    """Parse one spec file into a :class:`SpecNode`.

    Args:
        path: The spec markdown file.

    Returns:
        A governed node when the file carries valid frontmatter, else a
        frontier node.

    Raises:
        MalformedFrontmatterError: The file opens a frontmatter fence but
            the YAML is invalid, not a mapping, missing a required key, or
            carries a malformed ``owns`` block.
    """
    block = _split_frontmatter(path.read_text(encoding="utf-8"))
    if block is None:
        return SpecNode(id=_frontier_id(path), path=path, frontier=True)
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError as exc:
        raise MalformedFrontmatterError(f"{path.name}: invalid YAML frontmatter: {exc}") from exc
    if not isinstance(data, dict):
        raise MalformedFrontmatterError(f"{path.name}: frontmatter must be a mapping")
    missing = [key for key in _REQUIRED_KEYS if key not in data]
    if missing:
        raise MalformedFrontmatterError(f"{path.name}: missing frontmatter keys: {missing}")
    owns = data["owns"]
    if not isinstance(owns, dict):
        raise MalformedFrontmatterError(f"{path.name}: owns must be a mapping of code/resources")
    return SpecNode(
        id=str(data["id"]),
        path=path,
        owns_code=_normalise_owned_code(owns.get("code")),
        owns_resources=_as_tuple(owns.get("resources")),
        depends_on=_as_tuple(data.get("depends_on")),
    )


def _path_contains(owned: str, artifact: str) -> bool:
    """Return whether an owned code path is the artifact or an ancestor of it."""
    base = owned.rstrip("/")
    return artifact == base or artifact.startswith(base + "/")


def load_registry(specs_dir: Path) -> Registry:
    """Parse every spec under ``specs_dir`` into a :class:`Registry`.

    Underscore-prefixed files (``_TEMPLATE.md``) are skipped. A duplicate
    ``id`` is rejected — the SP-ID must be globally unique.

    Args:
        specs_dir: Directory of spec markdown files.

    Returns:
        The parsed registry.

    Raises:
        MalformedFrontmatterError: A governed spec is malformed, or two
            specs share an ``id``.
    """
    nodes: dict[str, SpecNode] = {}
    for path in sorted(specs_dir.glob("*.md")):
        if path.name.startswith("_"):
            continue
        node = _parse_node(path)
        if node.id in nodes:
            raise MalformedFrontmatterError(
                f"{path.name}: duplicate spec id {node.id!r} (already declared by "
                f"{nodes[node.id].path.name})"
            )
        nodes[node.id] = node
    return Registry(nodes=nodes)


@dataclass(frozen=True)
class Registry:
    """An immutable id-to-:class:`SpecNode` map over a spec corpus."""

    nodes: Mapping[str, SpecNode]

    def owner_of(self, artifact: str) -> str | None:
        """Resolve an artifact to its owning spec-id, or ``None`` (frontier).

        A resource key (``db:table:`` / ``queue:topic:`` / ``format:``) is
        matched exactly against ``owns_resources``; any other artifact is a
        code path, resolved by longest owned-prefix match.

        Args:
            artifact: A code path or a resource key.

        Returns:
            The owning spec-id, or ``None`` when no spec owns it.
        """
        if artifact.startswith(_RESOURCE_PREFIXES):
            for node in self.nodes.values():
                if artifact in node.owns_resources:
                    return node.id
            return None
        best_id: str | None = None
        best_len = -1
        for node in self.nodes.values():
            for owned in node.owns_code:
                base = owned.rstrip("/")
                if _path_contains(owned, artifact) and len(base) > best_len:
                    best_id = node.id
                    best_len = len(base)
        return best_id

    def disjointness_violations(self) -> list[OwnershipConflict]:
        """Return every cross-spec ownership overlap (resource or code path)."""
        conflicts: list[OwnershipConflict] = []
        resource_owners: dict[str, list[str]] = {}
        for node in self.nodes.values():
            for resource in node.owns_resources:
                resource_owners.setdefault(resource, []).append(node.id)
        for resource, owners in sorted(resource_owners.items()):
            if len(owners) > 1:
                conflicts.append(
                    OwnershipConflict(artifact=resource, spec_ids=tuple(sorted(owners)))
                )
        owned_paths = [
            (node.id, path.rstrip("/")) for node in self.nodes.values() for path in node.owns_code
        ]
        for outer in range(len(owned_paths)):
            for inner in range(outer + 1, len(owned_paths)):
                id_a, path_a = owned_paths[outer]
                id_b, path_b = owned_paths[inner]
                if id_a == id_b:
                    continue
                if _path_contains(path_a, path_b) or _path_contains(path_b, path_a):
                    deeper = path_a if len(path_a) >= len(path_b) else path_b
                    conflicts.append(
                        OwnershipConflict(artifact=deeper, spec_ids=tuple(sorted((id_a, id_b))))
                    )
        return conflicts

    def assert_disjoint(self) -> None:
        """Raise if any artifact is owned by more than one spec.

        Raises:
            OwnershipConflictError: Naming each conflicting artifact and the
                specs that claim it. The non-raising
                :meth:`disjointness_violations` is for rendering/reporting.
        """
        conflicts = self.disjointness_violations()
        if conflicts:
            detail = "; ".join(
                f"{conflict.artifact} owned by {list(conflict.spec_ids)}" for conflict in conflicts
            )
            raise OwnershipConflictError(detail)

    def graph(self) -> Graph:
        """Build the directed ``id -> depends_on`` graph."""
        return Graph(edges={nid: frozenset(node.depends_on) for nid, node in self.nodes.items()})

    def frontier_ids(self) -> tuple[str, ...]:
        """Return the ids of un-governed (frontier) specs, sorted."""
        return tuple(sorted(nid for nid, node in self.nodes.items() if node.frontier))

    def dangling_edges(self) -> list[tuple[str, str]]:
        """Return ``(source, target)`` edges whose target id is unknown."""
        return sorted(
            (nid, dep)
            for nid, node in self.nodes.items()
            for dep in node.depends_on
            if dep not in self.nodes
        )
