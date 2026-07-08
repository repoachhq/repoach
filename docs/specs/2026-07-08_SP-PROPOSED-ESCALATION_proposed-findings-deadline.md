---
id: SP-PROPOSED-ESCALATION
title: Deadline and escalation for findings frozen in proposed
version: 0.1
status: approved
author: jfaye (spec-gap verification dossier 2026-07-07; operator GO 2026-07-08)
created: 2026-07-08
updated: 2026-07-08

owns:
  code: []
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Deadline and escalation for findings frozen in proposed

## Intent

Give `proposed` findings a way out. Today a finding can sit in
`proposed` forever: the mechanical verifier defers claims it cannot
check ("no checkable symbol", `finding_verifiers.py:47`) without
persisting anything (`finding_verifiers.py:150-151`), the refuter only
ever sees judged claim types (`refuter.py:145`), and re-verification
only runs inside a review run of the same PR — no age deadline, no
sweep, no dossier. Since SP-CLAIM-TYPE-ROUTING's fail-closed gate
(`merge_gate.py:212`), a BLOCKING proposed finding on a live PR
permanently refuses the merge while the Coder queue ({VERIFIED, OPEN},
`coder_findings.py:64`) and the stuck cap (OPEN-only, `stuck.py:226`)
can never touch it — a deadlock with no exit.

## Context

Live ledger evidence: the 2026-07-06 sweep found 31/33 recent findings
frozen in `proposed`; as of 2026-07-07 the ledger holds 40/86 proposed
(all `missing_test` with an empty `verification_method`), 13 of them
BLOCKING. This is the third variant of the "blocked PR nobody can
repair" family, alongside SP-CI-FINDINGS-WIRE (red CI produces no
finding) and SP-DEV-WRAPUP-ATTRIBUTION (cross-cutting breakage owned
by no step) — both proven live on PR #53 (2026-07-07).

## Goals

- G1: Every verification deferral is PERSISTED without a status
  change: new `findings.record_verification_attempt(db_path,
  finding_id, *, method, result, checked_at_sha)` also increments a
  new `pr_findings.verify_attempts` column (ALTER self-heal per the
  `persistence.py` pattern).
- G2: `verify_findings_for_pr` writes the deferral diagnostic (e.g.
  `method='symbol_search'`, `result='no checkable symbol'`) on every
  deferred finding — `verification_method` is never empty after a run.
- G3: Mechanically-deferred proposed findings FALL BACK to the refuter
  judge in the same run: `judge_findings_for_pr`'s target list
  includes proposed findings whose latest verification attempt was a
  mechanical deferral, still bounded by `_MAX_JUDGED`.
- G4: A pure `stuck.assess_proposed_escalation(findings, *,
  max_attempts=PROPOSED_ESCALATION_ATTEMPTS)` (module constant,
  default 2) returns the blocking findings still proposed whose
  `verify_attempts >= max_attempts`.
- G5: After the verify+judge passes, the orchestrator fires a
  `kind='proposed_escalation'` dossier (finding ids, claims, attempt
  counts, deferral reasons) through the existing settings-guarded
  routine seam, exactly once per finding (fires only when
  `verify_attempts` crosses the threshold).

## Non-Goals

- No change to `ALLOWED_TRANSITIONS` — the lifecycle law is untouched;
  escalation reports on state, it does not invent new states.
- No relaxation of the fail-closed gate: a blocking proposed finding
  still refuses the merge; this spec makes the refusal REPAIRABLE
  (judge fallback) and VISIBLE (dossier), not bypassable.
- No background sweep daemon — attempts only accrue inside review runs.

## Assumptions

- A1: The routine-notification seam used by the existing stuck dossier
  is reusable for a second dossier kind without workflow changes.
- A2: `_MAX_JUDGED` remains an acceptable per-run bound for the judge
  fallback (starved findings accrue attempts across runs and escalate
  via G4/G5 rather than growing the judge budget).

## Behavior

1. Review run verifies findings: mechanical verifiers either decide
   (verified/refuted) or DEFER — every deferral now records an attempt
   with its diagnostic (G1+G2).
2. Same run, judge pass: proposed findings whose latest attempt was a
   mechanical deferral join the judged set, capped by `_MAX_JUDGED`
   (G3) — most frozen `missing_test` claims get a verdict here.
3. End of run: `assess_proposed_escalation` names the blocking
   findings that survived `>= 2` attempts still proposed; the
   orchestrator fires one `proposed_escalation` dossier per
   newly-crossed finding (G4+G5). The operator inherits a named,
   evidenced decision instead of a silent forever-refusal.

## Architecture Impact

Touches `findings.py` (schema + recorder), `finding_verifiers.py`
(persist deferrals), `refuter.py` (fallback targeting),
`stuck.py` (pure assessor + constant), `orchestrator.py` (dossier
wiring) — ~350 LOC, no new files, factory-implementable.

## Acceptance Criteria

- AC1: `record_verification_attempt` persists without a status change
  and `verify_attempts` self-heals on old DBs —
  `tests/unit/test_review_findings.py::test_record_verification_attempt_persists_without_status_change`
  and `::test_verify_attempts_column_self_heals`.
- AC2: A deferred verification persists its diagnostic —
  `tests/unit/test_finding_verifiers.py::test_deferred_verification_persists_diagnostic`.
- AC3: Mechanical deferrals fall back to the judge, bounded —
  `tests/unit/test_review_refuter.py::test_mechanical_deferral_falls_back_to_judge`
  and `::test_judge_fallback_respects_cap`.
- AC4: The pure assessor thresholds correctly —
  `tests/unit/test_stuck.py::test_assess_proposed_escalation_thresholds`.
- AC5: The dossier carries ids/claims/attempts/reasons and fires
  exactly once per finding —
  `tests/unit/test_stuck.py::test_proposed_escalation_dossier_shape`
  and `::test_proposed_escalation_fires_once`.
- AC6: Lifecycle law unchanged — `test_allowed_transitions_law` and
  `test_terminal_states_have_no_exits` stay green; full unit suite
  passes.

## Open Questions

- OQ1: Should the dossier also list NON-blocking frozen findings as an
  appendix (visibility without urgency)? Default: blocking only, keep
  the signal sharp.
