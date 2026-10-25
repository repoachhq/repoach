---
id: SP-PLAN-SELECTOR-AUDIT-WIRE
title: Promote the whole-plan promised-selector audit out of tmp/ and wire it into plan loading
version: 0.3
status: approved
author: agent
created: 2026-07-24
updated: 2026-10-25

owns:
  code:
    - src/repoach/review/spec_gate.py
    - src/repoach/review/planner.py
  resources: N/A

depends_on: [SP-DEV-STEP-PREFLIGHT, SP-ARCH-GRAPH]
provides_to: []

constraints: {}
---

# Promote the whole-plan promised-selector audit out of tmp/ and wire it into plan loading

## Amendment (0.2, 2026-10-25): a draft spec's absent selectors are planned

Measured on PR #246 (spec-only, `status: draft`): the review recorded
`pr_spec_coverage` = `0|0|base-ref-unavailable|0` and the pure gate
refused with `spec acceptance selectors not all present`. The base ref
had resolved; the spec simply had no committed plan, and the
no-plan branch of the orchestrator reused the `base-ref-unavailable`
marker. A spec-only PR can never carry the tests its criteria name, so
every draft spec PR was refused, and the only way through was
`safe_merge.sh --skip-review`, which the owner forbade on 2026-10-25.

The rule, in `spec_gate.py` (`draft_scope`, `coverage_of_selectors`,
`spec_acceptance_selectors`) and wired by the orchestrator:

- A spec whose head frontmatter `status` is `draft` or `proposed`
  (`PRE_IMPLEMENTATION_STATUSES`) has a draft scope. Without a committed
  plan it is graded against the `pytest` selectors in backticks in its
  `## Acceptance Criteria` section. With a plan, the plan stays the
  contract.
- In a draft scope, a selector whose FILE is absent at head is
  `planned`: recorded in `pr_spec_coverage.planned`, shown in the gate
  JSON under `spec_acceptance.planned[]`, and does not refuse. A
  selector whose file exists but whose test is absent or trivial is
  still `missing`.
- The scope holds only when every changed file is under `docs/` or is
  the spec itself. It is withheld, with one named refusal line each,
  when the PR changes any path outside `docs/` (`draft spec PR touches
  code outside docs (...): its acceptance selectors must be present`),
  when it changes a path under `docs/` that the spec's `owns.code`
  (head or base) names (`draft spec PR touches owned code (...): its
  acceptance selectors must be present`), when the spec exists at base
  with a status past draft (a PR cannot step its own spec back to
  draft), and when the base ref cannot be read.
- Any other status grades as before. The no-plan case of a non-draft
  spec now records `plan-absent` instead of `base-ref-unavailable`;
  the gate reason is unchanged.

## Amendment (0.3, 2026-10-25): a row graded by a retired rule never refuses on its own

Measured on PR #246 after 0.2 landed: the row at head had been written
by the pre-0.2 code, and nothing recomputes a row unless a fresh review
runs, so the gate refused on a grading the current rule no longer
makes.

- `pr_spec_coverage` gains `rule_version INTEGER NOT NULL DEFAULT 0`
  (additive; an existing row reads `0`). Every row written records
  `SPEC_COVERAGE_RULE_VERSION` (`1` = the 0.2 rule); the constant is
  bumped whenever the grading of a selector changes.
- `review gate` (and the auto-merge, on the pinned head tree) calls
  `refresh_stale_coverage` before gathering the facts: when the latest
  row at the decided head has a `rule_version` below the current one, it
  recomputes the coverage with `coverage_at_head` (the row's spec, the
  PR's base ref, the diff's paths; no model call) and records a new
  row, which the decision then reads. A row already at the current
  version is left alone and costs no forge read. A failed recomputation
  is logged with its consequence and the stored row stands (fail
  closed).
- The decision is `rule_version` rather than a code hash: a code hash
  would recompute on every release, a rule marker only when the grading
  changed.

## Intent

