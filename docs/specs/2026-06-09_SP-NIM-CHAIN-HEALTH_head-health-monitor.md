# SP-NIM-CHAIN-HEALTH — probe each chain's head model and classify its health

## Metadata

- **Status**: OPEN
- **Priority**: P2
- **Owner**: operator
- **Executor**: hand-implemented
- **Opened**: 2026-06-09

## Why

The hosted NIM is volatile: a head model can silently start **thinking-
leaking** (HTTP 200 but empty content — the budget consumed by hidden,
stripped reasoning) or go cold (latency spikes from <1s to >30s), as
`qwen/qwen3.5-122b-a10b` did on 2026-06-09. SP-PROXY-THINKING-BUDGET-RETRY
now makes the chain *self-heal* (a budget-starved head is retried with
headroom, else failover takes over), but the operator has **no
visibility** into which head is drifting — the self-healing hides it.

This slice adds observability: a one-shot probe of each capability
tier's head model that classifies its health, so a drifting model is
surfaced (and can be acted on) before/while the proxy papers over it.

## What

A `ferova monitor-chains` CLI command that, for each capability tier
(`opus` / `sonnet` / `haiku` / `coder`), reads the configured chain
(`Settings.model_*`), takes the **head** entry, and — when it is a
`nvidia_nim/<model>` head — probes that model directly against the NIM
`/chat/completions` endpoint with a small fixed prompt, then classifies:

- `ok` — non-empty content within the latency threshold;
- `slow` — non-empty content but latency above `--slow-threshold`
  (default 8s);
- `empty` — HTTP 200 with empty/whitespace content (the thinking-leak
  signature);
- `error` — non-2xx, timeout, or transport failure.

Print a per-tier report (human table by default, `--json` for machines)
carrying `tier`, `model`, `status`, `latency_s`, `content_chars`. Heads
that are not `nvidia_nim/*` (e.g. `claude_code/*`) are reported as
`skipped` (out of scope for NIM drift). Emit one structured
`nim_chain_health` log line per tier so a cron wrapper can alert.

1. **`src/ferova/review/chain_health.py`** (new):
   - `ModelHealth` dataclass (`tier`, `model`, `status`, `latency_s`,
     `content_chars`, `detail`).
   - `classify(status_code, latency_s, content, slow_threshold_s)` — the
     pure classification rule (the testable core).
   - `async probe_nim_model(client, base_url, api_key, model, *, prompt,
     max_tokens, timeout_s, slow_threshold_s) -> ModelHealth` — POST,
     time it, extract `choices[0].message.content`, classify. Never
     raises — a transport/timeout error returns `status="error"`.
   - `chain_head(chain_value)` — return the head `provider/model` of a
     comma-separated chain string.
   - `async check_tier_heads(settings, *, client, ...) -> list[ModelHealth]`
     — probe every tier head; non-NIM heads → `status="skipped"`.
2. **`src/ferova/cli/main.py`** — register `monitor-chains`
   (`--json`, `--slow-threshold`, `--max-tokens`). Exit code `0` when
   every NIM head is `ok`/`slow`/`skipped`, `1` when any head is `empty`
   or `error` (so a cron/monitor can gate on it).

## Files in scope

- `src/ferova/review/chain_health.py` (new)
- `src/ferova/cli/main.py`
- `tests/unit/test_chain_health.py` (new)

## Out of scope

- Auto-demoting or rewriting chains (`chains.env` stays operator-owned).
- Probing via the proxy (this measures the raw NIM head, deliberately
  bypassing the budget-retry/failover that would mask the drift).
- A systemd timer / alerting transport (a later ops slice).
- Probing non-NIM providers.

## Smoke scenario

A fake async HTTP client (injected) scripted to return, per model: a
content body, an empty-content 200, and a raised timeout. Driving
`check_tier_heads` with a two-tier settings fixture yields the expected
`ok` / `empty` / `error` statuses, and `classify` maps a slow-but-present
response to `slow`.

## Definition of Done

- `classify` maps (2xx + content + fast) → `ok`, (2xx + content + slow)
  → `slow`, (2xx + empty/whitespace) → `empty`, (non-2xx / exception
  sentinel) → `error` — pinned by
  `tests/unit/test_chain_health.py::test_classify_rules`.
- `probe_nim_model` never raises on a transport error/timeout and
  returns `status="error"` —
  `test_probe_returns_error_on_transport_failure`.
- `check_tier_heads` probes only `nvidia_nim/*` heads and marks others
  `skipped` — `test_non_nim_head_is_skipped`.
- A scripted empty-content head yields `status="empty"` end-to-end —
  `test_empty_content_head_classified_empty`.
- `monitor-chains --json` prints one object per tier and exits non-zero
  when any NIM head is `empty`/`error` —
  `test_cli_exit_code_reflects_worst_status`.
- `ruff` + `ruff format --check` + full `pytest tests/unit` green; zero
  inline comments; no `# noqa`.

## Commit plan

1. `feat(monitor): NIM chain head-health probe + classification`
2. `feat(cli): ferova monitor-chains (--json, exit code gates on drift)`
3. `test(monitor): classify rules + probe error-safety + tier skip + CLI exit`

## Risks

- **Live network in tests**: the probe must take an injected client so
  unit tests never hit NIM (the no-live-network sentinel rule). The
  default client is built only inside the CLI command.
- **Cost**: each probe spends a handful of tokens on Max-free NIM; the
  command is one-shot and operator-run, not a tight loop.
