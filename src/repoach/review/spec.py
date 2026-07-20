"""Load spec plans (spec) for review bots in the loop.

Every spec lives at ``docs/specs/<date>_<SP-ID>_<slug>.md``
following the project convention.  The same plan is fed to:

* the **Developer** agent (``run_dev_session``) to produce the
  initial implementation;
* the **Reviewer** NIM team (Architect / Sentinel / Tester / Scribe)
  to verify spec alignment in addition to generic quality;
* the **Coder** agent during the auto-fix loop to keep fixes
  spec-aware.

This module is the single source of truth for spec discovery, so
adding a new spec doc is the only step needed to make every NIM
bot in the loop spec-aware.

Module-level constants :
    - :data:`SPECS_DIR` is the repository-root-relative directory
      holding spec markdown plans.
    - ``_PLAN_HARD_CAP_CHARS`` caps how many characters of a spec
      plan we feed into a NIM prompt.  Each bot's prompt budget is
      ~30 KB total (diff + plan + reviews) and the plan should
      never dominate.
    - ``_BRANCH_RE`` extracts a spec id out of a branch name.
      Examples that match: ``feat/sp-sec-hardening``,
      ``fix/sp-pb1-registry``, ``chore/sp-dev-runner``.  Capturing
      group 1 returns the spec id in lower case (``sec``, ``pb1``,
      ``dev``); :func:`_normalise_id` then upper-cases it and adds
      the ``SP-`` prefix.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..core.logging import get_logger

_log = get_logger(__name__)

SPECS_DIR = Path("docs/specs")

_PLAN_HARD_CAP_CHARS: int = 12_000

_BRANCH_RE = re.compile(r"^[a-z]+/sp-([a-z0-9-]+?)(?:-impl|-fix)?-", re.IGNORECASE)


@dataclass(frozen=True)
class SpecPlan:
    """One spec loaded from disk.

    Attributes:
        id: Stable identifier in ``SP-<UPPER>`` form (``SP-SEC``, ``SP-DEV``).
        file_path: Path on disk (relative to repo root).
        raw_markdown: Full plan content, capped at
            :data:`_PLAN_HARD_CAP_CHARS`.
        title: First H1 line, stripped of the ``# `` marker.
        summary: First paragraph after the title, used as a one-line
            label in CLI output.
        referenced_paths: Repo-relative paths mentioned in the plan body,
            useful for the Developer agent to know which existing files
            to read into context.
    """

    id: str
    file_path: Path
    raw_markdown: str
    title: str
    summary: str
    referenced_paths: tuple[str, ...] = field(default_factory=tuple)


def _normalise_id(spec_id: str) -> str:
    """Return ``spec_id`` upper-cased + ``SP-`` prefixed.

    Accepts every reasonable spelling and emits the canonical form:

    * ``"sec"`` → ``"SP-SEC"``
    * ``"SP-SEC"`` → ``"SP-SEC"``
    * ``"sp-sec"`` → ``"SP-SEC"``
    * ``"PB1"`` → ``"SP-PB1"``
    """
    s = spec_id.strip().upper()
    if s.startswith("SP-"):
        return s
    return f"SP-{s}"


def _scan_known_spec_ids(root: Path | None = None) -> tuple[str, ...]:
    """Scan ``docs/specs/`` for spec IDs, longest-first.

    Walks every ``*.md`` file under :data:`SPECS_DIR` and
    extracts the canonical SP-ID encoded in the filename.

    Spec filename convention: ``<date>_<SP-ID>_<slug>.md`` where
    the SP-ID may contain dashes (e.g. ``SP-DEV-V2-B2``,
    ``SP-WA-AGENT-A2``, ``SP-MCP-EXT-A``).  We extract the segment
    bounded by ``_SP-`` and the next ``_``.

    Returning longest-first lets callers do greedy matching:
    a branch ``feat/sp-wa-agent-a2-impl`` should resolve to
    ``SP-WA-AGENT-A2`` rather than ``SP-WA`` even though both are
    legal prefixes.  Sorting longest-first deterministically resolves
    multi-scope specs (``SP-WA-AGENT-A2`` vs ``SP-WA-AGENT``).
    """
    base = (root or Path.cwd()) / SPECS_DIR
    if not base.is_dir():
        return ()
    ids: set[str] = set()
    pattern = re.compile(r"_(SP-[A-Z0-9-]+)_")
    for p in base.glob("*.md"):
        m = pattern.search(p.stem)
        if m:
            ids.add(m.group(1))
    return tuple(sorted(ids, key=lambda s: (-len(s), s)))


def detect_spec_from_branch(
    branch: str,
    *,
    root: Path | None = None,
) -> str | None:
    """Return the canonical spec id encoded in *branch*, or ``None``.

    Resolution order (changed SP-WA-AGENT-A2 work, 2026-04-30):

    1. **Greedy match against known spec IDs.**  Scan
       ``docs/specs/`` for the set of known ``SP-XXX`` IDs,
       and pick the LONGEST one that the branch starts with after
       the prefix.  This correctly handles multi-segment IDs like
       ``SP-WA-AGENT-A2`` (which the legacy regex would truncate to
       ``SP-WA``).

    2. **Legacy regex fallback** when no spec matches — preserves
       behaviour for branches that target a spec without a
       corresponding doc on disk yet (or in unit-test environments
       without a ``docs/specs/`` directory).

    Args:
        branch: Git branch name.
        root: Repository root.  Defaults to ``Path.cwd()``; injected
            in tests to point at a temp dir.

    Returns:
        The canonical ``SP-<ID>`` if the branch resolves, else
        ``None``.

    The greedy step accepts a body that equals the candidate id
    exactly, or a body that begins with the id followed by a dash
    separator (then the slug).  Anything else falls through to the
    short-form regex fallback.

    Examples:
        >>> detect_spec_from_branch("feat/sp-sec-hardening")  # doctest: +SKIP
        'SP-SEC'
        >>> detect_spec_from_branch("feat/sp-wa-agent-a2-drop-classifier")  # doctest: +SKIP
        'SP-WA-AGENT-A2'
        >>> detect_spec_from_branch("main") is None
        True
    """
    if not branch:
        return None

    parts = branch.split("/", 1)
    if len(parts) == 2:
        body = parts[1].lower()
        if body.startswith("sp-"):
            for known in _scan_known_spec_ids(root):
                expected = known.lower()
                if body == expected or body.startswith(expected + "-"):
                    return known

    m = _BRANCH_RE.match(branch)
    if m is None:
        return None
    return _normalise_id(m.group(1))


def _scan_referenced_paths(markdown: str) -> tuple[str, ...]:
    """Extract repo-relative paths mentioned in a spec plan body.

    Looks at three sources in decreasing reliability:

    1. Backtick paths matching ``src/`` or ``tests/`` or ``docs/``
       prefixes — almost always real file references.
    2. Inline ``"src/repoach/<...>"`` style mentions.
    3. Bare paths ending in ``.py`` or ``.md`` inside backticks.

    The pattern matches backtick-wrapped paths whose body contains
    one of the known top-level prefixes (``src``, ``tests``, ``docs``,
    ``prompts``, ``.github``), so deeper references such as
    ``src/repoach/...`` are also captured.

    Returns a deduplicated tuple, preserving first-occurrence order.
    """
    out: list[str] = []
    seen: set[str] = set()
    pattern = re.compile(
        r"`([a-zA-Z0-9_./-]*(?:src|tests|docs|prompts|\.github)/[a-zA-Z0-9_./-]+)`"
    )
    for m in pattern.finditer(markdown):
        path = m.group(1).strip(".")
        if path and path not in seen:
            seen.add(path)
            out.append(path)
    return tuple(out)


def _resolve_plan_file(spec_id: str, *, root: Path | None = None) -> Path | None:
    """Find the plan file for *spec_id* under :data:`SPECS_DIR`.

    Convention: ``docs/specs/<date>_<SP-ID>_<slug>.md``.  Multiple
    files matching the same id (e.g. follow-up plans) → the most recent
    by lexicographic sort wins.

    Args:
        spec_id: Canonical id, e.g. ``"SP-SEC"``.
        root: Repository root.  Defaults to ``Path.cwd()``.

    Returns:
        Absolute path to the matching markdown file, or ``None``.
    """
    base = (root or Path.cwd()) / SPECS_DIR
    if not base.is_dir():
        return None
    matches = sorted(p for p in base.glob(f"*{spec_id}*.md") if p.is_file())
    if not matches:
        return None
    return matches[-1]


def load_spec(spec_id: str, *, root: Path | None = None) -> SpecPlan:
    """Load the spec plan for *spec_id*.

    Args:
        spec_id: Any reasonable spelling (``"sec"``, ``"SP-SEC"``,
            ``"sp-sec"``).  Normalised internally.
        root: Optional repository root override (tests).

    Returns:
        A :class:`SpecPlan` populated from the matched markdown file.

    Raises:
        FileNotFoundError: When no spec doc matches the id.
    """
    canonical = _normalise_id(spec_id)
    path = _resolve_plan_file(canonical, root=root)
    if path is None:
        raise FileNotFoundError(f"no spec doc matches {canonical!r} under {SPECS_DIR}")
    raw = path.read_text(encoding="utf-8")
    if len(raw) > _PLAN_HARD_CAP_CHARS:
        raw = (
            raw[:_PLAN_HARD_CAP_CHARS]
            + "\n\n[... plan truncated; "
            + f"see {path} for the full version ...]\n"
        )
    title, summary = _parse_title_and_summary(raw)
    referenced = _scan_referenced_paths(raw)
    _log.info(
        "spec.loaded",
        spec_id=canonical,
        path=str(path),
        n_referenced=len(referenced),
    )
    return SpecPlan(
        id=canonical,
        file_path=path,
        raw_markdown=raw,
        title=title,
        summary=summary,
        referenced_paths=referenced,
    )


def _parse_title_and_summary(markdown: str) -> tuple[str, str]:
    """Return ``(title, summary)`` from a markdown document.

    Title = first ``# ...`` line (empty when the doc has no H1).
    Summary = first non-empty paragraph that is not a heading,
    blockquote, list marker, or table row.  When the doc has no
    title, the scan restarts from the top so body-only docs still
    produce a useful label.
    """
    title = ""
    summary = ""
    lines = markdown.splitlines()
    i = 0
    title_found = False
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("# ") and not title:
            title = line[2:].strip()
            title_found = True
            i += 1
            break
        i += 1
    if not title_found:
        i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line or line.startswith(("#", ">", "-", "*", "|")):
            i += 1
            continue
        summary = line.rstrip(".,;:") + "."
        break
    return title, summary[:240]


def maybe_load_active_spec(
    *,
    explicit_id: str | None = None,
    branch: str | None = None,
    root: Path | None = None,
) -> SpecPlan | None:
    """Best-effort spec discovery for the AgentLoop.

    Resolution order:
    1. ``explicit_id`` if provided (e.g. ``REPOACH_SPEC_ID`` env).
    2. ``branch`` parsed via :func:`detect_spec_from_branch`.

    Returns ``None`` (instead of raising) when no spec is detected
    or the matched doc cannot be loaded — callers fall back to
    diff-only review behaviour.
    """
    candidate = (explicit_id or "").strip()
    if not candidate and branch:
        detected = detect_spec_from_branch(branch, root=root)
        if detected:
            candidate = detected
    if not candidate:
        return None
    try:
        return load_spec(candidate, root=root)
    except FileNotFoundError as exc:
        _log.info("spec.not_found", id=candidate, error=str(exc))
        return None
