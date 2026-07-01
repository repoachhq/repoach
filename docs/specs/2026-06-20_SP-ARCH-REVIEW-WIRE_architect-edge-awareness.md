---
id: SP-ARCH-REVIEW-WIRE
title: Architect edge-awareness — wire declared edges into review
version: 0.1
status: approved
author: agent
created: 2026-06-20
updated: 2026-06-20

owns:
  code: [src/ferova/review/governed_spec.py]   # new accessor; review/ is otherwise frontier
  resources: N/A

depends_on: [SP-ARCH-GRAPH]           # imports load_registry to read a spec's owns/depends_on
provides_to: []                       # AUTO-maintained

constraints: {}
---

# SP-ARCH-REVIEW-WIRE — Architect edge-awareness

## Intent
Close the conceptual loop the spec-architecture arc opened: the
edge-honesty gate enforces tier-1 couplings (imports + table literals) in
CI, while the design leaves tier-2 (runtime topics, raw SQL, dynamic
imports) "to Architect review". But the Architect cannot judge an
*undeclared* coupling without knowing what IS declared. This slice feeds
the active spec's declared `depends_on` (+ `owns`) into the Architect
reviewer so tier-2 review is actually possible.

## Context
A new leaf `review/governed_spec.py` reads one spec's frontmatter by
reusing the `SP-ARCH-GRAPH` deriver (`load_registry`) — no second YAML
parser. The orchestrator, which already loads the active spec by branch,
looks the node up and threads its declared edges into the Architect's
rendered prompt via a new `{ARCH_EDGES}` placeholder. Legacy/frontier
specs (no frontmatter) yield empty edges, so the Architect falls back to
its current behaviour — fully backward-compatible.

## Goals
- G1: `load_governed_spec(spec_id) -> GovernedSpec | None` — returns the
  parsed `id` / `owns_code` / `owns_resources` / `depends_on` for a
  governed spec, or `None` for a frontier/unknown one. Reuses
  `arch.load_registry` (single source).
- G2: An `architecture` block (declared `depends_on` + `owns` + the
  tier-1-enforced / tier-2-reviewed split) rendered into the Architect
  prompt via `{ARCH_EDGES}`; empty string when the spec is frontier.
- G3: The Architect persona instructs: tier-1 is gate-enforced (do not
  re-flag it); YOUR job is tier-2 — flag a coupling the diff introduces
  (a queue topic, raw SQL table, dynamic import, cross-component call)
  that is NOT in the declared `depends_on`.
- G4: Backward-compatible — a frontier spec or a no-spec PR renders an
  empty `{ARCH_EDGES}` and the Architect behaves exactly as today.

## Non-Goals
- NG1: Does NOT re-enforce tier-1 — the CI gate already does. The
  Architect must not duplicate import/table-literal findings.
- NG2: Does NOT prime the Planner/Developer with `owns` boundaries — that
  is **slice B** (`SP-ARCH-DEV-WIRE`), which reuses this slice's
  `GovernedSpec` accessor.
- NG3: Does NOT touch SP-SPEC-GATE — the governed-frontmatter presence
  fact + merge-gate arch-check evidence are **slice C**
  (`SP-ARCH-GATE-WIRE`).
- NG4: Does NOT change `load_spec` / `SpecPlan` — `GovernedSpec` is an
  additive sibling, not a replacement.

This is **slice A of the pipeline-wiring arc** (operator chose full
coverage, delivered sliced not monolithic): A = Architect + the shared
`GovernedSpec` accessor (this spec); B = Planner/Developer owns priming;
C = SP-SPEC-GATE presence + merge-gate evidence. B and C build on A's
accessor.

## Assumptions
- A1: `SP-ARCH-GRAPH` exposes `load_registry` + `Registry.nodes`.
- A2: The orchestrator already resolves the active spec id from the PR
  head branch (`maybe_load_active_spec`).
- A3: Editing `prompts/review/architect_*.md` is hand-shipped (path
  whitelist forbids bot edits to `prompts/review/*`).

