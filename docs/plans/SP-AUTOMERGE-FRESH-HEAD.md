# SP-AUTOMERGE-FRESH-HEAD — Merge paths verify the API-served head against git ls-remote

Every merge-side head resolution gains a bounded API-vs-ls-remote convergence check that fails closed: a new resolve_verified_head helper (step 1), wired into run_auto_merge with a new OUTCOME_SKIP_STALE_HEAD and a last-second tip re-read before squash (step 2), the same verification in evaluate_merge_gate with default-stubbed fixtures keeping existing suites green (step 3), and a shell-side guard in safe_merge.sh plus the end-to-end integration test (step 4). Hand-authored after a Planner session exhausted five attempts on plan-form rules.

## Step 1 — resolve_verified_head: bounded ls-remote convergence, fail-closed

- **Files**: `src/ferova/review/auto_merge.py`, `tests/unit/test_automerge_fresh_head.py`
- **Action**: In src/ferova/review/auto_merge.py add resolve_verified_head(gh, pr_number, head_ref, *, repo_root, attempts=4, delay_s=30.0, sleep=time.sleep) -> tuple[str | None, str]: resolves the real branch tip via gh._run_git(["ls-remote", "origin", f"refs/heads/{head_ref}"]) (first whitespace-split token of the first stdout line), then compares gh.pr_head_sha(pr_number) against it, re-polling up to `attempts` times with `sleep(delay_s)` between polls until they agree; returns (converged_sha, "") on agreement. Fail-closed contract: on persistent mismatch returns (None, reason) where the reason carries BOTH 12-char SHA prefixes (api=..., ls_remote=...); when ls-remote itself fails (non-zero returncode or blank stdout) returns (None, reason) with the git error. Never raises for these paths; the injectable sleep keeps tests instant. Create tests/unit/test_automerge_fresh_head.py (module docstring citing SP-AUTOMERGE-FRESH-HEAD; stub gh via MagicMock like tests/unit/test_review_auto_merge.py) with tests/unit/test_automerge_fresh_head.py::test_verified_head_match_first_try (API == ls-remote on first call, no sleep called), tests/unit/test_automerge_fresh_head.py::test_verified_head_converges_after_repoll (API stale once then fresh; sleep called once; converged SHA returned), tests/unit/test_automerge_fresh_head.py::test_verified_head_persistent_mismatch_fails_closed (always different; returns None and the reason contains both 12-char prefixes), and tests/unit/test_automerge_fresh_head.py::test_verified_head_ls_remote_error_fails_closed (ls-remote returncode 1; returns None with the git error in the reason).
- **Commit**: `feat(review): bounded fail-closed verified-head resolution`
- **Done when**: pytest tests/unit/test_automerge_fresh_head.py::test_verified_head_match_first_try tests/unit/test_automerge_fresh_head.py::test_verified_head_converges_after_repoll tests/unit/test_automerge_fresh_head.py::test_verified_head_persistent_mismatch_fails_closed tests/unit/test_automerge_fresh_head.py::test_verified_head_ls_remote_error_fails_closed passes
- **Unit tests**: `tests/unit/test_automerge_fresh_head.py::test_verified_head_match_first_try`, `tests/unit/test_automerge_fresh_head.py::test_verified_head_converges_after_repoll`, `tests/unit/test_automerge_fresh_head.py::test_verified_head_persistent_mismatch_fails_closed`, `tests/unit/test_automerge_fresh_head.py::test_verified_head_ls_remote_error_fails_closed`

## Step 2 — run_auto_merge refuses stale heads and re-reads the tip before squash

