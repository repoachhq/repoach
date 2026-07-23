# SP-FRESH-HEAD-CONCURRENT — Run fresh-head guard concurrently with reviewer fan-out

Move resolve_fresh_head off the blocking review path by submitting it to a background ThreadPoolExecutor, passing repo_root=self._repo_root instead of Path.cwd(), and joining the future immediately before record_review_ledger. The up-to-30-second wait overlaps with the four-reviewer fan-out instead of preceding it, without weakening the freshness guarantee.

## Step 1 — Add _join_head_guard helper and its unit tests

- **Files**: `src/repoach/review/orchestrator.py`, `tests/unit/test_review_head_guard.py`
- **Action**: Add the private method `_join_head_guard(self, pool, future, *, pr_number) -> str | None` to `ReviewTeamOrchestrator` in `src/repoach/review/orchestrator.py`. The method joins the future if both pool and future are not None, catches exceptions, logs `review_team.head_guard_failed` with pr_number and exception type name, shuts the pool down, and returns the resolved SHA or None. When pool/future is None it returns None immediately. Add three unit tests to `tests/unit/test_review_head_guard.py`: `test_join_head_guard_returns_none_when_no_pool`, `test_join_head_guard_returns_future_result`, `test_join_head_guard_catches_exception_and_logs`. These tests import `ReviewTeamOrchestrator` and call `_join_head_guard` directly with fake futures and pools.
- **Commit**: `feat(review): add _join_head_guard helper for concurrent head resolution`
- **Done when**: pytest tests/unit/test_review_head_guard.py::test_join_head_guard_returns_none_when_no_pool tests/unit/test_review_head_guard.py::test_join_head_guard_returns_future_result tests/unit/test_review_head_guard.py::test_join_head_guard_catches_exception_and_logs -x -q passes
- **Unit tests**: `tests/unit/test_review_head_guard.py::test_join_head_guard_returns_none_when_no_pool`, `tests/unit/test_review_head_guard.py::test_join_head_guard_returns_future_result`, `tests/unit/test_review_head_guard.py::test_join_head_guard_catches_exception_and_logs`

## Step 2 — Modify review_pr to run guard concurrently and add orchestrator-level tests

- **Files**: `src/repoach/review/orchestrator.py`, `tests/unit/test_review_head_guard.py`, `tests/integration/test_fresh_head_concurrent.py`
- **Action**: In `ReviewTeamOrchestrator.review_pr` (`src/repoach/review/orchestrator.py`), replace the synchronous `resolve_fresh_head(self._gh, pr_number, repo_root=Path.cwd())` call at lines 331-333 with: when `self._post` is True, create a dedicated `ThreadPoolExecutor(max_workers=1)` and submit `resolve_fresh_head(self._gh, pr_number, repo_root=self._repo_root)` to it, storing the pool and future. Immediately before the `record_review_ledger` call (currently around line 526), call `head_sha = self._join_head_guard(pool, future, pr_number=pr_number)` and shut the pool down. When `self._post` is False, set `head_sha = None` and create no pool/future. Update the `review_pr` docstring to describe the concurrent scheduling (step 4 runs concurrently with the guard). Add two unit tests from the spec's AC1 and AC2 to `tests/unit/test_review_head_guard.py`: `test_orchestrator_head_guard_call_site_is_non_blocking` (constructs orchestrator with explicit repo_root, uses a fake resolve_fresh_head that records repo_root and waits on an event set by reviewer fakes, asserting head_sha == 'event-set' and repo_root matches) and `test_orchestrator_head_guard_overlaps_reviewer_fanout` (drives real resolve_fresh_head against a scratch git repo with a truthful stale-forever GhCli fake, asserting wall time is less than sum of guard and reviewer times). Create new integration test file `tests/integration/test_fresh_head_concurrent.py` with `test_concurrent_head_guard_integration` that constructs an orchestrator with a real scratch git repo under `tmp_path`, uses truthful fakes for GhCli and reviewers, and asserts `head_sha` is set correctly and the guard ran concurrently.
- **Commit**: `feat(review): run fresh-head guard concurrently with reviewer fan-out`
- **Done when**: pytest tests/unit/test_review_head_guard.py tests/unit/test_review_team.py tests/integration/test_fresh_head_concurrent.py -x -q passes
- **Unit tests**: `tests/unit/test_review_head_guard.py::test_orchestrator_head_guard_call_site_is_non_blocking`, `tests/unit/test_review_head_guard.py::test_orchestrator_head_guard_overlaps_reviewer_fanout`

## Integration tests

- `tests/integration/test_fresh_head_concurrent.py::test_concurrent_head_guard_integration`

