---
id: SP-RELEASE-GATE-ORIGIN-MAIN-WINDOWING
title: Fetch origin/main and origin/develop before verify_release's stale-ref shape checks
version: 0.1
status: approved
author: agent
created: 2026-07-24
updated: 2026-07-24

owns:
  code: []
  resources: []

depends_on: [SP-RELEASE-VERIFY-MERGE-COMMIT]
provides_to: []

constraints: {}
---

# Fetch origin/main and origin/develop before verify_release's stale-ref shape checks

## Intent

`verify_release` correctly reads the LIVE `main` tip SHA via
`git ls-remote origin main` (a real network call), but then evaluates
the merge-commit shape (`origin/main`'s second parent, and the
`main..develop` distance) against the LOCAL, possibly-stale
remote-tracking refs `origin/main`/`origin/develop`. If the operator's
working clone has not fetched since before the release merge landed,
`verify_release` silently misjudges a perfectly sanctioned merge as
unverified (or worse, could match a stale-but-coincidentally-equal
ref). Have `verify_release` fetch `origin main develop` itself before
any `rev-parse`/`rev-list` against those refs, so correctness never
depends on the operator remembering a manual fetch first.

## Context

Finding #18 (implementable-findings sweep), re-verified live against
`origin/develop` on 2026-07-31 — the bug is still present, unchanged:

- `src/repoach/review/release_gate.py` has no `git fetch` call
  anywhere in the file (confirmed by grep across the whole module and
  `gh_client.py` — the only `fetch` hits are the unrelated
  `fetch_merged_pr_shas` ledger helper and `pr_diff_fallback`'s own
  internal `git fetch` for PR-diff computation, neither of which
  `verify_release` calls).
