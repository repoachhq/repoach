# SP-REGEN-FRESH-CELLS — Bounded in-cycle cell sweep + freshness refusal in gather_and_regenerate

Add a bounded per-provider cell health sweep inside gather_and_regenerate before reading probe rows, with per-provider caps, concurrency limits, pacing, 429 filtering, credits-floor skip for open_router, and a since-windowed read that refuses loudly (StaleCellsError → typer.Exit(1)) when the freshest row is older than max_cell_age_h or zero rows exist. New Settings fields, a pacing_s keyword on sweep_cell_health, and a ranking= keyword seam on gather_and_regenerate. Zero new source modules.

## Step 1 — Add regeneration sweep settings fields

- **Files**: `src/repoach/llm_proxy/config/settings.py`, `tests/unit/test_settings_sharp_prefix_aliases.py`
- **Action**: Add five new fields to Settings: regen_max_cell_age_h (float, default 12.0), regen_sweep_per_provider_cap (int, default 12), regen_sweep_per_provider_concurrency (int, default 2), regen_sweep_pacing_s (float, default 0.5), regen_sweep_retry_backoff_s (float, default 2.0). Add corresponding entries to _LEGACY_TO_REPOACH_ALIAS and use _aliases() for validation_alias. Follow existing pattern. In the EXISTING file tests/unit/test_settings_sharp_prefix_aliases.py: add the five new (legacy, field) entries to its _LEGACY_TO_FIELD dict — the pinning test test_alias_map_covers_every_field_with_proxy_alias fails otherwise — and add test_regen_sweep_aliases_present asserting the aliases resolve correctly.
- **Commit**: `feat(settings): add regen sweep bounding and freshness guard settings`
- **Done when**: python -c "from repoach.llm_proxy.config.settings import Settings; s = Settings(_env_file=None); assert s.regen_max_cell_age_h == 12.0; assert s.regen_sweep_per_provider_cap == 12; assert s.regen_sweep_per_provider_concurrency == 2; assert s.regen_sweep_pacing_s == 0.5; assert s.regen_sweep_retry_backoff_s == 2.0" succeeds
- **Unit tests**: `tests/unit/test_settings_sharp_prefix_aliases.py::test_regen_sweep_aliases_present`

## Step 2 — Add pacing_s keyword to sweep_cell_health

- **Files**: `src/repoach/llm_proxy/providers/cell_probe_sweep.py`, `tests/unit/test_cell_probe_sweep.py`
- **Action**: Add optional pacing_s: float = 0.0 keyword-only argument to sweep_cell_health. Inside the _probe inner function, after semaphore block and before probe_cell call, insert `await asyncio.sleep(pacing_s)` when pacing_s > 0. Existing callers unaffected. Add unit test test_pacing_s_delays_probes that verifies pacing delays probes using a small matrix and measuring elapsed time.
- **Commit**: `feat(cell-probe-sweep): add optional pacing_s inter-probe delay`
- **Done when**: pytest tests/unit/test_cell_probe_sweep.py -k pacing passes
- **Unit tests**: `tests/unit/test_cell_probe_sweep.py::test_pacing_s_delays_probes`

## Step 3 — Bounded sweep mechanics: cell set, caps, credits skip, 429 filter

- **Files**: `src/repoach/llm_proxy/routing/chain_regen.py`, `tests/unit/test_chain_regen.py`
- **Action**: In chain_regen.py: (a) Add ranking: AaRanking | None = None keyword to gather_and_regenerate; when None call fetch_aa_ranking as today. (b) After matrix sweep, compute bounded cell set: collect chain-ref cells from current chains.env, add candidate cells from ranking, apply per-provider cap with chain-refs first, drop open_router cells if credits snapshot remaining < credits_floor_usd (import get_cached_credits from repoach.health.credits), log regen_sweep_planned. (c) Probe bounded cells per-provider using sweep_cell_health on the filtered sub-matrix, passing max_concurrency=regen_sweep_per_provider_concurrency and pacing_s=regen_sweep_pacing_s. (d) Filter out 429 results (detail == 'http=429') — retry once after regen_sweep_retry_backoff_s, log cell_probe_rate_limited if still 429, do not persist. (e) Persist non-429 results via record_cell_probes. Add unit tests in test_chain_regen.py: test_bounded_sweep (cap and chain-refs-first), test_429_handling (not persisted and retry once), test_credits_skip (skip and None case).
- **Commit**: `feat(chain-regen): bounded in-cycle sweep with caps, credits skip and 429 filter`
- **Done when**: pytest tests/unit/test_chain_regen.py -k 'test_bounded_sweep or test_429_handling or test_credits_skip' passes and ruff check src/repoach/llm_proxy/routing/chain_regen.py exits 0
- **Unit tests**: `tests/unit/test_chain_regen.py::test_bounded_sweep`, `tests/unit/test_chain_regen.py::test_429_handling`, `tests/unit/test_chain_regen.py::test_credits_skip`

