# SP-CI-FINDINGS-WIRE — Resolve stale CI findings on green and pin the materialiser's wiring

Exploration of the current codebase shows record_ci_failures_as_findings is already called from run_coder_fix_from_findings (coder_findings.py:468-475), and summarise_ledger_facts already derives ci_green from the ledger — the core G1/G2/G3 wiring the spec describes as missing already exists and is covered by tests/unit/test_coder_findings.py::test_run_from_findings_materializes_ci_red. The remaining, concrete gap is that resolve_broken_behavior_findings is only invoked deep inside the post-push success path, so a broken_behavior finding left OPEN from an earlier round (Coder failed to push, or someone fixed CI outside the bot) is never resolved once checks turn green on a later round where the Coder has no push to make, permanently misreporting ci_green=False. This plan closes that gap, adds the explicit AC5 regression guard the postmortem calls for, and adds an integration test pinning the red-to-blocked-to-green-to-merge-ready path end to end through the ledger and merge gate.

## Step 1 — Resolve stale broken_behavior findings when CI is already green

- **Files**: `src/ferova/review/coder_findings.py`, `tests/unit/test_coder_findings.py`
- **Action**: In src/ferova/review/coder_findings.py:run_coder_fix_from_findings, add CI_GREEN to the existing local `from .coder_loop import (CI_RED, apply_fixes, fetch_ci_status, fetch_failed_check_logs, git_commit_and_push, persist_placeholder_rejected, run_pytest_matrix, run_ruff_gate)` block (alphabetically before CI_RED). Immediately after the existing `if ci_state == CI_RED and failed_rows: record_ci_failures_as_findings(...)` block (around line 468-475), add `elif ci_state == CI_GREEN: resolve_broken_behavior_findings(db, pr_number=pr_number, head_sha=head_sha)` — resolve_broken_behavior_findings is already defined in this same module, no new import needed. This mirrors Behavior item 3 of the spec ('if all required checks are green at the current head: resolve open broken_behavior CI findings') at the top of the function, not only after this run's own push succeeds (the current code only calls resolve_broken_behavior_findings at line 634, inside the post-push branch, so a finding left OPEN from a prior round that never got fixed by the bot, or was fixed by someone else, is orphaned open forever once CI is actually green). In tests/unit/test_coder_findings.py add test_run_from_findings_resolves_stale_ci_finding_when_ci_turns_green: seed one Finding via the existing `_finding` helper with claim_type=ClaimType.BROKEN_BEHAVIOR, status=FindingStatus.OPEN, file='(ci):Test suite', claim='CI check failed: Test suite', record it with record_finding; monkeypatch coder_loop.fetch_ci_status to `lambda *a, **k: (coder_loop.CI_GREEN, [])`; call `run_coder_fix_from_findings(1, gh=_gh_mock(), repo_root=tmp_path, db_path=db)`; assert the finding fetched back via fetch_findings is FindingStatus.RESOLVED and `res.no_op_reason == 'no open blocking findings to resolve'`.
- **Commit**: `fix(review): resolve broken_behavior findings when CI is already green`
- **Done when**: pytest tests/unit/test_coder_findings.py::test_run_from_findings_resolves_stale_ci_finding_when_ci_turns_green passes and ruff check src/ferova/review/coder_findings.py exits 0
- **Unit tests**: `tests/unit/test_coder_findings.py::test_run_from_findings_resolves_stale_ci_finding_when_ci_turns_green`

## Step 2 — Add the AC5 regression guard for the CI-materialiser wiring

- **Files**: `tests/unit/test_coder_findings.py`
- **Action**: In tests/unit/test_coder_findings.py, add `import inspect` to the imports and a new test test_run_from_findings_still_calls_ci_materialiser_and_resolver that does `source = inspect.getsource(run_coder_fix_from_findings)` (run_coder_fix_from_findings is already imported in this file) and asserts both `'record_ci_failures_as_findings' in source` and `'resolve_broken_behavior_findings' in source`. This is the spec's G4/AC5 guard: SP-CI-FINDINGS-WIRE exists specifically because record_ci_failures_as_findings was implemented with zero callers and nobody noticed; this static source-level assertion fails immediately and loudly if either call is ever deleted from the coder-loop entry path again, independent of whether other behavioral tests around it are weakened or refactored at the same time.
- **Commit**: `test(review): guard the CI-materialiser/resolver wiring against regressing`
- **Done when**: pytest tests/unit/test_coder_findings.py::test_run_from_findings_still_calls_ci_materialiser_and_resolver passes
- **Unit tests**: `tests/unit/test_coder_findings.py::test_run_from_findings_still_calls_ci_materialiser_and_resolver`

## Step 3 — Add an end-to-end ledger/merge-gate integration test for the red-CI path

