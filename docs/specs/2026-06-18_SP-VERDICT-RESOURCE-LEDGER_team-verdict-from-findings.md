# SP-VERDICT-RESOURCE-LEDGER — source the team verdict from the findings ledger

**Status:** implemented (factory-shipped)
**Redesign slice:** 10b-1 (the behaviour slice — isolated first; 10b-2
deletes `consensus.py`, 10b-3 deletes the legacy Coder subtree)
**Touches forbidden paths:** no (src + tests only)

## Why

`TeamOutcome.final_verdict` is still produced by
`consensus.evaluate_consensus` — the legacy strict gate. Slice 10b deletes
`consensus.py`, but `final_verdict` has live consumers (CLI exit-2 →
auto_fix trigger + auto_merge job condition; the `_fire_routine` quota
gate; archive/report serialisation). It must be re-sourced from the
findings ledger **before** consensus can die — this slice does exactly
that, alone, so the behaviour change is easy to validate and revert.

Since the SP-PURE-MERGE-GATE flip (#390) the authoritative merge decision
is already `compute_merge_decision` over re-verified ledger facts;
`final_verdict` no longer gates the actual merge. This slice brings the
*review verdict* into lockstep with the *findings dimension* of that gate.

## Change

`merge_gate.verdict_from_facts(facts: MergeFacts) -> ReviewVerdict`:

```
REQUEST_CHANGES  iff  open_blocking_findings > 0  OR  not review_complete
APPROVE          otherwise
```

The orchestrator sets `final_verdict` from
`summarise_ledger_facts(db, pr, head_sha)` (the recorded-ledger snapshot,
computed after the verify/judge/sentinel passes) instead of
`consensus.final_verdict`. `evaluate_consensus` / the resolution-plan
block stay for now (informational only) and are removed in 10b-2.

### Operator decision (sémantique A)

CI freshness and spec coverage are deliberately **not** folded into the
verdict: CI is still pending while the review runs, and both are
re-checked by `compute_merge_decision`. The verdict tracks only the
findings + review-completeness dimensions, so it can never contradict the
merge it gates. This is a behaviour change vs. the legacy consensus, which
also blocked on `COMMENT` verdicts and `major` comments the pure gate
would happily merge — those now resolve to `APPROVE` (nothing for the
Coder to fix; the gate merges if CI is green and no blocking finding
survives). An **unparsed / crashed reviewer** makes `review_complete`
False → `REQUEST_CHANGES`, exactly as the merge gate already refuses on
`review_complete=False` (the CRITICAL #2 integrity fix).

## Acceptance

- `tests/unit/test_merge_gate.py`: `verdict_from_facts` approves a clean
  ledger; REQUEST_CHANGES on open blocking findings; REQUEST_CHANGES on an
  incomplete review; ignores CI / spec coverage.
- `tests/unit/test_review_team.py`: a crashed reviewer (unparsed) now
  yields a team `REQUEST_CHANGES` (was `APPROVE` under consensus) —
  assertion updated to pin the new, gate-consistent semantics.
- Full `tests/unit` green; `ruff` + format + no-inline-comments clean.

## Follow-on

10b-2: delete `consensus.py` + resolution-plan persistence + the
orchestrator consensus block + `_aggregate_verdict` + `LEGACY_VERDICT_HEADER`
(re-home the `[parse_failed:…]` marker test). 10b-3: delete the legacy
Coder subtree (`run_coder_fix` + arbiter/challenge/ACCEPT/iteration-cap/
pre-verify + `coder_verify.py`), hand-shipped.
