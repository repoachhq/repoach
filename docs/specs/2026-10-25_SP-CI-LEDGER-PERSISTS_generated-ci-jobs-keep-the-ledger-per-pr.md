---
id: SP-CI-LEDGER-PERSISTS
title: The generated CI jobs keep the findings ledger between jobs of one PR / MR
version: 0.2
status: draft
author: agent
created: 2026-10-25
updated: 2026-10-25

owns:
  code: [tests/unit/test_ci_ledger_persists.py]
  resources: []

depends_on: [SP-FINDINGS-BRIDGE-DOCFIX, SP-UNIVERSAL-CI-ENTRY]
provides_to: []

constraints: {}
---

# The generated CI jobs keep the findings ledger between jobs of one PR / MR

## Intent

`repoach init --ci gitlab|github` writes a job that starts every run
with an empty findings ledger, so the run cache of
SP-FINDINGS-BRIDGE-DOCFIX 0.3 (a completed `review pr` at the same head
is replayed from `pr_review_runs`) never fires in CI, and the older-head
re-check of SP-GATE-REVERIFIES-AT-HEAD has no earlier finding to
re-check. Make both generated jobs keep the ledger per PR / MR, and let
`doctor --ci` say whether they do.

## Context

Tech-debt row 84 (`docs/tech_debt.md`), verbatim: "CI never warms the
`review pr` run cache — SP-FINDINGS-BRIDGE-DOCFIX 0.3 replays a
completed run at the same head from `pr_review_runs` in
`.repoach/repoach.db`, but the snippets `repoach init --ci` generates
keep no state between jobs: the GitLab job has no `cache:` for
`.repoach/`, the GitHub workflow no `actions/cache` step, so every CI
job starts with an empty ledger, the replay never fires there and
`REPOACH_REVIEW_FORCE` is inert. The GitHub workflow's `Review` step
also does not pass `REPOACH_REVIEW_FORCE` through `env:`, so even a
warmed cache could not be forced. (verified 2026-10-22)"

Measured on `origin/develop` 2706bb6 (2026-10-25):

- F1: `render_gitlab_job` (`src/repoach/ci_entry/snippets.py`) emits
  `review:advisory` with `needs`, `rules`, `allow_failure`,
  `interruptible`, `timeout`, `image`, `variables`
  (`REPOACH_INSTALL`, `REPOACH_ADVISORY_POST`) and `script`; no
  `cache:` key.
- F2: `render_github_workflow` emits checkout, setup-python, the
  scrubbed install step and a `Review` step whose `env:` carries
  `GH_TOKEN`, `REPOACH_ADVISORY_POST`, the direct key and the two
  gateway variables; no `actions/cache` step and no
  `REPOACH_REVIEW_FORCE` (`FORCE_VAR`, `ci_entry/variables.py:36`,
  read by `ci_entry/advisory.py:583`).
- F3: the ledger is not under `.repoach/` by default.
  `Settings.db_path` (`core/config.py`) and `resolve_db_path`
  (`core/paths.py:382`) default to
  `$XDG_DATA_HOME/repoach/<project-slug>/repoach.db`
  (`default_db_path`, SP-UNIVERSAL-INSTALL G4), outside the checkout;
  only `REPOACH_DB_PATH` (or `[state].db_path` in `repoach.toml`, which
  Yoob sets to `.repoach/repoach.db`, Yoob `docs/BACKLOG.md` B-132)
  moves it into the workspace. A `cache:` on `.repoach/` alone would
  cache an empty directory in a project that has not set the path.
- F4: the generated GitLab job file itself lives under `.repoach/`
  (`GITLAB_JOB_PATH = .repoach/ci/gitlab-review.yml`): a cache of the
  whole directory restores an older copy of that tracked file over the
  checkout.
- F5: measured precedent. Yoob's `review:gate` job (Yoob
  `.gitlab-ci.yml`, `origin/develop`) declares
  `cache: {key: repoach-ledger-mr-$CI_MERGE_REQUEST_IID, paths: [.repoach/], when: always}`;
  on Yoob !314 (2026-10-25) the third pipeline restored
  `repoach-ledger-mr-314` and the gate ran its older-head re-check from
  it (Yoob `docs/specs/2026-10-20_advisory-review-runner.md` C4). The
  same-head replay is measured locally only: SP-FINDINGS-BRIDGE-DOCFIX
  AC17, yoob !306, run 1 `usage: 8 calls`, run 2 `usage: 0 calls`,
  `cached: true`, with both runs sharing one ledger.
- F6: a GitHub Actions cache entry is immutable once saved, and
  `actions/cache`'s post step saves only when the job succeeds. A key
  of the PR number alone is therefore written once and never refreshed;
  the generated job is `continue-on-error: true` and its `Review` step
  may fail, which would skip the save.