- **Files**: `tests/integration/test_ci_findings_wire.py`
- **Action**: Create tests/integration/test_ci_findings_wire.py, following the style of the existing tests/integration/test_findings_bridge.py (module docstring, plain function calls against a tmp_path db, no gh/network mocking). Add test_red_ci_finding_flips_ledger_to_blocked_then_resolves_to_merge_ready: (1) call `record_ci_failures_as_findings(db, pr_number=53, head_sha='shaA', failed_rows=[{'name': 'Test suite', 'link': 'https://x/runs/1/job/2'}])`; (2) call `facts = summarise_ledger_facts(db, pr_number=53, head_sha='shaA')`, assert `facts.ci_green is False`, assert `compute_merge_decision(facts).merge is False`, then `body = render_ledger_report(db, pr_number=53, decision=compute_merge_decision(facts), facts=facts)` and assert `'Decision: BLOCKED'` and `'| CI green | False |'` are both in body (AC1/AC2); (3) call `open_verified_blocking(db, 53, head_sha='shaA')` to mirror the real coder-loop sequence (VERIFIED -> OPEN); (4) call `resolved = resolve_broken_behavior_findings(db, pr_number=53, head_sha='shaB')` and assert `resolved == 1`; (5) recompute `facts2 = summarise_ledger_facts(db, pr_number=53, head_sha='shaB')` and assert `facts2.ci_green is True` and `facts2.open_blocking_findings == 0` (AC4). Import `record_ci_failures_as_findings, open_verified_blocking, resolve_broken_behavior_findings` from `ferova.review.coder_findings`, `compute_merge_decision, summarise_ledger_facts` from `ferova.review.merge_gate`, and `render_ledger_report` from `ferova.review.report`.
- **Commit**: `test(review): pin red-CI-to-ledger-to-green integration path`
- **Done when**: pytest tests/integration/test_ci_findings_wire.py::test_red_ci_finding_flips_ledger_to_blocked_then_resolves_to_merge_ready passes
- **Unit tests**: `tests/integration/test_ci_findings_wire.py::test_red_ci_finding_flips_ledger_to_blocked_then_resolves_to_merge_ready`

## Integration tests

