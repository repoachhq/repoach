---
id: SP-PLAN-QUALITY
title: Plan-form convergence — full rule catalog in the Planner loop, size cap, no-stub lint
version: 0.1
status: approved
author: jfaye (urgent operator directive 2026-07-08; 4/5 planning sessions exhausted on form rules)
created: 2026-07-08
updated: 2026-07-08

owns:
  code: []
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Plan-form convergence — full rule catalog in the Planner loop, size cap, no-stub lint

## Intent

Make valid plans the NORMAL Planner outcome again, and encode two
fresh operator rules as mechanical plan-form lints: steps sized to
the agent's turn budget, and no plan may instruct stubbing behavior
the plan itself introduces.

## Context

Evidence, 2026-07-07/08: four of the last five planning sessions
exhausted all five parse attempts on interlocking plan-form rules
(SP-CHAIN-DEAD-HOP-QUARANTINE twice, SP-PROPOSED-ESCALATION,
SP-AUTOMERGE-FRESH-HEAD; only SP-CI-FINDINGS-WIRE and SP-RELEASE-GATE
converged). The violations OSCILLATE — fixing rule N reintroduces
rule N-1 (integration selector under tests/unit, then integration
test omitted, then the integration step missing unit tests, then bare
file selectors, then undeclared creations). SP-PLANNER-REFINE-HISTORY
(#50) feeds the errors already made, but the model never sees the
FULL rule set — it discovers rules one failure at a time. Meanwhile
the fallback (hand-authored plans) carries its own defects: a
four-file step blew the 30-turn budget (dead-hop step 3), a step
contract omitted a test file exercising the changed entry point
(fresh-head step 3), and one plan instructed stubbing the new
verification in existing fixtures — triggering the operator's no-stub
rule ("s'il y a un stub évoqué, on écrit une spec"; this spec).

## Goals

- G1: Single-source rule catalog. Each plan-form validator on
  ActionPlan/PlanStep declares a one-line, human-readable rule
  sentence; `render_plan_form_rules()` renders the COMPLETE catalog
  from those declarations (never a hand-maintained copy). The
  catalog is injected code-side (planner.py — `prompts/review/*`
  stays operator-owned and untouched) into the INITIAL planning
  prompt AND every refine turn, alongside the existing error history.
- G2: Step-size lint. New validation errors when a step touches more
  than `PLAN_STEP_MAX_FILES = 3` files or promises more than
  `PLAN_STEP_MAX_UNIT_SELECTORS = 5` unit selectors (module
  constants; proxy for the Developer's 30-turn budget — the dead-hop
  step-3 blowout is the evidence).
- G3: No-stub lint. A step whose action text instructs `stub` or
  `monkeypatch` of a symbol or module that ANY step of the same plan
  touches is a validation error citing the operator rule verbatim
  ("no stubs — a stub temptation becomes a spec"). Truthful boundary
  fakes remain expressible (actions describing fakes of external
  boundaries — gh, LLM, network — do not trip the lint when the
  faked object is not plan-touched).
- G4: Convergence telemetry. Every planner attempt persists
  (spec_id, attempt, violated_rule) so oscillation is measurable;
  `ferova review insights` gains a planner section (rule →
  violation count across sessions). The parse-attempt budget becomes
  a setting (`planner_parse_attempts`, default 5) — fresh-head's
  attempt 4 nearly converged.

## Non-Goals

- NG1: No edits under `prompts/review/` (operator-owned; the catalog
  rides the code-side prompt assembly, as refine history already
  does).
- NG2: No relaxation of any existing form rule.
- NG3: No semantic plan review automation — coherence and contract
  completeness stay with the operator's plan review.

## Assumptions

- A1: Every existing form rule lives in (or can be moved into) an
  ActionPlan/PlanStep validator that can carry a rule sentence.
- A2: The planner telemetry can reuse the existing SQLite path
  (`get_settings().db_path`) with a small new table, self-healed like
  other schema additions.

## Behavior

Planning session start: the assembled prompt contains the full
numbered rule catalog. Refine turn: catalog + full error history (the
existing mechanism) — the model corrects against the whole rule set
rather than the last error alone. Validation: oversized or
stub-instructing steps fail with rule-citing messages exactly like
other form errors, so the same refine loop handles them. Insights:
`ferova review insights` shows which rules cost attempts.

## Acceptance Criteria

- AC1: `tests/unit/test_plan_form_rules.py::test_rule_catalog_covers_every_validator`
  — the rendered catalog has one entry per registered rule and adding
  a validator without a rule sentence fails the test.
- AC2: `::test_step_size_cap_rejects_oversized_step` — a 4-file step
  and a 6-selector step each fail with the size rule cited.
- AC3: `::test_no_stub_lint_rejects_stubbing_touched_behavior` — an
  action saying "monkeypatch resolve_verified_head" while another
  step touches auto_merge.py fails, citing the operator rule.
- AC4: `::test_no_stub_lint_allows_truthful_boundary_fakes` — an
  action describing a fake gh boundary object passes.
- AC5: `tests/unit/test_planner_prompt_rules.py::test_initial_prompt_carries_full_catalog`
  and `::test_refine_prompt_carries_catalog_and_history` (fake loop
  capturing prompts; the real prompt-assembly code runs — no stubs of
  planner internals).
- AC6: `tests/unit/test_planner_telemetry.py::test_attempt_rows_persisted`
  and `::test_insights_reports_rule_violations`.
- AC7: Integration:
  `tests/integration/test_planner_form_convergence.py::test_catalog_present_in_first_planner_request`
  — a planner session against a throwaway spec in tmp_path, driven
  with a truthful fake loop that records the first request, shows the
  catalog present before any failure occurred. Hermetic: no network,
  no .env reliance.
- AC8: Full unit suite green; `planner_parse_attempts` respected
  (`tests/unit/test_planner_prompt_rules.py::test_attempt_budget_setting`).

## Open Questions

- OQ1: Should the size caps also gate hand-authored plans at
  `load_plan` time (they do, mechanically — any objection is a spec
  amendment raising the constants)? Default: yes, same rules for
  every author.
