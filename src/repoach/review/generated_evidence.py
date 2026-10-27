"""The ``format:review-generated-json`` evidence and how the review reads it back.

The verification job writes it (:func:`evidence_payload`); the review job
loads it for the touched artefacts of the reviewed head and recomputes
everything it can derive itself (SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ
G2): the evidence is trusted for the check's exit code and output only.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..core.logging import get_logger
from .generated_types import (
    GeneratedCheck,
    GeneratedEntry,
    GeneratedState,
    GeneratedTiming,
    by_path,
    changed_inputs,
    declared_argv,
    sha256_hex,
    symlink_row,
    symlinked,
)

_log = get_logger(__name__)


EVIDENCE_FORMAT = "review-generated-json"
"""The ``format`` field of the evidence file (``format:review-generated-json``)."""


EVIDENCE_VERSION = 1
"""The evidence file's schema version."""


class GeneratedEvidenceMismatchError(ValueError):
    """The evidence file names another head or another artefact blob than the reviewed one."""


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
        if symlinked(head_dir, path):
            checks.append(symlink_row(entry, path, head_sha=head_sha, changed_files=changed_files))
            continue
        if not artefact.is_file():
            continue
        sources, producers = changed_inputs(entry, path, changed_files)
        checks.append(
            GeneratedCheck(
                path=path,
                argv=entry.check,
                head_sha=head_sha,
                artefact_sha256=sha256_hex(artefact.read_bytes()),
                sources_changed=sources,
                state=GeneratedState.NOT_RUN,
                exit_code=None,
                duration_ms=0,
                output_tail=f"no check evidence for {path} at {head_sha[:7]}",
                producer_inputs_changed=producers,
            )
        )
    return checks


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
        GeneratedEvidenceMismatchError: The evidence is of another format,
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
        if symlinked(head_dir, path):
            checks.append(
                symlink_row(touched[path], path, head_sha=head_sha, changed_files=changed_files)
            )
            continue
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
        blob = sha256_hex(artefact.read_bytes())
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
    argv = declared_argv(entry, head_sha, base_sha)
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
        except (GeneratedEvidenceMismatchError, ValueError, KeyError, OSError) as exc:
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


__all__ = [
    "EVIDENCE_FORMAT",
    "EVIDENCE_VERSION",
    "GeneratedEvidenceMismatchError",
    "checks_from_evidence",
    "evidence_payload",
    "load_review_evidence",
    "not_run_checks",
]
