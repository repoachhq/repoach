"""Declared generated artefacts: summarised for the reviewers, proved by their check.

A file a committed script derives from other committed files cannot be
reviewed by reading it: its content is a function of its sources. The
repository declares such a file under ``[[review.generated]]`` with the
command that proves the derivation (SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ).

The flow has two halves that never share a process:

- the secret-free verification job runs ``repoach review generated``,
  which runs each touched check once at the head (twice when the check
  script itself changed, D3) through an injected :class:`Runner` with
  ``shell=False`` and :func:`~repoach.review.secret_env.scrubbed_env`,
  and writes the ``format:review-generated-json`` evidence file;
- the review job reads the declarations at the base ref, replaces the
  artefact's diff unit by one summary paragraph before any reviewer lens
  sees it (:func:`summarise`), loads the evidence bound to the reviewed
  head, records one ``generated_checks`` row per run, and records a
  blocking ``generated_artefact`` finding for every path whose derivation
  is not shown (:func:`generated_findings`).

At a later head, :func:`settle_at_head` settles those findings, and the
older oversized findings of a newly declared path, from the
``generated_checks`` rows of that head, never through the refuter.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol

from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    insert,
    select,
)
from sqlalchemy.engine import Engine

from ..core.logging import get_logger
from ..core.sqlite_schema_init import ensure_schema_created
from ..project.config import (
    CONFIG_FILE_NAME,
    GENERATED_CHECK_TIMEOUT_S,
    ConfigError,
    ProjectConfig,
    glob_matches,
    parse_project_config,
    refuse_generated_conflicts,
)
from .diff_scoper import split_diff
from .findings import (
    ClaimType,
    Finding,
    FindingStatus,
    Severity,
    fetch_findings,
    init_findings_schema,
    record_finding,
)
from .findings_bridge import OVERSIZED_FINDER, finding_fingerprint
from .secret_env import scrubbed_env

_log = get_logger(__name__)

CHECKS_TOTAL_BUDGET_S = 300
"""Seconds every check of one PR may take together (``checks_total_budget_s``)."""

OUTPUT_TAIL_LINES = 20
"""Lines of a check's output kept on its row and in the evidence."""

EVIDENCE_FORMAT = "review-generated-json"
"""The ``format`` field of the evidence file (``format:review-generated-json``)."""

EVIDENCE_VERSION = 1
"""The evidence file's schema version."""

GENERATED_FINDER = "generated_check"
"""Finder and verification method of a ``generated_artefact`` finding."""

_CLAIM_PREFIX = "generated artefact not derived from its sources: "

CAUSE_LINE_MAX_CHARS = 200
"""Characters of a check's last output line a finding's cause keeps."""

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_MARKUP_RE = re.compile(r"[`<>\[\]|@*_~#]")
_SETTLEABLE_FINDERS = frozenset({GENERATED_FINDER, OVERSIZED_FINDER})


@dataclass(frozen=True)
class GeneratedEntry:
    """One declared generated artefact (a ``[[review.generated]]`` entry read at the base).

    Attributes:
        path: Glob of the artefact, repository-relative.
        check: The argv proving the derivation; ``check[0]`` is a script.
        sources: Globs of the files the artefact derives from (non-empty).
        producer_inputs: Globs of files pinning the producer (D1); they
            count as sources and are reported apart.
        check_timeout_s: Seconds the check may run.
    """

    path: str
    check: tuple[str, ...]
    sources: tuple[str, ...]
    producer_inputs: tuple[str, ...] = ()
    check_timeout_s: int = GENERATED_CHECK_TIMEOUT_S

    def matches(self, path: str) -> bool:
        """Whether ``path`` is this artefact."""
        return glob_matches(self.path, path)

    def argv_line(self) -> str:
        """The declared check, joined by spaces, as the report prints it."""
        return " ".join(self.check)


class GeneratedState(StrEnum):
    """The state of one check run (one ``generated_checks`` row)."""

    VERIFIED = "generated_artefact_verified"
    STALE = "generated_artefact_stale"
    NO_SOURCE_CHANGED = "generated_artefact_no_source_changed"
    CHECK_ABSENT = "generated_check_absent"
    SKIPPED_COST = "skipped (cost)"
    NOT_RUN = "generated_check_not_run"


ScriptRef = Literal["head", "base"]


