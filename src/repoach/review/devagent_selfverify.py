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

The judge is a **hard blocker both on a verdict and on unavailability**
(SP-SELFVERIFY-FAIL-CLOSED, 2026-07-13, superseding the earlier fail-open
calibration): a judge that cannot produce a verdict — no judge configured, an
empty diff, a call that raises, an unparseable reply — yields
:class:`JudgeVerdict` with ``available=False``, and the gate does NOT report
``ok=True`` on the mechanical half alone. An unverifiable blocking gate must
block, not wave the work through; the failure is logged loudly and the reason
names ``judge_unavailable`` so the caller can distinguish it from a parsed
``compliant: false``. The branch diff handed to the judge is the agent's OWN,
untrusted content — any verdict-shaped JSON object embedded in it is
neutralized before the prompt is built, so it cannot be mistaken for the
judge's own answer (see :func:`_neutralize_diff_verdict_objects`).
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
from ..core.config import get_settings
from ..core.logging import get_logger
from ..llm.capability import CapabilityTier
from .coder_loop import run_ruff_gate
from .plan import ActionPlan
from .spec import SpecPlan
from .spec_gate import SpecCoverage, compute_spec_coverage, selector_present

_log = get_logger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts" / "review"
_PERSONA = "judge_selfverify_0.2.1.md"
_DIFF_CAP_CHARS = 100_000
_MAX_REFUTABLE_GAPS = 10
_NEUTRALIZED_VERDICT_MARKER = "[[selfverify: embedded verdict-shaped object neutralized]]"

ComplianceJudge = Callable[[str], str]
"""A judge takes the rendered prompt and returns the raw model reply."""


@dataclass
class JudgeGap:
    """One unmet-requirement gap reported by the semantic judge.

    Attributes:
        claim: The shortfall in words, spec-anchored.
        file: Repo-relative file the claim is about, present only when the gap
            asserts an absence with checkable evidence (see
            :func:`_refute_gaps`); ``None`` for a plain semantic gap.
        absent_pattern: A Python regex (matched with ``re.M`` against ``file``'s
            full text) that would match the thing claimed absent; ``None`` for a
            plain semantic gap.
    """

    claim: str
    file: str | None = None
    absent_pattern: str | None = None


@dataclass
class JudgeVerdict:
    """The semantic judge's outcome.

    Attributes:
        available: ``True`` when the judge produced a parseable verdict; ``False``
            when it could not (no judge, empty diff, the call raised, or the reply
            was unparseable) — in which case the gate fails CLOSED (does not
            report ``ok=True``) rather than passing on the mechanical half alone.
        compliant: The judge's verdict (meaningful only when ``available``).
        reasons: Short rationale (<= 300 chars).
        gaps: Concrete unmet requirements when not compliant, each optionally
            carrying evidence a mechanical refutation pass can check (see
            :func:`_refute_gaps`).
    """

    available: bool = False
    compliant: bool = False
    reasons: str = ""
    gaps: list[JudgeGap] = field(default_factory=list)


