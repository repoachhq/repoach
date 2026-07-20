"""Shared Coder primitives: path whitelist, placeholder guard, gates, push.

After the evidence-first flip (redesign slice 10b) the legacy
``run_coder_fix`` archive-verdict loop is gone; this module is now the
toolbox the findings-driven Coder (:mod:`coder_findings`) and the
Developer session (:mod:`dev_runner`) build on:

* :func:`is_path_allowed` — the path whitelist (defence in depth on top
  of the prompt): :data:`FORBIDDEN_PATHS` / :data:`FORBIDDEN_PREFIXES`
  (``.github/``, ``.githooks/``, ``prompts/review/``, ``.env*``), no
  absolute paths, no ``..`` traversal.
* :func:`is_placeholder_content` / :func:`persist_placeholder_rejected`
  — reject LLM placeholder strings rather than corrupt the working tree.
* :func:`apply_fixes` — write validated search/replace or full-file
  edits through the whitelist + placeholder guard.
* :func:`run_ruff_gate` / :func:`run_pytest_matrix` — the local gates a
  fix must pass; :func:`git_commit_and_push` lands the result on green
  (a red gate leaves the work uncommitted — no destructive revert; the
  fix-forward stance carried in from the DEVAGENT arc, SP-DEVAGENT-WIRE).
* :func:`fetch_ci_status` / :func:`fetch_failed_check_logs` — read the
  PR's check state (a red check is materialised as a ``broken_behavior``
  finding by :mod:`coder_findings`).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..core.logging import get_logger
from .gh_client import GhCli
from .patch_apply import apply_search_replace_edits
from .secret_env import scrubbed_env

_log = get_logger(__name__)

_SIZE_GUARD_MAX_DELTA: float = 0.40
"""Reject a Coder patch when the line-count delta exceeds this fraction
of the original file.  SP-CODER-TARGETED-PATCH: a 1-line nit that shrinks
a 175-line file to 74 lines (58% delta) is a full-file rewrite, not a
targeted patch.  The massive_shrinkage layer (< 5% of original + < 200
chars) catches near-total deletions; this guard catches the subtler case
where the model regenerates most of the file from memory and drops
functionality it didn't memorise."""


FORBIDDEN_PATHS: frozenset[str] = frozenset(
    {
        "memory/L0_meta_rules.md",
        ".env",
        ".env.example",
        ".env.local",
        ".env.production",
    }
)
"""Exact repo-relative paths the Coder must never touch."""


FORBIDDEN_PREFIXES: tuple[str, ...] = (
    ".github/",
    ".githooks/",
    "prompts/review/",
    ".git/",
)
"""Directory prefixes (repo-relative, ending with ``/``) that are
off-limits to the Coder.  Even a one-line tweak to a workflow or a
reviewer persona could let the bot rewrite its own ratings.  All of
``.github/`` is covered (not just ``workflows/``): CODEOWNERS steers
review routing, dependabot.yml and action configs execute in CI.
``.githooks/`` is wired via ``git config core.hooksPath`` — a merged
malicious hook is local code execution on the operator's next
commit/push (SP-CODER-WHITELIST-HARDEN)."""


def is_path_allowed(path: str) -> bool:
    """Return True when ``path`` is safe for the Coder to overwrite.

    A path is allowed when:

    * It does not start with ``/`` (no absolute paths).
    * It does not contain a ``..`` segment (no traversal).
    * Its final component is not an env file — exactly ``.env``, any
      ``.env.*`` variant, or ``.envrc`` — anywhere in the tree, not
      just at repo root (SP-CODER-WHITELIST-HARDEN).
    * It is not in :data:`FORBIDDEN_PATHS`.
    * It does not start with any prefix in :data:`FORBIDDEN_PREFIXES`.

    Args:
        path: A repo-relative path proposed by the Coder.

    Returns:
        True when safe, False otherwise.
    """
    if not path or not isinstance(path, str):
        return False
    if path.startswith("/") or (len(path) > 1 and path[1] == ":"):
        return False
    parts = path.replace("\\", "/").split("/")
    if any(p == ".." for p in parts):
        return False
    basename = parts[-1]
    if basename == ".env" or basename.startswith(".env.") or basename == ".envrc":
        return False
    norm = path.replace("\\", "/")
    if norm in FORBIDDEN_PATHS:
        return False
    return all(not norm.startswith(prefix) for prefix in FORBIDDEN_PREFIXES)


