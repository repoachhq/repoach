# SP-REFUTER-JUDGES-WHAT-IT-CAN — Every deferred finding carries a named reason, and the refuter defers what it cannot see

Hand-authored after the spec was approved: the gate refused with 'plan-absent' because an approved spec is graded against its committed plan only. Steps 1-3 record what was delivered before this spec (refuter 0.1, injection hardening 0.2, mechanical verifiers); step 4 is spec 0.1 (AC1-AC6, 7940f79); step 5 is this PR's 0.2 amendment (AC7-AC12, AC15-AC18). The plan adds one integration test the spec's unit-only ACs omit (superset), required by the src/ plan form. AC13 and AC14 are a live run and a measurement method, not pytest selectors.

## Step 1 — The refuter judges design, security and spec_gap findings adversarially

- **Files**: `src/repoach/review/refuter.py`, `src/repoach/review/findings.py`, `tests/unit/test_refuter.py`
- **Action**: Delivered before this spec (refuter persona 0.1, SP-REFUTER): judge_finding builds one prompt per proposed design/security/spec_gap finding from its cited evidence, asks the judge for a verdict line and moves the finding to refuted or verified; missing evidence, an unparseable verdict or a judge exception leave it proposed. judge_findings_for_pr walks a PR's proposed findings, counts the transitions, and never builds a judge when nothing is judgeable.
- **Commit**: `feat(review): judge proposed findings with an adversarial refuter`
- **Done when**: pytest tests/unit/test_refuter.py passes
- **Unit tests**: `tests/unit/test_refuter.py::test_parse_verdict_extracts_refuted_bool`, `tests/unit/test_refuter.py::test_refute_finding_refuted`, `tests/unit/test_refuter.py::test_refute_finding_verified`, `tests/unit/test_refuter.py::test_refute_finding_proposed_on_missing_evidence`, `tests/unit/test_refuter.py::test_refute_finding_proposed_on_unparseable_verdict`, `tests/unit/test_refuter.py::test_refute_finding_proposed_when_judge_raises`, `tests/unit/test_refuter.py::test_judge_findings_for_pr_transitions_and_counts`

## Step 2 — Injection-safe evidence channel and verdict line

