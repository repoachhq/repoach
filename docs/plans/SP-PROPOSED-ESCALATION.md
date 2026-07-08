# SP-PROPOSED-ESCALATION — Deadline and escalation for findings frozen in proposed

Give proposed findings a way out: persist every verification deferral with its diagnostic (new verify_attempts column, self-healed), fall deferred findings back to the refuter judge in the same run, and after the verify+judge passes name the blocking findings still proposed after N attempts in a proposed_escalation dossier fired through the existing routine seam. Five small steps mirroring the existing stuck.py / persistence.py patterns; the counter and assessor stay pure. Hand-authored after a Planner session exhausted five attempts on plan-form rules.

## Step 1 — Ledger primitive: record_verification_attempt + verify_attempts column

- **Files**: `src/ferova/review/findings.py`, `tests/unit/test_review_findings.py`
- **Action**: In src/ferova/review/findings.py add a verify_attempts INTEGER NOT NULL DEFAULT 0 column to the pr_findings table, self-healed on existing databases the same way persistence.py self-heals introduced columns (ALTER TABLE ADD COLUMN guarded by an inspection of existing columns, documented with this spec id). Add record_verification_attempt(db_path, finding_id, *, method, result, checked_at_sha) -> int: it updates the finding's verification_method, verification_result and checked_at_sha, increments verify_attempts, NEVER changes status, and returns the new attempt count. Expose verify_attempts on the Finding model (default 0) so fetch_findings round-trips it. Add the unit tests tests/unit/test_review_findings.py::test_record_verification_attempt_persists_without_status_change (record a proposed finding, call record_verification_attempt twice with method='symbol_search', result='no checkable symbol'; fetch back and assert status is still PROPOSED, verification_method and verification_result carry the diagnostic, checked_at_sha updated, verify_attempts == 2) and tests/unit/test_review_findings.py::test_verify_attempts_column_self_heals (create the schema WITHOUT the column by monkeypatching or by issuing ALTER TABLE DROP COLUMN on a fresh db, then call init_findings_schema again and assert record_verification_attempt works — mirroring the persistence.py self-heal test pattern).
- **Commit**: `feat(review): verification attempts persisted on the findings ledger`
- **Done when**: pytest tests/unit/test_review_findings.py::test_record_verification_attempt_persists_without_status_change tests/unit/test_review_findings.py::test_verify_attempts_column_self_heals passes
- **Unit tests**: `tests/unit/test_review_findings.py::test_record_verification_attempt_persists_without_status_change`, `tests/unit/test_review_findings.py::test_verify_attempts_column_self_heals`

## Step 2 — Verifier deferrals persist their diagnostic

- **Files**: `src/ferova/review/finding_verifiers.py`, `tests/unit/test_finding_verifiers.py`
- **Action**: In src/ferova/review/finding_verifiers.py, verify_findings_for_pr currently drops mechanical deferrals on the floor: when verify_finding returns a PROPOSED status (e.g. ('symbol_search', 'no checkable symbol') at finding_verifiers.py:47) nothing is persisted. Change verify_findings_for_pr so every deferral calls findings.record_verification_attempt(db_path, finding.id, method=<returned method>, result=<returned result>, checked_at_sha=head_sha) — decided statuses (verified/refuted) keep their existing persistence path unchanged. After any run, no examined finding is left with an empty verification_method. Add the unit test tests/unit/test_finding_verifiers.py::test_deferred_verification_persists_diagnostic: seed a blocking missing_test finding whose promised symbol does not exist in a tmp repo, run verify_findings_for_pr, fetch the finding back and assert status is still PROPOSED, verification_method == 'symbol_search', verification_result == 'no checkable symbol' and verify_attempts == 1; run verify_findings_for_pr again and assert verify_attempts == 2.
- **Commit**: `feat(review): mechanical deferrals persist their diagnostic`
- **Done when**: pytest tests/unit/test_finding_verifiers.py::test_deferred_verification_persists_diagnostic passes
- **Unit tests**: `tests/unit/test_finding_verifiers.py::test_deferred_verification_persists_diagnostic`

