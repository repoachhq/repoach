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

SP-REFUTER-JUDGES-WHAT-IT-CAN: every proposed finding that enters
:func:`judge_findings_for_pr` leaves it in exactly one state —
``verified``, ``refuted``, ``deferred`` or ``unverified`` — and the two
verdict-less states carry a reason from a closed set
(:data:`DEFERRAL_REASONS`, :data:`UNVERIFIED_REASONS`). Nothing is
excluded silently: a finding the refuter does not judge is named, with
why, in :class:`JudgingReport`.

Measured on Yoob MRs !324/!327 (2026-10-25), the judge refuted 13 of 13
findings and an independent re-read found 8 of those verdicts wrong,
each reasoning "<X> is not in the evidence, so the claim cannot be
confirmed": not seeing the code was read as the claim being false. The
judge now answers one of three verdicts — ``refuted``, ``verified`` or
``cannot_judge`` — and both ``cannot_judge`` and a refutation whose
reasoning only says the code was not visible
(:data:`_UNSEEN_REASONING`) defer the finding with
:data:`EVIDENCE_OUTSIDE_WINDOW`: it stays ``proposed``, counted as not
validated, exactly as a cap-deferred finding. The evidence itself is the
whole cited file, the head-tree search of what the claim names, the
PR's hunks on that file and the cited spec section, filling the
refuter's own ``context_tokens`` (:mod:`refuter_evidence`); what was
served, what was not and its size are recorded on each outcome as
``evidence``, ``evidence_truncated`` and ``evidence_tokens``.
"""

from __future__ import annotations

import json
import re
import secrets
from collections import Counter
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..agent_engine.agent_loop import AgentLoop
from ..core.logging import get_logger
from ..core.resources import prompts_dir
from ..gates.config import (
    REFUTER_CONTEXT_TOKENS_FLOOR,
    resolve_refuter_context_tokens,
    resolve_refuter_max_judged,
)
from ..llm.capability import CapabilityTier
from .findings import (
    JUDGED_CLAIM_TYPES,
    ClaimType,
    Finding,
    FindingStatus,
    fetch_findings,
    update_finding_status,
)
from .hallucination_guard import make_repo_file_reader
from .refuter_evidence import EvidenceBundle, build_evidence, estimate_tokens
from .spec import load_spec

_log = get_logger(__name__)

_PROMPTS_DIR = prompts_dir()
_PERSONA = "refuter_0.3.0.md"

_VERDICT_LINE = re.compile(r"^VERDICT:\s*(\{.*\})\s*$")
"""The judge's verdict line — column one, nothing after the object.

Injection-resistant by construction (SP-REFUTER-INJECTION-HARDEN):
every evidence line is prefixed with its line number by
:func:`_evidence_excerpt`, so PR-head content can never produce a line
that starts with ``VERDICT:`` at column one inside the prompt or an
echoed reply."""

Verdict = Literal["refuted", "verified", "cannot_judge"]
"""The three answers of the judge's output contract (persona v0.3.0)."""

_VERDICT_BY_NAME: dict[str, Verdict] = {
    "refuted": "refuted",
    "verified": "verified",
    "cannot_judge": "cannot_judge",
}

_UNSEEN_REASONING = re.compile(
    r"not (?:in|within|part of|present in|included in|shown in|visible in|provided in) "
    r"the (?:evidence|window|excerpt|provided code|code shown|snippet)"
    r"|cannot be (?:confirmed|verified|determined|assessed|established)"
    r"|can ?not (?:see|confirm|verify|determine|assess)"
    r"|unable to (?:see|confirm|verify|determine|assess)"
    r"|\bunseen\b|not visible|outside (?:the |of the )?(?:evidence |cited |provided )?window",
    re.IGNORECASE,
)
"""A refutation that only says the code was not visible — deferred, never refuted."""

EVIDENCE_OUTSIDE_DIFF = "evidence outside the diff"
"""The cited file does not resolve to a file inside ``repo_root``."""

CHECKOUT_NOT_HEAD = "checkout is not the MR head ({sha7})"
"""Template of the reason when the cited file is in the diff but absent from
a tree that is not at the reviewed head (the evidence exists, elsewhere)."""

