# SP-CREDITS-CHECK — OpenRouter credits floor — probe module, CLI integration, /health surface

Create `src/ferova/health/credits.py` (CreditsSnapshot, fetch_openrouter_credits, get_cached_credits, reset_credits_cache) and its unit test module `tests/unit/test_credits.py` — the only two NEW files (AC5: ≤2 new files). Add credits settings to the llm_proxy Settings. Wire the credits check into `monitor-chains` behind a `_probe_client` factory seam. Add the `credits` field to `GET /health` behind a `get_credits_client` dependency with a TTL-cached lazy fetch. All test additions beyond `test_credits.py` land IN the existing suites the spec names (`test_chain_health.py`, `test_health_breaker.py`, `test_arch_graph.py`). The SP-HEALTH-STORE-NEUTRALIZE ownership narrowing is ALREADY in the tree (verified 2026-07-16; `arch graph --check` is clean) — no spec file is edited.

## Step 1 — Create credits module, settings, and unit tests

- **Files**: `src/ferova/health/credits.py`, `src/ferova/llm_proxy/config/settings.py`, `tests/unit/test_credits.py`
- **Action**: Add `FEROVA_CREDITS_FLOOR_USD` and `FEROVA_CREDITS_HEALTH_CACHE_TTL_S` to `_LEGACY_TO_FEROVA_ALIAS` and two Field declarations (`credits_floor_usd: float = 2.0`, `credits_health_cache_ttl_s: float = 3600.0`) in the llm_proxy `Settings` class. Create `src/ferova/health/credits.py` (≤100 LOC, imports only httpx/pydantic/logging; do NOT touch `src/ferova/health/__init__.py`): a frozen pydantic `CreditsSnapshot` with `total_credits: float`, `total_usage: float`, and a computed `remaining: float` (`total_credits - total_usage`, not clamped); `async fetch_openrouter_credits(api_key, *, client, timeout_s=10.0) -> CreditsSnapshot | None` doing one GET to `https://openrouter.ai/api/v1/credits` with `Authorization: Bearer <key>`, returning `None` on any transport error / non-2xx / unexpected payload (structlog warning `openrouter_credits_unavailable`), never raising; `async get_cached_credits(api_key, *, client, ttl_s, timeout_s=3.0) -> CreditsSnapshot | None` with a module-level `(snapshot, fetched_at_monotonic)` cache (on a `None` result, cache the failure for `min(60.0, ttl_s)` seconds and serve `None`, never stale); `reset_credits_cache()` mirroring `reset_breaker`. Create `tests/unit/test_credits.py` using `httpx.AsyncClient(transport=httpx.MockTransport(...))` (truthful boundary fake — no monkeypatching of ferova code): nominal payload → correct snapshot; parametrized failures (500, malformed JSON, missing keys, timeout) → `None`; cache hit, expiry, failure-caching window, and `reset_credits_cache` behaviour.
- **Commit**: `feat(health): add OpenRouter credits probe module with cached accessor and settings`
- **Done when**: `pytest tests/unit/test_credits.py -v` passes and `ruff check src/ferova/health/credits.py` exits 0
- **Unit tests**: `tests/unit/test_credits.py::test_fetch_nominal_returns_snapshot`, `tests/unit/test_credits.py::test_fetch_errors_return_none`, `tests/unit/test_credits.py::test_cache_ttl_expiry_and_reset`

## Step 2 — Integrate credits into monitor-chains CLI

