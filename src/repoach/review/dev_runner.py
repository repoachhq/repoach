"""Plan-driven dev session: spec → plan → step-by-step Developer → push → PR.

The runner is the unit invoked by ``ferova develop <spec-id>``.
SP-DEV-PLAN-EXEC rebuilt it around the action plan (architecture
decisions #2/#3/#4 — the monolithic 32K one-shot dispatch, its
500-LOC cliff and its escaping lottery are gone):

1. Load the spec via :func:`load_spec`; create/switch the branch.
2. **Plan first** — :func:`load_or_produce_plan` reads
   ``docs/plans/<SP-ID>.md`` or runs one Planner session, then
   :func:`commit_plan_document` makes the plan the branch's first
   commit.
3. **One step at a time** — :func:`execute_plan_step` dispatches the
   Developer on a step-scoped brief (:func:`build_step_brief`),
   enforces the step's file contract plus the path whitelist, runs
   the per-step gates (syntax → ruff → the step's promised unit
   tests via :func:`run_promised_tests`, which first attempts the
   exact promised selectors then falls back to file level when
   selector names differ), retries once with the gate output fed
   back, and commits each green step with the plan's own commit
   message.  A second red stops the session loudly; earlier green
   commits stay.
4. **Wrap up** — full unit suite, the plan's integration tests when
   their files exist, one push (:func:`push_branch`).  The PR is the
   CLI's responsibility via :func:`open_pr`.

The runner never raises on a gate failure — it returns a structured
:class:`DevSessionResult` and the CLI maps ``no_op_reason`` keywords
to exit codes.

Module-level constants :
    :data:`DEFAULT_BRANCH_TEMPLATE` is the canonical branch-name
    template used when the caller does not pass an explicit
    ``branch=`` kwarg.
"""

from __future__ import annotations

import contextlib
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ..core.config import get_settings
from ..core.logging import get_logger
from ..lint.no_inline_comments import scan_file as scan_inline_comments
from ..lint.no_silent_except import scan_file as scan_silent_except
from .coder_loop import (
    run_pytest_matrix,
    run_ruff_gate,
)
from .decompose import Decomposer, decompose_spec, render_sub_spec_markdown
from .devagent_selfverify import ComplianceJudge, make_compliance_judge, run_self_verify
from .devagent_tools import normalise_contract
from .gh_client import GhCli
from .governed_spec import load_governed_spec
from .import_gate import check_imports
from .inline_comment_heal import heal_inline_comments
from .persistence import init_schema, record_coder_response
from .plan import ActionPlan, PlanStep, load_plan, plan_relpath
from .reviewer import Developer
from .secret_env import scrubbed_env
from .spec import SpecPlan, load_spec
from .spec_gate import promised_present, selector_present
from .spec_supersede import supersede_parent_on_decompose
from .wrapup_attribution import StepCommit, attribute_failure_to_step

_log = get_logger(__name__)


def _promised_test_files(unit_tests: list[str]) -> set[str]:
    """Return the set of repo-relative file paths extracted from promised selectors.

    Each selector is split on ``::`` and the file part (index 0) is kept.
    Bare-file selectors that contain no ``::`` are skipped — only selectors
    that name an explicit node id contribute to the returned set.

    Args:
        unit_tests: Promised pytest selectors from a plan step.

    Returns:
        Set of repo-relative file paths whose promised selectors include
        a ``::`` node-id portion.
    """
    return {s.split("::", 1)[0] for s in unit_tests if "::" in s}


def _test_function_names_in_file(repo_root: Path, file_path: str) -> list[str]:
    """Return the ``def test_*`` function names defined in *file_path*.

    Args:
        repo_root: Repository root the *file_path* resolves against.
        file_path: Repo-relative path to the test file.

    Returns:
        Sorted list of test function names (without the ``def`` prefix or
        trailing parenthesis). Empty when the file is missing or unreadable.
    """
    target = (repo_root / file_path).resolve()
    try:
        source = target.read_text(encoding="utf-8")
    except OSError as exc:
        _log.warning(
            "dev_runner.test_function_names_unreadable",
            repo_root=str(repo_root),
            file_path=file_path,
            error=str(exc)[:200],
        )
        return []
    return sorted(set(re.findall(r"(?m)^\s*(?:async\s+)?def\s+(test_\w+)\s*\(", source)))


def _attempt_mechanical_rename(
    repo_root: Path,
    file_path: str,
    promised: list[str],
    delivered: list[str],
) -> tuple[str, str]:
    """Attempt to mechanically rename one delivered test function to its promised name.

    When exactly one promised node id is missing from *file_path* and exactly
    one test function is present in the file that no plan step promises, the
    function is renamed by rewriting ``def <delivered>(...)`` to
    ``def <promised>(...)`` in place.  Decorators and body are preserved.

    The three outcomes are distinct because the gate must treat them
    differently (SP-DEV-PROMISE-DELIVERY Errors: a failed rename falls
    back to the retryable G1 gate, while an ambiguous drift keeps the
    reconciled-accept — the first implementation conflated them into
    one ``False`` and the judge refused the push, 2026-07-05).

    Args:
        repo_root: Repository root the *file_path* resolves against.
        file_path: Repo-relative path to the test file.
        promised: Node-id names (the ``::name`` part) the step promised.
        delivered: Test function names actually defined in the file that no
            plan step promises.

    Returns:
        ``("renamed", original_content)`` when the rename succeeded (the
        caller restores *original_content* if the strict re-run stays
        red); ``("ambiguous", "")`` when the drift is not one-to-one
        (file untouched); ``("error", "")`` on any parse/IO failure
        (the file is restored to its original content when a write
        already happened).
    """
    if len(promised) != 1 or len(delivered) != 1:
        return "ambiguous", ""

    target = (repo_root / file_path).resolve()
    try:
        original = target.read_text(encoding="utf-8")
    except OSError:
        return "error", ""

    promised_name = promised[0]
    delivered_name = delivered[0]

    rewritten = original.replace(f"def {delivered_name}(", f"def {promised_name}(")
    if rewritten == original:
        return "error", ""

    try:
        compile(rewritten, str(target), "exec")
    except SyntaxError:
        return "error", ""

    try:
        target.write_text(rewritten, encoding="utf-8")
    except OSError:
        with contextlib.suppress(OSError):
            target.write_text(original, encoding="utf-8")
        return "error", ""

    return "renamed", original


def _restore_file_contents(repo_root: Path, originals: dict[str, str]) -> None:
    """Write back pre-rename contents after a failed mechanical rename.

    SP-DEV-PROMISE-DELIVERY's failure contract: a rename whose strict
    re-run stays red (or that errors midway across several files) must
    not leave mechanically-renamed content in the retry's working tree.
    """
    for file_path, content in originals.items():
        with contextlib.suppress(OSError):
            (repo_root / file_path).write_text(content, encoding="utf-8")


DEFAULT_BRANCH_TEMPLATE: str = "feat/sp-{slug}-impl"

_MAX_STEP_ATTEMPTS: int = 3
"""Per-step attempt cap.

Two attempts for every step; a third runs **only** when the prior attempt
failed a lint-class gate (ruff or the repo lint gate), where the model is
usually one nudge from green. A non-lint failure (syntax, import, tests,
commit) stops after two attempts, so a genuinely broken step costs no extra
dispatch latency (SP-DEV-STEP-LOOP-HARDEN).
"""

WRAPUP_REPAIR_ATTEMPTS: int = 1
"""Bounds the wrap-up repair loop per introducing step (SP-DEV-WRAPUP-ATTRIBUTION)."""

_LINT_GATE_KIND: str = "lint"
_OTHER_GATE_KIND: str = "other"


@dataclass
class DevSessionResult:
    """Outcome of one :func:`run_developer_session` invocation.

    SP-DEV-PLAN-EXEC adds the plan-execution tracking fields:
    ``steps_total`` / ``steps_completed`` count the plan's steps,
    ``failed_step_index`` names the step that stopped the session
    (``None`` when every step landed), ``plan_committed`` records
    whether the rendered plan document is the branch's first commit.

    SP-DEVAGENT-WIRE (slice 5) adds the decomposition + PR-handoff fields:
    ``decomposed`` / ``sub_spec_ids`` record a real multi-way split (the parent is
    superseded), and ``pr_title`` / ``pr_summary`` carry the parent's title / summary
    captured **before** any supersession so the PR can be opened without re-loading a
    deleted parent spec file.

    SP-DEV-WRAPUP-ATTRIBUTION adds ``wrapup_dossier``, carrying the attribution
    block (introducing step / pre-existing / error per failing selector) for
    a wrap-up that stayed red after the bounded repair attempt.
    """

    spec_id: str
    branch: str = ""
    fixes_applied: int = 0
    fixes_rejected: int = 0
    rejected_paths: list[str] = field(default_factory=list)
    ruff_passed: bool = False
    pytest_passed: bool = False
    pushed: bool = False
    pr_url: str = ""
    no_op_reason: str | None = None
    steps_total: int = 0
    steps_completed: int = 0
    failed_step_index: int | None = None
    plan_committed: bool = False
    self_verified: bool = False
    decomposed: bool = False
    sub_spec_ids: list[str] = field(default_factory=list)
    pr_title: str = ""
    pr_summary: str = ""
    wrapup_dossier: str = ""


