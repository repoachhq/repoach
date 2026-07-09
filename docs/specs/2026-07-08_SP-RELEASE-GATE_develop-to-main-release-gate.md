---
id: SP-RELEASE-GATE
title: Evidence-first gate for the develop → main release
version: 0.1
status: approved
author: jfaye (mechanises the manual release procedure run on PR #60, 2026-07-08)
created: 2026-07-08
updated: 2026-07-08

owns:
  code: []
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Evidence-first gate for the develop → main release

## Intent

Give the operator-only `develop → main` release the same evidence
discipline as the per-PR merge gate — mechanically, instead of a
hand-run checklist. The gate gathers the proof and prints a decision;
the operator alone clicks the merge, exactly as today. Never a bot
merge into `main`.

## Context

Release procedure run by hand on PR #60 (2026-07-08, develop 23
commits ahead of main):

1. full local CI mirror green at the develop head
   (`scripts/ci_local.sh`);
2. range provenance: every commit in `main..develop` is the squash of
   a gated PR (subject ends with `(#N)`), zero out-of-band commits;
3. head freshness: local develop == `origin/develop` tip (the PR #50
   stale-head incident showed served heads can lag);
4. merge-method discipline: the release must be a MERGE COMMIT — a
   squash would fork `main` from `develop` permanently and make every
   later release diff unreadable.

Each check is mechanical; today nothing enforces them, and step 4 is
only enforceable after the fact.

## Goals

- G1: `ferova release gate` — read-only CLI mirroring
  `ferova review gate` semantics (exit 0 = may merge, 5 = refused
  with printed reasons, 1 = evaluation error). Facts gathered:
  (a) release-range provenance via a pure classifier over
  `main..develop` commit subjects; (b) head freshness — local develop
  head == `git ls-remote origin develop` tip == the release PR's
  `headRefOid` when `--pr N` is given; (c) local CI mirror green
  (runs `scripts/ci_local.sh`, fail-closed on any non-zero exit).
- G2: The decision is a pure function of the gathered facts
  (mirroring `compute_merge_decision`): any red fact refuses with an
  explicit reason; the gate never merges, never pushes, never calls
  `gh pr merge`.
- G3: `ferova release verify` — post-merge check: `origin/main` tip
  equals the develop SHA the gate approved (recorded in a local gate
  receipt file). A squash or a stale merge diverges immediately and
  loudly, while the mistake is still one revert away.
- G4: The gate prints the required merge method ("Create a merge
  commit — never squash a release") in its PASS output, so the
  discipline travels with the decision.

## Non-Goals

- NG1: No automation of the merge itself — `main` stays
  operator-only; the workflows are untouched.
- NG2: No GitHub branch-protection configuration (server-side rules
  remain a separate operator decision).
- NG3: No re-running of the per-PR review bench over the release
  diff — every commit in the range already carries its own gate
  evidence; provenance checking is the release-level proof.

## Assumptions

- A1: Squash subjects of factory merges always end with `(#N)`
  (GitHub's default squash title, observed across #37-#59).
- A2: `scripts/ci_local.sh` remains the CI parity mirror; the gate
  shells out to it rather than duplicating its gates.
- A3: A local receipt file (e.g. `tmp/release_gate_receipt.json`,
  repo-relative, gitignored via tmp/) is an acceptable carrier
  between `gate` and `verify` on the operator's machine.

## Interface

`src/ferova/review/release_gate.py` (new module):

- `classify_release_range(subjects: list[str]) -> list[str]` — pure:
  returns the subjects that are NOT gated-PR squashes (no `(#N)`
  suffix). Empty list == clean provenance.
- `gather_release_facts(*, repo_root, gh, pr_number=None,
  ci_runner=None) -> ReleaseFacts` — provenance, head freshness,
  CI outcome (ci_runner injectable for tests; defaults to running
  `scripts/ci_local.sh`).
- `compute_release_decision(facts: ReleaseFacts) -> ReleaseDecision`
  — pure; refuses on any red fact, with reasons.
- `write_gate_receipt(path, *, develop_sha, decision)` /
  `verify_release(path, *, gh) -> ReleaseVerifyResult` — receipt
  round-trip and the post-merge main-tip comparison.

`src/ferova/cli/review_cmds.py` (or a small `release_cmds.py`):
`ferova release gate [--pr N]`, `ferova release verify` — exit codes
0 / 5 / 1 as in `review gate`.

## Behavior

### Nominal

Operator opens the release PR, runs `ferova release gate --pr 60`:
the gate classifies the range, compares the three heads, runs the CI
mirror, prints the facts table + "may merge — Create a merge commit,
never squash", writes the receipt. Operator merges on GitHub, runs
`ferova release verify`: main tip == receipt SHA → clean release.

### Edge cases

- An out-of-band commit in the range (hotfix pushed to develop
  bypassing a PR) → refusal naming the commit subject.
- `origin/develop` moved after the gate ran (new auto-merge landed) →
  `verify` fails the tip comparison; re-run the gate.
- Squash-merged release → `verify` reports divergence: main tip is a
  commit absent from develop; the printed remedy is revert + re-merge
  as a merge commit.
- `--pr` omitted → head-freshness compares only local vs ls-remote.

### Failure scenarios

- `scripts/ci_local.sh` missing or non-executable → exit 1
  (evaluation error), never a silent pass.
- `gh` unavailable / offline → exit 1 with the transport error.

## Architecture Impact

New leaf module in `src/ferova/review/` + CLI wiring; shells out to
the existing CI mirror; no new cross-component edges, no workflow
changes, no schema changes. ~300 LOC across 1 new src module and
1-2 new test files — within autonomous Developer capacity.

## Acceptance Criteria

- AC1: `classify_release_range` accepts a range of pure `(#N)`
  squashes and flags any out-of-band subject —
  `tests/unit/test_release_gate.py::test_release_range_all_squashes`
  and `::test_release_range_flags_non_pr_commit`.
- AC2: A stale head (local != ls-remote tip, or release-PR headRefOid
  != develop tip) refuses with a named reason —
  `tests/unit/test_release_gate.py::test_release_gate_refuses_stale_head`.
- AC3: A non-zero CI-mirror exit refuses (fail-closed), a missing
  script is an evaluation error not a pass —
  `tests/unit/test_release_gate.py::test_release_gate_fail_closed_on_red_ci`
  and `::test_release_gate_missing_ci_script_is_error`.
- AC4: The module never merges: no `pr merge` invocation exists in
  the release-gate source —
  `tests/unit/test_release_gate.py::test_release_gate_never_calls_merge`.
- AC5: `verify` detects a squashed or stale release from the receipt
  — `tests/unit/test_release_gate.py::test_release_verify_detects_squash_divergence`.
- AC6: End-to-end in a throwaway git repo (fixture builds its own tmp
  repo with squash-shaped commits): gate passes on a clean range,
  refuses after an out-of-band commit is added —
  `tests/integration/test_release_gate_end_to_end.py::test_release_gate_clean_range_then_out_of_band_refusal`.

## Open Questions

- OQ1: Should `verify` also fire the routine-notification seam on
  divergence (operator push alert), or is a red exit code enough for
  a command the operator just ran by hand? Default: exit code only.