## Step 4 — Freshness-windowed read, StaleCellsError and end-to-end refusal test

- **Files**: `src/repoach/llm_proxy/routing/chain_regen.py`, `tests/unit/test_chain_regen.py`, `tests/integration/test_chain_regen_freshness.py`
- **Action**: In chain_regen.py: (a) Define StaleCellsError exception. (b) After the step-3 sweep, call fetch_cell_probes with since=now - max_cell_age_h. (c) If no rows or newest recorded_at < (now - max_cell_age_h), raise StaleCellsError with log chain_regen_stale_cells. (d) Otherwise proceed with speed_for_from_rows and regenerate as today. Add unit tests in test_chain_regen.py: test_freshness_refusal (stale rows and zero rows). Add tests/integration/test_chain_regen_freshness.py::test_stale_after_real_sweep_refuses (superseded name; see step 8) — fake HTTP transport and tmp-path SQLite, asserting StaleCellsError propagation and no chains output.
- **Commit**: `feat(chain-regen): freshness-windowed read with loud StaleCellsError refusal`
- **Done when**: pytest tests/unit/test_chain_regen.py tests/integration/test_chain_regen_freshness.py passes
- **Unit tests**: `tests/unit/test_chain_regen.py::test_freshness_refusal`

## Step 5 — Wire StaleCellsError to typer.Exit(1) in regenerate-chains CLI

- **Files**: `src/repoach/cli/main.py`, `tests/unit/test_chain_regen.py`
- **Action**: In the regenerate-chains command's _run async function, catch StaleCellsError, print its one-line reason, and raise typer.Exit(1). Add unit test test_cli_stale_cells_exit_1 in test_chain_regen.py that invokes the CLI with a scenario that triggers StaleCellsError and asserts exit code 1 and the printed message.
- **Commit**: `feat(cli): map StaleCellsError to exit 1 in regenerate-chains`
- **Done when**: pytest tests/unit/test_chain_regen.py::test_cli_stale_cells_exit_1 passes and ruff check src/repoach/cli/main.py exits 0
- **Unit tests**: `tests/unit/test_chain_regen.py::test_cli_stale_cells_exit_1`

## Step 6 — Close the judge gaps: SP-MFC-REGEN edge declaration + the promised log assertions

- **Files**: `docs/specs/2026-06-30_SP-MFC-REGEN_live-gather-and-cli.md`, `tests/unit/test_chain_regen.py`, `tests/integration/test_chain_regen_freshness.py`
- **Action**: (a) In docs/specs/2026-06-30_SP-MFC-REGEN_live-gather-and-cli.md FRONTMATTER only: add SP-CREDITS-CHECK to depends_on and bump version — chain_regen.py (owned by SP-MFC-REGEN) now imports repoach.health.credits (owned by SP-CREDITS-CHECK) and the edge-honesty gate requires the declared edge; the spec's Architecture Impact mandates this same-PR change. (b) Add FOUR NEW test functions carrying the AC-promised log/transport assertions, using the structlog.testing capture_logs idiom (see tests/unit/test_dev_promise_reconcile.py). In tests/unit/test_chain_regen.py: test_bounded_sweep_logs_planned_count (AC2 — captures regen_sweep_planned and asserts the exact planned cell count), test_429_logs_rate_limited_once (AC3 — the twice-429 cell logs cell_probe_rate_limited exactly once), test_credits_skip_transport_silent_and_logged (AC4 — at the transport layer ZERO open_router probes are issued and the skipped_paid count is logged). In tests/integration/test_chain_regen_freshness.py: test_stale_cells_event_logged (AC1 — the refusal path logs chain_regen_stale_cells). No behavior change to src expected — fix src only if an assertion exposes a real defect.
- **Commit**: `test(chain-regen): close judge gaps — MFC-REGEN edge declaration + promised log assertions`
- **Done when**: pytest tests/unit/test_chain_regen.py tests/integration/test_chain_regen_freshness.py passes and repoach arch check --staged reports edge-honesty ok
- **Unit tests**: `tests/unit/test_chain_regen.py::test_bounded_sweep_logs_planned_count`, `tests/unit/test_chain_regen.py::test_429_logs_rate_limited_once`, `tests/unit/test_chain_regen.py::test_credits_skip_transport_silent_and_logged`, `tests/integration/test_chain_regen_freshness.py::test_stale_after_real_sweep_refuses`

