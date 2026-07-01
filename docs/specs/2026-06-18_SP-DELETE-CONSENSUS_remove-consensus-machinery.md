# SP-DELETE-CONSENSUS — delete the consensus / resolution-plan machinery

**Status:** implemented (factory-shipped)
**Redesign slice:** 10b-2 (after 10b-1 re-sourced `final_verdict` from the
ledger)
**Touches forbidden paths:** no (src + tests only)

## Why

10b-1 (`SP-VERDICT-RESOURCE-LEDGER`) re-sourced `TeamOutcome.final_verdict`
from the findings ledger, so the legacy strict-consensus gate is now dead
code with no live consumer. The 10b cartography confirmed `consensus.py`
is a clean delete — every symbol's only callers were the orchestrator's
(now-removed) informational consensus block and the consensus tests; the
predicate the 06-17 memory feared having to re-home (`is_unparsed_outcome`)
never existed — `findings_bridge._is_unparsed` is self-contained.

## Change

Deleted:
- `consensus.py` (whole module: `evaluate_consensus`, `ConsensusResult`,
  `Disagreement`, `ResolutionPlan/Step/Action`, `build_resolution_plan`,
  `consensus_to_dict`, `plan_to_dict`).
- `persistence.py`: the `pr_resolution_plans` table, `ResolutionPlanRow`,
  `record_resolution_plan`, `fetch_resolution_plans`.
- `orchestrator.py`: the consensus block in `review_pr`, the
  `TeamOutcome.consensus` / `current_plan` fields, the dead
  `_aggregate_verdict` staticmethod, `_render_consensus_section`, and the
  consensus portion of the archive `legacy_body` assembly.
- Tests: `test_review_consensus.py` (wholesale),
  `test_review_consensus_strict_gate.py` (the `evaluate_consensus`
  assertions); the 4 `_aggregate_verdict` tests in `test_review_team.py`.

Re-homed: the `Reviewer._parse_response` truncation/malformed guard tests
(the `[parse_failed:TRUNCATED]` / `[parse_failed:MALFORMED]` marker that
`findings_bridge._is_unparsed` keys on) → new
`tests/unit/test_reviewer_parse_guard.py`.

### Deferred to 10b-3

`report.LEGACY_VERDICT_HEADER` / the `legacy_verdict_block` param and the
orchestrator's `legacy_body` framework stay: the assembly braids the
consensus block (removed here) with the challenge block (removed with the
legacy Coder subtree in 10b-3), so the whole informational section is
retired there, not split across two slices.

## Acceptance

- Full `tests/unit` green (1003); `ruff` + format + no-inline-comments
  clean; no residual `team.consensus` / `current_plan` references.

## Follow-on

10b-3 (hand-shipped): delete the legacy Coder subtree (`run_coder_fix` +
arbiter / challenge / ACCEPT-consistency / iteration-cap / pre-verify +
`coder_verify.py`), the CLI `--no-from-findings` branch, the dead exit-3/8
workflow branches, and `LEGACY_VERDICT_HEADER` / `legacy_body`; mark
`coder_0.4.0.md` RETIRED.