EVIDENCE_UNREADABLE = "evidence unreadable"
"""The cited file exists but could not be read, or the cited lines are not in it."""

EVIDENCE_OUTSIDE_WINDOW = "evidence outside window"
"""The judge could not see the code the claim concerns (``cannot_judge``)."""

EVIDENCE_NOT_SERVED = "evidence outside window ({identifiers} not served)"
"""Template of the reason when the claim names an identifier none of whose hits were served.

The search timed out, could not run, or the budget, the hit cap or the
deadline left every hit out: the judge is not called, since it could
only refute what it cannot see."""

VERDICT_UNPARSEABLE = "verdict unparseable"
"""The judge answered without a well-formed ``VERDICT:`` line."""

JUDGE_CALL_FAILED = "judge call failed"
"""Prefix of the reason when the judge raised; the exception type name follows."""

JUDGING_CAP_REACHED = "judging cap {cap} reached"
"""Template of the reason a judgeable finding past the cap carries."""

NOT_YET_MECHANICALLY_VERIFIED = "not yet mechanically verified"
"""A non-judged claim type the mechanical verifiers have not attempted yet."""

DEFERRAL_REASONS = (
    EVIDENCE_OUTSIDE_DIFF,
    CHECKOUT_NOT_HEAD,
    EVIDENCE_UNREADABLE,
    JUDGING_CAP_REACHED,
    EVIDENCE_OUTSIDE_WINDOW,
    EVIDENCE_NOT_SERVED,
    VERDICT_UNPARSEABLE,
    JUDGE_CALL_FAILED,
)
"""The closed set of reasons a ``deferred`` finding carries (templates unexpanded)."""

UNVERIFIED_REASONS = (NOT_YET_MECHANICALLY_VERIFIED,)
"""The closed set of reasons an ``unverified`` finding carries."""

RefutationState = Literal["verified", "refuted", "deferred", "unverified"]
"""The one state every finding entering :func:`judge_findings_for_pr` ends in."""

_STATES: tuple[RefutationState, ...] = ("verified", "refuted", "deferred", "unverified")

JUDGE_MAX_OUTPUT_TOKENS = 800
"""Tokens the judge may answer with, reserved out of the context budget."""

CONTRACT_RESERVE_TOKENS = 4096
"""Tokens kept free for the reply contract (SP-AGENT-CONTEXT-LOOP ``contract_reserve_tokens``)."""

Judge = Callable[[str], str]
"""A judge takes the rendered prompt and returns the raw model reply."""


@dataclass(frozen=True)
class Refutation:
    """What the refuter did with one proposed finding.

    Attributes:
        finding_id: Ledger row id (``None`` for an unrecorded finding).
        file: The cited file.
        line_start: First cited line.
        claim_type: The finding's claim type.
        state: ``verified`` / ``refuted`` (a verdict was recorded),
            ``deferred`` (judgeable, no verdict this pass) or
            ``unverified`` (not judgeable yet).
        reason: For ``deferred`` / ``unverified``, one reason from
            :data:`DEFERRAL_REASONS` / :data:`UNVERIFIED_REASONS`
            (cap and exception filled in); for a verdict, the judge's
            reasoning.
        evidence: What the judge was served, one entry per source
            (:attr:`~repoach.review.refuter_evidence.EvidenceBundle.reads`);
            empty when the judge was not called.
        detail: The judge's reasoning when it answered without a
            verdict (``evidence outside window``), else empty.
        evidence_truncated: Items the budget or a hit cap left
            unserved; the judge was told its evidence is partial.
        evidence_tokens: Size of the evidence served, under
            :data:`~repoach.review.refuter_evidence.TOKEN_ESTIMATOR`.
    """

    finding_id: int | None
    file: str
    line_start: int
    claim_type: ClaimType
    state: RefutationState
    reason: str
    evidence: tuple[str, ...] = ()
    detail: str = ""
    evidence_truncated: tuple[str, ...] = ()
    evidence_tokens: int = 0

    def as_json(self) -> dict[str, object]:
        """Return the finding entry of the ``review pr`` JSON payload."""
        refutation: dict[str, object] = {
            "state": self.state,
            "reason": self.reason,
            "evidence": list(self.evidence),
            "evidence_truncated": list(self.evidence_truncated),
            "evidence_tokens": self.evidence_tokens,
        }
        if self.detail:
            refutation["detail"] = self.detail
        return {
            "id": self.finding_id,
            "file": self.file,
            "line": self.line_start,
            "claim_type": self.claim_type.value,
            "refutation": refutation,
        }


