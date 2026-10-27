# SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ — A declared generated artefact is proved by its check command, not read

Declare derived files under [[review.generated]] read at the base ref; the reviewers see a summary paragraph instead of the artefact's diff; a secret-free repoach review generated job runs the declared check shell-free with a scrubbed environment and writes evidence bound to the head; the review records generated_checks rows and one blocking generated_artefact finding per path the check does not prove, and head_reverify settles them, and the older oversized findings of a newly declared path, from the rows at a later head without the refuter.

## Step 1 — Declare generated artefacts in repoach.toml

- **Files**: `src/repoach/project/config.py`, `src/repoach/project/repoach.toml.template`, `tests/unit/test_generated_artefacts_config.py`
- **Action**: In config.py add GeneratedSection (path, check, sources, producer_inputs, check_timeout_s) with validators: path and every source glob relative, no '..', no leading '/'; check an argv list whose check[0] is a repository script and whose later elements match ^[A-Za-z0-9._/=:@,+-]+$ once {head} and {base} are removed. ReviewSection gains generated: list[GeneratedSection] capped at GENERATED_ENTRIES_MAX (32). Add glob_regex / glob_matches (** crosses directories), glob_code_extension (a glob that could match a file of a declared language) and refuse_generated_conflicts (code glob or duplicate path), called by parse_project_config after validation. Errors inside an entry are named review.generated[<i>].<key> with the line of that key in the i-th [[review.generated]] block. Document the key in the template under [review] as a commented generated = [] line.
- **Commit**: `feat(config): declare generated artefacts under review.generated`
- **Done when**: pytest -q tests/unit/test_generated_artefacts_config.py passes
- **Unit tests**: `tests/unit/test_generated_artefacts_config.py::test_one_entry_loads_into_generated_entry`, `tests/unit/test_generated_artefacts_config.py::test_string_check_is_refused`, `tests/unit/test_generated_artefacts_config.py::test_shell_shaped_check_argument_is_refused`, `tests/unit/test_generated_artefacts_config.py::test_empty_sources_is_refused`, `tests/unit/test_generated_artefacts_config.py::test_path_glob_matching_python_is_refused`

## Step 2 — Read declarations at the base and summarise touched artefacts

- **Files**: `src/repoach/review/generated_artefacts.py`, `tests/fixtures/generated_artefacts/harness.py`, `tests/unit/test_generated_artefacts_gate.py`
- **Action**: Create generated_artefacts.py with GeneratedEntry, load_generated_entries(text, path, languages), declared_at_base(root, base_ref) reading repoach.toml with git show at the base ref, touched_artefacts(entries, diff_files) in declaration order (two entries matching one changed path is a ConfigError), changed_inputs (sources and producer inputs changed, the artefact excluded), summary_paragraph and summarise(diff, touched, base_blob, head_blob) replacing each touched unit by its diff --git header and one paragraph built from sizes, sha256 prefixes and paths only, and review_scope(head_dir, diff, base_ref). Add the test harness: a bare origin, a seed pushing refs/pull/<n>/head, a GitHubForge serving the three-dot diff, approving reviewers that record every diff, a counting judge and a recording runner.
- **Commit**: `feat(review): summarise declared generated artefacts for reviewers`
- **Done when**: pytest -q tests/unit/test_generated_artefacts_config.py::test_two_globs_matching_one_changed_path_are_refused passes
- **Unit tests**: `tests/unit/test_generated_artefacts_config.py::test_two_globs_matching_one_changed_path_are_refused`, `tests/unit/test_generated_artefacts_config.py::test_the_same_path_twice_is_refused`

## Step 3 — Feed the reviewers the summary and name the oversized remedy

- **Files**: `src/repoach/review/orchestrator.py`, `src/repoach/review/findings_bridge.py`, `tests/unit/test_generated_artefacts_gate.py`
- **Action**: In ReviewTeamOrchestrator._review_pr compute _generated_scope(diff, base_ref) from readable_base and review_scope (a failure logs and keeps the diff, so the oversized finding still applies) and pass the summarised diff to every reviewer, the lessons block, round two, record_review_ledger and the refuter. In findings_bridge expose OVERSIZED_FINDER and append 'declare it under [[review.generated]] with its check, or split it' to the oversized finding's claim (G4), its claim type, severity and status unchanged.
- **Commit**: `feat(review): show reviewers the generated-artefact summary`
- **Done when**: pytest -q tests/unit/test_generated_artefacts_gate.py::test_undeclared_oversized_names_the_remedy passes
- **Unit tests**: `tests/unit/test_generated_artefacts_gate.py::test_undeclared_oversized_names_the_remedy`, `tests/unit/test_generated_artefacts_gate.py::test_declaration_read_from_base_ref`

