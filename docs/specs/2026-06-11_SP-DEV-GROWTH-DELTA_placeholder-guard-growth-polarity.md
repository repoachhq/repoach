# SP-DEV-GROWTH-DELTA — the placeholder size guard must not punish growth in build context

## Metadata

- **Status**: OPEN
- **Priority**: P1 (third dispatch-killer class found dogfooding
  SP-DEV-PROMISE-RECONCILE)
- **Owner**: operator
- **Executor**: hand-implemented (the fix touches `coder_loop.py`,
  2 448 lines — a factory full-file rewrite of the repo's biggest
  module would trip the truncation class; genuinely circular)
- **Opened**: 2026-06-11

## Why

`is_placeholder_content`'s `excessive_size_delta` rule uses a
**symmetric** delta: `abs(new_lines - existing_lines) / existing_lines
> 40%` rejects the fix. For the Coder loop (reviewing fixes to mature
files) a rewrite cap is defensible; for the Developer build context it
is structurally wrong — a plan step that extends a young file is what
building *is*. Observed live (SP-DEV-PROMISE-RECONCILE round 2,
2026-06-11): step 2 legitimately appended one e2e test to the 63-line
test file created by step 1 → `63→144 lines (delta 129%, cap 40%)` →
rejected as a placeholder twice → stall. The placeholder failure mode
the guard exists for (a file replaced by a stub) is a *shrinkage*
signal; growth is not evidence of a placeholder.

## What

In `src/ferova/review/coder_loop.py`:

1. `is_placeholder_content` gains keyword-only
   `allow_growth: bool = False`. When `True`, the
   `excessive_size_delta` layer fires only when `new_lines <
   existing_lines` (shrinkage beyond the cap); growth is unlimited.
   The `massive_shrinkage` layer and all other layers are unchanged
   in both modes.
2. `apply_fixes` gains keyword-only `allow_growth: bool = False`,
   passed through to `is_placeholder_content`.

In `src/ferova/review/dev_runner.py`:

3. Both `apply_fixes` call sites (plan-step execution and the legacy
   session path) pass `allow_growth=True` — the Developer builds,
   it does not patch.

The Coder loop's behaviour is byte-for-byte unchanged (defaults).

## Files in scope

- `src/ferova/review/coder_loop.py`
- `src/ferova/review/dev_runner.py`
- `tests/unit/test_dev_growth_delta.py` (new)

## Out of scope

- Re-tuning `_SIZE_GUARD_MAX_DELTA` for the Coder loop.
- The other placeholder layers (sentinel, single-comment,
  test-file-no-tests).

## Smoke scenario

### Setup

A tmp repo with a committed 63-line Python file.

### Execute

Call `is_placeholder_content` on a 144-line replacement with and
without `allow_growth=True`; then on a 2-line replacement in both
modes.

### Expected

Growth: rejected by default, accepted with `allow_growth=True`.
Shrinkage: rejected in both modes.

## Definition of Done

- Growth past the cap accepted only with `allow_growth=True` —
  `test_growth_allowed_in_build_context`,
  `test_growth_still_rejected_by_default`.
- Shrinkage past the cap rejected in BOTH modes —
  `test_shrinkage_rejected_regardless_of_growth_flag`.
- `massive_shrinkage` unchanged in both modes —
  `test_massive_shrinkage_unchanged`.
- `apply_fixes` passes the flag through —
  `test_apply_fixes_forwards_allow_growth`.
- `ruff` + `ruff format --check` + `pytest tests/unit` green; zero
  inline comments; no `# noqa`.

## Commit plan

1. `fix(review): placeholder size guard punishes only shrinkage when allow_growth`
2. `feat(dev): dev_runner applies fixes with allow_growth=True`
3. `test(dev): growth-delta polarity in build vs fix contexts`

## Risks

- **A real placeholder that grows**: sentinel-string and
  test-file-no-tests layers still fire on stub content regardless of
  size; the ruff + promised-tests + full-suite gates stand behind.
