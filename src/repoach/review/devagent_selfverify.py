"""The Developer's self-verification gate (SP-DEVAGENT-SELFVERIFY).

Slice 3 of the real-coding-agent arc (see ``docs/devagent_architecture.md``). After
the agentic loop (slice 2) has implemented every plan step and the wrap-up suite is
green, the Developer verifies its OWN work against the spec before the result is
pushed and handed to the 4 reviewers. Two halves, both required:

* **Mechanical** — the spec's promised *unit* acceptance selectors are present
  (:func:`spec_gate.selector_present`), the wrap-up unit suite is green (passed in,
  not re-run), and a final :func:`coder_loop.run_ruff_gate` passes. A missing
  *integration* selector is a warning only, matching the wrap-up's existing policy
  (it warns, not fails, on a promised-but-absent integration test).
* **Semantic** — an independent LLM judge (mirroring :mod:`review.refuter`'s
  one-shot pattern) reads the spec + the branch diff and verdicts whether the
  implementation truly satisfies the spec, returning ``{compliant, reasons, gaps}``.

The judge is a **hard blocker on a verdict** (parsed ``compliant: false`` blocks the
push) but **fail-open on unavailability** (operator calibration, 2026-06-28): a
judge that cannot produce a verdict — proxy/chain outage, a call that raises, an
unparseable reply — yields :class:`JudgeVerdict` with ``available=False`` and does
NOT block; the gate proceeds on the mechanical result and logs loudly. An infra
blip must not bury a correct implementation, and the 4 reviewers remain the net.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..agent_engine.agent_loop import AgentLoop
from ..core.logging import get_logger
from ..llm.capability import CapabilityTier
from .coder_loop import run_ruff_gate
from .plan import ActionPlan
from .spec import SpecPlan
from .spec_gate import SpecCoverage, compute_spec_coverage, selector_present

_log = get_logger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts" / "review"
_PERSONA = "judge_selfverify_0.1.1.md"
_DIFF_CAP_CHARS = 100_000

ComplianceJudge = Callable[[str], str]
"""A judge takes the rendered prompt and returns the raw model reply."""


@dataclass
class JudgeVerdict:
    """The semantic judge's outcome.

    Attributes:
        available: ``True`` when the judge produced a parseable verdict; ``False``
            when it could not (no judge, empty diff, the call raised, or the reply
            was unparseable) — in which case the gate fails open and does not block.
        compliant: The judge's verdict (meaningful only when ``available``).
        reasons: Short rationale (<= 300 chars).
        gaps: Concrete unmet requirements when not compliant.
    """

    available: bool = False
    compliant: bool = False
    reasons: str = ""
    gaps: list[str] = field(default_factory=list)


@dataclass
class SelfVerifyResult:
    """Outcome of one :func:`run_self_verify` call.

    Attributes:
        ok: The overall gate verdict — ``mechanical_ok and (judge unavailable or
            judge compliant)``. ``True`` means the work may be handed to review.
        mechanical_ok: Unit selectors present + suite green + ruff clean.
        coverage: The full presence report (unit + integration) for the record.
        ruff_ok: The final ruff gate result.
        judge: The semantic verdict (see :class:`JudgeVerdict`).
        reasons: Human-readable blocking reasons (empty when ``ok``).
    """

    ok: bool
    mechanical_ok: bool
    coverage: SpecCoverage
    ruff_ok: bool
    judge: JudgeVerdict
    reasons: list[str] = field(default_factory=list)


def make_compliance_judge() -> ComplianceJudge:
    """Build the production judge: an OPUS-tier one-shot over the proxy.

    OPUS brings heavier reasoning to a spec-vs-diff compliance call than the
    coder/sonnet chains, mirroring :func:`review.refuter.make_refuter_judge`. Built
    lazily by the caller so a session that never reaches the gate spins up no loop.
    """
    loop = AgentLoop(capability=CapabilityTier.OPUS, max_tokens=1200, temperature=0.0)
    return lambda prompt: loop.run_oneshot(prompt, json_response=True).text


def _unit_selectors(plan: ActionPlan) -> list[str]:
    """Return the plan's promised *unit* selectors (steps only), de-duplicated."""
    seen: set[str] = set()
    ordered: list[str] = []
    for step in plan.steps:
        for selector in step.unit_tests:
            if selector not in seen:
                seen.add(selector)
                ordered.append(selector)
    return ordered