## Step 7 — Guard the sweep failure path and condense to the AC6 LOC budget

- **Files**: `src/repoach/llm_proxy/routing/chain_regen.py`, `tests/unit/test_chain_regen.py`
- **Action**: (a) Wrap the bounded-sweep phase of gather_and_regenerate so ANY exception raised inside it (credits fetch, probe transport, record_cell_probes) is caught, logged as chain_regen_sweep_failed, and treated as zero fresh rows — the G5 freshness guard then raises StaleCellsError as designed; no raw exception may escape toward the CLI. Add NEW unit test test_sweep_failure_folds_into_stale_refusal: a gatherer/transport that raises mid-sweep yields StaleCellsError (not the original exception) and the chain_regen_sweep_failed event is captured. (b) Condense chain_regen.py toward the spec's AC6 budget (≤250 net new non-test lines vs develop): inline single-use helpers, deduplicate the bounded-set/persist plumbing, tighten docstrings — behavior unchanged, every existing test stays green. Report the resulting net LOC (git diff --stat develop -- src/repoach/llm_proxy/routing/chain_regen.py) in the step summary.
- **Commit**: `fix(chain-regen): fold sweep failures into the loud refusal; condense to the AC6 budget`
- **Done when**: pytest tests/unit/test_chain_regen.py tests/integration/test_chain_regen_freshness.py passes
- **Unit tests**: `tests/unit/test_chain_regen.py::test_sweep_failure_folds_into_stale_refusal`

## Step 8 — Rework AC1/AC5 tests against the designed seams

- **Files**: `tests/unit/test_chain_regen.py`, `tests/integration/test_chain_regen_freshness.py`
- **Action**: (a) NEW unit test test_nominal_via_injected_client_and_ranking implementing AC5 as specified: a real httpx.AsyncClient over httpx.MockTransport injected through the client seam plus a pre-built AaRanking passed via ranking= — NO monkeypatching of any repoach function; assert the regeneration completes on fresh rows. Then DELETE test_nominal_fresh_sweep_concludes and the _patch_gatherers helper if no remaining test uses it. (b) NEW integration test test_stale_after_real_sweep_refuses implementing AC1's construction: a cap > 0 sweep whose transport probes yield NO fresh rows (failing/stale probes), asserting StaleCellsError end to end with untouched chains output and the chain_regen_stale_cells event; replace the REGEN_SWEEP_PER_PROVIDER_CAP=0 escape-hatch construction in the existing refusal tests with this one (keep a cap=0 variant only if it pins a distinct behavior, otherwise delete it).
- **Commit**: `test(chain-regen): AC1/AC5 against real seams — injected client, ranking=, real-sweep staleness`
- **Done when**: pytest tests/unit/test_chain_regen.py tests/integration/test_chain_regen_freshness.py passes and grep -c "_patch_gatherers\|monkeypatch.setattr" tests/unit/test_chain_regen.py reports only boundary-safe uses
- **Unit tests**: `tests/unit/test_chain_regen.py::test_nominal_via_injected_client_and_ranking`, `tests/integration/test_chain_regen_freshness.py::test_stale_after_real_sweep_refuses`

## Integration tests

- `tests/integration/test_chain_regen_freshness.py::test_stale_after_real_sweep_refuses`

