# SP-PROXY-BREAKER-REASON-TTL — an EOL model stays down longer than a flap

**Status:** specified
**Redesign slice:** D2a — pillar 1 refinement. Umbrella:
`docs/proxy_routing_redesign_architecture.md`. Builds directly on
`SP-PROXY-HEALTH-BREAKER` (#410), whose own Follow-on names this slice.
**Touches forbidden paths:** no.

## Why

The health breaker (#410) closed the open loop, but with a **single flat
cool-down** (`breaker_ttl_s` = 120 s) for every failure reason. That is
wrong for a *permanent* death.

The 2026-06-20 session-start probe shows it live: `coder` →
`http=410` on `qwen/qwen3-coder-480b-a35b-instruct`. A `410 Gone` is the
NIM EOL signal (same class as the retired `meta/llama-3.1-405b`). Under
a flat 120 s TTL the breaker trips it, then the TTL lapses, the dead
model is **promoted back to the chain head**, and the very next coder
request pays the full 410 round-trip again — every two minutes, forever.
The loop is closed but it leaks: a permanent fault is treated as a
transient flap.

The matter to fix this is **already in hand**:
`_classify_failover_reason` (`api/services.py:38`) already maps a 410 to
the reason string `provider_410` — but `_trip_breaker` throws the reason
away and always trips for `breaker_ttl_s`. We just need to let the reason
choose the cool-down.

## Change

### `routing/breaker.py`

Add a pure, clock-free reason→TTL policy next to `BreakerState` (the
module stays free of any database or wall-clock dependency):

- `TERMINAL_REASONS: frozenset[str]` — failover reasons that signal a
  *permanent* upstream death rather than a transient flap. Seeded with
  `{"provider_410"}` (HTTP 410 Gone = model retired/EOL). Deliberately
  tight: a 5xx, a timeout, a rate-limit or an empty completion are all
  recoverable and must keep the short cool-down.
- `ttl_for_reason(reason, *, default_ttl_s, terminal_ttl_s) -> float` —
  returns `terminal_ttl_s` when `reason in TERMINAL_REASONS`, else
  `default_ttl_s`. Pure, total, no I/O.

`BreakerState.trip` is unchanged — it already takes an arbitrary
`ttl_s`; this slice only changes *which* TTL the caller passes.

### `api/services.py`

`_trip_breaker` takes the failover `reason` and derives the cool-down
from it:

```
def _trip_breaker(self, candidate: ResolvedModel, reason: str) -> None:
    if not self._settings.breaker_enabled:
        return
    ttl_s = ttl_for_reason(
        reason,
        default_ttl_s=self._settings.breaker_ttl_s,
        terminal_ttl_s=self._settings.breaker_ttl_terminal_s,
    )
    get_breaker().trip(ModelRef.parse(candidate.provider_model_ref), now=time.monotonic(), ttl_s=ttl_s)
```

Both call sites pass the reason they already compute:
- transport-exception path (`services.py:251`) → `primary_reason`
  (the `_classify_failover_reason(exc)` result, e.g. `provider_410`);
- empty-completion path (`services.py:302`) → `"empty_completion"`.

No other behaviour changes: which reasons trip the breaker is exactly as
before; only the *duration* now depends on the reason.

### `config/settings.py`

Add `breaker_ttl_terminal_s: float = 604_800.0` (7 days) with a
`validation_alias` of `BREAKER_TTL_TERMINAL_S` alongside the existing
`breaker_enabled` / `breaker_ttl_s`. Rationale for 7 days: an EOL `410`
is effectively permanent (a retired id is not coming back this week), so
a week-long cool-down stops the head re-promotion without ever pinning
the model dead — on TTL lapse it re-probes once and auto-rehabilitates if
NIM has revived the id, with no human edit of `chains.env`. The operator
can tune it via `.env`. (Probe-seed, D2b, gives a faster recovery path.)

## Recovery story

A terminal-tripped ref recovers by either (a) its 24 h TTL lapsing — it
re-enters the head and is probed once, clearing on success via the
existing `recover()` on the content path; or (b) `SP-PROXY-BREAKER-
PROBE-SEED` (D2b, follow-on) calling `recover()` when a fresh
`nim_health_probe` row shows the id healthy again. This slice keeps path
(a); it does not touch the probe store.

## Acceptance

- New tests in `tests/unit/test_health_breaker.py` (extending #410's
  suite):
  - `ttl_for_reason` returns `terminal_ttl_s` for `provider_410` and
    `default_ttl_s` for `timeout`, `rate_limited`, `provider_5xx`,
    `transport_error`, `empty_completion`, and an unknown reason;
  - integration: a `provider_410` failover trips the failed ref for the
    terminal TTL — the ref is still `is_down` at `now + breaker_ttl_s +
    1` (past the transient 120 s window) but no longer `is_down` past
    `now + breaker_ttl_terminal_s + 1` (past the 7-day window);
  - a `timeout` failover trips only for the transient window (recovers
    by `now + breaker_ttl_s + 1`).
- `_trip_breaker` is always called with a reason — both call sites
  updated; no call site left on the old one-arg signature.
- `test_settings_sharp_prefix_aliases` covers the new
  `BREAKER_TTL_TERMINAL_S` key.
- Existing failover + breaker suites stay green unchanged.
- Full `pytest tests/unit` green; ruff + format clean; no inline
  comments; no silent except.

## Follow-on

- `SP-PROXY-BREAKER-PROBE-SEED` (D2b): at startup read recent
  `nim_health_probe` rows (`fetch_probes`, currently caller-less) and
  pre-trip the breaker for any tier head whose latest probe is `error`/
  EOL — reusing `ttl_for_reason` so a probed 410 gets the same terminal
  cool-down. Removes the once-per-restart first-request 410 penalty that
  this slice alone still pays.