def _run(
    argv: list[str],
    *,
    cwd: Path,
    timeout_s: int = 300,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run ``argv`` and return ``(returncode, stdout, stderr)``.

    ``env`` defaults to ``None`` (inherit the full environment) for the git/tooling
    callers; the test-execution caller passes a secret-scrubbed environment so
    agent-authored code never sees live credentials (SP-DEVAGENT-LOOP finding S4).
    """
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            env=env,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError as exc:
        return 127, "", f"binary not found: {exc}"
    except subprocess.TimeoutExpired as exc:
        return -1, exc.stdout or "", f"timeout after {timeout_s}s"


_PLACEHOLDER_SENTINELS: tuple[str, ...] = (
    "as shown in the original",
    "rest of file",
    "rest of the file",
    "# unchanged",
    "// unchanged",
    "todo: full content",
    "(truncated)",
    "omitted for brevity",
    "<placeholder>",
    "...rest of code...",
    "...remaining code...",
)


@dataclass(frozen=True)
class PlaceholderResult:
    """Outcome of :func:`is_placeholder_content` for one fix.

    Attributes:
        is_placeholder: ``True`` when the content matches any layer of
            the heuristic; ``False`` when it looks like real code.
        reason: Short identifier of which heuristic fired
            (``"sentinel_string"`` / ``"single_comment_line"`` /
            ``"massive_shrinkage"`` / ``"excessive_size_delta"`` /
            ``"test_file_no_tests"`` / ``""``).
        evidence: Up to 240 chars of the offending content for the log.
    """

    is_placeholder: bool
    reason: str = ""
    evidence: str = ""


def is_placeholder_content(
    path: str,
    new_content: str,
    *,
    repo_root: Path | None = None,
    allow_growth: bool = False,
) -> PlaceholderResult:
    """Detect whether ``new_content`` is an LLM placeholder, not real code.

    Heuristic (returns on the FIRST layer that fires):

    1. **Sentinel string**: any substring in :data:`_PLACEHOLDER_SENTINELS`
       (case-insensitive).  Catches ``"# ... (full file contents as
       shown in the original file) ..."`` (PR #103 round 1) and the
       common ``"# rest of file unchanged"`` / ``"# (truncated)"`` shapes.

    2. **Single-comment-line file**: the entire content is one
       non-empty line beginning with ``#`` (after stripping shebang +
       blank lines), with no functions / classes / imports / strings.
       The PR #103 round 1 file matched exactly.

    3. **Massive shrinkage** (only when ``repo_root`` is provided AND
       the target exists at HEAD): proposed content is < 5% of the
       existing file's line count AND < 200 chars total.  A 95%
       shrinkage to a tiny blob almost always means the model
       dropped detail.

    4. **Excessive size delta** (SP-CODER-TARGETED-PATCH): proposed
       content differs by more than :data:`_SIZE_GUARD_MAX_DELTA`
       (40%) in line count from the existing file.  Catches the
       subtler full-file-rewrite pattern where the model regenerates
       most of a file and drops functionality it didn't memorise
       (PR #220: ``safe_merge.sh`` shrank from 175 to 74 lines on a
       1-line nit).  With ``allow_growth=True``
       (SP-DEV-GROWTH-DELTA, the Developer build context) this layer
       fires on shrinkage only — a plan step legitimately *extends*
       young files, and growth is not evidence of a placeholder.

    5. **Test file with no tests**: when ``path`` matches
       ``tests/**/*.py`` or starts with ``test_``, the new content
       has zero ``def test_`` lines.  A "test file" without tests
       is a placeholder regardless of byte count.

    Args:
        path: Repo-relative target path of the proposed fix.
        new_content: Proposed file contents.
        repo_root: Optional repo root for the shrinkage check.  When
            ``None``, layer 3 is skipped.
        allow_growth: When ``True`` (Developer build context), layer 4
            fires on shrinkage only — growing a file past the cap is
            legitimate build work, not a placeholder signal.

    Returns:
        :class:`PlaceholderResult` — ``is_placeholder=True`` triggers
        rejection in :func:`apply_fixes`.
    """
    if not isinstance(new_content, str):
        return PlaceholderResult(is_placeholder=False)

    lower = new_content.lower()
    for sentinel in _PLACEHOLDER_SENTINELS:
        if sentinel in lower:
            idx = lower.find(sentinel)
            preview = new_content[max(0, idx - 20) : idx + len(sentinel) + 80]
            return PlaceholderResult(
                is_placeholder=True,
                reason="sentinel_string",
                evidence=f"matched {sentinel!r}: {preview[:240]!r}",
            )

    significant = [
        line.rstrip()
        for line in new_content.splitlines()
        if line.strip() and not line.lstrip().startswith("#!")
    ]
    if len(significant) == 1 and significant[0].lstrip().startswith("#"):
        return PlaceholderResult(
            is_placeholder=True,
            reason="single_comment_line",
            evidence=significant[0][:240],
        )

    if repo_root is not None:
        try:
            target = (repo_root / path).resolve()
            if target.is_file():
                existing = target.read_text(encoding="utf-8")
                existing_lines = existing.count("\n") + 1
                new_lines = new_content.count("\n") + 1
                min_existing_lines_for_shrink_check = 20
                if (
                    existing_lines >= min_existing_lines_for_shrink_check
                    and new_lines / max(existing_lines, 1) < 0.05
                    and len(new_content) < 200
                ):
                    return PlaceholderResult(
                        is_placeholder=True,
                        reason="massive_shrinkage",
                        evidence=(
                            f"{existing_lines}→{new_lines} lines, "
                            f"{len(new_content)} chars; head: "
                            f"{new_content[:160]!r}"
                        ),
                    )
                delta_ratio = abs(new_lines - existing_lines) / max(existing_lines, 1)
                growth_exempted = allow_growth and new_lines >= existing_lines
                if (
                    existing_lines >= min_existing_lines_for_shrink_check
                    and delta_ratio > _SIZE_GUARD_MAX_DELTA
                    and not growth_exempted
                ):
                    return PlaceholderResult(
                        is_placeholder=True,
                        reason="excessive_size_delta",
                        evidence=(
                            f"{existing_lines}→{new_lines} lines "
                            f"(delta {delta_ratio:.0%}, cap {_SIZE_GUARD_MAX_DELTA:.0%})"
                        ),
                    )
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            _log.debug(
                "coder_loop.size_guard_unreadable",
                path=path,
                error_type=type(exc).__name__,
            )

    looks_like_test = (
        path.startswith("tests/") or "/tests/" in path or Path(path).name.startswith("test_")
    )
    if looks_like_test:
        has_tests = re.search(r"^\s*(?:async\s+)?def\s+test_\w+", new_content, re.MULTILINE)
        if not has_tests:
            return PlaceholderResult(
                is_placeholder=True,
                reason="test_file_no_tests",
                evidence=(
                    f"path={path!r} but new_content has no `def test_` line "
                    f"(content head: {new_content[:160]!r})"
                ),
            )

    return PlaceholderResult(is_placeholder=False)


def persist_placeholder_rejected(
    *,
    pr_number: int | str,
    plan: dict[str, Any],
    rejected: list[dict[str, Any]],
    logs_dir: Path | None = None,
) -> Path | None:
    """Persist the full Coder plan + rejected-fix details to ``logs/``.

    Same pattern as :func:`persist_accept_without_fixes` so the
    workflow's existing ``coder_*.txt`` glob picks the file up.

    Args:
        pr_number: Identifier for the filename.
        plan: The full Coder plan that produced the placeholder fix(es).
        rejected: List of ``{"path", "reason", "evidence"}`` dicts the
            placeholder check generated.
        logs_dir: Test-only override of the destination directory.

    Returns:
        The path written to, or ``None`` on I/O failure.
    """
    target_dir = logs_dir or (Path(__file__).resolve().parents[3] / "logs")
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _log.warning(
            "coder_loop.placeholder_log_mkdir_failed",
            target_dir=str(target_dir),
            error_type=type(exc).__name__,
            error=str(exc)[:200],
        )
        return None
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(pr_number))[:64] or "unknown"
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = target_dir / f"coder_placeholder_rejected_{safe_id}_{timestamp}.txt"
    payload = {"plan": plan, "placeholder_rejections": rejected}
    try:
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except OSError as exc:
        _log.warning(
            "coder_loop.placeholder_log_write_failed",
            path=str(path),
            error_type=type(exc).__name__,
            error=str(exc)[:200],
        )
        return None
    return path


def _materialise_edits(
    repo_root: Path,
    path_raw: str,
    edits: list[dict[str, str]],
    *,
    edit_failures_out: list[str] | None = None,
) -> str | None:
    """Resolve an anchored-edits fix to full file contents.

    SP-DEV-TARGETED-PATCH: reads the existing file and applies the
    search/replace pairs via :func:`apply_search_replace_edits`. Every
    failure is logged AND appended to ``edit_failures_out`` so the
    runner can feed the directive report back to the retry.

    Returns the resulting full contents, or ``None`` on any failure
    (missing/unreadable/escaping target, anchor not found or
    ambiguous).
    """
    target = (repo_root / path_raw).resolve()
    try:
        target.relative_to(repo_root)
    except ValueError:
        _log.warning("review.coder.edits_path_escape", path=path_raw)
        return None
    if not target.is_file():
        message = (
            f"{path_raw}: edits target a file that does not exist — "
            "use new_content to create new files"
        )
        _log.warning("review.coder.edits_target_missing", path=path_raw)
        if edit_failures_out is not None:
            edit_failures_out.append(message)
        return None
    try:
        existing = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _log.warning(
            "review.coder.edits_target_unreadable",
            path=path_raw,
            error=str(exc)[:120],
        )
        return None
    result, report = apply_search_replace_edits(existing, edits)
    if result is None:
        _log.warning("review.coder.edits_failed", path=path_raw, report=report[:300])
        if edit_failures_out is not None:
            edit_failures_out.append(f"{path_raw}: {report}")
        return None
    return result


def apply_fixes(
    fixes: list[dict[str, Any]],
    *,
    repo_root: Path,
    placeholder_rejections_out: list[dict[str, str]] | None = None,
    allow_growth: bool = False,
    edit_failures_out: list[str] | None = None,
) -> tuple[int, list[str]]:
    """Write each ``fixes[].new_content`` to disk, after whitelist check.

    SP-CODER-PLACEHOLDER-DETECT (2026-05-06): every fix passes
    through :func:`is_placeholder_content` before writing.
    Placeholder fixes are added to ``rejected`` and a loud
    ``review.coder.placeholder_rejected`` log is emitted with the
    matched heuristic + evidence preview.  PR #103 round 1
    demonstrated the failure mode: the Coder model emitted
    ``new_content="# ... (full file contents as shown in the
    original file) ..."`` and ``apply_fixes`` wrote it verbatim,
    truncating a 261-line test file to 1 line.

    SP-CODER-WHITELIST-RESOLVE (2026-07-15): the whitelist is enforced
    twice — once on the raw string (cheap first filter) and again on
    the RESOLVED repo-relative path right before writing, mirroring
    the Developer's ``_resolve_writable``.  The raw check alone let
    ``./``- or ``//``-prefixed strings and in-repo symlinks reach
    forbidden targets (``.github/``, ``.githooks/``,
    ``prompts/review/``, ``.git/``, env files): the disguised string
    never ``startswith`` the bare forbidden prefix, and a symlink's
    allowed name says nothing about where it resolves.  Any fix whose
    resolved form is not both inside the repo AND whitelist-allowed is
    rejected, never written (audit 2026-07-13 finding C1).

    Args:
        fixes: List of ``{"path": str, "new_content": str, ...}`` dicts.
        repo_root: Repository root.  All paths are resolved relative
            to this directory; anything that escapes is rejected.
        placeholder_rejections_out: Optional list the function APPENDS
            ``{"path", "reason", "evidence"}`` dicts to for every
            placeholder-rejected fix.  When provided, the caller can
            persist + escalate the diagnostic (CLI exit 9 in
            ``run_coder_fix``).  Pass ``None`` to ignore (back-compat
            for callers that don't care about the breakdown).
        allow_growth: Forwarded to :func:`is_placeholder_content` —
            ``True`` in the Developer build context
            (SP-DEV-GROWTH-DELTA) so the size guard punishes only
            shrinkage; the Coder loop keeps the symmetric default.
        edit_failures_out: Optional list the function APPENDS one
            directive line to per failed anchored-edits fix
            (SP-DEV-TARGETED-PATCH) — the runner includes them in the
            retry feedback so the model can fix its anchors.

    Returns:
        ``(applied_count, rejected_paths)``.  The placeholder
        rejections are ALSO in ``rejected_paths``; they're just
        additionally surfaced via ``placeholder_rejections_out``
        so the caller can distinguish them from path-whitelist
        rejections.
    """
    applied = 0
    rejected: list[str] = []
    repo_root = repo_root.resolve()
    for fix in fixes:
        path_raw = fix.get("path")
        new_content = fix.get("new_content")
        edits = fix.get("edits")
        if not isinstance(path_raw, str) or not path_raw:
            rejected.append(str(path_raw))
            continue
        if not is_path_allowed(path_raw):
            _log.warning("coder.path_rejected", path=path_raw)
            rejected.append(path_raw)
            continue
        if not isinstance(new_content, str) and isinstance(edits, list):
            new_content = _materialise_edits(
                repo_root, path_raw, edits, edit_failures_out=edit_failures_out
            )
            if new_content is None:
                rejected.append(path_raw)
                continue
        if not isinstance(new_content, str):
            rejected.append(path_raw)
            continue
        placeholder = is_placeholder_content(
            path_raw, new_content, repo_root=repo_root, allow_growth=allow_growth
        )
        if placeholder.is_placeholder:
            _log.warning(
                "review.coder.placeholder_rejected",
                path=path_raw,
                reason=placeholder.reason,
                evidence=placeholder.evidence,
                content_chars=len(new_content),
            )
            rejected.append(path_raw)
            if placeholder_rejections_out is not None:
                placeholder_rejections_out.append(
                    {
                        "path": path_raw,
                        "reason": placeholder.reason,
                        "evidence": placeholder.evidence,
                    }
                )
            continue
        target = (repo_root / path_raw).resolve()
        try:
            resolved_relative = target.relative_to(repo_root).as_posix()
        except ValueError:
            _log.warning("coder.path_escapes_repo", path=path_raw)
            rejected.append(path_raw)
            continue
        if not is_path_allowed(resolved_relative):
            _log.warning(
                "coder.resolved_path_rejected",
                path=path_raw,
                resolved=resolved_relative,
            )
            rejected.append(path_raw)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_content, encoding="utf-8")
        applied += 1
        _log.info("coder.path_applied", path=path_raw, bytes=len(new_content))
    return applied, rejected


def run_pytest(repo_root: Path, *, python: str | None = None) -> tuple[bool, str]:
    """Run the unit-test suite under ``python`` and return ``(passed, tail_log)``.

    Honours the same ignore list as ``ci.yml`` so the gate matches CI.

    Args:
        repo_root: Repository root.
        python: Path or name of the Python interpreter to invoke
            ``-m pytest`` under.  When ``None``, falls back to
            ``shutil.which("pytest")`` for backwards compatibility
            (single-version local runs).  In CI we pass an explicit
            interpreter per matrix version (``python3.11`` / ``python3.13``)
            so the same runner can validate both before pushing.
    """
    argv = [python, "-m", "pytest"] if python else [shutil.which("pytest") or "pytest"]
    argv += [
        "-q",
        "tests/unit",
    ]
    rc, stdout, stderr = _run(argv, cwd=repo_root, timeout_s=900, env=scrubbed_env())
    tail = (stdout + "\n" + stderr)[-4000:]
    return rc == 0, tail


def _pytest_pythons() -> list[str | None]:
    """Return the list of Python interpreters the local gate should run.

    Reads the ``REPOACH_CODER_PYTHONS`` env var, falling back to the
    pre-rename ``FEROVA_CODER_PYTHONS`` name (CSV of executable names
    or paths, e.g. ``"python3.11,python3.13"``).  Each entry is
    validated against :func:`shutil.which`; missing interpreters are
    silently skipped (so a developer running locally on a single
    Python doesn't get spurious failures).

    Returns:
        A non-empty list of interpreter strings, or ``[None]`` when
        the env is unset or no listed interpreter resolves — in
        which case :func:`run_pytest` falls back to the bare
        ``pytest`` binary on PATH.
    """
    raw = (
        os.environ.get("REPOACH_CODER_PYTHONS") or os.environ.get("FEROVA_CODER_PYTHONS", "")
    ).strip()
    if not raw:
        return [None]
    candidates = [s.strip() for s in raw.split(",") if s.strip()]
    resolved = [c for c in candidates if shutil.which(c)]
    return resolved or [None]


def run_pytest_matrix(repo_root: Path) -> tuple[bool, str]:
    """Run :func:`run_pytest` across every interpreter listed in env.

    Iterates over :func:`_pytest_pythons`.  Returns on the first
    failure (no need to keep going — a single CI matrix slot failing
    means we cannot push).  When the env is unset, this collapses to
    a single ``run_pytest`` call.
    """
    pythons = _pytest_pythons()
    for python in pythons:
        ok, tail = run_pytest(repo_root, python=python)
        label = python or "default"
        if not ok:
            return False, f"pytest failed under {label}:\n{tail}"
        _log.info("coder.pytest_matrix_slot_green", python=label)
    summary = ", ".join(p or "default" for p in pythons)
    return True, f"pytest green under {summary}"


def run_ruff_gate(repo_root: Path, *, unsafe_fixes: bool = False) -> tuple[bool, str]:
    """Run the same ruff checks CI does, with auto-fix applied first.

    Sequence:
        1. ``ruff check --fix src tests`` — apply the auto-fixable
           lint corrections (I001, F401, …) so the model isn't
           penalised for trivial nits.
        2. ``ruff format src tests`` — reflow.
        3. ``ruff check src tests`` — validate the residue is clean.
           Any remaining lint violation here means CI will fail.
        4. ``ruff format --check src tests`` — sanity that no file
           drifted between step 2 and step 3.

    Args:
        repo_root: Working tree root.
        unsafe_fixes: When ``True`` (the Developer build context,
            SP-DEV-RUFF-UNSAFE-FIXES) step 1 also passes
            ``--unsafe-fixes`` so ruff applies its own known fix for
            rules like ``SIM102`` that a model cannot reliably
            hand-restructure on retry. Any behaviour change such a fix
            introduces is caught by the promised-tests and full-suite
            gates downstream. The Coder loop keeps the conservative
            default (safe fixes only).

    Returns:
        ``(passed, tail_log)``. ``passed`` is ``False`` as soon as
        any step in 3-4 reports a violation; the caller is expected
        to revert the working tree before exiting.
    """
    ruff = shutil.which("ruff") or "ruff"
    fix_cmd = [ruff, "check", "--fix", "src", "tests"]
    if unsafe_fixes:
        fix_cmd.append("--unsafe-fixes")
    _run(fix_cmd, cwd=repo_root, timeout_s=120)
    _run([ruff, "format", "src", "tests"], cwd=repo_root, timeout_s=120)
    rc, stdout, stderr = _run([ruff, "check", "src", "tests"], cwd=repo_root, timeout_s=120)
    if rc != 0:
        return False, f"ruff check failed:\n{(stdout + stderr)[-2000:]}"
    rc, stdout, stderr = _run(
        [ruff, "format", "--check", "src", "tests"], cwd=repo_root, timeout_s=120
    )
    if rc != 0:
        return False, f"ruff format --check failed:\n{(stdout + stderr)[-2000:]}"
    return True, "ruff clean"


def git_commit_and_push(
    *,
    repo_root: Path,
    commit_message: str,
    branch: str,
) -> tuple[bool, str]:
    """Stage all changes, commit as the configured identity, push.

    The caller (the workflow) is responsible for setting
    ``user.name`` / ``user.email`` to the ``coder-bot[bot]`` identity
    before invoking this function.

    Returns ``(pushed, log_tail)``.  ``pushed`` is False when there
    was nothing to commit OR when the push failed.
    """
    git = shutil.which("git") or "git"
    rc, _, _ = _run([git, "add", "-A"], cwd=repo_root, timeout_s=60)
    if rc != 0:
        return False, "git add failed"
    rc, stdout, _ = _run([git, "diff", "--cached", "--name-only"], cwd=repo_root, timeout_s=30)
    if rc != 0 or not stdout.strip():
        return False, "no staged changes"
    msg = commit_message.strip() or "fix(review): coder-bot auto-fix"
    rc, stdout, stderr = _run([git, "commit", "-m", msg], cwd=repo_root, timeout_s=60)
    if rc != 0:
        return False, f"git commit failed: {stderr[:200]}"
    rc, stdout, stderr = _run(
        [git, "push", "origin", f"HEAD:{branch}"], cwd=repo_root, timeout_s=180
    )
    if rc != 0:
        return False, f"git push failed: {stderr[:200]}"
    return True, "pushed"


CI_GREEN: str = "GREEN"
CI_RED: str = "RED"
CI_PENDING: str = "PENDING"
CI_UNKNOWN: str = "UNKNOWN"


def fetch_ci_status(gh: GhCli, pr_number: int) -> tuple[str, list[dict[str, str]]]:
    """Return the aggregated required-check state on a PR.

    Args:
        gh: A :class:`GhCli` wrapper.
        pr_number: GitHub PR number.

    Returns:
        ``(state, failed_rows)`` where ``state`` is one of
        :data:`CI_GREEN`, :data:`CI_RED`, :data:`CI_PENDING`,
        :data:`CI_UNKNOWN`, and ``failed_rows`` carries
        ``{name, link, workflow}`` dicts for the failed checks (used
        downstream to fetch their failure logs).
    """
    res = gh._run(
        [
            "pr",
            "checks",
            str(pr_number),
            "--required",
            "--json",
            "name,state,bucket,link,workflow",
        ]
    )
    if not res.ok and res.returncode != 8:
        return CI_UNKNOWN, []
    try:
        rows = json.loads(res.stdout) or []
    except json.JSONDecodeError:
        return CI_UNKNOWN, []
    if not isinstance(rows, list):
        return CI_UNKNOWN, []
    if not rows:
        return CI_GREEN, []

    buckets = [str(r.get("bucket", "")).lower() for r in rows]
    if any(b in {"pending", ""} for b in buckets):
        return CI_PENDING, []
    failed_rows = [
        {
            "name": str(r.get("name", "")),
            "link": str(r.get("link", "")),
            "workflow": str(r.get("workflow", "")),
        }
        for r in rows
        if str(r.get("bucket", "")).lower() == "fail"
    ]
    if failed_rows:
        return CI_RED, failed_rows
    return CI_GREEN, []


_RUN_LINK_RE = re.compile(r"/runs/(\d+)(?:/job/(\d+))?")


def fetch_failed_check_logs(gh: GhCli, failed_rows: list[dict[str, str]]) -> list[str]:
    """Pull the ``--log-failed`` tail for each failed check link.

    Args:
        gh: A :class:`GhCli` wrapper.
        failed_rows: Output of :func:`fetch_ci_status` second tuple item.

    Returns:
        One log block per failed check, capped at ~8 KB each, prefixed
        with the check name so the Coder can correlate.  Failed lookups
        produce a placeholder line rather than raising.

    Note:
        Each block is capped at ~8 KB so the total payload stays well
        below the Coder's 60 KB diff cap even with several failed jobs.

        ``fetch_ci_status`` accepts ``gh pr checks`` exit code 8 in
        addition to 0 because the CLI exits 8 when required checks
        aren't all green but still emits the rows on stdout — both
        codes carry parseable JSON.
    """
    out: list[str] = []
    for row in failed_rows:
        name = row.get("name") or "?"
        link = row.get("link") or ""
        m = _RUN_LINK_RE.search(link)
        if m is None:
            out.append(f"### {name}\n_(no log link found in: {link!r})_")
            continue
        run_id = m.group(1)
        job_id = m.group(2)
        argv = ["run", "view", run_id, "--log-failed"]
        if job_id:
            argv += ["--job", job_id]
        res = gh._run(argv)
        if not res.ok:
            out.append(f"### {name}\n_(gh run view failed: {res.stderr[:200]})_")
            continue
        out.append(f"### {name}\n{res.stdout[-8000:]}")
    return out
