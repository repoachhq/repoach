# SP-NIM-LOG-DURABILITY — keep the proxy's NIM telemetry across restarts + enrich it with per-attempt latency

## Metadata

- **Status**: OPEN
- **Priority**: P2
- **Owner**: operator
- **Executor**: hand-implemented
- **Opened**: 2026-06-10

## Why

The active probe history (SP-NIM-HEALTH-HISTORY) samples NIM heads on a
timer, but the **passive** telemetry — what the proxy actually
experienced on real traffic (which candidate emptied, failed over, was
budget-retried, and how slow it was) — is the richer signal. Two gaps
make it unusable for analysis today:

1. **It is wiped on every restart.** `configure_logging`
   (`llm_proxy/config/logging_config.py`) calls
   `Path(log_file).write_text("")` on startup ("clean debugging
   slate"). The proxy is an editable install restarted between reviews,
   so `server.log` is truncated constantly — nothing accumulates across
   sessions. The sink already appends (`mode="a"`) and rotates
   (`rotation="50 MB"`); only the truncation defeats durability.
2. **The chain-walk events lack latency.** `proxy_chain_failover_fired`,
   `proxy_chain_failover_recovered` and `proxy_budget_retry` carry the
   candidate + reason but not **how long** the attempt took — so a
   slow-then-empty cold model is indistinguishable from a fast empty in
   later analysis, even though they mean different NIM symptoms.

## What

1. **`llm_proxy/config/logging_config.py`** — remove the
   `Path(log_file).write_text("")` truncation so a restart **appends**
   to `server.log` instead of wiping it; add a bounded `retention`
   (default 30 days) to the file sink so rotated telemetry accumulates
   without unbounded disk growth. Update the docstring (the "truncates …
   for a clean slate" wording is now wrong).
2. **`llm_proxy/api/services.py`** — time each chain-walk attempt
   (`time.monotonic()` before `stream_response`, elapsed at the
   failover / success / budget-retry decision) and add a rounded
   `latency_s` field to:
   - `proxy_chain_failover_fired` (both the transport-exception and the
     empty-completion branches),
   - `proxy_chain_failover_recovered`,
   - `proxy_budget_retry` (in `_retry_with_more_budget`).
   No control-flow change — purely additive fields.

## Files in scope

- `src/ferova/llm_proxy/config/logging_config.py`
- `src/ferova/llm_proxy/api/services.py`
- `tests/unit/test_proxy_logging_durability.py` (new)
- `tests/unit/test_proxy_failover_events.py` (assert the new latency field)

## Out of scope

- Routing proxy telemetry into SQLite (the on-disk rotated logs are the
  store for now; a future slice can ingest them).
- New event types or renaming existing ones.
- Log compression of rotated files (kept plain for easy analysis).

## Smoke scenario

Writing a line to a temp log file, then calling
`configure_logging(path, force=True)`, leaves the prior line in place
(append, not truncate) and adds new records after it. Driving
`_stream_with_failover` with a first candidate that empties and a second
that serves content, the captured `proxy_chain_failover_fired` record
carries a numeric `latency_s`.

## Definition of Done

- `configure_logging` no longer truncates an existing log file; prior
  content survives a reconfigure, and a missing log dir is created —
  `test_proxy_logging_durability.py`
  (`test_configure_logging_appends_not_truncates`,
  `test_configure_logging_creates_missing_log_dir`).
- Both `proxy_chain_failover_fired` branches (transport-exception +
  empty-completion) and `proxy_chain_failover_recovered` carry a numeric
  `latency_s` — asserted in the existing event tests in
  `test_proxy_failover_events.py`.
- `ruff` + `ruff format --check` + full `pytest tests/unit` green; zero
  inline comments; no `# noqa`. Global loguru state is restored after
  any test that calls `configure_logging`.

## Commit plan

1. `feat(proxy): stop wiping server.log on restart + add 30d retention`
2. `feat(proxy): add per-attempt latency_s to chain-walk telemetry events`
3. `test(proxy): log durability + failover-event latency`

## Risks

- **Disk growth**: append + 50 MB rotation + 30-day retention bounds it;
  proxy traffic is modest (reviews), so a month stays small.
- **Global loguru state in tests**: any test touching
  `configure_logging` must save/restore `_configured` and remove the
  sink it adds, or it pollutes sibling tests (see
  [[test-pollution-via-default-io-sinks]]).
