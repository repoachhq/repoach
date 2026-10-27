# SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ — A declared generated artefact is proved by its check command, not read

Declare derived files under [[review.generated]] read at the base ref; the reviewers see a summary paragraph instead of the artefact's diff; a secret-free repoach review generated job runs the declared check shell-free, scrubbed, in a throwaway clone, and writes evidence bound to the head; the review recomputes what it can derive from that evidence, records generated_checks rows and one blocking generated_artefact finding per path the check does not prove, and head_reverify settles them, and the older oversized findings of a newly declared path, from the rows at a later head without the refuter.

## Step 1 — Declare generated artefacts in repoach.toml

- **Files**: `src/repoach/project/globs.py`, `src/repoach/project/config.py`, `tests/unit/test_generated_artefacts_config.py`
- **Action**: In globs.py add glob_regex and glob_matches (** crosses directories, * and ? stay in one segment) and glob_code_extension(pattern, extensions), which tells whether a glob can match a file with one of the given extensions; globs.py imports nothing of repoach. In config.py add GeneratedSection (path, check, sources, producer_inputs, check_timeout_s): path and every source glob relative, no '..', no leading '/'; check an argv list whose check[0] is a repository script and whose later elements match ^[A-Za-z0-9._/=:@,+-]+$ once {head} and {base} are removed. ReviewSection gains generated, capped at GENERATED_ENTRIES_MAX (32). refuse_duplicate_generated refuses a duplicate path; refuse_code_globs refuses a code glob, given the extensions. Errors inside an entry are named review.generated[<i>].<key> with the line of that key in the i-th [[review.generated]] block.
- **Commit**: `feat(config): declare generated artefacts under review.generated`
- **Done when**: pytest -q tests/unit/test_generated_artefacts_config.py passes
- **Unit tests**: `tests/unit/test_generated_artefacts_config.py::test_one_entry_loads_into_generated_entry`, `tests/unit/test_generated_artefacts_config.py::test_string_check_is_refused`, `tests/unit/test_generated_artefacts_config.py::test_shell_shaped_check_argument_is_refused`, `tests/unit/test_generated_artefacts_config.py::test_empty_sources_is_refused`, `tests/unit/test_generated_artefacts_config.py::test_path_leaving_the_repository_is_refused`

## Step 2 — Document the key in the shipped template

- **Files**: `src/repoach/project/repoach.toml.template`, `tests/unit/test_project_config.py`
- **Action**: Under [review] in the template, describe [[review.generated]] in comments and add a commented generated = [] line, so the template round-trip test finds a line for every key.
- **Commit**: `docs(config): describe review.generated in the template`
- **Done when**: pytest -q tests/unit/test_project_config.py passes
- **Unit tests**: `tests/unit/test_project_config.py::TestTemplate::test_every_key_has_a_template_line_and_round_trips`

## Step 3 — Read declarations at the base and summarise touched artefacts

