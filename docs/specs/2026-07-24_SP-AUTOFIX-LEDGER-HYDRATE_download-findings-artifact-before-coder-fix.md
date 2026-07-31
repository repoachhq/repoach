---
id: SP-AUTOFIX-LEDGER-HYDRATE
title: Hydrate the auto_fix job's findings ledger from the review job's artifact before running Coder
version: 0.1
status: approved
author: agent
created: 2026-07-24
updated: 2026-07-24

owns:
  code: N/A
  resources: N/A

depends_on: [SP-LEDGER-TRANSPORT]
provides_to: []

constraints: {}
---

# Hydrate the auto_fix job's findings ledger from the review job's artifact before running Coder

## Intent

The `auto_fix` job starts every run against a brand-new, empty
`${{ runner.temp }}/repoach_review.db` and never downloads the
`findings-ledger-<pr>` artifact the `review` job just uploaded — so
`review fix`'s findings-driven work queue
(`run_coder_fix_from_findings`, whose own docstring calls that queue
"the merge gate's own blocker set — open blocking findings — not the
archive verdict") is fed an empty ledger on every invocation. In
practice the Coder can only ever react to CI red/green; every
design/security/missing-test/missing-docstring finding the review
team just recorded in the same workflow run is invisible to it unless
it also happens to fail CI independently. Add the missing
`actions/download-artifact` step, mirroring the one the `auto_merge`
job already has, so the auto_fix job's db is hydrated with the
review job's findings before `review fix` runs.

## Context

- `.github/workflows/auto-review.yml`, `review` job, `Upload findings
  ledger` step: uploads `findings-ledger-${{ steps.pr.outputs.number
  }}` (artifact name resolves to `findings-ledger-<pr>`) from
  `${{ runner.temp }}/repoach_review.db`.
- `.github/workflows/auto-review.yml`, `auto_fix` job (`needs: review`):
  the `Run Coder auto-fix` step sets a FRESH
  `REPOACH_DB_PATH: ${{ runner.temp }}/repoach_review.db` env var with
  no prior step populating that path from the review job's artifact —
  confirmed by parsing the job's step list at HEAD
  (`origin/develop`): `Checkout PR head (writable)` → `Set up Python
  3.11` → `Set up Python 3.13` → `Install package on both
  interpreters` → `Configure coder-bot git identity` → `Start LLM proxy
  sidecar` → `Run Coder auto-fix` — zero `actions/download-artifact`
  steps anywhere before (or after) `Run Coder auto-fix`. Re-verified
  2026-07-24: still true on `origin/develop`, problem still real.
- `.github/workflows/auto-review.yml`, `auto_fix` job, `Upload findings
  ledger (post-fix)` step (later in the same job): uploads the
  POST-fix db back to the same artifact name, gated on
  `steps.coder.outputs.pushed == 'true'`. This step exists and works;
  it is orthogonal to the gap here — it ships the ledger OUT after a
  fix, it does not pull the review job's ledger IN before one.
- `.github/workflows/auto-review.yml`, `auto_merge` job (`needs:
  [review, auto_fix]`), `Download findings ledger` step — the ONLY
  `actions/download-artifact` call in the whole workflow:
  ```yaml
  - name: Download findings ledger
    continue-on-error: true
    uses: actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093 # v4.3.0
    with:
      name: findings-ledger-${{ needs.review.outputs.pr_number }}
      path: ${{ runner.temp }}
  ```
  This is the exact pattern to mirror in `auto_fix`: same artifact
  name expression, same `continue-on-error: true` (a missing artifact
  — e.g. the review job never ran to completion, or this is the very
  first run before any upload — degrades to an empty db rather than
  failing the job), same `path: ${{ runner.temp }}` (which resolves to
  the same file the `Run Coder auto-fix` step's `REPOACH_DB_PATH`
  already points at, since both jobs use the identical
  `${{ runner.temp }}/repoach_review.db` filename).
- `docs/specs/2026-06-14_SP-LEDGER-TRANSPORT_cross-job-findings-via-artifact.md`
  established the review→auto_merge and auto_fix(post-fix)→auto_merge
  legs of this same transport pattern but its own "What" section (item
  2) only specifies the auto_fix job UPLOADING its post-fix ledger,
  never downloading the review job's PRE-fix ledger first — this spec
  closes that specific, previously out-of-scope leg. `depends_on:
  [SP-LEDGER-TRANSPORT]` names the edge; no new module or Python
  import is introduced (`owns.code: N/A`), matching the precedent of
  `SP-NIM-PROBE-UNPARSEABLE-DIAG`'s `depends_on` edit of a file it did
  not own.
- `src/repoach/review/coder_findings.py:438-440`
  (`run_coder_fix_from_findings` docstring): "The work queue is the
  merge gate's own blocker set — open blocking findings — not the
  archive verdict, so a PR that is archive-APPROVE yet carries a
  verified blocking finding is still acted on." This Python contract
  is correct and unchanged by this spec; the gap is purely that the CI
  wiring never gives it a populated db to read from.

## Goals

- G1: the `auto_fix` job downloads the `findings-ledger-<pr>` artifact
  (uploaded by the `review` job) into `${{ runner.temp }}` BEFORE the
  `Run Coder auto-fix` step runs, so `REPOACH_DB_PATH` points at a db
  file already populated with the review job's findings when `review
  fix` starts.
- G2: a missing artifact (first run on a PR, before any `review` job
  upload has ever happened, or a review job that failed before its
  upload step) degrades gracefully — the job continues with an empty
  db exactly as it does today — rather than failing the `auto_fix`
  job outright.
- G3: the new step uses the identical pinned action version, artifact
  name expression, and path as the `auto_merge` job's existing
  `Download findings ledger` step, so the workflow has one consistent
  ledger-transport idiom, not a second divergent one.

## Non-Goals

- NG1: no behavior change beyond adding the one download step to the
  `auto_fix` job — the `review` job's upload step, the `auto_fix`
  job's own post-fix upload step, and the `auto_merge` job's download
  step are all byte-for-byte unchanged.
- NG2: no Python change — `run_coder_fix_from_findings` and
  `coder_findings.py` already read whatever db is at `REPOACH_DB_PATH`
  correctly; this spec only ensures that path is populated before the
  job invokes it.
- NG3: no change to what counts as a "blocking finding" or to the
  stuck-escalation cap (`SP-STUCK-ESCALATION`) — purely a CI wiring
  fix that feeds the existing queue its intended input.
- NG4: no change to the artifact's retention, name, or upload steps in
  either the `review` or `auto_fix` job.

## Interface

`.github/workflows/auto-review.yml`, `auto_fix` job — insert one new
step after `Checkout PR head (writable)` (and before `Run Coder
auto-fix`, so the db exists before that step's `REPOACH_DB_PATH` is
read):

```yaml
- name: Download findings ledger
  continue-on-error: true
  uses: actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093 # v4.3.0
  with:
    name: findings-ledger-${{ needs.review.outputs.pr_number }}
    path: ${{ runner.temp }}
```

No Python signature changes anywhere.

## Behavior

### Nominal

- `review` job runs to completion, uploads `findings-ledger-<pr>`
  populated with the run's findings. `auto_fix` job starts, the new
  step downloads that artifact into `${{ runner.temp }}`, `Run Coder
  auto-fix` then reads a `REPOACH_DB_PATH` that already contains the
  review job's findings, and `run_coder_fix_from_findings` acts on
  them (design/security/missing-test findings become fixable, not only
  CI-red ones).

### Edge cases

- First run on a brand-new PR (`review` job's upload step has never
  run before this workflow run) — the download step's
  `continue-on-error: true` absorbs the "artifact not found" failure;
  the job proceeds with an empty db, identical to today's behavior.
- `review` job's own upload step itself failed or was skipped (e.g. the
  review-bot team errored before reaching that step) — same graceful
  degradation; `auto_fix` proceeds with an empty db rather than
  blocking on a findings source that never materialized.

### Failure scenarios

- Artifact exists but is corrupt/unreadable — `download-artifact`'s
  own failure is absorbed by `continue-on-error: true`; the subsequent
  `init_findings_schema` call inside `run_coder_fix_from_findings`
  creates a fresh empty schema against whatever partial file landed,
  matching today's fail-safe (never fail-closed) posture for this job.

## Architecture Impact

- Adds/Removes dependency: none — in-place addition of one workflow
  step; no new Python import, no new module. `owns.code: N/A`.
- New / changed coupling, cycles, or shared state: none beyond what
  `SP-LEDGER-TRANSPORT` already established (a run-scoped GitHub
  Actions artifact as the transport for `${{ runner.temp
  }}/repoach_review.db` between jobs in the same workflow run); this
  spec adds the missing consumer edge (`review` → `auto_fix`) to the
  existing producer/consumer graph, it does not introduce a new kind
  of coupling.

## Diagram

N/A (one workflow step insertion).

## Acceptance Criteria

- [ ] AC1: unit —
  `tests/unit/test_ci_ledger_download_wiring.py::test_auto_fix_job_downloads_findings_ledger_before_running_coder`.
  Parse `.github/workflows/auto-review.yml` with `yaml.safe_load`,
  locate `jobs.auto_fix.steps`, find the index of the step whose
  `run` contains `review fix` (the `Run Coder auto-fix` step) and
  assert that some EARLIER step in the same list has
  `step["uses"].startswith("actions/download-artifact@")` with
  `step["with"]["name"] == "findings-ledger-${{ needs.review.outputs.pr_number }}"`.
  FAILS on today's `auto-review.yml` (no `download-artifact` step
  exists anywhere in `jobs.auto_fix.steps`, so the search finds none
  before the Coder step, or none at all); PASSES once the step is
  added at the documented position.
- [ ] AC2: unit — same file,
  `test_auto_fix_download_step_matches_auto_merge_step_shape`. Locate
  the same new step in `jobs.auto_fix.steps` and the existing
  `Download findings ledger` step in `jobs.auto_merge.steps`; assert
  both have `continue-on-error is True`, the identical `uses` pin
  (same action + same commit-sha version comment), the identical
  `with.name` expression, and `with.path == "${{ runner.temp }}"`.
  FAILS on today's code (the `auto_fix` step does not exist, so the
  lookup raises/returns `None`); PASSES after the fix, proving the two
  jobs now share one consistent ledger-transport idiom (G3).
- [ ] AC3 (INTEGRATION):
  `tests/integration/test_ledger_hydration_across_jobs.py::test_finding_recorded_by_review_job_is_visible_to_auto_fix_coder_call`.
  In a `tmp_path` acting as `runner.temp`, write a `repoach_review.db`
  and record one open blocking finding into it via the same
  `findings.py` writer the `review` job's process uses (simulating the
  artifact having been produced by the `review` job and then
  downloaded — i.e. the file already exists at the shared path before
  `auto_fix` starts, exactly as it would once AC1's step runs). Then
  call `run_coder_fix_from_findings(pr_number=..., db_path=<the same
  file>, gh=<fake GhCli>, coder=<fake Coder>)` and assert the fake
  `Coder` is invoked with that finding included in its queue (not an
  empty queue) — proving that once the ledger is hydrated at the
  shared path (the CI-wiring gap AC1/AC2 close), the existing Python
  path already does the right thing with it. This test documents and
  locks the Python-side half of the contract that the workflow-level
  fix depends on; it passes on both pre- and post-change Python code
  (no Python change is made) but FAILS if the shared-db-path premise
  it encodes is ever violated (e.g. a future change makes
  `run_coder_fix_from_findings` ignore a pre-populated db).
- [ ] AC4: `ruff check` + `ruff format --check` green on the two new
  test files; zero inline comments (SP-NO-INLINE-COMMENTS-GATE) and no
  `# noqa` anywhere in the diff; full `pytest tests/unit
  tests/integration` green; `actionlint`/YAML validity on
  `.github/workflows/auto-review.yml` unchanged (still clean);
  `repoach arch graph --check` exits 0 (no new ownership conflict —
  `owns.code: N/A`, edit made under the `depends_on:
  [SP-LEDGER-TRANSPORT]` edge, no `src/` file touched).
- [ ] AC5: OPERATOR-MANUAL — this spec touches
  `.github/workflows/auto-review.yml`, a bot-forbidden path
  (`.github/workflows/*`); the diff is hand-implemented by the
  operator, not by the Coder/Developer bots, per the repo's own
  path-whitelist rule.

## Open Questions

(none)
