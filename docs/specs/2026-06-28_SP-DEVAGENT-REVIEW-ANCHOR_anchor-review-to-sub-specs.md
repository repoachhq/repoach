---
id: SP-DEVAGENT-REVIEW-ANCHOR
title: Anchor the review to sub-specs when a decomposed parent was superseded
version: 0.1
status: draft
author: agent
created: 2026-06-28
updated: 2026-06-28

owns:
  code:
    - src/ferova/review/subspec_anchor.py
  resources: []

depends_on:
  - SP-DEVAGENT-DECOMPOSE
  - SP-DEVAGENT-WIRE

provides_to: []
constraints: {}
---

# SP-DEVAGENT-REVIEW-ANCHOR — review a decomposed PR against its sub-specs

## Intent
Closes the tradeoff left by SP-DEVAGENT-WIRE: when a governed parent spec is
decomposed into sub-specs, WIRE **deletes the parent spec file** (so the arch
disjointness gate stays green). The review orchestrator resolves a PR's spec from
its branch via `maybe_load_active_spec(branch)`, which now returns `None` for such a
PR (the parent file is gone) — so the four reviewers fall back to a **spec-unaware**
review even though the diff fully implements the governed sub-specs (which ARE in the
diff as new files).

This slice anchors the review to those sub-specs: when the branch's parent governed
spec is absent but its decomposition sub-specs exist, the orchestrator feeds the
reviewers the **concatenated sub-spec markdown** and the **per-sub-spec architecture
edges**, restoring a spec-aware review. The single-spec / frontier paths are
untouched.

## Context
The discovery is mechanical and reuses existing machinery: `detect_spec_from_branch`
recovers the parent id from `feat/sp-<parent>-impl`; `load_governed_spec` confirms the
parent is gone (`None`); the decompose convention names sub-specs `<PARENT-ID>-<N>`
(`SP-PARENT-1`, `SP-PARENT-2`, …), so the arch registry (`load_registry`) enumerates
them by id prefix + numeric suffix; `load_spec` loads each sub-spec's markdown and
`render_arch_edges(load_governed_spec(sub_id))` renders its edges. The reviewer
contract is unchanged — each reviewer already takes a single `spec_plan: str | None`
and a single `arch_edges: str`; anchoring simply concatenates N sub-specs into those
two strings, so no reviewer signature changes.

## Goals
- G1: A new owned module `review/subspec_anchor.py` exposing `AnchoredReview`
  (frozen dataclass: `parent_id`, `sub_spec_ids`, `spec_plan_md`, `arch_edges`),
  `discover_sub_specs(parent_id, *, root)`, `render_anchored_review_inputs(sub_specs,
  *, root)`, and the high-level `maybe_anchor_decomposed_parent(branch, *, root)`.