@dataclass(frozen=True)
class JudgingReport:
    """Every finding :func:`judge_findings_for_pr` saw, each with its state.

    Attributes:
        outcomes: One :class:`Refutation` per proposed finding, in the
            order the refuter considered them.
        model_calls: Judge invocations made (a raising judge counts).
        stale: Proposed findings recorded at another head (an earlier
            run), left untouched and not in ``outcomes``.
    """

    outcomes: tuple[Refutation, ...] = ()
    model_calls: int = 0
    stale: int = 0

    @property
    def counts(self) -> dict[str, int]:
        """Return ``{"verified", "refuted", "deferred", "unverified"}`` counts."""
        tally = Counter(outcome.state for outcome in self.outcomes)
        return {state: tally.get(state, 0) for state in _STATES}

    @property
    def evidence_tokens(self) -> int:
        """Return the evidence served to the judge across every outcome, in tokens.

        Reported on the run line only; it never feeds back into the
        evidence, which ``context_tokens`` alone bounds.
        """
        return sum(outcome.evidence_tokens for outcome in self.outcomes)

    @property
    def deferrals(self) -> tuple[Refutation, ...]:
        """Return the outcomes with no verdict (``deferred`` and ``unverified``)."""
        return tuple(o for o in self.outcomes if o.state in ("deferred", "unverified"))

    def summary_line(self) -> str:
        """Return the one stderr line naming every state and reason.

        Example: ``refuter: judged 3, refuted 1, deferred 2 (evidence
        unreadable ×2), unverified 1 (not yet mechanically verified),
        evidence_tokens total 41250``.
        """
        counts = self.counts
        judged = counts["verified"] + counts["refuted"]
        parts = [
            f"judged {judged}",
            f"refuted {counts['refuted']}",
            self._bucket("deferred"),
            self._bucket("unverified"),
        ]
        if self.stale:
            parts.append(f"stale: {self.stale} (older heads)")
        parts.append(f"evidence_tokens total {self.evidence_tokens}")
        return "refuter: " + ", ".join(parts)

    def _bucket(self, state: RefutationState) -> str:
        """Render ``<state> <n> (<reason> ×k, …)`` for one verdict-less state."""
        reasons = Counter(o.reason for o in self.outcomes if o.state == state)
        total = sum(reasons.values())
        if not total:
            return f"{state} 0"
        named = ", ".join(
            reason if n == 1 else f"{reason} ×{n}" for reason, n in reasons.most_common()
        )
        return f"{state} {total} ({named})"


def make_refuter_judge() -> Judge:
    """Build the production judge: an OPUS-tier one-shot over the proxy.

    OPUS is a different chain than the SONNET finders (the redesign's
    "independent chain" requirement) and brings heavier reasoning to
    the verdict. Built lazily by :func:`judge_findings_for_pr` so a
    review with no judged findings never spins up the loop.
    """
    loop = AgentLoop(
        capability=CapabilityTier.OPUS, max_tokens=JUDGE_MAX_OUTPUT_TOKENS, temperature=0.0
    )
    return lambda prompt: loop.run_oneshot(prompt).text


def _cited_file_inside(finding: Finding, repo_root: Path) -> bool:
    """Return True when the cited path resolves to a file inside ``repo_root``."""
    root = repo_root.resolve()
    try:
        target = (root / finding.file).resolve()
    except (OSError, RuntimeError) as exc:
        _log.debug("refuter.cited_path_unresolvable", path=finding.file, error=type(exc).__name__)
        return False
    return target.is_relative_to(root) and target.is_file()