## Interface
Importable core (`review/governed_spec.py`):

Inputs:
- `spec_id`: `str` — any spelling; normalised like `load_spec`.
- `root`: `Path | None` — repo root override (tests).

Outputs:
- `GovernedSpec` — frozen: `id`, `owns_code: tuple[str,...]`,
  `owns_resources: tuple[str,...]`, `depends_on: tuple[str,...]`.
- `load_governed_spec(spec_id, *, root=None) -> GovernedSpec | None`.
- `render_arch_edges(spec: GovernedSpec | None) -> str` — the
  `{ARCH_EDGES}` block; `""` when `None` or no declared edges.

Errors:
- Propagates `MalformedFrontmatterError` from the deriver (a malformed
  governed spec should fail loudly, consistent with the gate).

## Behavior

### Nominal
Orchestrator resolves spec id → `load_governed_spec(id)` → if a governed
node exists, `render_arch_edges` builds the block → substituted for
`{ARCH_EDGES}` in the Architect prompt alongside the existing
`{SPEC_PLAN}`. The Architect reasons about tier-2 couplings against the
declared edges.

### Edge cases
- frontier/legacy spec (no frontmatter) ⇒ `load_governed_spec` returns
  `None` ⇒ `render_arch_edges(None)` == `""` ⇒ Architect unchanged.
- governed spec with empty `depends_on` ⇒ block states "no declared
  dependencies; flag ANY cross-component coupling".
- no active spec on the PR ⇒ empty block.

### Failure scenarios
- malformed frontmatter on the active spec ⇒ `MalformedFrontmatterError`
  surfaces (loud), matching the CI gate's stance.

## Architecture Impact
- Adds `src/ferova/review/governed_spec.py` — a new governed leaf
  inside the otherwise-frontier `review/` package (file-granular owns;
  disjoint, no nesting conflict).
- Adds dependency: `SP-ARCH-REVIEW-WIRE -> SP-ARCH-GRAPH` — imports
  `load_registry` to read a spec's declared edges (tier-1a code edge,
  gate-enforced on this file).
- Threads through frontier files (`orchestrator.py`, `reviewer.py`) and
  the Architect persona — frontier edits, not gate-enforced, by design.
- New coupling / cycles / shared state: none (review depends on arch; arch
  never imports review — acyclic).

## Diagram
```mermaid
flowchart TD
    A[PR head branch] --> B[active spec id]
    B --> C[load_governed_spec]
    C --> D{governed?}
    D -->|yes| E[render_arch_edges]
    D -->|frontier| F[empty block]
    E --> G[Architect prompt {ARCH_EDGES}]
    F --> G
```

## Acceptance Criteria
- [ ] AC1: `load_governed_spec` returns a `GovernedSpec` with the declared
  `owns`/`depends_on` for a governed fixture spec, and `None` for a
  frontier (frontmatter-less) one.
- [ ] AC2: `render_arch_edges` emits a block naming the declared
  `depends_on` for a governed spec, and `""` for `None` / no edges.
- [ ] AC3: the Architect prompt file carries the `{ARCH_EDGES}` placeholder
  and the tier-1-enforced / tier-2-reviewed instruction; the renderer
  substitutes it (empty for a frontier/no spec PR).
- [ ] AC4: a malformed governed spec propagates `MalformedFrontmatterError`.
- [ ] AC5: the existing review suites stay green (backward compatible —
  no-spec / frontier-spec PRs render an empty block).
- [ ] AC6: full `pytest tests/unit` green; ruff + format + no-inline +
  no-silent clean; `ferova arch check` passes (self-application: the
  new file's `SP-ARCH-GRAPH` import is declared).

## Open Questions
- None. (Resolved while drafting: reuse `load_registry` not a new parser;
  `GovernedSpec` is additive beside `SpecPlan`; Planner/Developer priming
  and SP-SPEC-GATE frontmatter-presence are out of scope, NG2/NG3.)
