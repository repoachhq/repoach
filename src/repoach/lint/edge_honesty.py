"""Edge-honesty gate — declared depends_on must cover observed couplings.

`SP-ARCH-EDGE-GATE`. At diff time, for every changed Python file owned by a
governed spec, this gate resolves the couplings the change introduces and
requires each to be declared in that spec's ``depends_on``:

- **tier 1a (code, enforced):** an intra-repo import whose target is owned
  by another governed spec must be declared.
- **tier 1b (table, enforced, high-confidence):** a SQLAlchemy
  ``Table("name", ...)`` literal naming a table owned by another governed
  spec must be declared — the *back-channel* rule (a literal in the table's
  owner is fine; the violation is a literal in a non-owner that goes
  undeclared).

Couplings the gate cannot prove from the AST — runtime-built queue topics,
raw SQL, dynamic imports — are out of enforcement (tier 2), left to
Architect review. Imports/tables resolving to no governed owner are
"frontier": reported in one aggregated, non-blocking line, never a failure.

It consumes the `SP-ARCH-GRAPH` deriver's :class:`Registry` + ``owner_of``;
its frontmatter dependency is transitive through the deriver, so its only
declared edge is ``SP-ARCH-GRAPH``.

Language-aware (SP-UNIVERSAL-GATES): intra-repo Python imports are the
project's own top-level packages found under its declared ``src_roots``
(``repoach`` under ``src`` without a ``repoach.toml``), Python files are
checked only when the project declares Python, and TypeScript /
JavaScript files have their relative and ``tsconfig`` ``paths``-aliased
``import … from`` targets resolved to repo paths and checked the same
way (tier 1a). The spec-presence check is blocking only when
``[arch].require_frontmatter`` is on (always, without a ``repoach.toml``).
"""

from __future__ import annotations

import ast
import subprocess
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from repoach.arch import Registry, has_frontmatter, load_registry
from repoach.gates import typescript as ts
from repoach.gates.config import GatesConfig, applicable, load_gates_config, python_packages
from repoach.gates.languages import Language, language_of

_JS_LANGUAGES = frozenset({Language.TYPESCRIPT, Language.JAVASCRIPT})
"""Languages whose ``import … from`` couplings are resolved through tsconfig."""


@dataclass(frozen=True)
class EdgeViolation:
    """An observed coupling missing from the source spec's ``depends_on``.

    Attributes:
        source: The spec owning the changed file.
        target: The governed spec owning the imported module or table.
        kind: ``import`` (tier 1a) or ``table`` (tier 1b).
        detail: The imported module path or ``db:table:<name>`` key.
        file: The changed file the coupling was found in.
    """

    source: str
    target: str
    kind: str
    detail: str
    file: str


@dataclass(frozen=True)
class FrontierRef:
    """A coupling into ungoverned territory — reported, never enforced."""

    source: str
    artifact: str
    file: str


@dataclass(frozen=True)
class SpecPresenceViolation:
    """A newly-added spec that carries no frontmatter (SP-ARCH-SPEC-PRESENCE).

    Such a spec becomes an un-graphed frontier node silently; this is a
    blocking violation so a new component cannot escape the dependency
    graph unnoticed.
    """

    path: str


@dataclass(frozen=True)
class Report:
    """The outcome of an edge-honesty run over a diff."""

    violations: tuple[EdgeViolation, ...]
    frontier: tuple[FrontierRef, ...]
    spec_violations: tuple[SpecPresenceViolation, ...] = ()
    governed_specs: int | None = None

    @property
    def ok(self) -> bool:
        """True when no tier-1 edge or spec-presence violation was found."""
        return not self.violations and not self.spec_violations


def _module_candidates(module: str, base_rel: str = "src") -> tuple[str, ...]:
    """Map a dotted module to the repo paths it could resolve to.

    Args:
        module: Dotted module name.
        base_rel: Repo-relative directory its top package sits in
            (``""`` for the repository root).
    """
    rel = module.replace(".", "/")
    prefix = f"{base_rel}/" if base_rel else ""
    return (f"{prefix}{rel}.py", f"{prefix}{rel}/__init__.py")


def _import_owner(registry: Registry, module: str, base_rel: str = "src") -> str | None:
    """Resolve an intra-repo import to its owning spec-id, if any."""
    for candidate in _module_candidates(module, base_rel):
        owner = registry.owner_of(candidate)
        if owner is not None:
            return owner
    return None


def _is_table_call(func: ast.expr) -> bool:
    """Whether a call target is a SQLAlchemy ``Table`` constructor."""
    if isinstance(func, ast.Name):
        return func.id == "Table"
    if isinstance(func, ast.Attribute):
        return func.attr == "Table"
    return False


