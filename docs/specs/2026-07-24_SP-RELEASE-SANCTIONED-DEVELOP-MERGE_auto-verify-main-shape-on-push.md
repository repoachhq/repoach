---
id: SP-RELEASE-SANCTIONED-DEVELOP-MERGE
title: Automatically verify the sanctioned merge shape every time main advances
version: 0.1
status: approved
author: agent
created: 2026-07-24
updated: 2026-07-24

owns:
  code: [.github/workflows/release-verify.yml]
  resources: N/A

depends_on: [SP-RELEASE-GATE, SP-RELEASE-VERIFY-MERGE-COMMIT]
provides_to: []

constraints: {}
---

# Automatically verify the sanctioned merge shape every time main advances

## Intent

`verify_release` (`src/repoach/review/release_gate.py:328-370`) already
correctly detects an off-shape landing on `main` — a squash-merge or a
stale merge satisfies neither the fast-forward nor the
merge-commit-with-zero-distance shape `release gate` prescribes — and
`repoach release verify` exposes it on the CLI. But nothing ever calls
it: the detective control only fires if the operator remembers to type
the command by hand after every `develop -> main` merge, so an
accidental squash goes completely undetected until someone notices the
two branches have quietly diverged. Close the loop by running the
check automatically on every push to `main`, and — because the
existing command depends on a receipt file that only ever exists on
the operator's own machine — add a receipt-free verification path that
a fresh CI checkout can actually run.

## Context

Re-verified against `origin/develop` at `bc4e4e0` (2026-07-24):

- `src/repoach/review/release_gate.py:328-370`, `verify_release(path,
  *, gh)` — reads a receipt (`{"develop_sha": ..., "merge": ...,
  "reasons": ...}`) written by `write_gate_receipt`, then computes
  `main_sha`, `second_parent = origin/main^2`, and
  `distance = git rev-list --count origin/main..origin/develop`, and
  returns `verified = (main_sha == expected_sha) or (second_parent ==
  expected_sha and distance == "0")` — the exact two-shape check the
  finding describes. Confirmed present verbatim, unchanged since
  landing.
- `src/repoach/cli/release_cmds.py:37`:
  `_RECEIPT_PATH = Path("tmp/release_gate_receipt.json")` — a path
  under `tmp/`, which `.gitignore:51` excludes (`tmp/*`) from every
  commit. The receipt is written locally by `repoach release gate`
  and read locally by `repoach release verify`; it never leaves the
  operator's working directory and is never available to a GitHub
  Actions runner, which always starts from a fresh checkout.
- `grep -rn "release verify\|verify_release" .github/workflows/*.yml
  scripts/*.sh` — zero hits, confirmed again today. No workflow, hook,
  or script invokes either the CLI command or the pure function
  anywhere in the tree.