- **Files**: `src/ferova/cli/main.py`, `tests/unit/test_chain_health.py`, `tests/integration/test_credits_integration.py`
- **Action**: In `src/ferova/cli/main.py` extract a module-level `_probe_client() -> httpx.AsyncClient` factory and use it BOTH for the tier probes (today constructed inline at ~`cli/main.py:105`) and for the credits check. After the tier-probe+persist block, when `settings.open_router_api_key` is set, call `fetch_openrouter_credits` with that client; compute status (`ok` / `LOW` / `unavailable` / `skipped`); log a structlog `openrouter_credits` event (`remaining`, `floor`, `status`); print the stdout line `credits open_router [remaining=<x> floor=<y>] <status>` (bracket fields only when a snapshot exists); under `--json` suppress the plain line and append a trailing final array element `{"kind":"credits","status":...,"total_credits":x|null,"total_usage":y|null,"remaining":z|null,"floor":f}`. Only a confirmed `LOW` degrades the run via the existing `typer.Exit(1)` path; `unavailable` and `skipped` never degrade. In `tests/unit/test_chain_health.py` neutralize the credits path in the two existing tests `test_cli_exit_code_reflects_worst_status` and `test_cli_exit_zero_when_all_healthy` by pinning `FEROVA_OPENROUTER_API_KEY` empty (via `monkeypatch.setenv`, which beats the repo `.env`), then add the CLI credits unit tests IN THIS SAME FILE, each overriding the `_probe_client` factory with a `MockTransport`-backed client answering both the NIM probe POSTs and the credits GET: `test_credits_low_exits_1` (healthy heads + `remaining < floor` → exit 1, `LOW` line), `test_credits_ok_exits_0` (healthy + sufficient → exit 0, `ok` line), `test_credits_skipped_when_key_empty` (empty key → `skipped` line, no credits GET recorded by the transport), `test_credits_json_output_shape` (`--json` → trailing `kind="credits"` object shape). Create the NEW `tests/integration/test_credits_integration.py` with `test_cli_credits_low_end_to_end`: a full `CliRunner` `monitor-chains` invocation with a `MockTransport`-backed `_probe_client` answering the NIM probe POSTs and a below-floor credits GET, asserting exit code 1 and the `LOW` line on stdout — the src-touching integration coverage the plan gate requires.
- **Commit**: `feat(cli): surface OpenRouter credits floor in monitor-chains`
- **Done when**: `pytest tests/unit/test_chain_health.py tests/integration/test_credits_integration.py -v` passes (the two edited unit tests stay offline and balance-independent) and `ruff check src/ferova/cli/main.py` exits 0
- **Unit tests**: `tests/unit/test_chain_health.py::test_credits_low_exits_1`, `tests/unit/test_chain_health.py::test_credits_ok_exits_0`, `tests/unit/test_chain_health.py::test_credits_skipped_when_key_empty`, `tests/unit/test_chain_health.py::test_credits_json_output_shape`

## Step 3 — Add credits surface to GET /health behind a dependency seam

- **Files**: `src/ferova/llm_proxy/api/dependencies.py`, `src/ferova/llm_proxy/api/routes.py`, `tests/unit/test_health_breaker.py`
- **Action**: In `src/ferova/llm_proxy/api/dependencies.py` add a `get_credits_client()` dependency returning `httpx.AsyncClient()` (overridable via `app.dependency_overrides`, mirroring the existing `get_settings` pattern). In `src/ferova/llm_proxy/api/routes.py` extend the `/health` handler: obtain the client via `Depends(get_credits_client)` and call `get_cached_credits(settings.open_router_api_key, client=..., ttl_s=settings.credits_health_cache_ttl_s, timeout_s=3.0)`; serve `"credits": {"open_router": {"total_credits":x,"total_usage":y,"remaining":z}}` when a snapshot exists, else `"credits": null` (empty key or `None` result); the endpoint's existing fields and its 200 status are unaffected on every path. Add the tests IN THE EXISTING `tests/unit/test_health_breaker.py` (it already drives `/health` via `TestClient` and calls `reset_breaker`): call `reset_credits_cache()` in setup, pin the API key non-empty through the settings dependency override and override `get_credits_client` with a `MockTransport`-backed client; assert two `/health` calls within the TTL perform exactly one upstream GET (transport call count), and that an upstream failure yields `"credits": null` with HTTP 200. Add `test_get_credits_client_returns_async_client` asserting the dependency returns an `httpx.AsyncClient`.
- **Commit**: `feat(health): expose OpenRouter credits in GET /health`
- **Done when**: `pytest tests/unit/test_health_breaker.py -v` passes and `ruff check src/ferova/llm_proxy/api/routes.py src/ferova/llm_proxy/api/dependencies.py` exits 0
- **Unit tests**: `tests/unit/test_health_breaker.py::test_health_credits_cached_single_fetch`, `tests/unit/test_health_breaker.py::test_health_credits_null_on_upstream_failure`, `tests/unit/test_health_breaker.py::test_get_credits_client_returns_async_client`

