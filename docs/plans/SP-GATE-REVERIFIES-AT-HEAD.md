# SP-GATE-REVERIFIES-AT-HEAD — The gate re-verifies open findings of older heads at the head

Hand-authored after the spec was approved: the gate refused with 'plan-absent' because an approved spec is graded against its committed plan only. Steps 1-3 record what #232 and #239 delivered (AC1-AC7, AC10-AC13); step 4 is this PR's 0.3 amendment (AC15-AC17). The plan adds one integration test the spec's unit-only ACs omit (superset), required by the src/ plan form. AC8, AC9 and AC14 are live demo and arch commands, not pytest selectors.

## Step 1 — The gate re-verifies open findings of older heads at the head

- **Files**: `src/repoach/review/head_reverify.py`, `src/repoach/review/auto_merge.py`, `src/repoach/review/merge_gate.py`, `src/repoach/review/findings.py`, `src/repoach/review/refuter.py`, `src/repoach/cli/review_cmds.py`, `tests/unit/test_gate_reverifies_at_head.py`
- **Action**: Add head_reverify.reverify_older_findings and call it from evaluate_merge_gate on the pinned head tree: every open blocking finding recorded at an older head is re-checked at the head (judged claims by the refuter over the head evidence, mechanical claims on disk with no model call); a settled finding becomes resolved_at_head with 'resolved at <sha7>: ...', a surviving one is named 'raised at <A7>, still present at <B7>', an unjudgeable one is 'unresolved: needs judgement (...)'. Stamp each verdict with checked_at_sha so a second gate at the same head makes no model call. review gate prints one 'gate: re-verified' stderr line and a 'reverified' JSON key (G1-G6, AC1-AC6).
- **Commit**: `fix(gate): re-verify open findings at the new head before refusing`
- **Done when**: pytest tests/unit/test_gate_reverifies_at_head.py passes
- **Unit tests**: `tests/unit/test_gate_reverifies_at_head.py::test_human_fix_resolves_the_finding_and_the_gate_passes`, `tests/unit/test_gate_reverifies_at_head.py::test_fix_that_keeps_the_defect_is_named_still_present`, `tests/unit/test_gate_reverifies_at_head.py::test_unjudgeable_finding_is_needs_judgement_not_silent`, `tests/unit/test_gate_reverifies_at_head.py::test_vitest_test_added_at_head_resolves_missing_test`, `tests/unit/test_gate_reverifies_at_head.py::test_second_gate_at_the_same_head_makes_no_model_call`, `tests/unit/test_gate_reverifies_at_head.py::test_cli_prints_the_line_and_the_json_key`

## Step 2 — Vitest selectors are graded by their it block body

- **Files**: `src/repoach/review/spec_gate.py`, `tests/unit/test_spec_gate_vitest_body.py`
- **Action**: In spec_gate, grade a Vitest selector (' > ' or '::' separator) by the body of its it/test block: a block with statements is non-trivial; an empty callback, it.todo, and a .ts file that is not valid Python are never non-trivial bodies, and the arrow selector counts as present in coverage (AC7).
- **Commit**: `fix(spec-gate): grade vitest selectors by their it block body`
- **Done when**: pytest tests/unit/test_spec_gate_vitest_body.py passes
- **Unit tests**: `tests/unit/test_spec_gate_vitest_body.py::test_vitest_block_with_a_body_is_non_trivial`, `tests/unit/test_spec_gate_vitest_body.py::test_vitest_block_without_a_body_is_trivial`, `tests/unit/test_spec_gate_vitest_body.py::test_typescript_parse_error_is_never_a_non_empty_body`, `tests/unit/test_spec_gate_vitest_body.py::test_coverage_counts_the_arrow_selector_present`

## Step 3 — review pr re-verifies older findings at the fix head

- **Files**: `src/repoach/review/head_reverify.py`, `src/repoach/review/orchestrator.py`, `src/repoach/review/report.py`, `src/repoach/cli/review_cmds.py`, `tests/unit/test_review_pr_reverifies_at_head.py`
- **Action**: run_review at head B, reading the pinned head tree, runs reverify_older_findings after the refuter pass and before the team verdict, so a fix head with no new finding approves; the report and 'review pr' JSON carry reverified and reverify_reasons, stderr one 'review: re-verified' line; a second review and gate at the same head make no judge call for the carried finding (G7-G10, AC10-AC13).
- **Commit**: `fix(review): review pr re-verifies older findings at the fix head`
- **Done when**: pytest tests/unit/test_review_pr_reverifies_at_head.py passes
- **Unit tests**: `tests/unit/test_review_pr_reverifies_at_head.py::test_review_at_the_fix_head_approves`, `tests/unit/test_review_pr_reverifies_at_head.py::test_review_at_a_non_fix_head_names_the_carried_finding`, `tests/unit/test_review_pr_reverifies_at_head.py::test_second_review_and_gate_at_the_same_head_make_no_model_call`, `tests/unit/test_review_pr_reverifies_at_head.py::test_cli_prints_the_line_and_the_json_keys`