- F7: Yoob's `review:gate` script line is `bash ci/review-gate.sh`;
  the word `repoach` appears in that job's `variables:`
  (`REPOACH_REF`, `REPOACH_LLM_PROXY_BASE_URL`, `REPOACH_REVIEW_POST`)
  and its `cache.key`, not in its `script:` (the driver script calls
  `repoach`: 32 lines of Yoob `ci/review-gate.sh` name it).
- F8: a GitLab job cannot read its runner's cache configuration (it
  lives in the runner host's `config.toml`); what a job can observe is
  whether a declared cache was restored, i.e. whether the ledger file
  exists when the job starts.

## Decisions (0.2, 2026-10-25)

- D1 (Q1): a project's explicit ledger path is never overridden. `init
  --ci` reads the project's `[state].db_path`: when none is set, the
  snippet sets `REPOACH_DB_PATH=.repoach/repoach.db`; when one is set,
  the snippet sets no `REPOACH_DB_PATH` and caches that path. When the
  path in force lies outside the workspace, the snippet declares no
  ledger cache at all (never a wrong path) and `doctor --ci` prints
  `ledger not cacheable (outside workspace)`.
- D2 (Q2): `doctor --ci` warns, without changing its exit code, when a
  GitLab MR pipeline declares the ledger cache but the ledger is absent
  at job start (F8: the runner's cache backend is not observable, its
  effect is). Information only.
- D3 (Q3): `doctor --ci` also reads hand-written jobs: any job of the
  checkout's `.gitlab-ci.yml` that names `repoach` in its `script`,
  `variables` or `cache` counts as a repoach job (F7: Yoob's
  `review:gate` names it only in `variables` and `cache.key`). Yoob's
  `review:gate` is the first client and the reference fixture.

## Goals

- G1: the generated GitLab job declares
  `cache: {key: repoach-ledger-mr-$CI_MERGE_REQUEST_IID, paths: [<ledger>], when: always}`
  (F5's key; the path narrowed to the ledger file, F4), where
  `<ledger>` is the path in force (D1); it sets
  `REPOACH_DB_PATH: ".repoach/repoach.db"` in `variables:` only when the
  project sets no `[state].db_path`.
- G2: the generated GitHub workflow restores the ledger before the
  `Review` step with `actions/cache/restore` (key
  `repoach-ledger-pr-<number>-<run_id>-<run_attempt>`, `restore-keys:
  repoach-ledger-pr-<number>-`) and saves it after it with
  `actions/cache/save` under `if: always()` (F6), both pinned by commit
  SHA as `CHECKOUT_ACTION` is; it sets
  `REPOACH_DB_PATH: .repoach/repoach.db` on the `Review` step under the
  same D1 rule.
- G3: the GitHub `Review` step passes
  `REPOACH_REVIEW_FORCE: ${{ vars.REPOACH_REVIEW_FORCE }}` through
  `env:`.
- G4: a second job of the same PR / MR at an unchanged head restores
  the first job's ledger and `review pr` replays the completed run:
  `cached: true`, 0 reviewer calls, 0 judge calls.
- G5: a ledger path in force outside the workspace yields no ledger
  cache in either snippet (D1).
- G6: `doctor --ci` prints the ledger path `ci review` would use,
  whether it is cacheable (inside the workspace), whether the file is
  present at that moment (a restored cache), and which repoach jobs
  declare the ledger cache: the generated job files and every
  hand-written `.gitlab-ci.yml` job naming `repoach` (D3).
- G7: `doctor --ci` prints one warning line, exit code unchanged, when
  it runs in a GitLab MR pipeline whose repoach job declares the
  ledger cache and the ledger is absent (D2).

## Non-Goals

- NG1: no ledger shared across PRs / MRs (Yoob B-132's remaining part):
  the key is per PR / MR.
- NG2: no change to the replay itself (SP-FINDINGS-BRIDGE-DOCFIX G9-G13)
  or to the older-head re-check.
- NG3: no rewrite of a project's own CI file: `.gitlab-ci.yml` and
  existing workflows stay untouched (SP-UNIVERSAL-CI-ENTRY); a job file
  already present and different stays `differs` without `--force`.
- NG4: no distributed GitLab cache setup; a runner without a shared
  cache keeps the ledger only on that runner (D2 makes it visible).
- NG5: `doctor --ci` does not follow `include:` entries of
  `.gitlab-ci.yml` other than the generated job file it already reads.

## Assumptions

- A1: `REPOACH_DB_PATH` relative to the project root resolves inside
  the CI workspace (`Settings._anchor_relative_paths`,
  `resolve_db_path`); the environment outranks `repoach.toml`
  (`core/config.py`, "environment > file > built-in default").
- A2: `REPOACH_DB_PATH` is not a secret and reaches the `ci review`
  child (`review/secret_env.py` names it among the non-secret config).
- A3: `src/repoach/ci_entry/` stays owned by SP-UNIVERSAL-CI-ENTRY;
  this spec edits `snippets.py`, `variables.py` and `cli.py` there
  under the `depends_on` edge and claims only its new test.
  `src/repoach/cli/doctor_cmds.py` is frontier; it passes the project
  root to `ci_doctor_lines`.

## Interface

Inputs:
- `LedgerPlan` (new, frozen, `ci_entry/snippets.py`): `path: Path |
  None` (the workspace-relative ledger to cache, `None` when not
  cacheable) and `sets_env: bool` (whether the snippet sets
  `REPOACH_DB_PATH`); `ledger_plan(root: Path) -> LedgerPlan` derives
  it from `[state].db_path` (D1).
- `render_gitlab_job(target, ledger: LedgerPlan)` and
  `render_github_workflow(target, ledger: LedgerPlan)`; `render` and
  `plan_ci_init` pass `ledger_plan(root)`.
- `DEFAULT_LEDGER_PATH: Path = Path(".repoach") / "repoach.db"` (new).
- `CACHE_RESTORE_ACTION`, `CACHE_SAVE_ACTION` (new constants):
  `actions/cache/restore@<sha>` and `actions/cache/save@<sha>`.
- `ci_doctor_lines(env, root: Path | None = None) -> list[str]`: `root`
  added, the checkout the job files are read from; `None` skips the
  job lines.

Outputs (new `doctor --ci` lines, names only, never a secret's value):
- `ledger=<path> (cacheable)`, or `ledger=<path>` followed by the line `ledger not cacheable (outside workspace)`
- `ledger_present=yes|no`
- `ledger_cache=<job> declared|<job> not declared`, one line per
  repoach job found (generated files and `.gitlab-ci.yml` jobs, D3), or
  `ledger_cache=no repoach job found`
- warning (stderr, D2): `repoach: warning: ledger cache declared but
  not restored; a runner without a shared cache keeps the ledger only
  on the runner that wrote it`

Errors: none new; an unparsable `.gitlab-ci.yml` prints
`ledger_cache=.gitlab-ci.yml unreadable` and exit 0.

## Behavior

### Nominal

Job 1 of MR `!N` at head `H`: the cache misses, `review pr` runs the
reviewers and writes `pr_review_runs` into the ledger, the job saves
the cache (`when: always` / `if: always()`). Job 2 of `!N` at `H` (a
retry, or a pipeline re-run): the cache restores the ledger,
`review pr` finds the completed run and replays it (`cached: true`,
`usage: 0 calls`). With `REPOACH_REVIEW_FORCE=1` job 2 runs the
reviewers again. Job 3 at a new head `H'`: the restored ledger holds
`H`'s findings, which the older-head re-check re-verifies at `H'`.

### Edge cases

- `[state].db_path = ".repoach/repoach.db"` (Yoob) -> no
  `REPOACH_DB_PATH` in the snippet, cache path `.repoach/repoach.db`.
- `[state].db_path` absolute, or relative but resolving outside the
  root -> no ledger cache, no `REPOACH_DB_PATH`; `doctor --ci` prints
  `ledger not cacheable (outside workspace)`.
- A CI/CD variable `REPOACH_DB_PATH` set by the project outranks both;
  `doctor --ci` prints the path actually in force and whether it is
  cacheable.
- `[state].db_path` changed after `init --ci` -> the rendered file
  differs; `init --ci` reports `differs` until `--force`.
- GitLab runner without a distributed cache: a job landing on another
  runner starts from an empty ledger and reviews from scratch, as Yoob
  documents (Yoob `docs/developer_guide/workflows.md`, "A fix push");
  `doctor --ci` warns (D2).
- The first pipeline of an MR: the ledger is legitimately absent; the
  D2 warning says "declared but not restored" without claiming a fault.
- GitHub `cancel-in-progress` cancels job 1 before the save: job 2
  restores the newest earlier entry by `restore-keys`, or none.
- A PR from a fork: the cache is scoped to that PR's ref by GitHub; the
  key never crosses PRs.

### Failure scenarios

- The cache restore fails or misses -> the job runs exactly as today
  (empty ledger), never fails because of the cache.
- The save fails -> logged by the runner; the review's result is
  unchanged.

## Architecture Impact

- Adds dependency: SP-CI-LEDGER-PERSISTS -> SP-UNIVERSAL-CI-ENTRY
  (edits `ci_entry/snippets.py`, `variables.py`, `cli.py`, owned there).
- Adds dependency: SP-CI-LEDGER-PERSISTS -> SP-FINDINGS-BRIDGE-DOCFIX
  (the replay from `pr_review_runs`, `review/review_cache.py`, is what
  the persisted ledger feeds; read, not edited).
- Removes dependency: none.
- New / changed coupling: `ci_entry` reads `[state].db_path` from
  `repoach.toml` through the existing project-config resolver.

## Diagram

```mermaid
flowchart TD
    A[init --ci] --> B{ledger path in force inside workspace?}
    B -->|yes| C[snippet caches it per PR/MR]
    B -->|no| D[no ledger cache; doctor: not cacheable]
    C --> E[job: restore, ci review, save always]
