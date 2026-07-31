---
id: SP-BRANCH-CONFIG
title: Configurable integration + release branch names (drop the hardcoded develop/main literals)
version: 0.1
status: approved
author: agent
created: 2026-07-31
updated: 2026-07-31

owns:
  code: [tests/unit/test_branch_config.py]
  resources: N/A

depends_on: [SP-CONFIG-ENV-ANCHOR, SP-RELEASE-GATE, SP-DEDUP-CLASSIFICATION-CONSTANTS, SP-DEV-STEP-PREFLIGHT, SP-REVIEW-POST-BATCH]
provides_to: []

constraints: {}
---

# Configurable integration + release branch names

## Intent

The two-branch model — `develop` (integration) and `main` (release) — is
baked into the factory as bare Python string literals, including two hard
refusals (`if base != "develop": <no-op>`). A third party whose repo uses a
single `main`, or `staging`/`master`, gets **silent no-op refusals** from the
Coder and the auto-merge gate with no indication why. Make the two branch
names come from configuration (`REPOACH_INTEGRATION_BRANCH`,
`REPOACH_RELEASE_BRANCH`) with the current values as defaults, so a fresh
clone behaves identically while a differently-branched repo just sets two env
vars.

## Context

Hardcoded literals found (grep 2026-07-31):
- **Refusals (the blocker):** `src/repoach/review/auto_merge.py:822`
  (`if base != "develop"`) and `src/repoach/review/coder_findings.py:488`
  (`if base != "develop"`) — both silently no-op when the base is not
  literally `develop`.
- **Release model:** `src/repoach/review/release_gate.py` —
  `rev-parse develop` (:244), `ls-remote origin develop` (:269),
  `fetch origin main develop` (:416, :455), `_ls_remote_sha(gh, "main")`
  / `(gh, "develop")` (:419, :458-459). These encode `main`=release,
  `develop`=integration.
- **Defaults:** `base: str = "develop"` in `dev_runner.ensure_branch`
  (:298), `_develop_one_spec` (:1854, :1998), `devagent_selfverify` (:443),
  `cli/main.py` `--base` option (:34), `review_cmds` develop command (:381);
  and `gh_client.py:383` `base: str = "main"` (the release-PR base).
- No `Settings` field for branches exists today.

## Goals

- G1: two new `Settings` fields (`core/config.py`) —
  `integration_branch: str = "develop"` (alias
  `REPOACH_INTEGRATION_BRANCH`) and `release_branch: str = "main"`
  (alias `REPOACH_RELEASE_BRANCH`) — following the existing
  `validation_alias=_aliases(...)` pattern.
- G2: the two refusal sites (`auto_merge.py:822`,
  `coder_findings.py:488`) compare against `get_settings().integration_branch`
  instead of the literal `"develop"`, and their no-op reason string names
  the configured branch (so a mismatch is legible, not silent).
- G3: `release_gate.py`'s develop/main git refs are sourced from
  `integration_branch` / `release_branch` — every `rev-parse`/`ls-remote`/
  `fetch` uses the configured names; the sanctioned-shape logic is
  otherwise unchanged.
- G4: the `base="develop"` defaults in `dev_runner` / `devagent_selfverify`
  / the CLI `--base` option, and the `base="main"` default in
  `gh_client.open_pr`-style helpers, default to the configured
  `integration_branch` / `release_branch` respectively (resolved at call
  time, not frozen at import, so a test/env override takes effect).

## Non-Goals

- NG1: defaults stay `develop` / `main` — a fresh clone with no env override
  behaves byte-for-byte as today; no workflow (`.github/*`) change here.
- NG2: no change to the branch-detection regex
  (`detect_spec_from_branch`, `feat/sp-<id>-impl`) or feature-branch naming —
  only the integration/release base names become configurable.
- NG3: no multi-branch / multi-environment model — exactly two configurable
  names, same topology as today.
- NG4: no change to the sanctioned-shape / fast-forward / merge-commit
  comparison logic in `release_gate` (SP-RELEASE-GATE / SP-RELEASE-SANCTIONED-
  DEVELOP-MERGE) beyond which ref names it reads.

## Interface

`src/repoach/core/config.py` — two fields on `Settings`:

```python
integration_branch: str = Field(default="develop", validation_alias=_aliases("INTEGRATION_BRANCH"))
release_branch: str = Field(default="main", validation_alias=_aliases("RELEASE_BRANCH"))
```

Refusal sites read `get_settings().integration_branch`; release_gate reads
both; default-argument sites resolve the setting inside the function body
(e.g. `base = base if base is not None else get_settings().integration_branch`,
signature default `None`) so overrides are honoured.

## Behavior

### Nominal
- No env override: `integration_branch == "develop"`,
  `release_branch == "main"` — every path behaves as today.
- `REPOACH_INTEGRATION_BRANCH=trunk`: the Coder / auto-merge gate accept a
  PR based on `trunk` and refuse others, naming `trunk` in the reason.

### Edge cases
- Only `REPOACH_RELEASE_BRANCH` set (integration left default): release_gate
  verifies the configured release branch against `develop`'s shape.

### Failure scenarios
- An unset/empty value falls back to the default (`develop`/`main`) — the
  field default handles it; no fail-loud needed (unlike the proxy URL).

## Acceptance Criteria

- [ ] AC1: `Settings().integration_branch == "develop"` and
  `.release_branch == "main"` by default; `REPOACH_INTEGRATION_BRANCH=trunk`
  / `REPOACH_RELEASE_BRANCH=release` override them.
- [ ] AC2: with `integration_branch="trunk"`, the `auto_merge` and
  `coder_findings` refusal accept `base="trunk"` and refuse `base="develop"`,
  naming `trunk` in the no-op reason — the inverse of pre-change behavior.
- [ ] AC3: `release_gate`'s verify path issues its git ref commands against
  the configured integration/release names (spy the git runner, assert the
  ref args follow the settings, not literal develop/main).
- [ ] AC4: a default-argument site (e.g. `ensure_branch`) uses the configured
  `integration_branch` when no base is passed.
- [ ] AC5: promised test `tests/unit/test_branch_config.py` with the above
  selectors; each fails on pre-change code (literal comparison / no field).
  Existing auto_merge / coder / release_gate / dev_runner tests stay green
  (defaults unchanged, NG1).

## Architecture Impact

- Adds two `Settings` fields; routes ~20 literal sites through them. No new
  module, no topology change. The two-branch model stays; only its names
  become configuration — the last hardcoded-assumption blocker for a repo
  that doesn't happen to use `develop`/`main`.
