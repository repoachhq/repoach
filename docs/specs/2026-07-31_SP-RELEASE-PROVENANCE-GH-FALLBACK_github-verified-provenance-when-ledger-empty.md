---
id: SP-RELEASE-PROVENANCE-GH-FALLBACK
title: GitHub-verified release provenance (opt-in) when the pr_merges ledger is empty
version: 0.1
status: approved
author: agent
created: 2026-07-31
updated: 2026-07-31

owns:
  code: [tests/unit/test_release_provenance_github.py, tests/integration/test_release_gate_github_provenance_flow.py]
  resources: N/A

depends_on: [SP-RELEASE-GATE, SP-RELEASE-PROVENANCE-LEDGER, SP-BRANCH-CONFIG, SP-RELEASE-SANCTIONED-DEVELOP-MERGE, SP-REVIEW-POST-BATCH]
provides_to: []

constraints: {}
---

# GitHub-verified release provenance (opt-in) when the ledger is empty

## Intent

`repoach release gate` verifies release-range provenance against the
`pr_merges` SQLite ledger (SP-RELEASE-PROVENANCE-LEDGER): every commit in
`integration_branch..release_branch`-reversed (`main..develop`) must have a
recorded `pr_merges.merged_sha`. The ledger is populated **only** by the
factory merge path (`repoach review merge`). When PRs are merged another
sanctioned way — the control-tower regime merges each PR by hand with
`gh pr merge` — the ledger stays empty and the gate fails **fail-closed** with
`pr_merges ledger has no recorded merges ...`, even when the release is
provably sound (CI green, refs fresh, every range commit a real gated-PR
squash).

Add an **opt-in** second provenance source: `--provenance github` verifies the
range against the set of merge-commit SHAs GitHub reports for PRs **merged into
`integration_branch`**. For a squash-merge, a PR's `mergeCommitOid` is exactly
the resulting commit SHA on the integration branch, so this is the authoritative
GitHub equivalent of `pr_merges.merged_sha` — and it reuses the existing pure
classifier unchanged. The default stays `ledger`, so nothing about the factory
regime changes and the empty-ledger refusal remains the fail-closed default.

## Context

- `src/repoach/review/release_gate.py`
  - `classify_release_range_against_ledger(commits, merged_shas)` (l.73) is a
    pure SHA-membership check: a commit is out-of-band iff its SHA is absent
    from `merged_shas`. It is **source-agnostic** — it does not care whether
    `merged_shas` came from the ledger or elsewhere. This spec feeds it a
    GitHub-derived set; the function itself is not modified.
  - `gather_release_facts(*, repo_root, gh, pr_number, ci_runner, db_path)`
    (l.206) currently branches on `db_path is None` (subject-only) vs a
    `db_path` (ledger). It sets `provenance_error = _PROVENANCE_LEDGER_EMPTY`
    when `fetch_merged_pr_shas` returns an empty set and the range is non-empty.
  - `_PROVENANCE_LEDGER_EMPTY` / `_PROVENANCE_LEDGER_UNREADABLE` (l.43-47) are
    the refusal strings.
- `src/repoach/cli/release_cmds.py` — `release_gate` command (l.47) calls
  `gather_release_facts(..., db_path=Path(get_settings().db_path))`
  unconditionally, so the ledger source is always used today.
- `src/repoach/review/gh_client.py` — `GhCli` already wraps `gh`/`git`
  (e.g. `pr_head_sha`); this spec adds one read-only method that lists merged
  PRs' merge-commit SHAs for a base branch.
- `integration_branch` comes from `get_settings().integration_branch`
  (SP-BRANCH-CONFIG), never a literal.

## Design

A `ProvenanceSource` selects where `merged_shas` comes from. Both sources feed
the **same** `classify_release_range_against_ledger` and the **same** empty-set
fail-closed rule, so the security properties are identical — only the SHA
source differs:

- `ledger` (default): unchanged. `fetch_merged_pr_shas(db_path)`; empty →
  `_PROVENANCE_LEDGER_EMPTY`.
- `github`: `merged_shas = gh.merged_pr_merge_shas(base=integration_branch)` —
  the `mergeCommitOid` of every PR merged into the integration branch. Empty →
  a new `_PROVENANCE_GH_EMPTY` refusal (fail-closed, same shape as the ledger
  case). Any range commit whose SHA is not in that set is out-of-band exactly
  as with the ledger.

`gh.merged_pr_merge_shas(base)` must page far enough to cover the whole release
range; it is read-only and never merges. A fork PR that never merged into the
integration branch contributes no `mergeCommitOid`, so a forged/out-of-band
commit is still flagged — the GitHub source is no weaker than the ledger for
the property the gate enforces (every released commit is a real merged-PR
result on the integration branch).

## Acceptance criteria

1. **CLI opt-in, default preserved.** `repoach release gate` accepts
   `--provenance` with choices `ledger` (default) and `github`. With no flag or
   `--provenance ledger`, `gather_release_facts` is called with the ledger
   `db_path` and behaviour is byte-identical to today. *(test:
   `test_release_gate_provenance_option_defaults_to_ledger`)*

2. **GitHub source classifies by merge-commit SHA.** With
   `--provenance github`, a range whose every commit SHA is a
   `mergeCommitOid` of a PR merged into `integration_branch` yields
   `out_of_band_commits == []` and `provenance_error is None`; a range commit
   whose SHA is absent from the GitHub set appears in `out_of_band_commits`.
   *(test: `test_github_provenance_flags_only_unmerged_shas`, using a fake
   `gh` returning a fixed merge-SHA set — no network)*

3. **Fail-closed on an empty GitHub set.** `--provenance github` with an empty
   GitHub merged-set and a non-empty release range sets `provenance_error`
   (a new `_PROVENANCE_GH_EMPTY` message naming the count) and
   `compute_release_decision` refuses. *(test:
   `test_github_provenance_empty_set_refuses`)*

4. **Ledger path unchanged.** The existing empty-ledger refusal and its
   message are unchanged; existing release-gate tests remain green. *(test:
   existing `tests/unit/test_release_*`)*

5. **Still never merges.** The module-source code-shape guarantee holds:
   `test_release_gate_never_calls_merge` still passes; the new `gh` method is
   read-only (`gh pr list`), invoking no merge/push. *(test: existing
   guarantee test + `test_merged_pr_merge_shas_is_read_only`)*

6. **Integration flow.** An end-to-end `release gate --provenance github` over a
   temporary git repo (real `main..develop` range) with an injected fake `gh`
   and an injected green CI runner returns `merge: true` when every range commit
   SHA is in the fake GitHub merged-set, and refuses when one is missing.
   *(test: `tests/integration/test_release_gate_github_provenance_flow.py`)*

## Out of scope

- No change to the default factory regime: the ledger stays the default and its
  empty-ledger fail-closed is untouched.
- No auto-detection/auto-fallback: the source is chosen explicitly by the
  operator, so an empty ledger under the factory regime still fails loud.
- `repoach release verify` is unchanged (its shape check is already
  ledger-independent).
