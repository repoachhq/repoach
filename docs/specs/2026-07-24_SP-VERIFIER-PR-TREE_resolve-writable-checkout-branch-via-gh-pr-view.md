---
id: SP-VERIFIER-PR-TREE
title: Resolve the auto_fix writable checkout ref via gh pr view, not the event-shape-dependent head.ref expression
version: 0.1
status: approved
author: agent
created: 2026-07-24
updated: 2026-07-24

owns:
  code: []
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Resolve the auto_fix writable checkout ref via gh pr view, not the event-shape-dependent head.ref expression

## Intent

The `auto_fix` job's writable checkout (`.github/workflows/auto-review.yml:326-331`)
resolves its `ref:` from `github.event.pull_request.head.ref`, an
expression that is empty for two of the workflow's own top-level
triggers (`issue_comment`, `pull_request_review`). When the pipeline
re-fires from a PR comment or a review submission — the exact re-fire
paths the workflow's own trigger comment (lines 10-16) says are
expected — the writable checkout lands on whatever branch the runner's
workspace happened to have checked out previously (or fails outright),
so `review fix` verifies findings and pushes fixes against the wrong
tree. Resolve the PR head branch once via `gh pr view --json
headRefName` — the same call already used later in this job
(`auto-review.yml:469`) — and feed that resolved value into the
checkout's `ref:` for every trigger shape.

## Context

Finding #13 (`tmp/implementable_findings.json`), re-verified against
`develop` (`origin/develop`, workflow at 682 lines) 2026-07-24:

- `.github/workflows/auto-review.yml:17-20`: the workflow listens for
  `issue_comment` (`types: [created, edited]`) and `pull_request_review`
  (`types: [submitted, edited]`) in addition to `pull_request`, with an
  explicit comment (lines 10-16) explaining these re-fire the pipeline
  so a later verdict flip (a human comment, a fresh `repoach review pr
  <N>` archive update) is not stranded.
- `.github/workflows/auto-review.yml:114-128` (`Resolve PR number`
  step, `review` job): already handles all three event shapes
  correctly — `github.event.pull_request.number ||
  github.event.issue.number || github.event.inputs.pr_number` — proving
  the workflow's own authors know `issue_comment` payloads carry no
  `pull_request` object (GitHub's documented event schema: `issue_comment`
  carries `issue`/`comment` only).
- `.github/workflows/auto-review.yml:326-331` (`Checkout PR head
  (writable)`, `auto_fix` job): `ref: ${{
  github.event.pull_request.head.ref }}` — the SAME event-shape problem
  the `Resolve PR number` step above already solved, left unsolved here.
  For `issue_comment` / `pull_request_review` runs this expression
  evaluates to an empty string; `actions/checkout` with an empty `ref`
  falls back to whatever `github.sha` the runner's persisted self-hosted
  workspace ref currently resolves to (self-hosted runners reuse the
  same workspace directory across jobs — `auto-review.yml:73,297` use
  `runs-on: self-hosted`), which is NOT guaranteed to be the PR's head.
- `.github/workflows/auto-review.yml:469`: the `Release held CI runs`
  step in the SAME job already resolves the branch correctly —
  `pr_branch="$(gh pr view "${{ needs.review.outputs.pr_number }}"
  --json headRefName --jq .headRefName)"` — proving the fix is a
  one-line pattern already proven inside this exact job, just applied
  too late (after the writable checkout already ran against the wrong
  ref).
- `needs.review.outputs.pr_number` (`auto-review.yml:88`) is already
  available to the `auto_fix` job (`needs: review`,
  `auto-review.yml:284`), so no new job output is required.

This spec touches ONLY `.github/workflows/auto-review.yml`, which is
WHITELIST-FORBIDDEN for the bots (CLAUDE.md path whitelist) — Execution
is OPERATOR-MANUAL: hand-implement with human review, never `repoach
develop`.

## Goals

- G1: the `auto_fix` job's writable checkout resolves the PR's actual
  head branch identically across all three trigger shapes
  (`pull_request`, `issue_comment`, `pull_request_review`), not just
  the shape that happens to carry `pull_request.head.ref`.
- G2: the resolution reuses the exact `gh pr view --json headRefName`
  call already proven at `auto-review.yml:469`, run ONCE, before the
  writable checkout, rather than re-deriving branch logic ad hoc.
- G3: no change to which PR number is targeted — `auto_fix` continues
  to operate on `needs.review.outputs.pr_number` exactly as today.

## Non-Goals

- NG1: no change to trigger `on:` block, the `concurrency` group, or
  the `review` job's own (already-correct) PR-number resolution
  (`auto-review.yml:114-128`) — those are unaffected.
- NG2: no change to the data-only checkout in the `review` job
  (`auto-review.yml:135-139`, `ref: refs/pull/<n>/head`) — that
  expression is already event-shape-independent and correct.
- NG3: no change to `review fix` / `coder_findings` / `coder_loop`
  Python modules — the bug is entirely in the workflow YAML's ref
  expression, not in any src module.
