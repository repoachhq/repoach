# SP-RELEASE-VERIFY-MERGE-COMMIT — Verify the release shape, exit loudly

Fix verify_release to accept the sanctioned merge-commit shape (approved SHA == main tip OR == the merge tip's second parent with zero main..develop), with truthful throwaway-git tests for all three shapes (step 1); pin the CLI exit codes as a regression guard (step 2 — G2 was a measurement artifact, the CLI already exits 5); prove the end-to-end merge-commit verify (step 3). Hand-authored: the Planner deadlocked because the spec's ACs are unit-only while the src form rule demands an integration test — the plan adds one (superset of the spec ACs). Correction folded in: verify_release exiting 0 was misread through a `tail` pipe; the only real defect is tip-equality vs shape. No stubs — tests use REAL git repos and a GhCli over real git; the CLI test redirects only the repo/receipt path constants.

## Step 1 — verify_release checks the release SHAPE

- **Files**: `src/ferova/review/release_gate.py`, `tests/unit/test_release_gate.py`
- **Action**: In src/ferova/review/release_gate.py rewrite verify_release (release_gate.py:246-282) to verify the release SHAPE instead of tip equality. Keep reading expected_sha from the receipt and main_sha from `ls-remote origin main` (for the report). Then compute via gh._run_git: second_parent = gh._run_git(["rev-parse", "origin/main^2"]).stdout.strip() (empty/error when main is not a merge commit) and distance = gh._run_git(["rev-list", "--count", "origin/main..origin/develop"]).stdout.strip(). Set verified = bool(expected_sha) and (main_sha == expected_sha (fast-forward) or (second_parent == expected_sha and distance == "0") (sanctioned merge commit)). Keep the existing detail messages (match vs "squash or stale merge? revert and re-merge as a merge commit") and preserve the FileNotFoundError/JSONDecodeError evaluation-error contract. Add three unit tests to tests/unit/test_release_gate.py building REAL throwaway git repos (mirror the _git helper in tests/integration/test_release_gate_end_to_end.py; a bare origin plus a work clone; construct GhCli(cwd=work) so real git runs; write the receipt via write_gate_receipt recording the develop tip): test_verify_accepts_merge_commit_release (git merge --no-ff develop into main, push; verify_release -> verified True), test_verify_still_refuses_squash (main advanced by a commit that is NOT a merge of develop -> verified False and detail mentions squash/stale), test_verify_refuses_stale_merge (merge --no-ff taken, then develop advances one commit so distance != 0 -> verified False).
- **Commit**: `fix(release): verify the release merge shape, not tip equality`
- **Done when**: pytest tests/unit/test_release_gate.py::test_verify_accepts_merge_commit_release tests/unit/test_release_gate.py::test_verify_still_refuses_squash tests/unit/test_release_gate.py::test_verify_refuses_stale_merge passes
- **Unit tests**: `tests/unit/test_release_gate.py::test_verify_accepts_merge_commit_release`, `tests/unit/test_release_gate.py::test_verify_still_refuses_squash`, `tests/unit/test_release_gate.py::test_verify_refuses_stale_merge`

## Step 2 — Pin the CLI verify exit codes (regression guard)

- **Files**: `tests/unit/test_release_cli.py`
- **Action**: The `ferova release verify` CLI already exits 5 on not-verified, 0 on verified and 1 on an evaluation error (release_cmds.py:91-126); the spec's G2 "exited 0 despite verified:false" was a measurement artifact (the original run piped through `tail`, so `$?` captured tail's exit, not ferova's). Add tests/unit/test_release_cli.py::test_cli_release_verify_exit_codes as a regression guard WITHOUT stubbing verify_release: build real throwaway git repos (reuse the step-1 real-repo helper style) for a merge-commit shape (verified) and a squash shape (not verified), write a real receipt for each, and redirect the module path constants release_cmds._repo_root and release_cmds._RECEIPT_PATH at the tmp work repo and receipt via a pytest fixture so the real verify_release runs over real git; assert release_cmds.release_verify() returns cleanly (exit 0) for the merge-commit shape, raises typer.Exit with code 5 for the squash shape, and raises typer.Exit with code 1 when the receipt path is missing.
- **Commit**: `test(release): pin ferova release verify exit codes`
- **Done when**: pytest tests/unit/test_release_cli.py::test_cli_release_verify_exit_codes passes
- **Unit tests**: `tests/unit/test_release_cli.py::test_cli_release_verify_exit_codes`

## Step 3 — End-to-end merge-commit verify integration test

- **Files**: `tests/integration/test_release_gate_end_to_end.py`
- **Action**: Add tests/integration/test_release_gate_end_to_end.py::test_release_verify_merge_commit_end_to_end using the file's existing _git helper: build a bare origin plus a work clone with main and develop; gate the develop head via gather_release_facts + compute_release_decision + write_gate_receipt (a truthful fake ci_runner returning returncode 0, as the existing end-to-end test does); perform a real `git merge --no-ff develop` into main and push; call verify_release(receipt, gh=GhCli(cwd=work)) and assert verified True with the match detail; then create a divergence (advance develop one commit after the merge) and assert a second verify_release is verified False. Hermetic: no network beyond the local bare repo, no LLM, no `.env` reliance.
- **Commit**: `test(release): end-to-end merge-commit verify integration test`
- **Done when**: pytest tests/integration/test_release_gate_end_to_end.py::test_release_verify_merge_commit_end_to_end passes
- **Unit tests**: `tests/integration/test_release_gate_end_to_end.py::test_release_verify_merge_commit_end_to_end`

## Integration tests

- `tests/integration/test_release_gate_end_to_end.py::test_release_verify_merge_commit_end_to_end`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-RELEASE-VERIFY-MERGE-COMMIT",
  "title": "Verify the release shape, exit loudly",
  "summary": "Fix verify_release to accept the sanctioned merge-commit shape (approved SHA == main tip OR == the merge tip's second parent with zero main..develop), with truthful throwaway-git tests for all three shapes (step 1); pin the CLI exit codes as a regression guard (step 2 - G2 was a measurement artifact, the CLI already exits 5); prove the end-to-end merge-commit verify (step 3). The plan adds an integration test the spec's unit-only ACs omit (superset). No stubs - real git repos and a GhCli over real git; the CLI test redirects only the repo/receipt path constants.",
  "steps": [
    {
      "index": 1,
      "title": "verify_release checks the release SHAPE",
      "files": [
        "src/ferova/review/release_gate.py",
        "tests/unit/test_release_gate.py"
      ],
      "action": "In src/ferova/review/release_gate.py rewrite verify_release (release_gate.py:246-282) to verify the release SHAPE instead of tip equality. Keep reading expected_sha from the receipt and main_sha from `ls-remote origin main` (for the report). Then compute via gh._run_git: second_parent = gh._run_git([\"rev-parse\", \"origin/main^2\"]).stdout.strip() (empty/error when main is not a merge commit) and distance = gh._run_git([\"rev-list\", \"--count\", \"origin/main..origin/develop\"]).stdout.strip(). Set verified = bool(expected_sha) and (main_sha == expected_sha (fast-forward) or (second_parent == expected_sha and distance == \"0\") (sanctioned merge commit)). Keep the existing detail messages (match vs \"squash or stale merge? revert and re-merge as a merge commit\") and preserve the FileNotFoundError/JSONDecodeError evaluation-error contract. Add three unit tests to tests/unit/test_release_gate.py building REAL throwaway git repos (mirror the _git helper in tests/integration/test_release_gate_end_to_end.py; a bare origin plus a work clone; construct GhCli(cwd=work) so real git runs; write the receipt via write_gate_receipt recording the develop tip): test_verify_accepts_merge_commit_release (git merge --no-ff develop into main, push; verify_release -> verified True), test_verify_still_refuses_squash (main advanced by a commit that is NOT a merge of develop -> verified False and detail mentions squash/stale), test_verify_refuses_stale_merge (merge --no-ff taken, then develop advances one commit so distance != 0 -> verified False).",
      "commit_message": "fix(release): verify the release merge shape, not tip equality",
      "done_when": "pytest tests/unit/test_release_gate.py::test_verify_accepts_merge_commit_release tests/unit/test_release_gate.py::test_verify_still_refuses_squash tests/unit/test_release_gate.py::test_verify_refuses_stale_merge passes",
      "unit_tests": [
        "tests/unit/test_release_gate.py::test_verify_accepts_merge_commit_release",
        "tests/unit/test_release_gate.py::test_verify_still_refuses_squash",
        "tests/unit/test_release_gate.py::test_verify_refuses_stale_merge"
      ]
    },
    {
      "index": 2,
      "title": "Pin the CLI verify exit codes (regression guard)",
      "files": [
        "tests/unit/test_release_cli.py"
      ],
      "action": "The `ferova release verify` CLI already exits 5 on not-verified, 0 on verified and 1 on an evaluation error (release_cmds.py:91-126); the spec's G2 \"exited 0 despite verified:false\" was a measurement artifact (the original run piped through `tail`, so `$?` captured tail's exit, not ferova's). Add tests/unit/test_release_cli.py::test_cli_release_verify_exit_codes as a regression guard WITHOUT stubbing verify_release: build real throwaway git repos (reuse the step-1 real-repo helper style) for a merge-commit shape (verified) and a squash shape (not verified), write a real receipt for each, and redirect the module path constants release_cmds._repo_root and release_cmds._RECEIPT_PATH at the tmp work repo and receipt via a pytest fixture so the real verify_release runs over real git; assert release_cmds.release_verify() returns cleanly (exit 0) for the merge-commit shape, raises typer.Exit with code 5 for the squash shape, and raises typer.Exit with code 1 when the receipt path is missing.",
      "commit_message": "test(release): pin ferova release verify exit codes",
      "done_when": "pytest tests/unit/test_release_cli.py::test_cli_release_verify_exit_codes passes",
      "unit_tests": [
        "tests/unit/test_release_cli.py::test_cli_release_verify_exit_codes"
      ]
    },
    {
      "index": 3,
      "title": "End-to-end merge-commit verify integration test",
      "files": [
        "tests/integration/test_release_gate_end_to_end.py"
      ],
      "action": "Add tests/integration/test_release_gate_end_to_end.py::test_release_verify_merge_commit_end_to_end using the file's existing _git helper: build a bare origin plus a work clone with main and develop; gate the develop head via gather_release_facts + compute_release_decision + write_gate_receipt (a truthful fake ci_runner returning returncode 0, as the existing end-to-end test does); perform a real `git merge --no-ff develop` into main and push; call verify_release(receipt, gh=GhCli(cwd=work)) and assert verified True with the match detail; then create a divergence (advance develop one commit after the merge) and assert a second verify_release is verified False. Hermetic: no network beyond the local bare repo, no LLM, no `.env` reliance.",
      "commit_message": "test(release): end-to-end merge-commit verify integration test",
      "done_when": "pytest tests/integration/test_release_gate_end_to_end.py::test_release_verify_merge_commit_end_to_end passes",
      "unit_tests": [
        "tests/integration/test_release_gate_end_to_end.py::test_release_verify_merge_commit_end_to_end"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_release_gate_end_to_end.py::test_release_verify_merge_commit_end_to_end"
  ]
}
```
