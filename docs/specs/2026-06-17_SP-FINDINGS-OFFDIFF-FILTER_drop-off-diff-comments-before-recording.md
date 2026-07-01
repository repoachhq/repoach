# SP-FINDINGS-OFFDIFF-FILTER — off-diff reviewer comments never become findings

## Metadata

- **Status**: OPEN
- **Priority**: P1 — trigger-flip prerequisite **P1 of 3** (the
  findings-driven Coder must absorb the legacy arbiter's off-diff filter
  before CI flips onto it; see `project_review_redesign.md` 2026-06-17
  trigger-flip parity)
- **Owner**: operator
- **Executor**: `ferova develop`
- **Opened**: 2026-06-17

## Why

The legacy verdict-driven Coder (`run_coder_fix`) runs
`arbiter_filter_reviews` before acting, which **drops every reviewer
comment whose cited file is not in the PR diff** (`coder_loop.py:1022-1026`
+ `_files_in_diff`). The findings-driven Coder
(`run_coder_fix_from_findings`) does NOT — `record_findings_for_outcomes`
records a finding for *every* comment of every parsed outcome, with no
diff awareness.

That gap is harmless today (CI still runs the legacy path) but becomes a
real defect the moment the trigger flips onto the findings path: a
reviewer comment citing an **existing-but-untouched** repo file — the
routine Architect/Sentinel hallucination the arbiter was built to
suppress — is recorded as a finding, then refuter-VERIFIED (the OPUS
judge reads a real code window from the cited file and may confirm a
problem the PR never introduced) or, for `missing_test`, mechanically
VERIFIED (the symbol verifier does not check the file is in the diff).
It enters the blocking queue, the Coder is handed a must-fix on a file
the PR never touched, and editing it can fail the single-pass ruff +
pytest gate (reverting the whole run). The dominant exposure is the
**design / security** lenses — exactly the high-volume output the
arbiter's off-diff filter most often catches.

Porting one pre-record filter into the bridge closes the gap for **all**
claim types at the correct timing — before any finding is persisted —
and is additive + dual-run (it changes nothing while CI still runs the
legacy path).

## What

1. **`src/ferova/review/findings_bridge.py`** — add a failure-soft
   diff-file extractor and an off-diff skip:
   - `_files_in_diff(diff: str) -> set[str]` — module-private; walks a
     unified-diff blob and returns the repo-relative touched paths from
     `diff --git a/<x> b/<y>` headers and `+++ b/<path>` / `--- a/<path>`
     lines, whitespace-tolerant, discarding `/dev/null`. Malformed /
     empty input yields whatever it could parse (an empty set is
     acceptable). This mirrors `coder_loop._files_in_diff`; define a
     fresh local copy — do **not** import the `coder_loop` private (that
     module is being retired in the trigger-flip DELETE phase; the
     duplication is intentional and temporary).
   - `record_findings_for_outcomes(...)` gains a new **required**
     keyword-only `diff: str` parameter. Compute
     `files_in_diff = _files_in_diff(diff)` once; set
     `diff_filter_enabled = bool(files_in_diff)`. Inside the per-comment
     loop, when `diff_filter_enabled and comment.file not in
     files_in_diff`, skip the comment (do not call `comment_to_finding`,
     do not record it) and increment a local `skipped` counter. When the
     diff is empty / malformed (`files_in_diff == set()`) the filter is
     disabled — every comment is recorded, matching the arbiter's
     never-silently-drop-everything contract. Unparsed-outcome skipping
     (`_is_unparsed`) is unchanged and still runs first.
   - When `skipped > 0`, emit a positive-event log
     `_log.info("findings_bridge.off_diff_skipped",
     pr_number=pr_number, n_skipped=skipped)`. Add the logger
     (`from ..core.logging import get_logger` · `_log =
     get_logger(__name__)`) — the module has none today.
2. **`src/ferova/review/orchestrator.py`** — wiring only: the
   `record_findings_for_outcomes(...)` call in `review_pr` (around
   line 393) passes `diff=diff`, reusing the **same** in-scope `diff`
   object already handed to `_round_two` (line 354) — never a re-fetch,
   to avoid a head-skew window. One-line anchored edit.
3. **Existing callers/tests** — update every existing
   `record_findings_for_outcomes(...)` call to pass `diff=` (the unit
   test in `tests/unit/test_findings_bridge.py` and the integration
   test in `tests/integration/test_findings_bridge.py`). `diff` is
   required deliberately: a forgotten wiring fails loudly at call time
   rather than silently disabling the filter.

