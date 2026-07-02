---
id: SP-FINDINGS-BRIDGE-DOCFIX
title: Drop the stale coder_loop cross-reference from _files_in_diff
version: 0.1
status: approved
author: jfaye + Claude (tech-debt survey 2026-07-02, ledger entry 10)
created: 2026-07-02
updated: 2026-07-02

owns:
  code: [src/ferova/review/findings_bridge.py]
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Drop the stale coder_loop cross-reference from _files_in_diff

## Intent

Make the `_files_in_diff` docstring in `findings_bridge.py` truthful
again: it claims to mirror a `coder_loop._files_in_diff` counterpart
"deliberately" as a "temporary duplicate", but no such counterpart
exists anywhere in the repository — the helper is the single
implementation. Stale prose misleads maintainers into hunting for a
duplicate that is not there.

## Context

`findings_bridge.py` converts reviewer comments into ledger Findings;
its `_files_in_diff` helper parses unified-diff headers for the
off-diff comment filter (SP-FINDINGS-OFFDIFF-FILTER). The 2026-07-02
tech-debt survey (docs/tech_debt.md entry 10) verified that
`coder_loop.py` contains no diff-header parser and never did in the
repo's visible history: the docstring's cross-reference and
"temporary duplicate" framing are stale, not a live duplication.
This spec also promotes `findings_bridge.py` from the ungoverned
frontier into the governed regime (opportunistic erosion per
SP-SPEC-TEMPLATE).

## Goals

- G1: The `_files_in_diff` docstring no longer references
  `coder_loop._files_in_diff` nor describes the helper as a temporary
  duplicate.
- G2: The failure-soft rationale stays documented: malformed input
  yields whatever could be parsed, an empty set is acceptable, and the
  caller then keeps every comment (historical no-filter behaviour).
- G3: A unit test pins the docstring invariant so the stale
  cross-reference cannot silently return.

## Non-Goals

- NG1: No change to any executable statement — this is a
  docstring-only slice; parsing behaviour is already pinned by the
  existing `findings_bridge` tests.
- NG2: No renaming, moving, or refactoring of `_files_in_diff`.
- NG3: No other docstring in the module is touched.

## Assumptions

- A1: `tests/unit/` and `tests/integration/test_findings_bridge.py`
  cover the helper's behaviour and stay green untouched.
- A2: `findings_bridge.py` has no owner in the arch registry
  (verified 2026-07-02: `Registry.owner_of` returns None), so this
  spec may claim `owns.code` without a disjointness conflict.

## Interface

Inputs: N/A (documentation-only change; the function signature
`_files_in_diff(diff: str) -> set[str]` is unchanged).

Outputs: N/A (unchanged).

Errors: N/A (unchanged).

## Behavior

### Nominal

The docstring describes what the helper does (walks `diff --git`,
`+++ b/`, `--- a/` headers, whitespace-tolerant, discards
`/dev/null`) and why it is failure-soft — with no reference to any
counterpart in another module.

### Edge cases

- N/A (documentation-only).

### Failure scenarios

- N/A (documentation-only).

## Architecture Impact

- No edge added or removed. `findings_bridge.py` moves from the
  frontier into this spec's `owns.code`; its imports resolve to
  frontier files (reported, not enforced), so `depends_on` stays
  empty.

## Diagram

N/A (docstring-only slice).

## Acceptance Criteria

- [ ] AC1: `grep -n "coder_loop" src/ferova/review/findings_bridge.py`
  returns no matches.
- [ ] AC2: The `_files_in_diff` docstring still documents the
  failure-soft contract (malformed input -> parsed subset, empty set
  acceptable, caller keeps every comment).
- [ ] AC3: `tests/unit/test_findings_bridge_docfix.py` asserts the
  docstring invariants of AC1 + AC2 and passes.
- [ ] AC4: The full unit suite passes with no change to any
  non-docstring line of `findings_bridge.py`.

## Open Questions

(none)
