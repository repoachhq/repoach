---
id: SP-RETRY-BACKOFF-DEDUP
title: Extract a shared retry_with_backoff helper for Reviewer and Developer
version: 0.1
status: approved
author: agent
created: 2026-07-24
updated: 2026-07-24

owns:
  code:
    - src/repoach/review/retry_backoff.py
    - src/repoach/review/reviewer.py
    - tests/unit/test_retry_backoff.py
  resources: N/A

depends_on: []
provides_to: []

constraints: {}
---

# Extract a shared retry_with_backoff helper for Reviewer and Developer

## Intent

`Reviewer._call_with_retry` (~110 lines) and `Developer._call_with_retry`
(~60 lines) — both defined in `src/repoach/review/reviewer.py` — are
independent, hand-synchronised implementations of the same
retry-with-backoff loop over `self._RETRY_BACKOFFS_S = (0.0, 30.0,
90.0)`. Extract the shared loop mechanics (sleep-then-attempt,
catch-and-retry-on-exception, catch-and-retry-on-rejected-outcome,
exhaustion fallback) into one small module both callers use, so a
future change to the retry policy (schedule, jitter, what counts as
retryable) is made once instead of twice.

## Context

- `src/repoach/review/reviewer.py:481-593` — `Reviewer._call_with_retry`:
  loops over `self._RETRY_BACKOFFS_S`, sleeps `wait_s` before attempts
  after the first, calls `self._loop.run_oneshot(...,
  accept_response=self._response_is_parsable)`, catches any exception
  and retries, otherwise runs `self._parse_response(result.text)` and
  retries again while `summary.startswith("[parse_failed:")`. On full
  exhaustion it returns the last parse-failed tuple if one exists,
  else synthesises a `_FailedRunResult` stub (line 587) and a
  `[parse_failed:TRANSPORT]` tuple.
- `src/repoach/review/reviewer.py:1643-1705` — `Developer._call_with_retry`:
  the same loop shape over its own `_RETRY_BACKOFFS_S` class attribute
  (line 1643), calling `self._loop.run_oneshot(...,
  accept_response=_developer_response_has_fixes)`, catching any
  exception and retrying, returning the result immediately on success
  or `None` after exhaustion (line 1705).
- The `Reviewer._RETRY_BACKOFFS_S` docstring (reviewer.py:441-450)
  states it "ports the `Developer._RETRY_BACKOFFS_S` pattern" — the
  duplication is acknowledged in prose but never factored into shared
  code.
- Confirmed present on `develop` at draft time (grep re-run 2026-07-24):
  both methods exist verbatim at the cited ranges; nothing in this
  week's fixes touched them.
- Existing pinned tests already constrain the two call sites
  independently: `tests/unit/test_reviewer_retry_with_backoff.py`
  (all cases) and the Developer retry cases in
  `tests/unit/test_review_developer.py`
  (`test_developer_retries_on_transient_nim_error`,
  `test_developer_retry_handles_empty_backoffs_tuple`,
  `test_developer_retry_handles_spec_id_none`,
  `test_developer_retry_catches_non_apiconnection_exceptions`,
  `test_developer_returns_empty_after_all_retries_exhausted`) — these
  patch/call `Reviewer`/`Developer` subclasses directly and must keep
  passing unmodified; they are the regression fence for this
  extraction.
- `reviewer.py` is owned by prior specs (SP-REVIEWER-RETRY-WITH-BACKOFF,
  SP-NIM-RES-V2, among others); this spec is an in-place modification
  of the two named methods only, plus one new sibling module following
  the existing pattern of small single-purpose files in
  `src/repoach/review/` (e.g. `diff_scoper.py`, `review_lessons.py`).

## Goals

- G1: a single shared function, `retry_with_backoff`, in a new module
  `src/repoach/review/retry_backoff.py`, implements the retry loop
  mechanics (schedule the wait, invoke the attempt, catch exceptions
  and retry, catch a caller-rejected outcome and retry, stop and
  return on caller-accepted outcome, fall back to the last-seen value
  or last-seen exception after the schedule is exhausted).
- G2: `Reviewer._call_with_retry` and `Developer._call_with_retry` both
  call `retry_with_backoff`, each supplying only its own per-attempt
  closure (what to call, how to shape the returned value, whether to
  accept it) and its own post-loop result shaping (Reviewer's
  `_FailedRunResult` stub construction; Developer's `None`-on-exhaustion
  contract) — no loop-control code remains duplicated between them.