Required imports (grep-verified against develop — copy, do not improvise;
the bridge still needs NOTHING from `consensus.py`):
- bridge adds: `from ..core.logging import get_logger`.
- orchestrator: no new import (the call site already exists; only the
  `diff=diff` kwarg is added).

## Files in scope

- `src/ferova/review/findings_bridge.py`
- `src/ferova/review/orchestrator.py` (one-line wiring)
- `tests/unit/test_findings_bridge.py`
- `tests/integration/test_findings_bridge.py` (call-site update only)

## Plan-shaping constraints

- Step 1 contracts `findings_bridge.py` (118 lines — small) plus its
  unit test `tests/unit/test_findings_bridge.py`.
- Step 2 contracts `orchestrator.py` (1 375 lines — the single big file
  of its step) via an **anchored** edit on the existing
  `record_findings_for_outcomes(` call (SP-DEV-TARGETED-PATCH: edit, do
  not re-emit the file) plus the `tests/integration/test_findings_bridge.py`
  call-site update.
- Two steps maximum. No magic size threshold anywhere — no test may
  hardcode a count derived from a hand-typed number (test-arithmetic
  law: derive expected counts from the fixtures).

## Out of scope

- Deleting `coder_loop._files_in_diff` / `arbiter_filter_reviews` — the
  trigger-flip DELETE phase owns the arbiter retirement; this slice only
  gives the findings path its own off-diff filter.
- The other two trigger-flip prerequisites (P2 EVIDENCE_REPLY_SENTINEL
  re-homing, P3 placeholder exit-9 port) — separate specs.
- Any prompt / persona / workflow change; any verdict / consensus /
  merge-gate change.
- claim_type refinement; the missing_test verifier's file-existence
  check (the pre-record filter makes it redundant).

## Smoke scenario

### Setup

A tmp db path; a diff blob touching only `src/ferova/review/foo.py`;
one Architect outcome (parsed, verdict not relevant) carrying two
blocker comments — one on `src/ferova/review/foo.py` (in diff) and
one on `src/ferova/review/coder_loop.py` (an existing file the diff
does NOT touch).

### Execute

`record_findings_for_outcomes(db, pr_number=..., head_sha=...,
outcomes=[outcome], round_n=1, diff=<the blob>)`, then `fetch_findings`.

### Expected

Exactly **1** finding recorded — the `foo.py` one; the off-diff
`coder_loop.py` comment is dropped and never reaches the ledger; the
function returns 1; `findings_bridge.off_diff_skipped` is emitted with
`n_skipped=1`.

## Definition of Done

- Off-diff comment dropped before recording —
  `test_off_diff_comment_skipped` (the design/security regression the
  parity analysis flagged: a blocker citing an existing-but-untouched
  file is not recorded).
- Off-diff `missing_test` comment (Tester lens) on an untouched file is
  likewise dropped — `test_off_diff_missing_test_skipped`.
- In-diff comments are all recorded unchanged —
  `test_in_diff_comments_recorded`.
- Empty / malformed diff disables the filter (every comment recorded) —
  `test_empty_diff_disables_filter`.
- Unparsed outcomes still skipped regardless of diff —
  `test_unparsed_skipped_with_diff`.
- The `n_skipped` positive-event log fires only when something was
  dropped — assert via a structlog-capturing fixture.
- Wiring: the orchestrator passes its real `diff`; existing
  `tests/integration/test_findings_bridge.py` updated to the new
  signature and green.
- `ruff` + `ruff format --check` + `pytest tests/unit` green; zero
  inline comments; no `# noqa`.

## Commit plan

1. `feat(review): findings bridge drops off-diff comments before recording`
2. `feat(review): orchestrator plumbs the PR diff into findings recording`

## Risks

- **`orchestrator.py` (1 375 lines) re-emission in step 2** — use an
  anchored edit on the single existing `record_findings_for_outcomes(`
  call (SP-DEV-TARGETED-PATCH); on an output-truncation stall, autopsy
  per the root-cause protocol before retrying.
- **Temporary `_files_in_diff` duplication** with `coder_loop` — accepted
  and explicit: the `coder_loop` copy is retired in the trigger-flip
  DELETE phase, leaving the bridge the sole owner. A reviewer DRY
  comment here is expected; the duplication is intentional, not an
  oversight.
- **Required `diff` param breaks an un-updated caller** — intended: the
  loud failure is the point (a silently-disabled filter re-opens the
  exact gap this slice closes). All current callers are in scope.