@dataclass(frozen=True)
class Judgement:
    """What :func:`judge_finding` concluded about one finding.

    Attributes:
        status: ``REFUTED``, ``VERIFIED``, or ``PROPOSED`` (no verdict).
        reason: The judge's reasoning for a verdict, else one reason of
            :data:`DEFERRAL_REASONS`.
        bundle: The evidence served; ``None`` when the judge was not called.
        detail: The judge's reasoning behind :data:`EVIDENCE_OUTSIDE_WINDOW`.
    """

    status: FindingStatus
    reason: str
    bundle: EvidenceBundle | None = None
    detail: str = ""

    @property
    def evidence(self) -> tuple[str, ...]:
        """Return the sources served, one entry each (empty when none)."""
        return self.bundle.reads if self.bundle else ()

    @property
    def truncated(self) -> tuple[str, ...]:
        """Return the items not served (empty when the evidence is whole)."""
        return self.bundle.truncated if self.bundle else ()

    @property
    def tokens(self) -> int:
        """Return the size of the evidence served (0 when none)."""
        return self.bundle.tokens if self.bundle else 0


def _evidence_budget(finding: Finding, context_tokens: int) -> int:
    """Return the tokens the evidence may take: the context less prompt and reserves."""
    overhead = estimate_tokens(_render_prompt(finding, ""))
    return max(0, context_tokens - overhead - CONTRACT_RESERVE_TOKENS - JUDGE_MAX_OUTPUT_TOKENS)


def _evidence_excerpt(
    finding: Finding,
    repo_root: Path,
    absent_reason: str = EVIDENCE_OUTSIDE_DIFF,
    *,
    context_tokens: int = REFUTER_CONTEXT_TOKENS_FLOOR,
    diff: str = "",
    specs: Sequence[tuple[str, str]] = (),
) -> tuple[EvidenceBundle | None, str]:
    """Return ``(evidence bundle, "")``, or ``(None, deferral reason)``.

    The bundle fills ``context_tokens`` less the prompt and the reply
    reserves (:func:`refuter_evidence.build_evidence`). The reason is
    ``absent_reason`` (by default :data:`EVIDENCE_OUTSIDE_DIFF`) when the
    cited file is not a file inside ``repo_root``, and
    :data:`EVIDENCE_UNREADABLE` when it is but cannot be read or holds
    none of the cited lines.
    """
    if not _cited_file_inside(finding, repo_root):
        return None, absent_reason
    reader = make_repo_file_reader(repo_root)
    source = reader(finding.file)
    if source is None:
        return None, EVIDENCE_UNREADABLE
    bundle = build_evidence(
        source,
        repo_root=repo_root,
        reader=reader,
        path=finding.file,
        line_start=finding.line_start,
        line_end=finding.line_end,
        claim=finding.claim,
        budget_tokens=_evidence_budget(finding, context_tokens),
        diff=diff,
        specs=specs,
        claim_is_spec_gap=finding.claim_type is ClaimType.SPEC_GAP,
    )
    if bundle is None:
        return None, EVIDENCE_UNREADABLE
    return bundle, ""


def claim_specs(
    finding: Finding, repo_root: Path, pr_spec: tuple[str, str] | None
) -> tuple[tuple[str, str], ...]:
    """Return the ``(spec id, markdown)`` the finding may cite: the PR's, then those its claim names.

    A spec id the claim names that no file under the head tree's specs
    directory matches is skipped (logged): the claim's spec section is
    then simply not served.
    """
    specs: list[tuple[str, str]] = [pr_spec] if pr_spec else []
    for spec_id in dict.fromkeys(re.findall(r"\bSP-[A-Z0-9]+(?:-[A-Z0-9]+)*\b", finding.claim)):
        if any(known == spec_id for known, _ in specs):
            continue
        try:
            specs.append((spec_id, load_spec(spec_id, root=repo_root).raw_markdown))
        except (FileNotFoundError, OSError, ValueError) as exc:
            _log.info("refuter.claim_spec_unresolved", spec_id=spec_id, error=type(exc).__name__)
    return tuple(specs)


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


