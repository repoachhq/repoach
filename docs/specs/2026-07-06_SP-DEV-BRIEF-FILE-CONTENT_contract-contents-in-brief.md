---
id: SP-DEV-BRIEF-FILE-CONTENT
title: Contract file contents in the step brief
version: 0.1
status: approved
author: jfaye (improvement-axes report; read-turn telemetry 2026-07-04/05)
created: 2026-07-06
updated: 2026-07-06

owns:
  code: []
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Contract file contents in the step brief

## Intent

Stop paying the LLM to rediscover its own working set. Developer
dispatches routinely spend two thirds of their tool budget reading the
step's contract files before the first write (21 of 26 calls on
SP-USAGE-REASONING-SPLIT dispatch 1; ~20 read turns of 30 on
SP-AGENT-THINKING-CONTROL step 4) — content the runner already knows
at brief-build time.

## Context

`build_step_brief` (`src/ferova/review/dev_runner.py`, owned by
SP-DEV-STEP-PREFLIGHT — this spec modifies without claiming) already
embeds the spec section, the plan step, the repo tree and gate
feedback. The step's file contract (`step.files`) is known, and
`read_existing_files` shows the house pattern for jail-safe reads.
The Developer's `read_file` keeps its paging (#18) for everything
OUTSIDE the contract; this spec only pre-loads what the step is FOR.

## Goals

- G1: The step brief embeds the current content of every existing
  contract file, each under a clear heading naming the path, with a
  per-file cap and a total budget (constants, e.g. 12k chars per file
  and 48k total) — over-budget files are truncated head-first with a
  note naming the exact `read_file(path, start_line=N)` continuation.
- G2: Contract paths that do not exist yet are listed under a "to
  create" heading so the Developer never greps for them.
- G3: The brief's retry variant (gate feedback) keeps the same
  embedding, refreshed from disk (the loop's own writes must be
  visible on retry).

## Non-Goals

- NG1: No tool changes — `read_file`/`grep_repo` stay as they are for
  out-of-contract exploration.
- NG2: No embedding of non-contract files (the repo tree remains the
  orientation for those).
- NG3: No prompt-budget accounting beyond the fixed caps.

## Assumptions

- A1: `dev_runner.py` remains owned by SP-DEV-STEP-PREFLIGHT; this
  spec owns nothing and adds no import edge.
- A2: The caps keep worst-case briefs under the proxy's context
  budget (the largest current contract file, `dev_runner.py` itself,
  is ~50k chars — the per-file cap deliberately truncates it).

## Interface

Inputs: N/A (internal change to `build_step_brief`).

Outputs: N/A (brief text shape).

Errors: none raised — an unreadable contract file is listed with the
read error, mirroring `read_existing_files`.

## Behavior

### Nominal

A step whose contract is one existing source file and one new test
file gets a brief carrying the source file's full content and a "to
create" entry for the test — the first tool call can be `write_file`.

### Edge cases

- Contract file larger than the per-file cap → truncated with the
  exact continuation call in the note.
- Total budget exhausted → remaining files listed by name with "read
  on demand" notes.
- Retry after a red gate → contents re-read from disk so the loop's
  previous writes are visible.

### Failure scenarios

- Read error on a contract file → the brief lists the path with the
  error string; the Developer falls back to its tools.

## Architecture Impact

- No edge added or removed; no ownership change.

## Diagram

N/A (brief assembly change in one function).

## Acceptance Criteria

- [ ] AC1: `tests/unit/test_dev_brief_file_content.py::test_brief_embeds_existing_contract_files`
  — a brief for a step with one existing file contains the file's
  content under a heading naming its path.
- [ ] AC2: `tests/unit/test_dev_brief_file_content.py::test_brief_lists_missing_contract_files_to_create`
  — nonexistent contract paths appear under the to-create heading.
- [ ] AC3: `tests/unit/test_dev_brief_file_content.py::test_oversized_file_truncated_with_continuation_note`
  — a file above the per-file cap is truncated and the note names
  `read_file` with the exact next start_line.
- [ ] AC4: `tests/unit/test_dev_brief_file_content.py::test_retry_brief_reflects_disk_state`
  — after a simulated loop write, the retry brief carries the new
  content.
- [ ] AC5: The full unit suite passes.

## Open Questions

(none)
