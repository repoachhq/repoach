# SP-PLAN-QUALITY — Plan-form convergence: rule catalog in the loop, size caps, no-stub lint

Give the Planner the whole rulebook instead of one error at a time: a rule catalog rendered from the validators themselves (step 1), the size caps and the operator's no-stub rule as a STRICT PRODUCTION-TIME layer (step 2) enforced in the Planner's emission loop (step 3) — never retroactively in load_plan: an empirical check showed model-level validators would newly break 13 of 31 committed plans, including plans of queued specs, plus existing test fixtures, and would make this very plan reject itself. Deliberate deviation from the spec's 'validation errors' wording, to be synced into the spec after merge. Telemetry per attempt (step 4), insights + one CLI key line (step 5 — the insights CLI hand-builds its JSON dict, so 'no CLI change' was false), end-to-end proof (step 6).

## Step 1 — Rule registry + rendered catalog in plan.py

- **Files**: `src/ferova/review/plan.py`, `tests/unit/test_plan_form_rules.py`
- **Action**: In src/ferova/review/plan.py add a module-level `_FORM_RULES: dict[str, str]` mapping each validator function name on PlanStep and ActionPlan to a one-line rule sentence, and `render_plan_form_rules() -> str` returning the numbered catalog (stable order, no duplicates), including the strict-layer rule sentences from `_STRICT_FORM_RULES` (introduced in step 2; render tolerates it being empty until then). The validators to cover, by exact registry name: PlanStep field validators _non_empty_text, _files_repo_relative, _selectors_safe, _require_node_ids and model validator _code_steps_promise_unit_tests; ActionPlan field validators _selectors_safe, _spec_id_shape, _non_empty_text, _contiguous_indexes and model validators _src_plans_promise_integration_tests, _promised_tests_are_created_by_the_plan, _integration_tests_under_integration_tree, _integration_promises_are_created_by_the_plan. Note: _non_empty_text and _selectors_safe are distinct functions on both models sharing a registry name — the flat name-keyed dict has one generically-phrased sentence per shared name (11 keys for 13 functions). Create tests/unit/test_plan_form_rules.py with tests/unit/test_plan_form_rules.py::test_rule_catalog_covers_every_validator — introspect `PlanStep.__pydantic_decorators__.field_validators` / `.model_validators` and the same on ActionPlan (confirmed working on pydantic 2.13.4; registry keys equal function names) and assert every name is a key of _FORM_RULES so adding a validator without a sentence fails — and tests/unit/test_plan_form_rules.py::test_catalog_renders_numbered_sentences (rendered text contains every sentence, numbered, no duplicates).
- **Commit**: `feat(review): plan-form rule registry and rendered catalog`
- **Done when**: pytest tests/unit/test_plan_form_rules.py::test_rule_catalog_covers_every_validator tests/unit/test_plan_form_rules.py::test_catalog_renders_numbered_sentences passes
- **Unit tests**: `tests/unit/test_plan_form_rules.py::test_rule_catalog_covers_every_validator`, `tests/unit/test_plan_form_rules.py::test_catalog_renders_numbered_sentences`

## Step 2 — Strict production-time layer: size caps + banned test-double keywords