## Step 3 — Deferred findings fall back to the refuter judge

- **Files**: `src/ferova/review/refuter.py`, `tests/unit/test_review_refuter.py`
- **Action**: In src/ferova/review/refuter.py, judge_findings_for_pr builds judged_targets from proposed findings whose claim_type is in JUDGED_CLAIM_TYPES (refuter.py:145). Extend the target list: also include proposed findings whose latest verification attempt was a mechanical deferral (verify_attempts >= 1 and status PROPOSED and claim_type NOT in JUDGED_CLAIM_TYPES), keeping the combined list bounded by _MAX_JUDGED with the original judged claim types first. The judge then decides verified/refuted for claims the mechanical verifiers could not check, in the same run. Add the unit tests tests/unit/test_review_refuter.py::test_mechanical_deferral_falls_back_to_judge (a proposed missing_test finding with verify_attempts=1 lands in the judged target list and a stubbed judge decision persists its status) and tests/unit/test_review_refuter.py::test_judge_fallback_respects_cap (with _MAX_JUDGED judged-type findings already queued, a deferred mechanical finding does not extend the list beyond _MAX_JUDGED).
- **Commit**: `feat(review): deferred mechanical findings fall back to the judge`
- **Done when**: pytest tests/unit/test_review_refuter.py::test_mechanical_deferral_falls_back_to_judge tests/unit/test_review_refuter.py::test_judge_fallback_respects_cap passes
- **Unit tests**: `tests/unit/test_review_refuter.py::test_mechanical_deferral_falls_back_to_judge`, `tests/unit/test_review_refuter.py::test_judge_fallback_respects_cap`

## Step 4 — Pure assessor and dossier builder in stuck.py

- **Files**: `src/ferova/review/stuck.py`, `tests/unit/test_stuck.py`
- **Action**: In src/ferova/review/stuck.py add PROPOSED_ESCALATION_ATTEMPTS: int = 2 (module constant), a pure assess_proposed_escalation(findings, *, max_attempts=PROPOSED_ESCALATION_ATTEMPTS) returning the blocking findings still PROPOSED whose verify_attempts >= max_attempts (mirroring assess_stuck at stuck.py:173), a pure select_newly_escalated(findings, *, max_attempts=PROPOSED_ESCALATION_ATTEMPTS) returning only those whose verify_attempts == max_attempts (the just-crossed set, so a dossier fires exactly once per finding across successive runs), and build_proposed_escalation_dossier(findings) mirroring build_stuck_dossier (stuck.py:243) with kind='proposed_escalation' and, per finding: id, claim, claim_type, verify_attempts, and the deferral diagnostic (verification_method + verification_result). Add the unit tests tests/unit/test_stuck.py::test_assess_proposed_escalation_thresholds (attempts 1 → empty, attempts 2 → returned, non-blocking or non-proposed → excluded), tests/unit/test_stuck.py::test_proposed_escalation_dossier_shape (dossier carries kind, ids, claims, attempt counts and deferral reasons) and tests/unit/test_stuck.py::test_proposed_escalation_fires_once (select_newly_escalated returns a finding at exactly max_attempts and excludes it at max_attempts + 1).
- **Commit**: `feat(review): proposed-escalation assessor and dossier builder`
- **Done when**: pytest tests/unit/test_stuck.py::test_assess_proposed_escalation_thresholds tests/unit/test_stuck.py::test_proposed_escalation_dossier_shape tests/unit/test_stuck.py::test_proposed_escalation_fires_once passes
- **Unit tests**: `tests/unit/test_stuck.py::test_assess_proposed_escalation_thresholds`, `tests/unit/test_stuck.py::test_proposed_escalation_dossier_shape`, `tests/unit/test_stuck.py::test_proposed_escalation_fires_once`

## Step 5 — Orchestrator fires the dossier; end-to-end integration test

