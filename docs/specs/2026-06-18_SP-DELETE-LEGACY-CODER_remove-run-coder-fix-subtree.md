# SP-DELETE-LEGACY-CODER — delete the legacy Coder subtree

**Status:** implemented (hand-shipped)
**Redesign slice:** 10b-3 (final 10b slice; after 10b-1 ledger verdict +
10b-2 consensus deletion)
**Touches forbidden paths:** yes (`.github/workflows/auto-review.yml`,
`prompts/review/coder_0.4.0.md`) — hand-shipped via PR, never bot-edited

## Why

The CI Coder flipped onto the evidence-first findings path
(`run_coder_fix_from_findings`) in #397; the legacy archive-verdict loop
`run_coder_fix` and its whole subtree have had no production caller since.
The 10b cartography verified the delete-closure has no live non-legacy
reference. This removes it.

## Change (~3.5k LOC net deletion)

`coder_loop.py` (2566 → ~360 LOC): deleted `run_coder_fix` and its
closure — the arbiter (`arbiter_filter_reviews` + `ArbiterDecision/Result`
+ the legacy `coder_loop._files_in_diff`), the challenge subtree
(`coder_challenge_pass` + `_parse/_verify/_enforce/_apply_challenges` +
`ChallengeRecord/Report` + `CHALLENGE_DECISIONS` +
`render/inject_challenges_block`), ACCEPT-consistency
(`AcceptConsistencyReport` + `assert_accept_records_have_fixes` +
`persist_accept_without_fixes`), the iteration cap (`MAX_ITERATIONS` +
`MAX_GATE_RETRIES` + `parse_iteration_counter` + `bump_iteration_marker`),
the pre-verify pass (`pre_verify_review_comments` + `_format_evidence_reply`
+ `_match_inline_thread` + `_stopword_overlap`), archive parsing
(`parse_team_outcome_from_archive` + `reviews_from_archive`) and
`CoderLoopResult`. The module is now the shared-primitives toolbox.

**Kept** (live via `coder_findings` + `dev_runner`): `is_path_allowed`,
`is_placeholder_content`, `persist_placeholder_rejected`, `apply_fixes`,
`run_ruff_gate`, `run_pytest_matrix`, `git_commit_and_push`,
`revert_working_tree`, `fetch_ci_status`, `fetch_failed_check_logs`,
`FORBIDDEN_PATHS/PREFIXES`, the CI constants. (`findings_bridge._files_in_diff`
is the keeper copy — distinct from the deleted `coder_loop` one.)

Also: deleted `coder_verify.py` (pre-verify-only); `review fix` is now
findings-only (dropped `--no-from-findings` + the `run_coder_fix` branch +
exit 3/8); `auto-review.yml` drops `--from-findings` (now the only path) and
the dead exit-3/8 handler branches; `orchestrator` drops the now-dead
challenge block from the archive `legacy_body` + `_extract_challenges_block`
+ `_CHALLENGES_BLOCK_RE`; `coder_0.4.0.md` marked RETIRED.

Tests: deleted `test_arbiter_pre_review_challenge.py` +
`test_coder_challenge.py`; trimmed `test_review_coder_loop.py` to the
kept-primitive coverage (whitelist / apply_fixes / CI status / pytest
matrix); dropped the `coder_verify` event-name pin and the deleted-function
event pins from `test_review_layer_silent_except_logging.py`; fixed two
`test_coder_findings` CLI calls (the `from_findings` kwarg is gone).

## Acceptance

- Full `tests/unit` green (962); `ruff` + format + no-inline-comments +
  no-silent-except clean; no residual reference to any deleted symbol.

## Deferred

`LEGACY_VERDICT_HEADER` / the `legacy_body` framework stay — they carry the
machine-readable TeamOutcome JSON that `review report` +
`auto_merge.parse_archive_verdict` still consume. Retiring those + the
`pr_merges.verdict` column is the optional 10b-4 (a schema migration).
`reviewer.Coder.respond` + the `coder_0.4.0.md` persona are dead but left
for the reviewer-persona rewrite chantier. Slice 9 (stuck escalation) adds
the cross-run iteration cap the legacy `MAX_ITERATIONS` provided.