<!-- repoach-action-plan -->
```json
{
  "spec_id": "SP-REGEN-FRESH-CELLS",
  "title": "Bounded in-cycle cell sweep + freshness refusal in gather_and_regenerate",
  "summary": "Add a bounded per-provider cell health sweep inside gather_and_regenerate before reading probe rows, with per-provider caps, concurrency limits, pacing, 429 filtering, credits-floor skip for open_router, and a since-windowed read that refuses loudly (StaleCellsError → typer.Exit(1)) when the freshest row is older than max_cell_age_h or zero rows exist. New Settings fields, a pacing_s keyword on sweep_cell_health, and a ranking= keyword seam on gather_and_regenerate. Zero new source modules.",
  "steps": [
    {
      "index": 1,
      "title": "Add regeneration sweep settings fields",
      "files": [
        "src/repoach/llm_proxy/config/settings.py",
        "tests/unit/test_settings_sharp_prefix_aliases.py"
      ],
      "action": "Add five new fields to Settings: regen_max_cell_age_h (float, default 12.0), regen_sweep_per_provider_cap (int, default 12), regen_sweep_per_provider_concurrency (int, default 2), regen_sweep_pacing_s (float, default 0.5), regen_sweep_retry_backoff_s (float, default 2.0). Add corresponding entries to _LEGACY_TO_REPOACH_ALIAS and use _aliases() for validation_alias. Follow existing pattern. In the EXISTING file tests/unit/test_settings_sharp_prefix_aliases.py: add the five new (legacy, field) entries to its _LEGACY_TO_FIELD dict — the pinning test test_alias_map_covers_every_field_with_proxy_alias fails otherwise — and add test_regen_sweep_aliases_present asserting the aliases resolve correctly.",
      "commit_message": "feat(settings): add regen sweep bounding and freshness guard settings",
      "done_when": "python -c \"from repoach.llm_proxy.config.settings import Settings; s = Settings(_env_file=None); assert s.regen_max_cell_age_h == 12.0; assert s.regen_sweep_per_provider_cap == 12; assert s.regen_sweep_per_provider_concurrency == 2; assert s.regen_sweep_pacing_s == 0.5; assert s.regen_sweep_retry_backoff_s == 2.0\" succeeds",
      "unit_tests": [
        "tests/unit/test_settings_sharp_prefix_aliases.py::test_regen_sweep_aliases_present"
      ]
    },
    {
      "index": 2,
      "title": "Add pacing_s keyword to sweep_cell_health",
      "files": [
        "src/repoach/llm_proxy/providers/cell_probe_sweep.py",
        "tests/unit/test_cell_probe_sweep.py"
      ],
      "action": "Add optional pacing_s: float = 0.0 keyword-only argument to sweep_cell_health. Inside the _probe inner function, after semaphore block and before probe_cell call, insert `await asyncio.sleep(pacing_s)` when pacing_s > 0. Existing callers unaffected. Add unit test test_pacing_s_delays_probes that verifies pacing delays probes using a small matrix and measuring elapsed time.",
      "commit_message": "feat(cell-probe-sweep): add optional pacing_s inter-probe delay",
      "done_when": "pytest tests/unit/test_cell_probe_sweep.py -k pacing passes",
      "unit_tests": [
        "tests/unit/test_cell_probe_sweep.py::test_pacing_s_delays_probes"
      ]
    },
    {
      "index": 3,
      "title": "Bounded sweep mechanics: cell set, caps, credits skip, 429 filter",
      "files": [
        "src/repoach/llm_proxy/routing/chain_regen.py",
        "tests/unit/test_chain_regen.py"
      ],
      "action": "In chain_regen.py: (a) Add ranking: AaRanking | None = None keyword to gather_and_regenerate; when None call fetch_aa_ranking as today. (b) After matrix sweep, compute bounded cell set: collect chain-ref cells from current chains.env, add candidate cells from ranking, apply per-provider cap with chain-refs first, drop open_router cells if credits snapshot remaining < credits_floor_usd (import get_cached_credits from repoach.health.credits), log regen_sweep_planned. (c) Probe bounded cells per-provider using sweep_cell_health on the filtered sub-matrix, passing max_concurrency=regen_sweep_per_provider_concurrency and pacing_s=regen_sweep_pacing_s. (d) Filter out 429 results (detail == 'http=429') — retry once after regen_sweep_retry_backoff_s, log cell_probe_rate_limited if still 429, do not persist. (e) Persist non-429 results via record_cell_probes. Add unit tests in test_chain_regen.py: test_bounded_sweep (cap and chain-refs-first), test_429_handling (not persisted and retry once), test_credits_skip (skip and None case).",
      "commit_message": "feat(chain-regen): bounded in-cycle sweep with caps, credits skip and 429 filter",
      "done_when": "pytest tests/unit/test_chain_regen.py -k 'test_bounded_sweep or test_429_handling or test_credits_skip' passes and ruff check src/repoach/llm_proxy/routing/chain_regen.py exits 0",
      "unit_tests": [
        "tests/unit/test_chain_regen.py::test_bounded_sweep",
        "tests/unit/test_chain_regen.py::test_429_handling",
        "tests/unit/test_chain_regen.py::test_credits_skip"
      ]
    },
    {
      "index": 4,
      "title": "Freshness-windowed read, StaleCellsError and end-to-end refusal test",
      "files": [
        "src/repoach/llm_proxy/routing/chain_regen.py",
        "tests/unit/test_chain_regen.py",
        "tests/integration/test_chain_regen_freshness.py"
      ],
      "action": "In chain_regen.py: (a) Define StaleCellsError exception. (b) After the step-3 sweep, call fetch_cell_probes with since=now - max_cell_age_h. (c) If no rows or newest recorded_at < (now - max_cell_age_h), raise StaleCellsError with log chain_regen_stale_cells. (d) Otherwise proceed with speed_for_from_rows and regenerate as today. Add unit tests in test_chain_regen.py: test_freshness_refusal (stale rows and zero rows), test_nominal_fresh_sweep_concludes (fresh rows lead to a completed regeneration). Add tests/integration/test_chain_regen_freshness.py::test_stale_after_real_sweep_refuses (superseded name; see step 8) — fake HTTP transport and tmp-path SQLite, asserting StaleCellsError propagation and no chains output.",
      "commit_message": "feat(chain-regen): freshness-windowed read with loud StaleCellsError refusal",
      "done_when": "pytest tests/unit/test_chain_regen.py tests/integration/test_chain_regen_freshness.py passes",
      "unit_tests": [
        "tests/unit/test_chain_regen.py::test_freshness_refusal"
      ]
    },
    {
      "index": 5,
      "title": "Wire StaleCellsError to typer.Exit(1) in regenerate-chains CLI",
      "files": [
        "src/repoach/cli/main.py",
        "tests/unit/test_chain_regen.py"
      ],
      "action": "In the regenerate-chains command's _run async function, catch StaleCellsError, print its one-line reason, and raise typer.Exit(1). Add unit test test_cli_stale_cells_exit_1 in test_chain_regen.py that invokes the CLI with a scenario that triggers StaleCellsError and asserts exit code 1 and the printed message.",
      "commit_message": "feat(cli): map StaleCellsError to exit 1 in regenerate-chains",
      "done_when": "pytest tests/unit/test_chain_regen.py::test_cli_stale_cells_exit_1 passes and ruff check src/repoach/cli/main.py exits 0",
      "unit_tests": [
        "tests/unit/test_chain_regen.py::test_cli_stale_cells_exit_1"
      ]
    },
    {
      "index": 6,
      "title": "Close the judge gaps: SP-MFC-REGEN edge declaration + the promised log assertions",
      "files": [
        "docs/specs/2026-06-30_SP-MFC-REGEN_live-gather-and-cli.md",
        "tests/unit/test_chain_regen.py",
        "tests/integration/test_chain_regen_freshness.py"
      ],
      "action": "(a) In docs/specs/2026-06-30_SP-MFC-REGEN_live-gather-and-cli.md FRONTMATTER only: add SP-CREDITS-CHECK to depends_on and bump version — chain_regen.py (owned by SP-MFC-REGEN) now imports repoach.health.credits (owned by SP-CREDITS-CHECK) and the edge-honesty gate requires the declared edge; the spec's Architecture Impact mandates this same-PR change. (b) Add FOUR NEW test functions carrying the AC-promised log/transport assertions, using the structlog.testing capture_logs idiom (see tests/unit/test_dev_promise_reconcile.py). In tests/unit/test_chain_regen.py: test_bounded_sweep_logs_planned_count (AC2 — captures regen_sweep_planned and asserts the exact planned cell count), test_429_logs_rate_limited_once (AC3 — the twice-429 cell logs cell_probe_rate_limited exactly once), test_credits_skip_transport_silent_and_logged (AC4 — at the transport layer ZERO open_router probes are issued and the skipped_paid count is logged). In tests/integration/test_chain_regen_freshness.py: test_stale_cells_event_logged (AC1 — the refusal path logs chain_regen_stale_cells). No behavior change to src expected — fix src only if an assertion exposes a real defect.",
      "commit_message": "test(chain-regen): close judge gaps — MFC-REGEN edge declaration + promised log assertions",
      "done_when": "pytest tests/unit/test_chain_regen.py tests/integration/test_chain_regen_freshness.py passes and repoach arch check --staged reports edge-honesty ok",
      "unit_tests": [
        "tests/unit/test_chain_regen.py::test_bounded_sweep_logs_planned_count",
        "tests/unit/test_chain_regen.py::test_429_logs_rate_limited_once",
        "tests/unit/test_chain_regen.py::test_credits_skip_transport_silent_and_logged",
        "tests/integration/test_chain_regen_freshness.py::test_stale_after_real_sweep_refuses"
      ]
    },
    {
      "index": 7,
      "title": "Guard the sweep failure path and condense to the AC6 LOC budget",
      "files": [
        "src/repoach/llm_proxy/routing/chain_regen.py",
        "tests/unit/test_chain_regen.py"
      ],
      "action": "(a) Wrap the bounded-sweep phase of gather_and_regenerate so ANY exception raised inside it (credits fetch, probe transport, record_cell_probes) is caught, logged as chain_regen_sweep_failed, and treated as zero fresh rows — the G5 freshness guard then raises StaleCellsError as designed; no raw exception may escape toward the CLI. Add NEW unit test test_sweep_failure_folds_into_stale_refusal: a gatherer/transport that raises mid-sweep yields StaleCellsError (not the original exception) and the chain_regen_sweep_failed event is captured. (b) Condense chain_regen.py toward the spec's AC6 budget (no more than 250 net new non-test lines vs develop): inline single-use helpers, deduplicate the bounded-set/persist plumbing, tighten docstrings — behavior unchanged, every existing test stays green. Report the resulting net LOC (git diff --stat develop -- src/repoach/llm_proxy/routing/chain_regen.py) in the step summary.",
      "commit_message": "fix(chain-regen): fold sweep failures into the loud refusal; condense to the AC6 budget",
      "done_when": "pytest tests/unit/test_chain_regen.py tests/integration/test_chain_regen_freshness.py passes",
      "unit_tests": [
        "tests/unit/test_chain_regen.py::test_sweep_failure_folds_into_stale_refusal"
      ]
    },
    {
      "index": 8,
      "title": "Rework AC1/AC5 tests against the designed seams",
      "files": [
        "tests/unit/test_chain_regen.py",
        "tests/integration/test_chain_regen_freshness.py"
      ],
      "action": "(a) NEW unit test test_nominal_via_injected_client_and_ranking implementing AC5 as specified: a real httpx.AsyncClient over httpx.MockTransport injected through the client seam plus a pre-built AaRanking passed via ranking= — NO monkeypatching of any repoach function; assert the regeneration completes on fresh rows. Then DELETE test_nominal_fresh_sweep_concludes and the _patch_gatherers helper if no remaining test uses it. (b) NEW integration test test_stale_after_real_sweep_refuses implementing AC1's construction: a cap > 0 sweep whose transport probes yield NO fresh rows (failing/stale probes), asserting StaleCellsError end to end with untouched chains output and the chain_regen_stale_cells event; replace the REGEN_SWEEP_PER_PROVIDER_CAP=0 escape-hatch construction in the existing refusal tests with this one (keep a cap=0 variant only if it pins a distinct behavior, otherwise delete it).",
      "commit_message": "test(chain-regen): AC1/AC5 against real seams — injected client, ranking=, real-sweep staleness",
      "done_when": "pytest tests/unit/test_chain_regen.py tests/integration/test_chain_regen_freshness.py passes",
      "unit_tests": [
        "tests/unit/test_chain_regen.py::test_nominal_via_injected_client_and_ranking",
        "tests/integration/test_chain_regen_freshness.py::test_stale_after_real_sweep_refuses"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_chain_regen_freshness.py::test_stale_after_real_sweep_refuses"
  ]
}
```
