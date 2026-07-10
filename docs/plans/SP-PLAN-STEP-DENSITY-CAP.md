# SP-PLAN-STEP-DENSITY-CAP — Cap plan-step action density at Planner emission

Add PLAN_STEP_MAX_ACTION_DENSITY = 2600 and a fourth per-step check in validate_plan_form_strict that refuses any step whose action chars / file count exceeds it (step 1), then register the rule in the catalog and prove the cap gates emission end-to-end with a truthful-fake integration test (step 2). Hand-authored: the Planner exhausted its attempts because the spec's ACs are unit-only while the src-touching form rule demands an integration test — the plan supplies one (a deliberate superset of the spec's ACs; sync the spec AC list post-merge). Every step here stays under the density cap it introduces (self-reference discipline; the cap is not merged mid-run so it does not self-gate mechanically).

## Step 1 — Density constant + strict-layer check + unit tests

- **Files**: `src/ferova/review/plan.py`, `tests/unit/test_plan_form_rules.py`
- **Action**: In src/ferova/review/plan.py add module constant PLAN_STEP_MAX_ACTION_DENSITY: int = 2600 beside PLAN_STEP_MAX_FILES/PLAN_STEP_MAX_UNIT_SELECTORS (~plan.py:73-74). Inside validate_plan_form_strict's `for step in plan.steps:` loop (~plan.py:134-160), append a fourth check: density = len(step.action) / max(1, len(step.files)); when density > PLAN_STEP_MAX_ACTION_DENSITY, append a reason citing round(density), PLAN_STEP_MAX_ACTION_DENSITY and the literal "30-turn" budget rationale, mirroring the existing file-cap reason text at plan.py:135-141. Register one new sentence in _STRICT_FORM_RULES (~plan.py:60-71) keyed "_action_density_cap" describing the chars-per-file cap (distinct sentence so the catalog render stays unique). Re-export PLAN_STEP_MAX_ACTION_DENSITY in the test import block (~tests/unit/test_plan_form_rules.py:15-25). Add tests/unit/test_plan_form_rules.py::test_action_density_cap_rejects_dense_step: build a step (via the _step helper) with 2 files and an action string longer than 2 * PLAN_STEP_MAX_ACTION_DENSITY chars, wrap via _plan, call validate_plan_form_strict, assert some reason contains str(PLAN_STEP_MAX_ACTION_DENSITY) and "30-turn"; then the SAME long action spread across enough files that density drops below the cap yields NO reason (proves density, not raw length). Add tests/unit/test_plan_form_rules.py::test_action_density_cap_is_emission_only: a plan whose step exceeds the density cap still round-trips through load_plan / parse_plan_markdown without raising (grandfathering — the check is not a pydantic validator), and validate_plan_form_strict is the only thing that flags it.
- **Commit**: `feat(review): cap plan-step action density at emission`
- **Done when**: pytest tests/unit/test_plan_form_rules.py::test_action_density_cap_rejects_dense_step tests/unit/test_plan_form_rules.py::test_action_density_cap_is_emission_only passes
- **Unit tests**: `tests/unit/test_plan_form_rules.py::test_action_density_cap_rejects_dense_step`, `tests/unit/test_plan_form_rules.py::test_action_density_cap_is_emission_only`

## Step 2 — Catalog registration test + emission-gate integration test

- **Files**: `tests/unit/test_plan_form_rules.py`, `tests/integration/test_planner_density_gate.py`
- **Action**: Add tests/unit/test_plan_form_rules.py::test_action_density_rule_in_catalog asserting the density rule sentence is a value in _STRICT_FORM_RULES and appears, uniquely numbered, in render_plan_form_rules(). Create tests/integration/test_planner_density_gate.py::test_density_cap_gates_planner_emission mirroring tests/integration/test_planner_refine_history.py (its _ScriptedLoop that records every prompt and replays scripted texts; Planner(loop=loop, repo_root=repo); run_planner_session(spec_id, root=repo, planner=planner)): write a throwaway governed spec into a tmp_path repo; the scripted loop's FIRST payload contains a step with 2 files and an action longer than 2 * PLAN_STEP_MAX_ACTION_DENSITY chars (over the density cap) plus is otherwise valid, its SECOND payload is a clean plan whose every step is under the cap; assert the session does NOT accept the first (it refines — the recorded refine prompt carries the density reason) and DOES write the second (written == True). Hermetic: no network, no LLM, no reliance on a .env file; the real prompt assembly, validate_plan_form_strict and refine path all run.
- **Commit**: `test(review): density rule in catalog + emission-gate integration test`
- **Done when**: pytest tests/unit/test_plan_form_rules.py::test_action_density_rule_in_catalog tests/integration/test_planner_density_gate.py::test_density_cap_gates_planner_emission passes
- **Unit tests**: `tests/unit/test_plan_form_rules.py::test_action_density_rule_in_catalog`

## Integration tests