## Step 4 — Run checks, record generated_checks rows and blocking findings

- **Files**: `src/repoach/review/generated_artefacts.py`, `src/repoach/review/findings.py`, `src/repoach/review/finding_verifiers.py`
- **Action**: ClaimType gains GENERATED_ARTEFACT, a member of MECHANICAL_CLAIM_TYPES; its verifier keeps the finding verified at its own head. In generated_artefacts add GeneratedState, GeneratedCheck, the Runner seam and subprocess_runner (shell=False, never a shell), run_checks (scrubbed_env, check_timeout_s per run, checks_total_budget_s per PR on an injected clock, check absent at head, base script then head script when check[0] changed, no source changed when only the artefact moved), generated_findings (one blocking verified generated_artefact finding per unproved path, its result naming the cause), the generated_checks table, the review-generated-json evidence and load_review_evidence, which refuses another head or another artefact blob and then reports the path not run. record_review_ledger records rows and findings inside its fail-closed write.
- **Commit**: `feat(review): record generated checks and their blocking findings`
- **Done when**: pytest -q tests/unit/test_generated_artefacts_gate.py -k 'absent or cost or not_run' passes
- **Unit tests**: `tests/unit/test_generated_artefacts_gate.py::test_declared_check_fails_is_refused_with_reason`, `tests/unit/test_generated_artefacts_gate.py::test_declared_sources_untouched_is_refused`, `tests/unit/test_generated_artefacts_gate.py::test_check_script_absent_at_head_is_refused`, `tests/unit/test_generated_artefacts_gate.py::test_check_over_timeout_is_skipped_for_cost`, `tests/unit/test_generated_artefacts_gate.py::test_evidence_of_another_head_is_not_run`

## Step 5 — Add review generated and report what proved each artefact

- **Files**: `src/repoach/cli/review_cmds.py`, `src/repoach/review/report.py`, `tests/unit/test_review_ci_mode.py`
- **Action**: Add repoach review generated --pr --head --out [--base], which calls verify_generated: declarations at the base, changed paths from the merge base, a head tree (the clean checkout or a detached worktree), run_checks, and the evidence with integer checks_ms and overhead_ms. review pr gains --generated-evidence, the JSON generated array and one stderr line per run (generated: <path> verified by <check> (<ms> ms), with base script / head script under D3), producer inputs changed: <list>, and generated checks: <n> ms + <m> ms overhead. The Markdown report gains a Generated artefacts section; TeamOutcome carries generated and generated_lines, rebuilt from the rows on a replay.
- **Commit**: `feat(cli): run generated checks in a secret-free job and report them`
- **Done when**: pytest -q tests/unit/test_generated_artefacts_gate.py::test_report_names_the_proving_command passes
- **Unit tests**: `tests/unit/test_generated_artefacts_gate.py::test_check_never_runs_in_review_job`, `tests/unit/test_generated_artefacts_gate.py::test_report_names_the_proving_command`, `tests/unit/test_generated_artefacts_gate.py::test_delay_is_measured_and_printed`, `tests/unit/test_generated_artefacts_gate.py::test_changed_check_script_runs_base_and_head`, `tests/unit/test_review_ci_mode.py::test_review_pr_json_payload_matches_documented_schema`

## Step 6 — Settle generated and older oversized findings at head

- **Files**: `src/repoach/review/head_reverify.py`, `src/repoach/review/generated_artefacts.py`
- **Action**: Add settlements_at_head(db_path, pr, head) (resolved when every run of a path is verified, with 'declared generated at <sha7>, verified by <check>', still_open with the cause otherwise) and settle_at_head(finding, settlements) for generated_artefact findings and oversized findings of a declared path. reverify_older_findings settles them first, before the cache and before the refuter, so no judge call is made for them.
- **Commit**: `feat(review): settle generated findings at head without the refuter`
- **Done when**: pytest -q tests/unit/test_generated_artefacts_gate.py::test_older_oversized_findings_settle_without_refuter passes
- **Unit tests**: `tests/unit/test_generated_artefacts_gate.py::test_older_oversized_findings_settle_without_refuter`, `tests/unit/test_generated_artefacts_gate.py::test_producer_input_counts_as_source`, `tests/unit/test_generated_artefacts_gate.py::test_two_artefacts_two_rows_in_declaration_order`, `tests/unit/test_generated_artefacts_gate.py::test_declared_but_untouched_runs_nothing`, `tests/unit/test_generated_artefacts_gate.py::test_declared_check_passes_merges`