- **Files**: `src/repoach/review/generated_artefacts.py`, `tests/fixtures/generated_artefacts/harness.py`, `tests/unit/test_generated_artefacts_gate.py`
- **Action**: Create generated_artefacts.py with GeneratedEntry, load_generated_entries(text, path, languages) (refusing a path glob that could match a file of the file's or the given languages, through code_extensions over gates.languages), declared_at_base(root, base_ref) reading repoach.toml with git show at the base ref, touched_artefacts in declaration order (two entries matching one changed path is a ConfigError), changed_inputs, summary_paragraph and summarise replacing each touched unit by its diff --git header and one paragraph of sizes, sha256 prefixes and paths, and review_scope. Add the test harness: a bare origin, a seed pushing refs/pull/<n>/head, a GitHubForge serving the three-dot diff, approving reviewers that record every diff, a counting judge and a recording runner.
- **Commit**: `feat(review): summarise declared generated artefacts for reviewers`
- **Done when**: pytest -q tests/unit/test_generated_artefacts_config.py passes
- **Unit tests**: `tests/unit/test_generated_artefacts_config.py::test_path_glob_matching_python_is_refused`, `tests/unit/test_generated_artefacts_config.py::test_code_glob_is_judged_against_the_declared_languages`, `tests/unit/test_generated_artefacts_config.py::test_two_globs_matching_one_changed_path_are_refused`, `tests/unit/test_generated_artefacts_config.py::test_the_same_path_twice_is_refused`

## Step 4 — Feed the reviewers the summary and name the oversized remedy

- **Files**: `src/repoach/review/orchestrator.py`, `src/repoach/review/findings_bridge.py`
- **Action**: In ReviewTeamOrchestrator._review_pr compute _generated_scope(diff, base_ref) from readable_base and review_scope (a failure logs and keeps the diff, so the oversized finding still applies) and pass the summarised diff to every reviewer, the lessons block, round two, record_review_ledger and the refuter. In findings_bridge expose OVERSIZED_FINDER and append 'declare it under [[review.generated]] with its check, or split it' to the oversized finding's claim (G4), its claim type, severity and status unchanged.
- **Commit**: `feat(review): show reviewers the generated-artefact summary`
- **Done when**: pytest -q tests/unit/test_generated_artefacts_gate.py::test_undeclared_oversized_names_the_remedy passes
- **Unit tests**: `tests/unit/test_generated_artefacts_gate.py::test_undeclared_oversized_names_the_remedy`, `tests/unit/test_generated_artefacts_gate.py::test_declaration_read_from_base_ref`

## Step 5 — Run checks and build blocking findings

- **Files**: `src/repoach/review/generated_artefacts.py`, `src/repoach/review/findings.py`, `src/repoach/review/finding_verifiers.py`
- **Action**: ClaimType gains GENERATED_ARTEFACT, a member of MECHANICAL_CLAIM_TYPES; its verifier keeps the finding verified at its own head. In generated_artefacts add GeneratedState, GeneratedCheck, run_checks over the Runner seam (scrubbed_env, check_timeout_s per run, checks_total_budget_s per PR on an injected clock, check absent at head, a symlinked check script refused, base script then head script when check[0] changed, no source changed when only the artefact moved), generated_findings (one blocking verified generated_artefact finding per unproved path, its cause made inert), the review-generated-json evidence, and load_review_evidence, which refuses another head or another artefact blob and recomputes argv, sources and state at review time.
- **Commit**: `feat(review): record generated checks and their blocking findings`
- **Done when**: pytest -q tests/unit/test_generated_artefacts_gate.py -k 'absent or cost or not_run' passes
- **Unit tests**: `tests/unit/test_generated_artefacts_gate.py::test_declared_check_fails_is_refused_with_reason`, `tests/unit/test_generated_artefacts_gate.py::test_declared_sources_untouched_is_refused`, `tests/unit/test_generated_artefacts_gate.py::test_check_script_absent_at_head_is_refused`, `tests/unit/test_generated_artefacts_gate.py::test_check_over_timeout_is_skipped_for_cost`, `tests/unit/test_generated_artefacts_gate.py::test_evidence_of_another_head_is_not_run`

## Step 6 — Store the generated_checks rows

- **Files**: `src/repoach/review/generated_store.py`, `src/repoach/review/orchestrator.py`, `tests/unit/test_taxonomy_constants_dedup.py`
- **Action**: generated_store.py holds the generated_checks table (PR, head, path, argv, exit code, ms, output tail, state, script ref, sources, producer inputs, artefact sha256, timing), record_generated_checks, fetch_generated_checks and settlements_at_head. record_review_ledger records the rows and the findings inside its fail-closed write; the orchestrator loads the evidence named by generated_evidence.
- **Commit**: `feat(review): store generated checks per head`
- **Done when**: pytest -q tests/unit/test_generated_artefacts_gate.py::test_evidence_states_are_recomputed_at_review passes
- **Unit tests**: `tests/unit/test_generated_artefacts_gate.py::test_evidence_states_are_recomputed_at_review`, `tests/unit/test_generated_artefacts_gate.py::test_check_output_is_made_inert_in_the_cause`, `tests/unit/test_generated_artefacts_gate.py::test_symlinked_check_script_is_never_run`, `tests/unit/test_generated_artefacts_gate.py::test_a_script_turned_into_a_symlink_by_the_base_run_is_not_written_through`

## Step 7 — Add review generated, the verification job

- **Files**: `src/repoach/review/generated_verify.py`, `src/repoach/cli/review_cmds.py`, `tests/unit/test_review_ci_mode.py`
- **Action**: generated_verify.py holds head_tree (a throwaway clone with its own git directory, removed on exit) and verify_generated(root, pr, head, base_ref, state_dir, runner): declarations at the base, changed paths from the merge base, run_checks, and the evidence with integer checks_ms and overhead_ms. repoach review generated --pr --head --out [--base] calls it with GENERATED_RUNNER; held_secrets and reexec_scrubbed re-run it (python -P) without any secret-bearing variable it holds. review pr gains --generated-evidence, the JSON generated array and one stderr line per run (generated: <path> verified by <check> (<ms> ms), base script / head script under D3), producer inputs changed: <list>, and generated checks: <n> ms + <m> ms overhead.
- **Commit**: `feat(cli): run generated checks in a secret-free job and report them`
- **Done when**: pytest -q tests/unit/test_generated_artefacts_gate.py::test_report_names_the_proving_command passes
- **Unit tests**: `tests/unit/test_generated_artefacts_gate.py::test_check_never_runs_in_review_job`, `tests/unit/test_generated_artefacts_gate.py::test_report_names_the_proving_command`, `tests/unit/test_generated_artefacts_gate.py::test_delay_is_measured_and_printed`, `tests/unit/test_generated_artefacts_gate.py::test_changed_check_script_runs_base_and_head`, `tests/unit/test_generated_artefacts_gate.py::test_head_tree_is_a_clone_with_its_own_git_dir`

## Step 8 — Report the generated artefacts in the Markdown report

- **Files**: `src/repoach/review/report.py`, `src/repoach/review/orchestrator.py`
- **Action**: render_ledger_report gains generated_lines, rendered as a Generated artefacts section inside a fence one backtick longer than any backtick run in the lines. TeamOutcome carries generated and generated_lines, rebuilt from the rows on a replay.
- **Commit**: `feat(review): add the generated artefacts section to the report`
- **Done when**: pytest -q tests/unit/test_generated_artefacts_gate.py::test_report_section_fences_check_output passes
- **Unit tests**: `tests/unit/test_generated_artefacts_gate.py::test_report_section_fences_check_output`, `tests/unit/test_review_ci_mode.py::test_review_pr_json_payload_matches_documented_schema`, `tests/unit/test_taxonomy_constants_dedup.py::test_mechanical_claim_types_value_unchanged`

## Step 9 — Settle generated and older oversized findings at head

- **Files**: `src/repoach/review/head_reverify.py`, `src/repoach/review/generated_store.py`
- **Action**: settlements_at_head(db_path, pr, head) is resolved when every run of a path is verified, with 'declared generated at <sha7>, verified by <check>', still_open with the cause otherwise; settle_at_head(finding, settlements) applies to generated_artefact findings and oversized findings of a declared path. reverify_older_findings settles them first, before the cache and before the refuter, so no judge call is made for them.
- **Commit**: `feat(review): settle generated findings at head without the refuter`
- **Done when**: pytest -q tests/unit/test_generated_artefacts_gate.py::test_older_oversized_findings_settle_without_refuter passes
- **Unit tests**: `tests/unit/test_generated_artefacts_gate.py::test_older_oversized_findings_settle_without_refuter`, `tests/unit/test_generated_artefacts_gate.py::test_producer_input_counts_as_source`, `tests/unit/test_generated_artefacts_gate.py::test_two_artefacts_two_rows_in_declaration_order`, `tests/unit/test_generated_artefacts_gate.py::test_declared_but_untouched_runs_nothing`, `tests/unit/test_generated_artefacts_gate.py::test_declared_check_passes_merges`

## Step 10 — Replay Yoob !322 from its recorded shape

- **Files**: `tests/integration/test_generated_artefacts_replay_yoob_322.py`, `tests/fixtures/generated_artefacts/yoob_322/shape.json`, `tests/fixtures/generated_artefacts/yoob_322/ledger.json`
- **Action**: Record !322's diff shape (corpus and user-guide units verbatim, the embeddings unit with its two vectors lines as a placeholder expanded to 1 556 480 characters), findings 3 and 6 as the ledger held them, the recorded --check output (exit 0) and the base repoach.toml declaring the embeddings. The integration test runs review generated with the recorded result, then review pr, and expects APPROVE, findings 3 and 6 resolved_at_head and zero judge calls.
- **Commit**: `test(review): replay yoob !322 with the embeddings declared`
- **Done when**: pytest -q tests/integration/test_generated_artefacts_replay_yoob_322.py passes
- **Unit tests**: `tests/unit/test_generated_artefacts_config.py::test_unknown_key_in_an_entry_is_refused`, `tests/unit/test_generated_artefacts_config.py::test_entries_are_capped`, `tests/unit/test_generated_artefacts_config.py::test_unterminated_bracket_is_a_literal`, `tests/unit/test_generated_artefacts_config.py::test_negated_class_matches_the_complement`, `tests/unit/test_generated_artefacts_config.py::test_error_names_the_line_of_the_key_in_the_second_block`

## Step 11 — Supervise the check processes

- **Files**: `src/repoach/review/generated_process.py`
- **Action**: generated_process.py holds RunResult, the Runner seam and subprocess_runner: no shell, its own session, waited on rather than its pipes, the whole session killed when the check exits, and every descendant that detached itself adopted (PR_SET_CHILD_SUBREAPER) and killed.
- **Commit**: `refactor(review): move check process supervision to its own module`
- **Done when**: pytest -q tests/unit/test_generated_artefacts_gate.py::test_a_detached_descendant_of_the_check_is_killed passes
- **Unit tests**: `tests/unit/test_generated_artefacts_gate.py::test_no_process_of_the_check_outlives_its_run`, `tests/unit/test_generated_artefacts_gate.py::test_a_detached_descendant_of_the_check_is_killed`

## Step 12 — Keep the git reads in one module

- **Files**: `src/repoach/review/generated_git.py`
- **Action**: generated_git.py holds run_git, blob_at, merge_base, resolve_commit and changed_paths, the git reads both the review side and the verification job use.
- **Commit**: `refactor(review): move the generated git reads to their own module`
- **Done when**: pytest -q tests/unit/test_generated_artefacts_gate.py::test_head_tree_is_a_clone_with_its_own_git_dir passes
- **Unit tests**: `tests/unit/test_generated_artefacts_gate.py::test_head_tree_is_a_clone_with_its_own_git_dir`

## Step 13 — Share the row types and their causes

- **Files**: `src/repoach/review/generated_types.py`
- **Action**: generated_types.py holds GeneratedState, GeneratedCheck, GeneratedTiming, GENERATED_FINDER, by_path, path_verified, check_cause and the output sanitisers (tail_lines, last_line), so generated_store depends on the types, not on the summarising and running logic.
- **Commit**: `refactor(review): move generated rows and causes to generated_types`
- **Done when**: pytest -q tests/unit/test_generated_artefacts_gate.py::test_check_output_is_made_inert_in_the_cause passes
- **Unit tests**: `tests/unit/test_generated_artefacts_gate.py::test_check_output_is_made_inert_in_the_cause`, `tests/unit/test_generated_artefacts_gate.py::test_a_symlinked_artefact_is_never_read_and_never_green`, `tests/unit/test_generated_artefacts_gate.py::test_review_generated_requires_the_base`, `tests/unit/test_generated_artefacts_gate.py::test_subprocess_runner_refuses_a_shell`, `tests/unit/test_generated_artefacts_gate.py::test_a_timed_out_check_has_its_session_killed`

## Step 14 — Read the evidence in its own module

- **Files**: `src/repoach/review/generated_evidence.py`, `src/repoach/review/generated_settle.py`
- **Action**: generated_evidence.py holds the review-generated-json format, evidence_payload, checks_from_evidence, the review-time recomputation and load_review_evidence (another head or blob refused, a tail re-trimmed). generated_settle.py holds ReverifyOutcome, settlements_at_head and settle_at_head, so generated_store stays persistence only.
- **Commit**: `refactor(review): move evidence reading to generated_evidence`
- **Done when**: pytest -q tests/unit/test_generated_artefacts_gate.py::test_a_forged_evidence_tail_is_bounded_and_inert passes
- **Unit tests**: `tests/unit/test_generated_artefacts_gate.py::test_a_forged_evidence_tail_is_bounded_and_inert`, `tests/unit/test_generated_artefacts_gate.py::test_only_the_end_of_a_long_output_is_read`, `tests/unit/test_generated_artefacts_gate.py::test_without_a_subreaper_the_session_is_still_killed`, `tests/unit/test_generated_artefacts_gate.py::test_settle_at_head_leaves_every_other_finding_to_the_refuter`, `tests/unit/test_generated_artefacts_config.py::test_a_class_never_matches_a_slash`

## Step 15 — Document the declaration and the Yoob job

- **Files**: `docs/generated_artefacts.md`
- **Action**: Describe the [[review.generated]] keys and rules, what the review shows, runs and records, what of the evidence is trusted, the causes a finding names, the report lines, and the GitLab snippet: a secret-free review:generated job writing review-generated.json and review:gate needing it.
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
  "summary": "Declare derived files under [[review.generated]] read at the base ref; the reviewers see a summary paragraph instead of the artefact's diff; a secret-free repoach review generated job runs the declared check shell-free, scrubbed, in a throwaway clone, and writes evidence bound to the head; the review recomputes what it can derive from that evidence, records generated_checks rows and one blocking generated_artefact finding per path the check does not prove, and head_reverify settles them, and the older oversized findings of a newly declared path, from the rows at a later head without the refuter.",
  "steps": [
    {
      "index": 1,
      "title": "Declare generated artefacts in repoach.toml",
      "files": [
        "src/repoach/project/globs.py",
        "src/repoach/project/config.py",
        "tests/unit/test_generated_artefacts_config.py"
      ],
      "action": "In globs.py add glob_regex and glob_matches (** crosses directories, * and ? stay in one segment) and glob_code_extension(pattern, extensions), which tells whether a glob can match a file with one of the given extensions; globs.py imports nothing of repoach. In config.py add GeneratedSection (path, check, sources, producer_inputs, check_timeout_s): path and every source glob relative, no '..', no leading '/'; check an argv list whose check[0] is a repository script and whose later elements match ^[A-Za-z0-9._/=:@,+-]+$ once {head} and {base} are removed. ReviewSection gains generated, capped at GENERATED_ENTRIES_MAX (32). refuse_duplicate_generated refuses a duplicate path; refuse_code_globs refuses a code glob, given the extensions. Errors inside an entry are named review.generated[<i>].<key> with the line of that key in the i-th [[review.generated]] block.",
      "commit_message": "feat(config): declare generated artefacts under review.generated",
      "done_when": "pytest -q tests/unit/test_generated_artefacts_config.py passes",
      "unit_tests": [
        "tests/unit/test_generated_artefacts_config.py::test_one_entry_loads_into_generated_entry",
        "tests/unit/test_generated_artefacts_config.py::test_string_check_is_refused",
        "tests/unit/test_generated_artefacts_config.py::test_shell_shaped_check_argument_is_refused",
        "tests/unit/test_generated_artefacts_config.py::test_empty_sources_is_refused",
        "tests/unit/test_generated_artefacts_config.py::test_path_leaving_the_repository_is_refused"
      ]
    },
    {
      "index": 2,
      "title": "Document the key in the shipped template",
      "files": [
        "src/repoach/project/repoach.toml.template",
        "tests/unit/test_project_config.py"
      ],
      "action": "Under [review] in the template, describe [[review.generated]] in comments and add a commented generated = [] line, so the template round-trip test finds a line for every key.",
      "commit_message": "docs(config): describe review.generated in the template",
      "done_when": "pytest -q tests/unit/test_project_config.py passes",
      "unit_tests": [
        "tests/unit/test_project_config.py::TestTemplate::test_every_key_has_a_template_line_and_round_trips"
      ]
    },
    {
      "index": 3,
      "title": "Read declarations at the base and summarise touched artefacts",
      "files": [
        "src/repoach/review/generated_artefacts.py",
        "tests/fixtures/generated_artefacts/harness.py",
        "tests/unit/test_generated_artefacts_gate.py"
      ],
      "action": "Create generated_artefacts.py with GeneratedEntry, load_generated_entries(text, path, languages) (refusing a path glob that could match a file of the file's or the given languages, through code_extensions over gates.languages), declared_at_base(root, base_ref) reading repoach.toml with git show at the base ref, touched_artefacts in declaration order (two entries matching one changed path is a ConfigError), changed_inputs, summary_paragraph and summarise replacing each touched unit by its diff --git header and one paragraph of sizes, sha256 prefixes and paths, and review_scope. Add the test harness: a bare origin, a seed pushing refs/pull/<n>/head, a GitHubForge serving the three-dot diff, approving reviewers that record every diff, a counting judge and a recording runner.",
      "commit_message": "feat(review): summarise declared generated artefacts for reviewers",
      "done_when": "pytest -q tests/unit/test_generated_artefacts_config.py passes",
      "unit_tests": [
        "tests/unit/test_generated_artefacts_config.py::test_path_glob_matching_python_is_refused",
        "tests/unit/test_generated_artefacts_config.py::test_code_glob_is_judged_against_the_declared_languages",
        "tests/unit/test_generated_artefacts_config.py::test_two_globs_matching_one_changed_path_are_refused",
        "tests/unit/test_generated_artefacts_config.py::test_the_same_path_twice_is_refused"
      ]
    },
    {
      "index": 4,
      "title": "Feed the reviewers the summary and name the oversized remedy",
      "files": [
        "src/repoach/review/orchestrator.py",
        "src/repoach/review/findings_bridge.py"
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
      "index": 5,
      "title": "Run checks and build blocking findings",
      "files": [
        "src/repoach/review/generated_artefacts.py",
        "src/repoach/review/findings.py",
        "src/repoach/review/finding_verifiers.py"
      ],
      "action": "ClaimType gains GENERATED_ARTEFACT, a member of MECHANICAL_CLAIM_TYPES; its verifier keeps the finding verified at its own head. In generated_artefacts add GeneratedState, GeneratedCheck, run_checks over the Runner seam (scrubbed_env, check_timeout_s per run, checks_total_budget_s per PR on an injected clock, check absent at head, a symlinked check script refused, base script then head script when check[0] changed, no source changed when only the artefact moved), generated_findings (one blocking verified generated_artefact finding per unproved path, its cause made inert), the review-generated-json evidence, and load_review_evidence, which refuses another head or another artefact blob and recomputes argv, sources and state at review time.",
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
      "index": 6,
      "title": "Store the generated_checks rows",
      "files": [
        "src/repoach/review/generated_store.py",
        "src/repoach/review/orchestrator.py",
        "tests/unit/test_taxonomy_constants_dedup.py"
      ],
      "action": "generated_store.py holds the generated_checks table (PR, head, path, argv, exit code, ms, output tail, state, script ref, sources, producer inputs, artefact sha256, timing), record_generated_checks, fetch_generated_checks and settlements_at_head. record_review_ledger records the rows and the findings inside its fail-closed write; the orchestrator loads the evidence named by generated_evidence.",
      "commit_message": "feat(review): store generated checks per head",
      "done_when": "pytest -q tests/unit/test_generated_artefacts_gate.py::test_evidence_states_are_recomputed_at_review passes",
      "unit_tests": [
        "tests/unit/test_generated_artefacts_gate.py::test_evidence_states_are_recomputed_at_review",
        "tests/unit/test_generated_artefacts_gate.py::test_check_output_is_made_inert_in_the_cause",
        "tests/unit/test_generated_artefacts_gate.py::test_symlinked_check_script_is_never_run",
        "tests/unit/test_generated_artefacts_gate.py::test_a_script_turned_into_a_symlink_by_the_base_run_is_not_written_through"
      ]
    },
    {
      "index": 7,
      "title": "Add review generated, the verification job",
      "files": [
        "src/repoach/review/generated_verify.py",
        "src/repoach/cli/review_cmds.py",
        "tests/unit/test_review_ci_mode.py"
      ],
      "action": "generated_verify.py holds head_tree (a throwaway clone with its own git directory, removed on exit) and verify_generated(root, pr, head, base_ref, state_dir, runner): declarations at the base, changed paths from the merge base, run_checks, and the evidence with integer checks_ms and overhead_ms. repoach review generated --pr --head --out [--base] calls it with GENERATED_RUNNER; held_secrets and reexec_scrubbed re-run it (python -P) without any secret-bearing variable it holds. review pr gains --generated-evidence, the JSON generated array and one stderr line per run (generated: <path> verified by <check> (<ms> ms), base script / head script under D3), producer inputs changed: <list>, and generated checks: <n> ms + <m> ms overhead.",
      "commit_message": "feat(cli): run generated checks in a secret-free job and report them",
      "done_when": "pytest -q tests/unit/test_generated_artefacts_gate.py::test_report_names_the_proving_command passes",
      "unit_tests": [
        "tests/unit/test_generated_artefacts_gate.py::test_check_never_runs_in_review_job",
        "tests/unit/test_generated_artefacts_gate.py::test_report_names_the_proving_command",
        "tests/unit/test_generated_artefacts_gate.py::test_delay_is_measured_and_printed",
        "tests/unit/test_generated_artefacts_gate.py::test_changed_check_script_runs_base_and_head",
        "tests/unit/test_generated_artefacts_gate.py::test_head_tree_is_a_clone_with_its_own_git_dir"
      ]
    },
    {
      "index": 8,
      "title": "Report the generated artefacts in the Markdown report",
      "files": [
        "src/repoach/review/report.py",
        "src/repoach/review/orchestrator.py"
      ],
      "action": "render_ledger_report gains generated_lines, rendered as a Generated artefacts section inside a fence one backtick longer than any backtick run in the lines. TeamOutcome carries generated and generated_lines, rebuilt from the rows on a replay.",
      "commit_message": "feat(review): add the generated artefacts section to the report",
      "done_when": "pytest -q tests/unit/test_generated_artefacts_gate.py::test_report_section_fences_check_output passes",
      "unit_tests": [
        "tests/unit/test_generated_artefacts_gate.py::test_report_section_fences_check_output",
        "tests/unit/test_review_ci_mode.py::test_review_pr_json_payload_matches_documented_schema",
        "tests/unit/test_taxonomy_constants_dedup.py::test_mechanical_claim_types_value_unchanged"
      ]
    },
    {
      "index": 9,
      "title": "Settle generated and older oversized findings at head",
      "files": [
        "src/repoach/review/head_reverify.py",
        "src/repoach/review/generated_store.py"
      ],
      "action": "settlements_at_head(db_path, pr, head) is resolved when every run of a path is verified, with 'declared generated at <sha7>, verified by <check>', still_open with the cause otherwise; settle_at_head(finding, settlements) applies to generated_artefact findings and oversized findings of a declared path. reverify_older_findings settles them first, before the cache and before the refuter, so no judge call is made for them.",
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
      "index": 10,
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
        "tests/unit/test_generated_artefacts_config.py::test_unknown_key_in_an_entry_is_refused",
        "tests/unit/test_generated_artefacts_config.py::test_entries_are_capped",
        "tests/unit/test_generated_artefacts_config.py::test_unterminated_bracket_is_a_literal",
        "tests/unit/test_generated_artefacts_config.py::test_negated_class_matches_the_complement",
        "tests/unit/test_generated_artefacts_config.py::test_error_names_the_line_of_the_key_in_the_second_block"
      ]
    },
    {
      "index": 11,
      "title": "Supervise the check processes",
      "files": [
        "src/repoach/review/generated_process.py"
      ],
      "action": "generated_process.py holds RunResult, the Runner seam and subprocess_runner: no shell, its own session, waited on rather than its pipes, the whole session killed when the check exits, and every descendant that detached itself adopted (PR_SET_CHILD_SUBREAPER) and killed.",
      "commit_message": "refactor(review): move check process supervision to its own module",
      "done_when": "pytest -q tests/unit/test_generated_artefacts_gate.py::test_a_detached_descendant_of_the_check_is_killed passes",
      "unit_tests": [
        "tests/unit/test_generated_artefacts_gate.py::test_no_process_of_the_check_outlives_its_run",
        "tests/unit/test_generated_artefacts_gate.py::test_a_detached_descendant_of_the_check_is_killed"
      ]
    },
    {
      "index": 12,
      "title": "Keep the git reads in one module",
      "files": [
        "src/repoach/review/generated_git.py"
      ],
      "action": "generated_git.py holds run_git, blob_at, merge_base, resolve_commit and changed_paths, the git reads both the review side and the verification job use.",
      "commit_message": "refactor(review): move the generated git reads to their own module",
      "done_when": "pytest -q tests/unit/test_generated_artefacts_gate.py::test_head_tree_is_a_clone_with_its_own_git_dir passes",
      "unit_tests": [
        "tests/unit/test_generated_artefacts_gate.py::test_head_tree_is_a_clone_with_its_own_git_dir"
      ]
    },
    {
      "index": 13,
      "title": "Share the row types and their causes",
      "files": [
        "src/repoach/review/generated_types.py"
      ],
      "action": "generated_types.py holds GeneratedState, GeneratedCheck, GeneratedTiming, GENERATED_FINDER, by_path, path_verified, check_cause and the output sanitisers (tail_lines, last_line), so generated_store depends on the types, not on the summarising and running logic.",
      "commit_message": "refactor(review): move generated rows and causes to generated_types",
      "done_when": "pytest -q tests/unit/test_generated_artefacts_gate.py::test_check_output_is_made_inert_in_the_cause passes",
      "unit_tests": [
        "tests/unit/test_generated_artefacts_gate.py::test_check_output_is_made_inert_in_the_cause",
        "tests/unit/test_generated_artefacts_gate.py::test_a_symlinked_artefact_is_never_read_and_never_green",
        "tests/unit/test_generated_artefacts_gate.py::test_review_generated_requires_the_base",
        "tests/unit/test_generated_artefacts_gate.py::test_subprocess_runner_refuses_a_shell",
        "tests/unit/test_generated_artefacts_gate.py::test_a_timed_out_check_has_its_session_killed"
      ]
    },
    {
      "index": 14,
      "title": "Read the evidence in its own module",
      "files": [
        "src/repoach/review/generated_evidence.py",
        "src/repoach/review/generated_settle.py"
      ],
      "action": "generated_evidence.py holds the review-generated-json format, evidence_payload, checks_from_evidence, the review-time recomputation and load_review_evidence (another head or blob refused, a tail re-trimmed). generated_settle.py holds ReverifyOutcome, settlements_at_head and settle_at_head, so generated_store stays persistence only.",
      "commit_message": "refactor(review): move evidence reading to generated_evidence",
      "done_when": "pytest -q tests/unit/test_generated_artefacts_gate.py::test_a_forged_evidence_tail_is_bounded_and_inert passes",
      "unit_tests": [
        "tests/unit/test_generated_artefacts_gate.py::test_a_forged_evidence_tail_is_bounded_and_inert",
        "tests/unit/test_generated_artefacts_gate.py::test_only_the_end_of_a_long_output_is_read",
        "tests/unit/test_generated_artefacts_gate.py::test_without_a_subreaper_the_session_is_still_killed",
        "tests/unit/test_generated_artefacts_gate.py::test_settle_at_head_leaves_every_other_finding_to_the_refuter",
        "tests/unit/test_generated_artefacts_config.py::test_a_class_never_matches_a_slash"
      ]
    },
    {
      "index": 15,
      "title": "Document the declaration and the Yoob job",
      "files": [
        "docs/generated_artefacts.md"
      ],
      "action": "Describe the [[review.generated]] keys and rules, what the review shows, runs and records, what of the evidence is trusted, the causes a finding names, the report lines, and the GitLab snippet: a secret-free review:generated job writing review-generated.json and review:gate needing it.",
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
