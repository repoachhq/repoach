# SP-CHAIN-STATUS-DIGEST — ferova chain-status — operator-visible chain digest wired to session start

Add a chain_status_window_h settings knob, implement the pure async build_chain_status aggregator that composes fetch_probes, the /health breaker snapshot, cell-probe freshness and OpenRouter credits into a stable stdout digest, expose it as the thin `ferova chain-status` CLI command with fail-open (exit 0 always) semantics, and wire it into the tracked SessionStart hook with end-to-end coverage so every Claude session opens on the digest.

## Step 1 — Add chain_status_window_h settings knob

- **Files**: `src/ferova/llm_proxy/config/settings.py`, `tests/unit/test_settings_sharp_prefix_aliases.py`
- **Action**: In src/ferova/llm_proxy/config/settings.py, next to the existing credits_floor_usd/credits_health_cache_ttl_s knobs, add `chain_status_window_h: float = Field(default=24.0, validation_alias=_aliases("CHAIN_STATUS_WINDOW_H"))` so the digest's window defaults from FEROVA_CHAIN_STATUS_WINDOW_H (or the legacy CHAIN_STATUS_WINDOW_H alias), matching how the credits knobs are wired. In tests/unit/test_settings_sharp_prefix_aliases.py add a "CHAIN_STATUS_WINDOW_H": "chain_status_window_h" pair to the existing alias-coverage table, and add a new test function named test_chain_status_window_h_alias_and_default that asserts Settings().chain_status_window_h == 24.0 by default and that setting FEROVA_CHAIN_STATUS_WINDOW_H="6" overrides it to 6.0.
- **Commit**: `feat(config): add chain_status_window_h settings knob`
- **Done when**: pytest tests/unit/test_settings_sharp_prefix_aliases.py::test_chain_status_window_h_alias_and_default -q passes
- **Unit tests**: `tests/unit/test_settings_sharp_prefix_aliases.py::test_chain_status_window_h_alias_and_default`

## Step 2 — Implement build_chain_status pure aggregation function

- **Files**: `src/ferova/cli/chain_status.py`, `tests/unit/test_chain_status.py`
- **Action**: Create src/ferova/cli/chain_status.py with `async def build_chain_status(db_path, window_h, *, proxy_url, client, settings) -> str`. Per tier (opus/sonnet/haiku): call health.store.fetch_probes(db_path, since=now-window_h, tier=tier), exclude status==skipped rows from the mix and from n=, compute ok/slow/err (err=error+empty) percentages over actual counts, render an `avg slow <x>s` figure as the mean latency_s of the window's slow rows (omitted when none); zero probes renders `no probes in window`. Resolve each tier's head via review.chain_health.chain_head against settings.model_opus/model_sonnet/model_haiku; a non-nvidia_nim head renders `UNMONITORED (probe skips non-NIM heads)`. GET f"{proxy_url}/health" through the injected httpx.AsyncClient for the breaker snapshot; map each breaker ref to a tier by matching against that tier's chain, refs matching no chain render under `breaker (unchained):`; any non-2xx or malformed response renders `proxy: unreachable (breaker state unknown)` with the HTTP status when available. Render cell freshness via providers.cell_probe_store.fetch_cell_probes' newest recorded_at (`cells: newest <age> ago` / `no probes recorded`). Render credits by calling health.credits.fetch_openrouter_credits directly through the injected client when a key is configured (else `credits: skipped (no key)`); a None result renders `credits: unavailable`; otherwise render remaining/floor with LOW flagged below settings.credits_floor_usd. Add tests/unit/test_chain_status.py with test_nominal_tier_mix_and_avg_slow_line (seeds a tmp-path SQLite via health.store.record_probes with 5 ok/2 slow/2 error/1 empty sonnet rows, asserts the exact `50% ok · 20% slow · 30% err` with `n=10` and the avg-slow rendering), test_unmonitored_head_warning (a non-NIM sonnet head renders UNMONITORED), test_breaker_mapping_chained_and_unchained (a `/health` snapshot with a chained sonnet ref and an unchained ref render on their respective lines), and test_credits_none_renders_unavailable (a credits fetch returning None renders `credits: unavailable`), each driving an httpx.AsyncClient built with httpx.MockTransport as the truthful boundary fake for /health and credits.
- **Commit**: `feat(cli): add build_chain_status pure aggregation function`
- **Done when**: pytest tests/unit/test_chain_status.py -q passes
- **Unit tests**: `tests/unit/test_chain_status.py::test_nominal_tier_mix_and_avg_slow_line`, `tests/unit/test_chain_status.py::test_unmonitored_head_warning`, `tests/unit/test_chain_status.py::test_breaker_mapping_chained_and_unchained`, `tests/unit/test_chain_status.py::test_credits_none_renders_unavailable`

