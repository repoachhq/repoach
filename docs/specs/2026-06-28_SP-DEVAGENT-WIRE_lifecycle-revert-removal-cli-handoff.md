---
id: SP-DEVAGENT-WIRE
title: Wire the real-coding-agent — parent supersession, revert removal, CLI handoff (DEVAGENT slice 5)
version: 0.1
status: draft
author: agent
created: 2026-06-28
updated: 2026-06-28

owns:
  code:
    - src/repoach/review/spec_supersede.py
  resources: []

depends_on:
  - SP-DEVAGENT-DECOMPOSE

provides_to: []
constraints: {}
---

# SP-DEVAGENT-WIRE — close the real-coding-agent loop

## Intent
The final slice of the real-coding-agent arc (umbrella `docs/devagent_architecture.md`).
The four capability gaps are filled (TOOLS #460, LOOP #461, SELFVERIFY #462,
DECOMPOSE #463); this slice **wires them into a clean, armed, lifecycle-correct
whole** and removes the last destructive footgun. Three lifecycle jobs plus a CLI
handoff polish, each a deliberate operator-calibrated decision (2026-06-28):

1. **Supersede the parent on a real decomposition** by **deleting the parent spec
   file** (`git rm`) in the sub-spec commit, so the arch disjointness gate stays
   green (only the sub-specs own the partitioned paths). The DECOMPOSE Open Question
   is closed.
2. **Remove the remaining destructive `revert_working_tree`** entirely (fix-forward
   everywhere): the two callers in `coder_findings.py` stop reverting, and the now
   dead `revert_working_tree` / `_REVERT_CLEAN_ROOTS` are deleted from `coder_loop.py`.
3. **Arm the multi-sub-spec path by default** — once supersession keeps arch check
   green, a governed multi-owns spec decomposes live with no flag or guard. (NG3 from
   DECOMPOSE is retired.)
4. **CLI / handoff polish** — surface the self-verify + decomposition outcome in the
   `ferova develop` payload, make the PR handoff resilient to a deleted parent,
   and correct the stale "working tree reverted" exit-code documentation.

## Context
The machinery is all present and reused, not rebuilt: `_resolve_sub_specs` already
decomposes, renders, stages, and commits the sub-spec files (`dev_runner.py:800`);
this slice adds one lifecycle step (delete the parent in that same commit) extracted
into a small owned helper. `revert_working_tree` lives in `coder_loop.py:690` with two
callers in `coder_findings.run_coder_fix_from_findings` (the review-loop Coder, after a
red ruff/pytest gate); the agentic step loop already dropped it (slice 2). The review
side already resolves the spec best-effort via `maybe_load_active_spec(branch=…)`
(`orchestrator.py:234`), which returns `None` when the parent is gone — so a decomposed
PR degrades to a spec-unaware review rather than crashing (see Open Questions). The
`ferova develop` CLI (`review_cmds.py:365`) re-loads the parent spec **after** the
session to open the PR (`load_spec(result.spec_id)` at line 461) — this breaks once the
parent is deleted, hence the handoff polish.

## Goals
- G1: A new owned module `review/spec_supersede.py` exposing
  `supersede_parent_on_decompose(repo_root, parent_spec, *, staged_subspecs) -> str`
  (returns `""` on success or a short error). It removes the parent spec file from the
  index and working tree (`git rm --`) so the arch registry no longer sees the parent
  co-owning the partitioned paths. Self-contained (stdlib `subprocess` git invocation,
  no new cross-component import edge); jailed to `docs/specs/` under `repo_root`; never
  raises.
- G2: **Wire supersession into `_resolve_sub_specs`** — on a non-identity decomposition,
  after the sub-spec files are written and staged, supersede (delete) the parent in the
  **same commit** as the sub-specs. The commit message becomes
  `docs(decompose): split <parent> into <N> sub-specs (supersedes <parent>)`. The
  `dev_runner.parent_owns_overlap` warning and the "(slice 5)" deferral comment are
  removed — the overlap no longer exists. The identity passthrough is untouched
  (parent kept, single-spec path byte-identical to today).
- G3: **Remove `revert_working_tree`** — delete both calls in
  `coder_findings.run_coder_fix_from_findings` (ruff-red and pytest-red branches now
  return their no-op result without reverting; the `no_op_reason` drops the "reverted"
  wording), drop the now-unused import, and delete the dead `revert_working_tree` +
  `_REVERT_CLEAN_ROOTS` from `coder_loop.py`. Delete `tests/unit/test_revert_working_tree_scope.py`
  and remove the three `revert_working_tree` monkeypatches from `test_coder_findings.py`
  (those paths now exercise the real no-revert behaviour).
- G4: **Arm the multi path** — no new guard; the default-on multi-sub-spec path runs
  for any governed multi-owns spec. Retire the NG3 framing from the code comments. The
  arch gate stays green because of G1/G2.
- G5: **CLI / handoff polish** — `DevSessionResult` carries `decomposed: bool`,
  `sub_spec_ids: list[str]`, `pr_title: str`, `pr_summary: str` (the parent's title /
  summary captured **before** deletion). `run_developer_session` populates them.
  `ferova develop` (a) builds the PR from those fields instead of re-loading the
  deleted parent, (b) adds `self_verified`, `steps_total`, `steps_completed`,
  `decomposed`, `sub_spec_ids` to the JSON payload, (c) corrects the exit-code docstring
  (no "working tree reverted"), and (d) maps a self-verify-gate failure and a decompose
  failure to distinct exit codes.

## Non-Goals
- NG1: No change to the agentic loop (slice 2), the self-verify gate's interface
  (slice 3), or the decomposition proposer/validator (slice 4).
- NG2: No review-side sub-spec anchoring — a decomposed PR reviews spec-unaware (the
  parent is gone; the sub-specs are in the diff). Anchoring reviewers to the per-sub-spec
  governed frontmatter is a possible follow-up (see Open Questions), not this slice.
- NG3: No change to the arch registry / `arch check` logic — supersession works by
  **removing** the parent node (deleting its file), so the existing disjointness check
  needs no `status: superseded` awareness.

## Interface
- `review.spec_supersede.supersede_parent_on_decompose(repo_root: Path, parent_spec:
  SpecPlan, *, staged_subspecs: Sequence[Path]) -> str` — `git rm --` the parent spec
  file (validated to live under `docs/specs/` within `repo_root`); returns `""` on
  success or a short error string; never raises. `staged_subspecs` is accepted for the
  guard that at least one sub-spec replaces the parent before it is removed.
- `dev_runner.DevSessionResult` — gains `decomposed`, `sub_spec_ids`, `pr_title`,
  `pr_summary`.
- `dev_runner._resolve_sub_specs` — unchanged signature; the multi branch deletes the
  parent within the sub-spec commit.
- `dev_runner.open_pr` — sources title/summary from supplied values (resilient to a
  missing parent); the CLI passes the captured `DevSessionResult` PR fields.

## Behavior
- Identity / single-spec run → byte-identical to today: parent kept, one plan, one
  self-verify, one push; `decomposed=False`, `sub_spec_ids=[]`.
- Real multi-owns governed spec → decompose → write + stage sub-specs → **delete the
  parent** → one commit for the lot → develop each sub-spec in dependency order →
  self-verify each → single push. `arch check` is green (parent node gone). The PR is
  opened from `pr_title`/`pr_summary`; the review team loads no active spec (parent
  deleted) and reviews the diff (sub-specs included) spec-unaware.
- `coder_findings` red ruff/pytest after applying fixes → returns its no-op result with
  the broken fixes left uncommitted on disk (no destructive reset; nothing reaches the
  branch).

## Architecture Impact
- Owns one new leaf module `review/spec_supersede.py`. Edge: `dev_runner` →
  `spec_supersede` (a new intra-`review` import). `depends_on: [SP-DEVAGENT-DECOMPOSE]`.
- Edits (not owned, no new ownership): `dev_runner.py` (DevSessionResult fields,
  `_resolve_sub_specs` supersession wiring, `run_developer_session` PR-field capture,
  `open_pr` resilience), `coder_findings.py` (revert removal), `coder_loop.py` (dead-code
  removal), `cli/review_cmds.py` (payload + exit codes + docstring), and the two test
  edits/deletions in G3.

## Acceptance Criteria
- [ ] AC1: `tests/unit/test_spec_supersede.py` covers: the parent file is removed from
  index + tree; a parent path that escapes `docs/specs/` is refused (returns error, file
  untouched); a git failure returns an error string without raising; the empty
  `staged_subspecs` guard refuses to delete.
- [ ] AC2: `tests/unit/test_review_plan_executor.py` asserts: identity decompose →
  parent kept, `decomposed=False`; a multi-sub-spec run (injected fake proposer + fake
  Planner + fake judge) deletes the parent, commits sub-specs + the deletion together,
  develops each sub-spec, pushes once, and sets `decomposed=True` + `sub_spec_ids`; after
  the run `arch check` (registry over the temp `docs/specs/`) reports **zero** ownership
  conflicts.
- [ ] AC3: `tests/unit/test_coder_findings.py` asserts a red ruff and a red pytest gate
  after applied fixes return a no-op result **without** reverting (the applied changes
  remain on disk), with no "reverted" wording; `revert_working_tree` no longer importable
  from `coder_loop`.
- [ ] AC4: CLI test asserts `ferova develop` opens the PR for a decomposed run
  without re-loading the parent, and the payload carries `self_verified`, `steps_total`,
  `steps_completed`, `decomposed`, `sub_spec_ids`.
- [ ] AC5: ruff + format + no-inline + no-silent-except + `arch check` (edge-honesty,
  disjointness) + full `pytest tests/unit` green under 3.11 and 3.13.

## Open Questions
- A decomposed PR currently reviews spec-unaware (parent deleted, no single active
  spec). If the reviewers' spec-awareness proves valuable on a real multi-way split, a
  follow-up can anchor each reviewer to the relevant sub-spec frontmatter (the sub-specs
  are first-class governed specs in the diff).
- Per-sub-spec self-verify still judges against the cumulative branch diff (carried from
  slice 4's Open Question); unchanged here.