## Step 4 — Pin ownership disjointness for the new module

- **Files**: `tests/unit/test_arch_graph.py`
- **Action**: The SP-HEALTH-STORE-NEUTRALIZE ownership narrowing is ALREADY in the tree (its `owns.code` is per-module: `__init__.py`, `model_health.py`, `store.py`; `arch graph --check` is clean) — do NOT edit any spec file. Add ONE regression test IN THE EXISTING `tests/unit/test_arch_graph.py` (which already builds a registry over the real specs, e.g. `test_full_run_over_real_specs_does_not_crash`): `test_health_credits_ownership_is_disjoint` loads the real registry and asserts `owner_of("src/ferova/health/credits.py")` resolves to `SP-CREDITS-CHECK` while `owner_of("src/ferova/health/store.py")` resolves to `SP-HEALTH-STORE-NEUTRALIZE`, proving the per-module split holds once `credits.py` exists (AC6).
- **Commit**: `test(arch): pin health-module ownership disjointness for credits.py`
- **Done when**: `pytest tests/unit/test_arch_graph.py -v` passes and `ferova arch graph --check` exits 0
- **Unit tests**: `tests/unit/test_arch_graph.py::test_health_credits_ownership_is_disjoint`

## Integration tests

- `tests/integration/test_credits_integration.py::test_cli_credits_low_end_to_end` — full `monitor-chains` run through `CliRunner` with a `MockTransport`-backed probe client; below-floor credits → exit 1 + `LOW` line.