## Step 3 — Add ferova chain-status CLI command with fail-open exit semantics

- **Files**: `src/ferova/cli/chain_status.py`, `src/ferova/cli/main.py`, `tests/unit/test_chain_status.py`
- **Action**: In src/ferova/cli/chain_status.py add a thin Typer command `chain-status` accepting `--window-hours` (default settings.chain_status_window_h), `--db-path` (mirrors monitor-chains' existing override), and `--proxy-url` (default http://127.0.0.1:8082); it builds one httpx.AsyncClient, calls `asyncio.run(build_chain_status(...))`, echoes the resulting digest, and always exits 0 (G4) by catching any unexpected exception before it can surface as a traceback. Register the command in src/ferova/cli/main.py via `app.command(name="chain-status")`. Add to tests/unit/test_chain_status.py a test named test_cli_argv_parsing_and_exit_zero using CliRunner with FEROVA_OPENROUTER_API_KEY="" pinned in the runner env, asserting `--window-hours`, `--db-path`, and `--proxy-url` are all accepted and the process exits 0, and a test named test_cli_degradation_matrix_unreachable_proxy_and_empty_db that points `--proxy-url` at an unbound localhost port and `--db-path` at a fresh tmp path, asserting exit 0, the `proxy: unreachable` and `no probes in window` lines render, and no traceback appears on stderr.
- **Commit**: `feat(cli): wire ferova chain-status command with fail-open degradation`
- **Done when**: pytest tests/unit/test_chain_status.py -q passes
- **Unit tests**: `tests/unit/test_chain_status.py::test_cli_argv_parsing_and_exit_zero`, `tests/unit/test_chain_status.py::test_cli_degradation_matrix_unreachable_proxy_and_empty_db`

## Step 4 — Wire chain-status into the tracked SessionStart hook with e2e coverage

- **Files**: `.claude/settings.json`, `tests/unit/test_chain_status_hook.py`, `tests/integration/test_chain_status_cli.py`
- **Action**: In .claude/settings.json add a SessionStart hook command that runs the digest (e.g. `ferova chain-status || true`) alongside the existing dream_check.py hook, so a broken venv can never block a session (G3, G4). Add tests/unit/test_chain_status_hook.py with a test named test_session_start_hook_includes_chain_status_command that resolves the repo root, `json.load`s the tracked .claude/settings.json, and asserts a SessionStart hook command contains both `chain-status` and `|| true`, following the repo-file-assertion pattern of tests/unit/test_dream_check_hook.py. Add tests/integration/test_chain_status_cli.py with a test named test_chain_status_end_to_end_degraded_environment that invokes the installed `ferova chain-status` command as a subprocess against a fresh tmp-path db and an unbound localhost proxy port, asserting exit code 0, the expected degraded digest lines (`no probes in window`, `proxy: unreachable`), and no traceback on stderr.
- **Commit**: `feat(cli): wire chain-status into SessionStart hook with e2e coverage`
- **Done when**: pytest tests/unit/test_chain_status_hook.py tests/integration/test_chain_status_cli.py -q passes
- **Unit tests**: `tests/unit/test_chain_status_hook.py::test_session_start_hook_includes_chain_status_command`

## Integration tests

- `tests/integration/test_chain_status_cli.py::test_chain_status_end_to_end_degraded_environment`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-CHAIN-STATUS-DIGEST",
  "title": "ferova chain-status — operator-visible chain digest wired to session start",
  "summary": "Add a chain_status_window_h settings knob, implement the pure async build_chain_status aggregator that composes fetch_probes, the /health breaker snapshot, cell-probe freshness and OpenRouter credits into a stable stdout digest, expose it as the thin `ferova chain-status` CLI command with fail-open (exit 0 always) semantics, and wire it into the tracked SessionStart hook with end-to-end coverage so every Claude session opens on the digest.",
  "steps": [
    {
      "index": 1,
      "title": "Add chain_status_window_h settings knob",
      "files": [
        "src/ferova/llm_proxy/config/settings.py",
        "tests/unit/test_settings_sharp_prefix_aliases.py"
      ],
      "action": "In src/ferova/llm_proxy/config/settings.py, next to the existing credits_floor_usd/credits_health_cache_ttl_s knobs, add `chain_status_window_h: float = Field(default=24.0, validation_alias=_aliases(\"CHAIN_STATUS_WINDOW_H\"))` so the digest's window defaults from FEROVA_CHAIN_STATUS_WINDOW_H (or the legacy CHAIN_STATUS_WINDOW_H alias), matching how the credits knobs are wired. In tests/unit/test_settings_sharp_prefix_aliases.py add a \"CHAIN_STATUS_WINDOW_H\": \"chain_status_window_h\" pair to the existing alias-coverage table, and add a new test function named test_chain_status_window_h_alias_and_default that asserts Settings().chain_status_window_h == 24.0 by default and that setting FEROVA_CHAIN_STATUS_WINDOW_H=\"6\" overrides it to 6.0.",
      "commit_message": "feat(config): add chain_status_window_h settings knob",
      "done_when": "pytest tests/unit/test_settings_sharp_prefix_aliases.py::test_chain_status_window_h_alias_and_default -q passes",
      "unit_tests": [
        "tests/unit/test_settings_sharp_prefix_aliases.py::test_chain_status_window_h_alias_and_default"
      ]
    },
    {
      "index": 2,
      "title": "Implement build_chain_status pure aggregation function",
      "files": [
        "src/ferova/cli/chain_status.py",
        "tests/unit/test_chain_status.py"
      ],
      "action": "Create src/ferova/cli/chain_status.py with `async def build_chain_status(db_path, window_h, *, proxy_url, client, settings) -> str`. Per tier (opus/sonnet/haiku): call health.store.fetch_probes(db_path, since=now-window_h, tier=tier), exclude status==skipped rows from the mix and from n=, compute ok/slow/err (err=error+empty) percentages over actual counts, render an `avg slow <x>s` figure as the mean latency_s of the window's slow rows (omitted when none); zero probes renders `no probes in window`. Resolve each tier's head via review.chain_health.chain_head against settings.model_opus/model_sonnet/model_haiku; a non-nvidia_nim head renders `UNMONITORED (probe skips non-NIM heads)`. GET f\"{proxy_url}/health\" through the injected httpx.AsyncClient for the breaker snapshot; map each breaker ref to a tier by matching against that tier's chain, refs matching no chain render under `breaker (unchained):`; any non-2xx or malformed response renders `proxy: unreachable (breaker state unknown)` with the HTTP status when available. Render cell freshness via providers.cell_probe_store.fetch_cell_probes' newest recorded_at (`cells: newest <age> ago` / `no probes recorded`). Render credits by calling health.credits.fetch_openrouter_credits directly through the injected client when a key is configured (else `credits: skipped (no key)`); a None result renders `credits: unavailable`; otherwise render remaining/floor with LOW flagged below settings.credits_floor_usd. Add tests/unit/test_chain_status.py with test_nominal_tier_mix_and_avg_slow_line (seeds a tmp-path SQLite via health.store.record_probes with 5 ok/2 slow/2 error/1 empty sonnet rows, asserts the exact `50% ok · 20% slow · 30% err` with `n=10` and the avg-slow rendering), test_unmonitored_head_warning (a non-NIM sonnet head renders UNMONITORED), test_breaker_mapping_chained_and_unchained (a `/health` snapshot with a chained sonnet ref and an unchained ref render on their respective lines), and test_credits_none_renders_unavailable (a credits fetch returning None renders `credits: unavailable`), each driving an httpx.AsyncClient built with httpx.MockTransport as the truthful boundary fake for /health and credits.",
      "commit_message": "feat(cli): add build_chain_status pure aggregation function",
      "done_when": "pytest tests/unit/test_chain_status.py -q passes",
      "unit_tests": [
        "tests/unit/test_chain_status.py::test_nominal_tier_mix_and_avg_slow_line",
        "tests/unit/test_chain_status.py::test_unmonitored_head_warning",
        "tests/unit/test_chain_status.py::test_breaker_mapping_chained_and_unchained",
        "tests/unit/test_chain_status.py::test_credits_none_renders_unavailable"
      ]
    },
    {
      "index": 3,
      "title": "Add ferova chain-status CLI command with fail-open exit semantics",
      "files": [
        "src/ferova/cli/chain_status.py",
        "src/ferova/cli/main.py",
        "tests/unit/test_chain_status.py"
      ],
      "action": "In src/ferova/cli/chain_status.py add a thin Typer command `chain-status` accepting `--window-hours` (default settings.chain_status_window_h), `--db-path` (mirrors monitor-chains' existing override), and `--proxy-url` (default http://127.0.0.1:8082); it builds one httpx.AsyncClient, calls `asyncio.run(build_chain_status(...))`, echoes the resulting digest, and always exits 0 (G4) by catching any unexpected exception before it can surface as a traceback. Register the command in src/ferova/cli/main.py via `app.command(name=\"chain-status\")`. Add to tests/unit/test_chain_status.py a test named test_cli_argv_parsing_and_exit_zero using CliRunner with FEROVA_OPENROUTER_API_KEY=\"\" pinned in the runner env, asserting `--window-hours`, `--db-path`, and `--proxy-url` are all accepted and the process exits 0, and a test named test_cli_degradation_matrix_unreachable_proxy_and_empty_db that points `--proxy-url` at an unbound localhost port and `--db-path` at a fresh tmp path, asserting exit 0, the `proxy: unreachable` and `no probes in window` lines render, and no traceback appears on stderr.",
      "commit_message": "feat(cli): wire ferova chain-status command with fail-open degradation",
      "done_when": "pytest tests/unit/test_chain_status.py -q passes",
      "unit_tests": [
        "tests/unit/test_chain_status.py::test_cli_argv_parsing_and_exit_zero",
        "tests/unit/test_chain_status.py::test_cli_degradation_matrix_unreachable_proxy_and_empty_db"
      ]
    },
    {
      "index": 4,
      "title": "Wire chain-status into the tracked SessionStart hook with e2e coverage",
      "files": [
        ".claude/settings.json",
        "tests/unit/test_chain_status_hook.py",
        "tests/integration/test_chain_status_cli.py"
      ],
      "action": "In .claude/settings.json add a SessionStart hook command that runs the digest (e.g. `ferova chain-status || true`) alongside the existing dream_check.py hook, so a broken venv can never block a session (G3, G4). Add tests/unit/test_chain_status_hook.py with a test named test_session_start_hook_includes_chain_status_command that resolves the repo root, `json.load`s the tracked .claude/settings.json, and asserts a SessionStart hook command contains both `chain-status` and `|| true`, following the repo-file-assertion pattern of tests/unit/test_dream_check_hook.py. Add tests/integration/test_chain_status_cli.py with a test named test_chain_status_end_to_end_degraded_environment that invokes the installed `ferova chain-status` command as a subprocess against a fresh tmp-path db and an unbound localhost proxy port, asserting exit code 0, the expected degraded digest lines (`no probes in window`, `proxy: unreachable`), and no traceback on stderr.",
      "commit_message": "feat(cli): wire chain-status into SessionStart hook with e2e coverage",
      "done_when": "pytest tests/unit/test_chain_status_hook.py tests/integration/test_chain_status_cli.py -q passes",
      "unit_tests": [
        "tests/unit/test_chain_status_hook.py::test_session_start_hook_includes_chain_status_command"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_chain_status_cli.py::test_chain_status_end_to_end_degraded_environment"
  ]
}
```