- `tests/integration/test_planner_density_gate.py::test_density_cap_gates_planner_emission`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-PLAN-STEP-DENSITY-CAP",
  "title": "Cap plan-step action density at Planner emission",
  "summary": "Add PLAN_STEP_MAX_ACTION_DENSITY = 2600 and a fourth per-step check in validate_plan_form_strict that refuses any step whose action chars / file count exceeds it (step 1), then register the rule in the catalog and prove the cap gates emission end-to-end with a truthful-fake integration test (step 2). The plan supplies an integration test the spec's unit-only ACs omit (deliberate superset; sync the spec AC list post-merge). Every step stays under the density cap it introduces.",
  "steps": [
    {
      "index": 1,
      "title": "Density constant + strict-layer check + unit tests",
      "files": [
        "src/ferova/review/plan.py",
        "tests/unit/test_plan_form_rules.py"
      ],
      "action": "In src/ferova/review/plan.py add module constant PLAN_STEP_MAX_ACTION_DENSITY: int = 2600 beside PLAN_STEP_MAX_FILES/PLAN_STEP_MAX_UNIT_SELECTORS (~plan.py:73-74). Inside validate_plan_form_strict's `for step in plan.steps:` loop (~plan.py:134-160), append a fourth check: density = len(step.action) / max(1, len(step.files)); when density > PLAN_STEP_MAX_ACTION_DENSITY, append a reason citing round(density), PLAN_STEP_MAX_ACTION_DENSITY and the literal \"30-turn\" budget rationale, mirroring the existing file-cap reason text at plan.py:135-141. Register one new sentence in _STRICT_FORM_RULES (~plan.py:60-71) keyed \"_action_density_cap\" describing the chars-per-file cap (distinct sentence so the catalog render stays unique). Re-export PLAN_STEP_MAX_ACTION_DENSITY in the test import block (~tests/unit/test_plan_form_rules.py:15-25). Add tests/unit/test_plan_form_rules.py::test_action_density_cap_rejects_dense_step: build a step (via the _step helper) with 2 files and an action string longer than 2 * PLAN_STEP_MAX_ACTION_DENSITY chars, wrap via _plan, call validate_plan_form_strict, assert some reason contains str(PLAN_STEP_MAX_ACTION_DENSITY) and \"30-turn\"; then the SAME long action spread across enough files that density drops below the cap yields NO reason (proves density, not raw length). Add tests/unit/test_plan_form_rules.py::test_action_density_cap_is_emission_only: a plan whose step exceeds the density cap still round-trips through load_plan / parse_plan_markdown without raising (grandfathering - the check is not a pydantic validator), and validate_plan_form_strict is the only thing that flags it.",
      "commit_message": "feat(review): cap plan-step action density at emission",
      "done_when": "pytest tests/unit/test_plan_form_rules.py::test_action_density_cap_rejects_dense_step tests/unit/test_plan_form_rules.py::test_action_density_cap_is_emission_only passes",
      "unit_tests": [
        "tests/unit/test_plan_form_rules.py::test_action_density_cap_rejects_dense_step",
        "tests/unit/test_plan_form_rules.py::test_action_density_cap_is_emission_only"
      ]
    },
    {
      "index": 2,
      "title": "Catalog registration test + emission-gate integration test",
      "files": [
        "tests/unit/test_plan_form_rules.py",
        "tests/integration/test_planner_density_gate.py"
      ],
      "action": "Add tests/unit/test_plan_form_rules.py::test_action_density_rule_in_catalog asserting the density rule sentence is a value in _STRICT_FORM_RULES and appears, uniquely numbered, in render_plan_form_rules(). Create tests/integration/test_planner_density_gate.py::test_density_cap_gates_planner_emission mirroring tests/integration/test_planner_refine_history.py (its _ScriptedLoop that records every prompt and replays scripted texts; Planner(loop=loop, repo_root=repo); run_planner_session(spec_id, root=repo, planner=planner)): write a throwaway governed spec into a tmp_path repo; the scripted loop's FIRST payload contains a step with 2 files and an action longer than 2 * PLAN_STEP_MAX_ACTION_DENSITY chars (over the density cap) plus is otherwise valid, its SECOND payload is a clean plan whose every step is under the cap; assert the session does NOT accept the first (it refines - the recorded refine prompt carries the density reason) and DOES write the second (written == True). Hermetic: no network, no LLM, no reliance on a .env file; the real prompt assembly, validate_plan_form_strict and refine path all run.",
      "commit_message": "test(review): density rule in catalog + emission-gate integration test",
      "done_when": "pytest tests/unit/test_plan_form_rules.py::test_action_density_rule_in_catalog tests/integration/test_planner_density_gate.py::test_density_cap_gates_planner_emission passes",
      "unit_tests": [
        "tests/unit/test_plan_form_rules.py::test_action_density_rule_in_catalog"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_planner_density_gate.py::test_density_cap_gates_planner_emission"
  ]
}
```