- G3: `Reviewer._RETRY_BACKOFFS_S` and `Developer._RETRY_BACKOFFS_S`
  remain as-is (each class keeps its own backoff-schedule class
  attribute; monkeypatching either independently, as
  `test_developer_retry_handles_empty_backoffs_tuple` does, keeps
  working).
- G4: every externally observable behavior of both methods is
  unchanged: call counts per scenario, return value shapes (Reviewer's
  4-tuple / `_FailedRunResult` stub; Developer's raw result or `None`),
  and the empty-backoffs-tuple edge case (zero attempts, no crash).

## Non-Goals

- NG1: no behavior change beyond the extraction — no change to the
  backoff schedule values, no jitter, no change to what counts as a
  retryable failure for either caller, no change to
  `_response_is_parsable` or `_developer_response_has_fixes`.
- NG2: no requirement to preserve the exact structured-log event key
  strings (e.g. `review.bot.parse_failed_retry`,
  `review.developer.retry_wait`) — no test asserts on these strings
  today (confirmed by grep); the shared helper may emit its own
  consistently-named events parameterized by a `log_scope` prefix, as
  long as a wait, an attempt failure, a rejected-outcome retry, and an
  exhaustion each still produce one structured log call.
- NG3: no change to `_FailedRunResult`, `ReviewVerdict`, `ReviewComment`,
  or any other dataclass/enum in `reviewer.py` beyond the two methods
  named in G2.
- NG4: no change to `review_diff` or `respond` (the public callers) —
  they keep calling `self._call_with_retry(...)` with the same
  signature and get the same return shape back.
- NG5: no change to `tests/unit/test_reviewer_retry_with_backoff.py`
  or the Developer retry tests inside `tests/unit/test_review_developer.py`
  — they must pass unmodified after the refactor (see Acceptance
  Criteria).

## Assumptions

- A1: `time.sleep` stays the injectable sleep point; existing tests
  monkeypatch the module-level `time` import (`_no_sleep` fixture,
  `monkeypatch.setattr(_time, "sleep", ...)` in `test_review_developer.py`)
  — the shared helper accepts an injectable `sleep` callable so those
  fixtures keep working without needing to know the helper's internal
  import path.

## Interface

New module `src/repoach/review/retry_backoff.py`:

```python
T = TypeVar("T")


@dataclass(frozen=True)
class AttemptOutcome(Generic[T]):
    value: T
    accept: bool


@dataclass(frozen=True)
class RetryResult(Generic[T]):
    value: T | None
    error: Exception | None
    attempts: int
    accepted: bool


def retry_with_backoff(
    attempt: Callable[[int], AttemptOutcome[T]],
    *,
    backoffs: tuple[float, ...],
    log_scope: str,
    log_context: Mapping[str, Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> RetryResult[T]: ...
```

`src/repoach/review/reviewer.py`:

- `Reviewer._call_with_retry(self, prompt, *, pr_number)` — unchanged
  signature and return type
  (`tuple[ReviewVerdict, str, list[ReviewComment], Any]`). Internally
  builds one closure `_attempt(attempt_no) -> AttemptOutcome[...]`
  that calls `self._loop.run_oneshot(...,
  accept_response=self._response_is_parsable)`, runs
  `self._parse_response(result.text)`, and returns
  `AttemptOutcome(value=(verdict, summary, comments, result),
  accept=not summary.startswith("[parse_failed:"))`. Calls
  `retry_with_backoff(_attempt, backoffs=self._RETRY_BACKOFFS_S,
  log_scope="review.bot", log_context={"role": self.role.value,
  "pr_number": pr_number})`; when `result.value is not None` returns it
  as-is, otherwise builds the existing `_FailedRunResult` /
  `[parse_failed:TRANSPORT]` stub tuple from `result.error`.
- `Developer._call_with_retry(self, prompt, *, spec_id)` — unchanged
  signature and return type (`Any | None`). Builds
  `_attempt(attempt_no) -> AttemptOutcome[Any]` that calls
  `self._loop.run_oneshot(..., accept_response=_developer_response_has_fixes)`
  and returns `AttemptOutcome(value=result, accept=True)` (Developer
  never rejects a successfully-parsed result — only an exception
  triggers its retry). Calls `retry_with_backoff(_attempt,
  backoffs=self._RETRY_BACKOFFS_S, log_scope="review.developer",
  log_context={"spec_id": spec_id})` and returns `result.value`
  (`None` when every attempt raised).

## Behavior

### Nominal

- First attempt is accepted (Reviewer: parseable JSON; Developer: any
  successful `run_oneshot` call) → `retry_with_backoff` returns after
  exactly one call to `attempt`, no `sleep` call, `RetryResult.accepted
  is True`.
- An attempt raises, the next attempt is accepted → `sleep` is called
  once with the second schedule entry, `attempt` is invoked twice,
  `RetryResult.accepted is True`.

### Edge cases

- `backoffs == ()` (monkeypatched, per
  `test_developer_retry_handles_empty_backoffs_tuple`) → the loop body
  never executes: `attempt` is called zero times, `sleep` is called
  zero times, `RetryResult(value=None, error=None, attempts=0,
  accepted=False)` is returned with no exception raised.
- Every attempt is rejected (Reviewer's parse-failed case) but none
  raises → after the schedule is exhausted, `RetryResult.value` is the
  **last** `AttemptOutcome.value` seen (not `None`), `error is None`,
  `accepted is False` — Reviewer returns that last tuple unchanged
  (matches `test_all_attempts_parse_failed_returns_last_marker`, which
  asserts the returned `result.text` is the literal last attempt's raw
  text, not a synthesised stub).
- A rejected outcome is followed by an attempt that raises, which is
  followed by an attempt that is accepted → `attempts == 3`,
  `accepted is True`, the returned value is the third attempt's.

### Failure scenarios

- Every attempt raises (no accepted outcome, no rejected-but-present
  outcome) → `RetryResult(value=None, error=<last exception>, attempts=
  len(backoffs), accepted=False)`. Reviewer builds the
  `_FailedRunResult` / `[parse_failed:TRANSPORT]` stub from
  `result.error`; Developer returns `None` (its `respond()` caller
  already turns that into an empty fix-plan with an "exhausted"
  summary — unchanged).

## Acceptance Criteria

- [ ] AC1: `src/repoach/review/retry_backoff.py` exists, exporting
  `retry_with_backoff`, `AttemptOutcome`, `RetryResult`, with Google-style
  docstrings on the module, both dataclasses, and the function; zero
  inline comments, zero `# noqa`.
- [ ] AC2: `Reviewer._call_with_retry` and `Developer._call_with_retry`
  in `src/repoach/review/reviewer.py` are rewritten to call
  `retry_with_backoff` per the Interface section; no duplicated
  sleep/try-except/logging loop remains in either method.
- [ ] AC3: promised new unit tests —
  `tests/unit/test_retry_backoff.py::test_accepts_first_attempt_no_retry`,
  `::test_retries_after_exception_then_succeeds`,
  `::test_retries_after_rejected_outcome_then_succeeds`,
  `::test_exhausted_rejected_returns_last_value_not_error`,
  `::test_exhausted_exceptions_returns_last_error_and_none_value`,
  `::test_empty_backoffs_makes_zero_attempts`,
  `::test_sleep_invoked_with_scheduled_backoff_seconds_only`. These
  import `repoach.review.retry_backoff` directly and MUST FAIL on
  pre-change code (the module does not exist yet — `ModuleNotFoundError`).
- [ ] AC4 (regression fence, no file edits): after the refactor,
  `tests/unit/test_reviewer_retry_with_backoff.py` (all cases) and the
  Developer retry cases in `tests/unit/test_review_developer.py`
  (`test_developer_retries_on_transient_nim_error`,
  `test_developer_retry_handles_empty_backoffs_tuple`,
  `test_developer_retry_handles_spec_id_none`,
  `test_developer_retry_catches_non_apiconnection_exceptions`,
  `test_developer_returns_empty_after_all_retries_exhausted`,
  `test_developer_respond_persists_token_usage_for_audit`) all pass
  unmodified — proving the extraction preserved call counts and return
  shapes for both callers.
- [ ] AC5: `ruff` + `ruff format --check` + `pytest tests/unit` green;
  zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`.

## Architecture Impact

- Adds one new module, `src/repoach/review/retry_backoff.py`, with no
  dependency beyond the stdlib (`time`, `dataclasses`, `typing`,
  `collections.abc`) — a leaf module inside `src/repoach/review/`,
  matching the existing pattern of small single-purpose siblings
  (`diff_scoper.py`).
- `reviewer.py` gains one new intra-package import
  (`from .retry_backoff import AttemptOutcome, RetryResult,
  retry_with_backoff`); no new cross-owner or cross-package coupling.
- Removes the duplicated retry-loop control flow between `Reviewer`
  and `Developer` — reduces coupling to a single source of truth for
  retry mechanics; the two callers keep their own accept-predicates
  and result shapes, which is the correct remaining variation point
  (not accidental duplication).

## Diagram

N/A (extract-a-helper refactor; no new runtime topology).

## Open Questions

(none)