def _extract_acceptance_criteria(markdown: str) -> str:
    """Return the spec's ``## Acceptance Criteria`` section, or ``""`` when absent.

    Captures everything between the ``## Acceptance Criteria`` heading and the next
    ``##`` heading (or end of file), so the judge prompt can foreground the contract
    even though the spec is also supplied in full.
    """
    match = re.search(
        r"^##\s+Acceptance Criteria\s*\n(.*?)(?=^##\s|\Z)",
        markdown,
        re.DOTALL | re.MULTILINE | re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def _branch_diff(repo_root: Path, base: str) -> str:
    """Return ``git diff <base>...HEAD`` for the branch, capped, or ``""`` on error.

    The three-dot form diffs HEAD against its merge-base with *base*, so only the
    branch's own changes are judged. Capped at :data:`_DIFF_CAP_CHARS` to bound the
    prompt; an empty or failed diff makes the judge unavailable (fail-open).
    """
    git = shutil.which("git") or "git"
    try:
        proc = subprocess.run(
            [git, "diff", f"{base}...HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning("selfverify.diff_failed", error=str(exc)[:160])
        return ""
    if proc.returncode != 0:
        _log.warning("selfverify.diff_failed", stderr=proc.stderr.strip()[:160])
        return ""
    return proc.stdout[:_DIFF_CAP_CHARS]


def _render_judge_prompt(spec_markdown: str, acceptance_criteria: str, diff: str) -> str:
    """Substitute the spec, acceptance criteria, and diff into the judge persona."""
    template = (_PROMPTS_DIR / _PERSONA).read_text(encoding="utf-8")
    return (
        template.replace("{SPEC_PLAN}", spec_markdown)
        .replace("{ACCEPTANCE_CRITERIA}", acceptance_criteria or "(none stated)")
        .replace("{DIFF}", diff)
    )


def _iter_balanced_objects(raw: str) -> list[str]:
    """Return every top-level balanced ``{...}`` span in *raw*, in order.

    A simple brace scanner — unlike a single greedy regex it does not merge two
    separate objects (e.g. the persona's contract example followed by the real
    verdict) into one invalid span.
    """
    spans: list[str] = []
    depth = 0
    start = -1
    for i, ch in enumerate(raw):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0:
                spans.append(raw[start : i + 1])
    return spans


def _parse_judge_verdict(raw: str) -> JudgeVerdict | None:
    """Extract a :class:`JudgeVerdict` from the judge's JSON reply, or ``None``.

    Tolerant of prose or fences around the object, and of a reply carrying more than
    one object (e.g. an echoed example before the real verdict): the LAST balanced
    object with a boolean ``compliant`` wins. Returns ``None`` when no such object
    can be recovered.
    """
    chosen: dict | None = None
    for blob in _iter_balanced_objects(raw):
        try:
            data = json.loads(blob)
        except (json.JSONDecodeError, ValueError) as exc:
            _log.debug("selfverify.verdict_json_decode_failed", error=str(exc)[:120])
            continue
        if isinstance(data, dict) and isinstance(data.get("compliant"), bool):
            chosen = data
    if chosen is None:
        return None
    raw_gaps = chosen.get("gaps", [])
    gaps = [str(g) for g in raw_gaps] if isinstance(raw_gaps, list) else []
    return JudgeVerdict(
        available=True,
        compliant=chosen["compliant"],
        reasons=str(chosen.get("reasons", ""))[:300],
        gaps=gaps,
    )


def _judge_compliance(
    repo_root: Path, *, spec: SpecPlan, base: str, judge: ComplianceJudge | None
) -> JudgeVerdict:
    """Run the semantic judge; an unavailable judge fails open (``available=False``)."""
    if judge is None:
        _log.warning("selfverify.judge_unavailable", reason="no judge configured")
        return JudgeVerdict(reasons="no judge configured")
    diff = _branch_diff(repo_root, base)
    if not diff:
        _log.warning("selfverify.judge_unavailable", reason="empty diff")
        return JudgeVerdict(reasons="diff unavailable")
    prompt = _render_judge_prompt(
        spec.raw_markdown, _extract_acceptance_criteria(spec.raw_markdown), diff
    )
    try:
        raw = judge(prompt)
    except Exception as exc:
        _log.warning(
            "selfverify.judge_unavailable", reason="judge call failed", error=str(exc)[:160]
        )
        return JudgeVerdict(reasons="judge call failed")
    verdict = _parse_judge_verdict(raw)
    if verdict is None:
        _log.warning("selfverify.judge_unavailable", reason="verdict unparseable")
        return JudgeVerdict(reasons="verdict unparseable")
    _log.info("selfverify.judge_verdict", compliant=verdict.compliant, n_gaps=len(verdict.gaps))
    return verdict


def run_self_verify(
    repo_root: Path,
    *,
    spec: SpecPlan,
    plan: ActionPlan,
    suite_green: bool,
    base: str = "develop",
    judge: ComplianceJudge | None,
) -> SelfVerifyResult:
    """Verify the implemented work against the spec before the review handoff.

    Mechanical: every promised unit acceptance selector present + ``suite_green`` +
    ruff clean. Semantic: the judge's compliance verdict (fail-open on
    unavailability). See the module docstring for the policy.

    Args:
        repo_root: The branch working tree.
        spec: The loaded spec (its ``raw_markdown`` feeds the judge).
        plan: The action plan supplying the acceptance selectors.
        suite_green: Whether the wrap-up unit suite already passed (not re-run).
        base: The base branch the diff is taken against.
        judge: The compliance judge, or ``None`` (treated as unavailable).

    Returns:
        A :class:`SelfVerifyResult`; ``ok`` gates the push.
    """
    reasons: list[str] = []

    coverage = compute_spec_coverage(repo_root, spec_id=spec.id, plan=plan)
    unit_missing = [s for s in _unit_selectors(plan) if not selector_present(repo_root, s)]
    if unit_missing:
        reasons.append(f"promised unit tests absent at head: {unit_missing}")
    if coverage.missing and not unit_missing:
        _log.warning(
            "selfverify.integration_selectors_absent",
            spec_id=spec.id,
            missing=coverage.missing,
        )
    if not suite_green:
        reasons.append("wrap-up unit suite is not green")

    ruff_ok, ruff_tail = run_ruff_gate(repo_root)
    if not ruff_ok:
        reasons.append(f"ruff gate: {ruff_tail[-200:]}")

    mechanical_ok = suite_green and not unit_missing and ruff_ok

    if not mechanical_ok:
        verdict = JudgeVerdict(reasons="skipped (mechanical gate failed)")
    else:
        verdict = _judge_compliance(repo_root, spec=spec, base=base, judge=judge)
        if verdict.available and not verdict.compliant:
            reasons.append(f"judge: not compliant — {verdict.reasons}; gaps={verdict.gaps}")

    ok = mechanical_ok and (not verdict.available or verdict.compliant)
    _log.info(
        "selfverify.result",
        spec_id=spec.id,
        ok=ok,
        mechanical_ok=mechanical_ok,
        judge_available=verdict.available,
        judge_compliant=verdict.compliant,
    )
    return SelfVerifyResult(
        ok=ok,
        mechanical_ok=mechanical_ok,
        coverage=coverage,
        ruff_ok=ruff_ok,
        judge=verdict,
        reasons=reasons,
    )


__all__ = [
    "ComplianceJudge",
    "JudgeVerdict",
    "SelfVerifyResult",
    "make_compliance_judge",
    "run_self_verify",
]