## Step 7 — Replay Yoob !322 from its recorded shape

- **Files**: `tests/integration/test_generated_artefacts_replay_yoob_322.py`, `tests/fixtures/generated_artefacts/yoob_322/shape.json`, `tests/fixtures/generated_artefacts/yoob_322/ledger.json`
- **Action**: Record !322's diff shape (corpus and user-guide units verbatim, the embeddings unit with its two vectors lines as a placeholder expanded to 1 556 480 characters), findings 3 and 6 as the ledger held them, the recorded --check output (exit 0) and the base repoach.toml declaring the embeddings. The integration test runs review generated with the recorded result, then review pr, and expects APPROVE, findings 3 and 6 resolved_at_head and zero judge calls.
- **Commit**: `test(review): replay yoob !322 with the embeddings declared`
- **Done when**: pytest -q tests/integration/test_generated_artefacts_replay_yoob_322.py passes
- **Unit tests**: `tests/unit/test_generated_artefacts_config.py::test_unknown_key_in_an_entry_is_refused`

## Step 8 — Document the declaration and the Yoob job

- **Files**: `docs/generated_artefacts.md`
- **Action**: Describe the [[review.generated]] keys and rules, what the review shows and records, the causes a finding names, the report lines, and the GitLab snippet: a secret-free review:generated job writing review-generated.json and review:gate needing it.
- **Commit**: `docs(review): document generated artefacts and the yoob ci job`
- **Done when**: docs/generated_artefacts.md names every key, cause and the job snippet
- **Unit tests**: _(docs-only step — none promised)_

## Integration tests

- `tests/integration/test_generated_artefacts_replay_yoob_322.py::test_yoob_322_approves_once_the_embeddings_are_declared`

