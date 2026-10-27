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

The rows live in :mod:`repoach.review.generated_store`; the verification
job's entry point is :mod:`repoach.review.generated_verify`.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol

from ..core.logging import get_logger
from ..gates.languages import Language, spec_for
from ..project.config import (
    CONFIG_FILE_NAME,
    GENERATED_CHECK_TIMEOUT_S,
    GENERATED_CHECKS_TOTAL_BUDGET_S,
    GENERATED_OUTPUT_TAIL_LINES,
    ConfigError,
    ProjectConfig,
    parse_project_config,
    refuse_code_globs,
)
from ..project.globs import glob_matches
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
from .generated_git import blob_at, merge_base
from .secret_env import scrubbed_env

_log = get_logger(__name__)

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
_MARKUP_RE = re.compile(r"[`<>\[\]|@]")
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
        output_tail: The last :data:`GENERATED_OUTPUT_TAIL_LINES` lines of output,
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
    """Run one check in its own session, never through a shell, and kill what it leaves.

    Args:
        argv: The interpreter and the declared check, placeholders replaced.
        cwd: The head tree the check runs in.
        env: The environment, already scrubbed of secrets.
        timeout_s: Seconds the check may run.
        shell: Must be False; present so a caller's intent is checked.

    Returns:
        The exit code and the merged stdout and stderr. The check is
        waited on, not its output pipes: a process it leaves behind holds
        no pipe open and is killed with its session.

    Raises:
        ValueError: ``shell`` is True (NG3: no shell, ever).
        subprocess.TimeoutExpired: The check outlived ``timeout_s``.
    """
    if shell:
        raise ValueError("a generated-artefact check never runs through a shell")
    adopting = _adopt_orphans()
    before = _children()
    with tempfile.TemporaryFile() as output:
        proc = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=dict(env),
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            proc.wait(timeout=timeout_s)
        finally:
            _kill_group(proc)
            if adopting:
                _kill_orphans(before)
        output.seek(0)
        text = output.read().decode("utf-8", errors="replace")
    return RunResult(proc.returncode, text)


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    """Kill every process the check left in its session, then reap the check.

    The check is pull-request code: a process it leaves behind could
    rewrite the evidence after ``repoach review generated`` wrote it, so
    nothing it started may outlive its run.
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        _log.debug("generated.check_group_gone", pid=proc.pid)
    proc.wait()


_PR_SET_CHILD_SUBREAPER = 36
"""``prctl`` option making this process adopt its orphaned descendants (Linux)."""

_ORPHAN_SWEEPS = 50
_ORPHAN_SWEEP_PAUSE_S = 0.02


def _adopt_orphans() -> bool:
    """Make this process the subreaper of its descendants; False where the kernel cannot.

    A process the check detaches from its session (``setsid``, a double
    fork) escapes :func:`_kill_group`; as a subreaper, this process
    adopts it when its parent dies, and :func:`_kill_orphans` kills it.
    """
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return bool(libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0)
    except (OSError, AttributeError) as exc:
        _log.warning("generated.subreaper_unavailable", exc=type(exc).__name__)
        return False


def _children() -> set[int]:
    """Return the pids whose parent is this process (read from ``/proc``)."""
    me = str(os.getpid())
    return {
        int(entry.name)
        for entry in Path("/proc").iterdir()
        if entry.name.isdigit() and _parent_of(entry) == me
    }


def _parent_of(proc_dir: Path) -> str:
    """Return the parent pid ``/proc/<pid>/stat`` names, empty when the process is gone."""
    try:
        stat = (proc_dir / "stat").read_text()
    except OSError as exc:
        _log.debug("generated.proc_gone", pid=proc_dir.name, exc=type(exc).__name__)
        return ""
    fields = stat.rsplit(")", 1)[-1].split()
    return fields[1] if len(fields) > 1 else ""


def _kill_orphans(before: set[int]) -> None:
    """Kill and reap every child adopted since ``before``, until none is left.

    Sweeps a few times: a grandchild reaches this process only once its
    own parent has died.
    """
    quiet = 0
    for _ in range(_ORPHAN_SWEEPS):
        adopted = _children() - before
        if not adopted:
            quiet += 1
            if quiet >= 3:
                return
            time.sleep(_ORPHAN_SWEEP_PAUSE_S)
            continue
        quiet = 0
        for pid in adopted:
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except (ProcessLookupError, ChildProcessError):
                _log.debug("generated.orphan_gone", pid=pid)
    _log.warning("generated.orphans_left", left=sorted(_children() - before))


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
    declared = (*config.project.languages, *languages)
    refuse_code_globs(path, base_toml_text, config.review.generated, code_extensions(declared))
    return _entries(config)


def code_extensions(languages: Sequence[str]) -> tuple[str, ...]:
    """Return the file extensions of every named language (``[project].languages``).

    Args:
        languages: Language names; unknown names contribute nothing.

    Returns:
        The extensions, with their dot, in order, each once.
    """
    known = {language.value for language in Language}
    extensions: list[str] = []
    for name in languages:
        spec = spec_for(Language(name)) if name in known else None
        if spec is not None:
            extensions.extend(ext for ext in spec.extensions if ext not in extensions)
    return tuple(extensions)


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
    return load_generated_entries(decoded, Path(f"{base_ref}:{CONFIG_FILE_NAME}"), ())


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

    The artefact itself never counts as its own source, and a path that
    is a source is not listed again as a producer input.

    Args:
        entry: The artefact's declaration.
        path: The artefact's changed path.
        diff_files: The paths the diff changes.

    Returns:
        The changed sources and the changed producer inputs, in diff order.
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

    Args:
        path: The artefact's changed path.
        entry: Its declaration.
        before: Its content at the base, ``None`` when it is new.
        after: Its content at the head, ``None`` when the diff deletes it.
        diff_files: The paths the diff changes.

    Returns:
        ``generated artefact <path>: <before> -> <after> bytes, sha256
        <before7> -> <after7>; sources changed in this diff: <list or
        none>; proved by: <check>``, plus the changed producer inputs.
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
    """The last :data:`GENERATED_OUTPUT_TAIL_LINES` non-empty lines of ``output``, control characters removed."""
    lines = [_CONTROL_RE.sub("", line) for line in output.splitlines()]
    lines = [line for line in lines if line.strip()]
    return "\n".join(lines[-GENERATED_OUTPUT_TAIL_LINES:])


def _inside(head_dir: Path, script: str) -> bool:
    """Whether ``script`` is a regular file of the head tree, reached without a symlink.

    The D3 base run writes the base script over ``script``, so a symlink,
    or a directory symlink on the way, would make that write land outside
    the tree.
    """
    current = head_dir
    for part in Path(script).parts:
        current = current / part
        if current.is_symlink():
            return False
    return current.is_file()


class ScriptTamperedError(RuntimeError):
    """The check script became a symlink, or left the head tree, while the base script ran."""


@contextmanager
def _script_at(head_dir: Path, script: str, content: bytes | None) -> Iterator[None]:
    """Put ``content`` at ``script`` in the head tree for one run, then restore it (D3).

    The run is pull-request code: by the time it returns every process it
    started is dead, but it may have turned ``script`` (or a directory on
    its way) into a symlink. The head script is then not written back
    through it.

    Raises:
        ScriptTamperedError: ``script`` is no longer a plain file of the tree.
    """
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
        if not _inside(head_dir, script):
            raise ScriptTamperedError(
                f"check script {script} changed into a symlink during the run"
            )
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
    if not _inside(head_dir, entry.check[0]):
        return (
            GeneratedState.STALE,
            None,
            0,
            f"check script {entry.check[0]} is a symlink or leaves the head tree",
        )
    argv = _interpreted(_declared_argv(entry, head_sha, base_sha), head_dir)
    started = budget.clock()
    try:
        with _script_at(head_dir, entry.check[0], script_content):
            result = runner(argv, cwd=head_dir, env=env, timeout_s=allowed, shell=False)
    except subprocess.TimeoutExpired:
        ms = round((budget.clock() - started) * 1000)
        return GeneratedState.SKIPPED_COST, None, ms, f"check not run within {allowed:g} s: {path}"
    except ScriptTamperedError as exc:
        ms = round((budget.clock() - started) * 1000)
        _log.warning("generated.check_script_tampered", path=path)
        return GeneratedState.STALE, None, ms, str(exc)
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
    budget_s: int = GENERATED_CHECKS_TOTAL_BUDGET_S,
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
        if not _inside(head_dir, script):
            checks.append(
                replace(
                    template,
                    state=GeneratedState.STALE,
                    output_tail=f"check script {script} is a symlink or leaves the head tree",
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
    """Return a ``generated_check_not_run`` row for each touched artefact present at the head.

    Args:
        touched: The declared artefacts the diff changes.
        head_dir: The reviewed head tree.
        head_sha: The reviewed head.
        changed_files: The paths the diff changes.

    Returns:
        One run per touched artefact present at the head.
    """
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
    """Name why ``check`` does not show the derivation (the finding's ``verification_result``).

    Args:
        check: A run that did not verify the derivation.

    Returns:
        The cause a finding stores as its ``verification_result``.
    """
    if check.state is GeneratedState.NO_SOURCE_CHANGED:
        return "no source changed"
    if check.state is GeneratedState.STALE:
        prefix = f"{check.script_ref} " if check.runs_for_path > 1 else ""
        if check.exit_code is None:
            return f"{prefix}check failed: {_last_line(check.output_tail)}"
        return f"{prefix}check exited {check.exit_code}: {_last_line(check.output_tail)}"
    if check.state is GeneratedState.CHECK_ABSENT and check.argv:
        return f"check script {check.argv[0]} absent at {check.head_sha[:7]}"
    return _last_line(check.output_tail) or check.state.value


def by_path(checks: Sequence[GeneratedCheck]) -> dict[str, list[GeneratedCheck]]:
    """Group runs by artefact path, keeping the order of first appearance.

    Args:
        checks: Runs of any artefacts.

    Returns:
        ``{path: its runs}``.
    """
    grouped: dict[str, list[GeneratedCheck]] = {}
    for check in checks:
        grouped.setdefault(check.path, []).append(check)
    return grouped


def path_verified(runs: Sequence[GeneratedCheck]) -> bool:
    """Whether every run of one path verified the derivation (a source changed, exit 0).

    Args:
        runs: Every run of one artefact.

    Returns:
        True when there is at least one run and each verified.
    """
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
    for path, runs in by_path(checks).items():
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
    """Persist :func:`generated_findings` once per head; return how many were new.

    Args:
        db_path: The findings ledger.
        checks: The runs of this head.
        pr_number: The pull request.
        round_n: The review round.

    Returns:
        How many findings were new at the head.
    """
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


def evidence_payload(
    *,
    pr_number: int,
    head_sha: str,
    base_sha: str,
    checks: Sequence[GeneratedCheck],
    timing: GeneratedTiming,
) -> dict[str, Any]:
    """Build the ``format:review-generated-json`` evidence document.

    Args:
        pr_number: The pull request.
        head_sha: The head the checks ran at.
        base_sha: The merge base the diff was read from.
        checks: The runs.
        timing: The measured delay.

    Returns:
        The JSON-ready document.
    """
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
    recorded = by_path([GeneratedCheck.from_json(item) for item in payload.get("checks", [])])
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

    Args:
        evidence: The evidence file, ``None`` when none was given.
        touched: The declared artefacts the diff changes.
        head_sha: The reviewed head.
        head_dir: The reviewed head tree.
        changed_files: The paths the diff changes.

    Returns:
        The runs to record and the evidence's timing (``None`` without usable evidence).
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
    """The ``generated`` array of the ``review pr`` JSON (G5).

    Args:
        checks: The runs of this head.

    Returns:
        One JSON entry per run.
    """
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
    """The report lines naming what proved each artefact, or why nothing did (G5, G7).

    Args:
        checks: The runs of this head.
        timing: The measured delay; its line is printed when given and there were runs.

    Returns:
        The lines, in declaration order.
    """
    lines: list[str] = []
    for path, runs in by_path(checks).items():
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


def settle_at_head(
    finding: Finding, settlements: Mapping[str, ReverifyOutcome]
) -> ReverifyOutcome | None:
    """Settle an older-head finding on a declared path from the head's rows, without a refuter.

    Applies to a ``generated_artefact`` finding and to the oversized
    finding of a path the base now declares; ``None`` for every other
    finding or a path with no row at the head.

    Args:
        finding: An unsettled finding of an older head.
        settlements: The outcomes :func:`~repoach.review.generated_store.settlements_at_head` computed at the head.

    Returns:
        ``(state, method, reason)``, or ``None`` when the finding is not settled here.
    """
    if finding.finder not in _SETTLEABLE_FINDERS:
        return None
    if finding.claim_type not in {ClaimType.GENERATED_ARTEFACT, ClaimType.SPEC_GAP}:
        return None
    return settlements.get(finding.file)


__all__ = [
    "EVIDENCE_FORMAT",
    "GENERATED_FINDER",
    "GeneratedCheck",
    "GeneratedEntry",
    "GeneratedEvidenceMismatch",
    "GeneratedEvidenceMismatchError",
    "GeneratedState",
    "GeneratedTiming",
    "ReverifyOutcome",
    "ReviewScope",
    "RunResult",
    "Runner",
    "by_path",
    "check_cause",
    "code_extensions",
    "declared_at_base",
    "evidence_payload",
    "generated_findings",
    "load_generated_entries",
    "load_review_evidence",
    "path_verified",
    "record_generated_findings",
    "report_json",
    "report_lines",
    "review_scope",
    "run_checks",
    "settle_at_head",
    "subprocess_runner",
    "summarise",
    "touched_artefacts",
]