- `verify_release` (`release_gate.py:328-378`):
  - line 359: `gh._run_git(["ls-remote", "origin", "main"])` — hits the
    network, always returns the LIVE `main` tip SHA.
  - line 362: `gh._run_git(["rev-parse", "origin/main^2"]).stdout.strip()`
    — resolves the LOCAL cached `refs/remotes/origin/main`, not the
    network. Stale locally ⇒ wrong (or unresolvable) second parent.
  - line 363:
    `gh._run_git(["rev-list", "--count", "origin/main..origin/develop"]).stdout.strip()`
    — same problem: both endpoints are local cached refs.
  - line 364-366: `verified` is computed from `main_sha` (live,
    correct) OR the local-ref-derived `second_parent`/`distance` pair
    (potentially stale) — the merge-commit shape (the one "Create a
    merge commit" produces, and the one `release gate` prescribes) is
    the one exposed to staleness.
- `gather_release_facts` (`release_gate.py:205-285`, the
  `repoach release gate` step) does NOT share this bug: its
  head-freshness check (`compute_release_decision`,
  `release_gate.py:171-175`) compares the local `develop` head against
  `remote_sha`, which comes from a live `git ls-remote origin develop`
  (`release_gate.py:269`) — never a cached ref. The finding's mention
  of a second call site ("`evaluate_release_gate`") does not match any
  function in the current tree; `gather_release_facts` was checked and
  is unaffected, so this spec scopes to `verify_release` only, the
  confirmed live bug.
- Operator lesson already on file (memory: `ferova-operating-model.md`)
  instructs refreshing `git pull --ff-only` AND
  `git fetch origin main:main` before running the release gate BY
  HAND — exactly the tribal-knowledge workaround this spec makes
  unnecessary for `repoach release verify`.
- `release_gate.py` is touched by three prior specs
  (`SP-RELEASE-GATE`, `SP-RELEASE-VERIFY-MERGE-COMMIT`,
  `SP-RELEASE-PROVENANCE-LEDGER`), all of which declare
  `owns.code: []` for it — the file carries no formal ownership claim
  in the arch graph. This spec follows that same established
  convention rather than introducing a first-ever ownership claim on a
  file three sibling specs already share unowned.

## Goals

- G1: `verify_release` runs `git fetch --quiet origin main develop`
  itself before computing `second_parent` (`rev-parse origin/main^2`)
  and `distance` (`rev-list --count origin/main..origin/develop`), so
  both reads always reflect the live remote state regardless of
  whether the caller fetched beforehand.
- G2: a fetch failure (non-zero exit — offline, auth failure,
  unreachable remote) raises an exception (fail-closed, mirroring the
  existing `_default_ci_runner` `FileNotFoundError` contract and the
  missing-receipt `FileNotFoundError`/`json.JSONDecodeError` paths)
  rather than silently falling through to a comparison against
  possibly-stale refs. `repoach/cli/release_cmds.py::release_verify`'s
  existing generic `except Exception` handler already maps any raised
  exception to exit code 1 ("could not evaluate") with zero CLI
  changes required.
- G3: the two sanctioned verification shapes (fast-forward,
  merge-commit) and their existing pinned tests
  (`test_verify_accepts_merge_commit_release`,
  `test_verify_still_refuses_squash`, `test_verify_refuses_stale_merge`,
  `test_release_verify_detects_squash_divergence`) keep passing
  unchanged — the fetch is additive; it changes ref freshness, never
  the decision predicate itself.

## Non-Goals

- NG1: no change to `gather_release_facts`/`compute_release_decision`
  (the `repoach release gate` step) — confirmed unaffected by this bug
  (see Context); no behavior change there.
- NG2: no change to the CLI exit-code contract in `release_cmds.py` —
  a fetch failure already falls into the existing generic
  `except Exception -> exit 1` path.
- NG3: no behavior change to the fast-forward or merge-commit
  predicates themselves (`release_gate.py:364-366`) — same logic, now
  evaluated against fresh refs instead of possibly-stale ones.
- NG4: no new CLI flag to skip the fetch, and no change to
  `write_gate_receipt` or the receipt schema — `verify_release`'s
  signature (`path: Path, *, gh: GhCli`) is unchanged.

## Interface

`src/repoach/review/release_gate.py`:

```python
def verify_release(path: Path, *, gh: GhCli) -> ReleaseVerifyResult:
```

Signature unchanged. Internally, before the existing
`rev-parse origin/main^2` / `rev-list origin/main..origin/develop`
calls, add:

```python
fetch_result = gh._run_git(["fetch", "--quiet", "origin", "main", "develop"])
if not fetch_result.ok:
    raise RuntimeError(
        f"git fetch origin main develop failed: {fetch_result.stderr.strip()}"
    )
```

No other function signature in this module changes.

## Behavior

### Nominal

- Operator runs `repoach release gate`, merges via the GitHub UI
  ("Create a merge commit"), then runs `repoach release verify` from a
  clone that has not fetched since before the merge landed.
  `verify_release` fetches `origin main develop` first, so the
  subsequent `origin/main^2` / `origin/main..origin/develop` reads see
  the live merge commit and correctly report `verified=True`.

### Edge cases

- Local refs already fresh (operator fetched manually beforehand) — the
  fetch is a network no-op; behavior is identical to today.
- The fast-forward shape (`main_sha == expected_sha`, both compared
  case) is unaffected either way since `main_sha` already came from a
  live `ls-remote`; the fetch exists specifically to keep the
  merge-commit shape's `origin/main^2` read correct too.

### Failure scenarios

- `git fetch origin main develop` fails (offline, auth, unreachable
  remote) → `verify_release` raises `RuntimeError` carrying the
  captured `stderr`; the CLI prints `{"error": ...}` and exits 1 —
  never a `ReleaseVerifyResult` computed from refs that could not be
  refreshed.

## Acceptance Criteria

- [ ] AC1: unit (mocked `gh`) — a `MagicMock` `gh._run_git` records
  call order; assert
  `["fetch", "--quiet", "origin", "main", "develop"]` is invoked before
  `["rev-parse", "origin/main^2"]` and before
  `["rev-list", "--count", "origin/main..origin/develop"]`.
- [ ] AC2: unit (mocked `gh`) — `gh._run_git` returns `returncode=1`
  for the `fetch` call; assert `verify_release` raises `RuntimeError`
  (never returns a `ReleaseVerifyResult`).
- [ ] AC3 (INTEGRATION-STYLE, real git clones — MUST FAIL ON
  PRE-CHANGE CODE): build one bare `origin` and TWO independent work
  clones, "developer" and "operator". Clone "operator" from `origin`
  BEFORE the release merge happens, so its local `origin/main` ref is
  stale (pre-merge). In "developer", perform the exact sanctioned
  `git merge --no-ff` shape onto `main` and push. Then, WITHOUT running
  any fetch in "operator", call
  `verify_release(receipt_path, gh=GhCli(cwd=operator_dir))` directly.
  On today's code (no internal fetch) this reproduces the live bug:
  `result.verified` is `False` even though the real, live `main` state
  is a valid sanctioned merge. The shipped test asserts
  `result.verified is True` — it fails on pre-change code and passes
  once `verify_release` fetches first.
- [ ] AC4: promised tests —
  `tests/unit/test_release_gate.py::test_verify_release_fetches_before_rev_parse_checks`
  (AC1),
  `tests/unit/test_release_gate.py::test_verify_release_raises_on_fetch_failure`
  (AC2),
  `tests/unit/test_release_gate.py::test_verify_release_fetches_stale_local_refs_before_merge_check`
  (AC3).
- [ ] AC5: `ruff` + `ruff format --check` + `pytest tests/unit` green;
  zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`;
  `repoach arch graph --check` exits 0 (no new ownership claim —
  `owns.code: []` matches the existing convention already used by the
  three sibling specs that touch this same file).

## Architecture Impact

- Adds/Removes dependency: none — in-place modification of
  `verify_release` in `src/repoach/review/release_gate.py`, which
  carries no ownership claim in the arch graph (mirrors the
  `owns.code: []` convention already used by `SP-RELEASE-GATE`,
  `SP-RELEASE-VERIFY-MERGE-COMMIT`, and
  `SP-RELEASE-PROVENANCE-LEDGER`, all of which touch this same file
  without claiming it). No new file, no new cross-module import — the
  fetch reuses the existing `gh._run_git` call already used for
  `ls-remote`.
- New / changed coupling, cycles, or shared state: none — one
  additional local `git` subprocess call inside an existing function;
  no new shared state, no new public symbol.

## Diagram

N/A (in-place fix, no new component or edge).

## Open Questions

(none)
