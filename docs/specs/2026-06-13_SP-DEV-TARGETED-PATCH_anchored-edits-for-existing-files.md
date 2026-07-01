# SP-DEV-TARGETED-PATCH — the Developer edits existing files with anchored patches

## Metadata

- **Status**: OPEN
- **Priority**: P0 — the big-file full-rewrite class stalled step 2 of
  two consecutive redesign slices (reviewer.py 1 718 lines, then
  orchestrator.py 1 235 lines)
- **Owner**: operator
- **Executor**: hand-implemented (touches `prompts/review/*` — bot
  whitelist forbids it; force-majeure per convention)
- **Opened**: 2026-06-13

## Why

The fix protocol is full-file re-emission. Above ~1 000 existing
lines, chain models reliably fail it: unparseable mega-outputs on the
first attempt, 40-60% shrunk files on the retry (correctly rejected by
the size guard — contained, but stalled). Every future slice touching
the orchestrator, the reviewer bench or the coder loop hits this wall.
Anchored search/replace edits flip the contract to what these models
ARE reliable at: copying literal snippets. No line numbers, no diff
hunks, deterministic application, directive failures.

## What

1. **New module `src/ferova/review/patch_apply.py`** —
   `apply_search_replace_edits(content, edits) -> (new_content | None,
   report)`: ordered, exact-match, each `search` must occur exactly
   once; failures report the closest existing line (absent anchor) or
   the match count (ambiguous anchor).
2. **`reviewer.py`** — `_normalise_fixes` accepts the second shape
   `{"path", "edits": [{"search", "replace"}], "rationale"}` (helper
   `_normalise_edits`); Developer persona bumped to
   `developer_0.2.0.md`.
3. **`coder_loop.py`** — `apply_fixes` materialises `edits` fixes via
   `_materialise_edits` (escape check, target must exist, apply, then
   the EXISTING pipeline: whitelist, placeholder guard on the result,
   write); new `edit_failures_out` parameter collects directive
   reports.
4. **`dev_runner.py`** — passes `edit_failures_out` and feeds the
   reports into the retry brief when nothing applied.
5. **`prompts/review/developer_0.2.0.md`** (hand-ship) — schema and
   rules: existing files via `edits` (verbatim unique anchors), new
   files via `new_content`; full re-emission of an existing file is
   called out as a size-guard rejection.

## Files in scope

- `src/ferova/review/patch_apply.py` (new)
- `src/ferova/review/reviewer.py`
- `src/ferova/review/coder_loop.py`
- `src/ferova/review/dev_runner.py`
- `prompts/review/developer_0.2.0.md` (new, from 0.1.0)
- `tests/unit/test_patch_apply.py` (new)

## Out of scope

- The Coder personas (fix loop keeps full-file; revisit when the
  class bites there).
- Whitespace-tolerant or fuzzy anchor matching (exact-first; loosen
  only with evidence).
- Deleting files via edits.

## Smoke scenario

### Setup

A tmp repo with a 2-line module.

### Execute

`apply_fixes` with an `edits` fix changing one line; then with an
absent anchor; then `apply_search_replace_edits` with an ambiguous
anchor.

### Expected

First: file updated, 1 applied. Second: rejected,
`edit_failures_out` carries "not found" + the closest line. Third:
`(None, report)` naming the match count and asking for a unique
anchor.

## Definition of Done

- Apply semantics + directive failures pinned —
  `test_single_edit_applies`, `test_edits_apply_in_order`,
  `test_anchor_not_found_is_directive`,
  `test_ambiguous_anchor_reports_count`,
  `test_invalid_edit_shapes_rejected`.
- Normaliser accepts the new shape, drops malformed ones —
  `test_normalise_fixes_accepts_edits_shape`.
- `apply_fixes` integration: materialise + write, missing target,
  failed anchor surfaced — `test_apply_fixes_materialises_edits`,
  `test_apply_fixes_edits_on_missing_file_rejected`,
  `test_apply_fixes_failed_anchor_surfaces_report`.
- `ruff` + `ruff format --check` + `pytest tests/unit` green; zero
  inline comments; no `# noqa`.

## Commit plan

1. `feat(review): anchored search/replace edits — module, normaliser, apply path`
2. `feat(prompts): developer 0.2.0 — edits for existing files, new_content for new`
3. `test(review): targeted-patch apply semantics and integration`

## Risks

- **Model anchors drift from file reality**: the directive reports
  (closest line, match count) are designed to converge the retry; the
  promised-tests and full-suite gates stand behind.
- **Both shapes coexist**: a model may still full-rewrite an existing
  file; the size guard keeps rejecting that, and the prompt says why.