- **Files**: `src/ferova/review/auto_merge.py`, `tests/unit/test_automerge_fresh_head.py`
- **Action**: In src/ferova/review/auto_merge.py add OUTCOME_SKIP_STALE_HEAD: str = "SKIP_STALE_HEAD" next to the existing OUTCOME_* constants (auto_merge.py:77-82). In run_auto_merge, before the CI gate: call resolve_verified_head; when it returns None, persist the outcome to pr_merges with the reason (both SHA prefixes) in notes via the existing recording path, log a warning auto_merge.stale_head, and return WITHOUT calling squash_merge. When it returns a SHA, thread it through: evaluate_ci_gate receives the verified head for its check matching, and decide_at_head gains an optional head_sha: str | None = None override (default None keeps today's gh.pr_head_sha behaviour for other callers) so gate facts are computed at the exact verified SHA. Immediately before the squash_merge call, re-read the remote tip once via the same ls-remote path: if the branch moved after the gate decision, refuse with OUTCOME_SKIP_STALE_HEAD exactly as above instead of merging. Add tests/unit/test_automerge_fresh_head.py::test_auto_merge_refuses_on_stale_head_and_does_not_merge (persistent mismatch → outcome SKIP_STALE_HEAD persisted with both SHAs in notes, squash_merge mock never called), tests/unit/test_automerge_fresh_head.py::test_gate_facts_computed_at_verified_head (decide_at_head receives head_sha == the converged SHA, not the raw API value), and tests/unit/test_automerge_fresh_head.py::test_auto_merge_refuses_when_head_moves_mid_gate (verification converges, gates pass, but the pre-squash re-read returns a new tip → refusal, squash_merge never called).
- **Commit**: `feat(review): auto-merge refuses stale heads, re-reads tip before squash`
- **Done when**: pytest tests/unit/test_automerge_fresh_head.py::test_auto_merge_refuses_on_stale_head_and_does_not_merge tests/unit/test_automerge_fresh_head.py::test_gate_facts_computed_at_verified_head tests/unit/test_automerge_fresh_head.py::test_auto_merge_refuses_when_head_moves_mid_gate passes
- **Unit tests**: `tests/unit/test_automerge_fresh_head.py::test_auto_merge_refuses_on_stale_head_and_does_not_merge`, `tests/unit/test_automerge_fresh_head.py::test_gate_facts_computed_at_verified_head`, `tests/unit/test_automerge_fresh_head.py::test_auto_merge_refuses_when_head_moves_mid_gate`

## Step 3 — evaluate_merge_gate verifies too; existing suites stay green

- **Files**: `src/ferova/review/auto_merge.py`, `tests/unit/test_automerge_fresh_head.py`, `tests/unit/test_review_auto_merge.py`
- **Action**: In evaluate_merge_gate (auto_merge.py:468), perform the same resolve_verified_head verification before decide_at_head: a stale or unverifiable head yields a decision with merge == False and a reason string containing 'stale head' plus both 12-char SHA prefixes, so `ferova review gate` exits 5 through the existing exit-code mapping (review_cmds.py:308-363, no CLI change needed). Feed the verified SHA into decide_at_head via the head_sha override from step 2. Update tests/unit/test_review_auto_merge.py fixtures so head verification is stubbed to an immediate match by default (one shared monkeypatch/fixture at module or conftest level — the existing scenarios must not start sleeping or failing), keeping the whole existing file green unchanged in its assertions. Add tests/unit/test_automerge_fresh_head.py::test_evaluate_merge_gate_stale_head_refuses (persistent mismatch → evaluation.decision.merge is False and a reason contains 'stale head' and both prefixes).
- **Commit**: `feat(review): merge gate fails closed on stale heads`
- **Done when**: pytest tests/unit/test_automerge_fresh_head.py::test_evaluate_merge_gate_stale_head_refuses tests/unit/test_review_auto_merge.py passes
- **Unit tests**: `tests/unit/test_automerge_fresh_head.py::test_evaluate_merge_gate_stale_head_refuses`

## Step 4 — safe_merge.sh guard + end-to-end integration test

- **Files**: `scripts/safe_merge.sh`, `tests/unit/test_automerge_fresh_head.py`, `tests/integration/test_automerge_fresh_head_end_to_end.py`
- **Action**: In scripts/safe_merge.sh, between the gate step and the `gh pr merge` invocation (steps 5 and 6 in the header comment), add a fresh-head guard: resolve `api_head=$(gh pr view "$PR" --json headRefOid -q .headRefOid)` and `remote_head=$(git ls-remote origin "refs/heads/$head_ref" | awk '{print $1}')`; on mismatch print both SHAs and abort with a non-zero exit — explicitly WITHOUT offering the emergency-override prompt used by other refusals (stale data is not overridable); keep shellcheck clean. Add tests/unit/test_automerge_fresh_head.py::test_safe_merge_script_contains_fresh_head_guard: read scripts/safe_merge.sh as text and assert (a) it contains an `ls-remote` comparison against `headRefOid`, (b) the guard text appears AFTER the `review gate` invocation and BEFORE the `gh pr merge` line (compare str.index positions), and (c) the override prompt marker used elsewhere in the script does not appear between the guard and the merge line. Create tests/integration/test_automerge_fresh_head_end_to_end.py with test_stale_head_refused_end_to_end: hermetic throwaway setup (tmp_path bare origin + clone, real git; gh stubbed via MagicMock whose pr_head_sha returns a SHA that never matches the real ls-remote tip and whose _run_git delegates to the real git in the clone via GhCli(cwd=work_dir)._run_git); call resolve_verified_head with attempts=2, delay_s=0, assert (None, reason) with both prefixes; then flip the stub to return the real tip and assert the converged SHA equals `git rev-parse` of the branch. No network beyond the local bare repo, no .env reliance, no sleeps (injected no-op).
- **Commit**: `feat(review): safe_merge fresh-head guard + end-to-end verification test`
- **Done when**: pytest tests/unit/test_automerge_fresh_head.py::test_safe_merge_script_contains_fresh_head_guard tests/integration/test_automerge_fresh_head_end_to_end.py::test_stale_head_refused_end_to_end passes and shellcheck scripts/safe_merge.sh exits 0
- **Unit tests**: `tests/unit/test_automerge_fresh_head.py::test_safe_merge_script_contains_fresh_head_guard`

## Integration tests

- `tests/integration/test_automerge_fresh_head_end_to_end.py::test_stale_head_refused_end_to_end`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-AUTOMERGE-FRESH-HEAD",
  "title": "Merge paths verify the API-served head against git ls-remote",
  "summary": "Every merge-side head resolution gains a bounded API-vs-ls-remote convergence check that fails closed: a new resolve_verified_head helper (step 1), wired into run_auto_merge with a new OUTCOME_SKIP_STALE_HEAD and a last-second tip re-read before squash (step 2), the same verification in evaluate_merge_gate with default-stubbed fixtures keeping existing suites green (step 3), and a shell-side guard in safe_merge.sh plus the end-to-end integration test (step 4).",
  "steps": [
    {
      "index": 1,
      "title": "resolve_verified_head: bounded ls-remote convergence, fail-closed",
      "files": [
        "src/ferova/review/auto_merge.py",
        "tests/unit/test_automerge_fresh_head.py"
      ],
      "action": "In src/ferova/review/auto_merge.py add resolve_verified_head(gh, pr_number, head_ref, *, repo_root, attempts=4, delay_s=30.0, sleep=time.sleep) -> tuple[str | None, str]: resolves the real branch tip via gh._run_git([\"ls-remote\", \"origin\", f\"refs/heads/{head_ref}\"]) (first whitespace-split token of the first stdout line), then compares gh.pr_head_sha(pr_number) against it, re-polling up to `attempts` times with `sleep(delay_s)` between polls until they agree; returns (converged_sha, \"\") on agreement. Fail-closed contract: on persistent mismatch returns (None, reason) where the reason carries BOTH 12-char SHA prefixes (api=..., ls_remote=...); when ls-remote itself fails (non-zero returncode or blank stdout) returns (None, reason) with the git error. Never raises for these paths; the injectable sleep keeps tests instant. Create tests/unit/test_automerge_fresh_head.py (module docstring citing SP-AUTOMERGE-FRESH-HEAD; stub gh via MagicMock like tests/unit/test_review_auto_merge.py) with tests/unit/test_automerge_fresh_head.py::test_verified_head_match_first_try (API == ls-remote on first call, no sleep called), tests/unit/test_automerge_fresh_head.py::test_verified_head_converges_after_repoll (API stale once then fresh; sleep called once; converged SHA returned), tests/unit/test_automerge_fresh_head.py::test_verified_head_persistent_mismatch_fails_closed (always different; returns None and the reason contains both 12-char prefixes), and tests/unit/test_automerge_fresh_head.py::test_verified_head_ls_remote_error_fails_closed (ls-remote returncode 1; returns None with the git error in the reason).",
      "commit_message": "feat(review): bounded fail-closed verified-head resolution",
      "done_when": "pytest tests/unit/test_automerge_fresh_head.py::test_verified_head_match_first_try tests/unit/test_automerge_fresh_head.py::test_verified_head_converges_after_repoll tests/unit/test_automerge_fresh_head.py::test_verified_head_persistent_mismatch_fails_closed tests/unit/test_automerge_fresh_head.py::test_verified_head_ls_remote_error_fails_closed passes",
      "unit_tests": [
        "tests/unit/test_automerge_fresh_head.py::test_verified_head_match_first_try",
        "tests/unit/test_automerge_fresh_head.py::test_verified_head_converges_after_repoll",
        "tests/unit/test_automerge_fresh_head.py::test_verified_head_persistent_mismatch_fails_closed",
        "tests/unit/test_automerge_fresh_head.py::test_verified_head_ls_remote_error_fails_closed"
      ]
    },
    {
      "index": 2,
      "title": "run_auto_merge refuses stale heads and re-reads the tip before squash",
      "files": [
        "src/ferova/review/auto_merge.py",
        "tests/unit/test_automerge_fresh_head.py"
      ],
      "action": "In src/ferova/review/auto_merge.py add OUTCOME_SKIP_STALE_HEAD: str = \"SKIP_STALE_HEAD\" next to the existing OUTCOME_* constants (auto_merge.py:77-82). In run_auto_merge, before the CI gate: call resolve_verified_head; when it returns None, persist the outcome to pr_merges with the reason (both SHA prefixes) in notes via the existing recording path, log a warning auto_merge.stale_head, and return WITHOUT calling squash_merge. When it returns a SHA, thread it through: evaluate_ci_gate receives the verified head for its check matching, and decide_at_head gains an optional head_sha: str | None = None override (default None keeps today's gh.pr_head_sha behaviour for other callers) so gate facts are computed at the exact verified SHA. Immediately before the squash_merge call, re-read the remote tip once via the same ls-remote path: if the branch moved after the gate decision, refuse with OUTCOME_SKIP_STALE_HEAD exactly as above instead of merging. Add tests/unit/test_automerge_fresh_head.py::test_auto_merge_refuses_on_stale_head_and_does_not_merge (persistent mismatch -> outcome SKIP_STALE_HEAD persisted with both SHAs in notes, squash_merge mock never called), tests/unit/test_automerge_fresh_head.py::test_gate_facts_computed_at_verified_head (decide_at_head receives head_sha == the converged SHA, not the raw API value), and tests/unit/test_automerge_fresh_head.py::test_auto_merge_refuses_when_head_moves_mid_gate (verification converges, gates pass, but the pre-squash re-read returns a new tip -> refusal, squash_merge never called).",
      "commit_message": "feat(review): auto-merge refuses stale heads, re-reads tip before squash",
      "done_when": "pytest tests/unit/test_automerge_fresh_head.py::test_auto_merge_refuses_on_stale_head_and_does_not_merge tests/unit/test_automerge_fresh_head.py::test_gate_facts_computed_at_verified_head tests/unit/test_automerge_fresh_head.py::test_auto_merge_refuses_when_head_moves_mid_gate passes",
      "unit_tests": [
        "tests/unit/test_automerge_fresh_head.py::test_auto_merge_refuses_on_stale_head_and_does_not_merge",
        "tests/unit/test_automerge_fresh_head.py::test_gate_facts_computed_at_verified_head",
        "tests/unit/test_automerge_fresh_head.py::test_auto_merge_refuses_when_head_moves_mid_gate"
      ]
    },
    {
      "index": 3,
      "title": "evaluate_merge_gate verifies too; existing suites stay green",
      "files": [
        "src/ferova/review/auto_merge.py",
        "tests/unit/test_automerge_fresh_head.py",
        "tests/unit/test_review_auto_merge.py"
      ],
      "action": "In evaluate_merge_gate (auto_merge.py:468), perform the same resolve_verified_head verification before decide_at_head: a stale or unverifiable head yields a decision with merge == False and a reason string containing 'stale head' plus both 12-char SHA prefixes, so `ferova review gate` exits 5 through the existing exit-code mapping (review_cmds.py:308-363, no CLI change needed). Feed the verified SHA into decide_at_head via the head_sha override from step 2. Update tests/unit/test_review_auto_merge.py fixtures so head verification is stubbed to an immediate match by default (one shared monkeypatch/fixture at module or conftest level — the existing scenarios must not start sleeping or failing), keeping the whole existing file green unchanged in its assertions. Add tests/unit/test_automerge_fresh_head.py::test_evaluate_merge_gate_stale_head_refuses (persistent mismatch -> evaluation.decision.merge is False and a reason contains 'stale head' and both prefixes).",
      "commit_message": "feat(review): merge gate fails closed on stale heads",
      "done_when": "pytest tests/unit/test_automerge_fresh_head.py::test_evaluate_merge_gate_stale_head_refuses tests/unit/test_review_auto_merge.py passes",
      "unit_tests": [
        "tests/unit/test_automerge_fresh_head.py::test_evaluate_merge_gate_stale_head_refuses"
      ]
    },
    {
      "index": 4,
      "title": "safe_merge.sh guard + end-to-end integration test",
      "files": [
        "scripts/safe_merge.sh",
        "tests/unit/test_automerge_fresh_head.py",
        "tests/integration/test_automerge_fresh_head_end_to_end.py"
      ],
      "action": "In scripts/safe_merge.sh, between the gate step and the `gh pr merge` invocation (steps 5 and 6 in the header comment), add a fresh-head guard: resolve `api_head=$(gh pr view \"$PR\" --json headRefOid -q .headRefOid)` and `remote_head=$(git ls-remote origin \"refs/heads/$head_ref\" | awk '{print $1}')`; on mismatch print both SHAs and abort with a non-zero exit — explicitly WITHOUT offering the emergency-override prompt used by other refusals (stale data is not overridable); keep shellcheck clean. Add tests/unit/test_automerge_fresh_head.py::test_safe_merge_script_contains_fresh_head_guard: read scripts/safe_merge.sh as text and assert (a) it contains an `ls-remote` comparison against `headRefOid`, (b) the guard text appears AFTER the `review gate` invocation and BEFORE the `gh pr merge` line (compare str.index positions), and (c) the override prompt marker used elsewhere in the script does not appear between the guard and the merge line. Create tests/integration/test_automerge_fresh_head_end_to_end.py with test_stale_head_refused_end_to_end: hermetic throwaway setup (tmp_path bare origin + clone, real git; gh stubbed via MagicMock whose pr_head_sha returns a SHA that never matches the real ls-remote tip and whose _run_git delegates to the real git in the clone via GhCli(cwd=work_dir)._run_git); call resolve_verified_head with attempts=2, delay_s=0, assert (None, reason) with both prefixes; then flip the stub to return the real tip and assert the converged SHA equals `git rev-parse` of the branch. No network beyond the local bare repo, no .env reliance, no sleeps (injected no-op).",
      "commit_message": "feat(review): safe_merge fresh-head guard + end-to-end verification test",
      "done_when": "pytest tests/unit/test_automerge_fresh_head.py::test_safe_merge_script_contains_fresh_head_guard tests/integration/test_automerge_fresh_head_end_to_end.py::test_stale_head_refused_end_to_end passes and shellcheck scripts/safe_merge.sh exits 0",
      "unit_tests": [
        "tests/unit/test_automerge_fresh_head.py::test_safe_merge_script_contains_fresh_head_guard"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_automerge_fresh_head_end_to_end.py::test_stale_head_refused_end_to_end"
  ]
}
```
