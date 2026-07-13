---
id: SP-PROXY-LOG-CONTENT-GUARD
title: Gate full-content proxy logging behind an explicit opt-in; keep prompts off argv
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

constraints:
  proxy_log_full_content_default: false
---

# Gate full-content proxy logging behind an explicit opt-in; keep prompts off argv

## Intent

The proxy persists PR diffs, source, and prompts verbatim to a 30-day file sink
by default, and puts the full system prompt in process argv where any local user
can read it. Redact/omit content bodies by default, gate full-content logging
behind an explicit off-by-default setting, and move the system prompt off argv.

## Context

`src/ferova/llm_proxy/config/logging_config.py:104-112` installs the file sink
UNCONDITIONALLY at `level="DEBUG"` (50 MB rotation, 30-day retention). At DEBUG:
- `services.py:257` logs `FULL_PAYLOAD [{}]: {}` with
  `attempt_request.model_dump()` — the complete request (all messages) of every
  attempt.
- `src/ferova/llm_proxy/core/anthropic/sse.py:143` logs `SSE_EVENT: {} - {}`
  for every outgoing SSE event (the full serialized event body).

Together these persist prompts, diffs, and model output verbatim for a month.

`src/ferova/llm_proxy/providers/claude_code/client.py:129-138` appends
`["--system-prompt", system_prompt]` to argv (`client.py:120-130`) — readable
via `/proc/<pid>/cmdline` — and then logs the whole command with
`logger.info("CLAUDE_CODE_STREAM:... cmd={}", ..., shlex.join(cmd))`
(`client.py:133-138`), leaking the system prompt at INFO regardless of the file
sink level. The subprocess is spawned at `client.py:146` with `stdin=PIPE`
already carrying the user prompt. Audit 2026-07-13 findings M18 + M19.

## Goals

- G1: a new off-by-default setting (e.g. `proxy_log_full_content: bool = False`,
  `FEROVA_*`-aliased) gates the verbatim `FULL_PAYLOAD` (`services.py:257`) and
  `SSE_EVENT` body (`sse.py:143`) logs. Default OFF: neither request bodies nor
  SSE event bodies are emitted; only non-content metadata (request_id, model,
  message count, event type) is logged.
- G2: the file sink no longer persists content bodies by default — with the
  setting OFF, a full request through the proxy leaves no prompt/diff/output
  body in the sink.
- G3: the `claude` CLI system prompt is moved OFF argv — delivered via stdin or
  a temp file (alongside the existing stdin user prompt at `client.py:146`) — so
  it no longer appears in `/proc/<pid>/cmdline`.
- G4: the command log at `client.py:133-138` no longer includes the system
  prompt (log the argv WITHOUT the prompt, or drop the `cmd=` field).

## Non-Goals

- NG1: no removal of the file sink itself, its rotation, or retention — only the
  content bodies are gated. Operators who opt in still get full DEBUG payloads.
- NG2: no redaction framework for arbitrary secrets in bodies — the guard omits
  bodies wholesale by default rather than scrubbing them field by field.
- NG3: no change to the `claude` CLI flags other than how the system prompt is
  delivered.

## Assumptions

- A1: the `claude` CLI accepts the system prompt via stdin or a file argument
  without requiring it on argv (verified against the CLI's `--system-prompt`
  behavior; if only argv is supported, deliver via a mode the CLI documents and
  note it in the plan).
- A2: operators who need full-content debugging can set the opt-in explicitly;
  the default posture is privacy-preserving.

## Interface

Changed (in-place):
- New setting `proxy_log_full_content: bool = False` on the llm_proxy
  `Settings`.
- `services.py:257` and `sse.py:143` guard their body logs on this setting
  (read from settings/config already in scope at each call site).
- `claude_code/client.py` system-prompt delivery changes from argv to
  stdin/file; the `cmd=` log field is stripped of the prompt.

## Behavior

### Nominal (default, setting OFF)

A request flows through the proxy; the file sink records request/response
METADATA only — no `FULL_PAYLOAD` body, no `SSE_EVENT` body. The `claude` CLI
runs with the system prompt off argv and unlogged.

### Edge cases

- Setting ON: legacy behavior — full payloads and SSE bodies logged at DEBUG
  (explicit operator opt-in).
- No system prompt supplied to the CLI: nothing to move; command log unchanged
  except for the (now absent) prompt field.

### Failure scenarios

- If the setting is misconfigured/unset, it defaults to `False` (fail CLOSED to
  the privacy-preserving posture) — content is never logged by accident.

## Architecture Impact

- Adds dependency: none new at the spec-graph level — in-place modification of
  `logging_config.py`, `services.py`, `sse.py`, `settings.py`, and
  `claude_code/client.py` (all owned by existing specs). No new cross-owner
  import.
- New / changed coupling, cycles, or shared state: `services.py` and `sse.py`
  now read one shared setting flag; no cycle.

## Diagram

N/A (in-place fix across existing call sites).

## Acceptance Criteria

- [ ] AC1: unit — with `proxy_log_full_content=False`, the guarded branches at
  `services.py:257` and `sse.py:143` do NOT emit body content (assert via a
  captured loguru sink that the emitted records carry only metadata); with the
  flag `True`, the bodies are present.
- [ ] AC2 (INTEGRATION): drive a real request through the proxy FastAPI
  `TestClient` (providers backed by `httpx.MockTransport` truthful boundary
  fakes) with default settings and a loguru sink capturing all records; assert
  no record contains the request message body / system-prompt text
  (`FULL_PAYLOAD` body and `SSE_EVENT` body absent). No monkeypatching of Ferova
  code.
- [ ] AC3: unit — the `claude_code` command builder places the system prompt on
  stdin/file, NOT in the argv list, and the command log record contains no
  system-prompt substring; promised selectors:
  `tests/unit/test_proxy_log_content_guard.py::test_default_off_no_bodies_in_sink`,
  `::test_opt_in_logs_bodies`,
  `tests/unit/test_claude_code_client_argv.py::test_system_prompt_off_argv`.
- [ ] AC4: `ruff` + `ruff format --check` + `pytest tests/unit` green; zero
  inline comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`;
  `ferova arch graph --check` exits 0.

## Open Questions

(none)
