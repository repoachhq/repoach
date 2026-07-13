---
id: SP-TOOL-DISPATCH-VALIDATE
title: Validate model tool-call args against the declared schema before dispatch
version: 0.1
status: draft
author: jfaye (Ferova audit 2026-07-13)
created: 2026-07-13
updated: 2026-07-13

owns:
  code: []
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Validate model tool-call args against the declared schema before dispatch

## Intent

The agent loop forwards model-controlled tool-call kwargs straight to
the Python callable without validating them against the schema it
declared to the model, and with no size cap on the arguments. The loop
runs untrusted chain models, so a malformed or oversized tool call
reaches the callable unchecked. Validate `tc.args` against the tool's
declared `parameters_schema` at the loop, and cap argument size, before
dispatch.

## Context

Audit 2026-07-13 finding M16.

- `src/ferova/agent_engine/agent_loop.py:723-726`:
  `raw = tools_by_name[tool_name].callable_fn(**tc.args)` — the
  model-supplied `tc.args` dict is splatted straight into the callable.
- `agent_loop.py:120-122` (`ToolDef.parameters_schema: dict[str,
  Any]`): the JSON schema is serialised to the model via
  `to_tool_spec()` (`agent_loop.py:125-131`) but never validated
  against the returned `tc.args`.
- `agent_loop.py:726`: only the RESULT is capped (`capped[:8000]`);
  there is no cap on the incoming argument payload and no loop-level
  guard between the model's response and the callable.
- `agent_loop.py:717-721` already logs `arg_keys`; the unknown-tool
  path (`agent_loop.py:701-716`) already returns a `ToolResultError`
  — the validated-rejection path should reuse that observation shape.

`agent_loop.py` is owned by an existing agent-engine spec; this is an
in-place modification of the dispatch path.

## Goals

- G1: before dispatch, `tc.args` is validated against the tool's
  declared `parameters_schema`; a call whose args violate the schema
  (unknown/extra property when `additionalProperties` is false, missing
  required key, wrong JSON type) is REJECTED at the loop and never
  reaches `callable_fn`.
- G2: a total-argument-size cap is enforced (serialised `tc.args`
  length); an oversized argument payload is rejected at the loop.
- G3: a rejection is observable — it produces a `ToolResultError`
  fed back to the model (so the model can correct) and a structured
  warning log, exactly like the existing unknown-tool path; it never
  raises out of the loop.

## Non-Goals

- NG1: no full JSON-Schema draft compliance — validate the subset the
  factory's tool schemas actually use (object with typed properties,
  `required`, `additionalProperties`). A pragmatic validator, not a
  spec-complete one.
- NG2: no change to `ToolDef` / `ToolSpec` shapes or the wire protocol.
- NG3: no per-tool custom validators — one loop-level gate for all
  tools.
- NG4: no change to the existing 8000-char RESULT cap.

## Assumptions

- A1: tool schemas are JSON-Schema-shaped objects with `type`,
  `properties`, and optionally `required`/`additionalProperties`
  (the shape `to_tool_spec` already emits).
- A2: `tc.args` is a dict when a tool call is well-formed; a non-dict
  `tc.args` is itself a rejection case (already partly handled by the
  `isinstance(tc.args, dict)` guard at `agent_loop.py:721`).

## Interface

`src/ferova/agent_engine/agent_loop.py`:
- Add a module-level helper
  `_validate_tool_args(schema: dict[str, Any], args: Any, *, max_bytes: int) -> str | None`
  returning `None` when the args are valid, or a human-readable
  rejection reason string otherwise (oversized payload, non-dict args,
  unknown property, missing required key, type mismatch).
- Add a module-level cap constant (e.g.
  `_MAX_TOOL_ARG_BYTES: int = 16000`) with a docstring stating the
  rationale (untrusted chain models; bound the splat payload).
- In the dispatch block (`agent_loop.py:717-726`), call the validator
  after the `tool_dispatched` log and, on a non-`None` reason, append a
  `ToolResultBlock(result=ToolResultError(ok=False, error={"message":
  reason}))` and `continue` — mirroring the unknown-tool branch —
  instead of calling `callable_fn`.

## Behavior

### Nominal

- A tool call whose `tc.args` matches the declared schema and is within
  the size cap → dispatched to `callable_fn` exactly as today.

### Edge cases

- Extra/unknown property with `additionalProperties: false` → rejected.
- Missing a `required` key → rejected.
- Wrong JSON type for a declared property (string where number
  declared) → rejected.
- `tc.args` not a dict → rejected.
- Args serialising above `_MAX_TOOL_ARG_BYTES` → rejected before the
  splat.

### Failure scenarios

- Any validation failure → fail CLOSED: no `callable_fn` invocation; a
  `ToolResultError` returned to the model and a structlog warning
  (`agent_loop.tool_args_rejected` with `turn`, `tool_name`, `reason`)
  emitted; the loop continues to the next turn (the model may retry
  with corrected args).

## Architecture Impact

- Adds/Removes dependency: none — in-place modification of
  `agent_loop.py` (owned by an existing agent-engine spec). The
  validator is a self-contained helper using only stdlib + the schema
  dict already present; no new third-party dependency and no new
  cross-owner import.
- New / changed coupling, cycles, or shared state: none.

## Diagram

N/A (in-place fix in the dispatch block).

## Acceptance Criteria

- [ ] AC1: unit — `_validate_tool_args` returns `None` for a matching
  args dict and a reason string for each rejection case (extra key,
  missing required, type mismatch, non-dict, oversized).
- [ ] AC2 (INTEGRATION): drive the real loop — construct an `AgentLoop`
  with a truthful boundary fake for the proxy transport
  (`httpx.MockTransport`) scripted to return a tool call whose args
  violate the declared `parameters_schema`, and a `ToolDef` whose
  `callable_fn` records every invocation; assert the callable is NEVER
  invoked, a `ToolResultError` is sent back on the wire, and the loop
  terminates cleanly — observing dispatch behavior, not just the
  helper's return.
- [ ] AC3: promised tests —
  `tests/unit/test_agent_loop.py::test_schema_violating_args_rejected_before_dispatch`,
  `::test_oversized_args_rejected`, and
  `::test_valid_args_still_dispatched`.
- [ ] AC4: `ruff` + `ruff format --check` + `pytest tests/unit` green;
  zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`;
  `ferova arch graph --check` exits 0.

## Open Questions

(none)