- `.github/workflows/ci.yml:4-11` already has a `push: branches:
  [main]` trigger (kept, per its own comment, "for develop -> main
  release validation") but its single `test` job only runs the
  standard lint/pytest/smoke matrix — it never touches the release
  shape.
- Consequence confirmed by re-reading the finding's own proposed
  fix literally: a naive `push: branches: [main]` job that runs
  `repoach release verify` unmodified would call `verify_release`
  against `_RECEIPT_PATH`, which never exists on a fresh CI runner —
  every single push to `main`, sanctioned or not, would fail closed
  with exit code `1` (`FileNotFoundError`) before ever reaching the
  actual shape comparison. The detective control has to compare
  against something a CI runner can see without any receipt: the
  live `origin/develop` tip, fetched fresh in the same job. This is
  the one design decision this spec adds beyond the finding's literal
  wording, and it is scoped tightly to making the proposed direction
  actually work.
- `src/repoach/review/release_gate.py` and
  `src/repoach/cli/release_cmds.py` were created by `SP-RELEASE-GATE`
  and reshaped by `SP-RELEASE-VERIFY-MERGE-COMMIT`; both specs record
  `owns.code: []` (pre-dating the ownership-tracking convention), so
  this spec depends on both rather than claiming ownership of
  pre-existing code, mirroring the precedent set by
  `SP-PROXY-EARLY-ABORT-ERROR-FRAME` / `SP-NIM-PROBE-UNPARSEABLE-DIAG`.
  The only file this spec owns outright is the new workflow it adds.

## Goals

- G1: a new pure function in `release_gate.py` verifies `main`'s tip
  against the sanctioned shape using `origin/develop`'s **live** tip
  as the expected SHA, with no dependency on any local receipt file —
  so the check can run unattended on a fresh CI checkout.
- G2: `repoach release verify` grows a `--live` flag that runs the new
  receipt-free path instead of the existing receipt-based one; the
  default (no flag) behavior for the operator's manual post-merge
  check is untouched.
- G3 (OPERATOR-MANUAL, hand-implemented — `.github/workflows/*` is
  bot-forbidden): a new `push` (branches: `[main]`) workflow runs
  `repoach release verify --live` on every push to `main` and fails
  the job loudly (non-zero exit → red check + GitHub's own
  failed-workflow-run notification to repo watchers) on any
  divergence, with zero operator action required to trigger the
  check.
- G4: the two verification paths share their shape-comparison logic
  (fast-forward or merge-commit-with-zero-distance) through one
  private helper, so the sanctioned-shape definition exists in exactly
  one place regardless of where the expected SHA came from.

## Non-Goals

- NG1: no behavior change to the existing receipt-based
  `verify_release` / plain `repoach release verify` (no flag) path —
  the operator's manual pre-merge workflow is byte-for-byte unchanged.
- NG2: no GitHub branch-protection API configuration (required-status
  checks, merge-queue rules) — that is a GitHub repository setting the
  operator configures by hand in the UI if they choose to make this
  check blocking; this spec only makes the check run and fail loudly,
  it does not make GitHub refuse the push.
- NG3: no new notification channel (Slack, email, routine) — "posts
  loudly" is satisfied by the GitHub Actions job itself going red,
  which is the same signal every other CI failure on this repo already
  produces; no bespoke alerting is added.
- NG4: no change to `repoach release gate`, `compute_release_decision`,
  `gather_release_facts`, `classify_release_range`, or the receipt
  schema — only `verify_release`'s sibling path is added.
- NG5: no attempt to reconcile the case where `origin/develop` has
  already advanced past the just-merged release by the time the
  workflow runs — this is the same timing assumption the existing
  receipt-based `verify_release` already makes (it compares against
  the *current* `origin/main..origin/develop` distance, not a
  point-in-time snapshot), so this spec introduces no new race
  condition, only the same one the finding already accepted.

## Interface

`src/repoach/review/release_gate.py`:

```python
def verify_release_live(*, gh: GhCli) -> ReleaseVerifyResult:
    """Verify main's tip against the sanctioned shape using develop's live tip.

    Args:
        gh: A :class:`~repoach.review.gh_client.GhCli`-like wrapper used
            for the ``git ls-remote``/``rev-parse``/``rev-list``
            invocations.

    Returns:
        The assembled :class:`ReleaseVerifyResult`, with
        ``expected_sha`` set to ``origin/develop``'s live tip rather
        than a value read from any receipt file.
    """
```

`verify_release` and `verify_release_live` both delegate their
fast-forward / merge-commit-with-zero-distance comparison to one new
private helper (illustrative shape; no inline comments in the real
diff):

```python
def _sanctioned_shape_result(
    *, expected_sha: str, main_sha: str, second_parent: str, distance: str
) -> ReleaseVerifyResult:
    verified = bool(expected_sha) and (
        main_sha == expected_sha or (second_parent == expected_sha and distance == "0")
    )
    detail = (
        "main tip matches the approved develop head"
        if verified
        else "main tip does not match the approved develop head -- squash or stale merge? "
        "revert and re-merge as a merge commit"
    )
    return ReleaseVerifyResult(
        verified=verified, main_sha=main_sha, expected_sha=expected_sha, detail=detail
    )
```

`src/repoach/cli/release_cmds.py`, `release_verify` (extended, not
replaced):

```python
@release_app.command("verify")
def release_verify(
    live: bool = typer.Option(
        False,
        "--live",
        help=(
            "Skip the local gate receipt; compare against origin/develop's "
            "live tip instead (the mode the push-triggered CI check uses)."
        ),
    ),
) -> None:
```

`.github/workflows/release-verify.yml` (NEW, hand-implemented,
illustrative shape):

```yaml
name: Release shape verify

on:
  push:
    branches: [main]

jobs:
  verify:
    runs-on: self-hosted
    steps:
      - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5 # v4.3.1
        with:
          fetch-depth: 0
      - run: pip install -e .
      - run: git fetch --no-tags origin develop
      - run: repoach release verify --live
```

## Behavior

### Nominal

- The operator merges `develop` into `main` via "Create a merge
  commit" exactly as `release gate` instructs → the push fires the new
  workflow → `repoach release verify --live` fetches `origin/develop`,
  finds `origin/main`'s second parent equals it with
  `origin/main..origin/develop` distance `0` → `verified: true`, exit
  `0`, green check.
- `main` fast-forwards to `develop`'s exact tip → `main_sha ==
  origin/develop tip` → `verified: true`, exit `0`.
- The operator additionally runs `repoach release verify` (no flag)
  locally right after the merge, as today → unchanged receipt-based
  behavior, both paths agree because they check the same shape.

### Edge cases

- Someone accidentally squash-merges `develop` into `main` → `main`'s
  tip is a new commit whose parent is the old `main` tip, not a merge
  commit with `develop`'s tip as a parent → neither shape matches →
  `verified: false`, exit `5`, the job goes red, GitHub's default
  failed-run notification reaches repo watchers with zero operator
  action.
- A hotfix commit lands directly on `main` (bypassing `develop`
  entirely) → same non-matching shape → `verified: false`, job red.
- `git fetch origin develop` in the workflow returns a `develop` tip
  that has already advanced past the just-merged release (a new PR
  merged into `develop` moments later) → `origin/main..origin/develop`
  distance is no longer `0` even though the merge itself was
  sanctioned → `verified: false` — an accepted, pre-existing false
  positive under NG5, identical to what the receipt-based path already
  risks; not a regression this spec introduces.

### Failure scenarios

- `git ls-remote`/`rev-parse`/`rev-list` transport failure (network
  blip, auth issue) inside `verify_release_live` → the CLI's existing
  `except Exception` wrapper catches it, echoes `{"error": str(exc)}`,
  exits `1` — distinguishable in the workflow logs from a genuine
  shape divergence (exit `5`).
- The workflow itself fails to check out or install the package →
  ordinary CI infrastructure failure, outside this spec's scope, same
  as any other job in `ci.yml`.

## Architecture Impact

- `src/repoach/review/release_gate.py` and
  `src/repoach/cli/release_cmds.py` are edited in place under the
  `depends_on: [SP-RELEASE-GATE, SP-RELEASE-VERIFY-MERGE-COMMIT]` edge
  — no new cross-owner import, no change to either module's existing
  public names' signatures (`verify_release`'s signature is untouched;
  `release_verify` gains an optional flag with a default that
  preserves today's behavior).
- New file `.github/workflows/release-verify.yml` is the only thing
  this spec owns outright (`owns.code`); it has no code-level
  dependents and introduces no import edge — it only shells out to the
  `repoach` CLI already exposed by the dependency edge above.
- New / changed coupling: `_sanctioned_shape_result` is a new private
  helper shared by two callers inside the same module — reduces
  duplication, adds no new coupling.

## Diagram

N/A (one new pure function + one CLI flag + one new, hand-implemented
workflow file; no new module boundary).

## Acceptance Criteria

- [ ] AC1: unit —
  `tests/unit/test_release_gate.py::test_verify_release_live_accepts_merge_commit_shape`
  and `::test_verify_release_live_rejects_squash_shape`. Build a real
  throwaway origin/work git pair with the existing
  `_init_origin_and_work` fixture (no stubbing of git plumbing): seed
  `main`, branch `develop` with one commit, then (a) perform the
  sanctioned `git merge --no-ff` + push and assert
  `verify_release_live(gh=GhCli(cwd=work_dir)).verified is True` with
  **no receipt file created anywhere in the test**; (b) advance `main`
  with a plain non-merge commit instead and assert `verified is False`
  with `"squash" in result.detail or "stale" in result.detail`. FAILS
  on pre-change code (`verify_release_live` does not exist —
  `AttributeError`).
- [ ] AC2 (INTEGRATION):
  `tests/integration/test_release_gate_end_to_end.py::test_release_verify_live_end_to_end_no_receipt_needed`.
  Drive the same real-repo fixture pattern already used by
  `test_release_verify_merge_commit_end_to_end` in this file, but
  never call `write_gate_receipt` and never create
  `tmp/release_gate_receipt.json`; call
  `release_cmds.release_verify(live=True)` directly against the
  throwaway `work_dir` (redirecting only `GhCli`'s `cwd`, per the
  existing `_redirect_release_cmds` helper style in
  `tests/unit/test_release_cli.py`) and assert it returns normally
  (exit `0`) for the sanctioned shape, then raises `typer.Exit` with
  `exit_code == 5` after a squash-style advance of `main`. FAILS on
  pre-change code: `release_verify()` accepts no `live` keyword today
  (`TypeError`).
- [ ] AC3: unit —
  `tests/unit/test_release_cli.py::test_cli_release_verify_live_flag_skips_receipt_and_detects_divergence`.
  Monkeypatch `release_cmds.verify_release_live` to return a
  `ReleaseVerifyResult(verified=False, ...)` and call
  `release_cmds.release_verify(live=True)`; assert `typer.Exit` with
  `exit_code == 5` and that `release_cmds.verify_release` (the
  receipt-based function) was never called. Also assert the default
  `release_cmds.release_verify(live=False)` still calls the
  receipt-based path unchanged (regression guard for NG1). FAILS on
  pre-change code (`release_verify` takes no arguments —
  `TypeError`).
- [ ] AC4: existing tests
  `tests/unit/test_release_gate.py::test_release_verify_detects_squash_divergence`,
  `::test_verify_accepts_merge_commit_release`,
  `::test_verify_still_refuses_squash`, `::test_verify_refuses_stale_merge`
  and `tests/unit/test_release_cli.py::test_cli_release_verify_exit_five_on_divergence`
  all still pass unmodified, proving the receipt-based path is
  byte-for-byte unaffected (NG1).
- [ ] AC5 (OPERATOR-MANUAL, hand-implemented): add
  `.github/workflows/release-verify.yml` per the Interface sketch above
  — `on: push: branches: [main]`, checkout with `fetch-depth: 0`,
  install the package, `git fetch origin develop`, then
  `repoach release verify --live`. The bots never touch this file
  (`.github/workflows/*` is whitelist-forbidden); the operator adds it
  by hand once AC1-AC4 are merged, and confirms it by pushing a
  throwaway sanctioned merge commit to a scratch remote (or observing
  the next real release) and checking the job goes green.
- [ ] AC6: `ruff check` + `ruff format --check` green; zero inline
  comments (SP-NO-INLINE-COMMENTS-GATE) and no `# noqa` anywhere in
  the diff; full `pytest tests/unit` and `pytest tests/integration`
  green; `repoach arch graph --check` exits 0 (no new ownership
  conflict — the only owned file is the new workflow, edits to
  existing modules ride the `depends_on` edge).

## Open Questions

- Whether to also make this check a required GitHub status check on
  `main` (turning the detective control into a preventive one) is an
  operator branch-protection decision (NG2), out of scope here.
