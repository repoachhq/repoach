---
id: SP-DEVAGENT-SELFVERIFY
title: Developer self-verification gate — mechanical + LLM judge (DEVAGENT slice 3)
version: 0.1
status: draft
author: agent
created: 2026-06-28
updated: 2026-06-28

owns:
  code:
    - src/repoach/review/devagent_selfverify.py
  resources:
    - prompts/review/judge_selfverify_0.1.0.md

depends_on:
  - SP-DEVAGENT-LOOP

provides_to: []
constraints: {}
---

# SP-DEVAGENT-SELFVERIFY — the gate the Developer clears before review

## Intent
Slice 3 of the real-coding-agent arc (umbrella `docs/devagent_architecture.md`).
After the agentic loop (slice 2) has implemented every plan step and the wrap-up
suite is green, the Developer must **verify its own work against the spec** before
the result is pushed and handed to the 4 reviewers. The gate has two halves, both
required to pass (operator calibration, 2026-06-27):

- **Mechanical** — the spec's acceptance contract is *present and green*: every
  promised **unit** acceptance selector exists in the tree (via
  `spec_gate.selector_present`; `compute_spec_coverage` is kept for the report), the
  wrap-up unit suite is green (already run), and a final `ruff` gate passes. A
  missing **integration** selector is a warning, not a block — consistent with the
  existing wrap-up policy (`run_developer_session` already warns, not fails, on a
  promised-but-absent integration test).
- **Semantic (LLM judge)** — an independent judge agent reads the spec + the
  branch diff and verdicts whether the implementation actually *satisfies* the
  spec, beyond tests passing — returning `{compliant, reasons, gaps}`.

## Context
Most machinery exists: `spec_gate.acceptance_selectors`/`compute_spec_coverage`
(presence), `coder_loop.run_ruff_gate` (lint), and the one-shot judge pattern
(`refuter.Judge = Callable[[str], str]`, `make_refuter_judge`, tolerant JSON
verdict parsing). This slice composes them into one gate and wires it into
`run_developer_session` at the push boundary.

The judge is a **hard blocker on a verdict** but **fail-open on unavailability**
(operator calibration, 2026-06-28): a parsed `compliant: false` blocks the push; a
judge that cannot produce a verdict (proxy/chain outage, unparseable reply after
retries) does **not** block — the gate proceeds on the mechanical result and logs
loudly. An infra blip must not bury a correct implementation, and the 4 reviewers
remain the downstream net. This mirrors the refuter, which leaves an unjudgeable
finding `proposed` rather than failing it.

## Goals
- G1: A new owned module `review/devagent_selfverify.py` exposing `SelfVerifyResult`,
  `JudgeVerdict`, `make_compliance_judge() -> ComplianceJudge`, and
  `run_self_verify(repo_root, *, spec, plan, suite_green, base, judge) -> SelfVerifyResult`.
- G2: Mechanical half — every promised **unit** acceptance selector must be present
  (`selector_present`), the passed-in `suite_green` must be true, and
  `run_ruff_gate` must pass. Any of these failing → `mechanical_ok=False` with a
  directive reason. A missing integration selector is logged as a warning only.
- G3: Semantic half — render the judge persona with the spec, the extracted
  `## Acceptance Criteria` section, and the branch diff (`git diff base...HEAD`,
  capped); call the judge; parse `{compliant, reasons, gaps}`. A `compliant: false`
  verdict blocks; an unavailable/unparseable judge yields
  `JudgeVerdict(available=False)` and does NOT block (fail-open, loud log).
- G4: `run_self_verify` returns `ok = mechanical_ok and (judge unavailable or
  judge.compliant)`.
- G5: Wire the gate into `run_developer_session` after the wrap-up suite/integration
  pass and **before** the push: a failing gate sets `no_op_reason` + `self_verified`
  and returns without pushing; a passing gate proceeds. New
  `DevSessionResult.self_verified: bool`.

## Non-Goals
- NG1: No re-running of the full unit suite — the wrap-up already ran
  `run_pytest_matrix`; the gate consumes that result (`suite_green`) plus the
  presence check + ruff, to stay non-duplicative.
- NG2: No change to the 4-reviewer handoff itself or the merge gate (the judge is a
  pre-handoff self-check, distinct from `spec_gate`'s merge-time coverage record).
- NG3: No decomposition (slice 4) and no further `revert_working_tree` removal or
  CLI polish (slice 5).

## Interface
- `review.devagent_selfverify.ComplianceJudge = Callable[[str], str]`.
- `review.devagent_selfverify.JudgeVerdict` — dataclass
  `available: bool, compliant: bool, reasons: str, gaps: list[str]`.
- `review.devagent_selfverify.SelfVerifyResult` — dataclass
  `ok: bool, mechanical_ok: bool, coverage: SpecCoverage, ruff_ok: bool,
  judge: JudgeVerdict, reasons: list[str]`.
- `review.devagent_selfverify.make_compliance_judge() -> ComplianceJudge`.
- `review.devagent_selfverify.run_self_verify(repo_root: Path, *, spec: SpecPlan,
  plan: ActionPlan, suite_green: bool, base: str = "develop",
  judge: ComplianceJudge | None) -> SelfVerifyResult`.

## Behavior
- Unit acceptance selectors present, suite green, ruff clean, judge says
  `compliant: true` → `ok=True`, push proceeds.
- A promised **unit** acceptance selector missing → `mechanical_ok=False` → blocked;
  a missing **integration** selector → warning only, not blocking.
- ruff red → `mechanical_ok=False` → blocked.
- Judge `compliant: false` (with mechanical OK) → `ok=False` → blocked, the judge's
  `reasons`/`gaps` surfaced in `no_op_reason`.
- Judge call errors or reply unparseable after retries → `JudgeVerdict(available=
  False)`, does NOT block; `ok` follows the mechanical result; a loud
  `selfverify.judge_unavailable` warning is logged.

## Architecture Impact
- Owns one new leaf module + one persona resource. Import edges:
  `devagent_selfverify` → `agent_engine.agent_loop`, `llm.capability`,
  `review.spec_gate`, `review.coder_loop`, `review.spec`, `review.plan`.
  `depends_on: [SP-DEVAGENT-LOOP]`.
- Edit (wiring, not ownership): `dev_runner.run_developer_session` gains the gate +
  an optional injected `judge`; `DevSessionResult` gains `self_verified`.

## Acceptance Criteria
- [ ] AC1: `tests/unit/test_devagent_selfverify.py` covers: mechanical pass; missing
  acceptance selector → blocked; ruff red → blocked; judge `compliant: false` →
  blocked; judge unavailable (raises / unparseable) → fail-open (not blocked) with
  the verdict marked unavailable; `_extract_acceptance_criteria` and the verdict
  parser.
- [ ] AC2: `tests/unit/test_review_plan_executor.py` (or a session test) asserts the
  gate blocks the push when self-verify fails and that a passing gate sets
  `self_verified=True` — with an injected fake judge (no live proxy).
- [ ] AC3: ruff + format + no-inline + no-silent-except + `arch check` (edge-honesty)
  + full `pytest tests/unit` green under 3.11 and 3.13.

## Open Questions
- The judge currently reads the whole capped diff; if large diffs dilute the verdict
  a future slice can scope it to the spec's owned paths.
