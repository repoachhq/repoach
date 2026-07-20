---
id: SP-DEVAGENT-DECOMPOSE
title: Always-on governed sub-spec decomposition front-end (DEVAGENT slice 4)
version: 0.1
status: draft
author: agent
created: 2026-06-28
updated: 2026-06-28

owns:
  code:
    - src/repoach/review/decompose.py
  resources:
    - prompts/review/decomposer_0.1.0.md

depends_on:
  - SP-DEVAGENT-LOOP
  - SP-ARCH-GRAPH

provides_to: []
constraints: {}
---

# SP-DEVAGENT-DECOMPOSE — split a spec into a governed partition, then build each part

## Intent
Slice 4 of the real-coding-agent arc (umbrella `docs/devagent_architecture.md`).
A governed parent spec is decomposed into **ordered governed sub-specs** whose
`owns` form a **partition** of the parent's `owns` (pairwise disjoint, and together
covering the parent), with an acyclic `depends_on` ordering that honours the arch
edge model. The Developer session then implements each sub-spec in dependency
order through the existing pipeline (Planner → agentic loop [slice 2] → self-verify
[slice 3]), pushing once at the end.

Per the operator's calibration: **always decompose** (uniform pipeline) — but a
small spec yields a **single sub-spec equal to the parent**, in which case the
session behaves **byte-identically to today** (no sub-spec files, one plan, one
self-verify, one push). The multi-sub-spec path is the added capability for large
specs; the common single-sub-spec path carries zero new risk.

## Context
The governance machinery exists and is reused, not rebuilt: `GovernedSpec` +
`load_governed_spec` (frontmatter), `arch.registry` (`disjointness_violations`,
`owner_of`, the `owns: {code, resources}` model), `arch.graph.Graph.cycles`
(acyclicity), and the proven proposer pattern (an LLM emits a JSON structure, a
mechanical validator accepts/retries — exactly how the Planner works). The Planner
itself does NOT change: it still plans one spec into steps; decomposition is the
*spec-level* front-end that runs before it.

The decomposition proposal is semantic (which sub-features, how to split the
owns) → an LLM proposer (OPUS tier, healthy; not the coder tier). The validation —
partition coverage, disjointness, acyclicity, edge-honesty — is mechanical and
deterministic, with a bounded retry feeding the validator's complaint back to the
proposer (mirroring `SP-PLANNER-PLAN-RETRY`).

## Goals
- G1: A new owned module `review/decompose.py` exposing `SubSpec`,
  `DecomposeResult`, `Decomposer` (the proposer callable type),
  `make_decomposer()`, `decompose_spec(spec, governed, *, proposer, repo_root)`,
  and `render_sub_spec_markdown(sub_spec)`.
- G2: **Identity passthrough** — when the parent owns ≤1 code path (a small spec),
  `decompose_spec` returns a single `SubSpec` equal to the parent without calling
  the proposer (no LLM, no new files).
- G3: **Proposer + validation** — for a larger parent, the proposer emits ordered
  sub-specs; the validator enforces: (a) every sub-spec's `owns` ⊆ the parent's
  `owns`; (b) the sub-specs' `owns` are pairwise disjoint (reusing the
  arch prefix-overlap logic) and together cover the parent's `owns` exactly; (c)
  each sub-spec's `depends_on` references only the parent's `depends_on` ∪ sibling
  sub-spec ids; (d) the inter-sub-spec graph is acyclic (`Graph.cycles`), yielding a
  dependency order. A validation failure retries the proposer with the complaint,
  up to a bounded cap; exhaustion returns a loud error result (never raises).
- G4: **Sub-spec persistence** — multi-sub-spec runs render each `SubSpec` to a
  governed `docs/specs/<date>_<SUB-ID>_<slug>.md` (frontmatter the arch registry
  can load + `arch graph --check`) and commit them, so each sub-spec is a
  first-class, arch-checkable, loadable spec.
- G5: **Wire into `run_developer_session`** — extract the current single-spec body
  (plan → step loop → full suite → integration → self-verify) into
  `_develop_one_spec`, then: decompose the parent → for each sub-spec in dependency
  order, persist+commit it (when not the identity passthrough) and run
  `_develop_one_spec`; on any sub-spec failure stop loudly; push once after all
  sub-specs verify. The single-sub-spec path calls `_develop_one_spec(parent)` with
  no sub-spec files — identical to today.

