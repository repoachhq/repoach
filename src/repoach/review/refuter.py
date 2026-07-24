"""Adversarial refuter for judged findings (SP-REFUTER, review redesign slice 5).

The mechanical verifiers (slice 4) leave ``design`` and ``security``
findings ``proposed`` — they are not checkable on disk. This module
judges them: an independent agent, on a heavier chain than the SONNET
finders (OPUS) and prompted to REFUTE the claim over the cited code
evidence, decides whether the finding stands. Refutation succeeds →
``refuted`` (a false positive); refutation fails → ``verified`` (a real
finding). Evidence the judge cannot read, or an unparseable verdict,
leaves the finding ``proposed`` for a later round. Dual-run: statuses
are recorded, no merge decision changes here.

SP-REFUTER-INJECTION-HARDEN (audit 2026-07-13 finding C3): the
evidence window is PR-head content — adversarial input. It is
delivered between per-prompt nonce markers it cannot forge, fence runs
inside it are neutralised, and the verdict is recovered from a
column-one ``VERDICT:`` line (last well-formed wins) that numbered
evidence lines can never produce. A wrongly refuted finding is no
longer buried forever: REFUTED re-opens to PROPOSED when a later
reviewer re-raises the claim (see :mod:`findings` /
:mod:`findings_bridge`).
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable
from pathlib import Path

from ..agent_engine.agent_loop import AgentLoop
from ..core.logging import get_logger
from ..llm.capability import CapabilityTier
from .findings import (
    JUDGED_CLAIM_TYPES,
    Finding,
    FindingStatus,
    fetch_findings,
    update_finding_status,
)
from .hallucination_guard import make_repo_file_reader

_log = get_logger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts" / "review"
_PERSONA = "refuter_0.2.1.md"

_VERDICT_LINE = re.compile(r"^VERDICT:\s*(\{.*\})\s*$")
"""The judge's verdict line — column one, nothing after the object.

Injection-resistant by construction (SP-REFUTER-INJECTION-HARDEN):
every evidence line is prefixed with its line number by
:func:`_evidence_excerpt`, so PR-head content can never produce a line
that starts with ``VERDICT:`` at column one inside the prompt or an
echoed reply."""

_MAX_JUDGED = 10
"""Cap on refuter calls per PR — judged findings are few; this bounds
cost on a pathological review."""

_EVIDENCE_CONTEXT = 25
"""Lines of context fetched on each side of a finding's cited range."""

Judge = Callable[[str], str]
"""A judge takes the rendered prompt and returns the raw model reply."""


def make_refuter_judge() -> Judge:
    """Build the production judge: an OPUS-tier one-shot over the proxy.

    OPUS is a different chain than the SONNET finders (the redesign's
    "independent chain" requirement) and brings heavier reasoning to
    the verdict. Built lazily by :func:`judge_findings_for_pr` so a
    review with no judged findings never spins up the loop.
    """
    loop = AgentLoop(capability=CapabilityTier.OPUS, max_tokens=800, temperature=0.0)
    return lambda prompt: loop.run_oneshot(prompt).text


def _evidence_excerpt(finding: Finding, repo_root: Path) -> str | None:
    """Return the cited code window, or ``None`` when it cannot be read."""
    reader = make_repo_file_reader(repo_root)
    source = reader(finding.file)
    if source is None:
        return None
    lines = source.splitlines()
    start = max(0, finding.line_start - 1 - _EVIDENCE_CONTEXT)
    end = min(len(lines), finding.line_end + _EVIDENCE_CONTEXT)
    window = lines[start:end]
    if not window:
        return None
    numbered = [f"{start + i + 1}: {line}" for i, line in enumerate(window)]
    return "\n".join(numbered)


def _neutralise_fences(evidence: str) -> str:
    """Render markdown fence runs inert inside the untrusted evidence.

    A run of three or more backticks in PR-head content could read as a
    fence terminator to the judge model even though the v0.2.0 template
    delivers evidence through nonce markers rather than a fence. Each
    backtick in such a run is space-separated (a triple backtick
    becomes backtick-space-backtick-space-backtick) — a visible,
    documented mutation that keeps the code legible while making the
    sequence structurally meaningless (SP-REFUTER-INJECTION-HARDEN G1).
    """
    return re.sub(r"`{3,}", lambda match: " ".join(match.group(0)), evidence)


