# SP-STRIP-DEAD-MODULES — delete two orphan proxy helper modules

## Metadata

- **Status**: OPEN
- **Priority**: P3
- **Owner**: operator
- **Executor**: hand-implemented
- **Opened**: 2026-06-10

## Why

Continuing the "builder only — everything outside is removed" sweep, an
orphan scan over `src/` found two modules imported by **nothing**:

- `llm_proxy/api/command_utils.py` — `extract_command_prefix` /
  `extract_filepaths_from_command`: command-string parsing from the
  stripped WhatsApp/command era. Zero importers, zero symbol users.
- `llm_proxy/core/anthropic/stream_contracts.py` — an SSE-contract
  assertion helper (`assert_anthropic_stream_contract`, `parse_sse_*`,
  …). Zero importers; the `anthropic/__init__` re-exports `content`,
  `conversion`, `errors`, `sse`, `thinking`, `tokens`, `tools`, `utils`
  but NOT `stream_contracts`. The lone `thinking_content` name match is
  a local variable in `thinking.py`, not an import.

## What

Delete both files. No other change — nothing imports them.

## Files in scope

- `src/ferova/llm_proxy/api/command_utils.py` (deleted)
- `src/ferova/llm_proxy/core/anthropic/stream_contracts.py` (deleted)

## Out of scope

- Any module that IS imported (the scan is module-level; orphan
  *functions* inside live modules are a finer later pass).

## Definition of Done

- Both files gone; `grep -r "command_utils|stream_contracts" src tests`
  is clean.
- The proxy app, the CLI, the review orchestrator/reviewer, and the
  AgentLoop all still import; full `pytest tests/unit` + integration
  green (the definitive proof nothing used them).

## Commit plan

1. `chore(proxy): delete two orphan helper modules (dead code)`

## Risks

- None: confirmed zero importers; the full test suite is green after
  deletion.