@dataclass(frozen=True)
class GeneratedCheck:
    """The outcome of one check run against the head tree.

    Attributes:
        path: The artefact's path.
        argv: The declared check, placeholders replaced.
        head_sha: The head the check ran against.
        artefact_sha256: The artefact's sha256 at that head.
        sources_changed: Source paths the diff changes.
        state: The run's :class:`GeneratedState`.
        exit_code: The check's exit code; ``None`` when it did not exit.
        duration_ms: Wall time of the run.
        output_tail: The last :data:`OUTPUT_TAIL_LINES` lines of output,
            or the reason it did not run.
        script_ref: ``base`` for the run with the base-ref script (D3).
        producer_inputs_changed: Producer-input paths the diff changes.
        runs_for_path: How many runs the path had (2 under D3).
    """

    path: str
    argv: tuple[str, ...]
    head_sha: str
    artefact_sha256: str
    sources_changed: tuple[str, ...]
    state: GeneratedState
    exit_code: int | None
    duration_ms: int
    output_tail: str
    script_ref: ScriptRef = "head"
    producer_inputs_changed: tuple[str, ...] = ()
    runs_for_path: int = 1

    def as_json(self) -> dict[str, Any]:
        """The evidence entry of this run."""
        return {
            "path": self.path,
            "argv": list(self.argv),
            "head_sha": self.head_sha,
            "artefact_sha256": self.artefact_sha256,
            "sources_changed": list(self.sources_changed),
            "producer_inputs_changed": list(self.producer_inputs_changed),
            "state": self.state.value,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "output_tail": self.output_tail,
            "script_ref": self.script_ref,
            "runs_for_path": self.runs_for_path,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> GeneratedCheck:
        """Rebuild a run from its evidence entry."""
        exit_code = data.get("exit_code")
        return cls(
            path=str(data["path"]),
            argv=tuple(str(arg) for arg in data["argv"]),
            head_sha=str(data["head_sha"]),
            artefact_sha256=str(data["artefact_sha256"]),
            sources_changed=tuple(str(p) for p in data.get("sources_changed", ())),
            state=GeneratedState(str(data["state"])),
            exit_code=None if exit_code is None else int(exit_code),
            duration_ms=int(data.get("duration_ms", 0)),
            output_tail=str(data.get("output_tail", "")),
            script_ref="base" if data.get("script_ref") == "base" else "head",
            producer_inputs_changed=tuple(str(p) for p in data.get("producer_inputs_changed", ())),
            runs_for_path=int(data.get("runs_for_path", 1)),
        )


@dataclass(frozen=True)
class GeneratedTiming:
    """Wall time of ``repoach review generated`` (G7).

    Attributes:
        checks_ms: Sum of the check runs.
        overhead_ms: Everything else: reading the declarations, the head
            tree, writing the evidence.
    """

    checks_ms: int
    overhead_ms: int

    def line(self) -> str:
        """The report line ``generated checks: <n> ms + <m> ms overhead``."""
        return f"generated checks: {self.checks_ms} ms + {self.overhead_ms} ms overhead"


class GeneratedEvidenceMismatchError(ValueError):
    """The evidence file names another head or another artefact blob than the reviewed one."""


GeneratedEvidenceMismatch = GeneratedEvidenceMismatchError
"""The name the spec's interface gives the error."""


@dataclass(frozen=True)
class RunResult:
    """What a :class:`Runner` returns: the exit code and the merged output."""

    exit_code: int
    output: str


class Runner(Protocol):
    """The subprocess seam ``run_checks`` calls (tests replace it).

    Raises ``subprocess.TimeoutExpired`` when the check outlives ``timeout_s``.
    """

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: float,
        shell: bool,
    ) -> RunResult:
        """Run ``argv`` in ``cwd`` with ``env`` and return its result."""
        ...


def subprocess_runner(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout_s: float,
    shell: bool = False,
) -> RunResult:
    """Run one check with :func:`subprocess.run`, never through a shell.

    Raises:
        ValueError: ``shell`` is True (NG3: no shell, ever).
        subprocess.TimeoutExpired: The check outlived ``timeout_s``.
    """
    if shell:
        raise ValueError("a generated-artefact check never runs through a shell")
    proc = subprocess.run(
        list(argv),
        cwd=cwd,
        env=dict(env),
        shell=False,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout_s,
        check=False,
    )
    return RunResult(proc.returncode, (proc.stdout or "") + (proc.stderr or ""))


def _entries(config: ProjectConfig) -> tuple[GeneratedEntry, ...]:
    """Turn the validated ``[[review.generated]]`` sections into entries."""
    return tuple(
        GeneratedEntry(
            path=section.path,
            check=tuple(section.check),
            sources=tuple(section.sources),
            producer_inputs=tuple(section.producer_inputs),
            check_timeout_s=section.check_timeout_s,
        )
        for section in config.review.generated
    )


def load_generated_entries(
    base_toml_text: str, path: Path, languages: Sequence[str]
) -> tuple[GeneratedEntry, ...]:
    """Parse the ``[[review.generated]]`` entries of a ``repoach.toml`` read at the base ref.

    Args:
        base_toml_text: The file's content at the base ref.
        path: Where it came from, for messages.
        languages: Languages whose files a ``path`` glob may never match,
            besides the file's own ``[project].languages``.

    Returns:
        The entries, in declaration order.

    Raises:
        ConfigError: A malformed entry, a glob matching code, or two
            entries declaring the same path; the message names the key
            and the line.
    """
    config = parse_project_config(base_toml_text, path)
    refuse_generated_conflicts(path, base_toml_text, config.review.generated, tuple(languages))
    return _entries(config)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    """Run one read-only git command in ``root`` and capture its bytes."""
    return subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)


def blob_at(root: Path, ref: str, path: str) -> bytes | None:
    """Return ``path``'s content at ``ref``, ``None`` when it is absent there."""
    shown = _git(root, "show", f"{ref}:{path}")
    return shown.stdout if shown.returncode == 0 else None


def merge_base(root: Path, base_ref: str, head: str) -> str | None:
    """Return the merge base of ``base_ref`` and ``head``, ``None`` when git cannot say."""
    found = _git(root, "merge-base", base_ref, head)
    if found.returncode != 0:
        return None
    return found.stdout.decode().strip() or None


def declared_at_base(root: Path, base_ref: str | None) -> tuple[GeneratedEntry, ...]:
    """Read the generated-artefact declarations of ``repoach.toml`` at the base ref (G1).

    The head never declares: an entry a PR adds applies from the next PR on.

    Args:
        root: Any checkout of the repository (refs are shared).
        base_ref: The base ref; ``None`` declares nothing.

    Returns:
        The entries, empty when the base has no file or no entry.

    Raises:
        ConfigError: The base file is invalid.
    """
    if not base_ref:
        return ()
    text = blob_at(root, base_ref, CONFIG_FILE_NAME)
    if text is None:
        return ()
    decoded = text.decode("utf-8", errors="replace")
    config = parse_project_config(decoded, Path(f"{base_ref}:{CONFIG_FILE_NAME}"))
    return _entries(config)