def _render_prompt(finding: Finding, evidence: str) -> str:
    """Substitute the finding + evidence into the refuter persona template.

    The evidence travels between ``<<<EVIDENCE {nonce}>>>`` markers
    whose nonce is generated per prompt — PR-head content cannot know
    it, so it cannot close the data channel and inject instructions
    (SP-REFUTER-INJECTION-HARDEN G1). Fence runs inside the evidence
    are neutralised, and ``{EVIDENCE}`` is substituted LAST so
    placeholder-shaped text inside the window is never expanded.
    """
    template = (_PROMPTS_DIR / _PERSONA).read_text(encoding="utf-8")
    nonce = secrets.token_hex(8)
    return (
        template.replace("{NONCE}", nonce)
        .replace("{CLAIM_TYPE}", finding.claim_type.value)
        .replace("{FILE}", f"{finding.file}:{finding.line_start}")
        .replace("{CLAIM}", finding.claim)
        .replace("{EVIDENCE}", _neutralise_fences(evidence))
    )


def _parse_verdict(raw: str) -> tuple[bool, str] | None:
    """Extract ``(refuted, reasoning)`` from the judge's verdict line.

    Only a line starting with ``VERDICT:`` at column one counts, and
    the LAST such line with a well-formed object wins — never the first
    JSON blob in the reply, which prose quoted from the evidence window
    could plant (SP-REFUTER-INJECTION-HARDEN G2; the evidence itself
    cannot forge the line because every evidence line carries a
    line-number prefix). Returns ``None`` when no verdict line with a
    boolean ``refuted`` exists — the caller leaves the finding
    ``proposed``, never defaulting to refuted.
    """
    verdict: tuple[bool, str] | None = None
    for line in raw.splitlines():
        match = _VERDICT_LINE.match(line)
        if match is None:
            continue
        try:
            data = json.loads(match.group(1))
        except (json.JSONDecodeError, ValueError) as exc:
            _log.debug("refuter.verdict_json_decode_failed", error=str(exc)[:120])
            continue
        if not isinstance(data, dict) or not isinstance(data.get("refuted"), bool):
            continue
        verdict = data["refuted"], str(data.get("reasoning", ""))[:300]
    return verdict


def refute_finding(finding: Finding, *, repo_root: Path, judge: Judge) -> tuple[FindingStatus, str]:
    """Judge one finding, returning ``(new_status, reasoning)``.

    ``REFUTED`` when the judge refutes the claim, ``VERIFIED`` when it
    cannot, ``PROPOSED`` when the evidence is unreadable or the verdict
    is unparseable (leave it for a later round).
    """
    evidence = _evidence_excerpt(finding, repo_root)
    if evidence is None:
        return FindingStatus.PROPOSED, "evidence unreadable"
    try:
        raw = judge(_render_prompt(finding, evidence))
    except Exception as exc:
        _log.warning("refuter.judge_call_failed", error=str(exc)[:160])
        return FindingStatus.PROPOSED, "judge call failed"
    verdict = _parse_verdict(raw)
    if verdict is None:
        return FindingStatus.PROPOSED, "verdict unparseable"
    refuted, reasoning = verdict
    return (FindingStatus.REFUTED if refuted else FindingStatus.VERIFIED), reasoning


def judge_findings_for_pr(
    db_path: Path,
    *,
    pr_number: int,
    repo_root: Path,
    head_sha: str | None,
    judge_factory: Callable[[], Judge] = make_refuter_judge,
) -> dict[str, int]:
    """Judge every proposed design/security finding of a PR.

    The judge is built lazily on the first judged finding (no LLM loop
    is created for a review without any). At most :data:`_MAX_JUDGED`
    findings are judged; the rest stay ``proposed``.

    Returns:
        Counts ``{"verified": n, "refuted": n, "deferred": n}``.
    """
    counts = {"verified": 0, "refuted": 0, "deferred": 0}
    proposed = fetch_findings(db_path, pr_number, status=FindingStatus.PROPOSED)
    primary_targets = [f for f in proposed if f.claim_type in JUDGED_CLAIM_TYPES]
    fallback_targets = [
        f for f in proposed if f.claim_type not in JUDGED_CLAIM_TYPES and f.verify_attempts >= 1
    ]
    judged_targets = primary_targets + fallback_targets
    judge: Judge | None = None
    for finding in judged_targets[:_MAX_JUDGED]:
        if judge is None:
            judge = judge_factory()
        new_status, reasoning = refute_finding(finding, repo_root=repo_root, judge=judge)
        if new_status is FindingStatus.PROPOSED or finding.id is None:
            counts["deferred"] += 1
            continue
        update_finding_status(
            db_path,
            finding.id,
            new_status,
            verification_method="refuter",
            verification_result=reasoning,
            checked_at_sha=head_sha or "",
        )
        counts[new_status.value] += 1
    counts["deferred"] += max(0, len(judged_targets) - _MAX_JUDGED)
    return counts
