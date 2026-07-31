---
id: SP-PROXY-SSE-SINGLE-PARSE
title: Parse each SSE chunk once in peek_for_content's _absorb dispatch
version: 0.1
status: approved
author: agent
created: 2026-07-24
updated: 2026-07-24

owns:
  code: N/A
  resources: []

depends_on: [SP-PROXY-FIRST-BYTE-DEADLINE]
provides_to: []

constraints: {}
---

# Parse each SSE chunk once in peek_for_content's _absorb dispatch

## Intent

`peek_for_content`'s per-chunk dispatcher (`_absorb`, in
`src/repoach/llm_proxy/api/_failover.py`) re-derives the same
`(event_type, data)` tuple up to six times per chunk by calling six
independent `chunk_*` helpers that each call `_parse_event(chunk)` on
their own. `_parse_event` runs two compiled `re.MULTILINE` regex
searches plus a `json.loads` of the data payload. This module is on
the universal streaming dispatch path (every provider response, not
just failover), so for a multi-hundred-chunk completion this is
several hundred redundant regex-and-JSON-parse cycles gating
time-to-first-byte to the client. Parse each chunk exactly once in
`_absorb` and dispatch the five per-chunk checks off that single
parsed tuple, with zero change to `peek_for_content`'s observable
decisions.

## Context

Confirmed against `develop` HEAD (`4dab908`):

- `src/repoach/llm_proxy/api/_failover.py:332-358` — `_absorb`, the
  inner closure `peek_for_content` calls once per chunk. In its
  general-case path (no early `stop_reason == "error"` exit) it
  calls, in order: `chunk_is_tool_use_start(chunk)` (line 336),
  `chunk_message_delta_usage(chunk)` (line 338),
  `chunk_message_delta_error_status(chunk)` (line 343),
  `chunk_message_delta_stop_reason(chunk)` (line 346),
  `chunk_text_delta(chunk)` (line 351), and
  `chunk_marks_stream_end(chunk)` (line 358, the return value used to
  decide `stream_done`) — six calls, each independently re-parsing
  the same immutable `chunk: str`.
- `src/repoach/llm_proxy/api/_failover.py:127-143` — `_parse_event`,
  the shared parse routine every `chunk_*` helper calls: two
  `.search()` calls against `_EVENT_TYPE_PATTERN` /
  `_DATA_LINE_PATTERN` (both compiled with `re.MULTILINE`, defined at
  lines 122-123) plus a `json.loads` of the data line when present.
- The six re-deriving call sites: `chunk_is_tool_use_start`
  (146-155), `chunk_message_delta_usage` (158-167),
  `chunk_message_delta_error_status` (170-183),
  `chunk_message_delta_stop_reason` (186-193),
  `chunk_marks_stream_end` (196-199), `chunk_text_delta` (202-233) —
  every one opens with `event_type, data = _parse_event(chunk)` (or
  the `event_type, _ = ...` form for `chunk_marks_stream_end`).
- `peek_for_content` (249-425) is the universal per-provider dispatch
  path documented in the module docstring (lines 1-44) — it runs for
  every streamed completion, not only when the chain fails over, so
  the six-fold re-parse cost is paid on every request.

`_failover.py` is owned by the existing SP-PROXY-CHAIN-FAILOVER /
SP-PROXY-EARLY-ABORT-ERROR-FRAME / SP-PROXY-FIRST-BYTE-DEADLINE
lineage; this spec is an in-place, behavior-preserving refactor of
the same module.

## Goals

- G1: `_absorb` calls `_parse_event(chunk)` at most once per chunk,
  then dispatches all per-chunk checks (tool-use start, usage,
  error status, stop reason, text delta, stream-end marker) from
  that single parsed `(event_type, data)` tuple.
- G2: `peek_for_content`'s observable outputs — `PeekResult.buffered`,
  `got_content`, `stream_done`, `looks_budget_starved`,
  `final_output_tokens`, `upstream_status_code` — are byte-for-byte
  identical to pre-change behavior for every existing fixture in
  `tests/unit/test_proxy_chain_failover.py`,
  `tests/unit/test_proxy_failover_events.py`,
  `tests/unit/test_proxy_failover_toolless.py`,
  `tests/unit/test_proxy_early_abort_terminal_error.py`,
  `tests/unit/test_proxy_failover_live_reasons.py`, and
  `tests/unit/test_proxy_first_byte_deadline.py` (all already-green,
  run unmodified after this change).
- G3: the public `chunk_is_tool_use_start`, `chunk_message_delta_usage`,
  `chunk_message_delta_error_status`, `chunk_message_delta_stop_reason`,
  `chunk_marks_stream_end`, and `chunk_text_delta` functions keep their
  existing `(chunk: str) -> ...` signatures and documented behavior
  unchanged, so every test that calls them directly with a raw string
  chunk keeps passing with no edits.

## Non-Goals

- NG1: no behavior change beyond eliminating the redundant re-parse
  calls — the failover decision rule (module docstring, lines 8-43),
  the whitespace/disguised-error/tool-use/stop-reason precedence, and
  `PeekResult`'s field semantics are untouched.
- NG2: no change to `_EVENT_TYPE_PATTERN`, `_DATA_LINE_PATTERN`, or
  `_parse_event`'s own parsing logic — this spec only removes
  duplicate *calls* to it, not its internals.
