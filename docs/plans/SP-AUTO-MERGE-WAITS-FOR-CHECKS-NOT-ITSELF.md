# SP-AUTO-MERGE-WAITS-FOR-CHECKS-NOT-ITSELF — Auto-merge waits for the named required checks, never for itself

The record of the delivered implementation (PR #265): the acceptance contract is the spec's Acceptance Criteria selectors, AC11 excepted (it belongs to SP-MERGE-QUEUE-BY-SPEC-DEPENDENCIES).

## Step 1 — Judge the named required checks at the head

- **Files**: `src/repoach/review/merge_wait.py`, `src/repoach/forge/github.py`, `tests/unit/test_merge_wait_required_checks.py`
- **Action**: Add merge_wait.judge_required_checks over the committed list (required_checks: [auto_merge].required_checks, else the CI workflow's job names, matrix expanded). Per name the latest run at the head that is not CANCELLED decides; a name with only cancelled runs is cancelled. GitHubForge.merge stops reading the rollup to refuse as pending; BLOCKED and UNSTABLE alone merge, BEHIND still refuses.
- **Commit**: `feat(merge): wait for the named required checks, record every attempt`
- **Done when**: `pytest -q tests/unit/test_merge_wait_required_checks.py` passes (AC1, AC3, AC4).
- **Unit tests**: `tests/unit/test_merge_wait_required_checks.py::test_own_run_pending_merges`, `tests/unit/test_merge_wait_required_checks.py::test_cancelled_placeholder_ignored`, `tests/unit/test_merge_wait_required_checks.py::test_real_red_not_merged`

## Step 2 — Skip a pending required check as re-armed, never as a timeout

- **Files**: `src/repoach/review/auto_merge.py`, `tests/unit/test_merge_wait_rearm.py`, `tests/integration/test_automerge_fail_fast_gate.py`
- **Action**: In run_auto_merge, a pending required check at wait 0 is SKIP_CI_PENDING with notes naming the check and the workflow whose completion re-runs the merge; SKIP_CI_CANCELLED joins the non-fatal skips (exit 5). The fixture forge emits the workflow_run CI-completed event, merge-on-ci's job if admits it, and the second invocation merges.
- **Commit**: `feat(merge): wait for the named required checks, record every attempt`
- **Done when**: `pytest -q tests/unit/test_merge_wait_rearm.py` passes (AC2, AC12 replay).
- **Unit tests**: `tests/unit/test_merge_wait_rearm.py::test_pending_required_check_rearms_then_merges`

## Step 3 — Re-arm through both workflows and coalesce owner bursts

- **Files**: `.github/workflows/merge-on-ci.yml`, `.github/workflows/auto-review.yml`, `tests/unit/test_merge_wait_workflows.py`
- **Action**: merge-on-ci fires on the completion of CI and of the review workflow and on workflow_dispatch with input pr. auto-review loses its workflow-level concurrency; a coalesce job on ubuntu-latest sleeps 60 s with cancel-in-progress, review needs it, and auto_fix / auto_merge share merge-<head branch> without cancelling. Every required check must be produced by a re-arming workflow.
- **Commit**: `ci(merge): re-arm on review completion and coalesce owner bursts`
- **Done when**: `pytest -q tests/unit/test_merge_wait_workflows.py` passes (AC5, AC6, AC7).
- **Unit tests**: `tests/unit/test_merge_wait_workflows.py::test_fourteen_review_events_coalesce`, `tests/unit/test_merge_wait_workflows.py::test_workflow_shape`, `tests/unit/test_merge_wait_workflows.py::test_every_required_check_is_rearmed`

## Step 4 — Record every attempt in pr_merges and the attempts comment

- **Files**: `src/repoach/review/persistence.py`, `src/repoach/cli/review_cmds.py`, `tests/unit/test_merge_attempts_ledger.py`
- **Action**: pr_merges gains head_sha, pending_checks, trigger and rule_version; each attempt is upserted into the repoach-merge-attempts comment through the write barrier. repoach review merge-attempts prints one JSON object on stdout and the attempts line on stderr.
- **Commit**: `feat(cli): print merge attempts and stranded prs, check re-arming`
- **Done when**: `pytest -q tests/unit/test_merge_attempts_ledger.py` passes (AC8, AC9).
- **Unit tests**: `tests/unit/test_merge_attempts_ledger.py::test_row_shape`, `tests/unit/test_merge_attempts_ledger.py::test_cli_attempts_line`

## Step 5 — List stranded PRs and dispatch the release sweep

- **Files**: `src/repoach/review/merge_wait.py`, `.github/workflows/release-verify.yml`, `tests/unit/test_merge_wait_stranded.py`
- **Action**: merge_wait.stranded lists open non-draft PRs green since t with no attempt at that head after t + grace; repoach review stranded prints them, --dispatch starts merge-on-ci.yml once per PR through the write barrier, --dry-run writes nothing. release-verify.yml runs the sweep after a release.
- **Commit**: `feat(cli): print merge attempts and stranded prs, check re-arming`
- **Done when**: `pytest -q tests/unit/test_merge_wait_stranded.py` passes (AC10, AC13).
- **Unit tests**: `tests/unit/test_merge_wait_stranded.py::test_stranded_listed`, `tests/unit/test_merge_wait_stranded.py::test_dispatch_sweep`

## Integration tests

- `tests/integration/test_automerge_fail_fast_gate.py`

<!-- repoach-action-plan -->
```json
{
  "spec_id": "SP-AUTO-MERGE-WAITS-FOR-CHECKS-NOT-ITSELF",
  "title": "Auto-merge waits for the named required checks, never for itself",
  "summary": "The record of the delivered implementation (PR #265): the acceptance contract is the spec's Acceptance Criteria selectors, AC11 excepted (it belongs to SP-MERGE-QUEUE-BY-SPEC-DEPENDENCIES).",
  "steps": [
    {
      "index": 1,
      "title": "Judge the named required checks at the head",
      "files": [
        "src/repoach/review/merge_wait.py",
        "src/repoach/forge/github.py",
        "tests/unit/test_merge_wait_required_checks.py"
      ],
      "action": "Add merge_wait.judge_required_checks over the committed list (required_checks: [auto_merge].required_checks, else the CI workflow's job names, matrix expanded). Per name the latest run at the head that is not CANCELLED decides; a name with only cancelled runs is cancelled. GitHubForge.merge stops reading the rollup to refuse as pending; BLOCKED and UNSTABLE alone merge, BEHIND still refuses.",
      "commit_message": "feat(merge): wait for the named required checks, record every attempt",
      "done_when": "`pytest -q tests/unit/test_merge_wait_required_checks.py` passes (AC1, AC3, AC4).",
      "unit_tests": [
        "tests/unit/test_merge_wait_required_checks.py::test_own_run_pending_merges",
        "tests/unit/test_merge_wait_required_checks.py::test_cancelled_placeholder_ignored",
        "tests/unit/test_merge_wait_required_checks.py::test_real_red_not_merged"
      ]
    },
    {
      "index": 2,
      "title": "Skip a pending required check as re-armed, never as a timeout",
      "files": [
        "src/repoach/review/auto_merge.py",
        "tests/unit/test_merge_wait_rearm.py",
        "tests/integration/test_automerge_fail_fast_gate.py"
      ],
      "action": "In run_auto_merge, a pending required check at wait 0 is SKIP_CI_PENDING with notes naming the check and the workflow whose completion re-runs the merge; SKIP_CI_CANCELLED joins the non-fatal skips (exit 5). The fixture forge emits the workflow_run CI-completed event, merge-on-ci's job if admits it, and the second invocation merges.",
      "commit_message": "feat(merge): wait for the named required checks, record every attempt",
      "done_when": "`pytest -q tests/unit/test_merge_wait_rearm.py` passes (AC2, AC12 replay).",
      "unit_tests": [
        "tests/unit/test_merge_wait_rearm.py::test_pending_required_check_rearms_then_merges"
      ]
    },
    {
      "index": 3,
      "title": "Re-arm through both workflows and coalesce owner bursts",
      "files": [
        ".github/workflows/merge-on-ci.yml",
        ".github/workflows/auto-review.yml",
        "tests/unit/test_merge_wait_workflows.py"
      ],
      "action": "merge-on-ci fires on the completion of CI and of the review workflow and on workflow_dispatch with input pr. auto-review loses its workflow-level concurrency; a coalesce job on ubuntu-latest sleeps 60 s with cancel-in-progress, review needs it, and auto_fix / auto_merge share merge-<head branch> without cancelling. Every required check must be produced by a re-arming workflow.",
      "commit_message": "ci(merge): re-arm on review completion and coalesce owner bursts",
      "done_when": "`pytest -q tests/unit/test_merge_wait_workflows.py` passes (AC5, AC6, AC7).",
      "unit_tests": [
        "tests/unit/test_merge_wait_workflows.py::test_fourteen_review_events_coalesce",
        "tests/unit/test_merge_wait_workflows.py::test_workflow_shape",
        "tests/unit/test_merge_wait_workflows.py::test_every_required_check_is_rearmed"
      ]
    },
    {
      "index": 4,
      "title": "Record every attempt in pr_merges and the attempts comment",
      "files": [
        "src/repoach/review/persistence.py",
        "src/repoach/cli/review_cmds.py",
        "tests/unit/test_merge_attempts_ledger.py"
      ],
      "action": "pr_merges gains head_sha, pending_checks, trigger and rule_version; each attempt is upserted into the repoach-merge-attempts comment through the write barrier. repoach review merge-attempts prints one JSON object on stdout and the attempts line on stderr.",
      "commit_message": "feat(cli): print merge attempts and stranded prs, check re-arming",
      "done_when": "`pytest -q tests/unit/test_merge_attempts_ledger.py` passes (AC8, AC9).",
      "unit_tests": [
        "tests/unit/test_merge_attempts_ledger.py::test_row_shape",
        "tests/unit/test_merge_attempts_ledger.py::test_cli_attempts_line"
      ]
    },
    {
      "index": 5,
      "title": "List stranded PRs and dispatch the release sweep",
      "files": [
        "src/repoach/review/merge_wait.py",
        ".github/workflows/release-verify.yml",
        "tests/unit/test_merge_wait_stranded.py"
      ],
      "action": "merge_wait.stranded lists open non-draft PRs green since t with no attempt at that head after t + grace; repoach review stranded prints them, --dispatch starts merge-on-ci.yml once per PR through the write barrier, --dry-run writes nothing. release-verify.yml runs the sweep after a release.",
      "commit_message": "feat(cli): print merge attempts and stranded prs, check re-arming",
      "done_when": "`pytest -q tests/unit/test_merge_wait_stranded.py` passes (AC10, AC13).",
      "unit_tests": [
        "tests/unit/test_merge_wait_stranded.py::test_stranded_listed",
        "tests/unit/test_merge_wait_stranded.py::test_dispatch_sweep"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_automerge_fail_fast_gate.py"
  ]
}
```