def _verdict_of(data: dict[str, object]) -> tuple[Verdict, str] | None:
    """Return the ``(verdict, reasoning)`` one parsed verdict object carries, or ``None``.

    Accepts the v0.3.0 shape ``{"verdict": "refuted" | "verified" |
    "cannot_judge"}`` and the earlier ``{"refuted": <bool>}``. A
    ``refuted`` whose reasoning only says the code was not visible
    (:data:`_UNSEEN_REASONING`) becomes ``cannot_judge``.
    """
    reasoning = str(data.get("reasoning", ""))[:300]
    named = data.get("verdict")
    verdict: Verdict
    if isinstance(named, str) and named in _VERDICT_BY_NAME:
        verdict = _VERDICT_BY_NAME[named]
    elif isinstance(data.get("refuted"), bool):
        verdict = "refuted" if data["refuted"] else "verified"
    else:
        return None
    if verdict == "refuted" and _UNSEEN_REASONING.search(reasoning):
        verdict = "cannot_judge"
    return verdict, reasoning


def _parse_verdict(raw: str) -> tuple[Verdict, str] | None:
    """Extract ``(verdict, reasoning)`` from the judge's verdict line.

    Only a line starting with ``VERDICT:`` at column one counts, and
    the LAST such line with a well-formed object wins — never the first
    JSON blob in the reply, which prose quoted from the evidence window
    could plant (SP-REFUTER-INJECTION-HARDEN G2; the evidence itself
    cannot forge the line because every evidence line carries a
    line-number or section prefix). Returns ``None`` when no verdict
    line names one of the three verdicts — the caller leaves the
    finding ``proposed``, never defaulting to refuted.
    """
    verdict: tuple[Verdict, str] | None = None
    for line in raw.splitlines():
        match = _VERDICT_LINE.match(line)
        if match is None:
            continue
        try:
            data = json.loads(match.group(1))
        except (json.JSONDecodeError, ValueError) as exc:
            _log.debug("refuter.verdict_json_decode_failed", error=str(exc)[:120])
            continue
        if not isinstance(data, dict):
            continue
        verdict = _verdict_of(data) or verdict
    return verdict


def _cites_any(reasoning: str, identifiers: Sequence[str]) -> bool:
    """Return True when ``reasoning`` names one of ``identifiers`` as a word."""
    return any(
        re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", reasoning)
        for name in identifiers
    )


def judge_finding(
    finding: Finding,
    *,
    repo_root: Path,
    judge: Judge,
    absent_reason: str = EVIDENCE_OUTSIDE_DIFF,
    context_tokens: int = REFUTER_CONTEXT_TOKENS_FLOOR,
    diff: str = "",
    pr_spec: tuple[str, str] | None = None,
) -> Judgement:
    """Judge one finding and say what the judge was served.

    ``REFUTED`` when the judge proves the claim false over the evidence
    it sees, ``VERIFIED`` when the evidence proves it, both with the
    judge's reasoning. ``PROPOSED`` (leave it for a later round) with a
    reason from :data:`DEFERRAL_REASONS` when the evidence is outside
    the repository or unreadable (the judge is then never called), when
    the judge raised (``judge call failed: <exception type>``), when its
    verdict is unparseable, and when it answered ``cannot_judge``,
    refuted only because the code was not visible, or refuted on an
    identifier of which only part of the hits were served
    (:data:`EVIDENCE_OUTSIDE_WINDOW`). When the claim names an identifier
    of which nothing was served, the judge is not called
    (:data:`EVIDENCE_NOT_SERVED`). A cited file absent from
    ``repo_root`` is deferred with ``absent_reason``.

    Args:
        finding: The proposed finding.
        repo_root: The pinned head tree the evidence is read from.
        judge: The judge (tests inject a fake).
        absent_reason: Deferral reason when the cited file is absent.
        context_tokens: The refuter's context size the evidence fills.
        diff: The PR's unified diff (its hunks on the cited file are served).
        pr_spec: ``(spec id, markdown)`` of the PR's governing spec.
    """
    bundle, reason = _evidence_excerpt(
        finding,
        repo_root,
        absent_reason,
        context_tokens=context_tokens,
        diff=diff,
        specs=claim_specs(finding, repo_root, pr_spec),
    )
    if bundle is None:
        return Judgement(FindingStatus.PROPOSED, reason)
    if bundle.unserved:
        return Judgement(
            FindingStatus.PROPOSED,
            EVIDENCE_NOT_SERVED.format(identifiers=", ".join(bundle.unserved)),
            bundle,
        )
    try:
        raw = judge(_render_prompt(finding, bundle.text))
    except Exception as exc:
        _log.warning("refuter.judge_call_failed", exc=type(exc).__name__, error=str(exc)[:160])
        return Judgement(
            FindingStatus.PROPOSED, f"{JUDGE_CALL_FAILED}: {type(exc).__name__}", bundle
        )
    verdict = _parse_verdict(raw)
    if verdict is None:
        return Judgement(FindingStatus.PROPOSED, VERDICT_UNPARSEABLE, bundle)
    answer, reasoning = verdict
    if answer == "refuted" and _cites_any(reasoning, bundle.partial):
        answer = "cannot_judge"
    if answer == "cannot_judge":
        return Judgement(FindingStatus.PROPOSED, EVIDENCE_OUTSIDE_WINDOW, bundle, reasoning)
    status = FindingStatus.REFUTED if answer == "refuted" else FindingStatus.VERIFIED
    return Judgement(status, reasoning, bundle)