def _intra_repo_imports(tree: ast.AST, tops: frozenset[str] = frozenset({"repoach"})) -> set[str]:
    """Return the dotted modules of the project's own packages ``tops`` a parsed file imports."""
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
    return {module for module in modules if module.split(".")[0] in tops}


def _table_literals(tree: ast.AST) -> set[str]:
    """Return the table names in high-confidence ``Table("name", ...)`` calls."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_table_call(node.func) and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                names.add(first.value)
    return names


def _read_source(rel_path: str, *, staged: bool, repo_root: Path) -> str:
    """Return the content to analyze for one changed file.

    In staged mode this is the STAGED BLOB (``git show :<rel_path>``) — the
    bytes that will actually be committed — rather than the worktree copy,
    which may have been reverted or further edited after ``git add``.

    Args:
        rel_path: Repo-relative path, as listed by the ``--cached`` diff.
        staged: When true, read the staged blob; else the worktree copy.
        repo_root: The repository root.

    Returns:
        The file's text content.

    Raises:
        subprocess.CalledProcessError: ``git show`` failed for a path the
            ``--cached`` listing claimed was staged — surfaced rather than
            silently skipping the file.
    """
    if staged:
        completed = subprocess.run(
            ["git", "show", f":{rel_path}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        return completed.stdout
    return (repo_root / rel_path).read_text(encoding="utf-8")


def _package_bases(repo_root: Path, config: GatesConfig) -> dict[str, str]:
    """Return ``{top_package: repo-relative directory it sits in}``."""
    top = repo_root.resolve()
    out: dict[str, str] = {}
    for name, base in python_packages(repo_root, config).items():
        if not base.is_relative_to(top):
            continue
        relative = base.relative_to(top).as_posix()
        out[name] = "" if relative == "." else relative
    return out


def _check_file(
    registry: Registry,
    repo_root: Path,
    rel_path: str,
    *,
    staged: bool,
    bases: dict[str, str] | None = None,
) -> tuple[list[EdgeViolation], list[FrontierRef]]:
    """Check one changed Python file's couplings against its owner's ``depends_on``."""
    owner = registry.owner_of(rel_path)
    if owner is None or registry.nodes[owner].frontier:
        return [], []
    package_bases = bases if bases is not None else {"repoach": "src"}
    declared = set(registry.nodes[owner].depends_on)
    tree = ast.parse(_read_source(rel_path, staged=staged, repo_root=repo_root))

    violations: list[EdgeViolation] = []
    frontier: list[FrontierRef] = []

    for module in sorted(_intra_repo_imports(tree, frozenset(package_bases))):
        target = _import_owner(registry, module, package_bases[module.split(".")[0]])
        if target is None or registry.nodes[target].frontier:
            frontier.append(FrontierRef(owner, module, rel_path))
        elif target != owner and target not in declared:
            violations.append(EdgeViolation(owner, target, "import", module, rel_path))

    for table in sorted(_table_literals(tree)):
        resource = f"db:table:{table}"
        target = registry.owner_of(resource)
        if target is None:
            frontier.append(FrontierRef(owner, resource, rel_path))
        elif target != owner and target not in declared:
            violations.append(EdgeViolation(owner, target, "table", resource, rel_path))

    return violations, frontier


def _check_js_file(
    registry: Registry, repo_root: Path, rel_path: str, *, staged: bool
) -> tuple[list[EdgeViolation], list[FrontierRef]]:
    """Check one changed TypeScript/JavaScript file's resolved imports (tier 1a).

    Relative and ``tsconfig`` ``paths``-aliased specifiers are resolved
    to repo files; bare package imports and unresolvable specifiers
    (the ``ts_imports`` verifier's concern) are skipped.
    """
    owner = registry.owner_of(rel_path)
    if owner is None or registry.nodes[owner].frontier:
        return [], []
    declared = set(registry.nodes[owner].depends_on)
    source = _read_source(rel_path, staged=staged, repo_root=repo_root)
    top = repo_root.resolve()
    file = (top / rel_path).resolve()
    tsconfig_path = ts.find_tsconfig(top, file)
    tsconfig = ts.load_tsconfig(tsconfig_path) if tsconfig_path is not None else None

    violations: list[EdgeViolation] = []
    frontier: list[FrontierRef] = []
    seen: set[str] = set()
    for _line, specifier in ts.import_specifiers(source):
        checked, resolved = ts.resolve_import(file, specifier, tsconfig)
        if not checked or resolved is None:
            continue
        if not resolved.is_relative_to(top):
            continue
        target_rel = resolved.relative_to(top).as_posix()
        if target_rel in seen or target_rel == rel_path:
            continue
        seen.add(target_rel)
        target = registry.owner_of(target_rel)
        if target is None or registry.nodes[target].frontier:
            frontier.append(FrontierRef(owner, target_rel, rel_path))
        elif target != owner and target not in declared:
            violations.append(EdgeViolation(owner, target, "import", target_rel, rel_path))
    return violations, frontier


def load_frontier_suppress(repo_root: Path) -> frozenset[str]:
    """Read the known-legacy suppression list from ``pyproject.toml``.

    ``[tool.repoach.arch] frontier_suppress`` lists artifacts (module
    paths or resource keys) whose frontier reports are silenced — the
    golden-rule home for exceptions. Keeps the day-one wall of un-owned
    legacy from drowning the signal.

    Args:
        repo_root: The repository root.

    Returns:
        The suppressed artifacts, empty when unconfigured.
    """
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.exists():
        return frozenset()
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    arch = data.get("tool", {}).get("repoach", {}).get("arch", {})
    return frozenset(str(item) for item in arch.get("frontier_suppress", []))


def check_diff(
    registry: Registry,
    changed_files: Iterable[str],
    repo_root: Path,
    *,
    staged: bool = False,
    suppress: frozenset[str] = frozenset(),
    config: GatesConfig | None = None,
) -> Report:
    """Check every changed Python / TypeScript file owned by a governed spec.

    Args:
        registry: The parsed spec registry.
        changed_files: Repo-relative paths changed by the diff.
        repo_root: The repository root.
        staged: When true, each file is read from the staged blob (the
            content that will actually be committed) rather than the
            worktree copy.
        suppress: Frontier artifacts to silence (known legacy).
        config: The project's gate configuration; read from
            ``repo_root`` when omitted. Python files are checked only
            when the project declares Python and they sit under its
            roots; TypeScript / JavaScript files only when declared.

    Returns:
        The aggregated :class:`Report` (frontier filtered by ``suppress``).
    """
    resolved_config = config if config is not None else load_gates_config(repo_root)
    bases = _package_bases(repo_root, resolved_config)
    violations: list[EdgeViolation] = []
    frontier: list[FrontierRef] = []
    for rel_path in changed_files:
        language = language_of(rel_path)
        if language in _JS_LANGUAGES and language in resolved_config.languages:
            file_violations, file_frontier = _check_js_file(
                registry, repo_root, rel_path, staged=staged
            )
        elif applicable(rel_path, resolved_config)[0]:
            file_violations, file_frontier = _check_file(
                registry, repo_root, rel_path, staged=staged, bases=bases
            )
        else:
            continue
        violations.extend(file_violations)
        frontier.extend(ref for ref in file_frontier if ref.artifact not in suppress)
    return Report(violations=tuple(violations), frontier=tuple(frontier))


def gather_changed_files(*, base: str, staged: bool, repo_root: Path) -> list[str]:
    """Return the repo-relative files changed in the diff.

    Args:
        base: The ref to diff against (three-dot, since the merge-base).
        staged: When true, diff the staged index instead (pre-commit).
        repo_root: The repository root.

    Returns:
        Added/copied/modified/renamed paths.

    Raises:
        subprocess.CalledProcessError: ``git`` failed — e.g. ``base`` is not
            reachable. Surfaced rather than swallowed so a misconfigured
            base ref fails loudly.
    """
    if staged:
        command = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"]
    else:
        command = ["git", "diff", "--name-only", "--diff-filter=ACMR", f"{base}...HEAD", "--"]
    completed = subprocess.run(command, cwd=repo_root, capture_output=True, text=True, check=True)
    return [line for line in completed.stdout.splitlines() if line]


_TEMPLATE_ERA = "2026-06-21"
"""The first day a spec must be governed (the day AFTER the template landed).

The governance template (SP-SPEC-TEMPLATE) landed 2026-06-20, but freeform
specs were still authored that day, so the whole transition day is
grandfathered: a spec filename (``<YYYY-MM-DD>_<SP-ID>_<slug>.md``) whose
date prefix is strictly before this is a pre-template freeform spec, skipped
by the presence check, so a base older than the template (a
``develop -> main`` diff) does not re-flag legacy frontier specs. Governed
specs dated 2026-06-20 carry frontmatter and are never flagged regardless.
"""


def _is_pre_template(name: str) -> bool:
    """Whether a spec filename's date prefix predates the template era."""
    prefix = name[:10]
    parts = prefix.split("-")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return False
    return prefix < _TEMPLATE_ERA


def gather_added_specs(*, base: str, staged: bool, repo_root: Path) -> list[str]:
    """Return the repo-relative ``docs/specs/*.md`` files ADDED in the diff.

    Added-only (``--diff-filter=A``): a *modified* legacy spec stays a
    frozen frontier node legitimately; only a brand-new spec must be
    governed. Underscore-prefixed files (``_TEMPLATE.md``) are excluded, and
    specs dated before :data:`_TEMPLATE_ERA` are grandfathered (a base older
    than the template must not re-flag pre-template frontier specs).

    Args:
        base: The ref to diff against (three-dot).
        staged: When true, diff the staged index instead (pre-commit).
        repo_root: The repository root.

    Returns:
        The added spec paths.
    """
    if staged:
        command = ["git", "diff", "--cached", "--name-only", "--diff-filter=A"]
    else:
        command = ["git", "diff", "--name-only", "--diff-filter=A", f"{base}...HEAD", "--"]
    completed = subprocess.run(command, cwd=repo_root, capture_output=True, text=True, check=True)
    out: list[str] = []
    for line in completed.stdout.splitlines():
        path = PurePosixPath(line)
        if (
            line.startswith("docs/specs/")
            and path.suffix == ".md"
            and not path.name.startswith("_")
            and not _is_pre_template(path.name)
        ):
            out.append(line)
    return out


def check_added_specs(
    added_specs: Iterable[str], repo_root: Path, *, staged: bool = False
) -> list[SpecPresenceViolation]:
    """Flag each ADDED spec that carries no frontmatter fence.

    A fence-less new spec would become an un-graphed frontier node
    silently; this is the blocking spec-presence check
    (`SP-ARCH-SPEC-PRESENCE`). A *malformed* fence is not this check's
    concern — :func:`repoach.arch.load_registry` already raises on it.

    Args:
        added_specs: Repo-relative added spec paths.
        repo_root: The repository root.
        staged: When true, each spec is read from the staged blob (the
            content that will actually be committed) rather than the
            worktree copy.

    Returns:
        One violation per fence-less added spec.
    """
    violations: list[SpecPresenceViolation] = []
    for rel_path in added_specs:
        text = _read_source(rel_path, staged=staged, repo_root=repo_root)
        if not has_frontmatter(text):
            violations.append(SpecPresenceViolation(path=rel_path))
    return violations


def run(
    *,
    base: str,
    staged: bool,
    specs_dir: Path,
    repo_root: Path,
    config: GatesConfig | None = None,
) -> Report:
    """Load the registry and check the diff — the orchestration entrypoint.

    A malformed frontmatter fence still raises (a fence that opens must
    parse); a frontmatter-less new spec is a violation only when
    ``config.require_frontmatter`` is on.

    Args:
        base: The ref to diff against.
        staged: Check the staged index instead.
        specs_dir: Directory of spec markdown files.
        repo_root: The repository root.
        config: The project's gate configuration; read from
            ``repo_root`` when omitted.

    Returns:
        The :class:`Report`, carrying the number of governed specs.
    """
    resolved_config = config if config is not None else load_gates_config(repo_root)
    registry = load_registry(specs_dir)
    governed = sum(1 for node in registry.nodes.values() if not node.frontier)
    changed = gather_changed_files(base=base, staged=staged, repo_root=repo_root)
    suppress = load_frontier_suppress(repo_root)
    report = check_diff(
        registry, changed, repo_root, staged=staged, suppress=suppress, config=resolved_config
    )
    spec_violations: list[SpecPresenceViolation] = []
    if resolved_config.require_frontmatter:
        added_specs = gather_added_specs(base=base, staged=staged, repo_root=repo_root)
        spec_violations = check_added_specs(added_specs, repo_root, staged=staged)
    return Report(
        violations=report.violations,
        frontier=report.frontier,
        spec_violations=tuple(spec_violations),
        governed_specs=governed,
    )


def report_lines(report: Report) -> list[str]:
    """Render a human ledger for a report (frontier aggregated, then violations)."""
    lines: list[str] = []
    if report.frontier:
        sources = {ref.source for ref in report.frontier}
        lines.append(
            f"frontier: {len(report.frontier)} ungoverned ref(s) across "
            f"{len(sources)} spec(s) — reported, not enforced"
        )
    for violation in report.violations:
        lines.append(
            f"undeclared edge: {violation.source} -> {violation.target} via "
            f"{violation.kind} '{violation.detail}' in {violation.file}; "
            f"declare {violation.target} in {violation.source} depends_on"
        )
    for spec_violation in report.spec_violations:
        lines.append(
            f"ungoverned new spec: {spec_violation.path} has no frontmatter — "
            "add the governed template block, or it becomes an un-graphed "
            "frontier node"
        )
    return lines
