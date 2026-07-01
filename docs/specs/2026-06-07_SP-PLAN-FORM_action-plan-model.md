# SP-PLAN-FORM — ActionPlan model + Markdown render (builder slice)

## Metadata

- **Status**: OPEN
- **Priority**: P1
- **Owner**: operator
- **Executor**: `ferova develop` (autonomous Developer)
- **Opened**: 2026-06-07

## Why

Architecture decision (design session 2026-06-06, decision #2): every
build starts with an ACTION PLAN — JSON validated by a Pydantic model,
rendered to Markdown and committed as the FIRST commit of the
implementation branch (`docs/plans/<SP-ID>.md`). The plan is the
lingua franca between the Planner agent (which will write it) and the
Developer agent (which will execute it step by step, one commit per
step). The test contract (decision #3) lives inside the plan: unit
tests are promised per step, integration tests per plan, and the
Tester reviewer will judge tests-promised-vs-delivered.

This slice ships the FORM only — the data model, its validation
rules, the Markdown renderer and the parser that closes the
round-trip. No agent behaviour changes.

## What

New module `src/ferova/review/plan.py` (Pydantic v2 only —
`field_validator` / `model_validator`, never v1 syntax):

### `PlanStep` (BaseModel)

- `index: int` — 1-based position in the plan.
- `title: str` — short imperative label, non-empty.
- `files: list[str]` — repo-relative paths this step touches,
  at least one entry.
- `action: str` — what to do, non-empty.
- `commit_message: str` — conventional-commit subject for the step's
  commit, non-empty.
- `done_when: str` — VERIFIABLE completion criterion, non-empty.
- `unit_tests: list[str]` — pytest paths or node-ids promised for
  this step (default empty list).

Validators:

- every entry of `files` must be repo-relative: reject absolute
  paths and any path containing a `..` traversal segment
  (raise `ValueError` naming the offending path);
- when any entry of `files` is NOT under `docs/`, `unit_tests` must
  be non-empty ("step not done until green" — only docs-only steps
  are exempt).

### `ActionPlan` (BaseModel)

- `spec_id: str` — must match the regex `^SP-[A-Z0-9-]+$`.
- `title: str` — non-empty.
- `summary: str` — non-empty.
- `steps: list[PlanStep]` — at least one step.
- `integration_tests: list[str]` — pytest paths promised at plan
  level (default empty list).

Validators:

- step indexes must be exactly `1..len(steps)` in order (contiguous,
  no duplicates — raise `ValueError` otherwise);
- when any step touches a file under `src/`, `integration_tests`
  must be non-empty (the per-plan test contract).

### Module functions

- `PLAN_MARKER: str = "<!-- ferova-action-plan -->"`
- `plan_relpath(spec_id: str) -> str` — returns
  `docs/plans/<SPEC-ID>.md`.
- `render_plan_markdown(plan: ActionPlan) -> str` — human-readable
  Markdown: H1 with spec_id + title, the summary, one `## Step <n> —
  <title>` section per step (listing files, action, commit message,
  done_when, promised unit tests), an `## Integration tests` section,
  and at the very end the canonical machine-readable payload: the
  `PLAN_MARKER` line followed by a ```json fence containing
  `plan.model_dump_json(indent=2)`.
- `parse_plan_markdown(text: str) -> ActionPlan` — locate the
  `PLAN_MARKER`, extract the json fence that follows it, validate via
  `ActionPlan.model_validate_json`. Raise `ValueError` with a clear
  message when the marker or the fence is missing — never return a
  partial object.
- `load_plan(spec_id: str, *, root: Path | None = None) ->
  ActionPlan` — read `plan_relpath(spec_id)` under `root` (default
  `Path.cwd()`) and delegate to `parse_plan_markdown`. Raise
  `FileNotFoundError` when the file does not exist.

Round-trip law (must hold and be tested):
`parse_plan_markdown(render_plan_markdown(p)) == p` for any valid
plan `p`.

## Files in scope

- `src/ferova/review/plan.py` (new)
- `tests/unit/test_review_plan.py` (new)

## Out of scope

- The Planner agent, its prompt, its CLI wiring.
- Any change to `dev_runner.py`, `reviewer.py`, `spec.py` or the
  Developer prompt.
- Creating any `docs/plans/*.md` content (the form ships empty).
- The `tests/integration/` directory and its CI stage (separate
  slice).

## Smoke scenario

### Setup

Nothing — pure library code, no service, no network.

### Execute

```bash
python -c "from ferova.review.plan import ActionPlan, PlanStep, render_plan_markdown; print(render_plan_markdown(ActionPlan(spec_id='SP-DEMO', title='Demo', summary='Smoke.', steps=[PlanStep(index=1, title='Add module', files=['src/ferova/demo.py'], action='Create the module.', commit_message='feat(demo): add module', done_when='pytest tests/unit/test_demo.py is green', unit_tests=['tests/unit/test_demo.py'])], integration_tests=['tests/integration/test_demo_flow.py'])))"
```

### Expected

Stdout contains `# SP-DEMO`, `## Step 1`, and
`<!-- ferova-action-plan -->` followed by a ```json fence.

## Definition of Done

- `src/ferova/review/plan.py` exists with `PlanStep`,
  `ActionPlan`, `PLAN_MARKER`, `plan_relpath`,
  `render_plan_markdown`, `parse_plan_markdown`, `load_plan` exactly
  as specified, strict type hints, Google-style docstrings on every
  public symbol, zero inline comments.
- All validators behave as specified and raise `ValueError` with
  messages naming the offending value.
- `tests/unit/test_review_plan.py` covers at minimum: a fully valid
  plan; absolute-path and `..`-traversal rejection; missing
  `unit_tests` on a `src/`-touching step rejected; docs-only step
  without unit tests accepted; non-contiguous and duplicate step
  indexes rejected; `integration_tests` required when a step touches
  `src/`; bad `spec_id` rejected; `render_plan_markdown` output
  contains the marker, the json fence and every step title;
  `parse_plan_markdown` round-trip equality; missing-marker and
  missing-fence `ValueError`; `load_plan` happy path (tmp_path) and
  `FileNotFoundError`.
- `ruff check` + `ruff format --check` pass; full
  `pytest tests/unit` green.

## Commit plan

1. `feat(review): ActionPlan + PlanStep — the plan form (SP-PLAN-FORM)`
2. `test(review): plan model validation, render and parse round-trip`

## Risks

- Over-strict path validation could reject legitimate new top-level
  dirs — validation is purely lexical (absolute / `..`), it must NOT
  check existence on disk.
- The json fence must be the LAST fence in the rendered document so
  naive extractors stay correct; keep the renderer layout stable.