def touched_artefacts(
    entries: Sequence[GeneratedEntry], diff_files: Sequence[str]
) -> dict[str, GeneratedEntry]:
    """Return every changed path a declared ``path`` matches, in declaration order.

    Args:
        entries: The declarations read at the base.
        diff_files: The paths the diff changes.

    Returns:
        ``{changed path: its entry}``; empty when the diff touches none.

    Raises:
        ConfigError: Two entries match the same changed path.
    """
    touched: dict[str, GeneratedEntry] = {}
    for entry in entries:
        for path in diff_files:
            if not entry.matches(path):
                continue
            other = touched.get(path)
            if other is not None and other is not entry:
                raise ConfigError(
                    Path(CONFIG_FILE_NAME),
                    f"{path} is matched by two entries ({other.path!r} and {entry.path!r})",
                    key="review.generated",
                )
            touched[path] = entry
    return touched


def changed_inputs(
    entry: GeneratedEntry, path: str, diff_files: Sequence[str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return ``(sources changed, producer inputs changed)`` for one artefact.

    The artefact itself never counts as its own source.
    """
    sources = tuple(
        f for f in diff_files if f != path and any(glob_matches(g, f) for g in entry.sources)
    )
    producers = tuple(
        f
        for f in diff_files
        if f != path and f not in sources and any(glob_matches(g, f) for g in entry.producer_inputs)
    )
    return sources, producers


def _sha256(content: bytes | None) -> str:
    """Return the hex sha256 of ``content``, empty for an absent file."""
    return hashlib.sha256(content).hexdigest() if content is not None else ""


def summary_paragraph(
    path: str,
    entry: GeneratedEntry,
    *,
    before: bytes | None,
    after: bytes | None,
    diff_files: Sequence[str],
) -> str:
    """Build the one paragraph every reviewer lens sees instead of the artefact's diff (G2).

    Sizes, hashes and paths only (NG1, NG2).
    """
    size_before = f"{len(before)}" if before is not None else "absent"
    size_after = f"{len(after)}" if after is not None else "deleted"
    sha_before = _sha256(before)[:7] or "none"
    sha_after = _sha256(after)[:7] or "none"
    sources, producers = changed_inputs(entry, path, diff_files)
    paragraph = (
        f"generated artefact {path}: {size_before} -> {size_after} bytes, "
        f"sha256 {sha_before} -> {sha_after}; "
        f"sources changed in this diff: {', '.join(sources) or 'none'}; "
        f"proved by: {entry.argv_line()}"
    )
    if producers:
        paragraph += f"; producer inputs changed: {', '.join(producers)}"
    return paragraph


def summarise(
    diff: str,
    touched: Mapping[str, GeneratedEntry],
    *,
    base_blob: Callable[[str], bytes | None],
    head_blob: Callable[[str], bytes | None],
) -> str:
    """Return ``diff`` with each touched artefact's unit replaced by its summary paragraph.

    The unit keeps its ``diff --git`` header so the path stays a changed
    path; nothing else of the artefact reaches a reviewer.

    Args:
        diff: The PR's unified diff.
        touched: The declared artefacts the diff changes.
        base_blob: Reads a path at the base.
        head_blob: Reads a path at the head.

    Returns:
        The diff the reviewers see; ``diff`` itself when nothing is touched.
    """
    if not touched:
        return diff
    units = split_diff(diff)
    if not units:
        return diff
    paths = [unit.path for unit in units]
    rebuilt: list[str] = []
    for unit in units:
        entry = touched.get(unit.path)
        if entry is None:
            rebuilt.append(unit.text)
            continue
        paragraph = summary_paragraph(
            unit.path,
            entry,
            before=base_blob(unit.path),
            after=head_blob(unit.path),
            diff_files=paths,
        )
        rebuilt.append(f"diff --git a/{unit.path} b/{unit.path}\n{paragraph}\n")
    return "".join(rebuilt)


@dataclass(frozen=True)
class ReviewScope:
    """What the review job derives from the declarations before any reviewer runs.

    Attributes:
        review_diff: The diff every reviewer lens sees (:func:`summarise`).
        touched: The declared artefacts the diff changes, in declaration order.
        changed: The paths the diff changes.
    """

    review_diff: str
    touched: Mapping[str, GeneratedEntry] = field(default_factory=dict)
    changed: tuple[str, ...] = ()


def review_scope(head_dir: Path, diff: str, base_ref: str | None) -> ReviewScope:
    """Read the declarations at ``base_ref`` and summarise the touched artefacts (G1, G2).

    Args:
        head_dir: The PR head tree (its refs reach the base).
        diff: The PR's unified diff.
        base_ref: A ref of the base that resolves; ``None`` declares nothing.

    Returns:
        The scope; ``review_diff`` is ``diff`` when nothing declared is touched.

    Raises:
        ConfigError: The base declarations are invalid, or two entries
            match one changed path.
    """
    changed = tuple(unit.path for unit in split_diff(diff))
    touched = touched_artefacts(declared_at_base(head_dir, base_ref), changed)
    if not touched or base_ref is None:
        return ReviewScope(review_diff=diff, changed=changed)
    before = merge_base(head_dir, base_ref, "HEAD") or base_ref

    def head_blob(path: str) -> bytes | None:
        target = head_dir / path
        return target.read_bytes() if target.is_file() else None

    summarised = summarise(
        diff,
        touched,
        base_blob=lambda path: blob_at(head_dir, before, path),
        head_blob=head_blob,
    )
    return ReviewScope(review_diff=summarised, touched=touched, changed=changed)


def _interpreted(argv: Sequence[str], head_dir: Path) -> list[str]:
    """Prefix the check's script with its interpreter (``.py`` python3, ``.sh`` bash)."""
    script = argv[0]
    if script.endswith(".py"):
        return ["python3", *argv]
    if script.endswith(".sh"):
        return ["bash", *argv]
    return [str(head_dir / script), *argv[1:]]


def _declared_argv(entry: GeneratedEntry, head_sha: str, base_sha: str) -> tuple[str, ...]:
    """The declared check with ``{head}`` / ``{base}`` replaced by the SHAs."""
    return tuple(arg.replace("{head}", head_sha).replace("{base}", base_sha) for arg in entry.check)


def _tail(output: str) -> str:
    """The last :data:`OUTPUT_TAIL_LINES` non-empty lines of ``output``, control characters removed."""
    lines = [_CONTROL_RE.sub("", line) for line in output.splitlines()]
    lines = [line for line in lines if line.strip()]
    return "\n".join(lines[-OUTPUT_TAIL_LINES:])


@contextmanager
def _script_at(head_dir: Path, script: str, content: bytes | None) -> Iterator[None]:
    """Put ``content`` at ``script`` in the head tree for one run, then restore it (D3)."""
    if content is None:
        yield
        return
    target = head_dir / script
    original = target.read_bytes()
    mode = target.stat().st_mode
    target.write_bytes(content)
    try:
        yield
    finally:
        target.write_bytes(original)
        target.chmod(mode)


@dataclass
class _Budget:
    """The PR's check budget, measured on ``clock``."""

    total_s: float
    clock: Callable[[], float]
    started: float = field(init=False)

    def __post_init__(self) -> None:
        """Start the budget now."""
        self.started = self.clock()

    def remaining(self) -> float:
        """Seconds left before the budget is spent."""
        return self.total_s - (self.clock() - self.started)


def _run_once(
    entry: GeneratedEntry,
    path: str,
    *,
    head_dir: Path,
    head_sha: str,
    base_sha: str,
    runner: Runner,
    timeout_s: int,
    budget: _Budget,
    script_content: bytes | None,
    env: Mapping[str, str],
) -> tuple[GeneratedState, int | None, int, str]:
    """Run one check and return ``(state, exit code, ms, output tail)``."""
    limit = min(entry.check_timeout_s, timeout_s)
    remaining = budget.remaining()
    if remaining <= 0:
        return (
            GeneratedState.SKIPPED_COST,
            None,
            0,
            f"check not run within {budget.total_s:g} s: {path}",
        )
    allowed = min(limit, remaining)
    argv = _interpreted(_declared_argv(entry, head_sha, base_sha), head_dir)
    started = budget.clock()
    try:
        with _script_at(head_dir, entry.check[0], script_content):
            result = runner(argv, cwd=head_dir, env=env, timeout_s=allowed, shell=False)
    except subprocess.TimeoutExpired:
        ms = round((budget.clock() - started) * 1000)
        return GeneratedState.SKIPPED_COST, None, ms, f"check not run within {allowed:g} s: {path}"
    except Exception as exc:
        ms = round((budget.clock() - started) * 1000)
        _log.warning("generated.check_raised", path=path, exc=type(exc).__name__)
        return GeneratedState.STALE, None, ms, type(exc).__name__
    elapsed = budget.clock() - started
    ms = round(elapsed * 1000)
    if elapsed > allowed:
        return GeneratedState.SKIPPED_COST, None, ms, f"check not run within {allowed:g} s: {path}"
    state = GeneratedState.VERIFIED if result.exit_code == 0 else GeneratedState.STALE
    return state, result.exit_code, ms, _tail(result.output)


def run_checks(
    touched: Mapping[str, GeneratedEntry],
    *,
    head_dir: Path,
    head_sha: str,
    runner: Runner,
    budget_s: int = CHECKS_TOTAL_BUDGET_S,
    timeout_s: int = GENERATED_CHECK_TIMEOUT_S,
    changed_files: Sequence[str] = (),
    base_sha: str = "",
    base_blob: Callable[[str], bytes | None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    env: Mapping[str, str] | None = None,
) -> list[GeneratedCheck]:
    """Run each touched artefact's check against the head tree, in declaration order.

    Never called by the review job (G2): only ``repoach review generated``
    runs it, in the secret-free verification job. Every run gets
    ``shell=False`` and :func:`scrubbed_env`. When ``check[0]`` itself
    changed in the diff, the check runs twice against the head tree: with
    the base-ref script, then with the head script (D3).

    Args:
        touched: The declared artefacts the diff changes.
        head_dir: The head tree.
        head_sha: The head SHA (``{head}``).
        runner: The subprocess seam.
        budget_s: Seconds every run of the PR may take together.
        timeout_s: Seconds one run may take, at most.
        changed_files: The paths the diff changes.
        base_sha: The base SHA (``{base}``).
        base_blob: Reads a path at the base (the D3 base script).
        clock: The monotonic clock the budget and durations read.
        env: The environment; :func:`scrubbed_env` when omitted.

    Returns:
        One :class:`GeneratedCheck` per run; an artefact deleted at the
        head gets none.
    """
    environment = dict(env) if env is not None else scrubbed_env()
    budget = _Budget(float(budget_s), clock)
    checks: list[GeneratedCheck] = []
    for path, entry in touched.items():
        artefact = head_dir / path
        if not artefact.is_file():
            continue
        artefact_sha = _sha256(artefact.read_bytes())
        sources, producers = changed_inputs(entry, path, changed_files)
        script = entry.check[0]
        template = GeneratedCheck(
            path=path,
            argv=_declared_argv(entry, head_sha, base_sha),
            head_sha=head_sha,
            artefact_sha256=artefact_sha,
            sources_changed=sources,
            state=GeneratedState.NOT_RUN,
            exit_code=None,
            duration_ms=0,
            output_tail="",
            producer_inputs_changed=producers,
        )
        if not (head_dir / script).is_file():
            checks.append(
                replace(
                    template,
                    state=GeneratedState.CHECK_ABSENT,
                    output_tail=f"check script {script} absent at {head_sha[:7]}",
                )
            )
            continue
        runs: list[tuple[ScriptRef, bytes | None]] = [("head", None)]
        if script in changed_files and base_blob is not None:
            base_script = base_blob(script)
            if base_script is not None:
                runs = [("base", base_script), ("head", None)]
        for ref, content in runs:
            state, exit_code, ms, tail = _run_once(
                entry,
                path,
                head_dir=head_dir,
                head_sha=head_sha,
                base_sha=base_sha,
                runner=runner,
                timeout_s=timeout_s,
                budget=budget,
                script_content=content,
                env=environment,
            )
            if state is GeneratedState.VERIFIED and not (sources or producers):
                state = GeneratedState.NO_SOURCE_CHANGED
            checks.append(
                replace(
                    template,
                    state=state,
                    exit_code=exit_code,
                    duration_ms=ms,
                    output_tail=tail,
                    script_ref=ref,
                    runs_for_path=len(runs),
                )
            )
    return checks


def not_run_checks(
    touched: Mapping[str, GeneratedEntry],
    *,
    head_dir: Path,
    head_sha: str,
    changed_files: Sequence[str],
) -> list[GeneratedCheck]:
    """Return a ``generated_check_not_run`` row for each touched artefact present at the head."""
    checks: list[GeneratedCheck] = []
    for path, entry in touched.items():
        artefact = head_dir / path
        if not artefact.is_file():
            continue
        sources, producers = changed_inputs(entry, path, changed_files)
        checks.append(
            GeneratedCheck(
                path=path,
                argv=entry.check,
                head_sha=head_sha,
                artefact_sha256=_sha256(artefact.read_bytes()),
                sources_changed=sources,
                state=GeneratedState.NOT_RUN,
                exit_code=None,
                duration_ms=0,
                output_tail=f"no check evidence for {path} at {head_sha[:7]}",
                producer_inputs_changed=producers,
            )
        )
    return checks


def _last_line(tail: str) -> str:
    """The last line of an output tail, made inert: the check's output is pull-request text.

    Control and Markdown markup characters become spaces and the line is
    cut at :data:`CAUSE_LINE_MAX_CHARS`, so the cause a finding stores and
    the report prints cannot carry a mention, a link or an escape sequence.
    """
    lines = tail.splitlines()
    if not lines:
        return ""
    plain = _MARKUP_RE.sub(" ", _CONTROL_RE.sub(" ", lines[-1]))
    return " ".join(plain.split())[:CAUSE_LINE_MAX_CHARS]


def check_cause(check: GeneratedCheck) -> str:
    """Name why ``check`` does not show the derivation (the finding's ``verification_result``)."""
    if check.state is GeneratedState.NO_SOURCE_CHANGED:
        return "no source changed"
    if check.state is GeneratedState.STALE:
        prefix = f"{check.script_ref} " if check.runs_for_path > 1 else ""
        if check.exit_code is None:
            return f"{prefix}check failed: {_last_line(check.output_tail)}"
        return f"{prefix}check exited {check.exit_code}: {_last_line(check.output_tail)}"
    return _last_line(check.output_tail) or check.state.value


def _by_path(checks: Sequence[GeneratedCheck]) -> dict[str, list[GeneratedCheck]]:
    """Group runs by artefact path, keeping the order of first appearance."""
    grouped: dict[str, list[GeneratedCheck]] = {}
    for check in checks:
        grouped.setdefault(check.path, []).append(check)
    return grouped


def path_verified(runs: Sequence[GeneratedCheck]) -> bool:
    """Whether every run of one path verified the derivation (a source changed, exit 0)."""
    return bool(runs) and all(run.state is GeneratedState.VERIFIED for run in runs)


def generated_findings(
    checks: Sequence[GeneratedCheck], *, pr_number: int, round_n: int
) -> list[Finding]:
    """Build one blocking ``generated_artefact`` finding per path the checks do not prove (G3).

    Args:
        checks: The runs of this head.
        pr_number: The pull request.
        round_n: The review round.

    Returns:
        The findings, ``verified`` (the check is the verification).
    """
    findings: list[Finding] = []
    for path, runs in _by_path(checks).items():
        if path_verified(runs):
            continue
        causes = [check_cause(run) for run in runs if run.state is not GeneratedState.VERIFIED]
        head = runs[0].head_sha
        findings.append(
            Finding(
                pr_number=pr_number,
                head_sha=head,
                round=round_n,
                finder=GENERATED_FINDER,
                claim_type=ClaimType.GENERATED_ARTEFACT,
                severity=Severity.BLOCKING,
                file=path,
                line_start=0,
                line_end=0,
                claim=f"{_CLAIM_PREFIX}{path}",
                evidence_pointer=f"{path}: proved by {' '.join(runs[0].argv)}",
                status=FindingStatus.VERIFIED,
                verification_method=GENERATED_FINDER,
                verification_result="; ".join(causes),
                checked_at_sha=head,
            )
        )
    return findings


def record_generated_findings(
    db_path: Path, checks: Sequence[GeneratedCheck], *, pr_number: int, round_n: int
) -> int:
    """Persist :func:`generated_findings` once per head; return how many were new."""
    findings = generated_findings(checks, pr_number=pr_number, round_n=round_n)
    if not findings:
        return 0
    init_findings_schema(db_path)
    head = findings[0].head_sha
    at_head = {
        finding_fingerprint(row)
        for row in fetch_findings(db_path, pr_number)
        if row.head_sha == head
    }
    recorded = 0
    for finding in findings:
        if finding_fingerprint(finding) in at_head:
            continue
        record_finding(db_path, finding)
        recorded += 1
    return recorded


_metadata = MetaData()

generated_checks = Table(
    "generated_checks",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("pr_number", Integer, nullable=False, index=True),
    Column("head_sha", String, nullable=False),
    Column("path", String, nullable=False),
    Column("argv", Text, nullable=False),
    Column("exit_code", Integer, nullable=True),
    Column("duration_ms", Integer, nullable=False),
    Column("output_tail", Text, nullable=False),
    Column("state", String, nullable=False),
    Column("script_ref", String, nullable=False),
    Column("runs_for_path", Integer, nullable=False),
    Column("sources_changed", Text, nullable=False),
    Column("producer_inputs_changed", Text, nullable=False),
    Column("artefact_sha256", String, nullable=False),
    Column("checks_ms", Integer, nullable=True),
    Column("overhead_ms", Integer, nullable=True),
    Column("recorded_at", String, nullable=False),
)
"""One row per check run at a head (``db:table:generated_checks``)."""


def _engine_for(db_path: Path) -> Engine:
    """Return a SQLite engine on ``db_path``, creating its directory."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{db_path}")


def init_generated_schema(db_path: Path) -> None:
    """Create the ``generated_checks`` table if absent (idempotent)."""
    ensure_schema_created(_engine_for(db_path), _metadata)


def record_generated_checks(
    db_path: Path,
    *,
    pr_number: int,
    head_sha: str,
    checks: Sequence[GeneratedCheck],
    timing: GeneratedTiming | None = None,
) -> None:
    """Replace the ``generated_checks`` rows of ``head_sha`` by ``checks``."""
    init_generated_schema(db_path)
    now = datetime.now(UTC).isoformat()
    engine = _engine_for(db_path)
    with engine.begin() as conn:
        conn.execute(
            delete(generated_checks).where(
                generated_checks.c.pr_number == pr_number,
                generated_checks.c.head_sha == head_sha,
            )
        )
        for check in checks:
            conn.execute(
                insert(generated_checks).values(
                    pr_number=pr_number,
                    head_sha=head_sha,
                    path=check.path,
                    argv=json.dumps(list(check.argv)),
                    exit_code=check.exit_code,
                    duration_ms=check.duration_ms,
                    output_tail=check.output_tail,
                    state=check.state.value,
                    script_ref=check.script_ref,
                    runs_for_path=check.runs_for_path,
                    sources_changed=json.dumps(list(check.sources_changed)),
                    producer_inputs_changed=json.dumps(list(check.producer_inputs_changed)),
                    artefact_sha256=check.artefact_sha256,
                    checks_ms=None if timing is None else timing.checks_ms,
                    overhead_ms=None if timing is None else timing.overhead_ms,
                    recorded_at=now,
                )
            )


def fetch_generated_checks(
    db_path: Path, pr_number: int, head_sha: str
) -> tuple[list[GeneratedCheck], GeneratedTiming | None]:
    """Return the runs recorded at ``head_sha`` and the timing they came with."""
    init_generated_schema(db_path)
    engine = _engine_for(db_path)
    with engine.connect() as conn:
        rows = conn.execute(
            select(generated_checks)
            .where(
                generated_checks.c.pr_number == pr_number,
                generated_checks.c.head_sha == head_sha,
            )
            .order_by(generated_checks.c.id)
        ).fetchall()
    checks = [
        GeneratedCheck(
            path=row.path,
            argv=tuple(json.loads(row.argv)),
            head_sha=row.head_sha,
            artefact_sha256=row.artefact_sha256,
            sources_changed=tuple(json.loads(row.sources_changed)),
            state=GeneratedState(row.state),
            exit_code=row.exit_code,
            duration_ms=row.duration_ms,
            output_tail=row.output_tail,
            script_ref="base" if row.script_ref == "base" else "head",
            producer_inputs_changed=tuple(json.loads(row.producer_inputs_changed)),
            runs_for_path=row.runs_for_path,
        )
        for row in rows
    ]
    timing = None
    if rows and rows[0].checks_ms is not None and rows[0].overhead_ms is not None:
        timing = GeneratedTiming(rows[0].checks_ms, rows[0].overhead_ms)
    return checks, timing


def evidence_payload(
    *,
    pr_number: int,
    head_sha: str,
    base_sha: str,
    checks: Sequence[GeneratedCheck],
    timing: GeneratedTiming,
) -> dict[str, Any]:
    """Build the ``format:review-generated-json`` evidence document."""
    return {
        "format": EVIDENCE_FORMAT,
        "version": EVIDENCE_VERSION,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "base_sha": base_sha,
        "checks_ms": timing.checks_ms,
        "overhead_ms": timing.overhead_ms,
        "checks": [check.as_json() for check in checks],
    }


def checks_from_evidence(
    payload: Mapping[str, Any],
    touched: Mapping[str, GeneratedEntry],
    *,
    head_sha: str,
    head_dir: Path,
    changed_files: Sequence[str],
) -> tuple[list[GeneratedCheck], GeneratedTiming]:
    """Read the runs of the touched artefacts out of an evidence document.

    Args:
        payload: The parsed evidence file.
        touched: The declared artefacts the reviewed diff changes.
        head_sha: The reviewed head.
        head_dir: The reviewed head tree (the artefacts' blobs).
        changed_files: The paths the diff changes.

    Returns:
        The runs, in declaration order, and the evidence's timing; an
        artefact the evidence does not name gets a ``not_run`` row.

    Raises:
        GeneratedEvidenceMismatch: The evidence is of another format,
            another head, or another blob of a touched artefact.
    """
    if payload.get("format") != EVIDENCE_FORMAT:
        raise GeneratedEvidenceMismatchError(f"not a {EVIDENCE_FORMAT} file")
    if payload.get("head_sha") != head_sha:
        raise GeneratedEvidenceMismatchError(
            f"evidence of head {str(payload.get('head_sha'))[:7]}, reviewing {head_sha[:7]}"
        )
    recorded = _by_path([GeneratedCheck.from_json(item) for item in payload.get("checks", [])])
    checks: list[GeneratedCheck] = []
    for path in touched:
        artefact = head_dir / path
        if not artefact.is_file():
            continue
        runs = recorded.get(path)
        if not runs:
            checks.extend(
                not_run_checks(
                    {path: touched[path]},
                    head_dir=head_dir,
                    head_sha=head_sha,
                    changed_files=changed_files,
                )
            )
            continue
        blob = _sha256(artefact.read_bytes())
        if any(run.artefact_sha256 != blob for run in runs):
            raise GeneratedEvidenceMismatchError(
                f"evidence of {path} names blob {runs[0].artefact_sha256[:7]}, head has {blob[:7]}"
            )
        checks.extend(
            _rebound(
                runs,
                touched[path],
                path=path,
                head_sha=head_sha,
                base_sha=str(payload.get("base_sha") or ""),
                changed_files=changed_files,
            )
        )
    timing = GeneratedTiming(int(payload.get("checks_ms", 0)), int(payload.get("overhead_ms", 0)))
    return checks, timing


def _rebound(
    runs: Sequence[GeneratedCheck],
    entry: GeneratedEntry,
    *,
    path: str,
    head_sha: str,
    base_sha: str,
    changed_files: Sequence[str],
) -> list[GeneratedCheck]:
    """Recompute at review time everything the evidence carries that the review can derive.

    The verification job runs pull-request code, so its JSON is trusted
    for the check's exit code and output only. The argv comes from the
    base declaration, the changed sources and producer inputs from the
    reviewed diff, and the state from the exit code and those sources: a
    run claiming ``verified`` without exit 0, or without a changed source,
    is not verified. When the check script changed, a missing base-script
    run (D3) is reported not run, unless the script is absent at the head.
    """
    sources, producers = changed_inputs(entry, path, changed_files)
    argv = _declared_argv(entry, head_sha, base_sha)
    rebound: list[GeneratedCheck] = []
    for run in runs:
        state = run.state
        if state in {GeneratedState.VERIFIED, GeneratedState.NO_SOURCE_CHANGED}:
            if run.exit_code != 0:
                state = GeneratedState.STALE
            elif not (sources or producers):
                state = GeneratedState.NO_SOURCE_CHANGED
            else:
                state = GeneratedState.VERIFIED
        rebound.append(
            replace(
                run,
                argv=argv,
                head_sha=head_sha,
                sources_changed=sources,
                producer_inputs_changed=producers,
                state=state,
            )
        )
    script_changed = entry.check[0] in changed_files
    absent = any(run.state is GeneratedState.CHECK_ABSENT for run in rebound)
    if script_changed and not absent and not any(run.script_ref == "base" for run in rebound):
        rebound.insert(
            0,
            replace(
                rebound[0],
                state=GeneratedState.NOT_RUN,
                exit_code=None,
                duration_ms=0,
                output_tail=f"no base-script run for {path} at {head_sha[:7]}",
                script_ref="base",
                runs_for_path=2,
            ),
        )
    return rebound


def load_review_evidence(
    evidence: Path | None,
    touched: Mapping[str, GeneratedEntry],
    *,
    head_sha: str,
    head_dir: Path,
    changed_files: Sequence[str],
) -> tuple[list[GeneratedCheck], GeneratedTiming | None]:
    """Return the runs the review job records for the touched artefacts.

    A missing, unreadable or mismatched evidence file is never green:
    each touched artefact then gets a ``generated_check_not_run`` row.
    """
    if not touched:
        return [], None
    if evidence is not None and evidence.is_file():
        try:
            payload = json.loads(evidence.read_text(encoding="utf-8"))
            return checks_from_evidence(
                payload,
                touched,
                head_sha=head_sha,
                head_dir=head_dir,
                changed_files=changed_files,
            )
        except (GeneratedEvidenceMismatch, ValueError, KeyError, OSError) as exc:
            _log.warning(
                "generated.evidence_refused",
                path=str(evidence),
                exc=type(exc).__name__,
                message=str(exc)[:200],
                consequence="each touched artefact is reported not run",
            )
    else:
        _log.warning("generated.evidence_absent", head_sha=head_sha[:12], touched=list(touched))
    return (
        not_run_checks(touched, head_dir=head_dir, head_sha=head_sha, changed_files=changed_files),
        None,
    )


def report_json(checks: Sequence[GeneratedCheck]) -> list[dict[str, Any]]:
    """The ``generated`` array of the ``review pr`` JSON (G5)."""
    return [
        {
            "path": check.path,
            "check": " ".join(check.argv),
            "state": check.state.value,
            "ms": check.duration_ms,
            "sources_changed": list(check.sources_changed),
            "producer_inputs_changed": list(check.producer_inputs_changed),
            "script_ref": check.script_ref,
        }
        for check in checks
    ]


def report_lines(
    checks: Sequence[GeneratedCheck], timing: GeneratedTiming | None = None
) -> list[str]:
    """The report lines naming what proved each artefact, or why nothing did (G5, G7)."""
    lines: list[str] = []
    for path, runs in _by_path(checks).items():
        for run in runs:
            argv = " ".join(run.argv)
            if run.state is GeneratedState.VERIFIED:
                where = f"{run.script_ref} script, " if run.runs_for_path > 1 else ""
                lines.append(f"generated: {path} verified by {argv} ({where}{run.duration_ms} ms)")
            else:
                lines.append(f"generated: {path} {run.state.value}: {check_cause(run)}")
        producers = runs[0].producer_inputs_changed
        if producers:
            lines.append(f"producer inputs changed: {', '.join(producers)}")
    if timing is not None and checks:
        lines.append(timing.line())
    return lines


ReverifyOutcome = tuple[Literal["resolved", "still_open"], str, str]


def settlements_at_head(db_path: Path, pr_number: int, head_sha: str) -> dict[str, ReverifyOutcome]:
    """Return how the ``generated_checks`` rows of ``head_sha`` settle each recorded path (G3, G6).

    ``resolved`` when every run of the path verified the derivation (a
    source changed, exit 0), ``still_open`` with the cause otherwise.
    """
    if not head_sha:
        return {}
    checks, _ = fetch_generated_checks(db_path, pr_number, head_sha)
    settled: dict[str, ReverifyOutcome] = {}
    for path, runs in _by_path(checks).items():
        if path_verified(runs):
            reason = f"declared generated at {head_sha[:7]}, verified by {' '.join(runs[0].argv)}"
            settled[path] = ("resolved", GENERATED_FINDER, reason)
        else:
            causes = "; ".join(
                check_cause(r) for r in runs if r.state is not GeneratedState.VERIFIED
            )
            settled[path] = ("still_open", GENERATED_FINDER, causes)
    return settled


def settle_at_head(
    finding: Finding, settlements: Mapping[str, ReverifyOutcome]
) -> ReverifyOutcome | None:
    """Settle an older-head finding on a declared path from the head's rows, without a refuter.

    Applies to a ``generated_artefact`` finding and to the oversized
    finding of a path the base now declares; ``None`` for every other
    finding or a path with no row at the head.
    """
    if finding.finder not in _SETTLEABLE_FINDERS:
        return None
    if finding.claim_type not in {ClaimType.GENERATED_ARTEFACT, ClaimType.SPEC_GAP}:
        return None
    return settlements.get(finding.file)


@contextmanager
def head_tree(root: Path, sha: str, state_dir: Path) -> Iterator[Path]:
    """Yield a tree at ``sha``: the checkout when it is there and clean, else a detached worktree.

    The worktree is removed on exit.
    """
    at = _git(root, "rev-parse", "HEAD").stdout.decode().strip()
    dirty = _git(root, "status", "--porcelain", "--untracked-files=no").stdout.strip()
    if at == sha and not dirty:
        yield root
        return
    target = state_dir / "generated" / sha[:12]
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        _git(root, "worktree", "remove", "--force", str(target))
    added = _git(root, "worktree", "add", "--detach", "--force", str(target), sha)
    if added.returncode != 0:
        raise RuntimeError(added.stderr.decode(errors="replace").strip()[:300])
    try:
        yield target
    finally:
        _git(root, "worktree", "remove", "--force", str(target))


class GeneratedRefusalError(RuntimeError):
    """``repoach review generated`` cannot run: a ref does not resolve or the base is invalid."""


def _resolve(root: Path, ref: str) -> str | None:
    """Return the commit ``ref`` names in ``root``, ``None`` when it does not resolve."""
    found = _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if found.returncode != 0:
        return None
    return found.stdout.decode().strip() or None


def verify_generated(
    root: Path,
    *,
    pr_number: int,
    head: str,
    base_ref: str,
    state_dir: Path,
    runner: Runner | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[dict[str, Any], list[GeneratedCheck], GeneratedTiming]:
    """Run the touched checks of a PR at its head and build the evidence (``repoach review generated``).

    The verification job's whole work, secret-free (G2, G7): the
    declarations are read at ``base_ref``, the changed paths between the
    merge base and the head, the checks run against a tree at the head
    (the checkout when it is there and clean, else a detached worktree
    under ``state_dir``), and the wall time is split into ``checks_ms``
    and ``overhead_ms``.

    Args:
        root: The checkout.
        pr_number: The pull request.
        head: The head SHA (or a ref naming it).
        base_ref: The base ref (``origin/develop``).
        state_dir: Where a worktree of the head may be created.
        runner: The subprocess seam; :func:`subprocess_runner`, looked up
            at call time, when omitted.
        clock: The monotonic clock.

    Returns:
        ``(evidence payload, runs, timing)``.

    Raises:
        GeneratedRefusalError: The head or the base does not resolve.
        ConfigError: The base declarations are invalid.
    """
    started = clock()
    head_sha = _resolve(root, head)
    if head_sha is None:
        raise GeneratedRefusalError(f"head {head!r} does not resolve to a commit")
    if _resolve(root, base_ref) is None:
        raise GeneratedRefusalError(f"base {base_ref!r} does not resolve to a commit")
    base_sha = merge_base(root, base_ref, head_sha) or base_ref
    changed = changed_paths(root, base_sha, head_sha)
    touched = touched_artefacts(declared_at_base(root, base_ref), changed)
    checks: list[GeneratedCheck] = []
    if touched:
        with head_tree(root, head_sha, state_dir) as tree:
            checks = run_checks(
                touched,
                head_dir=tree,
                head_sha=head_sha,
                runner=runner if runner is not None else subprocess_runner,
                changed_files=changed,
                base_sha=base_sha,
                base_blob=lambda path: blob_at(root, base_sha, path),
                clock=clock,
            )
    checks_ms = sum(check.duration_ms for check in checks)
    overhead_ms = max(round((clock() - started) * 1000) - checks_ms, 0)
    timing = GeneratedTiming(checks_ms=checks_ms, overhead_ms=overhead_ms)
    payload = evidence_payload(
        pr_number=pr_number, head_sha=head_sha, base_sha=base_sha, checks=checks, timing=timing
    )
    return payload, checks, timing


def changed_paths(root: Path, base_sha: str, head_sha: str) -> list[str]:
    """Return the paths changed between ``base_sha`` and ``head_sha``."""
    listed = _git(root, "diff", "--name-only", "--no-renames", base_sha, head_sha)
    return [line for line in listed.stdout.decode().splitlines() if line]


__all__ = [
    "CHECKS_TOTAL_BUDGET_S",
    "EVIDENCE_FORMAT",
    "GENERATED_FINDER",
    "GeneratedCheck",
    "GeneratedEntry",
    "GeneratedEvidenceMismatch",
    "GeneratedEvidenceMismatchError",
    "GeneratedRefusalError",
    "GeneratedState",
    "GeneratedTiming",
    "ReviewScope",
    "RunResult",
    "Runner",
    "blob_at",
    "changed_paths",
    "declared_at_base",
    "evidence_payload",
    "fetch_generated_checks",
    "generated_findings",
    "head_tree",
    "load_generated_entries",
    "load_review_evidence",
    "merge_base",
    "record_generated_checks",
    "record_generated_findings",
    "report_json",
    "report_lines",
    "review_scope",
    "run_checks",
    "settle_at_head",
    "settlements_at_head",
    "subprocess_runner",
    "summarise",
    "touched_artefacts",
    "verify_generated",
]