- NG3: no change to callers of `peek_for_content` (`services.py` /
  the chain-failover dispatcher) — the public async function's
  signature and return type are unchanged.
- NG4: no change to the six public `chunk_*` helpers' call signatures
  — they remain `(chunk: str) -> ...` for their existing direct unit
  tests; only their internals may be refactored to share a private
  tuple-based implementation.

## Interface

`src/repoach/llm_proxy/api/_failover.py`:

- Add private helpers operating on an already-parsed tuple instead of
  a raw chunk string:
  - `_is_tool_use_start(event_type: str | None, data: dict | None) -> bool`
  - `_message_delta_usage(event_type: str | None, data: dict | None) -> dict | None`
  - `_message_delta_error_status(event_type: str | None, data: dict | None) -> int | None`
  - `_message_delta_stop_reason(event_type: str | None, data: dict | None) -> str | None`
  - `_marks_stream_end(event_type: str | None) -> bool`
  - `_text_delta(event_type: str | None, data: dict | None) -> str | None`
  Each holds exactly the body currently inside the corresponding
  public `chunk_*` function, minus the `_parse_event` call.
- The six public `chunk_*` functions become thin wrappers that parse
  once and delegate, e.g.:
  ```python
  def chunk_is_tool_use_start(chunk: str) -> bool:
      event_type, data = _parse_event(chunk)
      return _is_tool_use_start(event_type, data)
  ```
  (existing docstrings preserved verbatim on the public wrappers).
- `_absorb(chunk: str) -> bool` (lines 332-358) is rewritten to parse
  once — `event_type, data = _parse_event(chunk)` — and call the six
  private tuple-based helpers directly, never the public `chunk_*`
  wrappers, so no redundant parse occurs on this path.

## Behavior

### Nominal

- A stream of N chunks flows through `peek_for_content` exactly as
  before: `_parse_event` is invoked once per chunk from within
  `_absorb` (plus, unchanged, once per direct call site elsewhere,
  none of which exist on this path) instead of up to six times.

### Edge cases

- A chunk that is unparseable (`_parse_event` returns `(None, None)`)
  — every private helper receiving `(None, None)` returns the same
  falsy/`None` result its public counterpart already returns for that
  input, since the guard conditions (`event_type != "..."` / `data is
  None`) are unchanged, only relocated.
- The early `stop_reason == "error"` exit (line 349-350) still short-
  circuits before the text-delta check, using the same parsed tuple
  already computed at the top of `_absorb` — no second parse is
  triggered by the early return.

### Failure scenarios

- None introduced — this is a pure refactor with no new failure mode.
  If a private helper's guard logic diverges from its public
  counterpart's, the byte-for-byte parity tests in AC2 catch it.

## Acceptance Criteria

- [ ] AC1: unit — with a stream of ordinary `content_block_delta`
  text chunks plus a terminal `message_delta` and `message_stop`,
  wrap `repoach.llm_proxy.api._failover._parse_event` (monkeypatch,
  counting calls) and drive it through `peek_for_content`; assert the
  total call count equals exactly the number of chunks absorbed (one
  parse per chunk), not a multiple of it. This assertion FAILS on
  pre-change code, where the count is five or six times the chunk
  count.
- [ ] AC2: unit — parity fixtures: for each of the existing SSE
  fixture sequences already exercised in
  `tests/unit/test_proxy_chain_failover.py` (tool-use success,
  zero-output-tokens failure, non-empty text success) and
  `tests/unit/test_proxy_failover_toolless.py` /
  `tests/unit/test_proxy_early_abort_terminal_error.py`
  (whitespace-only failure, `stop_reason == "error"` early abort),
  assert `peek_for_content` returns the identical `PeekResult` fields
  (`got_content`, `stream_done`, `looks_budget_starved`,
  `final_output_tokens`, `upstream_status_code`, and `buffered`
  length) after the refactor as before it.
- [ ] AC3: promised tests —
  `tests/unit/test_proxy_failover_parse_once.py::test_absorb_parses_each_chunk_exactly_once`
  and
  `::test_peek_for_content_decisions_unchanged_after_single_parse_refactor`.
- [ ] AC4: the existing suites
  `tests/unit/test_proxy_chain_failover.py`,
  `tests/unit/test_proxy_failover_events.py`,
  `tests/unit/test_proxy_failover_toolless.py`,
  `tests/unit/test_proxy_early_abort_terminal_error.py`,
  `tests/unit/test_proxy_failover_live_reasons.py`, and
  `tests/unit/test_proxy_first_byte_deadline.py` remain green
  unmodified.
- [ ] AC5: `ruff` + `ruff format --check` + `pytest tests/unit` green;
  zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`;
  `repoach arch graph --check` exits 0.

## Architecture Impact

- Adds/Removes dependency: none — in-place modification of a single
  existing module (`llm_proxy/api/_failover.py`); no new cross-owner
  import, no new public symbol exported outside this module (the six
  new `_`-prefixed helpers are module-private).
- New / changed coupling, cycles, or shared state: none — the six
  public `chunk_*` functions keep their existing signatures so no
  caller elsewhere in the tree needs a change; the private helpers
  are called only from within `_failover.py`.

## Diagram

N/A (in-place fix, no data-flow change).
</spec_markdown>
