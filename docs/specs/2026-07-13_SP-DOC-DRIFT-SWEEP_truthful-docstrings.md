---
id: SP-DOC-DRIFT-SWEEP
title: Correct three docstrings that contradict the shipped code
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

# Correct three docstrings that contradict the shipped code

## Intent

Three docstrings assert behavior the code does not implement — a
security-adjacent hazard because reviewers and future agents trust
these claims when reasoning about the whitelist, the agent-loop failure
paths, and the inline-comment healer. Correct each docstring to the
shipped policy. This spec changes documentation only; it prescribes NO
behavior change.

## Context

Audit 2026-07-13 documentation-drift findings:

- **D1** — `src/ferova/review/mcp_whitelist.py:5` (module docstring)
  claims the default policy is "fail-closed: every role maps to an
  empty tuple." The shipped `MCP_TOOL_WHITELIST_BY_ROLE`
  (lines 30-44) grants `git_status` to ARCHITECT / SENTINEL / TESTER /
  SCRIBE and `("git_status", "git_log")` to CODER / DEVELOPER; only
  PLANNER maps to `()`. The false "every role → empty tuple" claim
  understates the granted surface.
- **D2** — `src/ferova/agent_engine/agent_loop.py:149-155`
  (`NimAgentOutput.trace` attribute docstring) says the field is
  "Empty when the loop fell through to a transport-level stub." No such
  stub fallback exists any more — every failure path in the loop
  raises. The documented fallback is dead prose.
- **D3** — `src/ferova/review/inline_comment_heal.py:13-15` (module
  docstring) claims "standalone full-line comments are deliberately
  left for the gate to reject." The gate itself
  (`src/ferova/lint/no_inline_comments.py:22-28`) explicitly lists
  "Any standalone line comment, anywhere" as IMPLICITLY ALLOWED — the
  healer leaves them untouched because they are legal, not because the
  gate rejects them. The stated rationale is backwards.

Each fix is a pure docstring edit inside an existing already-owned
module (`owns.code: []`). No runtime surface changes.

## Goals

- G1: the `mcp_whitelist` module docstring states the actual per-role
  policy (reviewers get `git_status`; Coder/Developer additionally get
  `git_log`; Planner gets none) and preserves the accurate
  fail-closed-by-default framing for the LOOKUP (unknown role → `()`).
- G2: the `NimAgentOutput.trace` docstring describes the real
  empty-trace condition (e.g. the proxy emitted no trace) and drops the
  non-existent transport-level-stub fallback.
- G3: the `inline_comment_heal` module docstring states the true reason
  standalone line comments are left alone: the gate ALLOWS them, so
  they need no healing.

## Non-Goals

- NG1: NO behavior change — no edit to `MCP_TOOL_WHITELIST_BY_ROLE`, to
  any agent-loop control flow, or to the healer's line handling. If a
  reader believes the shipped policy is wrong, that is a SEPARATE spec.
- NG2: no docstring rewrites beyond the three false claims (no
  drive-by restyling of surrounding prose).
- NG3: no new tests of runtime behavior (there is none to change).

## Assumptions

- A1: the shipped code (whitelist rows, agent-loop raise-only failure
  paths, gate's allow-standalone rule) is the intended behavior and the
  docstrings are what drifted — confirmed against the cited anchors.

## Interface

N/A (doc-only; no signature, no public API change).

## Behavior

### Nominal

No runtime behavior changes. Only docstring text is edited at the three
anchors above so each statement matches the code beside it.

### Edge cases

N/A (doc-only).

### Failure scenarios

N/A (doc-only) — there is no fail-open behavior here; the risk is a
human/agent trusting a false claim, which the correction removes.

## Architecture Impact

- Adds/Removes dependency: none — in-place docstring edits in three
  existing modules (each owned by an existing spec); introduces no new
  import or coupling.
- New / changed coupling, cycles, or shared state: none.

## Diagram

N/A (doc-only).

## Acceptance Criteria

- [ ] AC1: the corrected docstrings no longer contain the false claims,
  asserted deterministically by a small test that reads the three
  module/attribute docstrings and checks the stale phrases are absent
  and a truthful phrase is present — e.g. `mcp_whitelist.__doc__` no
  longer contains "every role maps to an empty tuple" and DOES mention
  `git_log` for Coder/Developer; `NimAgentOutput` docstring no longer
  contains "transport-level stub"; `inline_comment_heal.__doc__` no
  longer claims standalone comments are "left for the gate to reject"
  and instead states the gate allows them. Promised test:
  `tests/unit/test_doc_drift_sweep.py::test_docstrings_match_shipped_policy`.
- [ ] AC2 (INTEGRATION — N/A for doc-only, replaced by a real-flow
  cross-check): the same test imports the actual runtime objects and
  asserts the docstring now agrees with the code it documents —
  `allowed_tools_for(BotRole.CODER)` contains `"git_log"` while the
  docstring's claimed policy matches, and
  `no_inline_comments`-scanning a file with a lone standalone comment
  yields zero violations (proving the gate allows what the healer's
  corrected docstring now says). This drives the real whitelist lookup
  and the real gate scanner, not a paraphrase.
- [ ] AC3: promised test file + selectors named in AC1/AC2
  (`tests/unit/test_doc_drift_sweep.py`).
- [ ] AC4: `ruff` + `ruff format --check` + `pytest tests/unit` green;
  zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`;
  `ferova arch graph --check` exits 0.

## Open Questions

(none)
