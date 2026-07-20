---
id: SP-DEVAGENT-TOOLS
title: Developer author + verify tool surface (DEVAGENT slice 1)
version: 0.1
status: draft
author: agent
created: 2026-06-27
updated: 2026-06-27

owns:
  code: [src/repoach/review/devagent_tools.py]
  resources: []

depends_on: []

provides_to: []
constraints: {}
---

# SP-DEVAGENT-TOOLS — the hands that change and check the tree

## Intent
Slice 1 of the real-coding-agent arc (umbrella `docs/devagent_architecture.md`).
Give the Developer the tools a genuine coding agent needs — author files and run
the checks — as `AgentLoop` `ToolDef`s, so a later slice (SP-DEVAGENT-LOOP) can
wire them into `AgentLoop.run` for an author→test→iterate loop. This slice is a
pure additive leaf: the toolbox exists and is tested in isolation; nothing imports
it yet.

## Context
The Planner already has read-only hands (`planner_tools.py`). This mirrors that
module for the Developer's *mutating* + *verifying* hands, reusing what exists:
anchored edits (`patch_apply.apply_search_replace_edits`), the write whitelist
(`coder_loop.is_path_allowed`), the ruff gate (`coder_loop.run_ruff_gate`), and
the repo-jail (`plan.require_repo_relative`).

Four tools from `make_developer_tools(repo_root)`:
- `write_file(path, content)` — create/overwrite a whole file (parent dirs made).
- `edit_file(path, edits)` — ordered anchored `{search, replace}` edits.
- `run_tests(target=tests/unit)` — pytest on a repo-relative target → PASS/FAIL + tail.
- `run_ruff()` — the repo's ruff lint+format gate → PASS/FAIL + tail.

Two invariants, both inherited from the Planner toolbox style:
- **Sandboxed.** Mutating tools jail the path to the repo root AND require
  `is_path_allowed` (never `.github/workflows`, `prompts/review/*`, `.env*`, no
  traversal). `run_tests` jails its target to the repo.
- **Never raise.** Every failure returns an error *string* the model can read and
  correct; tools never throw into the loop.

## Goals
- G1: `make_developer_tools` returns the four `ToolDef`s with JSON-dict schemas.
- G2: Mutating tools refuse forbidden paths and traversal with an error string and
  no write.
- G3: `edit_file` surfaces `apply_search_replace_edits`' anchor report on failure;
  a missing file points the model at `write_file`.
- G4: `run_tests` returns PASS/FAIL with the captured tail; bounded + timeout.

## Non-Goals
- NG1: No wiring into the Developer / `AgentLoop.run` (that is SP-DEVAGENT-LOOP).
- NG2: No new gate logic — reuses `run_ruff_gate` and pytest; no placeholder/AC
  checks (those are SP-DEVAGENT-SELFVERIFY).

## Interface
- `review.devagent_tools.make_developer_tools(repo_root: Path | None) -> list[ToolDef]`.

## Behavior
- write to an allowed path → file written, parents created, `ok:` string.
- write/edit to a forbidden path or via traversal → `error:` string, no write.
- edit with an absent/ambiguous anchor → `error:` with the anchor report; file
  unchanged.
- run_tests on a passing target → `PASS …`; on a failing target → `FAIL …`.

## Architecture Impact
- Owns one new leaf module. Import edges to `agent_engine.agent_loop`,
  `review.patch_apply`, `review.coder_loop`, `review.plan` — declared in
  `depends_on` per the edge-honesty gate.

## Acceptance Criteria
- [ ] AC1: `tests/unit/test_devagent_tools.py` covers the toolbox shape, write
  (incl. parent-dir creation), forbidden-path + traversal refusal (write + edit),
  anchored edit success + absent-anchor + missing-file errors, and run_tests
  PASS/FAIL.
- [ ] AC2: ruff + format + no-inline + `arch check` (edge-honesty) + full
  `pytest tests/unit` green.

## Open Questions
- A `run_tests` target is currently a single path/dir; richer selectors (`-k`,
  multiple paths) can come with the loop slice if the agent needs them.