## Step 4 — A required Python test is found by name at the head

- **Files**: `src/repoach/review/finding_verifiers.py`, `src/repoach/review/head_reverify.py`, `tests/unit/test_gate_reverifies_at_head.py`, `tests/unit/test_finding_verifiers.py`, `tests/integration/test_gate_reverifies_at_head_end_to_end.py`
- **Action**: The Python missing_test re-check searches every tracked Python test file of the pinned head tree (a configured test root, any tests/ or test/ directory, test_*.py, *_test.py, conftest.py), never code under review; a found test resolves with '<test> exists at <path>:<line>', none keeps it open with 'no test for <names> (searched <n> test files at <sha7>)'; a cached 'still present' stamp yields to an on-disk check that settles the finding differently at the head (G11, AC15-AC17). The integration test drives the real gate over a real origin: a judged and a mechanical finding of head A both settle at the fix head B, the PR merges, a second gate at B builds no judge.
- **Commit**: `fix(gate): find a required python test by name at the head`
- **Done when**: pytest tests/unit/test_gate_reverifies_at_head.py tests/unit/test_finding_verifiers.py tests/integration/test_gate_reverifies_at_head_end_to_end.py passes
- **Unit tests**: `tests/unit/test_gate_reverifies_at_head.py::test_python_test_written_under_a_nested_tests_dir_resolves_missing_test`, `tests/unit/test_gate_reverifies_at_head.py::test_python_test_absent_at_head_names_what_was_searched`, `tests/unit/test_gate_reverifies_at_head.py::test_stale_still_present_stamp_at_the_head_yields_to_the_test_found_there`, `tests/unit/test_finding_verifiers.py::test_missing_test_refuted_by_a_test_under_a_nested_tests_dir`, `tests/unit/test_finding_verifiers.py::test_missing_test_not_refuted_by_a_test_named_def_in_code_under_review`

## Integration tests

- `tests/integration/test_gate_reverifies_at_head_end_to_end.py::test_gate_at_the_fix_head_settles_judged_and_mechanical_findings_and_merges`

