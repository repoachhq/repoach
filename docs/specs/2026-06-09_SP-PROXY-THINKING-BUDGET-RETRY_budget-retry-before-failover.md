# SP-PROXY-THINKING-BUDGET-RETRY — retry a budget-starved candidate with more tokens before failing over

## Metadata

- **Status**: OPEN
- **Priority**: P1
- **Owner**: operator
- **Executor**: hand-implemented (the autonomous Coder stubbed the promised test; the strict gate reverted step 1)
- **Opened**: 2026-06-09

## Why

Thinking-capable models reason internally before answering. Several
NIM-served models (e.g. `qwen/qwen3.5-122b-a10b`) **strip** that
reasoning from the response, so it is invisible — but it still consumes
the `max_tokens` budget. With a tight budget the answer is starved to
nothing.

Empirically (2026-06-09, direct NIM `/chat/completions`):

| model / `max_tokens` | `finish_reason` | content | usage |
|---|---|---|---|
| qwen3.5-122b @ **25** | `None` | **0 chars** | `{}` |
| qwen3.5-122b @ **600** | `stop` | **713 chars** | 292 tok |
| mistral-medium-3.5 @ 25 | `length` | 104 chars | 25 tok |

The chain-walk in `ClaudeProxyService` (`api/services.py`, the
`for candidate in chain` loop) reads the empty completion via
`peek_for_content` (`got_content=False`) and **fails over to the next
candidate** — discarding a strong model that only needed more budget,
and cascading into the >70s / `/v1/agent` 502 failures seen this
session. The lever is the **budget**, not the provider: a starved empty
is not a dead provider.

## What

Add a reactive budget retry to the chain-walk: when a candidate returns
a **budget-starved empty** completion, retry **the same candidate** once
with an enlarged `max_tokens` **before** advancing to the next chain
candidate. Only advance / fail over if the enlarged retry is also empty.

1. **`api/_failover.py`** — expose, on `PeekResult`, enough signal for
   the caller to distinguish a *budget-starved* empty from a *dead /
   transport-error* empty. The "budget-starved" signature: the stream
   completed (not a transport error / not the NIM "Connection error."
   text disguise) with empty-or-whitespace text and a truncation/zero
   signal (`finish_reason` in {`length`, `null`} or `output_tokens`
   that did not produce usable text). Add a boolean such as
   `looks_budget_starved` — do **not** change the existing
   `got_content` semantics.
2. **`api/services.py`** — in the chain-walk loop, when
   `peek.got_content` is `False` **and** `peek.looks_budget_starved` is
   `True`, re-issue the request to the **same** `candidate` with
   `max_tokens` enlarged to
   `min(max(original_max_tokens * factor, floor), cap)` and peek again.
   If the enlarged attempt `got_content`, forward it. Retry **at most
   once per candidate** (then advance). Emit a `proxy_budget_retry`
   structured log. A non-starved empty (transport error, disguised
   error) keeps the current immediate-failover behaviour.
3. **`config/settings.py`** — add knobs (read via `FEROVA_PROXY_*`):
   `budget_retry_enabled` (default `true`), `budget_retry_factor`
   (default `8`), `budget_retry_floor` (default `512`),
   `budget_retry_cap` (default `4096`). When disabled, behaviour is
   exactly today's.

No behaviour change on the happy path (a candidate that yields content
on the first attempt is untouched) and no change to chain ordering or
model selection.

## Files in scope

- `src/ferova/llm_proxy/api/_failover.py`
- `src/ferova/llm_proxy/api/services.py`
- `src/ferova/llm_proxy/config/settings.py`
- `tests/unit/test_proxy_budget_retry.py` (new)
- `tests/unit/test_settings_sharp_prefix_aliases.py` (alias map lockstep)

## Out of scope

- Forwarding stripped/exposed `reasoning_content` as Anthropic
  `thinking` blocks (a later slice of the thinking bridge).
- Self-hosted NIM serving.
- Any change to `chains.env` / `MODEL_*` ordering or model membership.

## Smoke scenario

A fake provider (in the unit test) whose stream yields an **empty**
completion when the request `max_tokens` is below a threshold and a
**content** completion at or above it. Driving
`ClaudeProxyService.create_message` with that provider as the first
chain candidate and a small `max_tokens`, the service retries the
**same** candidate with the enlarged budget and returns its content —
it does **not** advance to a second candidate. (Live confirmation,
operator-run: `qwen/qwen3.5-122b-a10b` via the proxy with a tight
`max_tokens` returns its answer instead of failing over.)

## Definition of Done

- A budget-starved empty triggers exactly one same-candidate retry with
  enlarged `max_tokens`
  (`test_budget_starved_empty_retries_same_candidate_with_more_budget`).
- A genuinely dead candidate still fails over after the single enlarged
  retry — no infinite loop (`test_dead_candidate_fails_over_after_one_retry`).
- A non-starved empty (transport/disguised-error) fails over immediately
  with no budget retry (`test_non_starved_empty_does_not_retry`).
- `budget_retry_enabled=false` restores today's behaviour
  (`test_disabled_flag_keeps_immediate_failover`).
- `ruff` + `ruff format --check` + full `pytest tests/unit` green; zero
  inline comments; no `# noqa`.

## Commit plan

1. `feat(proxy): retry a budget-starved candidate with more tokens before failover`

## Risks

- **Latency**: a budget retry adds one round-trip. It only fires on an
  empty completion bearing the starvation signature (rare for healthy
  non-thinking models), and at most once per candidate.
- **Mis-classification**: a transport error mis-read as starved would
  waste one call before failover — bounded to one extra attempt. The
  signature excludes the NIM "Connection error." disguise and any
  non-completed / error-stop stream (the existing `_failover` rules
  isolate those).
- **Cap interplay**: the enlarged `max_tokens` is capped (default 4096)
  so a 100k-token request never explodes; the cap covers the observed
  thinking headroom (qwen3.5 needed ~300 completion tokens).