def refute_finding(
    finding: Finding,
    *,
    repo_root: Path,
    judge: Judge,
    absent_reason: str = EVIDENCE_OUTSIDE_DIFF,
) -> tuple[FindingStatus, str]:
    """Judge one finding, returning ``(new_status, reason)`` of :func:`judge_finding`."""
    judgement = judge_finding(
        finding, repo_root=repo_root, judge=judge, absent_reason=absent_reason
    )
    return judgement.status, judgement.reason


def configured_max_judged(repo_root: Path) -> int:
    """Return the refuter cap for ``repo_root``: env > ``repoach.toml`` > 10.

    See :func:`~repoach.gates.config.resolve_refuter_max_judged`.
    """
    return resolve_refuter_max_judged(repo_root)[0]


class LazyJudge:
    """Build the judge on its first call and count the calls made.

    A factory that raises (no provider, gateway down) raises again on
    every later call without being retried, so each finding it leaves
    unjudged is deferred with that exception named.
    """

    def __init__(self, factory: Callable[[], Judge]) -> None:
        self._factory = factory
        self._judge: Judge | None = None
        self._build_error: Exception | None = None
        self.calls = 0

    def __call__(self, prompt: str) -> str:
        """Run the judge on ``prompt``, building it first if needed."""
        self.calls += 1
        if self._build_error is not None:
            raise self._build_error
        if self._judge is None:
            try:
                self._judge = self._factory()
            except Exception as exc:
                self._build_error = exc
                raise
        return self._judge(prompt)


def _refutation(
    finding: Finding,
    state: RefutationState,
    reason: str,
    *,
    judgement: Judgement | None = None,
) -> Refutation:
    """Build the :class:`Refutation` of ``finding``, with the evidence ``judgement`` served."""
    return Refutation(
        finding_id=finding.id,
        file=finding.file,
        line_start=finding.line_start,
        claim_type=finding.claim_type,
        state=state,
        reason=reason,
        evidence=judgement.evidence if judgement else (),
        detail=judgement.detail if judgement else "",
        evidence_truncated=judgement.truncated if judgement else (),
        evidence_tokens=judgement.tokens if judgement else 0,
    )