The mechanical check that would have caught the REGEN orphaned-selector
drift — "does every promised selector across the WHOLE plan (every
step's `unit_tests` plus the plan-level `integration_tests`) still
resolve to a real `def` at head" — already exists and works, but only
as a 26-line ad hoc script in `tmp/` that a human/agent has to remember
to run by hand during incident triage. It protects nothing by default.
Promote that logic into `src/repoach/review/spec_gate.py` as the single
owning implementation, and call it from the one place in the
plan-driven Developer pipeline that currently has zero such protection:
loading an **already-committed** plan document, which today is trusted
verbatim with no re-audit.

## Context

- `tmp/check_plan_selectors.py` (26 lines, confirmed present at HEAD)
  implements exactly this check — for every `step.unit_tests` and every
  `plan.integration_tests` entry, resolve the file part, read it if it
  exists, and regex-search for `def <name>(` / `async def <name>(` at
  any indentation. `grep -rn "check_plan_selectors" src/ .github/`
  returns zero hits: it is never imported or invoked from source or CI.
- `src/repoach/review/planner.py:99-146` (`_check_promised_selectors`)
  already implements the same presence check — file absent is exempt
  (the file is the deliverable), a present-but-non-satisfying selector
  is exempt when the node id is declared verbatim in the promising
  step's `action` text, otherwise it is an offender — but it is invoked
  **only** from `Planner.plan()`'s own per-attempt refine loop
  (`planner.py:449`), against a single freshly-generated candidate
  plan. Existing tests
  (`tests/unit/test_review_planner.py::TestSelectorCheck`, four cases)
  pin this behavior end-to-end through `Planner.plan()`.
- `src/repoach/review/dev_runner.py:524-574` (`load_or_produce_plan`)
  is the actual gap: its first branch —
  `return load_plan(spec.id, root=repo_root), None` (line 554) — loads
  an **already-committed** `docs/plans/<SP-ID>.md` straight through
  with no selector audit whatsoever. This is precisely the shape of a
  resumed session, or a plan hand-consolidated across rounds outside
  the Planner's own attempt loop (the REGEN incident: "consolidation
  steps orphan promised selectors from earlier steps' lists + the top
  integration list") — nothing re-checks the loaded document's internal
  selector consistency before step execution begins.
- `src/repoach/review/devagent_selfverify.py:465-474` has a related but
  different check (`unit_missing`) that fires only at the **end** of a
  session (after every step has already executed), is scoped to unit
  selectors only, and treats a missing **integration** selector
  (`coverage.missing`) as a log-only warning, not a blocker — too late
  and too narrow to prevent a session from running against a plan whose
  promises don't reconcile.
- `spec_gate.py` already owns the shared selector predicates
  (`promised_present`, `selector_present`) that both `planner.py` and
  `dev_runner.py` import; it is the natural single owning module for
  the promoted whole-plan audit (mirrors the finding's own suggestion:
  "alongside `spec_gate.py`").

## Goals

