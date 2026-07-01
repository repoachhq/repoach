# SP-BUILDER-MEMORY — give the BUILD agents persistent memory via agentmemory (project=builder only)

## Metadata

- **Status**: OPEN
- **Priority**: P2
- **Owner**: operator
- **Executor**: hand-implemented
- **Opened**: 2026-06-10

## Why

The operator's system is meant to be self-evolving — it adjusts and
learns from its mistakes. The builder (Planner + Developer) currently
starts every spec from a blank slate and re-discovers the same traps
(integration DB must live outside the repo tree, promised tests must be
created by the plan, launch `develop` from the spec-carrying checkout,
…). The `agentmemory` service is deployed and its project-scoped
`search` is verified to isolate correctly, but **nothing in
`ferova` reads or writes it** (grep: zero references).

This slice wires the BUILD phase — and ONLY the BUILD phase
(`project=builder`; review/code/claude-sessions scopes are out of scope)
— to agentmemory so the builder recalls relevant past lessons before
planning and records what it learned after building: a self-improving
loop, on the builder alone.

## What

A recall-before / remember-after loop scoped to `project=builder`,
degrading gracefully (the service being down NEVER breaks a build).

1. **`src/ferova/memory/agentmemory_client.py`** (new) — thin REST
   client for the running service (verified API):
   - `recall(query, *, project, limit=5, base_url, timeout_s=4.0) -> list[str]`
     — `POST {base_url}/agentmemory/search {query, project, limit}`,
     return each hit's `observation.narrative` (fallback `title`).
     On any `httpx.HTTPError` / non-2xx / parse error → `[]` + a
     `agentmemory_recall_failed` warning (never raises).
   - `remember(content, *, project, mem_type="lesson", base_url, timeout_s=4.0) -> bool`
     — `POST {base_url}/agentmemory/remember {content, project, type}`,
     return `success`. On failure → `False` + warning (never raises).
2. **`src/ferova/review/builder_memory.py`** (new) — builder-scoped
   orchestration on top of the client (keeps the wiring in planner /
   dev_runner one-line and the logic testable):
   - `BUILDER_PROJECT = "builder"`.
   - `recall_builder_lessons(query) -> list[str]` — gated on
     `settings.builder_memory_enabled`; calls `recall(project=builder)`.
   - `lessons_section(lessons) -> str` — render a
     `## Lessons from past builds (agentmemory)` markdown block, or `""`
     when empty (pure, testable).
   - `remember_build_outcome(spec_id, *, pushed, no_op_reason, n_steps) -> bool`
     — compose a one-line memory ("Built SP-X: pushed, 3 steps" or
     "SP-X stalled: <no_op_reason>") and `remember(project=builder)`.
   - `SEED_LESSONS: tuple[str, ...]` (curated builder traps from
     [[build-phase-archive]]) + `seed_builder_memory() -> int`.
3. **Wiring (code only, no `prompts/review/` edit):**
   - `review/planner.py` `run_planner_session`: before building the
     Planner, `lessons = recall_builder_lessons(spec.id + " " + title)`
     and pass `spec_markdown = spec.raw_markdown + lessons_section(lessons)`
     so the recalled lessons ride inside the planner's `{SPEC_PLAN}`
     block.
   - `cli/review_cmds.py` `review_develop`: after `run_developer_session`
     returns, call `remember_build_outcome(...)` from the result — the
     single top-level build consumer, which avoids threading the call
     through the multi-return session function.
4. **`config/settings.py`** (or `core.config`): `agentmemory_url`
   (default `http://localhost:3111`), `builder_memory_enabled`
   (default `true`).
5. **CLI** `ferova memory seed-builder` (+ `ferova memory
   recall-builder <query>` for inspection).

## Files in scope

- `src/ferova/memory/__init__.py` + `agentmemory_client.py` (new)
- `src/ferova/review/builder_memory.py` (new)
- `src/ferova/review/planner.py` (recall wiring)
- `src/ferova/cli/review_cmds.py` (remember wiring at the develop entry)
- `src/ferova/core/config.py` (two settings)
- `src/ferova/cli/main.py` (`memory seed-builder` / `recall-builder`)
- `tests/unit/test_agentmemory_client.py`, `tests/unit/test_builder_memory.py` (new)

## Out of scope

- review / code / claude-sessions scopes (builder ONLY).
- Editing any `prompts/review/*` persona (force-majeure path).
- Automatic LLM consolidation in agentmemory (BM25-only stays).
- Recall/remember for the reviewers or the Coder.

## Smoke scenario

With an injected fake HTTP client: `recall` returns the narratives of
scripted hits and `[]` on a raised timeout; `remember` returns the
service's `success` and `False` on failure. `lessons_section([])` is
`""`; non-empty renders the block. `run_planner_session` (planner
monkeypatched) augments `spec_markdown` with the recalled block;
`run_developer_session` calls `remember_build_outcome` at wrap-up.
`seed_builder_memory()` posts each curated lesson.

## Definition of Done

- `recall` / `remember` never raise on transport failure, return
  `[]` / `False` + log — `test_agentmemory_client.py`.
- `recall` extracts `observation.narrative` from the search payload —
  same file.
- `lessons_section` is `""` for empty, renders the header + bullets
  otherwise — `test_builder_memory.py`.
- `remember_build_outcome` composes the pushed vs stalled line and calls
  the client with `project="builder"` — same file.
- `builder_memory_enabled=false` makes `recall_builder_lessons` a no-op
  returning `[]` without any HTTP — same file.
- `ruff` + `ruff format --check` + full `pytest tests/unit` green; zero
  inline comments; no `# noqa`.

## Commit plan

1. `feat(memory): agentmemory REST client (recall/remember, graceful)`
2. `feat(builder): recall-before planning + remember-after build (project=builder)`
3. `feat(cli): ferova memory seed-builder / recall-builder`
4. `test(memory): client graceful-degradation + builder-memory orchestration`

## Risks

- **Service down / cold start**: recall returns `[]`, build proceeds
  unchanged; `builder_memory_enabled=false` is the hard kill-switch.
- **Cold store**: `seed-builder` loads curated traps so recall is useful
  before remember-after has populated anything.
- **Prompt bloat**: `limit=5` recalled one-liners keep the injected
  block small relative to the spec.
