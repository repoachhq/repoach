# SP-CI-SECRETS-ISOLATION — stop executing PR-authored code with provider secrets in the environment

## Metadata

- **Status**: OPEN
- **Priority**: P2 (becomes P0 the day a second contributor gets push access)
- **Owner**: operator
- **Executor**: hand-implemented (touches `.github/workflows/` — bot whitelist forbids it)
- **Opened**: 2026-06-11

## Why

The audit's only security-CRITICAL: in
`.github/workflows/auto-review.yml`, jobs that hold all six provider
keys (`FEROVA_NVIDIA_NIM_API_KEY`, `FEROVA_OPENROUTER_API_KEY`,
`FEROVA_DEEPSEEK_API_KEY`, `FEROVA_KIMI_API_KEY`, `FEROVA_GROQ_API_KEY`,
`FEROVA_CEREBRAS_API_KEY`) and — in `auto_fix` — a `contents: write`
`GITHUB_TOKEN` also **execute PR-authored code**: `pip install -e`
runs the PR's build backend, `pytest` imports the PR's `conftest.py`,
and the review CLI itself is the PR's own package. This runs on
`opened`/`synchronize`, *before* any verdict. A malicious same-repo
branch exfiltrates every key and can push with the write token.

Safe today only because the repo is solo and private (fork PRs get no
secrets). The fix should land before the threat model changes, not
after. Verified clean already: no `pull_request_target`, no `${{ }}`
injection of PR title/body/branch into `run:` blocks.

## What

Defence in depth, three layers in `auto-review.yml`:

1. **Actor gate** — every job that mounts provider secrets or a write
   token gets
   `if: github.actor == github.repository_owner` (extend to an
   explicit allowlist if collaborators are ever added). Untrusted
   actors still get lint/test CI (`ci.yml`, secret-free), just no
   bot team.
2. **Trusted-tool installs** — in the `review` job, install
   `ferova` from the **base ref** (`develop`), not from the PR
   merge ref: `actions/checkout` the base into the workdir, fetch the
   PR diff/metadata via `gh` (the orchestrator already consumes the
   diff as data, and `GhCli.pr_diff` has the local-git fallback). PR
   file contents reach the bots as *data* (diff text, file excerpts),
   never as *imported code*. The PR head may be checked out into a
   separate directory for excerpt reading only — no `pip install`, no
   `pytest`, no imports from it in this job.
3. **auto_fix containment** — `auto_fix` must check out and test PR
   code by design (Coder gates run `ruff`/`pytest` on the fixed tree),
   so bind it to a GitHub **Environment** (e.g. `bots`) holding the
   provider secrets, with the actor gate from layer 1; optionally add
   a required-reviewer protection on that Environment later. Document
   in the workflow header *why* this job is the residual trust grant.

Also: scrub any remaining job that exports secrets it does not use
(audit noted the full key block is mounted wholesale).

## Files in scope

- `.github/workflows/auto-review.yml`

## Out of scope

- `ci.yml` (already secret-free for tests; its smoke step is
  SP-CI-SMOKE-REPAIR).
- Restructuring the review factory to run reviewers without a local
  package install (base-ref install achieves the isolation).
- GitHub branch-protection / repo-settings changes (operator console
  work, not repo code) — note them in the PR description as follow-up
  ops: create the `bots` Environment and move the six secrets into it.

## Smoke scenario

### Setup

A scratch PR from a feature branch (operator-authored) against
`develop`, plus — for the negative case — a PR whose head commits a
`conftest.py` that would `print` a canary env var at import time.

### Execute

Let `auto-review.yml` run on both PRs; inspect the run logs.

### Expected

Operator PR: review/auto_fix/auto_merge behave as before (verdicts
posted, archive upserted). Canary PR: the `review` job log shows the
canary `conftest.py` was never imported (no canary output anywhere in
the secret-bearing jobs' logs); the canary only executes in `auto_fix`
(by design, actor-gated) or in secret-free `ci.yml`.

## Definition of Done

- Every job mounting `FEROVA_*` provider keys or `contents: write`
  carries the actor gate.
- The `review` job installs ferova from the base ref and never
  imports/installs/executes PR-head code; PR content enters as diff
  data only.
- `auto_fix` references the `bots` Environment and keeps the actor
  gate.
- A real operator PR run is green end-to-end (review → verdict →
  archive) before merge; the canary check from the smoke scenario has
  been performed once.
- `actionlint`/`shellcheck` (CI shellcheck workflow) green.

## Commit plan

1. `fix(workflow): actor-gate all secret-bearing review jobs`
2. `fix(workflow): review job installs ferova from base ref, PR code as data only`
3. `chore(workflow): bind auto_fix to the bots environment + document residual trust`

## Risks

- **Base-ref tooling vs PR-head expectations**: a PR that changes the
  review factory itself is then reviewed by the *old* factory — this is
  the safe direction (the PR cannot tamper with its own reviewer), but
  factory-behaviour PRs may see stale-tool verdicts; the existing
  hand-ship convention for sensitive paths covers this.
- **Workflow edits are themselves the most dangerous file in the
  repo**: hand-ship, review the diff line by line, and verify on a
  scratch PR before trusting it.