- `tests/integration/test_ci_findings_wire.py::test_red_ci_finding_flips_ledger_to_blocked_then_resolves_to_merge_ready`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-CI-FINDINGS-WIRE",
  "title": "Resolve stale CI findings on green and pin the materialiser's wiring",
  "summary": "Exploration of the current codebase shows record_ci_failures_as_findings is already called from run_coder_fix_from_findings (coder_findings.py:468-475), and summarise_ledger_facts already derives ci_green from the ledger — the core G1/G2/G3 wiring the spec describes as missing already exists and is covered by tests/unit/test_coder_findings.py::test_run_from_findings_materializes_ci_red. The remaining, concrete gap is that resolve_broken_behavior_findings is only invoked deep inside the post-push success path, so a broken_behavior finding left OPEN from an earlier round (Coder failed to push, or someone fixed CI outside the bot) is never resolved once checks turn green on a later round where the Coder has no push to make, permanently misreporting ci_green=False. This plan closes that gap, adds the explicit AC5 regression guard the postmortem calls for, and adds an integration test pinning the red-to-blocked-to-green-to-merge-ready path end to end through the ledger and merge gate.",
  "steps": [
    {
      "index": 1,
      "title": "Resolve stale broken_behavior findings when CI is already green",
      "files": [
        "src/ferova/review/coder_findings.py",
        "tests/unit/test_coder_findings.py"
      ],
      "action": "In src/ferova/review/coder_findings.py:run_coder_fix_from_findings, add CI_GREEN to the existing local `from .coder_loop import (CI_RED, apply_fixes, fetch_ci_status, fetch_failed_check_logs, git_commit_and_push, persist_placeholder_rejected, run_pytest_matrix, run_ruff_gate)` block (alphabetically before CI_RED). Immediately after the existing `if ci_state == CI_RED and failed_rows: record_ci_failures_as_findings(...)` block (around line 468-475), add `elif ci_state == CI_GREEN: resolve_broken_behavior_findings(db, pr_number=pr_number, head_sha=head_sha)` — resolve_broken_behavior_findings is already defined in this same module, no new import needed. This mirrors Behavior item 3 of the spec ('if all required checks are green at the current head: resolve open broken_behavior CI findings') at the top of the function, not only after this run's own push succeeds (the current code only calls resolve_broken_behavior_findings at line 634, inside the post-push branch, so a finding left OPEN from a prior round that never got fixed by the bot, or was fixed by someone else, is orphaned open forever once CI is actually green). In tests/unit/test_coder_findings.py add test_run_from_findings_resolves_stale_ci_finding_when_ci_turns_green: seed one Finding via the existing `_finding` helper with claim_type=ClaimType.BROKEN_BEHAVIOR, status=FindingStatus.OPEN, file='(ci):Test suite', claim='CI check failed: Test suite', record it with record_finding; monkeypatch coder_loop.fetch_ci_status to `lambda *a, **k: (coder_loop.CI_GREEN, [])`; call `run_coder_fix_from_findings(1, gh=_gh_mock(), repo_root=tmp_path, db_path=db)`; assert the finding fetched back via fetch_findings is FindingStatus.RESOLVED and `res.no_op_reason == 'no open blocking findings to resolve'`.",
      "commit_message": "fix(review): resolve broken_behavior findings when CI is already green",
      "done_when": "pytest tests/unit/test_coder_findings.py::test_run_from_findings_resolves_stale_ci_finding_when_ci_turns_green passes and ruff check src/ferova/review/coder_findings.py exits 0",
      "unit_tests": [
        "tests/unit/test_coder_findings.py::test_run_from_findings_resolves_stale_ci_finding_when_ci_turns_green"
      ]
    },
    {
      "index": 2,
      "title": "Add the AC5 regression guard for the CI-materialiser wiring",
      "files": [
        "tests/unit/test_coder_findings.py"
      ],
      "action": "In tests/unit/test_coder_findings.py, add `import inspect` to the imports and a new test test_run_from_findings_still_calls_ci_materialiser_and_resolver that does `source = inspect.getsource(run_coder_fix_from_findings)` (run_coder_fix_from_findings is already imported in this file) and asserts both `'record_ci_failures_as_findings' in source` and `'resolve_broken_behavior_findings' in source`. This is the spec's G4/AC5 guard: SP-CI-FINDINGS-WIRE exists specifically because record_ci_failures_as_findings was implemented with zero callers and nobody noticed; this static source-level assertion fails immediately and loudly if either call is ever deleted from the coder-loop entry path again, independent of whether other behavioral tests around it are weakened or refactored at the same time.",
      "commit_message": "test(review): guard the CI-materialiser/resolver wiring against regressing",
      "done_when": "pytest tests/unit/test_coder_findings.py::test_run_from_findings_still_calls_ci_materialiser_and_resolver passes",
      "unit_tests": [
        "tests/unit/test_coder_findings.py::test_run_from_findings_still_calls_ci_materialiser_and_resolver"
      ]
    },
    {
      "index": 3,
      "title": "Add an end-to-end ledger/merge-gate integration test for the red-CI path",
      "files": [
        "tests/integration/test_ci_findings_wire.py"
      ],
      "action": "Create tests/integration/test_ci_findings_wire.py, following the style of the existing tests/integration/test_findings_bridge.py (module docstring, plain function calls against a tmp_path db, no gh/network mocking). Add test_red_ci_finding_flips_ledger_to_blocked_then_resolves_to_merge_ready: (1) call `record_ci_failures_as_findings(db, pr_number=53, head_sha='shaA', failed_rows=[{'name': 'Test suite', 'link': 'https://x/runs/1/job/2'}])`; (2) call `facts = summarise_ledger_facts(db, pr_number=53, head_sha='shaA')`, assert `facts.ci_green is False`, assert `compute_merge_decision(facts).merge is False`, then `body = render_ledger_report(db, pr_number=53, decision=compute_merge_decision(facts), facts=facts)` and assert `'Decision: BLOCKED'` and `'| CI green | False |'` are both in body (AC1/AC2); (3) call `open_verified_blocking(db, 53, head_sha='shaA')` to mirror the real coder-loop sequence (VERIFIED -> OPEN); (4) call `resolved = resolve_broken_behavior_findings(db, pr_number=53, head_sha='shaB')` and assert `resolved == 1`; (5) recompute `facts2 = summarise_ledger_facts(db, pr_number=53, head_sha='shaB')` and assert `facts2.ci_green is True` and `facts2.open_blocking_findings == 0` (AC4). Import `record_ci_failures_as_findings, open_verified_blocking, resolve_broken_behavior_findings` from `ferova.review.coder_findings`, `compute_merge_decision, summarise_ledger_facts` from `ferova.review.merge_gate`, and `render_ledger_report` from `ferova.review.report`.",
      "commit_message": "test(review): pin red-CI-to-ledger-to-green integration path",
      "done_when": "pytest tests/integration/test_ci_findings_wire.py::test_red_ci_finding_flips_ledger_to_blocked_then_resolves_to_merge_ready passes",
      "unit_tests": [
        "tests/integration/test_ci_findings_wire.py::test_red_ci_finding_flips_ledger_to_blocked_then_resolves_to_merge_ready"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_ci_findings_wire.py::test_red_ci_finding_flips_ledger_to_blocked_then_resolves_to_merge_ready"
  ]
}
```