- **Files**: `src/ferova/review/orchestrator.py`, `tests/unit/test_stuck.py`, `tests/integration/test_proposed_escalation.py`
- **Action**: In src/ferova/review/orchestrator.py, after the verify+judge passes complete for a review run, fetch the PR's findings and compute select_newly_escalated(...); when non-empty, build build_proposed_escalation_dossier(...) and fire it through the same settings-guarded routine seam the stuck dossier uses (the _fire_routine pathway at orchestrator.py:832 — reuse its guard so no routine secrets means no-op, never an error). Add the unit test tests/unit/test_stuck.py::test_orchestrator_escalation_seam_is_guarded (with routine settings absent, firing the proposed-escalation dossier is a silent no-op — monkeypatched transport never called). Create tests/integration/test_proposed_escalation.py with test_frozen_proposed_finding_escalates_end_to_end: in a tmp repo and tmp ledger, record one BLOCKING missing_test finding whose promised symbol resolves nowhere; call verify_findings_for_pr once and assert verify_attempts == 1 with the diagnostic persisted and assess_proposed_escalation empty; call verify_findings_for_pr again and assert verify_attempts == 2, assess_proposed_escalation returns exactly this finding, select_newly_escalated returns it (crossing), and build_proposed_escalation_dossier renders kind='proposed_escalation' with the claim and 'no checkable symbol'. Hermetic: no network, no LLM (the judge fallback is NOT invoked here — unit-tested with stubs in step 3), no reliance on a .env file.
- **Commit**: `feat(review): fire the proposed-escalation dossier from the orchestrator`
- **Done when**: pytest tests/unit/test_stuck.py::test_orchestrator_escalation_seam_is_guarded tests/integration/test_proposed_escalation.py::test_frozen_proposed_finding_escalates_end_to_end passes
- **Unit tests**: `tests/unit/test_stuck.py::test_orchestrator_escalation_seam_is_guarded`

## Integration tests

