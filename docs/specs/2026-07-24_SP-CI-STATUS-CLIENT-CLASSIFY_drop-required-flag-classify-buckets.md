---
id: SP-CI-STATUS-CLIENT-CLASSIFY
title: Drop the --required filter in fetch_ci_status and classify CI buckets client-side
version: 0.1
status: approved
author: agent
created: 2026-07-24
updated: 2026-07-24

owns:
  code:
    - src/repoach/review/coder_loop.py
  resources: N/A

depends_on: []
provides_to: []

constraints: {}
---

# Drop the --required filter in fetch_ci_status and classify CI buckets client-side

## Intent

`coder_loop.fetch_ci_status` calls `gh pr checks --required`, which
only ever returns rows when GitHub branch protection has required
checks configured. This repo has none, so the call permanently reads
`CI_UNKNOWN`, and `coder_findings.run_coder_fix_from_findings` treats
`CI_UNKNOWN` as a no-op — CI failures are never auto-recorded as
`broken_behavior` findings and CI-green never auto-resolves them, even
though `.github/workflows/ci.yml` runs real, informative checks on
every PR. Drop `--required` and classify the check buckets returned by
plain `gh pr checks` client-side, so real RED/GREEN/PENDING state
reaches the Coder loop on this repo.

## Context

Confirmed against the current `develop` tree (2026-07-31) and live
against the repo's branch protection:

- `.github/workflows/auto-review.yml:364-373` — an in-place comment
  documents the blindness: "`fetch_ci_status` filters on `--required`
  too and thus reads `CI_UNKNOWN` on this repo (never RED/GREEN — a
  pre-existing blindness, follow-up spec candidate)". This spec is
  that follow-up.
- `src/repoach/review/coder_loop.py:734-745` — `fetch_ci_status` builds
  `gh._run(["pr", "checks", str(pr_number), "--required", "--json",
  "name,state,bucket,link,workflow"])`; when the command errors
  (`res.returncode not in {0, 8}`) it returns `(CI_UNKNOWN, [])` at
  line 745.
- `gh api repos/:owner/:repo/branches/develop/protection` returns
  `404 Branch not protected` on this repo right now — `gh pr checks
  --required` has no required-check set to filter on, so on a real PR
  it either errors outright or returns an empty/degenerate row set,
  landing on the `CI_UNKNOWN` branch every time.
- `src/repoach/review/coder_findings.py:498-508` —
  `ci_state, failed_rows = fetch_ci_status(gh, pr_number)`; only
  `CI_RED` (records findings) and `CI_GREEN` (resolves findings) do
  anything. `CI_UNKNOWN` (and `CI_PENDING`) fall through with no
  branch executed — a silent no-op, exactly as the workflow comment
  describes.
- Note: this is unrelated to `SP-CI-SKIPPED-CONCLUSION` (queued,
  unwritten) — that candidate is about `auto_merge.py`'s
  `_SUCCESS_CONCLUSIONS` counting a `SKIPPED` required check as green
  in the merge gate. This spec is scoped to `coder_loop.fetch_ci_status`
  feeding the Coder auto-fix loop, a different file and a different
  mechanism; the two must not be conflated into one PR.
- `coder_loop.py` is presently unowned by any other spec's `owns.code`
  (checked via `grep -rl "coder_loop.py" docs/specs/*.md | xargs grep
  -l "owns:"` — only `inline_comment_heal.py`, `spec_supersede.py`,
  `findings_bridge.py` etc. are claimed elsewhere). Two pending specs
  (`SP-CODER-CHAINS-GUARD`, `SP-CONSISTENCY-SWEEP`) also touch this
  file without owning it — per the debt inventory these must be
  sequenced one Developer session at a time against `coder_loop.py`,
  not developed in parallel with this one.

## Goals

- G1: `fetch_ci_status` no longer passes `--required` to `gh pr
  checks`; it requests all checks on the PR
  (`--json name,state,bucket,link,workflow`) and classifies from the
  full row set.
- G2: classification remains: any `pending`/empty bucket → `CI_PENDING`
  (unless a `fail` is already present, which still governs); any `fail`
  bucket → `CI_RED` with `failed_rows` populated exactly as today;
  otherwise → `CI_GREEN`.
- G3: `skipping` and `cancel` buckets are explicitly treated as
  non-blocking — neither trigger `CI_PENDING` nor count as a failure —
  and are logged (`structlog`, no new exception) via a single line
  naming the check name and bucket so an operator can see a skipped
  job did not silently grant green by omission of the reasoning, only
  by its actual state.
- G4: on a `gh` transport error (non-zero, not 8) or unparseable JSON,
  behavior is unchanged: `(CI_UNKNOWN, [])`.

## Non-Goals

- NG1: no change to `coder_findings.run_coder_fix_from_findings`'s
  handling of `CI_UNKNOWN`/`CI_PENDING` (still a no-op) — this spec
  only fixes the upstream signal so `CI_RED`/`CI_GREEN` are reachable
  in practice; the no-op-on-unknown/pending policy itself is
  unchanged.
