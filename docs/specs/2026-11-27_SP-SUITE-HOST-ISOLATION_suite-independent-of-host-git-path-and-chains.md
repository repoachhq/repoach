---
id: SP-SUITE-HOST-ISOLATION
title: The test suite is independent of the host's git, PATH and chain heads
version: 0.1
status: approved
author: jfaye
created: 2026-11-27
updated: 2026-11-27

owns:
  code:
    - scripts/skew_replay.sh
  resources: []

depends_on:
  - SP-UNIVERSAL-INSTALL
  - SP-LEDGER-FORMAT-IS-VERSIONED
  - SP-CHAIN-STATUS-DIGEST
provides_to: []

constraints: {}
---

# The test suite is independent of the host's git, PATH and chain heads

## Intent

Thirteen tests fail on `develop` (`dfe3d67`, still at `e5dbc20`) on a
host whose temporary directory sits inside a git checkout, whose `PATH`
holds a `git` wrapper that writes advice to stderr, and whose installed
`repoach` is not the tree under test. One of them fails on every host
since `chains.env` heads every tier with `anthropic/…` (#321). None of
the asserted behaviours is wrong; each test reads a host fact it does
not pin. This spec pins those facts and keeps every assertion as it is.

## Context

- `tests/unit/test_universal_install.py` (11 tests): "outside a
  repository" is a directory under `tmp_path`. With `TMPDIR` inside a
  git working tree, `git rev-parse --show-toplevel` from that directory
  returns the enclosing tree, so `project_root()` and
  `require_project_root()` find a repository and the rc 2 refusals of
  `doctor`, `init` and the project-scoped commands never happen.
- `tests/unit/test_ledger_format_skew.py::test_t9_skew_replay_names_a_ref_that_does_not_resolve`:
  `scripts/skew_replay.sh` resolves a ref with
  `git rev-parse --verify --quiet`, whose own stderr is empty, then
  prints its one-line refusal. The probe's stderr is not redirected, so
  any `git` on `PATH` that writes to stderr on a failed probe adds lines
  before that refusal, and stderr is no longer exactly one line.
- `tests/integration/test_credits_integration.py::test_chain_status_end_to_end_degraded_environment`:
  `chain-status` prints `no probes in window` only for a NIM head; any
  other head short-circuits to `UNMONITORED` (`docs/tech_debt.md` row
  111). Since #321 `chains.env` heads every tier with `anthropic/…`. The
  unit twin in `tests/unit/test_chain_status.py` pins NIM heads through
  `MODEL_*`; this integration test was not updated. It also runs the
  first `repoach` on `PATH`, which can be another install than the tree
  under test.

## Goals

- G1: the `test_universal_install.py` tests stop git discovery at
  `tmp_path` (`GIT_CEILING_DIRECTORIES`), so "outside a repository"
  holds wherever the base temporary directory is.
- G2: `skew_replay.sh` discards the stderr of its ref probe; its stderr
  on an unresolvable ref is exactly its own refusal line, as D5 of
  SP-LEDGER-FORMAT-IS-VERSIONED states. Exit codes are unchanged.
- G3: the `chain-status` integration test pins the three tier heads to
  NIM refs, as its unit twin does, and runs `python -m repoach` with
  the interpreter running the suite.

## Non-Goals

- NG1: no change to `project_root`, `require_project_root`, the
  `chain-status` rendering, or the NIM-only probe rule (row 111 stays
  open).
- NG2: no assertion is relaxed or removed.

## Acceptance

- A1: the thirteen tests pass with `TMPDIR` inside a git checkout and a
  stderr-writing `git` wrapper first on `PATH`, and with `TMPDIR=/tmp`.
- A2: `scripts/ci_local.sh` is green.