- NG4: no behavior change beyond making the writable checkout target
  the correct branch on every trigger shape — the auto_fix job's
  downstream steps (Coder loop, CI matrix gate, push, re-review, held-CI
  release) are unchanged.

## Interface

N/A — no Python signatures. The change is a new YAML step plus one
`ref:` expression edit in `.github/workflows/auto-review.yml`, e.g.:

```yaml
- name: Resolve PR head branch
  id: pr_branch
  env:
    GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
  run: |
    ref="$(gh pr view "${{ needs.review.outputs.pr_number }}" \
      --repo "${{ github.repository }}" \
      --json headRefName --jq .headRefName)"
    echo "ref=${ref}" >> "$GITHUB_OUTPUT"

- name: Checkout PR head (writable)
  uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5 # v4.3.1
  with:
    fetch-depth: 0
    ref: ${{ steps.pr_branch.outputs.ref }}
    token: ${{ secrets.GITHUB_TOKEN }}
```

`--repo` is passed explicitly because this step runs before any
checkout in the `auto_fix` job — the self-hosted runner's workspace may
carry a stale or absent git remote at this point, so branch resolution
must not depend on an implicit repo context.

## Behavior

### Nominal

- `pull_request` trigger (opened/synchronize/reopened/ready_for_review):
  `gh pr view` resolves the same branch `github.event.pull_request.head.ref`
  would have carried; behavior is unchanged from today for this trigger
  shape.

### Edge cases

- `issue_comment` trigger (a PR comment re-fires the pipeline):
  `github.event.pull_request.head.ref` is empty today; after this
  change, `gh pr view --json headRefName` resolves the branch from the
  PR number regardless of event shape, so the writable checkout lands
  on the correct branch.
- `pull_request_review` trigger (a review submission re-fires the
  pipeline): same resolution path as `issue_comment` — `gh pr view`
  does not depend on the review payload carrying a `pull_request.head`
  field.
- `workflow_dispatch` trigger: `needs.review.outputs.pr_number` already
  resolves from `github.event.inputs.pr_number`
  (`auto-review.yml:127`); `gh pr view` resolves the branch from that
  number identically to the other shapes.

### Failure scenarios

- `gh pr view` fails (PR closed/deleted mid-run, API outage): the
  `Resolve PR head branch` step's `run:` exits non-zero (unset `set -e`
  default for `gh` failures), failing the job loudly BEFORE the writable
  checkout runs against a stale or wrong ref — strictly safer than
  today's silent empty-ref fallback.

## Architecture Impact

- Adds/Removes dependency: none — in-place workflow-YAML edit; no
  Python module ownership change, no cross-owner import. `owns.code` is
  `[]`.
- New / changed coupling, cycles, or shared state: none — the new step
  reuses the `needs.review.outputs.pr_number` output and the
  `gh pr view --json headRefName` pattern already present at
  `auto-review.yml:469` within the same job; no new shared state.

## Diagram

N/A (workflow-only fix).

## Acceptance Criteria

- [ ] AC1: `.github/workflows/auto-review.yml`'s `auto_fix` job's
  `Checkout PR head (writable)` step's `with.ref` no longer contains
  the literal `github.event.pull_request.head.ref`; a preceding step in
  the same job resolves the PR head branch via `gh pr view ... --json
  headRefName` and the checkout's `ref:` reads that step's output.
- [ ] AC2: the resolution step runs for every trigger shape the
  workflow accepts (`pull_request`, `issue_comment`,
  `pull_request_review`, `workflow_dispatch`) — it depends only on
  `needs.review.outputs.pr_number`, never on
  `github.event.pull_request.*`.
- [ ] AC3: promised test —
  `tests/unit/test_ci_workflow_pr_branch_resolution.py::test_writable_checkout_does_not_use_event_dependent_head_ref`
  and
  `tests/unit/test_ci_workflow_pr_branch_resolution.py::test_writable_checkout_ref_resolved_via_gh_pr_view_step`,
  parsing `.github/workflows/auto-review.yml` with `yaml.safe_load` and
  asserting the `auto_fix` job's writable-checkout `ref:` does not
  reference `github.event.pull_request.head.ref` and instead references
  the output of a preceding step whose `run:` invokes `gh pr view`
  with `--json headRefName`. Both MUST FAIL against the pre-change
  workflow (today's `ref: ${{ github.event.pull_request.head.ref }}`
  trips the first assertion; there is no preceding `gh pr view` step to
  satisfy the second).
- [ ] AC4: OPERATOR-MANUAL confirmation — the PR implementing this is
  hand-authored with human review; the bots did not (and cannot) emit
  the workflow edit (`.github/workflows/*` is path-whitelist-forbidden
  for bot-emitted fixes, CLAUDE.md).
- [ ] AC5: `ruff` + `ruff format --check` + `pytest tests/unit` green
  (including the two new tests); zero inline comments
  (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`; `repoach arch graph
  --check` exits 0.

## Open Questions

(none)