- NG2: no change to `.github/workflows/auto-review.yml` — the
  informational comment at lines 364-373 becomes stale once this
  ships, but that file is bot-forbidden
  (`FORBIDDEN_PREFIXES` in `coder_loop.py`); updating the comment is an
  OPERATOR-MANUAL follow-up, not part of this spec's Execution.
- NG3: no change to `fetch_failed_check_logs` or the `failed_rows`
  dict shape (`{name, link, workflow}`).
- NG4: no attempt to reconstruct GitHub's branch-protection semantics
  client-side (e.g. no `isRequired`-based filtering) — filtering on
  `isRequired` would reproduce the exact blindness this spec fixes,
  since this repo has zero required checks configured; every returned
  check is treated as CI-signal-bearing.
- NG5: no behavior change beyond the `gh pr checks` argv and the
  bucket-classification branches described in Goals/Behavior.

## Interface

`src/repoach/review/coder_loop.py`:

```python
def fetch_ci_status(gh: GhCli, pr_number: int) -> tuple[str, list[dict[str, str]]]:
    """Return the aggregated check state on a PR (all checks, not just
    branch-protection-required ones).
    """
```

Signature and return-type contract unchanged; only the internal `gh
pr checks` invocation and bucket handling change. No caller
(`coder_findings.run_coder_fix_from_findings`) needs a signature
change.

## Behavior

### Nominal

- A PR with all checks `pass` → `fetch_ci_status` returns
  `(CI_GREEN, [])`.
- A PR with one or more checks in the `fail` bucket → returns
  `(CI_RED, failed_rows)` with one dict per failing check.
- A PR with a check still `pending`/`in_progress` and no `fail` yet →
  returns `(CI_PENDING, [])`.

### Edge cases

- A PR with only `skipping`/`cancel` buckets (e.g. an actor-gated
  workflow that never ran) and otherwise all `pass` → `CI_GREEN`, with
  a logged line per skipped/cancelled check naming it — it must not be
  silently indistinguishable from an all-`pass` run in the logs, but it
  must also not block on it.
- Zero rows returned (e.g. no checks configured on the PR at all) →
  `CI_GREEN`, unchanged from today.
- A `fail` bucket present alongside `pending` ones → `CI_RED` wins over
  `CI_PENDING` (unchanged ordering: fail is checked, then pending, or
  fail short-circuits pending — see Acceptance Criteria for the exact
  precedence test).

### Failure scenarios

- `gh pr checks` exits with a code other than `0`/`8`, or emits
  unparseable/non-list JSON → `(CI_UNKNOWN, [])`, unchanged.

## Architecture Impact

- Adds/Removes dependency: none.
- New / changed coupling, cycles, or shared state: none — in-place
  change to one function's `gh` invocation and branch logic inside
  `coder_loop.py`, which this spec now owns. No new cross-module
  import; `coder_findings.py` keeps calling `fetch_ci_status` with the
  same signature.

## Diagram

N/A (in-place fix, single function).

## Acceptance Criteria

- [ ] AC1: unit — `fetch_ci_status` invokes `gh pr checks` WITHOUT a
  `--required` argument (assert on the captured argv passed to
  `GhCli._run`); this must FAIL on pre-change code, which always
  includes `--required` in argv.
- [ ] AC2: unit — a `gh` response shaped like this repo's real
  `develop` PRs (no branch protection, two matrix jobs, one `fail`
  bucket, e.g. `Test suite (Python 3.13)`) yields `(CI_RED,
  [{"name": "Test suite (Python 3.13)", ...}])`; this must FAIL on
  pre-change code, which reads `CI_UNKNOWN` whenever `--required`
  errors out or returns no rows on this repo's unprotected branch.
- [ ] AC3: unit — a response with one `pass` row and one
  `skipping` row (no `fail`, no `pending`) yields `(CI_GREEN, [])`, and
  a `structlog` capture (`caplog`/`capture_logs`) shows one log event
  naming the skipped check.
- [ ] AC4: unit — a response with one `pass` row and one `cancel` row
  (no `fail`, no `pending`) yields `(CI_GREEN, [])`.
- [ ] AC5: unit — existing precedence/edge behavior is preserved
  unchanged: all-`pass` → `CI_GREEN`; any `pending` bucket with no
  `fail` → `CI_PENDING`; `gh` transport error (returncode not in
  `{0, 8}`) → `CI_UNKNOWN`; empty row list → `CI_GREEN`.
- [ ] AC6: promised tests, added to the existing
  `tests/unit/test_review_coder_loop.py` CI-status-detection section:
  `test_fetch_ci_status_drops_required_flag`,
  `test_fetch_ci_status_red_on_unprotected_repo_shape`,
  `test_fetch_ci_status_green_with_skipped_check_logs_it`,
  `test_fetch_ci_status_green_with_cancelled_check`. All four import
  `fetch_ci_status` from `repoach.review.coder_loop` exactly as the
  existing tests in that module already do.
- [ ] AC7: `ruff` + `ruff format --check` + `pytest tests/unit` green;
  zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`;
  `repoach arch graph --check` exits 0.

## Open Questions

(none)
