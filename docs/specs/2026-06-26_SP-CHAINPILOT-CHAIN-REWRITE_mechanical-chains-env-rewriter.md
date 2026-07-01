---
id: SP-CHAINPILOT-CHAIN-REWRITE
title: Mechanical chains.env rewriter — apply structural edits, preserve everything else
version: 0.1
status: draft
author: agent
created: 2026-06-26
updated: 2026-06-26

owns:
  code: src/ferova/review/chain_rewrite.py
  resources: N/A

depends_on: []                   # Chain / ModelRef are frontier/pre-template (like 1e)

provides_to: []                  # AUTO-maintained (consumed by SP-CHAINPILOT-PLAN-REWRITE 3d-1b + SP-CHAINPILOT-APPLY-WRITE 3d-2)
constraints: {}
---

# SP-CHAINPILOT-CHAIN-REWRITE — the hands, not the brain

## Intent
Phase 3d-1a. The mechanical half of the apply layer: a pure, text-in / text-out
function that takes the `chains.env` content plus a list of structural edits and
re-renders **only** the four `MODEL_*` slot lines, leaving every comment, blank
line, other key and ordering byte-for-byte verbatim. It knows how to reshape a
chain *safely* (the backstop is never removed, a chain is never emptied,
demote/promote shift one step within the mutable prefix) but holds **no policy**
— which edits to make is the decision engine (3b) + the placement classifier
(3d-1b); whether to write the result to disk is 3d-2. Splitting the hands from
the brain keeps this slice fully testable on plain strings.

## Context
`chains.env` is the authoritative source for the four capability chains
(SP-CHAINS-SINGLE-SOURCE), each a comma-separated, ordered failover list of
`provider/model` refs with a `claude_code/<tier>` subprocess **backstop** at the
tail. The file is hand-curated with rich `#` comments that must survive any
machine edit untouched. `ModelRef.parse` / `str(ModelRef)` already round-trip a
ref's spelling and `Chain.parse` the comma-split + de-dup; this slice layers a
line-oriented rewriter on top so only the changed slot values are reformatted
and untouched slots stay identical to the byte.

The mutation vocabulary maps the decision engine's `PlannedMutation` kinds plus
a cold-start `INSERT` (the kind 3b deliberately deferred to the apply layer)
onto structural chain edits. Edits are **model-keyed** and, by default,
tier-agnostic (an evicted model leaves every chain); a caller may scope an edit
to one tier (3d-1b sets the tier for `INSERT` and may set it for the others).

## Goals
- G1: A new pure leaf `src/ferova/review/chain_rewrite.py` (no I/O, no DB,
  no network).
- G2: `EditOp` (`evict_model` / `drop_provider` / `demote` / `promote` /
  `insert`) and a frozen `ChainEdit(op, model, provider=None, tier=None,
  position=None, reason="")`.
- G3: `rewrite_chains(content, edits) -> RewriteResult(new_content,
  applied, skipped)` — applies the edits in order, re-renders only the slots
  that actually changed, returns the applied edits and a `SkippedEdit(edit,
  reason)` for each refused/no-op edit.
- G4: Safety invariants enforced mechanically (an edit that would breach one is
  skipped with a reason, never forced):
  - the `claude_code` backstop is never evicted, dropped or moved, and a new one
    is never inserted (an `insert` of any `claude_code` ref is refused — the
    safety layer trusts no caller, so it cannot rely on 3d-1b never emitting one);
  - a chain is never emptied;
  - `demote`/`promote` shift exactly one step and never move a ref past the
    backstop — **symmetrically** (both directions guard the backstop target);
  - `insert` adds above the backstop, de-duplicated, provider validated.
- G5: Faithful reporting — a tier-agnostic edit that changes one slot but is
  refused on another appears in BOTH `applied` and `skipped` (with the
  per-slot reason), so no refusal is silently dropped; an unknown tier names
  itself in the reason for every op, not just `insert`.

## Non-Goals
- NG1: Does NOT decide which edits to make (3b/3d-1b) nor read benchmarks,
  the matrix, the DB or the network.