<!-- repoach-action-plan -->
```json
{
  "spec_id": "SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ",
  "title": "A declared generated artefact is proved by its check command, not read",
  "summary": "Declare derived files under [[review.generated]] read at the base ref; the reviewers see a summary paragraph instead of the artefact's diff; a secret-free repoach review generated job runs the declared check shell-free with a scrubbed environment and writes evidence bound to the head; the review records generated_checks rows and one blocking generated_artefact finding per path the check does not prove, and head_reverify settles them, and the older oversized findings of a newly declared path, from the rows at a later head without the refuter.",
  "steps": [
    {
      "index": 1,
      "title": "Declare generated artefacts in repoach.toml",
      "files": [
        "src/repoach/project/config.py",
        "src/repoach/project/repoach.toml.template",
        "tests/unit/test_generated_artefacts_config.py"
      ],
      "action": "In config.py add GeneratedSection (path, check, sources, producer_inputs, check_timeout_s) with validators: path and every source glob relative, no '..', no leading '/'; check an argv list whose check[0] is a repository script and whose later elements match ^[A-Za-z0-9._/=:@,+-]+$ once {head} and {base} are removed. ReviewSection gains generated: list[GeneratedSection] capped at GENERATED_ENTRIES_MAX (32). Add glob_regex / glob_matches (** crosses directories), glob_code_extension (a glob that could match a file of a declared language) and refuse_generated_conflicts (code glob or duplicate path), called by parse_project_config after validation. Errors inside an entry are named review.generated[<i>].<key> with the line of that key in the i-th [[review.generated]] block. Document the key in the template under [review] as a commented generated = [] line.",
      "commit_message": "feat(config): declare generated artefacts under review.generated",
      "done_when": "pytest -q tests/unit/test_generated_artefacts_config.py passes",
      "unit_tests": [
        "tests/unit/test_generated_artefacts_config.py::test_one_entry_loads_into_generated_entry",
        "tests/unit/test_generated_artefacts_config.py::test_string_check_is_refused",
        "tests/unit/test_generated_artefacts_config.py::test_shell_shaped_check_argument_is_refused",
        "tests/unit/test_generated_artefacts_config.py::test_empty_sources_is_refused",
        "tests/unit/test_generated_artefacts_config.py::test_path_glob_matching_python_is_refused"
      ]
    },
    {
      "index": 2,
      "title": "Read declarations at the base and summarise touched artefacts",
      "files": [
        "src/repoach/review/generated_artefacts.py",
        "tests/fixtures/generated_artefacts/harness.py",
        "tests/unit/test_generated_artefacts_gate.py"
      ],
      "action": "Create generated_artefacts.py with GeneratedEntry, load_generated_entries(text, path, languages), declared_at_base(root, base_ref) reading repoach.toml with git show at the base ref, touched_artefacts(entries, diff_files) in declaration order (two entries matching one changed path is a ConfigError), changed_inputs (sources and producer inputs changed, the artefact excluded), summary_paragraph and summarise(diff, touched, base_blob, head_blob) replacing each touched unit by its diff --git header and one paragraph built from sizes, sha256 prefixes and paths only, and review_scope(head_dir, diff, base_ref). Add the test harness: a bare origin, a seed pushing refs/pull/<n>/head, a GitHubForge serving the three-dot diff, approving reviewers that record every diff, a counting judge and a recording runner.",
      "commit_message": "feat(review): summarise declared generated artefacts for reviewers",
      "done_when": "pytest -q tests/unit/test_generated_artefacts_config.py::test_two_globs_matching_one_changed_path_are_refused passes",
      "unit_tests": [
        "tests/unit/test_generated_artefacts_config.py::test_two_globs_matching_one_changed_path_are_refused",
        "tests/unit/test_generated_artefacts_config.py::test_the_same_path_twice_is_refused"
      ]
    },
    {
      "index": 3,
      "title": "Feed the reviewers the summary and name the oversized remedy",
      "files": [
        "src/repoach/review/orchestrator.py",
        "src/repoach/review/findings_bridge.py",
        "tests/unit/test_generated_artefacts_gate.py"
      ],
      "action": "In ReviewTeamOrchestrator._review_pr compute _generated_scope(diff, base_ref) from readable_base and review_scope (a failure logs and keeps the diff, so the oversized finding still applies) and pass the summarised diff to every reviewer, the lessons block, round two, record_review_ledger and the refuter. In findings_bridge expose OVERSIZED_FINDER and append 'declare it under [[review.generated]] with its check, or split it' to the oversized finding's claim (G4), its claim type, severity and status unchanged.",
      "commit_message": "feat(review): show reviewers the generated-artefact summary",
      "done_when": "pytest -q tests/unit/test_generated_artefacts_gate.py::test_undeclared_oversized_names_the_remedy passes",
      "unit_tests": [
        "tests/unit/test_generated_artefacts_gate.py::test_undeclared_oversized_names_the_remedy",
        "tests/unit/test_generated_artefacts_gate.py::test_declaration_read_from_base_ref"
      ]
    },
    {
      "index": 4,
      "title": "Run checks, record generated_checks rows and blocking findings",
      "files": [
        "src/repoach/review/generated_artefacts.py",
        "src/repoach/review/findings.py",
        "src/repoach/review/finding_verifiers.py"
      ],
      "action": "ClaimType gains GENERATED_ARTEFACT, a member of MECHANICAL_CLAIM_TYPES; its verifier keeps the finding verified at its own head. In generated_artefacts add GeneratedState, GeneratedCheck, the Runner seam and subprocess_runner (shell=False, never a shell), run_checks (scrubbed_env, check_timeout_s per run, checks_total_budget_s per PR on an injected clock, check absent at head, base script then head script when check[0] changed, no source changed when only the artefact moved), generated_findings (one blocking verified generated_artefact finding per unproved path, its result naming the cause), the generated_checks table, the review-generated-json evidence and load_review_evidence, which refuses another head or another artefact blob and then reports the path not run. record_review_ledger records rows and findings inside its fail-closed write.",
      "commit_message": "feat(review): record generated checks and their blocking findings",
      "done_when": "pytest -q tests/unit/test_generated_artefacts_gate.py -k 'absent or cost or not_run' passes",
      "unit_tests": [
        "tests/unit/test_generated_artefacts_gate.py::test_declared_check_fails_is_refused_with_reason",
        "tests/unit/test_generated_artefacts_gate.py::test_declared_sources_untouched_is_refused",
        "tests/unit/test_generated_artefacts_gate.py::test_check_script_absent_at_head_is_refused",
        "tests/unit/test_generated_artefacts_gate.py::test_check_over_timeout_is_skipped_for_cost",
        "tests/unit/test_generated_artefacts_gate.py::test_evidence_of_another_head_is_not_run"
      ]
    },
    {
      "index": 5,
      "title": "Add review generated and report what proved each artefact",
      "files": [
        "src/repoach/cli/review_cmds.py",
        "src/repoach/review/report.py",
        "tests/unit/test_review_ci_mode.py"
      ],
      "action": "Add repoach review generated --pr --head --out [--base], which calls verify_generated: declarations at the base, changed paths from the merge base, a head tree (the clean checkout or a detached worktree), run_checks, and the evidence with integer checks_ms and overhead_ms. review pr gains --generated-evidence, the JSON generated array and one stderr line per run (generated: <path> verified by <check> (<ms> ms), with base script / head script under D3), producer inputs changed: <list>, and generated checks: <n> ms + <m> ms overhead. The Markdown report gains a Generated artefacts section; TeamOutcome carries generated and generated_lines, rebuilt from the rows on a replay.",
      "commit_message": "feat(cli): run generated checks in a secret-free job and report them",
      "done_when": "pytest -q tests/unit/test_generated_artefacts_gate.py::test_report_names_the_proving_command passes",
      "unit_tests": [
        "tests/unit/test_generated_artefacts_gate.py::test_check_never_runs_in_review_job",
        "tests/unit/test_generated_artefacts_gate.py::test_report_names_the_proving_command",
        "tests/unit/test_generated_artefacts_gate.py::test_delay_is_measured_and_printed",
        "tests/unit/test_generated_artefacts_gate.py::test_changed_check_script_runs_base_and_head",
        "tests/unit/test_review_ci_mode.py::test_review_pr_json_payload_matches_documented_schema"
      ]
    },
    {
      "index": 6,
      "title": "Settle generated and older oversized findings at head",
      "files": [
        "src/repoach/review/head_reverify.py",
        "src/repoach/review/generated_artefacts.py"
      ],
      "action": "Add settlements_at_head(db_path, pr, head) (resolved when every run of a path is verified, with 'declared generated at <sha7>, verified by <check>', still_open with the cause otherwise) and settle_at_head(finding, settlements) for generated_artefact findings and oversized findings of a declared path. reverify_older_findings settles them first, before the cache and before the refuter, so no judge call is made for them.",
      "commit_message": "feat(review): settle generated findings at head without the refuter",
      "done_when": "pytest -q tests/unit/test_generated_artefacts_gate.py::test_older_oversized_findings_settle_without_refuter passes",
      "unit_tests": [
        "tests/unit/test_generated_artefacts_gate.py::test_older_oversized_findings_settle_without_refuter",
        "tests/unit/test_generated_artefacts_gate.py::test_producer_input_counts_as_source",
        "tests/unit/test_generated_artefacts_gate.py::test_two_artefacts_two_rows_in_declaration_order",
        "tests/unit/test_generated_artefacts_gate.py::test_declared_but_untouched_runs_nothing",
        "tests/unit/test_generated_artefacts_gate.py::test_declared_check_passes_merges"
      ]
    },
    {
      "index": 7,
      "title": "Replay Yoob !322 from its recorded shape",
      "files": [
        "tests/integration/test_generated_artefacts_replay_yoob_322.py",
        "tests/fixtures/generated_artefacts/yoob_322/shape.json",
        "tests/fixtures/generated_artefacts/yoob_322/ledger.json"
      ],
      "action": "Record !322's diff shape (corpus and user-guide units verbatim, the embeddings unit with its two vectors lines as a placeholder expanded to 1 556 480 characters), findings 3 and 6 as the ledger held them, the recorded --check output (exit 0) and the base repoach.toml declaring the embeddings. The integration test runs review generated with the recorded result, then review pr, and expects APPROVE, findings 3 and 6 resolved_at_head and zero judge calls.",
      "commit_message": "test(review): replay yoob !322 with the embeddings declared",
      "done_when": "pytest -q tests/integration/test_generated_artefacts_replay_yoob_322.py passes",
      "unit_tests": [
        "tests/unit/test_generated_artefacts_config.py::test_unknown_key_in_an_entry_is_refused"
      ]
    },
    {
      "index": 8,
      "title": "Document the declaration and the Yoob job",
      "files": [
        "docs/generated_artefacts.md"
      ],
      "action": "Describe the [[review.generated]] keys and rules, what the review shows and records, the causes a finding names, the report lines, and the GitLab snippet: a secret-free review:generated job writing review-generated.json and review:gate needing it.",
      "commit_message": "docs(review): document generated artefacts and the yoob ci job",
      "done_when": "docs/generated_artefacts.md names every key, cause and the job snippet",
      "unit_tests": []
    }
  ],
  "integration_tests": [
    "tests/integration/test_generated_artefacts_replay_yoob_322.py::test_yoob_322_approves_once_the_embeddings_are_declared"
  ]
}
```