- G1: Add a public `audit_plan_selectors(plan, repo_root)` function to
  `src/repoach/review/spec_gate.py` that returns every promised
  selector (from every step's `unit_tests` plus `plan.integration_tests`)
  that is an "orphan": its file exists at head, `selector_present`
  returns `False` for it, and (for step-scoped selectors only) the
  node id does not appear verbatim in that step's own `action` text.
  A selector whose file does not exist at head is exempt (undelivered
  deliverable, not a drift).
- G2: `planner.py`'s private `_check_promised_selectors` delegates its
  offender-collection to `spec_gate.audit_plan_selectors` instead of
  re-implementing the loop, keeping its own directive-message wording
  unchanged — a pure delegation with no behavior change, verified by
  the existing `TestSelectorCheck` suite staying green untouched.
- G3: `dev_runner.load_or_produce_plan` calls
  `spec_gate.audit_plan_selectors` immediately after loading an
  **already-existing** plan document (the `load_plan(...)` early-return
  branch) and, when it returns any offenders, fails loud: returns
  `(None, error)` naming every offending selector, instead of handing
  step execution a plan with unreconciled promises.
- G4: `tmp/check_plan_selectors.py` is deleted — its logic now lives,
  tested, in `spec_gate.py`.

## Non-Goals

- NG1: no behavior change to `Planner.plan()`'s own per-attempt refine
  loop beyond the internal delegation in G2 — the four existing
  `TestSelectorCheck` cases (hallucinated selector refined, resolved
  selector accepted, declared-creation accepted, new-file selector
  exempt) must keep passing unmodified.
- NG2: no change to `devagent_selfverify.py`'s end-of-session
  `unit_missing` / `coverage.missing` checks — those stay exactly as
  they are; this spec adds an earlier, whole-plan gate, it does not
  touch the existing later one.
- NG3: no new CLI command or CI workflow step — the audit runs inline
  inside the existing `load_or_produce_plan` call path used by
  `run_developer_session` / `repoach develop`.
- NG4: no change to the per-step preflight logic already in
  `dev_runner.py` (`step_preflight_complete`, the
  `step_preflight_selector_absent` guard) — that machinery is unrelated
  to this spec's plan-load-time audit and is left untouched.
- NG5: no attempt to auto-repair an orphaned plan document (e.g.
  re-invoking the Planner to patch it) — on drift, the session stops
  loudly with a clear error; repair is a human/operator or a follow-up
  spec's job.

## Interface

`src/repoach/review/spec_gate.py` (new public function):

```python
def audit_plan_selectors(plan: ActionPlan, repo_root: Path) -> list[str]:
    """Return every promised selector orphaned relative to the tree at head.

    A selector is orphaned when its file already exists at *repo_root*
    but the selector does not satisfy :func:`selector_present` and,
    for a step-scoped selector, its node id is not declared verbatim in
    the promising step's ``action`` text. A selector whose file does
    not yet exist is exempt — the file is an undelivered deliverable,
    not a drift. Checks every ``step.unit_tests`` entry and every
    ``plan.integration_tests`` entry (plan-level selectors have no
    declared-creation exemption, matching the existing planner-time
    check).

    Args:
        plan: The plan whose promised selectors to audit.
        repo_root: Repository root the selectors resolve against.

    Returns:
        The offending selectors, in plan order; empty when every
        promised selector is either undelivered-and-exempt or
        satisfied/declared.
    """
```

`src/repoach/review/planner.py`:
- `_check_promised_selectors(plan, repo_root)` calls
  `spec_gate.audit_plan_selectors(plan, repo_root)` and, when
  non-empty, formats the same directive message it already returns
  today (unchanged wording/remedies).

`src/repoach/review/dev_runner.py`:
- `load_or_produce_plan`'s existing-plan-doc branch becomes:

```python
try:
    loaded = load_plan(spec.id, root=repo_root)
except FileNotFoundError:
    ...  # unchanged produce path
except ValueError as exc:
    return None, f"committed plan is invalid: {str(exc)[:300]}"
offenders = audit_plan_selectors(loaded, repo_root)
if offenders:
    return None, f"committed plan has orphaned promised selectors: {offenders}"
return loaded, None
```

(exact control flow to be adapted to the function's existing
try/except shape; behavior described here is normative.)

## Behavior

### Nominal

- A freshly-produced plan (no prior `docs/plans/<SP-ID>.md`) is
  unaffected: `load_or_produce_plan` only reaches the new audit call on
  the *existing-plan-doc* branch; the produce branch's plan already
  passed `_check_promised_selectors` inside `Planner.plan()`.
- A previously-committed, internally-consistent plan doc (every
  promised selector either undelivered or already satisfied/declared)
  loads exactly as before — `audit_plan_selectors` returns `[]`.

### Edge cases

- A plan step's promised selector's file does not exist yet (a step not
  yet executed) → exempt, not an offender, regardless of plan-refine
  history.
- A plan-level `integration_tests` selector whose file already exists
  but whose promised `def` is absent → always an offender (no
  declared-creation exemption at plan level, matching the pre-existing
  per-attempt check's own rule).
- A step-level selector whose file exists, whose `def` is absent, but
  whose node id is declared verbatim in that step's own `action` text
  → exempt (the step is still expected to add it).

### Failure scenarios

- A committed plan doc has at least one orphaned selector (file
  exists, `def` absent, not declared) → `load_or_produce_plan` returns
  `(None, error)` naming every offender; `run_developer_session` /
  `repoach develop` stops before any step executes, with a
  `no_op_reason` surfacing the drift instead of silently running steps
  against an inconsistent plan and surfacing the gap later as a judge
  false-absence incident.

## Architecture Impact

- Adds/Removes dependency: none new. `dev_runner.py` already imports
  from `spec_gate.py` (`promised_present`, `selector_present`); this
  spec adds one more name (`audit_plan_selectors`) to that existing
  import. `planner.py` already imports `selector_present` from the
  same module.
- New / changed coupling, cycles, or shared state: reduces coupling —
  the offender-collection logic that today exists independently in
  `tmp/check_plan_selectors.py` (unwired) and `planner.py` (private,
  single call site) is consolidated into one tested, owned function in
  `spec_gate.py` that both `planner.py` and `dev_runner.py` call.
  `tmp/check_plan_selectors.py` is deleted (G4).

## Acceptance Criteria

- [ ] AC1: unit — `spec_gate.audit_plan_selectors` flags a selector
  whose file exists, whose promised `def` is absent, and whose node id
  is not declared in the promising step's `action` text.
- [ ] AC2: unit — `audit_plan_selectors` exempts a selector declared
  verbatim in its step's `action` text even though the `def` is absent.
- [ ] AC3: unit — `audit_plan_selectors` exempts a selector whose file
  does not exist at head (undelivered deliverable), and separately
  flags a `plan.integration_tests` selector whose file exists but whose
  `def` is absent (no declared-creation exemption at plan level).
- [ ] AC4 (INTEGRATION-SHAPED, run as a unit test against a real git
  fixture): `load_or_produce_plan`, given a pre-existing, committed
  `docs/plans/<SP-ID>.md` whose promised selector's backing file exists
  on disk but no longer defines the promised test, returns
  `(None, error)` with the offending selector named in `error`; given
  the same fixture with the selector satisfied, returns
  `(plan, None)` unchanged from current behavior.
- [ ] AC5: promised tests —
  `tests/unit/test_spec_gate.py::test_audit_plan_selectors_flags_selector_whose_file_exists_but_def_absent`,
  `tests/unit/test_spec_gate.py::test_audit_plan_selectors_exempts_selector_declared_in_step_action`,
  `tests/unit/test_spec_gate.py::test_audit_plan_selectors_exempts_selector_in_undelivered_file`,
  `tests/unit/test_review_plan_executor.py::TestLoadOrGeneratePlan::test_existing_plan_with_orphaned_selector_fails_loud`.
  Each of these tests imports/exercises `audit_plan_selectors` (directly
  or via `load_or_produce_plan`), which does not exist on pre-change
  code — every one of them fails (`ImportError`/`AttributeError`, or an
  assertion on the new fail-loud behavior) before this spec lands.
- [ ] AC6: regression — `tests/unit/test_review_planner.py::TestSelectorCheck`
  (all four existing cases) stays green unmodified, proving the G2
  delegation changes no observable `Planner.plan()` behavior.
- [ ] AC7: `tmp/check_plan_selectors.py` no longer exists in the tree.
- [ ] AC8: `ruff check` + `ruff format --check` + `pytest tests/unit`
  green; zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no
  `# noqa`; `python scripts/lint_no_inline_comments.py --summary` clean.
- [ ] AC9 (0.2): `pytest -q tests/unit/test_spec_gate_draft.py` → green:
  a draft spec whose selectors' files are absent merges with them listed
  in `spec_planned`; the same spec `approved` is refused with exactly
  `spec acceptance selectors not all present`; the draft spec with a
  file outside `docs/` in the diff (owned or unrelated `src/`) is
  refused with that reason plus `draft spec PR touches code outside
  docs (<path>): its acceptance selectors must be present`; another
  `docs/` file keeps the exemption; an owned path under `docs/`
  withholds it (`draft spec PR touches owned code (<path>): ...`); a
  spec `approved` at base and `draft` at head is
  refused; an unreadable base ref withholds the exemption; a selector
  in an existing file stays missing; an old `pr_spec_coverage` table
  gains the two columns.
- [ ] AC10 (0.3): `pytest -q tests/unit/test_replay_recomputes_spec_coverage.py`
  → green: `decide_at_head` on the head tree, given a `rule_version 0`
  row `base-ref-unavailable` for a draft spec whose selectors' files
  are absent, records a new row and merges with both selectors in
  `spec_planned`; given the same row at the current version, it reads
  the diff nowhere, records nothing and refuses; an old table gains
  `rule_version` with `0` on its rows.

## Open Questions

(none)