- NG2: Does NOT write `chains.env` — returns a string; 3d-2 writes (flag-gated).
- NG3: Does NOT map `PlannedMutation` → `ChainEdit` (that translation, with the
  metric→role→tier scoping, is 3d-1b).
- NG4: Does NOT journal — 3d-1b records the run via the 3c audit log.

## Assumptions
- A1: Each slot holds at most one ref per model (true for the current
  `chains.env`); demote/promote act on the first ref matching the model.
- A2: The four `MODEL_*` lines are single `KEY=value` lines with no trailing
  inline comment (true for `chains.env`; comments are full-line).
- A3: `ModelRef`/`Chain` validation is the trusted parser; a slot that fails to
  parse is a broken file and surfaces loudly (caller's concern).

## Interface
New (all in `chain_rewrite.py`):
- `class EditOp(StrEnum)`.
- `@dataclass(frozen=True) class ChainEdit`.
- `@dataclass(frozen=True) class SkippedEdit`.
- `@dataclass(frozen=True) class RewriteResult`.
- `def rewrite_chains(content: str, edits: Sequence[ChainEdit]) -> RewriteResult`.

## Behavior

### Nominal
- `evict_model` for a model in OPUS + CODER removes it from both; the other two
  slots and all comments are untouched.
- `drop_provider(model, provider)` removes that one ref; the model's other
  provider refs stay.
- `demote`/`promote` swap the model's ref with its neighbour one step down/up.
- `insert(model, provider, tier, position)` adds the ref at `position`,
  de-duplicated, never below the backstop.
- No edits → `new_content == content` exactly.

### Edge cases
- An edit targeting the backstop, or one that would empty a chain → skipped with
  a reason; the file is unchanged for that slot.
- `demote` whose down-neighbour is the backstop, or `promote` at the head →
  skipped (`already at the boundary`).
- `insert` of an already-present ref, an unknown provider, or an unknown tier →
  skipped with a reason.
- A model absent from every targeted slot → skipped.

### Failure scenarios
- Pure in-memory; the only raise path is a malformed slot value at parse, which
  signals a broken `chains.env` (fail loud, not this slice's to mask).

## Architecture Impact
- New pure leaf in `review/` importing `Chain`/`ModelRef` from
  `llm_proxy.routing` (frontier/pre-template, so `depends_on: []`, as 1e). No
  new governed edge; `arch check` unchanged.
- Nobody imports it yet (3d-1b/3d-2 will), so per
  [[unwired-invariant-breaks-next-slice]] no "nothing imports me" assertion is
  pinned and the FULL unit suite is run.

## Acceptance Criteria
- [ ] AC1: `evict_model` removes every ref of the model from each targeted slot;
  untouched slots and all non-slot lines are byte-identical.
- [ ] AC2: The `claude_code` backstop is never removed or moved; such an edit is
  skipped with a reason.
- [ ] AC3: `drop_provider` removes exactly the `(provider, model)` ref.
- [ ] AC4: `demote`/`promote` shift one step; a boundary / backstop-adjacent move
  is skipped.
- [ ] AC5: `insert` adds the ref above the backstop, de-duplicated, with the
  provider validated; bad provider / tier / duplicate is skipped.
- [ ] AC6: An empty edit list yields `new_content == content`; only slots that
  actually changed are re-rendered.
- [ ] AC7: `RewriteResult.skipped` carries a reason for every refused edit,
  including the refused slot of a partially-applied tier-agnostic edit; an
  unknown tier reports `unknown tier` for any op.
- [ ] AC8: An `insert` of a `claude_code` ref is refused; `promote` refuses to
  cross a backstop (symmetric with `demote`).
- [ ] AC9: ruff + format + no-inline + `arch check` pass; full `pytest
  tests/unit` green; the module is pure (no I/O).

## Open Questions
- None for the mechanics. (The metric→role→tier scoping of `PlannedMutation`s
  and the semantic-anchor tier placement are 3d-1b.)