def read_existing_files(plan: SpecPlan, *, repo_root: Path | None = None) -> dict[str, str]:
    """Return ``{path: contents}`` for every existing file referenced by *plan*.

    Paths in :attr:`SpecPlan.referenced_paths` are repo-relative
    strings.  Only existing files are returned — paths the Developer
    will create are skipped here (they're left to the Developer to
    populate from scratch).

    Args:
        plan: Loaded spec plan.
        repo_root: Repository root (defaults to ``Path.cwd()``).

    Returns:
        Mapping ``{path: contents}``.  Files with read errors are
        silently skipped — the Developer will see the omission and
        ask itself whether the path needs to be created.

    Paths that escape the repository (absolute paths, traversal
    sequences) are filtered out as a defence-in-depth check before
    any read happens.
    """
    root = (repo_root or Path.cwd()).resolve()
    out: dict[str, str] = {}
    for path in plan.referenced_paths:
        target = (root / path).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            _log.debug("dev_runner.path_outside_root", path=path, root=str(root))
            continue
        if not target.is_file():
            continue
        try:
            out[path] = target.read_text(encoding="utf-8")
        except OSError as exc:
            _log.warning("dev_runner.read_failed", path=path, error=str(exc))
    return out


def render_repo_tree(repo_root: Path | None = None, *, max_lines: int = 80) -> str:
    """Render a small ``find src/ tests/`` tree as orientation for the Developer.

    Capped at ``max_lines`` so the prompt budget stays under control.
    """
    root = (repo_root or Path.cwd()).resolve()
    bin_find = shutil.which("find") or "find"
    proc = subprocess.run(
        [bin_find, "src", "tests", "-type", "d", "-maxdepth", "4"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if proc.returncode != 0:
        return "_(repo tree unavailable)_"
    lines = sorted(set(proc.stdout.splitlines()))
    if len(lines) > max_lines:
        lines = [*lines[:max_lines], f"# [... {len(lines) - max_lines} more directories ...]"]
    return "\n".join(lines)


def check_python_syntax(repo_root: Path, paths: list[str]) -> tuple[bool, str]:
    """Compile every ``.py`` path under *repo_root* — fail fast on syntax errors.

    Runs after the agentic loop and BEFORE the ruff gate so a Developer
    that wrote syntactically-invalid Python is caught immediately with
    a crisp ``<file:line: SyntaxError: msg>`` message instead of the
    verbose ruff ``invalid-syntax`` output.

    Args:
        repo_root: Repository root the paths are relative to.
        paths: Repo-relative paths the Developer wrote.  Non-``.py``
            paths are skipped silently.

    Returns:
        ``(True, "")`` when every Python file parses, or
        ``(False, "<path>:<line>: <SyntaxError msg>")`` on the first
        failure.  We stop at the first failure so the message is
        focused.
    """
    root = repo_root.resolve()
    for path in paths:
        if not path.endswith(".py"):
            continue
        target = (root / path).resolve()
        if not target.is_file():
            continue
        try:
            source = target.read_text(encoding="utf-8")
        except OSError as exc:
            return False, f"{path}: read error: {exc}"
        try:
            compile(source, str(target), "exec")
        except SyntaxError as exc:
            return False, f"{path}:{exc.lineno}: SyntaxError: {exc.msg}"
    return True, ""


def spec_to_branch_slug(spec_id: str) -> str:
    """Return the branch-name slug for a spec id.

    ``SP-SEC`` → ``sec``; ``SP-PB1`` → ``pb1``; ``SP-DEV`` → ``dev``.
    """
    s = spec_id.upper()
    if s.startswith("SP-"):
        s = s[3:]
    return s.lower()


def ensure_branch(branch: str, *, base: str = "develop", repo_root: Path | None = None) -> bool:
    """Create or check out *branch* off *base*.

    Args:
        branch: Target branch name.
        base: Source ref to branch from when creating.
        repo_root: Repo working tree.

    Returns:
        ``True`` when the branch is ready (created or already current),
        ``False`` on a git error.
    """
    git = shutil.which("git") or "git"
    root = (repo_root or Path.cwd()).resolve()
    proc = subprocess.run(
        [git, "switch", branch], cwd=root, capture_output=True, text=True, check=False
    )
    if proc.returncode == 0:
        return True
    proc = subprocess.run(
        [git, "switch", "-c", branch, base],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        _log.warning(
            "dev_runner.ensure_branch_failed",
            branch=branch,
            base=base,
            stderr=proc.stderr[:200],
        )
        return False
    return True


def _run_git(
    repo_root: Path, *args: str, timeout_s: int = 60, tail_chars: int = 400
) -> tuple[int, str]:
    """Run one git command in *repo_root*, return ``(rc, output_tail)``.

    The default tail cap suits error tails fed back to models. A caller
    that PARSES the output must pass ``tail_chars=0`` (uncapped): the
    resume fast path grepped commit subjects through the 400-char tail
    and silently beheaded the newest subjects once a session branch
    grew past ~7 commits — SP-AGENT-THINKING-CONTROL re-paid a 683k
    token dispatch for a step whose commit sat at the top of the log
    (2026-07-04).
    """
    git = shutil.which("git") or "git"
    proc = subprocess.run(
        [git, *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    output = (proc.stdout + proc.stderr).strip()
    return proc.returncode, output[-tail_chars:] if tail_chars else output


def commit_all(repo_root: Path, message: str) -> tuple[bool, str]:
    """Stage everything and commit; ``(False, why)`` when nothing staged.

    Commit-only sibling of :func:`coder_loop.git_commit_and_push` —
    the step executor commits once per green step and pushes a single
    time at wrap-up, so the two phases must be separable.
    """
    rc, out = _run_git(repo_root, "add", "-A")
    if rc != 0:
        return False, f"git add failed: {out}"
    rc, out = _run_git(repo_root, "diff", "--cached", "--name-only")
    if rc != 0 or not out.strip():
        return False, "nothing to commit"
    rc, out = _run_git(repo_root, "commit", "-m", message)
    if rc != 0:
        return False, f"git commit failed: {out}"
    return True, "committed"


def _step_already_committed(repo_root: Path, step: PlanStep) -> bool:
    """True when the step's exact commit subject already sits on the branch.

    Session resume support: a multi-step session that fails at step N
    used to re-dispatch steps 1..N-1 on the next run — the Developer
    then found nothing left to write and the whole session died on the
    already-done step (observed on SP-ORCH-DOCSTRING attempt 4→5 and,
    as pure token waste, on the SP-FINDINGS-BRIDGE-DOCFIX re-runs:
    ~397k tokens re-paid for a committed step). Plan commit subjects
    are specific enough to key on; the range is bounded to the session
    branch (``origin/develop..HEAD``) so a coincidental subject on the
    base can never skip real work.
    """
    rc, out = _run_git(repo_root, "log", "--format=%s", "origin/develop..HEAD", tail_chars=0)
    if rc != 0:
        rc, out = _run_git(repo_root, "log", "--format=%s", "-n", "30", "HEAD", tail_chars=0)
        if rc != 0:
            return False
    return step.commit_message in out.splitlines()


def step_preflight_complete(repo_root: Path, plan: ActionPlan, step: PlanStep) -> bool:
    """Return True when every file in the step exists and its promised tests are green.

    Builds a test selector set from the step's ``unit_tests`` plus every
    ``plan.integration_tests`` selector whose file path (the substring before
    ``::``, or the whole selector when no ``::`` is present) is contained in
    ``step.files``.  Returns ``False`` when the selector set is empty (nothing
    mechanical to prove completion).  Returns ``False`` when any path in
    ``step.files`` does not exist on disk under *repo_root*.  Otherwise calls
    :func:`run_promised_tests` inside a broad try/except — any exception
    (pytest crash, timeout, git error) logs
    ``dev_runner.step_preflight_error`` and returns ``False`` (fail-open to a
    normal dispatch).

    A RECONCILED green is not proof: the file-level fallback exists for the
    step gate, where the Developer just wrote tests under drifted names. At
    preflight time the same fallback certified two untouched steps of
    SP-AGENT-THINKING-CONTROL as complete because an earlier step's
    unrelated tests in the same promised file were green (2026-07-04) —
    only a strict per-selector green counts here.

    Selectors run in one pytest invocation PER TEST TREE (``tests/unit``
    vs ``tests/integration``, mirroring how CI invokes them): a plan may
    promise the same basename in both trees, and a single combined
    invocation dies on the rootdir module-name collision — observed on
    SP-AGENT-THINKING-CONTROL step 4, whose fully-delivered work
    preflighted red for that reason alone.

    Every ``::`` node id must also resolve statically
    (:func:`selector_present`) BEFORE pytest runs: pytest silently
    swallows a missing node id when the same file is also listed bare
    in the invocation (exit 0), which certified SP-BUDGET-RETRY-FIXES
    step 3 complete while its three promised tests did not exist —
    the bare-file integration promise in the same attributed set
    masked them (2026-07-04).

    Args:
        repo_root: Repository working tree root.
        plan: The action plan for integration-test attribution.
        step: The plan step to preflight.

    Returns:
        ``True`` when the step is mechanically provable as complete.
    """
    attributed: list[str] = list(step.unit_tests)
    for selector in plan.integration_tests:
        file_path = selector.split("::", 1)[0]
        if file_path in step.files:
            attributed.append(selector)

    if not attributed:
        return False

    for file_path in step.files:
        if not (repo_root / file_path).is_file():
            return False

    node_selectors = [s for s in attributed if "::" in s]
    absent_nodes = [s for s in node_selectors if not selector_present(repo_root, s)]
    if absent_nodes:
        _log.info(
            "dev_runner.step_preflight_selector_absent",
            step=step.index,
            selectors=absent_nodes,
        )
        return False

    tree_groups: dict[str, list[str]] = {}
    for selector in attributed:
        tree = "integration" if selector.startswith("tests/integration/") else "unit"
        tree_groups.setdefault(tree, []).append(selector)

    try:
        for selectors in tree_groups.values():
            ok, _tail, reconciled = run_promised_tests(repo_root, selectors)
            if ok and reconciled:
                _log.info(
                    "dev_runner.step_preflight_reconciled_not_proof",
                    step=step.index,
                    selectors=selectors,
                )
                return False
            if not ok:
                return False
        return True
    except Exception as exc:
        _log.warning("dev_runner.step_preflight_error", error=str(exc))
        return False


def commit_paths(repo_root: Path, paths: list[str], message: str) -> tuple[bool, str]:
    """Stage exactly *paths* and commit; ``(False, why)`` when nothing staged.

    Targeted sibling of :func:`commit_all` for the step executor: a
    session may start with pre-existing untracked files in the tree
    (an uncommitted spec draft, a stray lockfile — the
    SP-FINDINGS-BRIDGE-DOCFIX attempt-1 incident), and ``git add -A``
    would silently sweep them into the step's commit. Staging the
    step's own changed paths keeps foreign files out of factory
    commits.
    """
    if not paths:
        return False, "nothing to commit"
    rc, out = _run_git(repo_root, "add", "--", *paths)
    if rc != 0:
        return False, f"git add failed: {out}"
    rc, out = _run_git(repo_root, "diff", "--cached", "--name-only")
    if rc != 0 or not out.strip():
        return False, "nothing to commit"
    rc, out = _run_git(repo_root, "commit", "-m", message)
    if rc != 0:
        return False, f"git commit failed: {out}"
    return True, "committed"


def push_branch(repo_root: Path, branch: str) -> tuple[bool, str]:
    """Push ``HEAD`` to ``origin/<branch>``; push-only wrap-up sibling."""
    rc, out = _run_git(repo_root, "push", "origin", f"HEAD:{branch}", timeout_s=180)
    if rc != 0:
        return False, f"git push failed: {out}"
    return True, "pushed"


def load_or_produce_plan(
    spec: SpecPlan,
    *,
    repo_root: Path,
    planner: object | None = None,
    explore_via: Literal["proxy", "claude_cli"] = "proxy",
    cc_model: str = "sonnet",
) -> tuple[ActionPlan | None, str | None]:
    """Return the spec's action plan, producing it when absent.

    SP-DEV-PLAN-EXEC step 1: the session is plan-first. An existing
    ``docs/plans/<SP-ID>.md`` wins (the operator or a previous Planner
    session wrote it); otherwise one Planner session runs. Both paths
    re-read through :func:`load_plan` so the executor only ever
    consumes a validated document.

    Args:
        spec: Loaded spec the session runs for.
        repo_root: Working tree root.
        planner: Optional pre-built Planner (tests inject one with a
            fake loop; default builds the real agent).
        explore_via: Exploration backend for the produce path
            (``"proxy"`` or ``"claude_cli"``); inert when a committed
            plan already exists. SP-DEV-CC-WIRE.
        cc_model: CLI model alias for ``"claude_cli"`` mode.

    Returns:
        ``(plan, error)`` — exactly one side is set.
    """
    try:
        return load_plan(spec.id, root=repo_root), None
    except FileNotFoundError:
        _log.info("dev_runner.plan_absent_producing", spec_id=spec.id)
    except ValueError as exc:
        return None, f"committed plan is invalid: {str(exc)[:300]}"

    from .planner import run_planner_session

    outcome = run_planner_session(
        spec.id,
        root=repo_root,
        planner=planner,
        explore_via=explore_via,
        cc_model=cc_model,
    )
    if not outcome.written:
        return None, f"planning failed: {outcome.error}"
    try:
        return load_plan(spec.id, root=repo_root), None
    except (FileNotFoundError, ValueError) as exc:
        return None, f"planner wrote an unloadable plan: {str(exc)[:300]}"


def commit_plan_document(repo_root: Path, spec_id: str) -> tuple[bool, str]:
    """Commit ``docs/plans/<SP-ID>.md`` as its own commit when dirty.

    Returns ``(committed_or_already_clean, detail)`` — a plan that is
    already part of history is success, not a failure.
    """
    relpath = plan_relpath(spec_id)
    rc, out = _run_git(repo_root, "status", "--porcelain", "--", relpath)
    if rc != 0:
        return False, f"git status failed: {out}"
    if not out.strip():
        return True, "plan already committed"
    rc, out = _run_git(repo_root, "add", "--", relpath)
    if rc != 0:
        return False, f"git add failed: {out}"
    rc, out = _run_git(repo_root, "commit", "-m", f"docs(plan): {spec_id} action plan")
    if rc != 0:
        return False, f"git commit failed: {out}"
    return True, "plan committed"


@dataclass
class StepOutcome:
    """Result of one :func:`execute_plan_step` invocation."""

    ok: bool
    reason: str = ""
    fixes_applied: int = 0
    fixes_rejected: int = 0
    rejected_paths: list[str] = field(default_factory=list)


_BRIEF_SPEC_CAP_CHARS = 12_000
"""Cap on the verbatim spec section appended to step briefs
(SP-DEV-STEP-CONTEXT) — context completeness is the dominant
reliability lever, but the brief must stay bounded."""

_EMBED_PER_FILE_CAP = 12_000
"""Per-file character cap for contract file embedding
(SP-DEV-BRIEF-FILE-CONTENT).  Files larger than this are truncated
head-first with a ``read_file`` continuation note."""

_EMBED_TOTAL_BUDGET = 48_000
"""Total character budget across all embedded contract file contents
(SP-DEV-BRIEF-FILE-CONTENT).  Once exhausted, remaining files are
listed by name with 'read on demand' notes."""


def _embed_contract_files(
    contract_paths: list[str],
    repo_root: Path | None = None,
) -> str:
    """Return a formatted string embedding contract file contents for the step brief.

    Two sections are produced:

    1. **Existing contract files** — for each *contract_paths* entry
       that exists on disk, the file's UTF-8 content is embedded under
       a ``### `path``` heading.  Content is capped at
       :data:`_EMBED_PER_FILE_CAP` chars per file; oversized files are
       truncated head-first with a continuation note naming the exact
       ``read_file(path, start_line=N)`` call.  A total budget of
       :data:`_EMBED_TOTAL_BUDGET` chars is enforced across all
       embedded content; once exhausted, remaining existing files are
       listed by name with "read on demand" notes.  Read errors are
       listed with the error string (mirroring
       :func:`read_existing_files`).

    2. **Files to create** — every *contract_paths* entry that does
       not exist on disk (or whose resolved path escapes the repo
       root) is listed under a ``## Files to create`` heading.

    Path resolution uses the same jail-safe pattern as
    :func:`read_existing_files`: resolve against *repo_root*, then
    verify the result is inside *repo_root* via ``relative_to``.

    Args:
        contract_paths: Repo-relative paths from the step's file contract.
        repo_root: Repository root (defaults to ``Path.cwd()``).

    Returns:
        Formatted markdown string suitable for appending to a step brief.
    """
    root = (repo_root or Path.cwd()).resolve()

    existing: list[tuple[str, str]] = []
    to_create: list[str] = []

    for path in contract_paths:
        target = (root / path).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            _log.warning(
                "dev_runner.embed_path_outside_root",
                path=path,
                root=str(root),
            )
            to_create.append(path)
            continue
        if not target.is_file():
            to_create.append(path)
            continue
        try:
            raw = target.read_text(encoding="utf-8")
        except OSError as exc:
            _log.warning(
                "dev_runner.embed_read_failed",
                path=path,
                error=str(exc)[:200],
            )
            existing.append((path, f"[read error: {exc}]"))
            continue

        if len(raw) > _EMBED_PER_FILE_CAP:
            truncated = raw[:_EMBED_PER_FILE_CAP]
            next_line = truncated.count("\n") + 1
            continuation_note = (
                f"\n\n[... truncated at {_EMBED_PER_FILE_CAP} chars; "
                f"continue with read_file('{path}', start_line={next_line}) ...]"
            )
            existing.append((path, truncated + continuation_note))
        else:
            existing.append((path, raw))

    sections: list[str] = []
    budget_used = 0
    budget_exhausted = False

    if existing:
        sections.append("## Existing contract files")
        sections.append("")
        for path, content in existing:
            if budget_exhausted:
                sections.append(f"### `{path}` (read on demand — budget exhausted)")
                sections.append("")
                continue
            content_len = len(content)
            if budget_used + content_len > _EMBED_TOTAL_BUDGET:
                budget_exhausted = True
                sections.append(f"### `{path}` (read on demand — budget exhausted)")
                sections.append("")
                continue
            sections.append(f"### `{path}`")
            sections.append("")
            sections.append("```")
            sections.append(content)
            sections.append("```")
            sections.append("")
            budget_used += content_len

    if to_create:
        sections.append("## Files to create")
        sections.append("")
        for path in to_create:
            sections.append(f"- `{path}`")
        sections.append("")

    return "\n".join(sections).rstrip() + "\n"


def run_repo_lint_gates(repo_root: Path, paths: list[str]) -> tuple[bool, str]:
    """Run the repo's own lint gates on the step's contract paths.

    SP-DEV-STEP-CONTEXT: the pre-commit hook enforces the
    no-inline-comments and no-silent-except rules anyway — running
    them HERE turns a late, opaque ``git commit gate`` failure into
    early, directive feedback the retry can act on.

    Returns:
        ``(True, "")`` when clean, else ``(False, report)`` with one
        line per violation plus the house-rule reminder.
    """
    issues: list[str] = []
    for path_str in paths:
        target = repo_root / path_str
        if target.suffix != ".py" or not target.is_file():
            continue
        for violation in scan_inline_comments(target):
            issues.append(f"inline comment forbidden: {violation.format()}")
        for violation in scan_silent_except(target):
            issues.append(f"silent except forbidden: {violation.format()}")
    if issues:
        reminder = (
            "House rules: ZERO inline comments (docstrings carry the why); "
            "every swallowed exception needs a _log.warning(...) before the "
            "trailer, or raise instead."
        )
        return False, "\n".join([*issues, reminder])
    return True, ""


def build_step_brief(
    plan: ActionPlan,
    step: PlanStep,
    *,
    gate_feedback: str = "",
    spec_markdown: str = "",
    arch_owns: str = "",
) -> str:
    """Render the step-scoped markdown brief the Developer receives.

    The brief carries the plan context, this step's action, the file
    contract, the verifiable completion criterion, the promised tests,
    — SP-DEV-STEP-CONTEXT — the source spec verbatim (capped at
    :data:`_BRIEF_SPEC_CAP_CHARS`), so a plan action that references
    "the spec" is resolvable instead of a dead pointer, —
    SP-ARCH-DEV-WIRE — the governed architecture contract (``arch_owns``,
    empty for a frontier/no-spec run) so the Developer imports only
    declared dependencies and passes the CI edge-honesty gate first try,
    and — SP-DEV-BRIEF-FILE-CONTENT — the current content of every
    existing contract file embedded under clear headings, with missing
    paths listed under a 'to create' heading.  The retry variant
    (``gate_feedback`` non-empty) re-reads from disk so the loop's
    previous writes are visible.
    """
    lines = [
        f"# {plan.spec_id} — step {step.index}/{len(plan.steps)}: {step.title}",
        "",
        f'Part of the action plan "{plan.title}": {plan.summary}',
        "",
        "## Your step",
        "",
        step.action,
        "",
        "## File contract — the ONLY paths you may create or modify",
        "",
    ]
    lines += [f"- `{f}`" for f in step.files]
    lines += [
        "",
        "Any fix outside these paths is rejected by the runner.",
        "",
        "## Definition of Done",
        "",
        f"- {step.done_when}",
    ]
    if arch_owns:
        lines += ["", arch_owns]
    if step.unit_tests:
        lines += ["", "## Tests that must pass", ""]
        lines += [f"- `{t}`" for t in step.unit_tests]
    if gate_feedback:
        lines += [
            "",
            "## Previous attempt failed its gates — fix this",
            "",
            "```",
            gate_feedback[:2000],
            "```",
        ]
    contract_section = _embed_contract_files(step.files)
    if contract_section.strip():
        lines += [
            "",
            "## Contract files",
            "",
            contract_section.rstrip(),
        ]
    if spec_markdown:
        lines += [
            "",
            "## Source spec (verbatim — the plan's authority)",
            "",
            spec_markdown[:_BRIEF_SPEC_CAP_CHARS],
        ]
    return "\n".join(lines)


def run_pytest_selectors(repo_root: Path, selectors: list[str]) -> tuple[bool, str]:
    """Run pytest on explicit selectors, return ``(green, output_tail)``.

    Selectors starting with ``-`` are refused outright (defence in
    depth behind the plan-form validator): spliced into argv they
    would act as pytest options, and plans are LLM-generated.
    """
    import sys

    hostile = [s for s in selectors if s.lstrip().startswith("-")]
    if hostile:
        return False, f"refused flag-like test selector(s): {hostile}"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *selectors],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
        env=scrubbed_env(),
    )
    output = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, output[-400:]


def run_promised_tests(repo_root: Path, selectors: list[str]) -> tuple[bool, str, bool]:
    """Run promised test selectors with name-mismatch reconciliation.

    Tries the exact promised selectors first; on failure falls back to
    the file level so that a test renamed after planning still counts
    as green (the function was delivered, just under a different name).
    The hostile-selector guard is inherited from :func:`run_pytest_selectors`.

    Args:
        repo_root: Repository root the selectors are relative to.
        selectors: Pytest selectors of the form ``file.py::test_name``
            (or bare file paths).  Flag-like values are refused by the
            inner :func:`run_pytest_selectors` call.

    Returns:
        ``(green, output_tail, reconciled)`` where ``reconciled=True``
        means the exact selectors failed but the file-level run passed
        (name mismatch — the implementation is green but under a
        different function name).
    """
    exact_ok, exact_tail = run_pytest_selectors(repo_root, selectors)
    if exact_ok:
        return True, exact_tail, False

    file_paths = sorted({s.split("::", 1)[0] for s in selectors})
    file_ok, file_tail = run_pytest_selectors(repo_root, file_paths)
    if file_ok:
        return True, file_tail, True

    return False, file_tail, False


def _changed_paths(repo_root: Path) -> list[str]:
    """Return repo-relative paths changed (tracked or untracked) since HEAD.

    Reads ``git status --porcelain --untracked-files=all`` so both modified-tracked
    files and the brand-new untracked files the agentic loop wrote are captured (a
    plain ``git diff`` would miss the latter), and so a wholly-untracked new
    directory is reported file-by-file rather than collapsed to the directory entry.
    A rename reports its destination path. Used by :func:`execute_plan_step` to
    enforce the step's file contract and to size the step's commit after the loop.
    Returns ``[]`` on a git error.
    """
    git = shutil.which("git") or "git"
    proc = subprocess.run(
        [git, "status", "--porcelain", "--untracked-files=all"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        return []
    paths: list[str] = []
    for line in proc.stdout.splitlines():
        if len(line) <= 3:
            continue
        entry = line[3:]
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        paths.append(entry.strip().strip('"'))
    return paths


def _run_selector_at_commit(repo_root: Path, sha: str, selector: str) -> bool:
    """Run *selector* at *sha* in a throwaway git worktree; return whether green.

    Creates a detached worktree via ``git worktree add --detach`` at a fresh
    ``tempfile.mkdtemp`` directory, runs :func:`run_pytest_selectors` inside it,
    and always tears the worktree down in a ``finally`` block — the session's
    own checkout is never disturbed (SP-DEV-WRAPUP-ATTRIBUTION G2).

    Args:
        repo_root: The session's repository root (the worktree's origin).
        sha: The commit to check out into the temporary worktree.
        selector: The pytest selector to run there.

    Returns:
        ``True`` when the selector is green at *sha*, ``False`` when red.

    Raises:
        RuntimeError: When ``git worktree add`` fails; the message carries
            git's combined stdout/stderr tail.
    """
    tmp_dir = tempfile.mkdtemp(prefix="ferova-wrapup-")
    try:
        rc, out = _run_git(repo_root, "worktree", "add", "--detach", tmp_dir, sha)
        if rc != 0:
            raise RuntimeError(out)
        green, _tail = run_pytest_selectors(Path(tmp_dir), [selector])
        return green
    finally:
        _run_git(repo_root, "worktree", "remove", "--force", tmp_dir)
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _step_commits_for_plan(repo_root: Path, plan: ActionPlan, base: str) -> list[StepCommit]:
    """Resolve the plan's ordered step commits (base first) on the current branch.

    Args:
        repo_root: The session's repository root.
        plan: The action plan whose steps' commit messages are matched
            against the branch's commit log.
        base: The ref the implementation branch was created from.

    Returns:
        ``[StepCommit(0, "plan base", merge_base_sha), ...]`` followed by one
        :class:`StepCommit` per plan step whose ``commit_message`` matches a
        commit subject reachable from the merge-base to ``HEAD``, in plan
        order. Empty list when the merge-base cannot be resolved.
    """
    rc, merge_base = _run_git(repo_root, "merge-base", "HEAD", base, tail_chars=0)
    if rc != 0:
        return []
    merge_base_sha = merge_base.strip()
    commits = [StepCommit(index=0, title="plan base", sha=merge_base_sha)]

    _rc, log_out = _run_git(
        repo_root, "log", "--format=%H%x1f%s", "--reverse", f"{merge_base_sha}..HEAD", tail_chars=0
    )
    parsed: list[tuple[str, str]] = []
    for line in log_out.splitlines():
        if "\x1f" not in line:
            continue
        sha, subject = line.split("\x1f", 1)
        parsed.append((sha, subject))

    for step in plan.steps:
        for sha, subject in parsed:
            if step.commit_message == subject:
                commits.append(StepCommit(index=step.index, title=step.title, sha=sha))
                break

    return commits


def _promised_selectors_for_plan(plan: ActionPlan) -> set[str]:
    """Return every selector any part of *plan* promises, plan- and step-level.

    Args:
        plan: The action plan whose promised selectors bound wrap-up
            attribution — a failure on one of these selectors is the
            existing loop's job, not the attribution path's.

    Returns:
        Union of :attr:`ActionPlan.integration_tests` and every step's
        :attr:`PlanStep.unit_tests`.
    """
    return set(plan.integration_tests) | {t for step in plan.steps for t in step.unit_tests}


def _collect_failing_wrapup_selectors(repo_root: Path, promised: set[str]) -> list[str]:
    """Run the full unit suite and return failing selectors that no step promised.

    Args:
        repo_root: The session's repository root.
        promised: Every selector promised by the plan (see
            :func:`_promised_selectors_for_plan`) — excluded from the result
            since those failures are the existing loop's responsibility.

    Returns:
        Sorted list of ``FAILED`` selectors from ``pytest -rf`` output that
        are not in *promised*.
    """
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=no", "-rf", "tests/unit"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
        env=scrubbed_env(),
    )
    failing: set[str] = set()
    for line in proc.stdout.splitlines():
        if not line.startswith("FAILED "):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        failing.add(parts[1])
    return sorted(failing - promised)


def repair_wrapup_failures(
    repo_root: Path,
    plan: ActionPlan,
    *,
    dev: Developer,
    db: Path,
    base: str,
    failing_selectors: list[str],
    result: DevSessionResult,
) -> str | None:
    """Attribute each wrap-up failure to its introducing step and repair it.

    SP-DEV-WRAPUP-ATTRIBUTION G2/G3: builds the plan's ordered step commits
    via :func:`_step_commits_for_plan`, then for every selector in
    *failing_selectors* calls :func:`attribute_failure_to_step` with a real
    git-worktree selector runner (:func:`_run_selector_at_commit`).

    A ``pre_existing`` outcome (the selector was already red at the plan
    base) is recorded in the dossier and never repaired — the failure
    predates the session (NG3). An ``error`` outcome, or any exception
    escaping the attribution call itself, is also recorded and left
    unrepaired, failing closed rather than guessing an attribution. An
    ``introduced_by_step`` outcome dispatches up to
    :data:`WRAPUP_REPAIR_ATTEMPTS` bounded repair attempts to the existing
    Developer loop, scoped to the introducing step's file contract plus the
    failing selector's own file, with a brief naming the selector, the
    step's index/title/files, and its commit's diff stat. A repair whose
    strict re-run of the selector goes green is committed as
    ``fix(wrapup): <selector> broken by step <n>``; an attempt loop that
    exhausts without green is recorded unrepaired.

    Args:
        repo_root: The session's repository root — never disturbed;
            attribution and repair both operate on it directly (the
            worktree used for attribution is temporary and torn down
            per commit by :func:`_run_selector_at_commit`).
        plan: The action plan whose step commits are walked and whose
            steps supply file contracts for repair briefs.
        dev: The Developer agent (or an injected fake) dispatched for
            each bounded repair attempt.
        db: L4 database path for dispatch persistence.
        base: The ref the implementation branch was created from — the
            plan base attribution is checked against first.
        failing_selectors: Wrap-up selectors that failed and that no
            step promised (see :func:`_collect_failing_wrapup_selectors`).
        result: The session result updated in place —
            :attr:`DevSessionResult.wrapup_dossier` is always set to the
            newline-joined dossier lines once every selector has been
            processed.

    Returns:
        ``None`` when every selector ended up repaired. Otherwise a
        ``no_op_reason`` string built from the FIRST selector that stayed
        unrepaired: for an attributed-but-unfixed step it names the
        step's index/title and the selector; for a ``pre_existing`` or
        ``error`` outcome it reuses that selector's dossier line verbatim.
    """
    step_commits = _step_commits_for_plan(repo_root, plan, base)
    dossier_lines: list[str] = []
    unrepaired: list[tuple[int | None, str, str]] = []

    for selector in failing_selectors:
        try:
            outcome = attribute_failure_to_step(
                selector,
                step_commits,
                run_selector=lambda sha, sel: _run_selector_at_commit(repo_root, sha, sel),
            )
        except Exception as exc:
            line = f"error: {selector}: attribution crashed: {exc}"
            _log.warning(
                "dev_runner.wrapup_attribution_crashed",
                selector=selector,
                error=str(exc)[:200],
            )
            dossier_lines.append(line)
            unrepaired.append((None, "", line))
            continue

        if outcome.status == "pre_existing":
            line = f"pre_existing: {selector}"
            dossier_lines.append(line)
            unrepaired.append((None, "", line))
            continue

        if outcome.status == "error":
            line = f"error: {selector}: {outcome.error}"
            dossier_lines.append(line)
            unrepaired.append((None, "", line))
            continue

        matched_step = next(s for s in plan.steps if s.index == outcome.step.index)
        _rc, diff_stat = _run_git(repo_root, "show", "--stat", outcome.step.sha, tail_chars=0)
        commit_message = f"fix(wrapup): {selector} broken by step {outcome.step.index}"
        brief = (
            f"Wrap-up repair: the selector `{selector}` is broken by step "
            f"{matched_step.index} ({matched_step.title}).\n\n"
            f"Step files: {matched_step.files}\n\n"
            f"Step diff (git show --stat {outcome.step.sha}):\n{diff_stat}\n\n"
            "Fix the regression this step introduced so the selector above "
            "passes again, without breaking the step's own promised tests."
        )

        repaired = False
        for _attempt in range(WRAPUP_REPAIR_ATTEMPTS):
            loop_result = dev.develop_step(
                brief=brief,
                repo_root=repo_root,
                allowed_paths=[*matched_step.files, selector.split("::", 1)[0]],
                repo_tree="",
                spec_id=f"{plan.spec_id}:wrapup-repair-step-{outcome.step.index}",
            )
            record_coder_response(
                db,
                pr_number=0,
                plan={
                    "fixes": [],
                    "commit_message": commit_message,
                    "summary": f"wrapup repair attempt for {selector}",
                },
                model_used=loop_result.model_used,
                elapsed_s=loop_result.elapsed_s,
                tokens_used=loop_result.tokens_used,
            )
            ok, _tail = run_pytest_selectors(repo_root, [selector])
            if ok:
                commit_all(repo_root, commit_message)
                repaired = True
                break

        if not repaired:
            diff_stat_summary = " ".join(diff_stat.splitlines()[-1].split())[:200]
            line = (
                f"step {outcome.step.index} ({matched_step.title}): {selector} "
                f"\u2014 {diff_stat_summary}"
            )
            dossier_lines.append(line)
            unrepaired.append((outcome.step.index, matched_step.title, selector))

    result.wrapup_dossier = "\n".join(dossier_lines)

    if not unrepaired:
        return None

    step_index, step_title, entry = unrepaired[0]
    if step_index is not None:
        return (
            f"full unit suite pytest red — step {step_index} ({step_title}) "
            f"introduced a failure in {entry} and the bounded repair attempt did not fix it"
        )
    return f"full unit suite pytest red — {entry}"


def execute_plan_step(
    step: PlanStep,
    *,
    plan: ActionPlan,
    repo_root: Path,
    developer: Developer,
    repo_tree: str,
    db: Path,
    spec_markdown: str = "",
    arch_owns: str = "",
    pre_existing: frozenset[str] = frozenset(),
) -> StepOutcome:
    """Execute one plan step agentically: drive the loop, verify, commit.

    SP-DEVAGENT-LOOP rewires this from the one-shot apply path to the agentic
    tool loop. The Developer (:meth:`Developer.develop_step`) reads the tree,
    writes/edits within the step's file contract (enforced in-loop by the tools'
    ``allowed_paths`` jail), runs the tests and ruff, and fixes forward until it
    stops. The model's self-report is not trusted: after the loop the runner
    re-verifies authoritatively with the existing gates (syntax → imports → ruff →
    repo lint → the step's promised tests) and commits the step with its plan
    commit message only on green.

    Unlike the legacy path this NEVER reverts. A red gate keeps the partial work
    on disk and re-enters the loop with the gate tail fed back as feedback
    (fix-forward), up to :data:`_MAX_STEP_ATTEMPTS`. An exhausted step leaves its
    uncommitted work in place for inspection rather than wiping untracked files —
    the autopilot-incident lesson carried into the DEVAGENT arc; the destructive
    :func:`revert_working_tree` is gone from this path.

    Two attempts always run; a third runs only after a lint-class gate failure,
    where the model is usually one nudge from green (:data:`_MAX_STEP_ATTEMPTS`).

    A promised test file absent after the loop is terminal ONLY when the step's
    own contract cannot create it (a genuinely mis-shaped plan). When the file
    sits inside the contract, the absence is a retryable gate like any other:
    SP-USAGE-REASONING-SPLIT dispatch 1 was finalized mid-thought ("Now let me
    write the test file:"), and the old unconditional stop discarded a
    310k-token attempt one write_file short of green.

    Args:
        step: The plan step to execute.
        plan: The full plan (context for the brief).
        repo_root: Working tree root.
        developer: The Developer agent (or an injected fake).
        repo_tree: Pre-rendered orientation tree appended to the brief.
        db: L4 database path for dispatch persistence.
        spec_markdown: The source spec verbatim, forwarded into every step brief
            (SP-DEV-STEP-CONTEXT) — empty keeps the plan-only brief.
        arch_owns: The governed architecture contract (SP-ARCH-DEV-WIRE),
            forwarded into the step brief; empty for a frontier/no-spec run.
        pre_existing: Repo-relative paths already modified or
            untracked when the SESSION started — never attributed to
            the step, never staged into its commit (attempt-1 of the
            SP-FINDINGS-BRIDGE-DOCFIX dogfood was refused over an
            uncommitted spec draft and a stray lockfile).

    Returns:
        A :class:`StepOutcome`; ``ok=True`` means the step's commit is on the
        branch.
    """
    allowed_paths = list(step.files)
    contract = normalise_contract(repo_root, allowed_paths)
    totals = StepOutcome(ok=False)
    gate_feedback = ""
    last_gate_kind = ""

    for attempt in range(1, _MAX_STEP_ATTEMPTS + 1):
        if attempt == _MAX_STEP_ATTEMPTS and last_gate_kind != _LINT_GATE_KIND:
            break
        last_gate_kind = _OTHER_GATE_KIND
        brief = build_step_brief(
            plan,
            step,
            gate_feedback=gate_feedback,
            spec_markdown=spec_markdown,
            arch_owns=arch_owns,
        )
        result = developer.develop_step(
            brief=brief,
            repo_root=repo_root,
            allowed_paths=allowed_paths,
            repo_tree=repo_tree,
            spec_id=f"{plan.spec_id}:step-{step.index}",
        )
        record_coder_response(
            db,
            pr_number=0,
            plan={
                "fixes": [],
                "commit_message": step.commit_message,
                "summary": result.text[:240],
            },
            model_used=result.model_used,
            elapsed_s=result.elapsed_s,
            tokens_used=result.tokens_used,
        )
        if result.error:
            gate_feedback = f"agentic loop failed: {result.error}"
            _log.warning(
                "dev_runner.step_loop_failed",
                spec_id=plan.spec_id,
                step=step.index,
                attempt=attempt,
                error=result.error,
            )
            continue

        changed = [p for p in _changed_paths(repo_root) if p not in pre_existing]
        if not changed:
            gate_feedback = (
                "Your loop ended without writing any file. Use write_file/edit_file "
                "to implement the step inside its file contract."
            )
            _log.warning(
                "dev_runner.step_no_changes",
                spec_id=plan.spec_id,
                step=step.index,
                attempt=attempt,
            )
            continue

        out_of_contract = [p for p in changed if p not in contract]
        if out_of_contract:
            totals.fixes_rejected += len(out_of_contract)
            totals.rejected_paths.extend(out_of_contract)
            totals.reason = (
                f"step {step.index} ({step.title!r}) changed files outside its "
                f"contract: {out_of_contract} — the in-loop jail should refuse "
                "these; not retried"
            )
            _log.warning(
                "dev_runner.step_out_of_contract",
                spec_id=plan.spec_id,
                step=step.index,
                paths=out_of_contract,
            )
            return totals

        healed = heal_inline_comments(repo_root, allowed_paths)
        if healed:
            _log.info(
                "dev_runner.inline_comments_healed",
                spec_id=plan.spec_id,
                step=step.index,
                attempt=attempt,
                paths=healed,
            )

        syntax_ok, syntax_tail = check_python_syntax(repo_root, allowed_paths)
        if not syntax_ok:
            gate_feedback = f"python syntax gate: {syntax_tail}"
            continue
        imports_ok, imports_report = check_imports(repo_root, allowed_paths)
        if not imports_ok:
            gate_feedback = f"import gate: {imports_report}"
            continue
        ruff_ok, ruff_tail = run_ruff_gate(repo_root, unsafe_fixes=True)
        if not ruff_ok:
            gate_feedback = f"ruff gate: {ruff_tail}"
            last_gate_kind = _LINT_GATE_KIND
            continue
        lint_ok, lint_report = run_repo_lint_gates(repo_root, allowed_paths)
        if not lint_ok:
            gate_feedback = f"repo lint gate: {lint_report}"
            last_gate_kind = _LINT_GATE_KIND
            continue
        if step.unit_tests:
            absent = [t for t in step.unit_tests if not (repo_root / t.split("::", 1)[0]).is_file()]
            if absent:
                absent_files = sorted({t.split("::", 1)[0] for t in absent})
                creatable = [f for f in absent_files if f in step.files]
                if creatable:
                    gate_feedback = (
                        f"promised-test gate: {creatable} were never written, yet they "
                        "sit inside this step's file contract — create them now with "
                        "write_file and make their promised tests pass"
                    )
                    _log.warning(
                        "dev_runner.step_promise_unwritten",
                        spec_id=plan.spec_id,
                        step=step.index,
                        attempt=attempt,
                        files=creatable,
                    )
                    continue
                totals.reason = (
                    f"step {step.index} ({step.title!r}) promises tests that do not "
                    f"exist after its loop and are not in its file contract: {absent} — "
                    "the plan is mis-shaped (the step must create the tests it promises); "
                    "not retried (work left in place)"
                )
                _log.warning(
                    "dev_runner.step_promises_absent_tests",
                    spec_id=plan.spec_id,
                    step=step.index,
                    absent=absent,
                )
                return totals
            tests_ok, tests_tail, reconciled = run_promised_tests(repo_root, list(step.unit_tests))
            if not tests_ok:
                gate_feedback = f"pytest gate on the step's promised tests: {tests_tail}"
                continue
            if reconciled:
                touched_promised = _promised_test_files(step.unit_tests) & set(changed)
                if not touched_promised:
                    gate_feedback = (
                        "promised-test gate: reconciled green but the loop did not "
                        "touch the promised test file(s) in this attempt \u2014 write "
                        "tests named exactly: " + ", ".join(step.unit_tests)
                    )
                    _log.warning(
                        "dev_runner.promised_tests_untouched",
                        spec_id=plan.spec_id,
                        step=step.index,
                        promised=list(step.unit_tests),
                    )
                    continue
                rename_errored = False
                renamed_originals: dict[str, str] = {}
                for file_path in sorted(touched_promised):
                    promised_names = [
                        s.rsplit("::", 1)[-1].split("[", 1)[0]
                        for s in step.unit_tests
                        if s.split("::", 1)[0] == file_path
                    ]
                    defined = _test_function_names_in_file(repo_root, file_path)
                    delivered_names = [n for n in defined if n not in promised_names]
                    outcome, original = _attempt_mechanical_rename(
                        repo_root, file_path, promised_names, delivered_names
                    )
                    if outcome == "renamed":
                        renamed_originals[file_path] = original
                    elif outcome == "error":
                        rename_errored = True
                        break

                if rename_errored:
                    _restore_file_contents(repo_root, renamed_originals)
                    gate_feedback = (
                        "promised-test gate: mechanical rename failed \u2014 write "
                        "tests named exactly: " + ", ".join(step.unit_tests)
                    )
                    _log.warning(
                        "dev_runner.promised_tests_rename_failed",
                        spec_id=plan.spec_id,
                        step=step.index,
                        promised=list(step.unit_tests),
                    )
                    continue
                if renamed_originals:
                    re_ok, re_tail = run_pytest_selectors(repo_root, list(step.unit_tests))
                    if re_ok:
                        _log.info(
                            "dev_runner.promised_tests_renamed",
                            spec_id=plan.spec_id,
                            step=step.index,
                            promised=list(step.unit_tests),
                        )
                    else:
                        _restore_file_contents(repo_root, renamed_originals)
                        gate_feedback = (
                            "promised-test gate: the mechanical rename did not make "
                            "the promised selectors green \u2014 write tests named "
                            "exactly: " + ", ".join(step.unit_tests)
                        )
                        _log.warning(
                            "dev_runner.promised_tests_rename_rerun_red",
                            spec_id=plan.spec_id,
                            step=step.index,
                            tail=re_tail[:200],
                        )
                        continue
                else:
                    absent_promises: list[str] = []
                    for selector in step.unit_tests:
                        if selector.split("::", 1)[0] not in touched_promised:
                            continue
                        if not promised_present(repo_root, selector):
                            absent_promises.append(selector)
                    if absent_promises:
                        delivered_listing: list[str] = []
                        for file_path in sorted(touched_promised):
                            names = _test_function_names_in_file(repo_root, file_path)
                            if names:
                                delivered_listing.append(f"{file_path}: {', '.join(names)}")
                        delivered_block = (
                            "\n".join(delivered_listing)
                            if delivered_listing
                            else "(no test_* functions found in the touched file)"
                        )
                        absent_names = ", ".join(absent_promises)
                        gate_feedback = (
                            "promised-test gate: the loop touched the promised test "
                            "file(s) but the promised trailing name(s) are absent "
                            f"({absent_names}). Delivered test functions found in the "
                            f"touched file(s):\n{delivered_block}\n"
                            "add a test named exactly <name>, or correct the plan "
                            "promise."
                        )
                        _log.warning(
                            "dev_runner.promised_tests_fanout_refused",
                            spec_id=plan.spec_id,
                            step=step.index,
                            absent=absent_promises,
                            delivered=delivered_listing,
                        )
                        continue
                    _log.warning(
                        "dev_runner.promised_tests_reconciled",
                        spec_id=plan.spec_id,
                        step=step.index,
                        promised=list(step.unit_tests),
                    )

        step_changes = [p for p in _changed_paths(repo_root) if p not in pre_existing]
        escaped = [p for p in step_changes if p not in contract]
        if escaped:
            totals.fixes_rejected += len(escaped)
            totals.rejected_paths.extend(escaped)
            totals.reason = (
                f"step {step.index} ({step.title!r}): the gates introduced changes "
                f"outside the contract: {escaped} (e.g. ruff reformatting an unrelated "
                "file) — refusing to commit them; not retried"
            )
            _log.warning(
                "dev_runner.step_gate_escaped_contract",
                spec_id=plan.spec_id,
                step=step.index,
                paths=escaped,
            )
            return totals

        committed, commit_detail = commit_paths(repo_root, step_changes, step.commit_message)
        if not committed:
            gate_feedback = f"git commit gate: {commit_detail}"
            continue

        totals.fixes_applied += len(changed)
        totals.ok = True
        _log.info(
            "dev_runner.step_done",
            spec_id=plan.spec_id,
            step=step.index,
            attempt=attempt,
            files_changed=len(changed),
        )
        return totals

    totals.reason = (
        f"step {step.index} ({step.title!r}) failed after retry; work left in place — "
        f"last gate: {gate_feedback[:300]}"
    )
    _log.warning("dev_runner.step_failed", spec_id=plan.spec_id, step=step.index)
    return totals


def _resolve_sub_specs(
    spec: SpecPlan, *, repo: Path, decomposer: Decomposer | None
) -> tuple[list[SpecPlan], str | None]:
    """Decompose *spec* into the (sub-)specs to develop, in dependency order.

    An un-governed (frontier) parent or the identity passthrough yields ``[spec]``
    (develop the parent directly — the unchanged single-spec path). A real
    multi-way decomposition renders each governed sub-spec to a committed
    ``docs/specs`` file, **supersedes the parent by removing it** (so the sub-specs
    are the sole owners and ``arch check`` stays green), and loads each sub-spec back
    as a :class:`SpecPlan`. Both the sub-spec writes and the parent deletion land in a
    single commit. Returns ``(targets, None)`` on success or ``([], reason)`` when
    decomposition or supersession fails — the session then stops without developing
    anything.

    SP-DEVAGENT-DECOMPOSE (slice 4) built the decomposition; SP-DEVAGENT-WIRE
    (slice 5) added the parent supersession that arms the multi path.
    """
    try:
        governed = load_governed_spec(spec.id, root=repo)
    except Exception as exc:
        _log.warning("dev_runner.governed_load_failed", spec_id=spec.id, error=str(exc)[:200])
        return [], f"could not load governed frontmatter: {str(exc)[:160]}"
    if governed is None:
        return [spec], None

    decomp = decompose_spec(spec, governed, proposer=decomposer, repo_root=repo)
    if decomp.error is not None:
        return [], f"decompose failed: {decomp.error}"
    if decomp.identity:
        return [spec], None

    specs_dir = (spec.file_path.parent).resolve()
    date_prefix = spec.file_path.name.split("_", 1)[0]
    written: list[Path] = []
    try:
        for sub in decomp.sub_specs:
            target = (specs_dir / f"{date_prefix}_{sub.id}_sub.md").resolve()
            if not target.is_relative_to(specs_dir):
                raise ValueError(f"sub-spec id {sub.id!r} escapes the specs directory")
            target.write_text(render_sub_spec_markdown(sub), encoding="utf-8")
            written.append(target)
    except (OSError, ValueError) as exc:
        for path in written:
            path.unlink(missing_ok=True)
        _log.warning("dev_runner.sub_spec_write_failed", spec_id=spec.id, error=str(exc)[:200])
        return [], f"could not write sub-spec files: {str(exc)[:160]}"

    rel_written = [str(p.relative_to(repo)) for p in written]
    rc, add_out = _run_git(repo, "add", "--", *rel_written)
    if rc != 0:
        for path in written:
            path.unlink(missing_ok=True)
        return [], f"could not stage sub-spec files: {add_out}"

    supersede_error = supersede_parent_on_decompose(repo, spec, staged_subspecs=written)
    if supersede_error:
        for path in written:
            path.unlink(missing_ok=True)
        return [], f"could not supersede parent spec: {supersede_error}"

    commit_msg = (
        f"docs(decompose): split {spec.id} into {len(written)} sub-specs (supersedes {spec.id})"
    )
    rc, commit_out = _run_git(repo, "commit", "-m", commit_msg)
    if rc != 0:
        return [], f"could not commit sub-spec files: {commit_out}"

    targets: list[SpecPlan] = []
    for sub in decomp.sub_specs:
        try:
            targets.append(load_spec(sub.id, root=repo))
        except (FileNotFoundError, ValueError) as exc:
            return [], f"sub-spec {sub.id} not loadable after decompose: {exc}"
    _log.info("dev_runner.decomposed", spec_id=spec.id, n_sub_specs=len(targets))
    return targets, None


def _develop_one_spec(
    spec: SpecPlan,
    *,
    repo: Path,
    dev: Developer,
    db: Path,
    base: str,
    judge: ComplianceJudge | None,
    planner: object | None,
    explore_via: Literal["proxy", "claude_cli"],
    cc_model: str,
    tree: str,
    result: DevSessionResult,
) -> str | None:
    """Plan, implement, and self-verify ONE (sub-)spec on the current branch.

    Updates *result* in place and returns ``None`` on success or a ``no_op_reason``
    string on failure. Does NOT push — :func:`run_developer_session` pushes once
    after every (sub-)spec has self-verified. SP-DEVAGENT-DECOMPOSE (slice 4)
    extracted this from the single-spec session body unchanged; the single-spec /
    identity path therefore behaves exactly as before.
    """
    action_plan, plan_error = load_or_produce_plan(
        spec,
        repo_root=repo,
        planner=planner,
        explore_via=explore_via,
        cc_model=cc_model,
    )
    if action_plan is None:
        return plan_error
    result.steps_total += len(action_plan.steps)
    plan_ok, plan_detail = commit_plan_document(repo, spec.id)
    if not plan_ok:
        return f"plan commit failed: {plan_detail}"
    result.plan_committed = True

    arch_owns_brief = ""
    try:
        from .governed_spec import render_owns_brief

        arch_owns_brief = render_owns_brief(load_governed_spec(spec.id, root=repo))
    except Exception as exc:
        _log.info("dev_runner.arch_owns_failed", spec_id=spec.id, error=str(exc))

    pre_existing = frozenset(_changed_paths(repo))
    if pre_existing:
        _log.warning(
            "dev_runner.pre_existing_worktree_files_ignored",
            spec_id=spec.id,
            n=len(pre_existing),
            paths=sorted(pre_existing)[:10],
        )

    for step in action_plan.steps:
        if _step_already_committed(repo, step):
            result.steps_completed += 1
            _log.info(
                "dev_runner.step_already_committed",
                spec_id=spec.id,
                step=step.index,
                commit=step.commit_message,
            )
            continue
        if step_preflight_complete(repo, action_plan, step):
            contract = set(step.files)
            dirty = [p for p in _changed_paths(repo) if p in contract]
            if dirty:
                commit_paths(repo, dirty, step.commit_message)
            result.steps_completed += 1
            selectors = list(step.unit_tests) + [
                t for t in action_plan.integration_tests if t.split("::", 1)[0] in contract
            ]
            _log.info(
                "dev_runner.step_preflight_complete",
                spec_id=spec.id,
                step=step.index,
                selectors=selectors,
            )
            record_coder_response(
                db,
                pr_number=0,
                plan={
                    "fixes": [],
                    "commit_message": step.commit_message,
                    "summary": (
                        f"preflight-complete step {step.index} ({step.title}): "
                        f"{', '.join(selectors)}"
                    ),
                },
                model_used="preflight",
                elapsed_s=0.0,
                tokens_used=0,
            )
            continue
        outcome = execute_plan_step(
            step,
            plan=action_plan,
            repo_root=repo,
            developer=dev,
            repo_tree=tree,
            db=db,
            spec_markdown=spec.raw_markdown,
            arch_owns=arch_owns_brief,
            pre_existing=pre_existing,
        )
        result.fixes_applied += outcome.fixes_applied
        result.fixes_rejected += outcome.fixes_rejected
        result.rejected_paths.extend(outcome.rejected_paths)
        if not outcome.ok:
            result.failed_step_index = step.index
            return outcome.reason
        result.steps_completed += 1

    full_ok, full_tail = run_pytest_matrix(repo)
    result.ruff_passed = True
    result.pytest_passed = full_ok
    if not full_ok:
        promised = _promised_selectors_for_plan(action_plan)
        failing = _collect_failing_wrapup_selectors(repo, promised)
        if failing:
            reason = repair_wrapup_failures(
                repo,
                action_plan,
                dev=dev,
                db=db,
                base=base,
                failing_selectors=failing,
                result=result,
            )
            if reason is not None:
                return reason
            full_ok, full_tail = run_pytest_matrix(repo)
            result.pytest_passed = full_ok
    if not full_ok:
        return f"full unit suite pytest red after all steps ({full_tail[:160]})"

    integration_present = [
        t for t in action_plan.integration_tests if (repo / t.split("::")[0]).exists()
    ]
    promised_missing = [
        t for t in action_plan.integration_tests if not (repo / t.split("::")[0]).exists()
    ]
    if promised_missing:
        _log.warning(
            "dev_runner.integration_tests_promised_but_absent",
            spec_id=spec.id,
            missing=promised_missing,
        )
    if integration_present:
        int_ok, int_tail = run_pytest_selectors(repo, integration_present)
        if not int_ok:
            result.pytest_passed = False
            return f"integration tests pytest red ({int_tail[:160]})"

    try:
        self_verify = run_self_verify(
            repo,
            spec=spec,
            plan=action_plan,
            suite_green=result.pytest_passed,
            base=base,
            judge=judge,
        )
    except Exception as exc:
        _log.warning("dev_runner.self_verify_crashed", spec_id=spec.id, error=str(exc)[:200])
        return f"self-verify gate crashed: {type(exc).__name__}: {str(exc)[:160]}"
    result.self_verified = self_verify.ok
    if not self_verify.ok:
        _log.warning("dev_runner.self_verify_failed", spec_id=spec.id, reasons=self_verify.reasons)
        return "self-verify gate failed: " + " | ".join(self_verify.reasons)[:240]
    return None


def run_developer_session(
    spec_id: str,
    *,
    repo_root: Path | None = None,
    branch: str | None = None,
    base: str = "develop",
    push: bool = True,
    developer: Developer | None = None,
    planner: object | None = None,
    explore_via: Literal["proxy", "claude_cli"] = "proxy",
    cc_model: str = "sonnet",
    db_path: Path | None = None,
    judge: ComplianceJudge | None = None,
    decomposer: Decomposer | None = None,
) -> DevSessionResult:
    """Run one plan-driven Developer session end-to-end.

    SP-DEV-PLAN-EXEC pipeline:

    1. Load the spec, create/switch to the implementation branch.
    2. **Plan first** — load ``docs/plans/<SP-ID>.md`` or run one
       Planner session to produce it, then commit the plan document
       as the branch's first commit.
    3. **Execute each step** of the plan in order: step-scoped
       Developer dispatch, the step's file contract enforced, per-step
       gates (syntax → ruff → the step's promised unit tests), one
       retry with the gate output fed back, one commit per green step
       with the step's own commit message. A second red stops the
       session loudly — earlier green commits stay on the branch.
    4. **Wrap up** — full unit suite, then the plan's integration
       tests when their files exist, then a single push.

    Args:
        spec_id: Any reasonable spelling — normalised by
            :func:`load_spec`.
        repo_root: Working tree root (defaults to ``cwd``).
        branch: Override branch name (defaults to the
            :data:`DEFAULT_BRANCH_TEMPLATE` rendering).
        base: Source ref for the new branch (defaults to ``develop``).
        push: When ``False`` the runner stops after committing
            locally — useful for tests / dry-runs.
        developer: Optional pre-built :class:`Developer` (tests
            inject a mock).
        planner: Optional pre-built Planner forwarded to
            :func:`load_or_produce_plan` (tests inject a fake).
        explore_via: Exploration backend for the produce path
            (``"proxy"`` / ``"claude_cli"``); inert when a committed
            plan exists. SP-DEV-CC-WIRE.
        cc_model: CLI model alias for ``"claude_cli"`` mode.
        db_path: Override L4 db path (tests).
        judge: Optional injected compliance judge for the self-verify gate
            (SP-DEVAGENT-SELFVERIFY); ``None`` builds the production OPUS-tier
            judge. Tests inject a fake to avoid a live proxy call.
        decomposer: Optional injected sub-spec proposer for the decomposition
            front-end (SP-DEVAGENT-DECOMPOSE); ``None`` builds the production
            proposer lazily (only reached for a governed multi-owns spec). Tests
            inject a fake.

    Returns:
        A :class:`DevSessionResult`.  No exception raised on gate
        failures — the caller (CLI) maps ``no_op_reason`` keywords to
        exit codes.
    """
    repo = (repo_root or Path.cwd()).resolve()
    settings = get_settings()
    db = db_path or Path(settings.db_path)
    init_schema(db)

    try:
        spec = load_spec(spec_id, root=repo)
    except FileNotFoundError as exc:
        return DevSessionResult(
            spec_id=spec_id,
            no_op_reason=f"spec not found: {exc}",
        )

    target_branch = branch or DEFAULT_BRANCH_TEMPLATE.format(slug=spec_to_branch_slug(spec.id))
    if not ensure_branch(target_branch, base=base, repo_root=repo):
        return DevSessionResult(
            spec_id=spec.id,
            branch=target_branch,
            no_op_reason=f"could not create or switch to branch {target_branch!r}",
        )

    tree = render_repo_tree(repo_root=repo)
    dev = developer or Developer()
    result = DevSessionResult(
        spec_id=spec.id,
        branch=target_branch,
        pr_title=f"feat({spec.id.lower()}): {spec.title}"[:200],
        pr_summary=spec.summary,
    )

    sub_targets, decompose_error = _resolve_sub_specs(spec, repo=repo, decomposer=decomposer)
    if decompose_error is not None:
        result.no_op_reason = decompose_error
        return result
    result.decomposed = not (len(sub_targets) == 1 and sub_targets[0].id == spec.id)
    if result.decomposed:
        result.sub_spec_ids = [sub.id for sub in sub_targets]

    gate_judge = judge
    if gate_judge is None and push:
        try:
            gate_judge = make_compliance_judge()
        except Exception as exc:
            _log.warning("dev_runner.judge_build_failed", spec_id=spec.id, error=str(exc)[:160])
            gate_judge = None

    for sub_target in sub_targets:
        reason = _develop_one_spec(
            sub_target,
            repo=repo,
            dev=dev,
            db=db,
            base=base,
            judge=gate_judge,
            planner=planner,
            explore_via=explore_via,
            cc_model=cc_model,
            tree=tree,
            result=result,
        )
        if reason is not None:
            result.no_op_reason = reason
            return result

    if not push:
        result.no_op_reason = "dry-run: push=False"
        return result

    pushed, push_log = push_branch(repo, target_branch)
    if not pushed:
        result.no_op_reason = f"commit/push failed: {push_log}"
        return result
    result.pushed = True
    return result


def open_pr(
    *,
    spec_id: str,
    title: str,
    summary: str,
    spec_ref: str,
    branch: str,
    base: str = "develop",
    gh: GhCli | None = None,
) -> str:
    """Open a draft-quality PR for *branch* via the ``gh`` CLI.

    Takes the PR metadata as plain values rather than a :class:`SpecPlan` so the
    handoff survives a **superseded (deleted) parent** spec — on a decomposed run the
    parent file no longer exists, so the caller sources title / summary from the
    :class:`DevSessionResult` it captured before supersession (SP-DEVAGENT-WIRE).

    Args:
        spec_id: The parent SP-ID (for the PR body).
        title: The PR title (already shaped as ``feat(<id>): <title>``).
        summary: One-line spec summary for the body.
        spec_ref: A human reference to the spec source — the parent spec path for a
            single-spec run, or a note that the parent was decomposed for a multi run.
        branch: The head branch to open the PR from.
        base: The PR base (defaults to ``develop``).
        gh: Optional :class:`GhCli` (tests inject a fake).

    Returns:
        The PR URL on success, empty string on failure.
    """
    gh = gh or GhCli()
    title = title[:200]
    body = (
        f"Autonomous NIM Developer implementation of **{spec_id}**.\n\n"
        f"Spec: {spec_ref}\n\n"
        f"Summary: {summary}\n\n"
        "_Generated by `ferova develop`. The NIM review pipeline "
        "(Architect/Sentinel/Tester/Scribe + Coder fix loop) takes over "
        "from here._"
    )
    res = gh.pr_create(head=branch, base=base, title=title, body=body)
    if not res.ok:
        _log.warning("dev_runner.pr_create_failed", stderr=res.stderr[:200])
        return ""
    for line in reversed(res.stdout.strip().splitlines()):
        if line.startswith("http"):
            return line.strip()
    return res.stdout.strip()
