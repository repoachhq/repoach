---
id: SP-WRAPUP-ATTRIB-BASE-ERROR
title: Carry the probed StepCommit on a wrap-up attribution error outcome
version: 0.1
status: approved
author: agent
created: 2026-07-24
updated: 2026-07-24

owns:
  code: [src/repoach/review/wrapup_attribution.py]
  resources: N/A

depends_on: [SP-DEV-STEP-PREFLIGHT, SP-DEV-WRAPUP-ATTRIBUTION]
provides_to: []

constraints: {}
---

# Carry the probed StepCommit on a wrap-up attribution error outcome

## Intent

`attribute_failure_to_step` already knows exactly which commit it was
probing (the plan base, or a numbered step) the instant `run_selector`
raises, but discards that identity before returning — every crash
outcome reads `step=None`. Carry the probed `StepCommit` on the
`"error"` outcome so `dev_runner.py`'s wrap-up dossier line, and the
`no_op_reason` built from it, can say WHERE the walk crashed ("plan
base" vs "step N") instead of an unattributed `error: <selector>:
<message>` an operator can only localize by re-running the walk by
hand.

## Context

- `src/repoach/review/wrapup_attribution.py:104-108` — the base-check
  exception path:
  ```python
  try:
      base_green = run_selector(base.sha, selector)
  except Exception as exc:
      return AttributionOutcome(selector, status="error", error=str(exc)[:300])
  ```
  `base` (the `StepCommit` being probed) is in scope but not attached
  to the returned outcome.
- `src/repoach/review/wrapup_attribution.py:114-117` — the per-step
  exception path, same shape, with `commit` (the `StepCommit` for the
  current loop iteration) in scope and dropped the same way:
  ```python
  except Exception as exc:
      return AttributionOutcome(selector, status="error", error=str(exc)[:300])
  ```
- `src/repoach/review/wrapup_attribution.py:59` (`AttributionOutcome.step`
  docstring): `` `None` for `"error"` `` — the field's contract today
  explicitly forbids carrying this identity for the error case.
- `src/repoach/review/dev_runner.py:1143-1147` — the consumer treats
  every `"error"` outcome identically:
  ```python
  if outcome.status == "error":
      line = f"error: {selector}: {outcome.error}"
      dossier_lines.append(line)
      unrepaired.append((None, "", line))
      continue
  ```
  `outcome.step` is never read here, so the dossier line (and, via
  `unrepaired[0]`, the `no_op_reason` string built at the end of
  `repair_wrapup_failures`) can never distinguish a crash checking the
  plan base from a crash checking step N.
- The other two exception sites this spec does NOT touch:
  `wrapup_attribution.py:121-125` (the no-exception "selector green at
  every recorded commit; attribution inconclusive" fallback — no probe
  was crashing, so there is no `StepCommit` to attach) and
  `dev_runner.py`'s OUTER `except Exception` around the
  `attribute_failure_to_step` call itself (an exception escaping the
  call, not one of the two internal try/excepts) stay `step=None`.
- `wrapup_attribution.py` was introduced by SP-DEV-WRAPUP-ATTRIBUTION,
  whose own frontmatter lists `owns.code: []` — the file is currently
  unowned in the ownership registry, so this spec claims it. `dev_runner.py`
  is owned by SP-DEV-STEP-PREFLIGHT; this spec makes an in-place edit
  to one existing branch of `repair_wrapup_failures`, the same pattern
  SP-DEV-STEP-SATISFIED-COMMIT and SP-PLAN-SELECTOR-AUDIT-WIRE already
  used to touch the same file without claiming it.

## Goals

- G1: `AttributionOutcome.step` is populated (not `None`) for `"error"`
  outcomes produced by a `run_selector` exception: `step=base` for the
  base-check exception, `step=commit` (the loop's current step commit)
  for a per-step exception. The no-exception "green at every recorded
  commit" `"error"` fallback keeps `step=None` — no probe crashed there.
- G2: `AttributionOutcome`'s docstring for `step` is corrected to
  describe the new contract (populated for a crash-carrying `"error"`,
  `None` only for the inconclusive-exhaustion `"error"`).
- G3: `dev_runner.py`'s wrap-up dossier line for an `"error"` outcome
  names WHERE the crash happened when `outcome.step` is present —
  `"plan base"` for `outcome.step.index == 0`, `"step {index}
  ({title})"` otherwise — falling back to today's unqualified `error:
  {selector}: {message}` only when `outcome.step` is `None`.

## Non-Goals

- NG1: no retry policy. This spec only carries the identity so a
  future caller COULD special-case a base-check crash; it adds no
  retry, no re-probe, no change to how many times anything runs.
- NG2: no behavior change to the `"pre_existing"` or
  `"introduced_by_step"` outcomes, or to their existing dossier lines
  and repair dispatch — untouched.
- NG3: no change to the no-exception "selector green at every recorded
  commit; attribution inconclusive" fallback (`wrapup_attribution.py:121-125`)
  — it keeps returning `step=None`, unchanged.
- NG4: no change to `dev_runner.py`'s OUTER `except Exception` block
  around the `attribute_failure_to_step` call itself (the "attribution
  crashed" dossier line) — a distinct code path, out of this spec's
  scope.
- NG5: no change to `AttributionOutcome.status`'s `Literal` values or
  to any public signature beyond the docstring in G2.

## Interface

`src/repoach/review/wrapup_attribution.py` (no signature changes —
behavior only, within `attribute_failure_to_step`):
- Base-check exception path returns
  `AttributionOutcome(selector, status="error", step=base, error=str(exc)[:300])`.
- Per-step exception path returns
  `AttributionOutcome(selector, status="error", step=commit, error=str(exc)[:300])`.
- `AttributionOutcome.step`'s docstring updated to state it is
  populated for a crash-carrying `"error"` (naming the commit being
  probed) and `None` only for the inconclusive-exhaustion `"error"`.

`src/repoach/review/dev_runner.py` (`repair_wrapup_failures`, the
`outcome.status == "error"` branch only):
- When `outcome.step is not None`, build the dossier line as
  `f"error: {selector}: crashed at {step_label}: {outcome.error}"`
  where `step_label` is `outcome.step.title` (already `"plan base"`
  for index `0`, per `_step_commits_for_plan`) when
  `outcome.step.index == 0`, else
  `f"step {outcome.step.index} ({outcome.step.title})"`.
- When `outcome.step is None`, keep today's
  `f"error: {selector}: {outcome.error}"` line unchanged.

## Behavior

### Nominal

- `run_selector` raises while checking the plan base → the returned
  `AttributionOutcome` carries `step=base`; the wrap-up dossier line
  reads `error: <selector>: crashed at plan base: <message>`.
- `run_selector` raises while checking step N's commit → the returned
  `AttributionOutcome` carries `step=<that StepCommit>`; the dossier
  line reads `error: <selector>: crashed at step N (<title>):
  <message>`.

### Edge cases

- Every recorded commit runs `run_selector` without raising, but the
  selector is green everywhere (the inconclusive-exhaustion case) →
  `AttributionOutcome.step` stays `None`; the dossier line is
  unchanged from today (`error: <selector>: <message>`).
- The exception escapes `attribute_failure_to_step`'s own call in
  `dev_runner.py` (rather than being caught inside it) → unaffected by
  this spec; that branch never constructs an `AttributionOutcome` at
  all.

### Failure scenarios

- N/A beyond the above — this spec only enriches an already fail-closed
  path; no new failure mode is introduced.

## Architecture Impact

- Adds/Removes dependency: none. In-place behavior change inside
  `attribute_failure_to_step` (this spec's own file) and inside one
  existing branch of `repair_wrapup_failures` in `dev_runner.py`
  (owned by SP-DEV-STEP-PREFLIGHT) — no new cross-module import, no
  new coupling.
- New / changed coupling, cycles, or shared state: none.

## Diagram

N/A (in-place fix, no new components).

## Acceptance Criteria

- [ ] AC1: unit — in `attribute_failure_to_step`, a `run_selector` that
  raises while checking `step_commits[0]` (the base) yields an
  `AttributionOutcome` with `status == "error"` and `step ==
  step_commits[0]`; a `run_selector` that returns green at the base
  but raises on the FIRST real step yields `status == "error"` and
  `step` equal to that step's `StepCommit`. Must FAIL on pre-change
  code (today both cases yield `step is None`).
- [ ] AC2: unit — the no-exception "green at every recorded commit"
  path still yields `status == "error"` with `step is None`
  (regression guard for NG3/G1's carve-out).
- [ ] AC3 (INTEGRATION-SHAPED, run as unit against `dev_runner.py`
  directly): `repair_wrapup_failures`, given a real two-commit git repo
  (base green, one step commit) and a monkeypatched
  `_run_selector_at_commit` that raises for the probed commit, produces
  a `result.wrapup_dossier` line containing `"plan base"` when the base
  probe raises, and a line containing the step's title (e.g. `"Step
  one"`) when the step probe raises instead — and the returned
  `no_op_reason` string contains the same wording. Must FAIL on
  pre-change code (today the dossier line is the unqualified `error:
  <selector>: <message>` in both cases, naming neither).
- [ ] AC4: promised tests —
  `tests/unit/test_wrapup_attribution.py::test_attribution_error_carries_probed_base_commit`,
  `tests/unit/test_wrapup_attribution.py::test_attribution_error_carries_probed_step_commit`,
  `tests/unit/test_dev_runner_wrapup.py::test_wrapup_error_dossier_names_plan_base`,
  `tests/unit/test_dev_runner_wrapup.py::test_wrapup_error_dossier_names_step`.
- [ ] AC5: `ruff` + `ruff format --check` + `pytest tests/unit` green;
  zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`;
  `repoach arch graph --check` exits 0.

## Open Questions

(none)