```

## Acceptance Criteria

- [ ] AC1: with no `[state].db_path`, `render_gitlab_job` output parses
  as YAML and its `review:advisory` job has
  `variables.REPOACH_DB_PATH == ".repoach/repoach.db"` and `cache ==
  {key: "repoach-ledger-mr-$CI_MERGE_REQUEST_IID", paths:
  [".repoach/repoach.db"], when: "always"}` —
  `tests/unit/test_ci_ledger_persists.py`.
- [ ] AC2: `render_github_workflow` output parses as YAML; the
  `review` job has an `actions/cache/restore@<40-hex>` step before
  `Review` and an `actions/cache/save@<40-hex>` step after it with
  `if: always()`, both on the ledger path, the save key containing
  `github.event.pull_request.number`, `github.run_id` and
  `github.run_attempt`, the restore key prefix
  `repoach-ledger-pr-${{ github.event.pull_request.number }}-`.
- [ ] AC3: the GitHub `Review` step's `env:` contains
  `REPOACH_REVIEW_FORCE: ${{ vars.REPOACH_REVIEW_FORCE }}`, and
  `REPOACH_DB_PATH: .repoach/repoach.db` when no `[state].db_path` is
  set.
- [ ] AC4 (D1): with `[state].db_path = "state/ledger.db"` both
  snippets contain no `REPOACH_DB_PATH` and cache `state/ledger.db`;
  with `[state].db_path = "/var/lib/repoach.db"` (or `../x.db`) both
  contain no `REPOACH_DB_PATH` and no ledger cache step or `cache:` key.
- [ ] AC5 (fixture): two simulated jobs of PR 7 at one head, each in a
  fresh workspace, with only the paths the rendered snippet caches
  copied from job 1's workspace into job 2's, and fake reviewers and
  judge that count their calls: job 1 makes ≥1 reviewer call and
  reports `cached: false`; job 2 reports `cached: true`, 0 reviewer
  calls, 0 judge calls, the same verdict.
- [ ] AC6 (fixture): the same as AC5 with `REPOACH_REVIEW_FORCE=1` in
  job 2's environment: job 2 reports `cached: false` and calls the
  reviewers.
- [ ] AC7: `doctor --ci` with `REPOACH_DB_PATH=.repoach/repoach.db`,
  the ledger file present and the generated GitLab job file in the
  checkout prints `ledger=<root>/.repoach/repoach.db (cacheable)`,
  `ledger_present=yes`, `ledger_cache=review:advisory declared`; with an
  absolute path outside the root it prints `ledger not cacheable
  (outside workspace)`; with a job file lacking the cache stanza it
  prints `ledger_cache=review:advisory not declared`.
- [ ] AC8 (D3): a `.gitlab-ci.yml` fixture copied from Yoob's
  `review:gate` (script `bash ci/review-gate.sh`, `REPOACH_*`
  variables, `cache: {key: repoach-ledger-mr-$CI_MERGE_REQUEST_IID,
  paths: [.repoach/], when: always}`) yields `ledger_cache=review:gate
  declared`; a job not naming `repoach` is not listed.
- [ ] AC9 (D2): `CI_PIPELINE_SOURCE=merge_request_event`, a repoach job
  declaring the ledger cache, the ledger absent -> the warning line on
  stderr and exit 0; the ledger present -> no warning.
- [ ] AC10: `repoach init --ci gitlab` on a checkout holding the
  previous rendering reports `differs` (and `--force` rewrites it):
  the existing `plan_ci_init` contract holds for the new content.
- [ ] AC11 (measured, recorded in the PR): on one GitLab MR using the
  generated job, two pipelines at the same head; the second job's log
  shows the cache restored, `review: cached at <sha7>` and `usage: 0
  calls`. Job URLs in the PR description.

## Open Questions

(none)
