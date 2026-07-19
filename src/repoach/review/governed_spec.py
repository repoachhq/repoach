"""Read one spec's governed frontmatter for the review pipeline.

`SP-ARCH-REVIEW-WIRE` (slice A). A thin accessor over the `SP-ARCH-GRAPH`
deriver: it loads the spec corpus once via :func:`repoach.arch.load_registry`
and returns the declared ``owns`` / ``depends_on`` for a single spec, so
the Architect reviewer can reason about tier-2 couplings (the ones the
edge-honesty gate cannot prove) against what the spec actually declares.

Reusing the deriver keeps a single frontmatter parser — there is no second
YAML reader here. A legacy/frontier spec (no frontmatter) resolves to
``None`` so the caller falls back to spec-unaware behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from repoach.arch import load_registry

from .spec import SPECS_DIR, _normalise_id


@dataclass(frozen=True)
class GovernedSpec:
    """The governed frontmatter of one spec, for review-side reasoning.

    Attributes:
        id: The SP-ID.
        owns_code: Owned code paths.
        owns_resources: Owned resource keys (``db:table:`` / ``queue:topic:``
            / ``format:``).
        depends_on: Declared architecture edges, by SP-ID.
    """

    id: str
    owns_code: tuple[str, ...]
    owns_resources: tuple[str, ...]
    depends_on: tuple[str, ...]


def load_governed_spec(spec_id: str, *, root: Path | None = None) -> GovernedSpec | None:
    """Return the governed frontmatter for *spec_id*, or ``None``.

    Args:
        spec_id: Any spelling; normalised like :func:`repoach.review.spec.load_spec`.
        root: Optional repository root override (tests).

    Returns:
        A :class:`GovernedSpec` when a governed node with that id exists and
        carries frontmatter; ``None`` for a frontier (frontmatter-less) or
        unknown spec.

    Raises:
        MalformedFrontmatterError: A governed spec in the corpus is
            malformed — surfaced loudly, matching the CI gate's stance.
    """
    canonical = _normalise_id(spec_id)
    specs_dir = (root or Path.cwd()) / SPECS_DIR
    registry = load_registry(specs_dir)
    node = registry.nodes.get(canonical)
    if node is None or node.frontier:
        return None
    return GovernedSpec(
        id=node.id,
        owns_code=node.owns_code,
        owns_resources=node.owns_resources,
        depends_on=node.depends_on,
    )


def render_arch_edges(spec: GovernedSpec | None) -> str:
    """Render the ``{ARCH_EDGES}`` block for the Architect prompt.

    Args:
        spec: The governed spec, or ``None`` for a frontier/no-spec PR.

    Returns:
        A markdown block naming the declared dependencies and ownership, or
        an empty string when there is no governed spec — in which case the
        Architect reviews exactly as it did before this wiring.
    """
    if spec is None:
        return ""
    if spec.depends_on:
        edges = ", ".join(spec.depends_on)
        declared = f"This component ({spec.id}) declares dependencies on: {edges}."
    else:
        declared = (
            f"This component ({spec.id}) declares NO dependencies — flag ANY "
            "cross-component coupling the diff introduces."
        )
    owns_code = ", ".join(spec.owns_code) if spec.owns_code else "(none)"
    owns_resources = ", ".join(spec.owns_resources) if spec.owns_resources else "(none)"
    return (
        "## Architecture edges (declared)\n\n"
        f"{declared}\n\n"
        f"- Owns code: {owns_code}\n"
        f"- Owns resources: {owns_resources}\n\n"
        'Tier-1 couplings (intra-repo imports, SQLAlchemy `Table("name")` '
        "literals) are already enforced by the CI edge-honesty gate — do NOT "
        "re-flag them. YOUR job is tier-2: flag any coupling this diff "
        "introduces that the gate cannot prove — a queue topic built at "
        "runtime, a raw-SQL table access, a dynamic/late import, a "
        "cross-component call — when its owner is NOT in the declared "
        "dependencies above."
    )


def render_owns_brief(spec: GovernedSpec | None) -> str:
    """Render the authoring architecture contract for the Developer brief.

    Unlike :func:`render_arch_edges` (a *review* brief), this is an
    *authoring* constraint: it tells the Developer which components it may
    import and which paths it owns, so the code it writes passes the CI
    edge-honesty gate on the first try.

    Args:
        spec: The governed spec, or ``None`` for a frontier/no-spec run.

    Returns:
        A markdown block stating owned paths + allowed dependencies, or an
        empty string when there is no governed spec — in which case the
        step brief is unchanged.
    """
    if spec is None:
        return ""
    owns_code = ", ".join(f"`{path}`" for path in spec.owns_code) if spec.owns_code else "(none)"
    if spec.depends_on:
        allowed = ", ".join(spec.depends_on)
        deps = (
            f"This component ({spec.id}) may import only these governed "
            f"components: {allowed}. Importing any OTHER governed component "
            "fails the CI edge-honesty gate and bounces the PR."
        )
    else:
        deps = (
            f"This component ({spec.id}) declares NO dependencies — do not "
            "import another governed component, or the CI edge-honesty gate "
            "fails the PR. Declare a new dependency in the spec frontmatter "
            "only if the design truly requires it."
        )
    return f"## Architecture contract (SP-ARCH-DEV-WIRE)\n\n- Owned paths: {owns_code}\n\n{deps}"