- **Files**: `src/ferova/review/plan.py`, `tests/unit/test_plan_form_rules.py`
- **Action**: In src/ferova/review/plan.py add module constants PLAN_STEP_MAX_FILES: int = 3 and PLAN_STEP_MAX_UNIT_SELECTORS: int = 5, a `_STRICT_FORM_RULES: dict[str, str]` registry, and a pure function `validate_plan_form_strict(plan: ActionPlan) -> list[str]` returning one reason per violation (empty list == clean). Checks: (a) step size — any step with len(files) > PLAN_STEP_MAX_FILES or len(unit_tests) > PLAN_STEP_MAX_UNIT_SELECTORS yields a reason citing the cap and the Developer's 30-turn budget; (b) the operator's no-test-double rule — scan each step's action text with a case-insensitive WORD-BOUNDARY regex for the four banned keywords enumerated in the spec's G3 (the two verbs for replacing our own behavior with canned doubles, their participle forms, and the pytest fixture-patching verb; build the regex from a module-level frozenset so the catalog sentence and the scan share one source); any whole-word match yields a reason quoting the operator rule and pointing to the truthful-boundary-fake vocabulary. This layer is deliberately NOT a pydantic validator: load_plan stays permissive (13/31 committed plans, including queued specs' plans, would otherwise break retroactively — empirically verified), enforcement happens where NEW plans are born (step 3). Register both rule sentences in _STRICT_FORM_RULES so the step-1 catalog carries them. Add tests/unit/test_plan_form_rules.py::test_step_size_cap_rejects_oversized_step (a 4-file step and a 6-selector step each produce a cap-citing reason), tests/unit/test_plan_form_rules.py::test_form_lint_rejects_banned_double_keywords (an action text naming the fixture-patching verb against resolve_verified_head produces a reason quoting the operator rule; word-boundary proof: an identifier merely containing a banned keyword as a substring, and prose like 'stubborn', produce NO reason) and tests/unit/test_plan_form_rules.py::test_form_lint_allows_truthful_boundary_fakes (an action describing 'a truthful gh boundary fake whose pr_head_sha is scripted by the test' is clean).
- **Commit**: `feat(review): strict plan-form layer — size caps and banned-double lint`
- **Done when**: pytest tests/unit/test_plan_form_rules.py::test_step_size_cap_rejects_oversized_step tests/unit/test_plan_form_rules.py::test_form_lint_rejects_banned_double_keywords tests/unit/test_plan_form_rules.py::test_form_lint_allows_truthful_boundary_fakes passes
- **Unit tests**: `tests/unit/test_plan_form_rules.py::test_step_size_cap_rejects_oversized_step`, `tests/unit/test_plan_form_rules.py::test_form_lint_rejects_banned_double_keywords`, `tests/unit/test_plan_form_rules.py::test_form_lint_allows_truthful_boundary_fakes`

## Step 3 — Catalog into both planner backends; strict layer gates emission

- **Files**: `src/ferova/review/planner.py`, `tests/unit/test_planner_prompt_rules.py`
- **Action**: In src/ferova/review/planner.py: (1) inject render_plan_form_rules() under the fixed heading 'Plan-form rules (all of them — every attempt is validated against every rule)' at the backend-neutral point — run_planner_session already augments spec_markdown (planner.py:565-566, spec.raw_markdown + lessons_section) and that text flows through the shared _spec_block (planner.py:357) into BOTH _plan_via_proxy (initial prompt, lines 379-387) and _plan_via_cc (lines 459-460); injecting alongside the lessons section covers both backends with one change. (2) Add the catalog to _refine_prompt (planner.py:132), keeping the existing error history exactly as is. (3) Enforcement at emission: in the parse/refine loop, after a payload passes ActionPlan validation, run validate_plan_form_strict; a non-empty reason list is treated exactly like a plan_invalid validation failure (same refine path, reasons fed back verbatim). prompts/review/* untouched. Create tests/unit/test_planner_prompt_rules.py with tests/unit/test_planner_prompt_rules.py::test_initial_prompt_carries_full_catalog and tests/unit/test_planner_prompt_rules.py::test_refine_prompt_carries_catalog_and_history (drive the REAL prompt-assembly paths — a recording fake loop for the initial prompt per the _ScriptedLoop pattern in tests/integration/test_planner_refine_history.py:62-128 — and assert the fixed heading plus at least three rule sentences appear; the refine case also keeps the prior error lines), tests/unit/test_planner_prompt_rules.py::test_strict_rules_gate_planner_emission (a scripted payload violating the size cap is refused and refined; the second scripted payload, clean, is written) and tests/unit/test_planner_prompt_rules.py::test_attempt_budget_setting (_parse_attempts at planner.py:44 reads FEROVA_PLANNER_PARSE_ATTEMPTS; parseable values below 1 clamp to 1; unset or non-integer falls back to 5).
- **Commit**: `feat(review): rule catalog in both planner backends, strict gate at emission`
- **Done when**: pytest tests/unit/test_planner_prompt_rules.py::test_initial_prompt_carries_full_catalog tests/unit/test_planner_prompt_rules.py::test_refine_prompt_carries_catalog_and_history tests/unit/test_planner_prompt_rules.py::test_strict_rules_gate_planner_emission tests/unit/test_planner_prompt_rules.py::test_attempt_budget_setting passes
- **Unit tests**: `tests/unit/test_planner_prompt_rules.py::test_initial_prompt_carries_full_catalog`, `tests/unit/test_planner_prompt_rules.py::test_refine_prompt_carries_catalog_and_history`, `tests/unit/test_planner_prompt_rules.py::test_strict_rules_gate_planner_emission`, `tests/unit/test_planner_prompt_rules.py::test_attempt_budget_setting`

## Step 4 — Per-attempt telemetry persistence

- **Files**: `src/ferova/review/planner_telemetry.py`, `src/ferova/review/planner.py`, `tests/unit/test_planner_telemetry.py`
- **Action**: Create src/ferova/review/planner_telemetry.py following the imperative SQLAlchemy Core scaffold of findings.py/persistence.py exactly: module-level MetaData() + Table planner_attempts (id, spec_id, attempt, violated_rule, recorded_at), _engine_for(db_path) that mkdirs the parent, init_planner_telemetry_schema(db_path) doing metadata.create_all(engine, checkfirst=True) plus an (initially empty) _migrate_missing_columns-style scaffold. All functions take db_path: Path as explicit first parameter — the module never calls get_settings() itself (the persistence-module convention). record_planner_attempt(db_path, *, spec_id, attempt, violated_rule) -> bool wraps its whole engine/insert path in try/except that LOGS a warning (planner_telemetry.record_failed — the no-silent-except gate requires the log) and returns False instead of raising; fetch_planner_attempts(db_path, spec_id=None) returns rows ordered by id. In planner.py, inside the existing refine loop, record one row per rejected attempt (first 300 chars of the validation or strict-layer reasons as violated_rule), resolving the db path at the call site via Path(get_settings().db_path) — the coder_findings.py:453 idiom; planner.py gains that import. Add tests/unit/test_planner_telemetry.py::test_attempt_rows_persisted (two rejected attempts recorded with correct spec_id and attempt numbers; fetch returns them ordered) and tests/unit/test_planner_telemetry.py::test_telemetry_failure_never_breaks_planning (db path inside a nonexistent, uncreatable location — e.g. under a FILE used as a directory: record_planner_attempt returns False, no exception escapes, warning logged).
- **Commit**: `feat(review): planner per-attempt telemetry`
- **Done when**: pytest tests/unit/test_planner_telemetry.py::test_attempt_rows_persisted tests/unit/test_planner_telemetry.py::test_telemetry_failure_never_breaks_planning passes
- **Unit tests**: `tests/unit/test_planner_telemetry.py::test_attempt_rows_persisted`, `tests/unit/test_planner_telemetry.py::test_telemetry_failure_never_breaks_planning`

## Step 5 — Insights section, including the CLI key line

- **Files**: `src/ferova/review/review_lessons.py`, `src/ferova/cli/review_cmds.py`, `tests/unit/test_planner_telemetry.py`
- **Action**: In src/ferova/review/review_lessons.py add planner_rule_violations: dict[str, int] to the frozen FindingsInsights dataclass (review_lessons.py:63-77, default empty) and populate it in gather_insights (review_lessons.py:258) via fetch_planner_attempts aggregation (violated_rule -> count across ALL sessions; the planner section deliberately ignores the pr_number scoping parameter — planner attempts are keyed by spec_id, not PR). In src/ferova/cli/review_cmds.py the insights command hand-builds its json.dumps dict (review_cmds.py:286-301 — nothing renders new dataclass fields automatically, which is why this step exists): add exactly one key line `"planner_rule_violations": insights.planner_rule_violations` after the "by_claim_type" entry. Add tests/unit/test_planner_telemetry.py::test_insights_reports_rule_violations — seed three attempts across two spec_ids, assert gather_insights aggregates the counts AND that the rendered CLI dict (drive the review_insights command body or its dict-building path directly) carries the planner_rule_violations key.
- **Commit**: `feat(review): planner rule-violation section in insights`
- **Done when**: pytest tests/unit/test_planner_telemetry.py::test_insights_reports_rule_violations passes
- **Unit tests**: `tests/unit/test_planner_telemetry.py::test_insights_reports_rule_violations`

## Step 6 — End-to-end convergence proof

- **Files**: `tests/integration/test_planner_form_convergence.py`
- **Action**: Create tests/integration/test_planner_form_convergence.py with test_catalog_present_in_first_planner_request, mirroring tests/integration/test_planner_refine_history.py (the _ScriptedLoop at :62 recording every prompt at :86, Planner(loop=loop, repo_root=repo) at :125, run_planner_session(spec_id, root=repo, planner=planner) at :128): write a throwaway governed spec into a tmp_path repo, drive one session whose scripted loop immediately answers a valid, strict-clean plan payload, then assert the FIRST recorded prompt already contains the fixed catalog heading and at least three rule sentences — the whole point: the rules arrive BEFORE any failure — and that the session writes the plan (written == True). Hermetic: no network, no LLM, no reliance on a .env file; the real prompt assembly, validation, strict layer and telemetry paths all run.
- **Commit**: `test(review): end-to-end proof the rule catalog precedes any failure`
- **Done when**: pytest tests/integration/test_planner_form_convergence.py::test_catalog_present_in_first_planner_request passes
- **Unit tests**: `tests/integration/test_planner_form_convergence.py::test_catalog_present_in_first_planner_request`

## Step 7 — Banned keyword set matches the spec exactly

- **Files**: `src/ferova/review/plan.py`, `tests/unit/test_plan_form_rules.py`
- **Action**: In src/ferova/review/plan.py set the banned test-double keyword frozenset to EXACTLY the seven whole words enumerated in the spec's G3 (the noun for a canned double, its two participle forms, the pytest fixture-patching verb, and the three forms of the other test-double noun) — no more, no fewer — keeping the word-boundary regex built from that single frozenset. Update the strict-rule sentence in _STRICT_FORM_RULES if it names the words. Add tests/unit/test_plan_form_rules.py::test_banned_keyword_set_matches_spec: assert the frozenset equals the spec's seven words verbatim, that each word as a whole word in an action text produces a reason, and that substring occurrences inside identifiers or words like 'stubborn' and 'mockingbird' produce none.
- **Commit**: `fix(review): banned test-double keyword set matches spec G3`
- **Done when**: pytest tests/unit/test_plan_form_rules.py::test_banned_keyword_set_matches_spec passes
- **Unit tests**: `tests/unit/test_plan_form_rules.py::test_banned_keyword_set_matches_spec`

## Integration tests

- `tests/integration/test_planner_form_convergence.py::test_catalog_present_in_first_planner_request`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-PLAN-QUALITY",
  "title": "Plan-form convergence: rule catalog in the loop, size caps, no-stub lint",
  "summary": "Give the Planner the whole rulebook instead of one error at a time: a rule catalog rendered from the validators themselves (step 1), the size caps and the operator's no-stub rule as a STRICT PRODUCTION-TIME layer (step 2) enforced in the Planner's emission loop (step 3) — never retroactively in load_plan: an empirical check showed model-level validators would newly break 13 of 31 committed plans, including plans of queued specs, plus existing test fixtures, and would make this very plan reject itself. Deliberate deviation from the spec's 'validation errors' wording, to be synced into the spec after merge. Telemetry per attempt (step 4), insights + one CLI key line (step 5 — the insights CLI hand-builds its JSON dict, so 'no CLI change' was false), end-to-end proof (step 6).",
  "steps": [
    {
      "index": 1,
      "title": "Rule registry + rendered catalog in plan.py",
      "files": [
        "src/ferova/review/plan.py",
        "tests/unit/test_plan_form_rules.py"
      ],
      "action": "In src/ferova/review/plan.py add a module-level `_FORM_RULES: dict[str, str]` mapping each validator function name on PlanStep and ActionPlan to a one-line rule sentence, and `render_plan_form_rules() -> str` returning the numbered catalog (stable order, no duplicates), including the strict-layer rule sentences from `_STRICT_FORM_RULES` (introduced in step 2; render tolerates it being empty until then). The validators to cover, by exact registry name: PlanStep field validators _non_empty_text, _files_repo_relative, _selectors_safe, _require_node_ids and model validator _code_steps_promise_unit_tests; ActionPlan field validators _selectors_safe, _spec_id_shape, _non_empty_text, _contiguous_indexes and model validators _src_plans_promise_integration_tests, _promised_tests_are_created_by_the_plan, _integration_tests_under_integration_tree, _integration_promises_are_created_by_the_plan. Note: _non_empty_text and _selectors_safe are distinct functions on both models sharing a registry name — the flat name-keyed dict has one generically-phrased sentence per shared name (11 keys for 13 functions). Create tests/unit/test_plan_form_rules.py with tests/unit/test_plan_form_rules.py::test_rule_catalog_covers_every_validator — introspect `PlanStep.__pydantic_decorators__.field_validators` / `.model_validators` and the same on ActionPlan (confirmed working on pydantic 2.13.4; registry keys equal function names) and assert every name is a key of _FORM_RULES so adding a validator without a sentence fails — and tests/unit/test_plan_form_rules.py::test_catalog_renders_numbered_sentences (rendered text contains every sentence, numbered, no duplicates).",
      "commit_message": "feat(review): plan-form rule registry and rendered catalog",
      "done_when": "pytest tests/unit/test_plan_form_rules.py::test_rule_catalog_covers_every_validator tests/unit/test_plan_form_rules.py::test_catalog_renders_numbered_sentences passes",
      "unit_tests": [
        "tests/unit/test_plan_form_rules.py::test_rule_catalog_covers_every_validator",
        "tests/unit/test_plan_form_rules.py::test_catalog_renders_numbered_sentences"
      ]
    },
    {
      "index": 2,
      "title": "Strict production-time layer: size caps + banned test-double keywords",
      "files": [
        "src/ferova/review/plan.py",
        "tests/unit/test_plan_form_rules.py"
      ],
      "action": "In src/ferova/review/plan.py add module constants PLAN_STEP_MAX_FILES: int = 3 and PLAN_STEP_MAX_UNIT_SELECTORS: int = 5, a `_STRICT_FORM_RULES: dict[str, str]` registry, and a pure function `validate_plan_form_strict(plan: ActionPlan) -> list[str]` returning one reason per violation (empty list == clean). Checks: (a) step size — any step with len(files) > PLAN_STEP_MAX_FILES or len(unit_tests) > PLAN_STEP_MAX_UNIT_SELECTORS yields a reason citing the cap and the Developer's 30-turn budget; (b) the operator's no-test-double rule — scan each step's action text with a case-insensitive WORD-BOUNDARY regex for the four banned keywords enumerated in the spec's G3 (the two verbs for replacing our own behavior with canned doubles, their participle forms, and the pytest fixture-patching verb; build the regex from a module-level frozenset so the catalog sentence and the scan share one source); any whole-word match yields a reason quoting the operator rule and pointing to the truthful-boundary-fake vocabulary. This layer is deliberately NOT a pydantic validator: load_plan stays permissive (13/31 committed plans, including queued specs' plans, would otherwise break retroactively — empirically verified), enforcement happens where NEW plans are born (step 3). Register both rule sentences in _STRICT_FORM_RULES so the step-1 catalog carries them. Add tests/unit/test_plan_form_rules.py::test_step_size_cap_rejects_oversized_step (a 4-file step and a 6-selector step each produce a cap-citing reason), tests/unit/test_plan_form_rules.py::test_form_lint_rejects_banned_double_keywords (an action text naming the fixture-patching verb against resolve_verified_head produces a reason quoting the operator rule; word-boundary proof: an identifier merely containing a banned keyword as a substring, and prose like 'stubborn', produce NO reason) and tests/unit/test_plan_form_rules.py::test_form_lint_allows_truthful_boundary_fakes (an action describing 'a truthful gh boundary fake whose pr_head_sha is scripted by the test' is clean).",
      "commit_message": "feat(review): strict plan-form layer — size caps and banned-double lint",
      "done_when": "pytest tests/unit/test_plan_form_rules.py::test_step_size_cap_rejects_oversized_step tests/unit/test_plan_form_rules.py::test_form_lint_rejects_banned_double_keywords tests/unit/test_plan_form_rules.py::test_form_lint_allows_truthful_boundary_fakes passes",
      "unit_tests": [
        "tests/unit/test_plan_form_rules.py::test_step_size_cap_rejects_oversized_step",
        "tests/unit/test_plan_form_rules.py::test_form_lint_rejects_banned_double_keywords",
        "tests/unit/test_plan_form_rules.py::test_form_lint_allows_truthful_boundary_fakes"
      ]
    },
    {
      "index": 3,
      "title": "Catalog into both planner backends; strict layer gates emission",
      "files": [
        "src/ferova/review/planner.py",
        "tests/unit/test_planner_prompt_rules.py"
      ],
      "action": "In src/ferova/review/planner.py: (1) inject render_plan_form_rules() under the fixed heading 'Plan-form rules (all of them — every attempt is validated against every rule)' at the backend-neutral point — run_planner_session already augments spec_markdown (planner.py:565-566, spec.raw_markdown + lessons_section) and that text flows through the shared _spec_block (planner.py:357) into BOTH _plan_via_proxy (initial prompt, lines 379-387) and _plan_via_cc (lines 459-460); injecting alongside the lessons section covers both backends with one change. (2) Add the catalog to _refine_prompt (planner.py:132), keeping the existing error history exactly as is. (3) Enforcement at emission: in the parse/refine loop, after a payload passes ActionPlan validation, run validate_plan_form_strict; a non-empty reason list is treated exactly like a plan_invalid validation failure (same refine path, reasons fed back verbatim). prompts/review/* untouched. Create tests/unit/test_planner_prompt_rules.py with tests/unit/test_planner_prompt_rules.py::test_initial_prompt_carries_full_catalog and tests/unit/test_planner_prompt_rules.py::test_refine_prompt_carries_catalog_and_history (drive the REAL prompt-assembly paths — a recording fake loop for the initial prompt per the _ScriptedLoop pattern in tests/integration/test_planner_refine_history.py:62-128 — and assert the fixed heading plus at least three rule sentences appear; the refine case also keeps the prior error lines), tests/unit/test_planner_prompt_rules.py::test_strict_rules_gate_planner_emission (a scripted payload violating the size cap is refused and refined; the second scripted payload, clean, is written) and tests/unit/test_planner_prompt_rules.py::test_attempt_budget_setting (_parse_attempts at planner.py:44 reads FEROVA_PLANNER_PARSE_ATTEMPTS; parseable values below 1 clamp to 1; unset or non-integer falls back to 5).",
      "commit_message": "feat(review): rule catalog in both planner backends, strict gate at emission",
      "done_when": "pytest tests/unit/test_planner_prompt_rules.py::test_initial_prompt_carries_full_catalog tests/unit/test_planner_prompt_rules.py::test_refine_prompt_carries_catalog_and_history tests/unit/test_planner_prompt_rules.py::test_strict_rules_gate_planner_emission tests/unit/test_planner_prompt_rules.py::test_attempt_budget_setting passes",
      "unit_tests": [
        "tests/unit/test_planner_prompt_rules.py::test_initial_prompt_carries_full_catalog",
        "tests/unit/test_planner_prompt_rules.py::test_refine_prompt_carries_catalog_and_history",
        "tests/unit/test_planner_prompt_rules.py::test_strict_rules_gate_planner_emission",
        "tests/unit/test_planner_prompt_rules.py::test_attempt_budget_setting"
      ]
    },
    {
      "index": 4,
      "title": "Per-attempt telemetry persistence",
      "files": [
        "src/ferova/review/planner_telemetry.py",
        "src/ferova/review/planner.py",
        "tests/unit/test_planner_telemetry.py"
      ],
      "action": "Create src/ferova/review/planner_telemetry.py following the imperative SQLAlchemy Core scaffold of findings.py/persistence.py exactly: module-level MetaData() + Table planner_attempts (id, spec_id, attempt, violated_rule, recorded_at), _engine_for(db_path) that mkdirs the parent, init_planner_telemetry_schema(db_path) doing metadata.create_all(engine, checkfirst=True) plus an (initially empty) _migrate_missing_columns-style scaffold. All functions take db_path: Path as explicit first parameter — the module never calls get_settings() itself (the persistence-module convention). record_planner_attempt(db_path, *, spec_id, attempt, violated_rule) -> bool wraps its whole engine/insert path in try/except that LOGS a warning (planner_telemetry.record_failed — the no-silent-except gate requires the log) and returns False instead of raising; fetch_planner_attempts(db_path, spec_id=None) returns rows ordered by id. In planner.py, inside the existing refine loop, record one row per rejected attempt (first 300 chars of the validation or strict-layer reasons as violated_rule), resolving the db path at the call site via Path(get_settings().db_path) — the coder_findings.py:453 idiom; planner.py gains that import. Add tests/unit/test_planner_telemetry.py::test_attempt_rows_persisted (two rejected attempts recorded with correct spec_id and attempt numbers; fetch returns them ordered) and tests/unit/test_planner_telemetry.py::test_telemetry_failure_never_breaks_planning (db path inside a nonexistent, uncreatable location — e.g. under a FILE used as a directory: record_planner_attempt returns False, no exception escapes, warning logged).",
      "commit_message": "feat(review): planner per-attempt telemetry",
      "done_when": "pytest tests/unit/test_planner_telemetry.py::test_attempt_rows_persisted tests/unit/test_planner_telemetry.py::test_telemetry_failure_never_breaks_planning passes",
      "unit_tests": [
        "tests/unit/test_planner_telemetry.py::test_attempt_rows_persisted",
        "tests/unit/test_planner_telemetry.py::test_telemetry_failure_never_breaks_planning"
      ]
    },
    {
      "index": 5,
      "title": "Insights section, including the CLI key line",
      "files": [
        "src/ferova/review/review_lessons.py",
        "src/ferova/cli/review_cmds.py",
        "tests/unit/test_planner_telemetry.py"
      ],
      "action": "In src/ferova/review/review_lessons.py add planner_rule_violations: dict[str, int] to the frozen FindingsInsights dataclass (review_lessons.py:63-77, default empty) and populate it in gather_insights (review_lessons.py:258) via fetch_planner_attempts aggregation (violated_rule -> count across ALL sessions; the planner section deliberately ignores the pr_number scoping parameter — planner attempts are keyed by spec_id, not PR). In src/ferova/cli/review_cmds.py the insights command hand-builds its json.dumps dict (review_cmds.py:286-301 — nothing renders new dataclass fields automatically, which is why this step exists): add exactly one key line \"planner_rule_violations\": insights.planner_rule_violations after the \"by_claim_type\" entry. Add tests/unit/test_planner_telemetry.py::test_insights_reports_rule_violations — seed three attempts across two spec_ids, assert gather_insights aggregates the counts AND that the rendered CLI dict (drive the review_insights command body or its dict-building path directly) carries the planner_rule_violations key.",
      "commit_message": "feat(review): planner rule-violation section in insights",
      "done_when": "pytest tests/unit/test_planner_telemetry.py::test_insights_reports_rule_violations passes",
      "unit_tests": [
        "tests/unit/test_planner_telemetry.py::test_insights_reports_rule_violations"
      ]
    },
    {
      "index": 6,
      "title": "End-to-end convergence proof",
      "files": [
        "tests/integration/test_planner_form_convergence.py"
      ],
      "action": "Create tests/integration/test_planner_form_convergence.py with test_catalog_present_in_first_planner_request, mirroring tests/integration/test_planner_refine_history.py (the _ScriptedLoop at :62 recording every prompt at :86, Planner(loop=loop, repo_root=repo) at :125, run_planner_session(spec_id, root=repo, planner=planner) at :128): write a throwaway governed spec into a tmp_path repo, drive one session whose scripted loop immediately answers a valid, strict-clean plan payload, then assert the FIRST recorded prompt already contains the fixed catalog heading and at least three rule sentences — the whole point: the rules arrive BEFORE any failure — and that the session writes the plan (written == True). Hermetic: no network, no LLM, no reliance on a .env file; the real prompt assembly, validation, strict layer and telemetry paths all run.",
      "commit_message": "test(review): end-to-end proof the rule catalog precedes any failure",
      "done_when": "pytest tests/integration/test_planner_form_convergence.py::test_catalog_present_in_first_planner_request passes",
      "unit_tests": [
        "tests/integration/test_planner_form_convergence.py::test_catalog_present_in_first_planner_request"
      ]
    }
    ,{
      "index": 7,
      "title": "Banned keyword set matches the spec exactly",
      "files": [
        "src/ferova/review/plan.py",
        "tests/unit/test_plan_form_rules.py"
      ],
      "action": "In src/ferova/review/plan.py set the banned test-double keyword frozenset to EXACTLY the seven whole words enumerated in the spec's G3 (the noun for a canned double, its two participle forms, the pytest fixture-patching verb, and the three forms of the other test-double noun) — no more, no fewer — keeping the word-boundary regex built from that single frozenset. Update the strict-rule sentence in _STRICT_FORM_RULES if it names the words. Add tests/unit/test_plan_form_rules.py::test_banned_keyword_set_matches_spec: assert the frozenset equals the spec's seven words verbatim, that each word as a whole word in an action text produces a reason, and that substring occurrences inside identifiers or words like 'stubborn' and 'mockingbird' produce none.",
      "commit_message": "fix(review): banned test-double keyword set matches spec G3",
      "done_when": "pytest tests/unit/test_plan_form_rules.py::test_banned_keyword_set_matches_spec passes",
      "unit_tests": [
        "tests/unit/test_plan_form_rules.py::test_banned_keyword_set_matches_spec"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_planner_form_convergence.py::test_catalog_present_in_first_planner_request"
  ]
}
```
