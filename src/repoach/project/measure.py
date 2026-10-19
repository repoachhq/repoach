"""Measure a repository's defaults for ``repoach init`` (SP-UNIVERSAL-PROJECT-CONFIG).

Every value written by ``init`` is read from the clone, never assumed:

* the forge from ``git remote get-url origin`` (``github.com`` → github,
  a ``gitlab`` host label → gitlab, an unknown host answering
  ``glab api version`` → gitlab; otherwise left unset);
* the branches from ``origin/HEAD``: the release branch is the remote
  default branch, the integration branch is ``develop`` when the remote
  carries one, else the default branch itself;
* the merge method and branch deletion from the forge API when the
  ``gh`` / ``glab`` CLI can read them, else ``squash`` / ``true``;
* the languages from a census of ``git ls-files`` by extension;
* the source and test roots from the census' top-level directories;
* the lint and test commands from known markers (``pyproject.toml``,
  a root ``package.json`` with ``lint`` / ``test`` scripts, ``go.mod``),
  left empty otherwise.

Network probes are skipped with ``probe=False`` (``repoach init
--offline``); a probe that fails is logged and its key keeps the default.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit

from ..core.logging import get_logger
from ..core.paths import origin_url
from .config import DEFAULT_LINT_COMMAND, DEFAULT_TEST_COMMAND, ForgeName, MergeMethodName
from .resolve import infer_forge_from_url
from .toml_write import TomlScalar

_log = get_logger(__name__)

LANGUAGE_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "python": (".py",),
    "typescript": (".ts", ".tsx"),
    "javascript": (".js", ".jsx", ".mjs", ".cjs"),
    "go": (".go",),
    "rust": (".rs",),
    "verilog": (".v", ".sv"),
    "shell": (".sh",),
}
"""Extensions counted by the census (A2), in the order languages are listed."""

TEST_ROOT_NAMES: frozenset[str] = frozenset({"tests", "test", "spec", "e2e", "__tests__"})
"""Top-level directory names taken as test roots."""

NON_SOURCE_DIRS: frozenset[str] = frozenset(
    {"docs", "doc", "examples", "example", "scripts", "tools", "ci", "deploy", "vendor"}
)
"""Top-level directories never reported as source roots."""

PROBE_TIMEOUT_S = 15
"""Seconds allowed for one ``gh`` / ``glab`` probe."""


@dataclass(frozen=True)
class Measurement:
    """What ``repoach init`` measured.

    Attributes:
        values: ``{"section.key": value}`` to write uncommented.
        notes: One human line per decision worth reporting.
    """

    values: dict[str, TomlScalar]
    notes: list[str] = field(default_factory=list)


def _git(root: Path, *args: str) -> str | None:
    """Run ``git <args>`` in ``root``; stdout stripped, or None on failure."""
    completed = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=False
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def tracked_files(root: Path) -> list[str]:
    """Return every path ``git ls-files`` reports, repository-relative.

    Args:
        root: The project root.

    Returns:
        The tracked paths, POSIX-separated.
    """
    completed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=False
    )
    if completed.returncode != 0:
        return []
    return [path for path in completed.stdout.decode("utf-8", "replace").split("\0") if path]


def language_of(path: str) -> str | None:
    """Return the census language of one path, or None."""
    suffix = PurePosixPath(path).suffix.lower()
    for language, extensions in LANGUAGE_EXTENSIONS.items():
        if suffix in extensions:
            return language
    return None


def census(files: list[str]) -> list[str]:
    """Return the languages with at least one tracked file, in census order.

    Args:
        files: Tracked repository-relative paths.

    Returns:
        Language names ordered as :data:`LANGUAGE_EXTENSIONS`.
    """
    present = {language for path in files if (language := language_of(path)) is not None}
    return [language for language in LANGUAGE_EXTENSIONS if language in present]


def layout(files: list[str]) -> tuple[list[str], list[str]]:
    """Return ``(src_roots, test_roots)`` from the top-level directories.

    A root holding source files directly makes ``.`` the only source
    root (Go-style layouts).  Hidden and non-source directories
    (:data:`NON_SOURCE_DIRS`) are never source roots.

    Args:
        files: Tracked repository-relative paths.

    Returns:
        Sorted source roots and sorted test roots.
    """
    source_tops: set[str] = set()
    test_tops: set[str] = set()
    root_has_source = False
    for path in files:
        parts = PurePosixPath(path).parts
        if len(parts) == 1:
            root_has_source = root_has_source or language_of(path) not in (None, "shell")
            continue
        top = parts[0]
        if top in TEST_ROOT_NAMES:
            test_tops.add(top)
        elif (
            not top.startswith(".")
            and top not in NON_SOURCE_DIRS
            and language_of(path) not in (None, "shell")
        ):
            source_tops.add(top)
    src_roots = ["."] if root_has_source else sorted(source_tops)
    return src_roots, sorted(test_tops)


def _branches(root: Path) -> tuple[str, str] | None:
    """Return ``(integration_branch, release_branch)`` from ``origin/HEAD``."""
    head = _git(root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if not head or "/" not in head:
        return None
    default = head.split("/", 1)[1]
    has_develop = _git(root, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/develop")
    integration = "develop" if has_develop else default
    return integration, default


def _remote_host_and_path(url: str) -> tuple[str, str] | None:
    """Split a remote URL into ``(host, owner/repo)``."""
    if "://" in url:
        parts = urlsplit(url)
        host, path = parts.hostname or "", parts.path
    elif ":" in url:
        host, path = url.split("@", 1)[-1].split(":", 1)
    else:
        return None
    path = path.strip("/").removesuffix(".git")
    if not host or not path:
        return None
    return host.lower(), path


def _run_json(argv: list[str], cwd: Path) -> dict[str, object] | None:
    """Run a forge CLI command and parse its JSON object, or None."""
    try:
        completed = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log.info("init.probe_failed", argv=argv[:3], error=str(exc)[:160])
        return None
    if completed.returncode != 0:
        _log.info("init.probe_refused", argv=argv[:3], stderr=completed.stderr[:160])
        return None
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        _log.info("init.probe_unparsable", argv=argv[:3], error=str(exc)[:160])
        return None
    return parsed if isinstance(parsed, dict) else None


def _gitlab_answers(host: str, root: Path) -> bool:
    """Return whether ``host`` is a GitLab instance ``glab`` can reach."""
    if shutil.which("glab") is None:
        return False
    return _run_json(["glab", "api", "version", "--hostname", host], root) is not None


def _merge_facts(
    forge: ForgeName, host: str, path: str, root: Path
) -> tuple[MergeMethodName, bool] | None:
    """Read the merge method and branch deletion from the forge API."""
    if forge == "gitlab":
        if shutil.which("glab") is None:
            return None
        data = _run_json(
            ["glab", "api", f"projects/{quote(path, safe='')}", "--hostname", host], root
        )
        if data is None:
            return None
        squash = str(data.get("squash_option", ""))
        method = str(data.get("merge_method", "merge"))
        name: MergeMethodName
        if squash in ("always", "default_on"):
            name = "squash"
        elif method in ("rebase_merge", "ff"):
            name = "rebase"
        else:
            name = "merge"
        return name, bool(data.get("remove_source_branch_after_merge", True))
    if shutil.which("gh") is None:
        return None
    data = _run_json(["gh", "api", f"repos/{path}"], root)
    if data is None:
        return None
    if data.get("allow_squash_merge", True):
        github_name: MergeMethodName = "squash"
    elif data.get("allow_merge_commit", False):
        github_name = "merge"
    else:
        github_name = "rebase"
    return github_name, bool(data.get("delete_branch_on_merge", False))


def _commands(
    root: Path, files: list[str], src_roots: list[str], test_roots: list[str]
) -> tuple[str, str]:
    """Return the measured ``(lint, test)`` commands; empty when no marker."""
    lint: list[str] = []
    test: list[str] = []
    tracked = set(files)
    if "pyproject.toml" in tracked:
        python_roots = [
            top
            for top in [*src_roots, *test_roots]
            if top != "." and any(f.startswith(f"{top}/") and f.endswith(".py") for f in files)
        ]
        if python_roots == ["src", "tests"]:
            lint.append(DEFAULT_LINT_COMMAND)
        else:
            targets = " ".join(python_roots) or "."
            lint.append(f"ruff check {targets} && ruff format --check {targets}")
        if any(f.startswith("tests/unit/") for f in files):
            test.append(DEFAULT_TEST_COMMAND)
        elif "tests" in test_roots:
            test.append("pytest -q tests")
        else:
            test.append("pytest -q")
    if "package.json" in tracked:
        try:
            scripts = json.loads((root / "package.json").read_text(encoding="utf-8")).get(
                "scripts", {}
            )
        except (OSError, ValueError, AttributeError) as exc:
            _log.info("init.package_json_unreadable", error=str(exc)[:160])
            scripts = {}
        if isinstance(scripts, dict) and "lint" in scripts:
            lint.append("npm run lint")
        if isinstance(scripts, dict) and "test" in scripts:
            test.append("npm test")
    if "go.mod" in tracked:
        lint.append("go vet ./...")
        test.append("go test ./...")
    return " && ".join(lint), " && ".join(test)


def measure_defaults(root: Path, *, probe: bool = True) -> Measurement:
    """Measure the values ``repoach init`` writes for the repository at ``root``.

    Args:
        root: The project root (git toplevel).
        probe: Whether the forge may be asked over the network
            (``glab api`` / ``gh api``).

    Returns:
        The values to write and the notes to print.
    """
    values: dict[str, TomlScalar] = {"repoach.schema": 1}
    notes: list[str] = []

    url = origin_url(root) or ""
    remote = _remote_host_and_path(url) if url else None
    forge = infer_forge_from_url(url) if url else None
    if forge is None and remote is not None and probe and _gitlab_answers(remote[0], root):
        forge = "gitlab"
    if forge is not None:
        values["repoach.forge"] = forge
    elif url:
        notes.append(
            f"forge not recognised from origin {url!r}; set [repoach].forge or REPOACH_FORGE"
        )
    else:
        notes.append("no origin remote; set [repoach].forge or REPOACH_FORGE before 'review'")

    branches = _branches(root)
    if branches is not None:
        values["repoach.integration_branch"], values["repoach.release_branch"] = branches

    merge_method: MergeMethodName = "squash"
    facts = (
        _merge_facts(forge, remote[0], remote[1], root)
        if probe and forge is not None and remote is not None
        else None
    )
    if facts is not None:
        merge_method, remove_branch = facts
        values["repoach.remove_source_branch"] = remove_branch
        notes.append(f"merge_method {merge_method!r} read from the {forge} API")
    values["repoach.merge_method"] = merge_method
    values["repoach.specs_dir"] = "docs/specs"

    files = tracked_files(root)
    src_roots, test_roots = layout(files)
    values["project.languages"] = census(files)
    values["project.src_roots"] = src_roots
    values["project.test_roots"] = test_roots
    lint, test = _commands(root, files, src_roots, test_roots)
    values["commands.lint"] = lint
    values["commands.test"] = test
    return Measurement(values=values, notes=notes)