<!-- repoach-action-plan -->
```json
{
  "spec_id": "SP-GATE-REVERIFIES-AT-HEAD",
  "title": "The gate re-verifies open findings of older heads at the head",
  "summary": "Hand-authored after the spec was approved: the gate refused with 'plan-absent' because an approved spec is graded against its committed plan only. Steps 1-3 record what #232 and #239 delivered (AC1-AC7, AC10-AC13); step 4 is this PR's 0.3 amendment (AC15-AC17). The plan adds one integration test the spec's unit-only ACs omit (superset), required by the src/ plan form. AC8, AC9 and AC14 are live demo and arch commands, not pytest selectors.",
  "steps": [
    {
      "index": 1,
      "title": "The gate re-verifies open findings of older heads at the head",
      "files": [
        "src/repoach/review/head_reverify.py",
        "src/repoach/review/auto_merge.py",
        "src/repoach/review/merge_gate.py",
        "src/repoach/review/findings.py",
        "src/repoach/review/refuter.py",
        "src/repoach/cli/review_cmds.py",
        "tests/unit/test_gate_reverifies_at_head.py"
      ],
      "action": "Add head_reverify.reverify_older_findings and call it from evaluate_merge_gate on the pinned head tree: every open blocking finding recorded at an older head is re-checked at the head (judged claims by the refuter over the head evidence, mechanical claims on disk with no model call); a settled finding becomes resolved_at_head with 'resolved at <sha7>: ...', a surviving one is named 'raised at <A7>, still present at <B7>', an unjudgeable one is 'unresolved: needs judgement (...)'. Stamp each verdict with checked_at_sha so a second gate at the same head makes no model call. review gate prints one 'gate: re-verified' stderr line and a 'reverified' JSON key (G1-G6, AC1-AC6).",
      "commit_message": "fix(gate): re-verify open findings at the new head before refusing",
      "done_when": "pytest tests/unit/test_gate_reverifies_at_head.py passes",
      "unit_tests": [
        "tests/unit/test_gate_reverifies_at_head.py::test_human_fix_resolves_the_finding_and_the_gate_passes",
        "tests/unit/test_gate_reverifies_at_head.py::test_fix_that_keeps_the_defect_is_named_still_present",
        "tests/unit/test_gate_reverifies_at_head.py::test_unjudgeable_finding_is_needs_judgement_not_silent",
        "tests/unit/test_gate_reverifies_at_head.py::test_vitest_test_added_at_head_resolves_missing_test",
        "tests/unit/test_gate_reverifies_at_head.py::test_second_gate_at_the_same_head_makes_no_model_call",
        "tests/unit/test_gate_reverifies_at_head.py::test_cli_prints_the_line_and_the_json_key"
      ]
    },
    {
      "index": 2,
      "title": "Vitest selectors are graded by their it block body",
      "files": [
        "src/repoach/review/spec_gate.py",
        "tests/unit/test_spec_gate_vitest_body.py"
      ],
      "action": "In spec_gate, grade a Vitest selector (' > ' or '::' separator) by the body of its it/test block: a block with statements is non-trivial; an empty callback, it.todo, and a .ts file that is not valid Python are never non-trivial bodies, and the arrow selector counts as present in coverage (AC7).",
      "commit_message": "fix(spec-gate): grade vitest selectors by their it block body",
      "done_when": "pytest tests/unit/test_spec_gate_vitest_body.py passes",
      "unit_tests": [
        "tests/unit/test_spec_gate_vitest_body.py::test_vitest_block_with_a_body_is_non_trivial",
        "tests/unit/test_spec_gate_vitest_body.py::test_vitest_block_without_a_body_is_trivial",
        "tests/unit/test_spec_gate_vitest_body.py::test_typescript_parse_error_is_never_a_non_empty_body",
        "tests/unit/test_spec_gate_vitest_body.py::test_coverage_counts_the_arrow_selector_present"
      ]
    },
    {
      "index": 3,
      "title": "review pr re-verifies older findings at the fix head",
      "files": [
        "src/repoach/review/head_reverify.py",
        "src/repoach/review/orchestrator.py",
        "src/repoach/review/report.py",
        "src/repoach/cli/review_cmds.py",
        "tests/unit/test_review_pr_reverifies_at_head.py"
      ],
      "action": "run_review at head B, reading the pinned head tree, runs reverify_older_findings after the refuter pass and before the team verdict, so a fix head with no new finding approves; the report and 'review pr' JSON carry reverified and reverify_reasons, stderr one 'review: re-verified' line; a second review and gate at the same head make no judge call for the carried finding (G7-G10, AC10-AC13).",
      "commit_message": "fix(review): review pr re-verifies older findings at the fix head",
      "done_when": "pytest tests/unit/test_review_pr_reverifies_at_head.py passes",
      "unit_tests": [
        "tests/unit/test_review_pr_reverifies_at_head.py::test_review_at_the_fix_head_approves",
        "tests/unit/test_review_pr_reverifies_at_head.py::test_review_at_a_non_fix_head_names_the_carried_finding",
        "tests/unit/test_review_pr_reverifies_at_head.py::test_second_review_and_gate_at_the_same_head_make_no_model_call",
        "tests/unit/test_review_pr_reverifies_at_head.py::test_cli_prints_the_line_and_the_json_keys"
      ]
    },
    {
      "index": 4,
      "title": "A required Python test is found by name at the head",
      "files": [
        "src/repoach/review/finding_verifiers.py",
        "src/repoach/review/head_reverify.py",
        "tests/unit/test_gate_reverifies_at_head.py",
        "tests/unit/test_finding_verifiers.py",
        "tests/integration/test_gate_reverifies_at_head_end_to_end.py"
      ],
      "action": "The Python missing_test re-check searches every tracked Python test file of the pinned head tree (a configured test root, any tests/ or test/ directory, test_*.py, *_test.py, conftest.py), never code under review; a found test resolves with '<test> exists at <path>:<line>', none keeps it open with 'no test for <names> (searched <n> test files at <sha7>)'; a cached 'still present' stamp yields to an on-disk check that settles the finding differently at the head (G11, AC15-AC17). The integration test drives the real gate over a real origin: a judged and a mechanical finding of head A both settle at the fix head B, the PR merges, a second gate at B builds no judge.",
      "commit_message": "fix(gate): find a required python test by name at the head",
      "done_when": "pytest tests/unit/test_gate_reverifies_at_head.py tests/unit/test_finding_verifiers.py tests/integration/test_gate_reverifies_at_head_end_to_end.py passes",
      "unit_tests": [
        "tests/unit/test_gate_reverifies_at_head.py::test_python_test_written_under_a_nested_tests_dir_resolves_missing_test",
        "tests/unit/test_gate_reverifies_at_head.py::test_python_test_absent_at_head_names_what_was_searched",
        "tests/unit/test_gate_reverifies_at_head.py::test_stale_still_present_stamp_at_the_head_yields_to_the_test_found_there",
        "tests/unit/test_finding_verifiers.py::test_missing_test_refuted_by_a_test_under_a_nested_tests_dir",
        "tests/unit/test_finding_verifiers.py::test_missing_test_not_refuted_by_a_test_named_def_in_code_under_review"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_gate_reverifies_at_head_end_to_end.py::test_gate_at_the_fix_head_settles_judged_and_mechanical_findings_and_merges"
  ]
}
```
