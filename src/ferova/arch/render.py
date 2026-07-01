"""Render a :class:`~ferova.arch.registry.Registry` as a graph.

Three formats: ``mermaid`` (a flowchart for docs), ``json`` (the
machine-readable registry + edges + frontier/conflict/cycle report, for
the edge-honesty gate and tooling), and ``dot`` (Graphviz).
"""

from __future__ import annotations

import json

from ferova.arch.registry import Registry


def render(registry: Registry, fmt: str) -> str:
    """Render the registry in ``fmt``.

    Args:
        registry: The parsed spec registry.
        fmt: One of ``mermaid``, ``json``, ``dot``.

    Returns:
        The rendered graph.

    Raises:
        ValueError: ``fmt`` is not a supported format.
    """
    if fmt == "mermaid":
        return _mermaid(registry)
    if fmt == "json":
        return _json(registry)
    if fmt == "dot":
        return _dot(registry)
    raise ValueError(f"unsupported format {fmt!r}; expected mermaid, json or dot")


def _edges(registry: Registry) -> list[tuple[str, str]]:
    """Return the sorted ``(source, target)`` edges over known targets."""
    return sorted(
        (nid, dep)
        for nid, node in registry.nodes.items()
        for dep in node.depends_on
        if dep in registry.nodes
    )


def _mermaid(registry: Registry) -> str:
    lines = ["flowchart TD"]
    for nid in sorted(registry.nodes):
        shape = (
            f'{_safe(nid)}(["{nid}"])' if registry.nodes[nid].frontier else f'{_safe(nid)}["{nid}"]'
        )
        lines.append(f"    {shape}")
    for source, target in _edges(registry):
        lines.append(f"    {_safe(source)} --> {_safe(target)}")
    return "\n".join(lines)


def _dot(registry: Registry) -> str:
    lines = ["digraph architecture {"]
    for nid in sorted(registry.nodes):
        lines.append(f'    "{nid}";')
    for source, target in _edges(registry):
        lines.append(f'    "{source}" -> "{target}";')
    lines.append("}")
    return "\n".join(lines)


def _json(registry: Registry) -> str:
    payload = {
        "nodes": [
            {
                "id": node.id,
                "frontier": node.frontier,
                "owns_code": list(node.owns_code),
                "owns_resources": list(node.owns_resources),
                "depends_on": list(node.depends_on),
            }
            for _, node in sorted(registry.nodes.items())
        ],
        "edges": [{"from": source, "to": target} for source, target in _edges(registry)],
        "frontier": list(registry.frontier_ids()),
        "dangling": [
            {"from": source, "to": target} for source, target in registry.dangling_edges()
        ],
        "conflicts": [
            {"artifact": conflict.artifact, "spec_ids": list(conflict.spec_ids)}
            for conflict in registry.disjointness_violations()
        ],
        "cycles": registry.graph().cycles(),
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def _safe(node_id: str) -> str:
    """Make a spec-id usable as a mermaid/dot node identifier."""
    return node_id.replace("-", "_").replace(".", "_")
