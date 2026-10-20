---
id: SP-GH-GLAB-SHIM
title: Disposable gh-to-glab read shim for a GitLab dry-run review (W1 measurement)
version: 0.1
status: draft
author: agent
created: 2026-10-18
updated: 2026-10-18

owns:
  code: [scripts/gh_glab_shim.py, tests/unit/test_gh_glab_shim.py]
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Disposable gh-to-glab read shim for a GitLab dry-run review

## Intent
Measure, with zero change to `src/repoach`, how far repoach is from reviewing
a GitLab merge request: an executable named `gh`, first on `PATH`, answers
the read calls `repoach review pr <iid> --dry-run` makes from `glab`, and
refuses every write. The measurement, not the shim, is the deliverable; the
shim is deleted once the `Forge` protocol (W2) exists.

## Context
`review/gh_client.py` resolves `gh` with `shutil.which` (line 155) and every
forge read goes through it. The production plan
(`docs/plans/2026-10-18_yoob-production-plan.md`, worksite W1) names Yoob
MR !283 as the target. Results:
`docs/plans/2026-10-18_W1-gitlab-dry-run-measurement.md`.

## Goals
- G1: `pr diff`, `pr view --json`, `api …/pulls/N/comments`,
  `api …/pulls/N/reviews/<id>/comments` and `api …/issues/N/comments`
  answer in GitHub's shape from GitLab.
- G2: every write exits 70 without calling `glab`.
- G3: each call is logged (`GH_GLAB_SHIM_LOG`) so the run lists what it used.

## Non-Goals
- NG1: posting, merging, CI status, or any change under `src/repoach`.
- NG2: a `Forge` protocol (W2).

## Assumptions
- A1: `glab` is authenticated and the cwd is a clone whose remote is the
  GitLab project (`glab api projects/:id/...` resolves from it).

## Interface
Inputs:
- `argv`: list[str] — the arguments `gh` would receive.

Outputs:
- stdout/stderr/exit code of the emulated `gh` call.

Errors:
- exit 70: a write was requested; exit 64: an unsupported call.

## Behavior

### Nominal
- `pr diff N` -> `glab api projects/:id/merge_requests/N/raw_diffs`.
- `pr view N --json f1,f2` -> `glab api projects/:id/merge_requests/N`,
  fields mapped (`iid`, `description`, `source_branch`, `target_branch`,
  `sha`, `author.username`, `state`, `web_url`).
- `pulls/N/comments` -> `DiffNote` notes of the MR discussions.

### Edge cases
- `--slurp` wraps the list in one page; concatenated pages are merged.
- `pulls/N/reviews/<id>/comments` -> `[]` (GitLab has no review object).

### Failure scenarios
- `glab` fails -> exit 1 with its stderr; repoach then falls back as it
  would on a failed `gh`.

## Architecture Impact
- Adds dependency: none (a script outside the package).
- New / changed coupling, cycles, or shared state: none.

## Diagram
N/A — one dispatch function.

## Acceptance Criteria
- [ ] AC1: `tests/unit/test_gh_glab_shim.py` feeds the five read argv shapes and
  the write shapes through a fake `glab` and checks the field mapping.
- [ ] AC2: `repoach review pr 283 --dry-run` from the Yoob checkout, shim on
  `PATH`, exits 0 with four reviews and zero posts.

## Open Questions
- Q1: Deleted once W2's `GitLabForge` lands; nothing should import it.
