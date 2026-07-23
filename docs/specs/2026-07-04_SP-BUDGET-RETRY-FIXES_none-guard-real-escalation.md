---
id: SP-BUDGET-RETRY-FIXES
title: Budget retry — None guard and real escalation headroom
version: 0.1
status: approved
author: jfaye (thinking-handling audit, 2026-07-04)
created: 2026-07-04
updated: 2026-07-23

owns:
  code: [src/repoach/llm_proxy/api/services.py]
  resources: []

depends_on: [SP-PROXY-FIRST-BYTE-DEADLINE]
provides_to: []

constraints: {}
---

# Budget retry — None guard and real escalation headroom

## Intent

Fix two proven defects in the thinking-budget retry
(SP-PROXY-THINKING-BUDGET-RETRY): a crash path on requests without
`max_tokens`, and a cap that makes the retry a provable no-op for
exactly the four providers it targets.

## Context

`_retry_with_more_budget` (`api/services.py`) computes
`original_max * budget_retry_factor` where
`MessagesRequest.max_tokens` is `int | None`
(`api/models/anthropic.py`) — a `/v1/messages` caller omitting
`max_tokens` whose stream comes back budget-starved raises `TypeError`
at the multiplication, outside any try block. Separately, the
escalated value is capped at `budget_retry_cap` (default 4096,
`config/settings.py`), while the combined-budget providers
(kimi/groq/cerebras/deepseek) already raise their effective request to
a 4096 answer-headroom floor in `_build_request_body`
(`providers/reasoning.py` + `providers/openai_generic.py`): the
"enlarged" retry re-sends the same effective 4096 and cannot help.

## Goals

- G1: A `None` `max_tokens` on the starved-retry path is handled, not
  raised: the retry bases its escalation on the provider-effective
  default that was actually in play, and a unit test pins the no-crash
  behaviour.
- G2: The escalation is real for combined-budget providers: the
  enlarged value strictly exceeds the effective (post-floor) tokens of
  the first attempt, or the retry is skipped with the existing
  "already at cap" semantics. The default `budget_retry_cap` is raised
  to 8192 so the ×8 factor has room to act above the 4096 floor.
- G3: Existing pinned behaviours are preserved: one escalation per
  candidate, no carry-over to the next candidate, disabled-flag
  passthrough, non-starved empties never retried.

## Non-Goals

- NG1: No change to starvation detection (`peek_for_content` /
  `looks_budget_starved`).
- NG2: No second escalation, no cross-candidate budget carry-over.
- NG3: No breaker/ledger semantics change (the recovery-on-retry gap
  is noted debt, separate concern).

## Assumptions

- A1: `src/ferova/llm_proxy/api/services.py` is unowned in the arch
  registry (verified 2026-07-04: `owner_of` returns `None`).
- A2: Raising the default cap to 8192 is safe for providers with
  smaller context limits because the per-provider request builders
  already bound what they send upstream; the cap governs only the
  retry's ask.

## Interface

Inputs: N/A (internal retry path; one settings default change:
`budget_retry_cap` 4096 → 8192, env override unchanged).

Outputs: N/A.

Errors: none new; the `TypeError` path is removed.

## Behavior

### Nominal

A starved empty from a kimi candidate first attempted at effective
4096 is retried once at 8192; a starved empty at `max_tokens=512` is
retried at 4096 (×8) exactly as today.

### Edge cases

- `max_tokens=None` + starved empty → retry proceeds from the
  effective default instead of crashing.
- First attempt already at or above the cap → no retry, immediate
  failover (existing semantics, now against the 8192 default).

### Failure scenarios

- The enlarged retry is still empty → fail over to the next
  candidate at original request values (existing pinned behaviour).

## Architecture Impact

- No edge added or removed. `services.py` moves from the frontier
  into this spec's `owns.code`.
- 2026-07-23 addendum (SP-PROXY-FIRST-BYTE-DEADLINE): `services.py`'s
  existing import of `repoach.llm_proxy.config.settings` became a
  governed edge once that module moved from the frontier into
  SP-PROXY-FIRST-BYTE-DEADLINE's `owns.code` — no new import was
  written, but SP-ARCH-EDGE-GATE now requires the coupling declared
  here. `depends_on` updated accordingly.

## Diagram

N/A (two guards in one function).

## Acceptance Criteria

- [ ] AC1: `tests/unit/test_proxy_budget_retry.py::test_none_max_tokens_starved_empty_does_not_crash`
  — a starved empty on a request with `max_tokens=None` triggers a
  retry (not a `TypeError`), with the escalated ask recorded.
- [ ] AC2: `tests/unit/test_proxy_budget_retry.py::test_escalation_exceeds_the_effective_floor`
  — a combined-budget provider first attempt at effective 4096 is
  retried strictly above 4096.
- [ ] AC3: `tests/unit/test_proxy_budget_retry.py::test_at_cap_requests_still_fail_over_without_retry`
  — first attempt at the cap → no retry, immediate failover.
- [ ] AC4: The existing budget-retry suite
  (`tests/unit/test_proxy_budget_retry.py`) passes unmodified except
  where a pinned default (4096 cap) is updated to the new default.
- [ ] AC5: The full unit suite passes.

## Open Questions

(none)