**AC5 deviation (flagged):** the spec's AC5 caps new files at two (`credits.py` + one test module), but the plan-form validator hard-requires a `tests/integration/` selector for any src-touching plan (`ActionPlan` rejects `integration_tests: []`), and no existing integration suite is a natural home for a credits end-to-end. This plan therefore adds THREE new files — `src/ferova/health/credits.py`, `tests/unit/test_credits.py`, `tests/integration/test_credits_integration.py` — one over AC5. All other acceptance checks stay in-place in the existing `test_chain_health.py` / `test_health_breaker.py` / `test_arch_graph.py` suites the spec designates. The AC5 prose is not gate-enforced; this deviation is the minimal way to satisfy both AC1 (helper unit tests) and the integration-coverage validator.

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-CREDITS-CHECK",
  "title": "OpenRouter credits floor — probe module, CLI integration, /health surface",
  "summary": "Create src/ferova/health/credits.py (CreditsSnapshot, fetch_openrouter_credits, get_cached_credits, reset_credits_cache) and its unit test module tests/unit/test_credits.py — the only two NEW files (AC5). Add credits_floor_usd + credits_health_cache_ttl_s to the llm_proxy Settings. Wire the credits check into monitor-chains behind a _probe_client factory seam. Add a credits field to GET /health behind a get_credits_client dependency with a TTL-cached lazy fetch. Every acceptance check beyond test_credits.py lands in the existing suites the spec names (test_chain_health.py, test_health_breaker.py, test_arch_graph.py). The SP-HEALTH-STORE-NEUTRALIZE ownership narrowing is already in the tree (arch graph --check clean) — no spec file is edited.",
  "steps": [
    {
      "index": 1,
      "title": "Create credits module, settings, and unit tests",
      "files": [
        "src/ferova/health/credits.py",
        "src/ferova/llm_proxy/config/settings.py",
        "tests/unit/test_credits.py"
      ],
      "action": "Add FEROVA_CREDITS_FLOOR_USD and FEROVA_CREDITS_HEALTH_CACHE_TTL_S to _LEGACY_TO_FEROVA_ALIAS and two Field declarations (credits_floor_usd: float = 2.0, credits_health_cache_ttl_s: float = 3600.0) in the llm_proxy Settings class. Create src/ferova/health/credits.py (<=100 LOC, imports only httpx/pydantic/logging; do NOT touch src/ferova/health/__init__.py): a frozen pydantic CreditsSnapshot with total_credits: float, total_usage: float, and a computed remaining: float (total_credits - total_usage, not clamped); async fetch_openrouter_credits(api_key, *, client, timeout_s=10.0) -> CreditsSnapshot | None doing one GET to https://openrouter.ai/api/v1/credits with Authorization: Bearer <key>, returning None on any transport error / non-2xx / unexpected payload (structlog warning openrouter_credits_unavailable), never raising; async get_cached_credits(api_key, *, client, ttl_s, timeout_s=3.0) -> CreditsSnapshot | None with a module-level (snapshot, fetched_at_monotonic) cache (on a None result, cache the failure for min(60.0, ttl_s) seconds and serve None, never stale); reset_credits_cache() mirroring reset_breaker. Create tests/unit/test_credits.py using httpx.AsyncClient(transport=httpx.MockTransport(...)) (truthful boundary fake, no monkeypatching of ferova code): nominal payload -> correct snapshot; parametrized failures (500, malformed JSON, missing keys, timeout) -> None; cache hit, expiry, failure-caching window, and reset_credits_cache behaviour.",
      "commit_message": "feat(health): add OpenRouter credits probe module with cached accessor and settings",
      "done_when": "pytest tests/unit/test_credits.py -v passes and ruff check src/ferova/health/credits.py exits 0",
      "unit_tests": [
        "tests/unit/test_credits.py::test_fetch_nominal_returns_snapshot",
        "tests/unit/test_credits.py::test_fetch_errors_return_none",
        "tests/unit/test_credits.py::test_cache_ttl_expiry_and_reset"
      ]
    },
    {
      "index": 2,
      "title": "Integrate credits into monitor-chains CLI",
      "files": [
        "src/ferova/cli/main.py",
        "tests/unit/test_chain_health.py",
        "tests/integration/test_credits_integration.py"
      ],
      "action": "In src/ferova/cli/main.py extract a module-level _probe_client() -> httpx.AsyncClient factory and use it BOTH for the tier probes (today inline at ~cli/main.py:105) and for the credits check. After the tier-probe+persist block, when settings.open_router_api_key is set, call fetch_openrouter_credits with that client; compute status (ok/LOW/unavailable/skipped); log a structlog openrouter_credits event (remaining, floor, status); print the stdout line 'credits open_router [remaining=<x> floor=<y>] <status>' (bracket fields only when a snapshot exists); under --json suppress the plain line and append a trailing final array element {\"kind\":\"credits\",\"status\":...,\"total_credits\":x|null,\"total_usage\":y|null,\"remaining\":z|null,\"floor\":f}. Only a confirmed LOW degrades the run via the existing typer.Exit(1) path; unavailable and skipped never degrade. In tests/unit/test_chain_health.py neutralize the credits path in the two existing tests test_cli_exit_code_reflects_worst_status and test_cli_exit_zero_when_all_healthy by pinning FEROVA_OPENROUTER_API_KEY empty via monkeypatch.setenv (beats the repo .env), then add the CLI credits unit tests IN THIS SAME FILE, each overriding the _probe_client factory with a MockTransport-backed client answering both the NIM probe POSTs and the credits GET: test_credits_low_exits_1 (healthy heads + remaining < floor -> exit 1, LOW line), test_credits_ok_exits_0 (healthy + sufficient -> exit 0, ok line), test_credits_skipped_when_key_empty (empty key -> skipped line, no credits GET recorded), test_credits_json_output_shape (--json -> trailing kind=credits object shape). Create the NEW tests/integration/test_credits_integration.py with test_cli_credits_low_end_to_end: a full CliRunner monitor-chains run with a MockTransport-backed _probe_client answering the NIM probe POSTs and a below-floor credits GET, asserting exit code 1 and the LOW line on stdout.",
      "commit_message": "feat(cli): surface OpenRouter credits floor in monitor-chains",
      "done_when": "pytest tests/unit/test_chain_health.py tests/integration/test_credits_integration.py -v passes (the two edited unit tests stay offline and balance-independent) and ruff check src/ferova/cli/main.py exits 0",
      "unit_tests": [
        "tests/unit/test_chain_health.py::test_credits_low_exits_1",
        "tests/unit/test_chain_health.py::test_credits_ok_exits_0",
        "tests/unit/test_chain_health.py::test_credits_skipped_when_key_empty",
        "tests/unit/test_chain_health.py::test_credits_json_output_shape"
      ]
    },
    {
      "index": 3,
      "title": "Add credits surface to GET /health behind a dependency seam",
      "files": [
        "src/ferova/llm_proxy/api/dependencies.py",
        "src/ferova/llm_proxy/api/routes.py",
        "tests/unit/test_health_breaker.py"
      ],
      "action": "In src/ferova/llm_proxy/api/dependencies.py add a get_credits_client() dependency returning httpx.AsyncClient() (overridable via app.dependency_overrides, mirroring get_settings). In src/ferova/llm_proxy/api/routes.py extend the /health handler: obtain the client via Depends(get_credits_client) and call get_cached_credits(settings.open_router_api_key, client=..., ttl_s=settings.credits_health_cache_ttl_s, timeout_s=3.0); serve 'credits': {'open_router': {'total_credits':x,'total_usage':y,'remaining':z}} when a snapshot exists, else 'credits': null (empty key or None result); the endpoint's existing fields and its 200 status are unaffected on every path. Add the tests IN THE EXISTING tests/unit/test_health_breaker.py (it already drives /health via TestClient and calls reset_breaker): call reset_credits_cache() in setup, pin the API key non-empty through the settings dependency override and override get_credits_client with a MockTransport-backed client; assert two /health calls within the TTL perform exactly one upstream GET (transport call count), and that an upstream failure yields 'credits': null with HTTP 200. Add test_get_credits_client_returns_async_client asserting the dependency returns an httpx.AsyncClient.",
      "commit_message": "feat(health): expose OpenRouter credits in GET /health",
      "done_when": "pytest tests/unit/test_health_breaker.py -v passes and ruff check src/ferova/llm_proxy/api/routes.py src/ferova/llm_proxy/api/dependencies.py exits 0",
      "unit_tests": [
        "tests/unit/test_health_breaker.py::test_health_credits_cached_single_fetch",
        "tests/unit/test_health_breaker.py::test_health_credits_null_on_upstream_failure",
        "tests/unit/test_health_breaker.py::test_get_credits_client_returns_async_client"
      ]
    },
    {
      "index": 4,
      "title": "Pin ownership disjointness for the new module",
      "files": [
        "tests/unit/test_arch_graph.py"
      ],
      "action": "The SP-HEALTH-STORE-NEUTRALIZE ownership narrowing is ALREADY in the tree (its owns.code is per-module: __init__.py, model_health.py, store.py; arch graph --check is clean) — do NOT edit any spec file. Add ONE regression test IN THE EXISTING tests/unit/test_arch_graph.py (which already builds a registry over the real specs, e.g. test_full_run_over_real_specs_does_not_crash): test_health_credits_ownership_is_disjoint loads the real registry and asserts owner_of('src/ferova/health/credits.py') resolves to SP-CREDITS-CHECK while owner_of('src/ferova/health/store.py') resolves to SP-HEALTH-STORE-NEUTRALIZE, proving the per-module split holds once credits.py exists (AC6).",
      "commit_message": "test(arch): pin health-module ownership disjointness for credits.py",
      "done_when": "pytest tests/unit/test_arch_graph.py -v passes and ferova arch graph --check exits 0",
      "unit_tests": [
        "tests/unit/test_arch_graph.py::test_health_credits_ownership_is_disjoint"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_credits_integration.py::test_cli_credits_low_end_to_end"
  ]
}
```
