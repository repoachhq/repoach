---
id: SP-CODER-CHAINS-GUARD
title: chains.env is Coder-forbidden — chain changes are data-driven or human
version: 0.1
status: approved
author: jfaye (hole found by adversarial panel 2026-07-11; architecture docs/chain_resilience_architecture.md W1.5)
created: 2026-07-11
updated: 2026-07-11

owns:
  code: []
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# chains.env is Coder-forbidden — chain changes are data-driven or human

## Intent

Close a discovered hole: the Coder's path filter blocks `.env`,
`.env.*` and `.envrc` by basename plus the `FORBIDDEN` path lists —
but `chains.env` passes. On a REQUEST_CHANGES round, an LLM Coder
could rewrite the capability chains from reviewer prose with zero
probe evidence, and neither CI (which never exercises `chains.env`
against live providers) nor the evidence-first merge gate would
catch it. Chain changes must come from humans or from data-driven
regeneration (the chainpilot), never from LLM text edits.

## Context

`is_path_allowed` (`src/ferova/review/coder_loop.py:82-114`) rejects
env-like basenames (`:109`); the `FORBIDDEN` path/prefix lists
(`coder_loop.py:54-71`) cover all of `.github/`, `.githooks/` and
`prompts/review/`; absolute paths and `..` traversal are rejected
inside the function body (`:103-107`). The same predicate is the
autonomous Developer's write gate
(`src/ferova/review/devagent_tools.py:123`), so this guard
intentionally binds BOTH LLM actors — any future spec whose
implementation must edit `chains.env` content is human-implemented
or chainpilot-regenerated, never `ferova develop`.
`chains.env` is the AUTHORITATIVE
model-selection config (SP-CHAINS-SINGLE-SOURCE) read by the proxy
and sourced by CI. Its existing tests live in
`tests/unit/test_review_coder_loop.py`. The wave-3 chainpilot
propose-PR mode (architecture doc) assumes this guard: a chainpilot
PR is fixed by regenerating from data, never by Coder edit.

## Goals

- G1: any Coder-emitted change touching `chains.env` (at any depth)
  is rejected by the path filter, with the same rejection surface as
  the other forbidden paths.

## Non-Goals

- NG1: no change to what humans or the (future) chainpilot may do to
  `chains.env`.
- NG2: no broadening of the filter beyond this one basename — the
  whitelist redesign is not this spec.

## Assumptions

- A1: no legitimate Coder fix has ever needed to touch `chains.env`
  (verified: no such path appears in any `pr_coder_responses`
  history-driven fix flow to date).

## Interface

Inputs / Outputs: unchanged — `is_path_allowed(path: str) -> bool`
returns `False` for any path whose basename is exactly `chains.env`.

Errors: none (pure predicate).

## Behavior

### Nominal

`is_path_allowed("chains.env")` → `False`;
`is_path_allowed("sub/dir/chains.env")` → `False` (basename match,
consistent with the `.env` handling).

### Edge cases

- `chains.env.bak`, `mychains.env` → judged by the existing rules
  only (this spec matches the exact basename `chains.env`; exactly
  `.env`, `.env.*` variants and `.envrc` are already blocked).
- Case variations (`Chains.env`) → allowed (the repo is
  case-sensitive; the canonical file is exactly `chains.env` —
  matching stays exact, no case folding).

### Failure scenarios

- A Coder response containing a `chains.env` hunk → the hunk is
  rejected through the existing forbidden-path flow (same log +
  refusal shape as a `.github/workflows/*` hunk; no new error type).

## Architecture Impact

- New / changed coupling, cycles, or shared state: none. Tightens the
  existing chain of custody: chains.env writers = {human, chainpilot
  regeneration}, enforced at the shared LLM write gate — the
  predicate guards both the Coder fix flow and the Developer write
  tool (`devagent_tools.py:123`).

## Diagram

N/A (one predicate extension).

## Acceptance Criteria

- [ ] AC1: unit tests in `tests/unit/test_review_coder_loop.py`:
  `is_path_allowed` returns `False` for `chains.env` and
  `a/b/chains.env`; regression — `src/ferova/x.py`, `tests/unit/x.py`
  and `docs/x.md` remain `True`; `.env`/`.env.local` remain `False`.
- [ ] AC2: the fix-flow-level rejection test (same pattern as the
  existing forbidden-path rejection cases in
  `tests/unit/test_review_coder_loop.py`) shows a Coder patch with a
  `chains.env` hunk is refused end-to-end, not silently dropped.
- [ ] AC3: `ruff` clean, no inline comments, full `pytest tests/unit`
  green; diff ≤ 15 LOC of source change.

## Open Questions

(none)
