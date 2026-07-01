"""Action-plan form — the lingua franca between Planner and Developer.

SP-PLAN-FORM (architecture session 2026-06-06, decisions #2 and #3):
every build starts with an ACTION PLAN — JSON validated by the models
below, rendered to Markdown and committed as the first commit of the
implementation branch (``docs/plans/<SP-ID>.md``). The Planner agent
writes it; the Developer agent executes it step by step, one commit
per step. The test contract lives inside the plan: unit tests are
promised per step (a step is not done until they are green), and
integration tests are promised per plan, so the Tester reviewer can
judge tests-promised-vs-delivered.

The rendered document carries the canonical machine-readable payload
at its very end — the :data:`PLAN_MARKER` line followed by a ``json``
fence — so downstream consumers re-read the committed plan without
parsing prose. :func:`parse_plan_markdown` closes the round-trip:
``parse_plan_markdown(render_plan_markdown(p)) == p``.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, field_validator, model_validator

PLAN_MARKER: str = "<!-- ferova-action-plan -->"

_SPEC_ID_RE = re.compile(r"^SP-[A-Z0-9-]+$")
_PLAN_FENCE_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def require_repo_relative(path: str) -> None:
    """Reject absolute paths and ``..`` traversal segments.

    The check is purely lexical — existence on disk is deliberately
    NOT verified so plans can name files they are about to create.

    Args:
        path: Candidate repo-relative path.

    Raises:
        ValueError: When the path is empty, absolute, or contains a
            ``..`` traversal segment.
    """
    if not path.strip():
        raise ValueError("plan file path must be a non-empty string")
    pure = PurePosixPath(path)
    if pure.is_absolute():
        raise ValueError(f"plan file path must be repo-relative, got absolute: {path!r}")
    if ".." in pure.parts:
        raise ValueError(f"plan file path must not traverse with '..': {path!r}")


class PlanStep(BaseModel):
    """One executable step of an action plan.

    Attributes:
        index: 1-based position in the plan.
        title: Short imperative label.
        files: Repo-relative paths this step touches.
        action: What to do.
        commit_message: Conventional-commit subject for the step's
            commit.
        done_when: Verifiable completion criterion.
        unit_tests: Pytest paths or node-ids promised for this step.
            May be empty only when every file in :attr:`files` lives
            under ``docs/``.
    """

    index: int
    title: str
    files: list[str]
    action: str
    commit_message: str
    done_when: str
    unit_tests: list[str] = []

    @field_validator("title", "action", "commit_message", "done_when")
    @classmethod
    def _non_empty_text(cls, value: str) -> str:
        """Reject empty or whitespace-only required text fields."""
        if not value.strip():
            raise ValueError("field must be a non-empty string")
        return value

    @field_validator("files")
    @classmethod
    def _files_repo_relative(cls, files: list[str]) -> list[str]:
        """Require at least one file, each lexically repo-relative."""
        if not files:
            raise ValueError("a step must touch at least one file")
        for path in files:
            require_repo_relative(path)
        return files

    @field_validator("unit_tests")
    @classmethod
    def _selectors_safe(cls, selectors: list[str]) -> list[str]:
        """Reject pytest selectors that would parse as command flags.

        Plans are LLM-generated: a selector starting with ``-`` would
        be spliced into pytest argv as an option (``--pdb``, ``-p``…)
        by the step executor. Refuse at the form, not at the runner.
        """
        for selector in selectors:
            if not selector.strip():
                raise ValueError("test selector must be a non-empty string")
            if selector.lstrip().startswith("-"):
                raise ValueError(f"test selector must not start with '-': {selector!r}")
        return selectors

    @model_validator(mode="after")
    def _code_steps_promise_unit_tests(self) -> PlanStep:
        """Enforce the per-step test contract.

        A step is not done until its promised unit tests are green —
        only steps whose every file lives under ``docs/`` are exempt.
        """
        touches_code = any(not f.startswith("docs/") for f in self.files)
        if touches_code and not self.unit_tests:
            raise ValueError(
                f"step {self.index} ({self.title!r}) touches non-docs files "
                "and must promise at least one unit test"
            )
        return self


class ActionPlan(BaseModel):
    """A validated action plan for one spec.

    Attributes:
        spec_id: Spec identifier, ``SP-...`` uppercase form.
        title: Plan title.
        summary: One-paragraph intent of the plan.
        steps: Ordered executable steps, indexes ``1..len(steps)``.
        integration_tests: Pytest paths promised at plan level. May
            be empty only when no step touches a file under ``src/``.
    """

    spec_id: str
    title: str
    summary: str
    steps: list[PlanStep]
    integration_tests: list[str] = []

    @field_validator("integration_tests")
    @classmethod
    def _selectors_safe(cls, selectors: list[str]) -> list[str]:
        """Reject pytest selectors that would parse as command flags."""
        for selector in selectors:
            if not selector.strip():
                raise ValueError("test selector must be a non-empty string")
            if selector.lstrip().startswith("-"):
                raise ValueError(f"test selector must not start with '-': {selector!r}")
        return selectors

    @field_validator("spec_id")
    @classmethod
    def _spec_id_shape(cls, value: str) -> str:
        """Require the canonical ``SP-[A-Z0-9-]+`` identifier shape."""
        if not _SPEC_ID_RE.match(value):
            raise ValueError(f"spec_id must match SP-[A-Z0-9-]+, got {value!r}")
        return value

    @field_validator("title", "summary")
    @classmethod
    def _non_empty_text(cls, value: str) -> str:
        """Reject empty or whitespace-only required text fields."""
        if not value.strip():
            raise ValueError("field must be a non-empty string")
        return value

    @field_validator("steps")
    @classmethod
    def _contiguous_indexes(cls, steps: list[PlanStep]) -> list[PlanStep]:
        """Require at least one step, indexed exactly ``1..n`` in order."""
        if not steps:
            raise ValueError("a plan must contain at least one step")
        indexes = [s.index for s in steps]
        expected = list(range(1, len(steps) + 1))
        if indexes != expected:
            raise ValueError(f"step indexes must be exactly {expected}, got {indexes}")
        return steps

    @model_validator(mode="after")
    def _src_plans_promise_integration_tests(self) -> ActionPlan:
        """Enforce the per-plan test contract for ``src/`` changes."""
        touches_src = any(f.startswith("src/") for s in self.steps for f in s.files)
        if touches_src and not self.integration_tests:
            raise ValueError("plan touches src/ and must promise at least one integration test")
        return self

    @model_validator(mode="after")
    def _promised_tests_are_created_by_the_plan(self) -> ActionPlan:
        """Every promised test must be created by its step or an earlier one.

        The per-step contract is "a step is not done until its promised
        unit tests are green", so a promised test must EXIST when the
        step's gate runs. This validator enforces both failure modes
        the SP-INTEGRATION-STAGE acceptance flight surfaced
        (2026-06-07):

        * **forward reference** (v1) — the test is created by a
          higher-indexed step, so it does not exist yet;
        * **never created** (v3) — the test is promised but listed in
          no step's ``files`` at all.

        A promised unit-test file must therefore appear in the
        ``files`` of the promising step or an earlier one. Together
        with the Planner's parse/validation retry
        (``SP-PLANNER-PLAN-RETRY``) this is self-healing: a mis-shaped
        plan is rejected at generation and the rejection text guides
        the Planner to add the test file to the step.

        The rare legitimate case — promising a pre-existing repo test a
        step only reads — is served by listing that test in the step's
        ``files`` (harmless: the executor may rewrite it, which an
        extended test should anyway).
        """
        for step in self.steps:
            created_so_far = {
                path
                for earlier in self.steps
                if earlier.index <= step.index
                for path in earlier.files
            }
            for selector in step.unit_tests:
                test_file = selector.split("::", 1)[0]
                if test_file not in created_so_far:
                    raise ValueError(
                        f"step {step.index} promises test {selector!r} but no step "
                        f"up to {step.index} creates {test_file!r} — add that test "
                        "file to this step's files (code and its tests ship together)"
                    )
        return self


def plan_relpath(spec_id: str) -> str:
    """Return the repo-relative path of a spec's committed plan.

    Args:
        spec_id: Spec identifier (e.g. ``"SP-PLAN-FORM"``).

    Returns:
        ``docs/plans/<SPEC-ID>.md``.
    """
    return f"docs/plans/{spec_id}.md"


def render_plan_markdown(plan: ActionPlan) -> str:
    """Render a plan as the Markdown document committed on the branch.

    The human-readable sections come first; the document ends with the
    :data:`PLAN_MARKER` line followed by a ``json`` fence carrying the
    canonical payload. That fence is the LAST fence in the document —
    :func:`parse_plan_markdown` and any naive extractor rely on the
    layout staying that way.

    Args:
        plan: Validated plan to render.

    Returns:
        The full Markdown document.
    """
    lines: list[str] = [f"# {plan.spec_id} — {plan.title}", "", plan.summary, ""]
    for step in plan.steps:
        lines += [
            f"## Step {step.index} — {step.title}",
            "",
            f"- **Files**: {', '.join(f'`{f}`' for f in step.files)}",
            f"- **Action**: {step.action}",
            f"- **Commit**: `{step.commit_message}`",
            f"- **Done when**: {step.done_when}",
        ]
        if step.unit_tests:
            lines.append(f"- **Unit tests**: {', '.join(f'`{t}`' for t in step.unit_tests)}")
        else:
            lines.append("- **Unit tests**: _(docs-only step — none promised)_")
        lines.append("")
    lines += ["## Integration tests", ""]
    if plan.integration_tests:
        lines += [f"- `{t}`" for t in plan.integration_tests]
    else:
        lines.append("_(none promised)_")
    lines += ["", PLAN_MARKER, "```json", plan.model_dump_json(indent=2), "```", ""]
    return "\n".join(lines)


def parse_plan_markdown(text: str) -> ActionPlan:
    """Parse a rendered plan document back into an :class:`ActionPlan`.

    Args:
        text: Full Markdown content of a ``docs/plans/<SP-ID>.md`` file.

    Returns:
        The validated plan.

    Raises:
        ValueError: When the :data:`PLAN_MARKER` is missing, when no
            ``json`` fence follows it, or when the payload fails
            validation (pydantic's ``ValidationError`` is a
            ``ValueError``). Never returns a partial object.
    """
    marker_idx = text.find(PLAN_MARKER)
    if marker_idx == -1:
        raise ValueError(f"plan document carries no {PLAN_MARKER!r} marker")
    match = _PLAN_FENCE_RE.search(text, marker_idx)
    if match is None:
        raise ValueError("plan document has the marker but no json fence after it")
    return ActionPlan.model_validate_json(match.group(1))


def load_plan(spec_id: str, *, root: Path | None = None) -> ActionPlan:
    """Load and parse the committed plan of a spec.

    Args:
        spec_id: Spec identifier whose plan to read.
        root: Repository root (defaults to ``Path.cwd()``).

    Returns:
        The validated plan.

    Raises:
        FileNotFoundError: When ``docs/plans/<SPEC-ID>.md`` does not
            exist under *root*.
        ValueError: When the file exists but does not parse (see
            :func:`parse_plan_markdown`).
    """
    base = (root or Path.cwd()).resolve()
    target = base / plan_relpath(spec_id)
    if not target.is_file():
        raise FileNotFoundError(f"no committed plan at {target}")
    return parse_plan_markdown(target.read_text(encoding="utf-8"))
