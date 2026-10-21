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
"""

from __future__ import annotations

import json
import re
import secrets
from collections import Counter
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..agent_engine.agent_loop import AgentLoop
from ..core.logging import get_logger
from ..core.resources import prompts_dir
from ..gates.config import resolve_refuter_max_judged
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

_log = get_logger(__name__)

_PROMPTS_DIR = prompts_dir()
_PERSONA = "refuter_0.2.1.md"

_VERDICT_LINE = re.compile(r"^VERDICT:\s*(\{.*\})\s*$")
"""The judge's verdict line — column one, nothing after the object.

Injection-resistant by construction (SP-REFUTER-INJECTION-HARDEN):
every evidence line is prefixed with its line number by
:func:`_evidence_excerpt`, so PR-head content can never produce a line
that starts with ``VERDICT:`` at column one inside the prompt or an
echoed reply."""

_EVIDENCE_CONTEXT = 25
"""Lines of context fetched on each side of a finding's cited range."""

EVIDENCE_OUTSIDE_DIFF = "evidence outside the diff"
"""The cited file does not resolve to a file inside ``repo_root``."""

CHECKOUT_NOT_HEAD = "checkout is not the MR head ({sha7})"
"""Template of the reason when the cited file is in the diff but absent from
a tree that is not at the reviewed head (the evidence exists, elsewhere)."""

EVIDENCE_UNREADABLE = "evidence unreadable"
"""The cited file exists but could not be read, or the cited lines are not in it."""

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
    VERDICT_UNPARSEABLE,
    JUDGE_CALL_FAILED,
)
"""The closed set of reasons a ``deferred`` finding carries (templates unexpanded)."""

UNVERIFIED_REASONS = (NOT_YET_MECHANICALLY_VERIFIED,)
"""The closed set of reasons an ``unverified`` finding carries."""

RefutationState = Literal["verified", "refuted", "deferred", "unverified"]
"""The one state every finding entering :func:`judge_findings_for_pr` ends in."""

_STATES: tuple[RefutationState, ...] = ("verified", "refuted", "deferred", "unverified")

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
    """

    finding_id: int | None
    file: str
    line_start: int
    claim_type: ClaimType
    state: RefutationState
    reason: str

    def as_json(self) -> dict[str, object]:
        """Return the finding entry of the ``review pr`` JSON payload."""
        return {
            "id": self.finding_id,
            "file": self.file,
            "line": self.line_start,
            "claim_type": self.claim_type.value,
            "refutation": {"state": self.state, "reason": self.reason},
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
    def deferrals(self) -> tuple[Refutation, ...]:
        """Return the outcomes with no verdict (``deferred`` and ``unverified``)."""
        return tuple(o for o in self.outcomes if o.state in ("deferred", "unverified"))

    def summary_line(self) -> str:
        """Return the one stderr line naming every state and reason.

        Example: ``refuter: judged 3, refuted 1, deferred 2 (evidence
        unreadable ×2), unverified 1 (not yet mechanically verified)``.
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
    loop = AgentLoop(capability=CapabilityTier.OPUS, max_tokens=800, temperature=0.0)
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


def _evidence_excerpt(
    finding: Finding, repo_root: Path, absent_reason: str = EVIDENCE_OUTSIDE_DIFF
) -> tuple[str | None, str]:
    """Return ``(cited code window, "")``, or ``(None, deferral reason)``.

    The reason is ``absent_reason`` (by default
    :data:`EVIDENCE_OUTSIDE_DIFF`) when the cited file is not a file
    inside ``repo_root``, and :data:`EVIDENCE_UNREADABLE` when it is but
    cannot be read or holds none of the cited lines.
    """
    if not _cited_file_inside(finding, repo_root):
        return None, absent_reason
    reader = make_repo_file_reader(repo_root)
    source = reader(finding.file)
    if source is None:
        return None, EVIDENCE_UNREADABLE
    lines = source.splitlines()
    start = max(0, finding.line_start - 1 - _EVIDENCE_CONTEXT)
    end = min(len(lines), finding.line_end + _EVIDENCE_CONTEXT)
    window = lines[start:end]
    if not window:
        return None, EVIDENCE_UNREADABLE
    numbered = [f"{start + i + 1}: {line}" for i, line in enumerate(window)]
    return "\n".join(numbered), ""


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


def refute_finding(
    finding: Finding,
    *,
    repo_root: Path,
    judge: Judge,
    absent_reason: str = EVIDENCE_OUTSIDE_DIFF,
) -> tuple[FindingStatus, str]:
    """Judge one finding, returning ``(new_status, reason)``.

    ``REFUTED`` when the judge refutes the claim, ``VERIFIED`` when it
    cannot, both with the judge's reasoning. ``PROPOSED`` (leave it for
    a later round) with a reason from :data:`DEFERRAL_REASONS` when the
    evidence is outside the repository or unreadable (the judge is then
    never called), when the judge raised (``judge call failed:
    <exception type>``) or when its verdict is unparseable. A cited file
    absent from ``repo_root`` is deferred with ``absent_reason``.
    """
    evidence, reason = _evidence_excerpt(finding, repo_root, absent_reason)
    if evidence is None:
        return FindingStatus.PROPOSED, reason
    try:
        raw = judge(_render_prompt(finding, evidence))
    except Exception as exc:
        _log.warning("refuter.judge_call_failed", exc=type(exc).__name__, error=str(exc)[:160])
        return FindingStatus.PROPOSED, f"{JUDGE_CALL_FAILED}: {type(exc).__name__}"
    verdict = _parse_verdict(raw)
    if verdict is None:
        return FindingStatus.PROPOSED, VERDICT_UNPARSEABLE
    refuted, reasoning = verdict
    return (FindingStatus.REFUTED if refuted else FindingStatus.VERIFIED), reasoning


def configured_max_judged(repo_root: Path) -> int:
    """Return the refuter cap for ``repo_root``: env > ``repoach.toml`` > 10.

    See :func:`~repoach.gates.config.resolve_refuter_max_judged`.
    """
    return resolve_refuter_max_judged(repo_root)[0]


class _LazyJudge:
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


def _refutation(finding: Finding, state: RefutationState, reason: str) -> Refutation:
    """Build the :class:`Refutation` of ``finding``."""
    return Refutation(
        finding_id=finding.id,
        file=finding.file,
        line_start=finding.line_start,
        claim_type=finding.claim_type,
        state=state,
        reason=reason,
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

    Returns:
        One :class:`Refutation` per proposed finding, plus the number of
        judge calls made.
    """
    cap = configured_max_judged(repo_root) if max_judged is None else max_judged
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
    judge = _LazyJudge(judge_factory)
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
        new_status, reason = refute_finding(
            finding, repo_root=repo_root, judge=judge, absent_reason=absent
        )
        if new_status is FindingStatus.PROPOSED:
            outcomes.append(_refutation(finding, "deferred", reason))
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
        outcomes.append(_refutation(finding, state, reason))
    outcomes.extend(
        _refutation(finding, "unverified", NOT_YET_MECHANICALLY_VERIFIED) for finding in unverified
    )
    return JudgingReport(outcomes=tuple(outcomes), model_calls=judge.calls, stale=stale)