@dataclass
class SelfVerifyResult:
    """Outcome of one :func:`run_self_verify` call.

    Attributes:
        ok: The overall gate verdict — ``mechanical_ok and judge.available and
            judge.compliant``. ``True`` means the work may be handed to review;
            a judge that never produced a verdict fails the gate closed.
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
    prompt; an empty or failed diff makes the judge unavailable (fail-closed).
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
    """Substitute the spec, acceptance criteria, and the (neutralized) diff.

    *diff* is expected to already have passed through
    :func:`_neutralize_diff_verdict_objects` — the branch diff is the agent's OWN,
    untrusted content, and the persona's "The diff to judge" section frames it
    explicitly as evidence to read, not instructions to follow.
    """
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


def _neutralize_diff_verdict_objects(diff: str) -> str:
    """Redact any verdict-shaped JSON object embedded in *diff*.

    The diff is the agent's OWN branch content, judged as untrusted evidence — an
    adversarial branch could append a trailing object carrying a boolean
    ``compliant`` key (docstring, string literal, comment) hoping the judge either
    reads it as its real verdict or reflects it back verbatim in its reply, letting
    :func:`_parse_judge_verdict` pick it up downstream. Every top-level balanced
    ``{...}`` span in *diff* that parses as a JSON object with a boolean
    ``compliant`` key is replaced with :data:`_NEUTRALIZED_VERDICT_MARKER` before
    the diff is ever rendered into the judge prompt, so no such object reaches the
    judge (or a later parse step) intact.
    """
    neutralized = diff
    for span in _iter_balanced_objects(diff):
        try:
            data = json.loads(span)
        except (json.JSONDecodeError, ValueError) as exc:
            _log.debug("selfverify.diff_span_json_decode_failed", error=str(exc)[:120])
            continue
        if isinstance(data, dict) and isinstance(data.get("compliant"), bool):
            neutralized = neutralized.replace(span, _NEUTRALIZED_VERDICT_MARKER)
    return neutralized


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
    gaps: list[JudgeGap] = []
    if isinstance(raw_gaps, list):
        for g in raw_gaps:
            if isinstance(g, dict):
                claim = str(g.get("claim", ""))
                file_value = g.get("file")
                pattern_value = g.get("absent_pattern")
                gaps.append(
                    JudgeGap(
                        claim=claim,
                        file=str(file_value) if file_value is not None else None,
                        absent_pattern=str(pattern_value) if pattern_value is not None else None,
                    )
                )
            else:
                gaps.append(JudgeGap(claim=str(g)))
    return JudgeVerdict(
        available=True,
        compliant=chosen["compliant"],
        reasons=str(chosen.get("reasons", ""))[:300],
        gaps=gaps,
    )


def _judge_compliance(
    repo_root: Path, *, spec: SpecPlan, base: str, judge: ComplianceJudge | None
) -> JudgeVerdict:
    """Run the semantic judge; an unavailable judge yields ``available=False``.

    The caller (:func:`run_self_verify`) treats an unavailable judge as
    fail-closed — it does not report ``ok=True`` on the mechanical half alone.
    """
    if judge is None:
        _log.warning("selfverify.judge_unavailable", reason="no judge configured")
        return JudgeVerdict(reasons="no judge configured")
    diff = _branch_diff(repo_root, base)
    if not diff:
        _log.warning("selfverify.judge_unavailable", reason="empty diff")
        return JudgeVerdict(reasons="diff unavailable")
    prompt = _render_judge_prompt(
        spec.raw_markdown,
        _extract_acceptance_criteria(spec.raw_markdown),
        _neutralize_diff_verdict_objects(diff),
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


def _refute_gaps(verdict: JudgeVerdict, repo_root: Path) -> JudgeVerdict:
    """Mechanically refute evidence-bearing gaps and overturn when all fall.

    For at most :data:`_MAX_REFUTABLE_GAPS` evidence-bearing gaps (those carrying
    both ``file`` and ``absent_pattern``), attempt refutation: if ``file`` exists
    under *repo_root* and ``absent_pattern`` (compiled with ``re.M``) matches its
    text, the judge's absence claim is false — the gap is dropped and
    ``selfverify.gap_refuted`` is logged. A gap whose evidence is malformed (regex
    does not compile, path escapes the repo, or the file is missing/unreadable) is
    KEPT as blocking and logged as ``selfverify.gap_evidence_invalid`` (fail-closed,
    G4). Plain semantic gaps (no evidence) and gaps beyond the cap pass through
    unchecked.

    When the original verdict was non-compliant and every one of its gaps was
    refuted, the returned verdict is overturned to ``compliant=True`` with an empty
    gap list, and ``selfverify.verdict_overturned_by_refutation`` logs the full
    original verdict for audit. Any unexpected exception during the pass returns
    the ORIGINAL verdict unchanged, logging ``selfverify.refutation_failed``
    (fail-closed to the judge's word).

    Args:
        verdict: The parsed judge verdict (only acted on when ``available``).
        repo_root: Root evidence file paths resolve against.

    Returns:
        The verdict, with refuted gaps dropped and possibly overturned.
    """
    evidence_count = sum(
        1 for g in verdict.gaps if g.file is not None and g.absent_pattern is not None
    )
    if evidence_count > _MAX_REFUTABLE_GAPS:
        _log.warning(
            "selfverify.refutation_capped",
            evidence_count=evidence_count,
            cap=_MAX_REFUTABLE_GAPS,
        )
        return verdict
    try:
        surviving: list[JudgeGap] = []
        checked = 0
        refuted_count = 0
        for gap in verdict.gaps:
            has_evidence = gap.file is not None and gap.absent_pattern is not None
            if not has_evidence or checked >= _MAX_REFUTABLE_GAPS:
                surviving.append(gap)
                continue
            checked += 1
            try:
                candidate = (repo_root / gap.file).resolve()
                repo_resolved = repo_root.resolve()
                if repo_resolved not in candidate.parents or not candidate.is_file():
                    raise ValueError("evidence file missing or outside repo")
                text = candidate.read_text(encoding="utf-8")
                pattern = re.compile(gap.absent_pattern, re.M)
            except (OSError, ValueError, re.error) as exc:
                _log.warning(
                    "selfverify.gap_evidence_invalid",
                    claim=gap.claim,
                    file=gap.file,
                    pattern=gap.absent_pattern,
                    error=str(exc)[:160],
                )
                surviving.append(gap)
                continue
            if pattern.search(text) is not None:
                refuted_count += 1
                _log.info(
                    "selfverify.gap_refuted",
                    claim=gap.claim,
                    file=gap.file,
                    pattern=gap.absent_pattern,
                )
            else:
                surviving.append(gap)
        if (
            not verdict.compliant
            and verdict.gaps
            and refuted_count == len(verdict.gaps)
            and not surviving
        ):
            _log.info(
                "selfverify.verdict_overturned_by_refutation",
                original_compliant=verdict.compliant,
                original_reasons=verdict.reasons,
                original_gaps=[
                    {"claim": g.claim, "file": g.file, "absent_pattern": g.absent_pattern}
                    for g in verdict.gaps
                ],
            )
            return JudgeVerdict(
                available=verdict.available,
                compliant=True,
                reasons=verdict.reasons,
                gaps=[],
            )
        return JudgeVerdict(
            available=verdict.available,
            compliant=verdict.compliant,
            reasons=verdict.reasons,
            gaps=surviving,
        )
    except Exception as exc:
        _log.warning("selfverify.refutation_failed", error=str(exc)[:160])
        return verdict


def run_self_verify(
    repo_root: Path,
    *,
    spec: SpecPlan,
    plan: ActionPlan,
    suite_green: bool,
    base: str | None = None,
    judge: ComplianceJudge | None,
) -> SelfVerifyResult:
    """Verify the implemented work against the spec before the review handoff.

    Mechanical: every promised unit acceptance selector present + ``suite_green`` +
    ruff clean. Semantic: the judge's compliance verdict (fail-closed on
    unavailability). See the module docstring for the policy.

    Args:
        repo_root: The branch working tree.
        spec: The loaded spec (its ``raw_markdown`` feeds the judge).
        plan: The action plan supplying the acceptance selectors.
        suite_green: Whether the wrap-up unit suite already passed (not re-run).
        base: The base branch the diff is taken against. Defaults to
            :attr:`~repoach.core.config.Settings.integration_branch`,
            resolved at call time so an env/test override takes effect.
        judge: The compliance judge, or ``None`` (treated as unavailable).

    Returns:
        A :class:`SelfVerifyResult`; ``ok`` gates the push.
    """
    base = base if base is not None else get_settings().integration_branch
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
            verdict = _refute_gaps(verdict, repo_root)
        if verdict.available and not verdict.compliant:
            gap_claims = [g.claim for g in verdict.gaps]
            reasons.append(f"judge: not compliant — {verdict.reasons}; gaps={gap_claims}")
        elif not verdict.available:
            reasons.append(f"judge_unavailable: {verdict.reasons}")

    ok = mechanical_ok and verdict.available and verdict.compliant
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
    "JudgeGap",
    "JudgeVerdict",
    "SelfVerifyResult",
    "make_compliance_judge",
    "run_self_verify",
]
