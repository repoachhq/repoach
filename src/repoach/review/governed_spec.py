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

import subprocess
from dataclasses import dataclass
from pathlib import Path

from repoach.arch import MalformedFrontmatterError, SpecNode, load_registry, parse_spec_text
from repoach.core.logging import get_logger

from .spec import _SPEC_ID_IN_FILENAME_RE, _normalise_id, detect_spec_from_branch, specs_dir_for

_log = get_logger(__name__)


@dataclass(frozen=True)
class GovernedSpec:
    """The governed frontmatter of one spec, for review-side reasoning.

    Attributes:
        id: The SP-ID.
        owns_code: Owned code paths.
        owns_resources: Owned resource keys (``db:table:`` / ``queue:topic:``
            / ``format:``).
        depends_on: Declared architecture edges, by SP-ID.
        status: The frontmatter ``status``, lower-cased.
        path: The spec file the frontmatter was read from.
    """

    id: str
    owns_code: tuple[str, ...]
    owns_resources: tuple[str, ...]
    depends_on: tuple[str, ...]
    status: str = ""
    path: Path | None = None


class SpecRefUnavailableError(RuntimeError):
    """A git ref does not resolve to a commit, so its specs cannot be read."""


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
    specs_dir = specs_dir_for(root)
    registry = load_registry(specs_dir)
    return _governed(registry.nodes.get(canonical))


def _governed(node: SpecNode | None) -> GovernedSpec | None:
    """Project a registry node onto :class:`GovernedSpec` (``None`` for frontier)."""
    if node is None or node.frontier:
        return None
    return GovernedSpec(
        id=node.id,
        owns_code=node.owns_code,
        owns_resources=node.owns_resources,
        depends_on=node.depends_on,
        status=node.status,
        path=node.path,
    )


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one read-only git command under *root*."""
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)


def load_governed_spec_at_ref(spec_id: str, ref: str, *, root: Path) -> GovernedSpec | None:
    """Return *spec_id*'s governed frontmatter as committed at *ref*, or ``None``.

    Reads the spec directory of *ref* with ``git ls-tree`` / ``git show``
    — no checkout — and parses the matching file with the registry's own
    frontmatter parser, so the base-ref view of a spec follows the same
    rules as the head view. The match is by the ``_<SP-ID>_`` filename
    segment, the rule :func:`repoach.review.spec.load_spec` uses; the
    last match in sort order wins.

    Args:
        spec_id: Any spelling; normalised like :func:`load_governed_spec`.
        ref: A git revision (branch, remote-tracking ref or commit).
        root: Repository root the git commands run against.

    Returns:
        The governed frontmatter at *ref*; ``None`` when *ref* carries no
        spec file with that id, or carries it without frontmatter.

    Raises:
        SpecRefUnavailableError: *ref* does not resolve to a commit.
        MalformedFrontmatterError: The file at *ref* is malformed.
    """
    base = root.resolve()
    canonical = _normalise_id(spec_id)
    matches = sorted(name for name, found in _spec_files_at_ref(ref, base) if found == canonical)
    if not matches:
        return None
    shown = _git(base, "show", f"{ref}:{matches[-1]}")
    if shown.returncode != 0:
        return None
    return _governed(parse_spec_text(shown.stdout, Path(matches[-1])))


def spec_markdown_at_ref(spec_id: str, ref: str, *, root: Path) -> str | None:
    """Return *spec_id*'s markdown as committed at *ref*, or ``None`` when absent.

    The file is matched as :func:`load_governed_spec_at_ref` matches it.

    Args:
        spec_id: Any spelling; normalised like :func:`load_governed_spec`.
        ref: A git revision (branch, remote-tracking ref or commit).
        root: Repository root the git commands run against.

    Returns:
        The markdown at *ref*; ``None`` when *ref* carries no spec file
        with that id.

    Raises:
        SpecRefUnavailableError: *ref* does not resolve to a commit.
    """
    base = root.resolve()
    canonical = _normalise_id(spec_id)
    matches = sorted(name for name, found in _spec_files_at_ref(ref, base) if found == canonical)
    if not matches:
        return None
    shown = _git(base, "show", f"{ref}:{matches[-1]}")
    return shown.stdout if shown.returncode == 0 else None


def governed_specs_at_ref(ref: str, *, root: Path) -> list[GovernedSpec]:
    """Return every governed spec committed at *ref*, in path order.

    Frontier (frontmatter-less) and malformed specs are left out: a
    malformed spec owns nothing a PR can be graded against.

    Args:
        ref: A git revision (branch, remote-tracking ref or commit).
        root: Repository root the git commands run against.

    Returns:
        The governed frontmatter of each spec file at *ref*.

    Raises:
        SpecRefUnavailableError: *ref* does not resolve to a commit.
    """
    base = root.resolve()
    specs: list[GovernedSpec] = []
    for name, _ in sorted(_spec_files_at_ref(ref, base)):
        shown = _git(base, "show", f"{ref}:{name}")
        if shown.returncode != 0:
            continue
        try:
            governed = _governed(parse_spec_text(shown.stdout, Path(name)))
        except MalformedFrontmatterError as exc:
            _log.info("governed_spec.malformed_at_ref", ref=ref, path=name, error=str(exc)[:200])
            continue
        if governed is not None:
            specs.append(governed)
    return specs


def _spec_files_at_ref(ref: str, base: Path) -> list[tuple[str, str]]:
    """Return ``(path, filename SP-ID)`` for every spec file committed at *ref*.

    Raises:
        SpecRefUnavailableError: *ref* does not resolve to a commit.
    """
    if _git(base, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode != 0:
        raise SpecRefUnavailableError(f"ref {ref!r} does not resolve to a commit in {base}")
    specs_rel = specs_dir_for(base).resolve().relative_to(base).as_posix()
    listing = _git(base, "ls-tree", "--name-only", ref, f"{specs_rel}/")
    files: list[tuple[str, str]] = []
    for name in listing.stdout.splitlines():
        found = _SPEC_ID_IN_FILENAME_RE.search(Path(name).stem)
        if name.endswith(".md") and found is not None:
            files.append((name, found.group(1)))
    return files


def branch_spec_id_at_ref(branch: str, ref: str, *, root: Path) -> str | None:
    """Return the spec id *branch* names, resolved against the specs committed at *ref*.

    The checkout a command runs from may predate the PR's spec: a branch
    ``docs/sp-agent-context-loop`` matched against a checkout without
    ``SP-AGENT-CONTEXT-LOOP`` falls back to the short regex and yields
    ``SP-AGENT``. Matching against the ids at *ref* (the PR head) and
    returning the matched spec's frontmatter ``id`` gives the id the
    review itself graded.

    Args:
        branch: The PR head branch name.
        ref: The PR head commit.
        root: Repository root the git commands run against.

    Returns:
        The frontmatter ``id`` of the spec the branch names at *ref*;
        ``None`` when no spec at *ref* matches the branch.

    Raises:
        SpecRefUnavailableError: *ref* does not resolve to a commit.
        MalformedFrontmatterError: The matched spec's frontmatter is malformed.
    """
    base = root.resolve()
    known = tuple({found for _, found in _spec_files_at_ref(ref, base)})
    detected = detect_spec_from_branch(branch, known_ids=known)
    if detected is None or detected not in known:
        return None
    governed = load_governed_spec_at_ref(detected, ref, root=base)
    return governed.id if governed is not None else detected


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