## Non-Goals
- NG1: No change to the Planner, the agentic loop (slice 2), or the self-verify
  gate's interface (slice 3) — `_develop_one_spec` calls `run_self_verify(sub_spec,
  sub_plan)` per sub-spec; the judge sees the cumulative branch diff (acceptable —
  see Open Questions).
- NG2: No removal of the remaining `revert_working_tree` callers or CLI/flag polish
  (slice 5 SP-DEVAGENT-WIRE).
- NG3: The multi-sub-spec path needs the Planner per sub-spec (coder tier) and the
  proposer (OPUS); it is built + unit-tested with injected fakes but not exercised
  live in this slice while the coder tier is degraded.

## Interface
- `review.decompose.SubSpec` — dataclass `id, title, summary, owns_code, owns_resources,
  depends_on, body` (frozen).
- `review.decompose.DecomposeResult` — dataclass `sub_specs: list[SubSpec],
  identity: bool, error: str | None`.
- `review.decompose.Decomposer = Callable[[str], str]` (prompt → raw JSON reply).
- `review.decompose.make_decomposer() -> Decomposer` (OPUS one-shot).
- `review.decompose.decompose_spec(spec: SpecPlan, governed: GovernedSpec, *,
  proposer: Decomposer | None, repo_root: Path) -> DecomposeResult`.
- `review.decompose.render_sub_spec_markdown(sub_spec: SubSpec) -> str`.

## Behavior
- Parent owns ≤1 code path → `DecomposeResult(sub_specs=[parent-as-subspec],
  identity=True)`, proposer not called.
- Larger parent, valid proposal → ordered sub-specs partitioning the parent's owns,
  `identity=False`.
- Proposal with an owns gap, overlap, out-of-parent path, undeclared edge, or cycle
  → retried with the specific complaint; persistent failure → `error` set, the
  session stops without developing (no partial sub-spec files left uncommitted).
- `run_developer_session` on a single-sub-spec spec → unchanged end-to-end
  behaviour; on a multi-sub-spec spec → one commit-group per sub-spec, a single
  push after the last sub-spec self-verifies.

## Architecture Impact
- Owns one new leaf module + one persona. Import edges: `decompose` →
  `agent_engine.agent_loop`, `llm.capability`, `review.spec`, `review.governed_spec`,
  `arch.registry`, `arch.graph`. `depends_on: [SP-DEVAGENT-LOOP, SP-ARCH-GRAPH]`.
- Edit (wiring): `dev_runner.run_developer_session` is refactored around
  `_develop_one_spec` + the decomposition loop.

## Acceptance Criteria
- [ ] AC1: `tests/unit/test_decompose.py` covers: identity passthrough (≤1 owns path,
  proposer not called); a valid multi-sub-spec proposal accepted + ordered; rejection
  of an owns gap, an owns overlap, an out-of-parent path, an undeclared edge, and a
  cycle; retry-then-success; persistent-failure error result; `render_sub_spec_markdown`
  round-trips through `load_governed_spec`.
- [ ] AC2: `tests/unit/test_review_plan_executor.py` asserts the single-sub-spec path
  is unchanged (identity decompose → one plan → one self-verify → push) and a
  multi-sub-spec run (injected fake proposer + fake Planner + fake judge) develops each
  sub-spec in order and pushes once.
- [ ] AC3: ruff + format + no-inline + no-silent-except + `arch check` (edge-honesty)
  + full `pytest tests/unit` green under 3.11 and 3.13.

## Open Questions
- Per-sub-spec self-verify judges against the cumulative branch diff (it includes
  earlier sub-specs). If this dilutes the verdict, a later slice can scope the diff to
  the sub-spec's own commits.
- The "small spec" heuristic (≤1 owns code path) is deliberately conservative; it can
  be tuned (e.g. an estimated-LOC threshold) once the multi-sub-spec path runs live.
- **Parent/sub-spec ownership overlap (deferred to SP-DEVAGENT-WIRE).** Writing the
  sub-specs leaves the parent still owning the partitioned paths, so `arch graph
  --check` would flag a disjointness conflict on a real multi-way decomposition. The
  multi path is unarmed (NG3) and a loud `decompose.parent_owns_overlap` warning is
  logged; superseding the parent's ownership (empty its `owns` / mark
  `status: superseded`) is a lifecycle step the WIRE slice owns. Sub-spec ids are
  validated `^SP-[A-Z0-9-]+$` so a proposer id can never traverse out of `docs/specs`.