def judge_findings_for_pr(
    db_path: Path,
    *,
    pr_number: int,
    repo_root: Path,
    head_sha: str | None,
    judge_factory: Callable[[], Judge] = make_refuter_judge,
    max_judged: int | None = None,
    diff_files: Collection[str] = (),
    tree_sha: str | None = None,
    review_head: str | None = None,
    scope_head: str | None = None,
    diff: str = "",
    pr_spec: tuple[str, str] | None = None,
    context_tokens: int | None = None,
) -> JudgingReport:
    """Judge the proposed findings of a PR and name what it could not judge.

    Judgeable findings are those of a judged claim type
    (``design`` / ``security`` / ``spec_gap``) and those the mechanical
    verifiers already attempted (``verify_attempts >= 1``). Every other
    proposed finding is reported ``unverified`` with
    :data:`NOT_YET_MECHANICALLY_VERIFIED`. A judgeable finding whose
    evidence cannot be read is deferred without a model call; once
    ``max_judged`` judge calls were made, each remaining judgeable
    finding is deferred with ``judging cap N reached``. The judge is
    built on the first call, so a PR with nothing readable to judge
    never builds one.

    Args:
        db_path: The findings ledger database.
        pr_number: PR whose proposed findings are judged.
        repo_root: Root the cited evidence is read from.
        head_sha: Commit stored on each verdict row; ``None`` becomes ``""``.
        judge_factory: Builds the judge (tests inject a fake).
        max_judged: Judge calls allowed; ``None`` reads
            ``[gates].refuter_max_judged`` (default 10).
        diff_files: Paths the PR's diff touches.
        tree_sha: Commit ``repo_root`` is at (``None`` when unknown).
        review_head: Head commit the review is about; defaults to
            ``head_sha``. When both it and ``tree_sha`` are known and
            differ, a cited file of the diff absent from ``repo_root``
            is deferred with ``checkout is not the MR head (<sha7>)``
            rather than ``evidence outside the diff``.
        scope_head: When given, only proposed findings recorded at this
            head (this run's) are judged; the others, from earlier runs
            or with an empty head, are counted in ``stale`` and left alone.
        diff: The PR's unified diff; each finding is served its hunks.
        pr_spec: ``(spec id, markdown)`` of the PR's governing spec.
        context_tokens: The refuter's context size; ``None`` reads
            ``[gates].refuter_context_tokens`` (default and floor 40 000).

    Returns:
        One :class:`Refutation` per proposed finding, plus the number of
        judge calls made.
    """
    cap = configured_max_judged(repo_root) if max_judged is None else max_judged
    budget = (
        resolve_refuter_context_tokens(repo_root)[0] if context_tokens is None else context_tokens
    )
    reviewed = review_head or head_sha
    tree_behind = bool(reviewed and tree_sha and tree_sha != reviewed)
    in_diff = frozenset(diff_files)
    proposed = fetch_findings(db_path, pr_number, status=FindingStatus.PROPOSED)
    stale = 0
    if scope_head:
        current = [f for f in proposed if f.head_sha == scope_head]
        stale = len(proposed) - len(current)
        proposed = current
    primary = [f for f in proposed if f.claim_type in JUDGED_CLAIM_TYPES]
    fallback = [
        f for f in proposed if f.claim_type not in JUDGED_CLAIM_TYPES and f.verify_attempts >= 1
    ]
    unverified = [
        f for f in proposed if f.claim_type not in JUDGED_CLAIM_TYPES and f.verify_attempts == 0
    ]
    judge = LazyJudge(judge_factory)
    outcomes: list[Refutation] = []
    for finding in primary + fallback:
        if judge.calls >= cap:
            outcomes.append(_refutation(finding, "deferred", JUDGING_CAP_REACHED.format(cap=cap)))
            continue
        absent = (
            CHECKOUT_NOT_HEAD.format(sha7=(reviewed or "")[:7])
            if tree_behind and finding.file in in_diff
            else EVIDENCE_OUTSIDE_DIFF
        )
        judgement = judge_finding(
            finding,
            repo_root=repo_root,
            judge=judge,
            absent_reason=absent,
            context_tokens=budget,
            diff=diff,
            pr_spec=pr_spec,
        )
        new_status, reason = judgement.status, judgement.reason
        if new_status is FindingStatus.PROPOSED:
            outcomes.append(_refutation(finding, "deferred", reason, judgement=judgement))
            continue
        if finding.id is not None:
            update_finding_status(
                db_path,
                finding.id,
                new_status,
                verification_method="refuter",
                verification_result=reason,
                checked_at_sha=head_sha or "",
            )
        state: RefutationState = "refuted" if new_status is FindingStatus.REFUTED else "verified"
        outcomes.append(_refutation(finding, state, reason, judgement=judgement))
    outcomes.extend(
        _refutation(finding, "unverified", NOT_YET_MECHANICALLY_VERIFIED) for finding in unverified
    )
    return JudgingReport(outcomes=tuple(outcomes), model_calls=judge.calls, stale=stale)
