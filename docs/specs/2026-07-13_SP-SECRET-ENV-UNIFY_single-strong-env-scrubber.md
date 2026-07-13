---
id: SP-SECRET-ENV-UNIFY
title: Unify the child-process env scrubber on the strong marker-based implementation
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

# Unify the child-process env scrubber on the strong marker-based implementation

## Intent

Stop leaking the operator's credentials into the `claude` CLI child that reads
untrusted repository content. The planner's local scrubber only strips
`FEROVA_*`, so `GITHUB_TOKEN`, `GH_TOKEN`, and every legacy unprefixed provider
key still reach a subprocess that ingests arbitrary repo text. Replace it with
the repo's stronger marker-based scrubber and widen the marker set.

## Context

`src/ferova/review/planner_cc.py:53-62` `_scrubbed_env()` returns
`{k: v for ... if not k.startswith("FEROVA_")}`. It is the `env=` for the
read-only `claude` CLI exploration subprocess (`child_env` at
`planner_cc.py:135`, spawned with `_CC_READ_ONLY_TOOLS`). Because the filter is
prefix-only, `GITHUB_TOKEN` / `GH_TOKEN` and the legacy unprefixed provider
names (`OPENROUTER_API_KEY`, `NVIDIA_NIM_API_KEY`, `ANTHROPIC_AUTH_TOKEN`)
survive into a child that reads attacker-influenceable repo content.

The repo already carries the stronger `src/ferova/review/secret_env.py:24-46`
`scrubbed_env()`, which strips every name containing `TOKEN` / `KEY` /
`SECRET` / `PASSWORD` / `PASSWD` / `CREDENTIAL` (`_SECRET_ENV_MARKERS`,
`secret_env.py:24-31`). `secret_env` is a leaf module (imports only `os`), so
`planner_cc` importing it introduces no cycle. Audit 2026-07-13 findings H7
(agent code can read secrets) and H8 (weak scrubber).

## Goals

- G1: `planner_cc` builds the child env through
  `secret_env.scrubbed_env()`; the local prefix-only `_scrubbed_env` filter is
  removed (or reduced to a thin delegator kept only if a call site imports the
  name — see A2).
- G2: `_SECRET_ENV_MARKERS` is broadened to also catch `*_AUTH*`, `BEARER*`,
  and `DATABASE_URL`, so `ANTHROPIC_AUTH_TOKEN`, bearer-style variables, and the
  ledger DSN are stripped from every subprocess that runs agent-authored or
  untrusted content.
- G3: non-secret config the CLI needs (`HOME`, `PATH`, `PYTHONPATH`,
  `FEROVA_DB_PATH`, `FEROVA_CODER_PYTHONS`) is preserved.

## Non-Goals

- NG1: H7 residual — agent/CLI code can still read `.env` and `~/.ssh` off the
  filesystem; env-scrubbing does not and cannot contain filesystem reads. This
  is an explicitly documented residual, OUT OF SCOPE here; a filesystem jail is
  a separate, larger effort.
- NG2: no change to what tools the CLI is granted (`_CC_READ_ONLY_TOOLS`) or its
  timeout.
- NG3: no new module — the strong scrubber already exists and is owned.

## Assumptions

- A1: `secret_env.scrubbed_env()` (marker-based) is a strict superset of the
  planner's prefix filter for secret-bearing names, so the swap only widens
  what is stripped.
- A2: adding `DATABASE_URL` and `BEARER*`/`*_AUTH*` markers does not strip any
  name the `claude` CLI requires to run (verified against the preserved set in
  G3).

## Interface

N/A (in-place fix). The public surface of `secret_env.scrubbed_env()` is
unchanged; only its marker tuple grows. `planner_cc._scrubbed_env` is removed or
becomes a one-line delegator to `secret_env.scrubbed_env`.

## Behavior

### Nominal

`planner_cc` spawns the `claude` CLI with
`env=secret_env.scrubbed_env()`: every secret-marked variable is absent, all
non-secret config remains.

### Edge cases

- A variable named e.g. `MY_AUTH_MODE` (benign but matching `*_AUTH*`) is
  stripped. This is accepted: fail CLOSED — over-stripping a benign var is
  strictly safer than leaking a credential, and no such name is on the CLI's
  required set (A2).
- `GH_TOKEN` / `GITHUB_TOKEN` — stripped (contain `TOKEN`).

### Failure scenarios

- If a future secret-bearing name evades the markers, the failure mode is a
  narrower leak than today, never a broader one. The scrubber is fail-closed by
  construction (allowlist-of-shape via denylist markers on a copy of environ).

## Architecture Impact

- Adds dependency: none new at the spec-graph level — in-place modification of
  `planner_cc.py` and `secret_env.py` (both owned by existing specs). The new
  intra-package import (`review.planner_cc` -> `review.secret_env`) stays inside
  one owner package and creates no cross-owner edge or cycle (`secret_env`
  imports only `os`).
- New / changed coupling, cycles, or shared state: none.

## Diagram

N/A (in-place fix).

## Acceptance Criteria

- [ ] AC1: unit — `secret_env.scrubbed_env()` over a constructed environ
  (patched via `monkeypatch.setenv` on real `os.environ`, not by faking Ferova
  code) removes `GITHUB_TOKEN`, `GH_TOKEN`, `OPENROUTER_API_KEY`,
  `NVIDIA_NIM_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `DATABASE_URL`, and a
  `BEARER_TOKEN`-style name, while preserving `PATH`, `HOME`, and
  `FEROVA_DB_PATH`.
- [ ] AC2 (INTEGRATION): invoke the planner's real env-building path —
  `planner_cc._scrubbed_env()` (or its replacement call to
  `secret_env.scrubbed_env`) with `monkeypatch.setenv` populating
  `GITHUB_TOKEN`, `OPENROUTER_API_KEY`, and a benign `FEROVA_DB_PATH`; assert
  both secrets are absent from the returned dict and the benign var is present.
  No monkeypatching of Ferova functions — only the real environ is set.
- [ ] AC3: promised test file + selectors —
  `tests/unit/test_planner_cc_scrub.py::test_child_env_strips_github_and_provider_keys`
  and `::test_child_env_keeps_benign_config`; marker-set unit lives in
  `tests/unit/test_secret_env.py::test_markers_cover_auth_bearer_database_url`.
- [ ] AC4: `ruff` + `ruff format --check` + `pytest tests/unit` green; zero
  inline comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`;
  `ferova arch graph --check` exits 0.

## Open Questions

(none)
