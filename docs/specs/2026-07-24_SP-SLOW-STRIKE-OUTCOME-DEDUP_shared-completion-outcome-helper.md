---
id: SP-SLOW-STRIKE-OUTCOME-DEDUP
title: Shared helper for the slow-strike completion-outcome policy (primary + budget-retry success paths)
version: 0.1
status: approved
author: agent
created: 2026-07-24
updated: 2026-07-24

owns:
  code: N/A
  resources: N/A

depends_on: [SP-BUDGET-RETRY-FIXES]
provides_to: []

constraints: {}
---

# Shared helper for the slow-strike completion-outcome policy (primary + budget-retry success paths)

## Intent

`ClaudeProxyService._stream_with_failover` applies the exact same
slow-strike completion-outcome policy — classify the completion via
`is_slow_completion`, fold it into the ref's k-of-n window via
`BreakerState.record_success`, then either log-and-trip, log-and-not-trip,
or recover the breaker — at two call sites: the primary success path and
the budget-retry success path. The two ~40-line bodies are copy-pasted
with only the latency-variable name changed, and the two sites have
already silently drifted (the retry path's `proxy_chain_failover_recovered`
log fires under different conditions and with an extra field than the
primary path's). Extract the shared sequence into one private method so a
future policy change (a new shadow condition, a different logging shape,
an added metric) is applied once and re-verified once. The existing
difference between the two sites is preserved exactly, as an explicit
`budget_retry` parameter — this spec does not decide whether that
difference is intentional, it only stops it from being ambiguous
copy-paste drift.

## Context

Re-verified against `origin/develop` HEAD (`git show
origin/develop:src/repoach/llm_proxy/api/services.py`) on 2026-07-24 —
the duplication is present, unchanged in shape from the original finding:

- `src/repoach/llm_proxy/api/services.py:439` —
  `ClaudeProxyService._stream_with_failover`, the single async generator
  both call sites live inside; it already has `dispatch_id`,
  `request_id`, `candidate`, `attempt_index`, and `prior_failures` in
  scope at both sites (lines 502, 508, 542).
- Primary success path, `services.py:636-687`: `ref = ModelRef.parse(...)`
  → `is_slow_completion(attempt_latency_s, peek.final_output_tokens,
  gate_s=..., tps_floor=...)` → `get_breaker().record_success(ref, True,
  k=..., n=...)` → if `should_trip`: `breaker_slow_strike_shadow` log
  (shadow mode) or `trip_slow` + `_persist_breaker_state` (enforcing) →
  else: `breaker_slow_strike` log → then, outside that if/else (not
  slow at all): `get_breaker().recover(ref)` + `_persist_breaker_state(ref)`
  → finally, UNCONDITIONALLY on `attempt_index > 0` (regardless of the
  slow/not-slow branch above), logs `proxy_chain_failover_recovered`.
- Budget-retry success path, `services.py:704-758`: identical sequence
  with `retry_latency_s` in place of `attempt_latency_s` and
  `retry_peek` in place of `peek`, EXCEPT `proxy_chain_failover_recovered`
  is only logged inside the not-slow branch (line 748-758) — never when
  the retry attempt itself strikes as slow — and that log call carries
  one extra field, `budget_retry=True`, the primary site's call (line
  679-687) does not carry.