- **Files**: `src/repoach/review/refuter.py`, `prompts/review/refuter_0.2.1.md`, `tests/unit/test_refuter.py`
- **Action**: Delivered before this spec (refuter persona 0.2, SP-REFUTER-INJECTION-HARDEN, #93): the evidence travels in a delimited channel the judge is told never to obey, and the verdict is read from the last VERDICT: line only, so an object injected earlier in the reply or in the evidence cannot refute a blocking finding; bad verdict shapes are rejected.
- **Commit**: `fix(refuter): injection-safe evidence channel and verdict line`
- **Done when**: pytest tests/unit/test_refuter.py -k "parse_verdict or injection" passes
- **Unit tests**: `tests/unit/test_refuter.py::test_parse_verdict_rejects_bad_shapes`, `tests/unit/test_refuter.py::test_parse_verdict_ignores_injected_leading_object`, `tests/unit/test_refuter.py::test_evidence_injection_does_not_refute_blocking`

## Step 3 — Mechanical verifiers settle checkable claims without a model call

- **Files**: `src/repoach/review/finding_verifiers.py`, `tests/unit/test_finding_verifiers.py`
- **Action**: Delivered before this spec: verify_findings_for_pr checks missing_test, missing_docstring and lint claims on disk at the head (a test by name under a real tests tree, a docstring, a ruff run) and records the verdict with a diagnostic; design claims are left to the refuter; code under review never refutes a missing-test claim.
- **Commit**: `feat(review): verify checkable findings mechanically`
- **Done when**: pytest tests/unit/test_finding_verifiers.py passes
- **Unit tests**: `tests/unit/test_finding_verifiers.py::test_missing_test_refuted_when_symbol_exists`, `tests/unit/test_finding_verifiers.py::test_missing_test_verified_when_absent`, `tests/unit/test_finding_verifiers.py::test_missing_test_proposed_without_symbol`, `tests/unit/test_finding_verifiers.py::test_missing_test_not_refuted_by_src_symbol`, `tests/unit/test_finding_verifiers.py::test_missing_test_refuted_by_real_tests_dir_test`, `tests/unit/test_finding_verifiers.py::test_src_planted_test_does_not_refute`, `tests/unit/test_finding_verifiers.py::test_missing_docstring_refuted_when_documented`, `tests/unit/test_finding_verifiers.py::test_missing_docstring_verified_when_undocumented`, `tests/unit/test_finding_verifiers.py::test_missing_docstring_proposed_when_file_absent`, `tests/unit/test_finding_verifiers.py::test_missing_docstring_refuted_on_exempt_path`, `tests/unit/test_finding_verifiers.py::test_lint_verified_on_violation`, `tests/unit/test_finding_verifiers.py::test_lint_refuted_when_clean`, `tests/unit/test_finding_verifiers.py::test_design_finding_deferred`, `tests/unit/test_finding_verifiers.py::test_verify_findings_for_pr_round_trip`, `tests/unit/test_finding_verifiers.py::test_deferred_verification_persists_diagnostic`

## Step 4 — Every finding left without a verdict carries a named reason

- **Files**: `src/repoach/review/refuter.py`, `src/repoach/review/orchestrator.py`, `src/repoach/gates/config.py`, `tests/unit/test_refuter.py`, `tests/unit/test_review_team.py`
- **Action**: Delivered by 7940f79 (spec 0.1, AC1-AC6): judge_findings_for_pr returns a JudgingReport with one Refutation per proposed finding in exactly one state (verified, refuted, deferred, unverified), the last two with a reason from a closed set (evidence outside the diff, evidence unreadable, judging cap N reached, verdict unparseable, judge call failed: <type>, not yet mechanically verified, checkout not at the head). The cap is [gates].refuter_max_judged (default 10), REPOACH env outranking it; the orchestrator logs one review_team.finding_deferred event per verdict-less finding and review_team.findings_judged with the four counts and model_calls.
- **Commit**: `fix(review): name every finding the refuter leaves without a verdict`
- **Done when**: pytest tests/unit/test_refuter.py tests/unit/test_review_team.py -k "deferral_reason or unverified_target or judges_every_readable or max_judged or finding_deferred_event" passes
- **Unit tests**: `tests/unit/test_refuter.py::test_judge_findings_no_targets_never_builds_judge`, `tests/unit/test_refuter.py::test_deferral_reason_evidence_outside_the_diff`, `tests/unit/test_refuter.py::test_deferral_reason_evidence_unreadable`, `tests/unit/test_refuter.py::test_deferral_reason_verdict_unparseable`, `tests/unit/test_refuter.py::test_deferral_reason_judge_call_failed_names_the_exception`, `tests/unit/test_refuter.py::test_deferral_reason_judge_factory_failure_is_built_once`, `tests/unit/test_refuter.py::test_deferral_reason_judging_cap_names_the_overflow_findings`, `tests/unit/test_refuter.py::test_deferral_reason_checkout_is_not_the_mr_head`, `tests/unit/test_refuter.py::test_unverified_target_not_yet_mechanically_verified`, `tests/unit/test_refuter.py::test_judges_every_readable_finding_under_the_cap`, `tests/unit/test_refuter.py::test_max_judged_configurable_from_gates_config`, `tests/unit/test_refuter.py::test_max_judged_configurable_defaults_to_ten`, `tests/unit/test_refuter.py::test_max_judged_configurable_invalid_value_falls_back_to_ten`, `tests/unit/test_refuter.py::test_max_judged_env_outranks_repoach_toml`, `tests/unit/test_refuter.py::test_max_judged_env_is_printed_by_doctor`, `tests/unit/test_refuter.py::test_max_judged_refuses_a_boolean_in_repoach_toml`, `tests/unit/test_refuter.py::test_docs_only_mr_names_every_finding_with_zero_model_calls`, `tests/unit/test_refuter.py::test_every_finding_ends_in_exactly_one_state`, `tests/unit/test_refuter.py::test_summary_line_matches_the_documented_shape`, `tests/unit/test_refuter.py::test_scope_head_judges_this_run_and_counts_older_heads_as_stale`, `tests/unit/test_review_team.py::test_orchestrator_logs_one_finding_deferred_event_per_deferral`, `tests/unit/test_review_team.py::test_orchestrator_finding_deferred_event_absent_when_everything_is_judged`

## Step 5 — The refuter defers what it cannot see; its evidence fills its own context

- **Files**: `src/repoach/review/refuter.py`, `src/repoach/review/refuter_evidence.py`, `prompts/review/refuter_0.3.0.md`, `src/repoach/gates/config.py`, `src/repoach/review/review_cache.py`, `src/repoach/review/orchestrator.py`, `src/repoach/cli/review_cmds.py`, `src/repoach/llm_proxy/providers/claude_code/client.py`, `tests/unit/test_refuter_defers_unseen.py`, `tests/unit/test_claude_code_output_cap.py`, `tests/integration/test_refuter_defers_unserved_end_to_end.py`
- **Action**: This PR (amendment 0.2, G6-G15, AC7-AC18): persona 0.3.0 offers refuted / verified / cannot_judge; cannot_judge, and a refuted whose reasoning only says the code was not visible, defer with 'evidence outside window'. refuter_evidence.build_evidence fills the refuter's context_tokens (default and floor 40 000, [gates].refuter_context_tokens, REPOACH_REFUTER_CONTEXT_TOKENS outranking it) in priority order from the pinned head tree, path-jailed: the whole cited file (a slice never below +-25 lines when over budget), git grep -w hits of every named identifier with +-10 lines (at most 50, nearest first), named paths and bare file names (one git ls-files), the diff hunks of the cited file and the cited spec section; everything cut is named in evidence_truncated. An identifier whose hits exist but none was served defers with 'evidence outside window (<ids> not served)' without a judge call; grep timeouts (60 s) and the 180 s assembly deadline are named. The judge's reply cap is 2 048 tokens with thinking disabled, reaching the claude_code CLI as CLAUDE_CODE_MAX_OUTPUT_TOKENS and MAX_THINKING_TOKENS=0. The run line ends 'evidence_tokens total T' and the JSON carries refuter_tokens {evidence, total}; no cost figure shapes the evidence (G14). The integration test drives the real review with a fake judge that must not be called. AC13 and AC14 are a live run and a measurement method, not pytest selectors.
- **Commit**: `fix(refuter): defer what it cannot see, fill its own context`
- **Done when**: pytest tests/unit/test_refuter_defers_unseen.py tests/unit/test_claude_code_output_cap.py tests/integration/test_refuter_defers_unserved_end_to_end.py passes
- **Unit tests**: `tests/unit/test_refuter_defers_unseen.py::test_parse_verdict_reads_literal_cannot_judge`, `tests/unit/test_refuter_defers_unseen.py::test_parse_verdict_maps_unseen_refutation_to_cannot_judge`, `tests/unit/test_refuter_defers_unseen.py::test_prompt_offers_cannot_judge_and_names_partial_evidence`, `tests/unit/test_refuter_defers_unseen.py::test_evidence_outside_window_is_a_named_deferral`, `tests/unit/test_refuter_defers_unseen.py::test_claim_about_truly_absent_code_is_deferred_with_its_reason`, `tests/unit/test_refuter_defers_unseen.py::test_hand_typed_enum_is_seen_not_refuted`, `tests/unit/test_refuter_defers_unseen.py::test_half_derived_table_is_seen_not_refuted`, `tests/unit/test_refuter_defers_unseen.py::test_untested_migration_is_seen_not_refuted`, `tests/unit/test_refuter_defers_unseen.py::test_whole_cited_file_is_served_beyond_the_old_window`, `tests/unit/test_refuter_defers_unseen.py::test_file_over_the_budget_is_a_named_slice_never_below_the_floor`, `tests/unit/test_refuter_defers_unseen.py::test_claim_identifiers_names_symbols_and_paths`, `tests/unit/test_refuter_defers_unseen.py::test_search_serves_hits_with_ten_lines_of_context`, `tests/unit/test_refuter_defers_unseen.py::test_search_caps_hits_at_fifty_and_names_the_overflow`, `tests/unit/test_refuter_defers_unseen.py::test_budget_exhaustion_is_named_in_priority_order`, `tests/unit/test_refuter_defers_unseen.py::test_diff_hunks_of_the_cited_file_are_served`, `tests/unit/test_refuter_defers_unseen.py::test_cited_spec_section_is_served`, `tests/unit/test_refuter_defers_unseen.py::test_bare_file_name_is_found_and_served_whole`, `tests/unit/test_refuter_defers_unseen.py::test_context_tokens_floor_and_overrides`, `tests/unit/test_refuter_defers_unseen.py::test_judge_receives_a_budget_of_its_own_context`, `tests/unit/test_refuter_defers_unseen.py::test_search_is_path_jailed`, `tests/unit/test_refuter_defers_unseen.py::test_search_hits_cannot_forge_a_verdict_line`, `tests/unit/test_refuter_defers_unseen.py::test_refutation_evidence_survives_the_review_cache`, `tests/unit/test_refuter_defers_unseen.py::test_grep_timeout_defers_without_calling_the_judge`, `tests/unit/test_refuter_defers_unseen.py::test_hits_cut_by_the_budget_defer_without_calling_the_judge`, `tests/unit/test_refuter_defers_unseen.py::test_refutation_resting_on_a_partly_served_identifier_is_deferred`, `tests/unit/test_refuter_defers_unseen.py::test_common_identifier_serves_the_nearest_fifty_hits`, `tests/unit/test_refuter_defers_unseen.py::test_unserved_reason_is_a_closed_set_template`, `tests/unit/test_refuter_defers_unseen.py::test_assembly_deadline_names_what_it_left_out`, `tests/unit/test_refuter_defers_unseen.py::test_bare_file_names_cost_one_ls_files_per_finding`, `tests/unit/test_refuter_defers_unseen.py::test_refuter_route_asks_for_a_wide_reply_without_thinking`, `tests/unit/test_refuter_defers_unseen.py::test_run_line_and_payload_show_the_refuter_cost`, `tests/unit/test_refuter_defers_unseen.py::test_evidence_window_is_measured_in_tokens_never_money`, `tests/unit/test_claude_code_output_cap.py::test_max_tokens_reaches_the_cli_as_its_output_cap`, `tests/unit/test_claude_code_output_cap.py::test_no_cap_keeps_the_parent_environment`, `tests/unit/test_claude_code_output_cap.py::test_cap_rides_on_the_filtered_environment`, `tests/unit/test_claude_code_output_cap.py::test_disabled_thinking_reaches_the_cli_as_a_zero_budget`

## Integration tests

- `tests/integration/test_refuter_defers_unserved_end_to_end.py::test_unserved_identifier_is_deferred_through_the_review_without_a_judge_call`

<!-- repoach-action-plan -->
```json
{
  "spec_id": "SP-REFUTER-JUDGES-WHAT-IT-CAN",
  "title": "Every deferred finding carries a named reason, and the refuter defers what it cannot see",
  "summary": "Hand-authored after the spec was approved: the gate refused with 'plan-absent' because an approved spec is graded against its committed plan only. Steps 1-3 record what was delivered before this spec (refuter 0.1, injection hardening 0.2, mechanical verifiers); step 4 is spec 0.1 (AC1-AC6, 7940f79); step 5 is this PR's 0.2 amendment (AC7-AC12, AC15-AC18). The plan adds one integration test the spec's unit-only ACs omit (superset), required by the src/ plan form. AC13 and AC14 are a live run and a measurement method, not pytest selectors.",
  "steps": [
    {
      "index": 1,
      "title": "The refuter judges design, security and spec_gap findings adversarially",
      "files": [
        "src/repoach/review/refuter.py",
        "src/repoach/review/findings.py",
        "tests/unit/test_refuter.py"
      ],
      "action": "Delivered before this spec (refuter persona 0.1, SP-REFUTER): judge_finding builds one prompt per proposed design/security/spec_gap finding from its cited evidence, asks the judge for a verdict line and moves the finding to refuted or verified; missing evidence, an unparseable verdict or a judge exception leave it proposed. judge_findings_for_pr walks a PR's proposed findings, counts the transitions, and never builds a judge when nothing is judgeable.",
      "commit_message": "feat(review): judge proposed findings with an adversarial refuter",
      "done_when": "pytest tests/unit/test_refuter.py passes",
      "unit_tests": [
        "tests/unit/test_refuter.py::test_parse_verdict_extracts_refuted_bool",
        "tests/unit/test_refuter.py::test_refute_finding_refuted",
        "tests/unit/test_refuter.py::test_refute_finding_verified",
        "tests/unit/test_refuter.py::test_refute_finding_proposed_on_missing_evidence",
        "tests/unit/test_refuter.py::test_refute_finding_proposed_on_unparseable_verdict",
        "tests/unit/test_refuter.py::test_refute_finding_proposed_when_judge_raises",
        "tests/unit/test_refuter.py::test_judge_findings_for_pr_transitions_and_counts"
      ]
    },
    {
      "index": 2,
      "title": "Injection-safe evidence channel and verdict line",
      "files": [
        "src/repoach/review/refuter.py",
        "prompts/review/refuter_0.2.1.md",
        "tests/unit/test_refuter.py"
      ],
      "action": "Delivered before this spec (refuter persona 0.2, SP-REFUTER-INJECTION-HARDEN, #93): the evidence travels in a delimited channel the judge is told never to obey, and the verdict is read from the last VERDICT: line only, so an object injected earlier in the reply or in the evidence cannot refute a blocking finding; bad verdict shapes are rejected.",
      "commit_message": "fix(refuter): injection-safe evidence channel and verdict line",
      "done_when": "pytest tests/unit/test_refuter.py -k \"parse_verdict or injection\" passes",
      "unit_tests": [
        "tests/unit/test_refuter.py::test_parse_verdict_rejects_bad_shapes",
        "tests/unit/test_refuter.py::test_parse_verdict_ignores_injected_leading_object",
        "tests/unit/test_refuter.py::test_evidence_injection_does_not_refute_blocking"
      ]
    },
    {
      "index": 3,
      "title": "Mechanical verifiers settle checkable claims without a model call",
      "files": [
        "src/repoach/review/finding_verifiers.py",
        "tests/unit/test_finding_verifiers.py"
      ],
      "action": "Delivered before this spec: verify_findings_for_pr checks missing_test, missing_docstring and lint claims on disk at the head (a test by name under a real tests tree, a docstring, a ruff run) and records the verdict with a diagnostic; design claims are left to the refuter; code under review never refutes a missing-test claim.",
      "commit_message": "feat(review): verify checkable findings mechanically",
      "done_when": "pytest tests/unit/test_finding_verifiers.py passes",
      "unit_tests": [
        "tests/unit/test_finding_verifiers.py::test_missing_test_refuted_when_symbol_exists",
        "tests/unit/test_finding_verifiers.py::test_missing_test_verified_when_absent",
        "tests/unit/test_finding_verifiers.py::test_missing_test_proposed_without_symbol",
        "tests/unit/test_finding_verifiers.py::test_missing_test_not_refuted_by_src_symbol",
        "tests/unit/test_finding_verifiers.py::test_missing_test_refuted_by_real_tests_dir_test",
        "tests/unit/test_finding_verifiers.py::test_src_planted_test_does_not_refute",
        "tests/unit/test_finding_verifiers.py::test_missing_docstring_refuted_when_documented",
        "tests/unit/test_finding_verifiers.py::test_missing_docstring_verified_when_undocumented",
        "tests/unit/test_finding_verifiers.py::test_missing_docstring_proposed_when_file_absent",
        "tests/unit/test_finding_verifiers.py::test_missing_docstring_refuted_on_exempt_path",
        "tests/unit/test_finding_verifiers.py::test_lint_verified_on_violation",
        "tests/unit/test_finding_verifiers.py::test_lint_refuted_when_clean",
        "tests/unit/test_finding_verifiers.py::test_design_finding_deferred",
        "tests/unit/test_finding_verifiers.py::test_verify_findings_for_pr_round_trip",
        "tests/unit/test_finding_verifiers.py::test_deferred_verification_persists_diagnostic"
      ]
    },
    {
      "index": 4,
      "title": "Every finding left without a verdict carries a named reason",
      "files": [
        "src/repoach/review/refuter.py",
        "src/repoach/review/orchestrator.py",
        "src/repoach/gates/config.py",
        "tests/unit/test_refuter.py",
        "tests/unit/test_review_team.py"
      ],
      "action": "Delivered by 7940f79 (spec 0.1, AC1-AC6): judge_findings_for_pr returns a JudgingReport with one Refutation per proposed finding in exactly one state (verified, refuted, deferred, unverified), the last two with a reason from a closed set (evidence outside the diff, evidence unreadable, judging cap N reached, verdict unparseable, judge call failed: <type>, not yet mechanically verified, checkout not at the head). The cap is [gates].refuter_max_judged (default 10), REPOACH env outranking it; the orchestrator logs one review_team.finding_deferred event per verdict-less finding and review_team.findings_judged with the four counts and model_calls.",
      "commit_message": "fix(review): name every finding the refuter leaves without a verdict",
      "done_when": "pytest tests/unit/test_refuter.py tests/unit/test_review_team.py -k \"deferral_reason or unverified_target or judges_every_readable or max_judged or finding_deferred_event\" passes",
      "unit_tests": [
        "tests/unit/test_refuter.py::test_judge_findings_no_targets_never_builds_judge",
        "tests/unit/test_refuter.py::test_deferral_reason_evidence_outside_the_diff",
        "tests/unit/test_refuter.py::test_deferral_reason_evidence_unreadable",
        "tests/unit/test_refuter.py::test_deferral_reason_verdict_unparseable",
        "tests/unit/test_refuter.py::test_deferral_reason_judge_call_failed_names_the_exception",
        "tests/unit/test_refuter.py::test_deferral_reason_judge_factory_failure_is_built_once",
        "tests/unit/test_refuter.py::test_deferral_reason_judging_cap_names_the_overflow_findings",
        "tests/unit/test_refuter.py::test_deferral_reason_checkout_is_not_the_mr_head",
        "tests/unit/test_refuter.py::test_unverified_target_not_yet_mechanically_verified",
        "tests/unit/test_refuter.py::test_judges_every_readable_finding_under_the_cap",
        "tests/unit/test_refuter.py::test_max_judged_configurable_from_gates_config",
        "tests/unit/test_refuter.py::test_max_judged_configurable_defaults_to_ten",
        "tests/unit/test_refuter.py::test_max_judged_configurable_invalid_value_falls_back_to_ten",
        "tests/unit/test_refuter.py::test_max_judged_env_outranks_repoach_toml",
        "tests/unit/test_refuter.py::test_max_judged_env_is_printed_by_doctor",
        "tests/unit/test_refuter.py::test_max_judged_refuses_a_boolean_in_repoach_toml",
        "tests/unit/test_refuter.py::test_docs_only_mr_names_every_finding_with_zero_model_calls",
        "tests/unit/test_refuter.py::test_every_finding_ends_in_exactly_one_state",
        "tests/unit/test_refuter.py::test_summary_line_matches_the_documented_shape",
        "tests/unit/test_refuter.py::test_scope_head_judges_this_run_and_counts_older_heads_as_stale",
        "tests/unit/test_review_team.py::test_orchestrator_logs_one_finding_deferred_event_per_deferral",
        "tests/unit/test_review_team.py::test_orchestrator_finding_deferred_event_absent_when_everything_is_judged"
      ]
    },
    {
      "index": 5,
      "title": "The refuter defers what it cannot see; its evidence fills its own context",
      "files": [
        "src/repoach/review/refuter.py",
        "src/repoach/review/refuter_evidence.py",
        "prompts/review/refuter_0.3.0.md",
        "src/repoach/gates/config.py",
        "src/repoach/review/review_cache.py",
        "src/repoach/review/orchestrator.py",
        "src/repoach/cli/review_cmds.py",
        "src/repoach/llm_proxy/providers/claude_code/client.py",
        "tests/unit/test_refuter_defers_unseen.py",
        "tests/unit/test_claude_code_output_cap.py",
        "tests/integration/test_refuter_defers_unserved_end_to_end.py"
      ],
      "action": "This PR (amendment 0.2, G6-G15, AC7-AC18): persona 0.3.0 offers refuted / verified / cannot_judge; cannot_judge, and a refuted whose reasoning only says the code was not visible, defer with 'evidence outside window'. refuter_evidence.build_evidence fills the refuter's context_tokens (default and floor 40 000, [gates].refuter_context_tokens, REPOACH_REFUTER_CONTEXT_TOKENS outranking it) in priority order from the pinned head tree, path-jailed: the whole cited file (a slice never below +-25 lines when over budget), git grep -w hits of every named identifier with +-10 lines (at most 50, nearest first), named paths and bare file names (one git ls-files), the diff hunks of the cited file and the cited spec section; everything cut is named in evidence_truncated. An identifier whose hits exist but none was served defers with 'evidence outside window (<ids> not served)' without a judge call; grep timeouts (60 s) and the 180 s assembly deadline are named. The judge's reply cap is 2 048 tokens with thinking disabled, reaching the claude_code CLI as CLAUDE_CODE_MAX_OUTPUT_TOKENS and MAX_THINKING_TOKENS=0. The run line ends 'evidence_tokens total T' and the JSON carries refuter_tokens {evidence, total}; no cost figure shapes the evidence (G14). The integration test drives the real review with a fake judge that must not be called. AC13 and AC14 are a live run and a measurement method, not pytest selectors.",
      "commit_message": "fix(refuter): defer what it cannot see, fill its own context",
      "done_when": "pytest tests/unit/test_refuter_defers_unseen.py tests/unit/test_claude_code_output_cap.py tests/integration/test_refuter_defers_unserved_end_to_end.py passes",
      "unit_tests": [
        "tests/unit/test_refuter_defers_unseen.py::test_parse_verdict_reads_literal_cannot_judge",
        "tests/unit/test_refuter_defers_unseen.py::test_parse_verdict_maps_unseen_refutation_to_cannot_judge",
        "tests/unit/test_refuter_defers_unseen.py::test_prompt_offers_cannot_judge_and_names_partial_evidence",
        "tests/unit/test_refuter_defers_unseen.py::test_evidence_outside_window_is_a_named_deferral",
        "tests/unit/test_refuter_defers_unseen.py::test_claim_about_truly_absent_code_is_deferred_with_its_reason",
        "tests/unit/test_refuter_defers_unseen.py::test_hand_typed_enum_is_seen_not_refuted",
        "tests/unit/test_refuter_defers_unseen.py::test_half_derived_table_is_seen_not_refuted",
        "tests/unit/test_refuter_defers_unseen.py::test_untested_migration_is_seen_not_refuted",
        "tests/unit/test_refuter_defers_unseen.py::test_whole_cited_file_is_served_beyond_the_old_window",
        "tests/unit/test_refuter_defers_unseen.py::test_file_over_the_budget_is_a_named_slice_never_below_the_floor",
        "tests/unit/test_refuter_defers_unseen.py::test_claim_identifiers_names_symbols_and_paths",
        "tests/unit/test_refuter_defers_unseen.py::test_search_serves_hits_with_ten_lines_of_context",
        "tests/unit/test_refuter_defers_unseen.py::test_search_caps_hits_at_fifty_and_names_the_overflow",
        "tests/unit/test_refuter_defers_unseen.py::test_budget_exhaustion_is_named_in_priority_order",
        "tests/unit/test_refuter_defers_unseen.py::test_diff_hunks_of_the_cited_file_are_served",
        "tests/unit/test_refuter_defers_unseen.py::test_cited_spec_section_is_served",
        "tests/unit/test_refuter_defers_unseen.py::test_bare_file_name_is_found_and_served_whole",
        "tests/unit/test_refuter_defers_unseen.py::test_context_tokens_floor_and_overrides",
        "tests/unit/test_refuter_defers_unseen.py::test_judge_receives_a_budget_of_its_own_context",
        "tests/unit/test_refuter_defers_unseen.py::test_search_is_path_jailed",
        "tests/unit/test_refuter_defers_unseen.py::test_search_hits_cannot_forge_a_verdict_line",
        "tests/unit/test_refuter_defers_unseen.py::test_refutation_evidence_survives_the_review_cache",
        "tests/unit/test_refuter_defers_unseen.py::test_grep_timeout_defers_without_calling_the_judge",
        "tests/unit/test_refuter_defers_unseen.py::test_hits_cut_by_the_budget_defer_without_calling_the_judge",
        "tests/unit/test_refuter_defers_unseen.py::test_refutation_resting_on_a_partly_served_identifier_is_deferred",
        "tests/unit/test_refuter_defers_unseen.py::test_common_identifier_serves_the_nearest_fifty_hits",
        "tests/unit/test_refuter_defers_unseen.py::test_unserved_reason_is_a_closed_set_template",
        "tests/unit/test_refuter_defers_unseen.py::test_assembly_deadline_names_what_it_left_out",
        "tests/unit/test_refuter_defers_unseen.py::test_bare_file_names_cost_one_ls_files_per_finding",
        "tests/unit/test_refuter_defers_unseen.py::test_refuter_route_asks_for_a_wide_reply_without_thinking",
        "tests/unit/test_refuter_defers_unseen.py::test_run_line_and_payload_show_the_refuter_cost",
        "tests/unit/test_refuter_defers_unseen.py::test_evidence_window_is_measured_in_tokens_never_money",
        "tests/unit/test_claude_code_output_cap.py::test_max_tokens_reaches_the_cli_as_its_output_cap",
        "tests/unit/test_claude_code_output_cap.py::test_no_cap_keeps_the_parent_environment",
        "tests/unit/test_claude_code_output_cap.py::test_cap_rides_on_the_filtered_environment",
        "tests/unit/test_claude_code_output_cap.py::test_disabled_thinking_reaches_the_cli_as_a_zero_budget"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_refuter_defers_unserved_end_to_end.py::test_unserved_identifier_is_deferred_through_the_review_without_a_judge_call"
  ]
}
```