- `tests/integration/test_proposed_escalation.py::test_frozen_proposed_finding_escalates_end_to_end`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-PROPOSED-ESCALATION",
  "title": "Deadline and escalation for findings frozen in proposed",
  "summary": "Give proposed findings a way out: persist every verification deferral with its diagnostic (new verify_attempts column, self-healed), fall deferred findings back to the refuter judge in the same run, and after the verify+judge passes name the blocking findings still proposed after N attempts in a proposed_escalation dossier fired through the existing routine seam. Five small steps mirroring the existing stuck.py / persistence.py patterns; the counter and assessor stay pure.",
  "steps": [
    {
      "index": 1,
      "title": "Ledger primitive: record_verification_attempt + verify_attempts column",
      "files": [
        "src/ferova/review/findings.py",
        "tests/unit/test_review_findings.py"
      ],
      "action": "In src/ferova/review/findings.py add a verify_attempts INTEGER NOT NULL DEFAULT 0 column to the pr_findings table, self-healed on existing databases the same way persistence.py self-heals introduced columns (ALTER TABLE ADD COLUMN guarded by an inspection of existing columns, documented with this spec id). Add record_verification_attempt(db_path, finding_id, *, method, result, checked_at_sha) -> int: it updates the finding's verification_method, verification_result and checked_at_sha, increments verify_attempts, NEVER changes status, and returns the new attempt count. Expose verify_attempts on the Finding model (default 0) so fetch_findings round-trips it. Add the unit tests tests/unit/test_review_findings.py::test_record_verification_attempt_persists_without_status_change (record a proposed finding, call record_verification_attempt twice with method='symbol_search', result='no checkable symbol'; fetch back and assert status is still PROPOSED, verification_method and verification_result carry the diagnostic, checked_at_sha updated, verify_attempts == 2) and tests/unit/test_review_findings.py::test_verify_attempts_column_self_heals (create the schema WITHOUT the column by monkeypatching or by issuing ALTER TABLE DROP COLUMN on a fresh db, then call init_findings_schema again and assert record_verification_attempt works — mirroring the persistence.py self-heal test pattern).",
      "commit_message": "feat(review): verification attempts persisted on the findings ledger",
      "done_when": "pytest tests/unit/test_review_findings.py::test_record_verification_attempt_persists_without_status_change tests/unit/test_review_findings.py::test_verify_attempts_column_self_heals passes",
      "unit_tests": [
        "tests/unit/test_review_findings.py::test_record_verification_attempt_persists_without_status_change",
        "tests/unit/test_review_findings.py::test_verify_attempts_column_self_heals"
      ]
    },
    {
      "index": 2,
      "title": "Verifier deferrals persist their diagnostic",
      "files": [
        "src/ferova/review/finding_verifiers.py",
        "tests/unit/test_finding_verifiers.py"
      ],
      "action": "In src/ferova/review/finding_verifiers.py, verify_findings_for_pr currently drops mechanical deferrals on the floor: when verify_finding returns a PROPOSED status (e.g. ('symbol_search', 'no checkable symbol') at finding_verifiers.py:47) nothing is persisted. Change verify_findings_for_pr so every deferral calls findings.record_verification_attempt(db_path, finding.id, method=<returned method>, result=<returned result>, checked_at_sha=head_sha) — decided statuses (verified/refuted) keep their existing persistence path unchanged. After any run, no examined finding is left with an empty verification_method. Add the unit test tests/unit/test_finding_verifiers.py::test_deferred_verification_persists_diagnostic: seed a blocking missing_test finding whose promised symbol does not exist in a tmp repo, run verify_findings_for_pr, fetch the finding back and assert status is still PROPOSED, verification_method == 'symbol_search', verification_result == 'no checkable symbol' and verify_attempts == 1; run verify_findings_for_pr again and assert verify_attempts == 2.",
      "commit_message": "feat(review): mechanical deferrals persist their diagnostic",
      "done_when": "pytest tests/unit/test_finding_verifiers.py::test_deferred_verification_persists_diagnostic passes",
      "unit_tests": [
        "tests/unit/test_finding_verifiers.py::test_deferred_verification_persists_diagnostic"
      ]
    },
    {
      "index": 3,
      "title": "Deferred findings fall back to the refuter judge",
      "files": [
        "src/ferova/review/refuter.py",
        "tests/unit/test_review_refuter.py"
      ],
      "action": "In src/ferova/review/refuter.py, judge_findings_for_pr builds judged_targets from proposed findings whose claim_type is in JUDGED_CLAIM_TYPES (refuter.py:145). Extend the target list: also include proposed findings whose latest verification attempt was a mechanical deferral (verify_attempts >= 1 and status PROPOSED and claim_type NOT in JUDGED_CLAIM_TYPES), keeping the combined list bounded by _MAX_JUDGED with the original judged claim types first. The judge then decides verified/refuted for claims the mechanical verifiers could not check, in the same run. Add the unit tests tests/unit/test_review_refuter.py::test_mechanical_deferral_falls_back_to_judge (a proposed missing_test finding with verify_attempts=1 lands in the judged target list and a stubbed judge decision persists its status) and tests/unit/test_review_refuter.py::test_judge_fallback_respects_cap (with _MAX_JUDGED judged-type findings already queued, a deferred mechanical finding does not extend the list beyond _MAX_JUDGED).",
      "commit_message": "feat(review): deferred mechanical findings fall back to the judge",
      "done_when": "pytest tests/unit/test_review_refuter.py::test_mechanical_deferral_falls_back_to_judge tests/unit/test_review_refuter.py::test_judge_fallback_respects_cap passes",
      "unit_tests": [
        "tests/unit/test_review_refuter.py::test_mechanical_deferral_falls_back_to_judge",
        "tests/unit/test_review_refuter.py::test_judge_fallback_respects_cap"
      ]
    },
    {
      "index": 4,
      "title": "Pure assessor and dossier builder in stuck.py",
      "files": [
        "src/ferova/review/stuck.py",
        "tests/unit/test_stuck.py"
      ],
      "action": "In src/ferova/review/stuck.py add PROPOSED_ESCALATION_ATTEMPTS: int = 2 (module constant), a pure assess_proposed_escalation(findings, *, max_attempts=PROPOSED_ESCALATION_ATTEMPTS) returning the blocking findings still PROPOSED whose verify_attempts >= max_attempts (mirroring assess_stuck at stuck.py:173), a pure select_newly_escalated(findings, *, max_attempts=PROPOSED_ESCALATION_ATTEMPTS) returning only those whose verify_attempts == max_attempts (the just-crossed set, so a dossier fires exactly once per finding across successive runs), and build_proposed_escalation_dossier(findings) mirroring build_stuck_dossier (stuck.py:243) with kind='proposed_escalation' and, per finding: id, claim, claim_type, verify_attempts, and the deferral diagnostic (verification_method + verification_result). Add the unit tests tests/unit/test_stuck.py::test_assess_proposed_escalation_thresholds (attempts 1 -> empty, attempts 2 -> returned, non-blocking or non-proposed -> excluded), tests/unit/test_stuck.py::test_proposed_escalation_dossier_shape (dossier carries kind, ids, claims, attempt counts and deferral reasons) and tests/unit/test_stuck.py::test_proposed_escalation_fires_once (select_newly_escalated returns a finding at exactly max_attempts and excludes it at max_attempts + 1).",
      "commit_message": "feat(review): proposed-escalation assessor and dossier builder",
      "done_when": "pytest tests/unit/test_stuck.py::test_assess_proposed_escalation_thresholds tests/unit/test_stuck.py::test_proposed_escalation_dossier_shape tests/unit/test_stuck.py::test_proposed_escalation_fires_once passes",
      "unit_tests": [
        "tests/unit/test_stuck.py::test_assess_proposed_escalation_thresholds",
        "tests/unit/test_stuck.py::test_proposed_escalation_dossier_shape",
        "tests/unit/test_stuck.py::test_proposed_escalation_fires_once"
      ]
    },
    {
      "index": 5,
      "title": "Orchestrator fires the dossier; end-to-end integration test",
      "files": [
        "src/ferova/review/orchestrator.py",
        "tests/unit/test_stuck.py",
        "tests/integration/test_proposed_escalation.py"
      ],
      "action": "In src/ferova/review/orchestrator.py, after the verify+judge passes complete for a review run, fetch the PR's findings and compute select_newly_escalated(...); when non-empty, build build_proposed_escalation_dossier(...) and fire it through the same settings-guarded routine seam the stuck dossier uses (the _fire_routine pathway at orchestrator.py:832 — reuse its guard so no routine secrets means no-op, never an error). Add the unit test tests/unit/test_stuck.py::test_orchestrator_escalation_seam_is_guarded (with routine settings absent, firing the proposed-escalation dossier is a silent no-op — monkeypatched transport never called). Create tests/integration/test_proposed_escalation.py with test_frozen_proposed_finding_escalates_end_to_end: in a tmp repo and tmp ledger, record one BLOCKING missing_test finding whose promised symbol resolves nowhere; call verify_findings_for_pr once and assert verify_attempts == 1 with the diagnostic persisted and assess_proposed_escalation empty; call verify_findings_for_pr again and assert verify_attempts == 2, assess_proposed_escalation returns exactly this finding, select_newly_escalated returns it (crossing), and build_proposed_escalation_dossier renders kind='proposed_escalation' with the claim and 'no checkable symbol'. Hermetic: no network, no LLM (the judge fallback is NOT invoked here — unit-tested with stubs in step 3), no reliance on a .env file.",
      "commit_message": "feat(review): fire the proposed-escalation dossier from the orchestrator",
      "done_when": "pytest tests/unit/test_stuck.py::test_orchestrator_escalation_seam_is_guarded tests/integration/test_proposed_escalation.py::test_frozen_proposed_finding_escalates_end_to_end passes",
      "unit_tests": [
        "tests/unit/test_stuck.py::test_orchestrator_escalation_seam_is_guarded"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_proposed_escalation.py::test_frozen_proposed_finding_escalates_end_to_end"
  ]
}
```
