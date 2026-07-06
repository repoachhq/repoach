"""Planner agent — understands the spec, explores the repo, writes the plan.

SP-PLANNER-AGENT (architecture decision #1): the Planner is AI #1 of
the BUILD phase. It receives the spec, explores the repository through
the read-only toolbox in :mod:`planner_tools`, and emits a validated
:class:`~ferova.review.plan.ActionPlan` that
:func:`run_planner_session` renders to ``docs/plans/<SP-ID>.md``.
The Developer then executes that plan — committing it as the first
commit of the branch is SP-DEV-PLAN-EXEC's wiring, not this module's.

Failure policy: a plan that does not parse, validates against the
wrong spec id, or comes back empty is a LOUD error on the outcome —
nothing is written, no partial object escapes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ..agent_engine.adapters import GatewayError
from ..agent_engine.agent_loop import PROXY_SONNET_CHAIN, AgentLoop
from ..core.logging import get_logger
from .plan import ActionPlan, plan_relpath, render_plan_markdown
from .planner_cc import run_cc_exploration
from .planner_tools import make_planner_tools
from .reviewer import BotRole
from .spec import load_spec
from .spec_gate import selector_present

_log = get_logger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts" / "review"

_SPEC_HARD_CAP_CHARS: int = 12_000
_JSON_FENCE_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
_PLAN_PARSE_ATTEMPTS: int = 3


def _parse_and_validate(text: str, spec_id: str) -> tuple[ActionPlan | None, str]:
    """Parse the final text into a validated plan, or describe the failure.

    Returns ``(plan, "")`` on success or ``(None, reason)`` — the
    reason is the message fed back to the model on the next refinement
    attempt, so it must name the concrete problem.
    """
    raw = _extract_plan_json(text)
    if raw is None:
        return None, "planner emitted no JSON plan payload"
    try:
        plan = ActionPlan.model_validate_json(raw)
    except ValueError as exc:
        return None, f"plan payload failed validation: {str(exc)[:300]}"
    if plan.spec_id != spec_id:
        return None, f"plan is for {plan.spec_id!r}, requested {spec_id!r}"
    return plan, ""


def _check_promised_selectors(plan: ActionPlan, repo_root: Path) -> str | None:
    """Return None when every promised selector is valid, else a directive.

    A selector is valid when:
    - its file does not exist at head (exempt, the file is the deliverable), or
    - it satisfies :func:`selector_present`, or
    - its node id appears verbatim in the promising step's action text.

    Args:
        plan: The validated plan whose selectors to check.
        repo_root: Repository root the selectors resolve against.

    Returns:
        ``None`` when all selectors are valid; otherwise a directive
        message listing each offending selector and the two remedies.
    """
    offenders: list[str] = []
    for step in plan.steps:
        for selector in step.unit_tests:
            file_part, _, node = selector.partition("::")
            target = repo_root / file_part
            if not target.is_file():
                continue
            if selector_present(repo_root, selector):
                continue
            if node and node in step.action:
                continue
            offenders.append(selector)
    for selector in plan.integration_tests:
        file_part, _, node = selector.partition("::")
        target = repo_root / file_part
        if not target.is_file():
            continue
        if selector_present(repo_root, selector):
            continue
        # Integration tests are not tied to a single step's action,
        # so the "declared creation" remedy does not apply here.
        offenders.append(selector)
    if not offenders:
        return None
    remedies = (
        "make selector_present resolve it (the test already exists at head), or "
        "declare creation by naming the node id verbatim in the step action text"
    )
    return (
        "promised selectors are not present at head and not declared for creation:\n"
        + "\n".join(f"  - {s}: {remedies}" for s in offenders)
    )


def _refine_prompt(previous_text: str, error: str) -> str:
    """Build the no-tools refinement prompt after a rejected plan.

    The Planner has already explored; re-running the full tool loop to
    fix a JSON-shape slip would re-pay the whole exploration cost, so
    the retry is a single tool-less turn that hands back the rejected
    candidate plus the exact validation error
    (SP-PLANNER-PLAN-RETRY).
    """
    return (
        "Your previous plan was REJECTED by the schema validator:\n\n"
        f"{error}\n\n"
        "Here is the plan you proposed:\n\n"
        f"{previous_text[:6000]}\n\n"
        "Reply with ONLY the corrected JSON plan in a single ```json fence — "
        "fix exactly that error, keep everything else identical, do not call any tool."
    )


@dataclass
class PlannerOutcome:
    """Result of one :func:`run_planner_session` invocation.

    Attributes:
        spec_id: Spec the session planned for.
        plan_path: Repo-relative path of the rendered plan document.
        written: Whether the plan file was written.
        error: Loud failure description, ``None`` on success.
        n_steps: Number of steps in the accepted plan.
        tool_calls: Names of every exploration tool call, in order.
        turns: Model round-trips consumed.
        tokens_used: Total tokens across the session.
        model_used: Upstream model the proxy chain landed on.
        elapsed_s: Wall-clock duration of the agent loop.
    """

    spec_id: str
    plan_path: str = ""
    written: bool = False
    error: str | None = None
    n_steps: int = 0
    tool_calls: list[str] = field(default_factory=list)
    turns: int = 0
    tokens_used: int = 0
    model_used: str = ""
    elapsed_s: float = 0.0


def _balanced_json_objects(text: str) -> list[str]:
    """Return every top-level brace-balanced ``{...}`` substring of *text*.

    String-aware: braces inside double-quoted JSON strings (and their
    escapes) are ignored, so a ``"done_when": "ruff {src}"`` value does
    not corrupt the balance. Used to salvage a JSON plan a verbose
    brain embedded in prose without the required fence.
    """
    objects: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start != -1:
                objects.append(text[start : i + 1])
                start = -1
    return objects


def _extract_plan_json(text: str) -> str | None:
    """Return the candidate ActionPlan JSON payload from the final text.

    Tolerant extraction, most-disciplined form first
    (SP-PLANNER-JSON-TOLERANT, after the brain-swap experiment showed
    opus emits the plan wrapped in prose without the required fence):

    1. the LAST ``json`` fence — the persona's output contract;
    2. the bare text when it is itself a JSON object;
    3. otherwise, the last brace-balanced ``{...}`` object that carries
       the plan signature (``"spec_id"`` and ``"steps"``), salvaging a
       plan a verbose brain buried in surrounding prose.

    The last candidate wins in every case because exploration
    transcripts quote earlier fragments while reasoning.
    """
    fences = _JSON_FENCE_RE.findall(text)
    if fences:
        return fences[-1]
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    plan_shaped = [
        obj for obj in _balanced_json_objects(text) if '"spec_id"' in obj and '"steps"' in obj
    ]
    if plan_shaped:
        return plan_shaped[-1]
    return None


class Planner:
    """Spec-to-plan agent over the read-only exploration toolbox.

    Attributes:
        role: :data:`BotRole.PLANNER`.
        persona_filename: Versioned persona under ``prompts/review/``.
        model_chain: SONNET proxy chain — the balanced tier that backs
            the coding agents (Planner / Coder / Developer) since the
            redundant CODER tier was retired (SP-CODER-TIER-RETIRE-AGENT).
            Reading code to plan code sits comfortably at sonnet quality.
    """

    role = BotRole.PLANNER
    persona_filename = "planner_0.2.0.md"
    cc_persona_filename = "planner_cc_0.1.0.md"
    model_chain = PROXY_SONNET_CHAIN
    max_tokens = 8000
    temperature = 0.1
    max_turns = 12

    def __init__(
        self,
        *,
        loop: AgentLoop | None = None,
        repo_root: Path | None = None,
        prompts_dir: Path | None = None,
        explore_via: Literal["proxy", "claude_cli"] = "proxy",
        cc_model: str = "sonnet",
    ) -> None:
        """Create the Planner.

        Args:
            loop: Optional pre-built :class:`AgentLoop` (tests inject
                a fake). Used only by the ``"proxy"`` mode.
            repo_root: Repository root the exploration is jailed to
                (defaults to ``Path.cwd()``).
            prompts_dir: Directory the persona is loaded from
                (defaults to the repo's ``prompts/review/``; tests
                inject a tmp dir to exercise persona failure modes).
            explore_via: ``"proxy"`` (default) drives the AgentLoop ↔
                local-tools loop over the proxy chain; ``"claude_cli"``
                delegates exploration to one ``claude -p`` session with
                the CLI's native read-only tools (SP-PLANNER-CC-EXPLORE).
            cc_model: CLI model alias for the ``"claude_cli"`` mode.
        """
        self._repo_root = (repo_root or Path.cwd()).resolve()
        self._prompts_dir = prompts_dir or _PROMPTS_DIR
        self._explore_via = explore_via
        self._cc_model = cc_model
        if loop is not None:
            self._loop = loop
        elif explore_via == "proxy":
            self._loop = AgentLoop(
                model_chain=self.model_chain,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                max_turns=self.max_turns,
            )
        else:
            self._loop = None

    def plan(
        self, *, spec_id: str, spec_markdown: str, repo_tree: str = ""
    ) -> tuple[
        ActionPlan | None,
        str | None,
        dict[str, object],
    ]:
        """Run one planning session and parse the resulting plan.

        Dispatches to the proxy loop or the delegated ``claude -p``
        session per :attr:`_explore_via`. Both paths feed the same
        strict ``_parse_and_validate`` / retry loop, so the plan-form
        contract is identical regardless of how the repo was explored.

        Args:
            spec_id: Identifier the plan must carry.
            spec_markdown: Full spec content (capped before prompting).
            repo_tree: Pre-rendered directory listing; used by the
                proxy path only (the CLI explores the tree itself in
                ``claude_cli`` mode, where this stays empty).

        Returns:
            ``(plan, error, audit)`` — exactly one of ``plan`` /
            ``error`` is set; ``audit`` carries the path's telemetry.
        """
        if self._explore_via == "claude_cli":
            return self._plan_via_cc(spec_id=spec_id, spec_markdown=spec_markdown)
        return self._plan_via_proxy(
            spec_id=spec_id, spec_markdown=spec_markdown, repo_tree=repo_tree
        )

    def _spec_block(self, spec_markdown: str) -> str:
        """Cap the spec for the prompt with an explicit truncation note."""
        block = spec_markdown[:_SPEC_HARD_CAP_CHARS]
        if len(spec_markdown) > _SPEC_HARD_CAP_CHARS:
            block += "\n\n[... spec truncated for the prompt; read the file for the rest ...]"
        return block

    def _plan_via_proxy(
        self, *, spec_id: str, spec_markdown: str, repo_tree: str
    ) -> tuple[ActionPlan | None, str | None, dict[str, object]]:
        """Plan by driving the AgentLoop ↔ local-tools loop over the proxy.

        A proxy/chain failure (every candidate empty-completing, a
        transport error, the chain exhausting) is caught and returned
        as a loud error outcome — never propagated as a crash. This
        matches the delegated path's contract (``run_cc_exploration``
        never raises) so a brain/infra outage fails the SAME safe way
        regardless of the exploration backend. Surfaced by the
        brain-swap experiment (2026-06-09), where a degraded NIM coder
        chain crashed the proxy Planner with an unhandled
        ``GatewayTransportError``.
        """
        persona = (self._prompts_dir / self.persona_filename).read_text(encoding="utf-8")
        system = persona.replace("{SPEC_PLAN}", self._spec_block(spec_markdown)).replace(
            "{REPO_TREE}", repo_tree
        )
        prompt = (
            f"Write the action plan for spec {spec_id}. Explore the repository "
            "first; when your exploration is complete, reply with the final "
            "JSON plan only."
        )
        audit: dict[str, object] = {
            "tool_calls": [],
            "turns": 0,
            "tokens_used": 0,
            "model_used": "",
            "elapsed_s": 0.0,
            "explore_via": "proxy",
        }
        try:
            output = self._loop.run(
                prompt,
                system=system,
                tools=make_planner_tools(self._repo_root),
            )
        except GatewayError as exc:
            _log.warning("planner.proxy_exploration_failed", spec_id=spec_id, error=str(exc)[:200])
            return None, f"proxy exploration failed: {str(exc)[:200]}", audit
        audit.update(
            {
                "tool_calls": list(output.tool_calls_made),
                "turns": output.turns,
                "tokens_used": output.tokens_used,
                "model_used": output.model_used,
                "elapsed_s": output.elapsed_s,
            }
        )
        candidate_text = output.text or ""
        last_error = ""
        for attempt in range(1, _PLAN_PARSE_ATTEMPTS + 1):
            plan, last_error = _parse_and_validate(candidate_text, spec_id)
            if plan is not None:
                selector_error = _check_promised_selectors(plan, self._repo_root)
                if selector_error:
                    last_error = selector_error
                    _log.warning(
                        "planner.plan_invalid",
                        spec_id=spec_id,
                        explore_via="proxy",
                        attempt=attempt,
                        error=last_error[:300],
                    )
                    if attempt == _PLAN_PARSE_ATTEMPTS:
                        break
                    try:
                        refine = self._loop.run(
                            _refine_prompt(candidate_text, last_error), system=system
                        )
                    except GatewayError as exc:
                        _log.warning(
                            "planner.proxy_refinement_failed",
                            spec_id=spec_id,
                            error=str(exc)[:200],
                        )
                        return None, f"proxy refinement failed: {str(exc)[:200]}", audit
                    audit["tokens_used"] = int(audit.get("tokens_used") or 0) + refine.tokens_used
                    candidate_text = refine.text or ""
                    continue
                _log.info(
                    "planner.plan_accepted",
                    spec_id=spec_id,
                    explore_via="proxy",
                    n_steps=len(plan.steps),
                    parse_attempt=attempt,
                )
                return plan, None, audit
            _log.warning(
                "planner.plan_invalid",
                spec_id=spec_id,
                explore_via="proxy",
                attempt=attempt,
                error=last_error[:300],
            )
            if attempt == _PLAN_PARSE_ATTEMPTS:
                break
            try:
                refine = self._loop.run(_refine_prompt(candidate_text, last_error), system=system)
            except GatewayError as exc:
                _log.warning(
                    "planner.proxy_refinement_failed", spec_id=spec_id, error=str(exc)[:200]
                )
                return None, f"proxy refinement failed: {str(exc)[:200]}", audit
            audit["tokens_used"] = int(audit.get("tokens_used") or 0) + refine.tokens_used
            candidate_text = refine.text or ""
        return None, last_error, audit

    def _plan_via_cc(
        self, *, spec_id: str, spec_markdown: str
    ) -> tuple[ActionPlan | None, str | None, dict[str, object]]:
        """Plan by delegating exploration to one ``claude -p`` session."""
        persona = (self._prompts_dir / self.cc_persona_filename).read_text(encoding="utf-8")
        prompt = persona.replace("{SPEC_PLAN}", self._spec_block(spec_markdown))
        audit: dict[str, object] = {
            "tool_calls": [],
            "turns": 0,
            "tokens_used": 0,
            "model_used": f"claude-cli/{self._cc_model}",
            "elapsed_s": 0.0,
            "explore_via": "claude_cli",
        }
        candidate_text = ""
        last_error = ""
        for attempt in range(1, _PLAN_PARSE_ATTEMPTS + 1):
            if attempt == 1:
                cc = run_cc_exploration(
                    prompt=prompt,
                    repo_root=self._repo_root,
                    model=self._cc_model,
                    allow_tools=True,
                )
            else:
                cc = run_cc_exploration(
                    prompt=_refine_prompt(candidate_text, last_error),
                    repo_root=self._repo_root,
                    model=self._cc_model,
                    allow_tools=False,
                )
            audit["turns"] = int(audit.get("turns") or 0) + cc.num_turns
            audit["elapsed_s"] = float(audit.get("elapsed_s") or 0.0) + cc.duration_ms / 1000
            if cc.is_error:
                last_error = f"claude exploration failed: {cc.error}"
                _log.warning(
                    "planner.cc_attempt_failed", spec_id=spec_id, attempt=attempt, error=cc.error
                )
                continue
            candidate_text = cc.text
            plan, last_error = _parse_and_validate(candidate_text, spec_id)
            if plan is not None:
                selector_error = _check_promised_selectors(plan, self._repo_root)
                if selector_error:
                    last_error = selector_error
                    _log.warning(
                        "planner.plan_invalid",
                        spec_id=spec_id,
                        explore_via="claude_cli",
                        attempt=attempt,
                        error=last_error[:300],
                    )
                    continue
                _log.info(
                    "planner.plan_accepted",
                    spec_id=spec_id,
                    explore_via="claude_cli",
                    n_steps=len(plan.steps),
                    parse_attempt=attempt,
                )
                return plan, None, audit
            _log.warning(
                "planner.plan_invalid",
                spec_id=spec_id,
                explore_via="claude_cli",
                attempt=attempt,
                error=last_error[:300],
            )
        return None, last_error, audit


def run_planner_session(
    spec_id: str,
    *,
    root: Path | None = None,
    planner: Planner | None = None,
    explore_via: Literal["proxy", "claude_cli"] = "proxy",
    cc_model: str = "sonnet",
) -> PlannerOutcome:
    """Plan one spec end to end and write ``docs/plans/<SP-ID>.md``.

    Args:
        spec_id: Spec identifier (``docs/specs/`` lookup via
            :func:`load_spec`).
        root: Repository root (defaults to ``Path.cwd()``).
        planner: Optional pre-built :class:`Planner` (tests inject one
            with a fake loop); when given, ``explore_via`` / ``cc_model``
            are ignored.
        explore_via: Exploration backend for the default Planner —
            ``"proxy"`` or ``"claude_cli"`` (SP-PLANNER-CC-EXPLORE).
        cc_model: CLI model alias for ``"claude_cli"`` mode.

    Returns:
        A :class:`PlannerOutcome`; ``written=True`` only when the plan
        validated and the document landed on disk.
    """
    repo = (root or Path.cwd()).resolve()
    try:
        spec = load_spec(spec_id, root=repo)
    except FileNotFoundError as exc:
        return PlannerOutcome(spec_id=spec_id, error=f"spec not found: {exc}")

    from .dev_runner import render_repo_tree

    tree = "" if explore_via == "claude_cli" else render_repo_tree(repo_root=repo)
    agent = planner or Planner(repo_root=repo, explore_via=explore_via, cc_model=cc_model)

    from .builder_memory import lessons_section, recall_builder_lessons

    lessons = recall_builder_lessons(f"{spec.id} {spec.raw_markdown[:300]}")
    spec_markdown = spec.raw_markdown + lessons_section(lessons)
    plan, error, audit = agent.plan(
        spec_id=spec.id,
        spec_markdown=spec_markdown,
        repo_tree=tree,
    )
    outcome = PlannerOutcome(
        spec_id=spec.id,
        tool_calls=list(audit.get("tool_calls") or []),
        turns=int(audit.get("turns") or 0),
        tokens_used=int(audit.get("tokens_used") or 0),
        model_used=str(audit.get("model_used") or ""),
        elapsed_s=float(audit.get("elapsed_s") or 0.0),
    )
    if plan is None:
        outcome.error = error
        return outcome

    target = repo / plan_relpath(spec.id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_plan_markdown(plan), encoding="utf-8")
    outcome.plan_path = plan_relpath(spec.id)
    outcome.written = True
    outcome.n_steps = len(plan.steps)
    _log.info(
        "planner.session_done",
        spec_id=spec.id,
        plan_path=outcome.plan_path,
        n_steps=outcome.n_steps,
    )
    return outcome
