---
id: SP-PUSH-FALLBACK-REFUSE
title: Coder push fallback must refuse when no head branch resolves — never target develop
version: 0.1
status: draft
author: jfaye (Ferova audit 2026-07-13)
created: 2026-07-13
updated: 2026-07-13

owns:
  code: []
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Coder push fallback must refuse when no head branch resolves — never target develop

## Intent

When the Coder resolves findings on a PR whose head branch cannot be
read, the push falls back to the literal `"develop"` branch — pushing
unreviewed commits straight onto the protected integration branch,
bypassing PR review (CI runners carry no local pre-push hook). The
fallback must REFUSE, never target `develop`.

## Context

Audit 2026-07-13 finding M5. `src/ferova/review/coder_findings.py`:

- Lines 621-623:
  `git_commit_and_push(repo_root=repo, commit_message=commit_msg, branch=head or "develop")`.
- `head` is `str(pr_meta.get("headRefName") or "")` (line 459), where
  `pr_meta = gh.pr_view(pr_number) or {}` (line 457). When `pr_view`
  returns nothing or lacks `headRefName`, `head` is `""` and the `or
  "develop"` fallback pushes to `develop`.
- This runs on the CI review runner, which has no `.githooks/pre-push`
  guard (the client-side branch-protection equivalent), so nothing else
  stops the direct push to a protected branch.

Execution: hand-implement with human review (audit 2026-07-13) — this
guards a protected-branch push path adjacent to the merge/push
machinery.

## Goals

- G1: when no head branch resolves (`head` empty), the push path
  REFUSES — it performs no push and returns a `CoderFindingsResult`
  with a clear `no_op_reason`, exactly as the existing push-failure
  branch (lines 624-633) does.
- G2: the string literal `"develop"` (and any protected branch) is
  never used as a push target fallback.
- G3: the refusal is logged loudly so the operator sees why the Coder
  round produced no push.

## Non-Goals

- NG1: no change to the successful path where `head` is a real feature
  branch.
- NG2: no re-derivation of the branch from git state as a second
  fallback — the finding wants a refusal, not a cleverer guess.
- NG3: no change to `git_commit_and_push` internals.

## Assumptions

- A1: a resolvable head is the normal case; an empty head signals a
  transient `gh pr view` failure or a closed/odd PR — the safe response
  is to no-op and let the round be retried, never to push to `develop`.
- A2: `CoderFindingsResult` with a `no_op_reason` is the sanctioned way
  to report "nothing pushed" (the pattern already used at lines
  624-633, 586-593, 597-604).

## Interface

N/A (in-place fix, no public signature change).

## Behavior

### Nominal

`head` is a real feature branch → `git_commit_and_push(..., branch=head)`
as today; the fixes are committed and pushed to the PR's branch.

### Edge cases

- `pr_view` returns `{}` or a dict without `headRefName` → `head == ""`
  → the push path returns a no-op `CoderFindingsResult`
  (`no_op_reason` naming the unresolved head), nothing is pushed, and a
  warning is logged. No commit lands on `develop`.

### Failure scenarios

- `gh pr view` transient failure yielding an empty head → refuse (fail
  closed): the unreviewed fixes stay local/uncommitted-to-remote rather
  than reaching a protected branch. The round can retry once the head
  resolves.

## Architecture Impact

- Adds/Removes dependency: none — in-place modification of
  `coder_findings.py` (owned by an existing spec, the findings-Coder
  arc); no new cross-owner import.
- New / changed coupling, cycles, or shared state: none.

## Diagram

N/A (in-place fix)

## Acceptance Criteria

- [ ] AC1: unit — with a resolved `head`, the push target is that head;
  with an empty `head`, no push is attempted and a no-op result with a
  descriptive `no_op_reason` is returned.
- [ ] AC2 (INTEGRATION): drive the findings-Coder resolution path
  end-to-end against a tmp git repo (real local git; the remote is a
  bare tmp repo) with a `GhCli`/`pr_view` boundary fake returning no
  `headRefName`. Assert nothing is pushed to the tmp `develop` ref (its
  SHA is unchanged) and the returned result carries the refusal reason.
  A companion case with a real head branch confirms the push still
  targets that branch.
- [ ] AC3: promised test file + selectors —
  `tests/unit/test_coder_findings.py::test_push_refuses_when_no_head_branch`.
- [ ] AC4: `ruff` + `ruff format --check` + `pytest tests/unit` green;
  zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`;
  `ferova arch graph --check` exits 0.

## Open Questions

- OQ1: implement by hand + human review before re-trusting the Coder
  push path (audit) — it borders the protected-branch push surface.
