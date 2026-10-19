---
id: SP-UNIVERSAL-CI-ENTRY
title: review pr and review gate from any CI with one semantics; the human-merge race ends in ALREADY_MERGED
version: 0.1
status: draft
author: agent
created: 2026-10-18
updated: 2026-10-18

owns:
  code: [src/repoach/ci_entry/, tests/unit/test_ci_entry.py, tests/unit/test_merge_race.py]
  resources: []

depends_on: [SP-UNIVERSAL-FORGE, SP-UNIVERSAL-PROVIDERS, SP-UNIVERSAL-PROJECT-CONFIG, SP-MERGE-EXIT-CONTRACT, SP-AUTOMERGE-EVENT-DRIVEN, SP-CI-SECRETS-ISOLATION, SP-VERIFIER-PR-TREE]
provides_to: []

constraints: {}
---

# review pr and review gate from any CI with one semantics

## Intent

`repoach review pr` and `repoach review gate` behave identically from a
shell, GitHub Actions, GitLab CI or cron: the PR number is resolved
from the environment when omitted, posting is explicit, the bot
identity is per forge, the exit contract is one table, and a PR merged
by a human while the bot runs ends in `ALREADY_MERGED` instead of a red
job.

## Context

- Measured 2026-10-18 on repoach's last 60 `auto-review` runs
  (2026-07-24 → 08-01): 7 success, 53 failure; in every sampled failure
  the `review` job succeeded and the red step is `auto_fix` → "Checkout
  PR head (writable)" (`.github/workflows/auto-review.yml:341`) or
  `auto_merge` → "Run auto-merge" (`:685`). Cause: the owner's local
  `scripts/safe_merge.sh` merged first and deleted the head branch
  (`safe_merge.sh:255-256`); PR #200 merged 01:09:18Z, the job started
  01:22:33Z and failed in 11 s. `merge-on-ci.yml`: 39/15, all failures
  at "Materialize the PR head tree" for the same reason.
- `auto_merge.py` already has `OUTCOME_ALREADY_MERGED` and
  `NON_FATAL_SKIP_OUTCOMES` (`:80-95`), but the checkout happens in
  YAML before any Python runs.
- The workflows inject everything per job: `GH_TOKEN`,
  `REPOACH_LLM_PROXY_BASE_URL`, `REPOACH_DB_PATH`,
  `REPOACH_CODER_PYTHONS` (`auto-review.yml:440-450`); a GitLab job has
  none of these and provides `CI_MERGE_REQUEST_IID`, `CI_PROJECT_ID`,
  `CI_JOB_TOKEN`, `GITLAB_TOKEN` as a masked variable.
- `review pr` posts unless `--dry-run` (`cli/review_cmds.py:66-125`);
  there is no `--post` flag, so a CI job that wants advisory output
  must remember the flag.
- `review_bot_login` is global (`core/config.py:223`); a GitLab bot is a
  project access token user, Yoob's would be `josephwfaye` or a token
  user.

## Goals