- G2: **Discovery** — `discover_sub_specs` enumerates the arch registry for ids
  matching `^<parent_id>-<digits>$`, loads each via `load_spec`, and returns them
  sorted by numeric suffix (the decomposition's natural order). Returns `[]` when none
  match (a non-decomposed missing parent → no anchoring).
- G3: **Anchoring gate** — `maybe_anchor_decomposed_parent` returns an
  `AnchoredReview` ONLY when (a) a parent id parses from the branch, (b) that parent's
  governed spec no longer loads (`load_governed_spec is None` — it was superseded), and
  (c) at least one sub-spec is discovered. Otherwise it returns `None` (the caller keeps
  today's behaviour). Never raises — any failure logs and returns `None`.
- G4: **Concatenation** — `render_anchored_review_inputs` returns
  `(spec_plan_md, arch_edges)`: the markdown is a brief header stating the PR implements
  a decomposed parent split into N governed sub-specs, followed by each sub-spec's
  markdown under a `## Sub-spec <id>: <title>` heading; the edges are the non-empty
  `render_arch_edges` blocks joined.
- G5: **Wire into the orchestrator** — in the spec-resolution block of `run_review`,
  when `maybe_load_active_spec` returns `None`, call
  `maybe_anchor_decomposed_parent(head_ref)`; on a hit set `spec_plan_md`,
  `spec_id` (the parent id, for logging/coverage), and `arch_edges_block` from the
  anchored inputs, and **skip** the single-spec `render_arch_edges` path (so it does not
  clobber the anchored edges with an empty block). Log `review_team.anchored_to_sub_specs`.

## Non-Goals
- NG1: No reviewer signature change (still one `spec_plan` + one `arch_edges` per
  reviewer); anchoring concatenates into those.
- NG2: No spec-coverage aggregation across sub-specs — `compute_spec_coverage`
  (`load_plan(parent_id)`) has no parent plan after decomposition and degrades to
  no-coverage exactly as today. Per-sub-spec coverage is a possible follow-up
  (Open Questions), not this slice.
- NG3: No change to decompose / WIRE / the agentic loop / self-verify.

## Interface
- `review.subspec_anchor.AnchoredReview` — frozen dataclass `parent_id: str,
  sub_spec_ids: tuple[str, ...], spec_plan_md: str, arch_edges: str`.
- `review.subspec_anchor.discover_sub_specs(parent_id: str, *, root: Path | None) ->
  list[SpecPlan]`.
- `review.subspec_anchor.render_anchored_review_inputs(sub_specs: Sequence[SpecPlan],
  *, root: Path | None) -> tuple[str, str]`.
- `review.subspec_anchor.maybe_anchor_decomposed_parent(branch: str, *,
  root: Path | None) -> AnchoredReview | None`.

## Behavior
- Single-spec / identity PR (parent file present) → `maybe_load_active_spec` returns the
  spec; anchoring never runs; review unchanged.
- Frontier / no-spec branch → parent id is `None` or no sub-specs → `None`; review
  spec-unaware exactly as today.
- Decomposed PR (parent superseded, sub-specs `SP-PARENT-1..N` on disk) → reviewers
  receive the concatenated sub-spec markdown + joined arch edges; `spec_id` logged as the
  parent id; `review_team.anchored_to_sub_specs` emitted.

## Architecture Impact
- Owns one new leaf module `review/subspec_anchor.py`. Import edges: `subspec_anchor`
  → `review.spec`, `review.governed_spec`, `arch` (registry). `depends_on:
  [SP-DEVAGENT-DECOMPOSE, SP-DEVAGENT-WIRE]`.
- Edit (wiring, not owned): `review/orchestrator.py` spec-resolution block gains the
  `maybe_anchor_decomposed_parent` fallback + an `anchored` guard around the single-spec
  arch-edges path.

## Acceptance Criteria
- [ ] AC1: `tests/unit/test_subspec_anchor.py` covers: discovery enumerates and
  numeric-sorts `SP-PARENT-1/-2/-10` (10 after 2), ignores unrelated `SP-PARENTX` and
  non-numeric `SP-PARENT-FOO`; `maybe_anchor_decomposed_parent` returns `None` when the
  parent still loads, `None` when no sub-specs exist, and an `AnchoredReview` (with both
  sub-spec markdowns + each sub-spec's arch edges) when the parent is gone but sub-specs
  remain; never raises on a malformed corpus.
- [ ] AC2: `tests/unit/test_review_team.py` (or the orchestrator test module) asserts a
  decomposed-parent PR feeds each reviewer the concatenated sub-spec `spec_plan` + joined
  `arch_edges`, while a normal single-spec PR is unchanged.
- [ ] AC3: ruff + format + no-inline + no-silent-except + `arch check` (edge-honesty +
  disjointness) + full `pytest tests/unit` green under 3.11 and 3.13.

## Open Questions
- Spec coverage for a decomposed PR is not aggregated (NG2); if the merge gate's
  coverage signal proves valuable on multi-sub-spec PRs, a follow-up can sum per-sub-spec
  `compute_spec_coverage`.
- The discovery heuristic keys on the `<PARENT-ID>-<N>` id convention; if decomposition
  ever adopts non-numeric sub-spec ids, discovery must widen accordingly.