- `services.py:470-484` (the method's docstring) already documents this
  shared policy in prose ("Both the primary success path and the
  budget-retry success path (SP-BREAKER-SLOW-STRIKE) apply the same
  recover-or-strike policy...") without the code itself sharing an
  implementation — the prose promise and the code are out of sync today.
- `services.py` is owned by `SP-BUDGET-RETRY-FIXES` (`owns.code`,
  verified: `grep -rl "llm_proxy/api/services.py" docs/specs/ | xargs
  grep -l "owns:"` lists nine specs referencing the path in body text,
  but only `SP-BUDGET-RETRY-FIXES`'s frontmatter lists
  `src/repoach/llm_proxy/api/services.py` under `owns.code`). This spec
  creates no new file and touches no file other than that one, so
  `owns.code: N/A` (precedent: `SP-PROXY-EARLY-ABORT-ERROR-FRAME`,
  `SP-NIM-PROBE-UNPARSEABLE-DIAG`).

## Goals

- G1: a single private method,
  `ClaudeProxyService._record_completion_outcome`, implements the
  is-slow-completion → record_success → shadow/trip/strike-log →
  recover-or-persist sequence exactly once.
- G2: both the primary success path (`services.py:636-687` today) and
  the budget-retry success path (`services.py:704-758` today) call
  `_record_completion_outcome` instead of running their own copy of the
  sequence; the two remaining behavioral differences — whether
  `proxy_chain_failover_recovered` is logged unconditionally on
  `attempt_index > 0` vs. only in the not-slow branch, and whether the
  log call carries `budget_retry=True` — become the single explicit
  `budget_retry: bool` parameter's documented branches, not two
  divergent copy-pasted bodies.
- G3: the method's docstring (`services.py:470-484`) is updated so its
  existing "both paths apply the same policy" claim names
  `_record_completion_outcome` as the single implementation backing
  that claim, instead of describing an aspiration two copies
  approximate.
- G4: net duplicated code shrinks — the two ~40-line bodies collapse to
  one ~45-line implementation plus two call sites of a few lines each.

## Non-Goals

- NG1: no behavior change beyond the code motion itself. The existing
  asymmetry in when/how `proxy_chain_failover_recovered` fires between
  the two call sites is preserved exactly as today — this spec does
  not decide whether that asymmetry is a bug; it only stops expressing
  it as silent copy-paste drift. (A future spec may choose to unify or
  intentionally diverge that log further; out of scope here.)
- NG2: no change to `is_slow_completion`, `BreakerState.record_success`,
  `BreakerState.trip_slow`, `BreakerState.recover`, or any
  `routing/breaker.py` semantics — this spec only reshapes the call
  site in `services.py`; the breaker module itself is untouched by this
  diff.
- NG3: no change to `_stream_with_failover`'s other branches (failover
  logging, budget-exhaustion handling, `_retry_with_more_budget` itself)
  beyond the two success-path bodies named in G2.
- NG4: no new `Settings` field, no new log event name, no new metric —
  the three existing event names (`breaker_slow_strike_shadow`,
  `breaker_slow_strike`, `proxy_chain_failover_recovered`) and their
  existing field sets are reproduced unchanged.

## Assumptions

- A1: both call sites always have `dispatch_id`, `request_id`,
  `candidate`, `attempt_index`, and `prior_failures` in scope at the
  point they currently run the duplicated sequence — verified above;
  the helper's signature can therefore take them as plain parameters
  with no new state threading required.

## Interface

`src/repoach/llm_proxy/api/services.py`, new private method on
`ClaudeProxyService`:

```python
def _record_completion_outcome(
    self,
    ref: ModelRef,
    latency_s: float,
    output_tokens: int | None,
    *,
    dispatch_id: str,
    request_id: str,
    candidate: ResolvedModel,
    attempt_index: int,
    prior_failures: list[tuple[str, str]],
    budget_retry: bool,
) -> None:
```

No public signature changes anywhere — `_stream_with_failover`'s
signature, `PeekResult`, and every existing log event's field set are
unchanged.

## Behavior

### Nominal

- A non-slow primary-success completion: `_record_completion_outcome`
  is called with `budget_retry=False`; it classifies not-slow, calls
  `get_breaker().recover(ref)` + `_persist_breaker_state(ref)`, and (if
  `attempt_index > 0`) logs `proxy_chain_failover_recovered` with no
  `budget_retry` field — identical to today's primary-path behavior.
- A non-slow budget-retry-success completion: called with
  `budget_retry=True`; same recover/persist, and (if `attempt_index >
  0`) logs `proxy_chain_failover_recovered` WITH `budget_retry=True` —
  identical to today's retry-path behavior.

### Edge cases

- A slow completion that does not yet reach the k-of-n threshold: logs
  `breaker_slow_strike` with the strike count — identical at both call
  sites regardless of `budget_retry`.
- A slow completion that reaches the k-of-n threshold in shadow mode:
  logs `breaker_slow_strike_shadow` with `would_trip=True`, does not
  trip — identical at both call sites regardless of `budget_retry`.
- A slow completion that reaches the k-of-n threshold NOT in shadow
  mode: trips via `trip_slow` + persists — identical at both call sites
  regardless of `budget_retry`.
- `attempt_index > 0` on a SLOW completion: primary path
  (`budget_retry=False`) still logs `proxy_chain_failover_recovered`
  (the log fires unconditionally there today); retry path
  (`budget_retry=True`) does NOT log it (the log only fires in the
  not-slow branch there today) — this asymmetry is preserved exactly,
  driven by the `budget_retry` parameter, per NG1.

### Failure scenarios

- None new. `_record_completion_outcome` never raises on its own,
  exactly as the inlined sequence never did today; any exception from
  `get_breaker()`/`_persist_breaker_state` propagates unchanged.

## Acceptance Criteria

- [ ] AC1: unit — new file
  `tests/unit/test_slow_strike_outcome_helper.py::test_primary_success_path_calls_shared_outcome_helper_once`.
  Monkeypatches `ClaudeProxyService._record_completion_outcome` with a
  recording spy (`monkeypatch.setattr(ClaudeProxyService,
  "_record_completion_outcome", spy)`) and drives one non-slow
  primary-success request (reusing the `_ScriptedProvider` /
  `_build_service` harness from `tests/unit/test_slow_breaker_wiring.py`)
  through `create_message`. Asserts the spy is called exactly once with
  `budget_retry=False` and `attempt_index=0`. On today's tree, no such
  attribute exists on `ClaudeProxyService`, so `monkeypatch.setattr`
  raises `AttributeError` and the test fails immediately; after the fix
  it passes.
- [ ] AC2: unit — same file,
  `test_budget_retry_success_path_calls_shared_outcome_helper_with_budget_retry_true`.
  Same spy technique; drives a request whose first attempt is
  zero-output-tokens (`looks_budget_starved`) with
  `budget_retry_enabled=True`, and whose `_retry_with_more_budget`
  second attempt returns real content. Asserts the spy is called
  exactly once (not for the budget-starved first attempt, only for the
  successful retry) with `budget_retry=True`. Fails today for the same
  `AttributeError` reason; after a correct fix it also fails if the
  retry call site were left un-migrated (spy never called, or called
  with the wrong `budget_retry` value).
- [ ] AC3: unit — same file,
  `test_helper_call_signature_is_identical_across_both_sites`. Runs
  both AC1 and AC2's scenarios, captures each spy call's kwarg KEY SET,
  and asserts they are identical (`dispatch_id`, `request_id`,
  `candidate`, `attempt_index`, `prior_failures`, `budget_retry` all
  present at both sites) — proving both call sites route through one
  shared parameter contract rather than two divergent helper shapes.
  Fails today (`AttributeError`, same as AC1/AC2) since the helper does
  not exist to compare.
- [ ] AC4 (regression, unmodified): the full pre-existing
  `tests/unit/test_slow_breaker_wiring.py`,
  `tests/unit/test_slow_completion_policy.py`, and
  `tests/integration/test_slow_breaker.py` suites stay green with zero
  edits — proving the extraction reproduces every existing observable
  log event and breaker-state transition byte-for-byte, at both call
  sites, not just the new helper's call contract.
- [ ] AC5: `ruff check` + `ruff format --check` green; zero inline
  comments (SP-NO-INLINE-COMMENTS-GATE) and no `# noqa` anywhere in the
  diff; full `pytest tests/unit` green; `services.py`'s
  `_stream_with_failover` method shrinks by at least 25 net lines
  relative to today's two-copy body.

## Architecture Impact

- `services.py` is owned by `SP-BUDGET-RETRY-FIXES` (`owns.code`); this
  spec's `depends_on: [SP-BUDGET-RETRY-FIXES]` is the edge that
  authorizes editing it — no additional edge is introduced. No new
  file, no new module, no new cross-owner import: `ModelRef`,
  `get_breaker`, `is_slow_completion` are already imported at the top
  of `services.py` (line 34) and `ResolvedModel` is already imported
  from `.model_router` (line 39); the new private method uses only
  already-imported names.
- New / changed coupling, cycles, or shared state: none — this is a
  pure in-class code motion. The two call sites' existing dependency on
  `get_breaker()`'s process-singleton state is unchanged; nothing new
  is shared across requests or across the two call sites beyond what
  already existed (the singleton breaker).

## Open Questions

None.