- G1: `src/repoach/ci_entry/resolve.py`: `resolve_pr_number(explicit: int | None, env: Mapping[str, str]) -> tuple[int, str]` returns `(number, source)` in order: explicit argument; `REPOACH_PR_NUMBER`; GitLab `CI_MERGE_REQUEST_IID`; GitHub `GITHUB_EVENT_PATH` (`pull_request.number`, `issue.number` when `issue.pull_request` is set, `inputs.pr_number`) then `GITHUB_REF` `refs/pull/<n>/merge`; else `NoPullRequestError`, rc 2. The PR argument of `review pr`, `report`, `fix`, `merge`, `gate`, `insights` becomes optional.
- G2: posting is explicit: `--post` posts, `--dry-run` never posts, neither flag → `[review].post_by_default` (default `false` on a foreign repo, `true` in repoach's own `repoach.toml`); the JSON always carries `posted_comments`, `posted_reviews`, `mode`.
- G3: bot identity per forge: `[review].bot_username` (`SP-UNIVERSAL-FORGE`); `review pr --post` refuses with rc 2 when it is empty on GitLab (`bot username required to trust the archive marker`).
- G4: the race: before any write or writable checkout, `forge.pr_state(n)` is read; `merged`/`closed` → the command ends with outcome `ALREADY_MERGED`/`CLOSED`, rc 0, one line, no checkout. `review fix` performs its own writable checkout by `head_sha` (`git fetch origin <sha>`), never by branch name, and re-checks `pr_state` after the fix before pushing; a push rejected because the branch is gone ends `ALREADY_MERGED`. `review merge` on a merged PR ends `ALREADY_MERGED` rc 0 (today's outcome, kept).
- G5: one exit-code table for the six commands, printed by `repoach review exit-codes` and pinned by a test: 0 success/non-fatal skip, 1 internal error, 2 configuration/provider/forge unavailable, 3 no fixes possible, 4 nothing to fix, 5 gate refused, 6 no PR resolvable, 9 placeholders rejected.
- G6: shipped CI snippets rendered by `repoach init --ci github|gitlab --print`: a GitHub Actions reusable workflow (`repoach/review@<tag>` style, inputs: `post`, `provider`, secrets by name) and a GitLab CI job (`merge_request_event`, `allow_failure: true`, `tags` left to the project); `--print` writes to stdout only — `init` never writes under `.github/` or into `.gitlab-ci.yml`.
- G7: all inputs are environment or config; no step of either snippet runs anything but `pipx install repoach==<version>` and `repoach review pr --post`.

## Non-Goals

- NG1: no change to repoach's own `auto_fix`/`auto_merge` job topology beyond making their steps call the CLI with no PR argument and dropping the YAML writable checkout (umbrella migration step 2).
- NG2: no new event lane; `merge-on-ci.yml` stays and keeps running `main`'s code (umbrella NG2).
- NG3: no secret management; tokens are the CI's masked variables.
- NG4: no retry of a red provider from CI; a provider error is rc 2 and the job's own `allow_failure` decides.

## Assumptions

- A1: inside GitLab CI, `CI_JOB_TOKEN` can read the MR and its pipeline; posting needs `GITLAB_TOKEN` (project access token with `api`).
- A2: inside GitHub Actions, `GITHUB_TOKEN` reads and writes PR comments; the fork case stays the maintainer's hop (CONTRIBUTING).

## Interface

```python
def resolve_pr_number(explicit: int | None, env: Mapping[str, str]) -> tuple[int, str]: ...
def posting_mode(post: bool, dry_run: bool, config: ProjectConfig) -> Literal["post", "dry-run"]: ...
def preflight_write(forge: Forge, number: int) -> PreflightResult: ...
```

Inputs:
- `REPOACH_PR_NUMBER`, `CI_MERGE_REQUEST_IID`, `GITHUB_EVENT_PATH`, `GITHUB_REF`: str — resolution sources.
- `--post` / `--dry-run`: flags, mutually exclusive.

Outputs:
- JSON on stdout for `review pr`/`gate`/`merge`: adds `pr_source`, `mode`, `outcome`; unchanged keys keep their names (the `tests/unit/test_review_ci_mode.py` pin).
- `repoach review exit-codes`: the table above.

Errors:
- `NoPullRequestError`: rc 6, names the variables it looked at.
- `PostingRefusedError`: rc 2, reason (`bot username required`, `token lacks api scope`).

## Behavior

### Nominal
GitLab job: `pipx install repoach==0.2.0 && repoach review pr --post`
→ `CI_MERGE_REQUEST_IID` resolves the MR, `GITLAB_TOKEN` posts one
discussion per inline comment and one archive note, JSON on stdout,
rc 0 or 5. GitHub job: identical command, `GITHUB_EVENT_PATH` resolves
the PR. Shell: `repoach review pr 283 --dry-run`.

### Edge cases
- `issue_comment` event on a plain issue → rc 6 `not a pull request`, no provider call (today's YAML `if:` guard, now in code).
- `--post` and `--dry-run` together → rc 2 before anything.
- head branch deleted but PR still `open` (force-deleted) → `preflight_write` ends `SKIP_HEAD_MISSING`, rc 0.
- cron with `REPOACH_PR_NUMBER` unset → `review pr` iterates `forge.open_pull_requests(integration_branch)` only with `--all-open`; otherwise rc 6.

### Failure scenarios
- `pr_state` call itself fails (network) → rc 2, nothing posted, nothing checked out; the ledger records `preflight_failed`.
- the PR merges between `preflight_write` and the push of a fix → the push fails on the missing ref; the command ends `ALREADY_MERGED`, rc 0, the local fix commit is left in the job's workspace and logged.

## Architecture Impact
- Adds dependency: SP-UNIVERSAL-CI-ENTRY -> SP-UNIVERSAL-FORGE (`pr_state`, `open_pull_requests`, posting)
- Adds dependency: SP-UNIVERSAL-CI-ENTRY -> SP-UNIVERSAL-PROVIDERS (provider resolved per run, rc 2 contract)
- Adds dependency: SP-UNIVERSAL-CI-ENTRY -> SP-UNIVERSAL-PROJECT-CONFIG (`[review].post_by_default`, `bot_username`)
- Adds dependency: SP-UNIVERSAL-CI-ENTRY -> SP-MERGE-EXIT-CONTRACT (the exit table extends it, no code changes meaning)
- Adds dependency: SP-UNIVERSAL-CI-ENTRY -> SP-AUTOMERGE-EVENT-DRIVEN (`merge-on-ci` keeps its lane; its "Materialize" step becomes `repoach review gate` with the preflight)
- Adds dependency: SP-UNIVERSAL-CI-ENTRY -> SP-CI-SECRETS-ISOLATION (the snippets keep secrets on the posting job only)
- Adds dependency: SP-UNIVERSAL-CI-ENTRY -> SP-VERIFIER-PR-TREE (its `gh pr view --json headRefName` resolution is replaced by `pull_request().head_sha` inside the CLI)
- Removes coupling: the writable checkout leaves YAML; `.github/workflows/auto-review.yml` shrinks to env + one CLI call per job.

## Diagram
```mermaid
flowchart TD
    A[shell / GH Actions / GitLab CI / cron] --> B[resolve_pr_number]
    B --> C[preflight_write: pr_state]
    C -- merged/closed --> D[ALREADY_MERGED rc 0]
    C -- open --> E[review pr / fix / merge / gate]
    E --> F[JSON + exit code table]
```

## Acceptance Criteria
- [ ] AC1: `pytest -q tests/unit/test_ci_entry.py` → green: resolution order pinned for the five sources; `--post --dry-run` → rc 2; GitLab with empty `bot_username` and `--post` → rc 2.
- [ ] AC2: `pytest -q tests/unit/test_merge_race.py` → green with a fake forge whose `pr_state` flips to `merged` between preflight and push: `review fix` ends `ALREADY_MERGED`, rc 0, no `ForgeError` escapes; `review merge` on `merged` → `ALREADY_MERGED`, rc 0.
- [ ] AC3: from `yoob_2`: `CI_MERGE_REQUEST_IID=283 repoach review pr --dry-run` → JSON `pr_source == "CI_MERGE_REQUEST_IID"`, `mode == "dry-run"`, `posted_comments == 0`.
- [ ] AC4: `GITHUB_EVENT_PATH=tests/fixtures/ci_entry/pull_request.json repoach review gate` in repoach's checkout → JSON `pr_source == "GITHUB_EVENT_PATH"` and the same `facts` as `repoach review gate <n>`.
- [ ] AC5: `repoach review exit-codes` → eight lines; `grep -c "exit" .github/workflows/auto-review.yml` after the migration ≤ 6 (the YAML no longer re-maps exit codes beyond `allow`/`fail`).
- [ ] AC6: `repoach init --ci gitlab --print | yq '.["review:advisory"].rules[0].if'` → contains `merge_request_event`; `repoach init --ci github --print | yq '.on.pull_request.types'` → lists `opened`; `git status --short` empty after both.
- [ ] AC7: on repoach's own repository after the migration, 20 consecutive `auto-review` runs where the owner merges locally during the run show zero red `auto_fix`/`auto_merge` jobs (`gh run list --workflow auto-review.yml --limit 20 --json conclusion`).

## Open Questions
- Q1: should the GitHub reusable workflow live in this repository (`.github/workflows/review.yml` with `workflow_call`) or in a separate `repoachhq/review-action` repository? Recommend here, versioned by tag, until a second consumer exists.
