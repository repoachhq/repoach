# SP-PLANNER-AGENT — the Planner agent (builder slice)

## Metadata

- **Status**: OPEN
- **Priority**: P1
- **Owner**: operator
- **Executor**: hand-implemented (touches `prompts/review/` — bot-forbidden path)
- **Opened**: 2026-06-07

## Why

Architecture decision #1 (design session 2026-06-06): the Planner is
AI #1 of the BUILD phase — it understands the spec, EXPLORES the
repository with read-only tools, and writes the ACTION PLAN the
Developer will execute. Today context selection is a regex over the
spec (`_scan_referenced_paths`): if the spec does not name a file the
Developer never sees it. The Planner replaces that with genuine
exploration. SP-PLAN-FORM (merged, PR #322) provides the output
contract: a validated `ActionPlan` rendered to
`docs/plans/<SP-ID>.md`.

## What

### Exploration tools — `src/ferova/review/planner_tools.py`

`make_planner_tools(repo_root: Path) -> list[ToolDef]` returning three
read-only, repo-jailed tools (lexical validation reusing
`plan._require_repo_relative`, then a resolved `relative_to` check):

- `list_dir(path)` — sorted entries, directories suffixed `/`,
  noise pruned (`.git`, `__pycache__`, caches), capped at 200
  entries.
- `read_file(path)` — contents capped at 24 000 chars with an
  explicit truncation note.
- `grep_repo(pattern, glob)` — regex search across the working tree
  (same noise pruning), `path:line: text` matches, capped at 80.

Tools NEVER raise on bad input — they return an error string the
model can read and correct (a crashed loop teaches the model
nothing).

### The agent — `src/ferova/review/planner.py`

- `BotRole.PLANNER` added to the enum; `mcp_whitelist` row PLANNER →
  `()` (its exploration tools are local ToolDefs, not MCP — fail
  closed).
- `Planner` class: persona `prompts/review/planner_0.1.0.md`, chain
  `PROXY_OPUS_CHAIN` (planning is reasoning-tier), `max_tokens=8000`,
  `AgentLoop.run(prompt, system=persona, tools=make_planner_tools(...))`.
- Final-answer parsing: the LAST ```json fence in the final text
  (fallback: the text as bare JSON), validated through
  `ActionPlan.model_validate_json`. A plan whose `spec_id` does not
  match the requested spec is an ERROR, not a warning.
- `run_planner_session(spec_id, *, root=None, planner=None) ->
  PlannerOutcome`: `load_spec` → `render_repo_tree` → `Planner.plan`
  → write `docs/plans/<SP-ID>.md` via `render_plan_markdown`.
  `PlannerOutcome` carries spec_id / plan_path / written / error /
  n_steps / tool_calls / turns / tokens_used / model_used /
  elapsed_s. Committing the plan as first commit is OUT of scope
  (SP-DEV-PLAN-EXEC wires that).

### Persona — `prompts/review/planner_0.1.0.md`

Placeholders `{SPEC_PLAN}` + `{REPO_TREE}`. Teaches: explore before
planning (read the real files, verify paths exist, match existing
conventions), plan quality (3-7 small committable steps, one concern
each, VERIFIABLE `done_when` per step), the test contract (unit
tests promised per step, integration tests per plan), and the strict
output contract (final message = exactly one ```json fence matching
the ActionPlan schema, nothing else).

### CLI

`ferova plan <spec-id>` (top-level alias, like `develop`) and
`ferova review plan <spec-id>` — run the session, print the
outcome as JSON, exit 0 on success / 1 on failure.

## Files in scope

- `src/ferova/review/planner_tools.py` (new)
- `src/ferova/review/planner.py` (new)
- `prompts/review/planner_0.1.0.md` (new)
- `src/ferova/review/reviewer.py` (BotRole.PLANNER)
- `src/ferova/review/mcp_whitelist.py` (PLANNER row)
- `src/ferova/cli/review_cmds.py` + `src/ferova/cli/main.py`
- `tests/unit/test_review_planner_tools.py` (new)
- `tests/unit/test_review_planner.py` (new)
- `tests/unit/test_review_mcp_whitelist.py` (PLANNER coverage)

## Out of scope

- dev_runner integration / plan-as-first-commit (SP-DEV-PLAN-EXEC).
- The delegated-exploration mode via claude CLI native tools
  (SP-PLANNER-CC-EXPLORE).
- Change-Request Analyst input path (REVIEW loop, separate slice).

## Smoke scenario

### Setup

Proxy running on :8082 with the OPUS chain reachable.

### Execute

```bash
ferova plan SP-PLAN-FORM
```

### Expected

Exit 0; `docs/plans/SP-PLAN-FORM.md` exists, contains
`<!-- ferova-action-plan -->` and parses via
`load_plan("SP-PLAN-FORM")`; the printed JSON reports `written: true`
and at least one `tool_calls` entry (the Planner actually explored).

## Definition of Done

- The three tools behave as specified (jail, caps, error-strings) —
  unit-tested against a tmp repo.
- `Planner` + `run_planner_session` behave as specified with an
  injected fake loop: happy path writes a parseable plan file;
  spec-id mismatch, unparseable output and empty text are loud
  errors with nothing written.
- Persona file exists, carries both placeholders and the exact
  ActionPlan field names.
- Whitelist covers PLANNER explicitly; the whitelist suite extends
  its role list.
- `ruff` + full `pytest tests/unit` green; zero inline comments.

## Commit plan

1. `feat(review): planner exploration tools — repo-jailed read-only ToolDefs`
2. `feat(review): Planner agent + run_planner_session + CLI wiring`
3. `feat(prompts): planner persona 0.1.0`
4. `test(review): planner tools + session suites, whitelist coverage`

## Risks

- Model ignores the output contract on long explorations — mitigated
  by the strict final-fence parsing and loud errors; quality
  iteration happens on the persona file (semver bump).
- Tool result floods: caps keep any single result under ~25 KB so a
  15-turn exploration stays inside the proxy's context budget.
