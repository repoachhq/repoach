# SP-PROXY-LOG-EXTRA — serialize loguru extra fields in the JSON sink

## Metadata

- **Status**: OPEN
- **Priority**: P1
- **Owner**: operator
- **Executor**: `ferova develop`
- **Opened**: 2026-06-11

## Why

The 2026-06-11 audit confirmed (against a live rotated `server.log`)
that `_serialize_with_context` in
`src/ferova/llm_proxy/config/logging_config.py` emits only
`time/level/message/module/function/line` plus the three whitelisted
context keys (`request_id`, `node_id`, `chat_id`). All chain-walk
telemetry is logged kwarg-style (`proxy_chain_failover_fired(
primary_reason=…, latency_s=…, chain_remaining=…)`,
`proxy_chain_exhausted(failures=…)`, `proxy_budget_retry(…)` in
`api/services.py`); loguru puts those kwargs in `record["extra"]`, and
the serializer discards them. The entire NIM observability arc
(#336 monitor-chains, #338 durable+enriched logs) is therefore writing
attribute-less events — the deferred NIM degradation analysis has no
data to work with.

## What

In `_serialize_with_context`:

1. After building the six fixed fields and promoting the three context
   keys, merge **every remaining** `record["extra"]` item into the
   output object, skipping any key that would collide with the six
   reserved field names (`time`, `level`, `message`, `module`,
   `function`, `line`) — collisions are emitted under an `extra_`
   prefix instead of overwriting.
2. Values stay JSON-serialised via the existing `default=str`, so
   exceptions, Paths and enums degrade to strings rather than raising
   inside the sink.
3. Update the module docstring to state that all bound/kwarg extras
   are emitted at top level.

No call-site changes: the existing `logger.warning("event", key=val)`
style throughout `llm_proxy` becomes fully durable as-is.

## Files in scope

- `src/ferova/llm_proxy/config/logging_config.py`
- `tests/unit/test_proxy_logging_extra.py` (new)

## Out of scope

- Making the file-sink level configurable (`FEROVA_PROXY_LOG_LEVEL` is
  currently dead — separate cleanup).
- Re-classifying `empty_completion` vs real upstream causes in the
  failover logs (separate proxy slice).
- Any change to the structlog configuration of the main tree.

## Smoke scenario

### Setup

Configure loguru with a list-appending sink (the established loguru
test pattern in this repo) using the real `_serialize_with_context`
format function, in a tmp-path log file.

### Execute

Emit `logger.bind(request_id="req_x").warning(
"proxy_chain_failover_fired", primary_reason="empty_completion",
latency_s=1.25, chain_remaining=3)`.

### Expected

The captured line parses as JSON and contains `request_id`,
`primary_reason`, `latency_s == 1.25`, `chain_remaining == 3` at top
level, alongside the six fixed fields.

## Definition of Done

- Kwarg extras appear at top level in the serialized JSON —
  `test_extra_kwargs_serialized`.
- The three context keys still appear and take precedence —
  `test_context_keys_promoted`.
- A reserved-name collision (`extra` containing `message=`) does not
  overwrite the record message — `test_reserved_key_collision_prefixed`.
- Non-JSON-serialisable extra values degrade via `default=str` without
  raising — `test_non_serializable_value_degrades`.
- `ruff` + `ruff format --check` + `pytest tests/unit` green; zero
  inline comments; no `# noqa`.

## Commit plan

1. `fix(proxy): JSON log sink serializes all loguru extra fields`
2. `test(proxy): extra-field serialization, precedence and collisions`

## Risks

- **Log volume**: lines grow by their kwargs; chain-walk events are
  low-frequency and the file sink already rotates — negligible.
- **Downstream parsers**: any tooling assuming the old fixed schema
  keeps working (fields are additive).