<!-- repoach-action-plan -->
```json
{
  "spec_id": "SP-FRESH-HEAD-CONCURRENT",
  "title": "Run fresh-head guard concurrently with reviewer fan-out",
  "summary": "Move resolve_fresh_head off the blocking review path by submitting it to a background ThreadPoolExecutor, passing repo_root=self._repo_root instead of Path.cwd(), and joining the future immediately before record_review_ledger. The up-to-30-second wait overlaps with the four-reviewer fan-out instead of preceding it, without weakening the freshness guarantee.",
  "steps": [
    {
      "index": 1,
      "title": "Add _join_head_guard helper and its unit tests",
      "files": [
        "src/repoach/review/orchestrator.py",
        "tests/unit/test_review_head_guard.py"
      ],
      "action": "Add the private method `_join_head_guard(self, pool, future, *, pr_number) -> str | None` to `ReviewTeamOrchestrator` in `src/repoach/review/orchestrator.py`. The method joins the future if both pool and future are not None, catches exceptions, logs `review_team.head_guard_failed` with pr_number and exception type name, shuts the pool down, and returns the resolved SHA or None. When pool/future is None it returns None immediately. Add three unit tests to `tests/unit/test_review_head_guard.py`: `test_join_head_guard_returns_none_when_no_pool`, `test_join_head_guard_returns_future_result`, `test_join_head_guard_catches_exception_and_logs`. These tests import `ReviewTeamOrchestrator` and call `_join_head_guard` directly with fake futures and pools.",
      "commit_message": "feat(review): add _join_head_guard helper for concurrent head resolution",
      "done_when": "pytest tests/unit/test_review_head_guard.py::test_join_head_guard_returns_none_when_no_pool tests/unit/test_review_head_guard.py::test_join_head_guard_returns_future_result tests/unit/test_review_head_guard.py::test_join_head_guard_catches_exception_and_logs -x -q passes",
      "unit_tests": [
        "tests/unit/test_review_head_guard.py::test_join_head_guard_returns_none_when_no_pool",
        "tests/unit/test_review_head_guard.py::test_join_head_guard_returns_future_result",
        "tests/unit/test_review_head_guard.py::test_join_head_guard_catches_exception_and_logs"
      ]
    },
    {
      "index": 2,
      "title": "Modify review_pr to run guard concurrently and add orchestrator-level tests",
      "files": [
        "src/repoach/review/orchestrator.py",
        "tests/unit/test_review_head_guard.py",
        "tests/integration/test_fresh_head_concurrent.py"
      ],
      "action": "In `ReviewTeamOrchestrator.review_pr` (`src/repoach/review/orchestrator.py`), replace the synchronous `resolve_fresh_head(self._gh, pr_number, repo_root=Path.cwd())` call at lines 331-333 with: when `self._post` is True, create a dedicated `ThreadPoolExecutor(max_workers=1)` and submit `resolve_fresh_head(self._gh, pr_number, repo_root=self._repo_root)` to it, storing the pool and future. Immediately before the `record_review_ledger` call (currently around line 526), call `head_sha = self._join_head_guard(pool, future, pr_number=pr_number)` and shut the pool down. When `self._post` is False, set `head_sha = None` and create no pool/future. Update the `review_pr` docstring to describe the concurrent scheduling (step 4 runs concurrently with the guard). Add two unit tests from the spec's AC1 and AC2 to `tests/unit/test_review_head_guard.py`: `test_orchestrator_head_guard_call_site_is_non_blocking` (constructs orchestrator with explicit repo_root, uses a fake resolve_fresh_head that records repo_root and waits on an event set by reviewer fakes, asserting head_sha == 'event-set' and repo_root matches) and `test_orchestrator_head_guard_overlaps_reviewer_fanout` (drives real resolve_fresh_head against a scratch git repo with a truthful stale-forever GhCli fake, asserting wall time is less than sum of guard and reviewer times). Create new integration test file `tests/integration/test_fresh_head_concurrent.py` with `test_concurrent_head_guard_integration` that constructs an orchestrator with a real scratch git repo under `tmp_path`, uses truthful fakes for GhCli and reviewers, and asserts `head_sha` is set correctly and the guard ran concurrently.",
      "commit_message": "feat(review): run fresh-head guard concurrently with reviewer fan-out",
      "done_when": "pytest tests/unit/test_review_head_guard.py tests/unit/test_review_team.py tests/integration/test_fresh_head_concurrent.py -x -q passes",
      "unit_tests": [
        "tests/unit/test_review_head_guard.py::test_orchestrator_head_guard_call_site_is_non_blocking",
        "tests/unit/test_review_head_guard.py::test_orchestrator_head_guard_overlaps_reviewer_fanout"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_fresh_head_concurrent.py::test_concurrent_head_guard_integration"
  ]
}
```
