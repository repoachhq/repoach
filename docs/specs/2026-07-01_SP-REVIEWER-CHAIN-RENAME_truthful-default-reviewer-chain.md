---
id: SP-REVIEWER-CHAIN-RENAME
title: Rename DEFAULT_NIM_CHAIN to a truthful DEFAULT_REVIEWER_CHAIN
version: 0.1
status: draft
author: operator
created: 2026-07-01
updated: 2026-07-01

owns:
  code: N/A                # pure rename of an existing symbol; owns no new file
  resources: N/A           # no shared state

depends_on: []             # no NEW edge — the reviewer -> agent_engine import already exists
provides_to: []
constraints: {}
---

# SP-REVIEWER-CHAIN-RENAME — a truthful name for the reviewers' default chain

## Intent
Retire the misleading module-level constant `DEFAULT_NIM_CHAIN`. Despite the
name it is NOT a NIM-only chain: it is literally aliased to
`PROXY_SONNET_CHAIN` (the proxy *sonnet* capability alias), and every agent
that uses it sends a `PROXY_*_CHAIN` alias to the proxy, which walks the full
`MODEL_SONNET` chain ending in the `claude_code/sonnet` tail. The "NIM" in the
name is a genesis-era vestige that wrongly suggests reviewers are pinned to
NIM. Rename it to `DEFAULT_REVIEWER_CHAIN` — its real role, the base
`Reviewer.model_chain` default. Tech-debt ledger item #1.

## Context
`DEFAULT_NIM_CHAIN` is defined in `agent_engine/agent_loop.py` (`= PROXY_SONNET_CHAIN`,
listed in `__all__`, described in the module docstring) and imported once, by
`review/reviewer.py`, where it is the default value of the base
`Reviewer.model_chain` field. Neither file is owned by a governed spec (both are
frontier), and the reviewer -> agent_engine import already exists, so this
change adds no architecture edge. The value is unchanged — only the identifier.

## Goals
- G1: `agent_loop.py` exports `DEFAULT_REVIEWER_CHAIN` (def, `__all__`, and the
  module-docstring bullet) with the same value it has today (`PROXY_SONNET_CHAIN`).
- G2: `reviewer.py` imports and uses `DEFAULT_REVIEWER_CHAIN` as the base
  `Reviewer.model_chain` default.
- G3: The old name `DEFAULT_NIM_CHAIN` is gone from `src/` (no back-compat alias —
  it has exactly one internal importer and no external consumers).
- G4: Tech-debt ledger item #1 is removed from `docs/tech_debt.md`.

## Non-Goals
- NG1: Does NOT change any chain value, routing behaviour, or the resolved
  `MODEL_SONNET` walk — identifier only.
- NG2: Does NOT touch the other back-compat aliases (`PROXY_SONNET_CHAIN`,
  `PROXY_OPUS_CHAIN`) — they carry truthful names already.
- NG3: Does NOT introduce a deprecation shim for the old name.

## Assumptions
- A1: `DEFAULT_NIM_CHAIN` has no importer outside this repo — it is an internal
  module constant, so a hard rename (no alias) breaks nothing downstream.
- A2: `PROXY_SONNET_CHAIN` remains the correct value for the reviewers' base
  default (unchanged by this spec).

## Interface
- Removed: `agent_engine.agent_loop.DEFAULT_NIM_CHAIN`.
- Added: `agent_engine.agent_loop.DEFAULT_REVIEWER_CHAIN: tuple[str, ...]`
  (value: `PROXY_SONNET_CHAIN`), exported in `__all__`.
- `review.reviewer` imports `DEFAULT_REVIEWER_CHAIN` and uses it as
  `Reviewer.model_chain`'s default.

## Behavior

### Nominal
A grep for `DEFAULT_NIM_CHAIN` over `src/` returns nothing; a grep for
`DEFAULT_REVIEWER_CHAIN` returns the def + `__all__` entry in `agent_loop.py`
and the import + field default in `reviewer.py`. Reviewer construction and the
resolved proxy chain are byte-for-byte identical to before.

### Edge cases
- The module docstring's constants section names `DEFAULT_REVIEWER_CHAIN`, not
  the retired name -> documentation stays honest.

### Failure scenarios
- A stray reference to the old name anywhere in `src/`/`tests/` -> `ruff`
  (F821 undefined name) or the import fails at collection time -> caught by CI.

## Architecture Impact
- No dependency added or removed: the `review -> agent_engine` import edge
  already exists and is unchanged; both files are frontier (un-owned). No new
  coupling, cycle, or shared state.

## Diagram
N/A — single-symbol rename, no internal flow.

## Acceptance Criteria
- [ ] AC1: `grep -rn "DEFAULT_NIM_CHAIN" src` returns no matches.
- [ ] AC2: `agent_loop.py` defines `DEFAULT_REVIEWER_CHAIN = PROXY_SONNET_CHAIN`
  and lists it in `__all__`; the module docstring names it instead of the old name.
- [ ] AC3: `reviewer.py` imports `DEFAULT_REVIEWER_CHAIN` and the base
  `Reviewer.model_chain` default resolves to `PROXY_SONNET_CHAIN` (value unchanged).
- [ ] AC4: Tech-debt ledger item #1 is deleted from `docs/tech_debt.md`.
- [ ] AC5: ruff + format + no-inline-comments + `arch check` pass; full
  `pytest tests/unit` green.

## Open Questions
- None.
</content>
</invoke>
