---
id: SP-ORCH-DOCSTRING
title: Truthful orchestrator module docstring
version: 0.1
status: approved
author: jfaye (tech-debt ledger entry 4)
created: 2026-07-03
updated: 2026-07-03

owns:
  code: [src/ferova/review/orchestrator.py]
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Truthful orchestrator module docstring

## Intent

Make the `orchestrator.py` module docstring describe the pipeline that
actually runs. It still narrates the retired verdict-first design:
"Aggregates their verdicts into a final team verdict", "does **not**
auto-merge anything and does **not** auto-invoke the Coder bot — those
are explicit follow-up actions (future :func:`run_coder_response`)".
All three claims are false since the evidence-first flip: the team
verdict is derived from the findings ledger, and both the auto-merge
and the findings-driven Coder loop exist and run from the CI workflow.

## Context

`orchestrator.py` hosts `ReviewTeamOrchestrator.review_pr`, the entry
point invoked by `ferova review pr` and the auto-review workflow. The
current pipeline it drives: fetch the PR diff via `gh` → run the four
reviewers concurrently → hallucination guard → optional round-2
confirm-or-retract dialogue → record findings and the review-integrity
row → mechanical verification (`finding_verifiers`) and the
adversarial refuter for judged claims → derive the team verdict from
the ledger (`merge_gate.verdict_from_facts`) → post inline comments,
per-bot reviews and the REPORT-ONLY sticky archive comment → persist
outcomes. Auto-merge and the Coder fix loop live in `auto_merge.py`
and `coder_findings.py`, driven by the workflow — the orchestrator
reviews; it never merges.

## Goals

- G1: The module docstring (currently lines 1-19) describes the
  evidence-first pipeline above, step by step, truthfully.
- G2: The stale phrases are gone: no "Aggregates their verdicts", no
  "does **not** auto-merge", no "future :func:`run_coder_response`",
  and the archive comment is described as report-only rather than as
  the retrievable source of truth.
- G3: A unit test pins the docstring's truthfulness so the retired
  narrative cannot silently return.

## Non-Goals

- NG1: No change to any executable statement, import, or other
  docstring in the module — this is a module-docstring-only slice.
- NG2: No behavioural or API change of any kind.

## Assumptions

- A1: The existing orchestrator test suites (`test_review_team.py`,
  `test_review_round_two.py`, …) stay green untouched.
- A2: `orchestrator.py` has no owner in the arch registry (verified
  2026-07-03: `Registry.owner_of` returns None), so this spec may
  claim `owns.code` without a disjointness conflict.

## Interface

Inputs: N/A (documentation-only change).

Outputs: N/A.

Errors: N/A.

## Behavior

### Nominal

The module docstring reads as an accurate map for a newcomer: what
`review_pr` does today, in pipeline order, including that the verdict
is derived from the findings ledger and that the archive comment is
report-only; it states explicitly that auto-merge and the Coder loop
are separate workflow-driven follow-ups hosted elsewhere.

### Edge cases

- N/A (documentation-only).

### Failure scenarios

- N/A (documentation-only).

## Architecture Impact

- No edge added or removed. `orchestrator.py` moves from the frontier
  into this spec's `owns.code`; its imports resolve to frontier files,
  so `depends_on` stays empty.

## Diagram

N/A (docstring-only slice).

## Acceptance Criteria

- [ ] AC1: `grep -nE "Aggregates their verdicts|does \*\*not\*\* auto-merge|run_coder_response" src/ferova/review/orchestrator.py` returns no matches.
- [ ] AC2: The new module docstring mentions the findings ledger as
  the source of the derived team verdict (the words "findings" and
  "derived" both appear in `orchestrator.__doc__`), and describes the
  archive comment as report-only ("report-only" appears).
- [ ] AC3: `tests/unit/test_orchestrator_docstring.py` pins AC1+AC2 via
  `test_orchestrator_docstring_drops_the_retired_narrative` and
  `test_orchestrator_docstring_describes_the_ledger_pipeline`, and passes.
- [ ] AC4: The full unit suite passes with no change to any
  non-docstring line of `orchestrator.py`.

## Open Questions

(none)
