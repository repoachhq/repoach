# SP-BREAKER-PROVIDER-SCOPE — Provider-scoped account faults and a proactive credits gate

Close the two gaps between what the proxy already knows and what dispatch consults: an account-class breaker trip (401/402/403/auth_failed) on one open_router ref now benches every open_router ref present in the resolved chains in the same call via a new BreakerState.trip_provider, while provider_404 keeps today's single-ref behavior; independently, a private async helper reads the cached OpenRouter credits snapshot and, when remaining is below the floor, excludes open_router refs from dispatch through the existing skip_models seam before the first attempt, failing open whenever the snapshot is unavailable.

## Step 1 — Add provider-scoped bench to BreakerState

- **Files**: `src/repoach/llm_proxy/routing/breaker.py`, `tests/unit/test_breaker_provider_scope.py`
- **Action**: In breaker.py add ACCOUNT_FAULT_REASONS: frozenset[str] = frozenset({'auth_failed', 'provider_401', 'provider_402', 'provider_403'}) (provider_404 deliberately excluded, it stays per-ref). Add a method trip_provider(self, provider_id: str, refs: Iterable[ModelRef], *, now: float, ttl_s: float, reason: str) -> None on BreakerState that calls self.trip(ref, now=now, ttl_s=ttl_s, reason=reason) for every ref in refs whose ref.provider_id == provider_id, reusing trip's existing extend-never-shorten semantics for idempotency. Do not touch trip, is_down, down_refs, snapshot, or clear. Create tests/unit/test_breaker_provider_scope.py with real BreakerState instances (no patching of repoach code): test_account_fault_benches_all_provider_refs (trip_provider with three open_router refs benches all three with the same ttl and reason), test_404_stays_single_ref (calling plain trip for provider_404 on one ref leaves siblings untouched), test_propagation_idempotent_on_rebench (calling trip_provider twice does not shorten or duplicate), test_other_provider_refs_untouched (a claude_code ref stays up after an open_router trip_provider call).
- **Commit**: `feat(breaker): add provider-scoped bench for account-class faults`
- **Done when**: pytest tests/unit/test_breaker_provider_scope.py -q passes
- **Unit tests**: `tests/unit/test_breaker_provider_scope.py::test_account_fault_benches_all_provider_refs`, `tests/unit/test_breaker_provider_scope.py::test_404_stays_single_ref`, `tests/unit/test_breaker_provider_scope.py::test_propagation_idempotent_on_rebench`, `tests/unit/test_breaker_provider_scope.py::test_other_provider_refs_untouched`

## Step 2 — Wire account-fault propagation into the failover loop

- **Files**: `src/repoach/llm_proxy/api/services.py`, `tests/unit/test_health_breaker.py`, `tests/integration/test_provider_scope_and_credits_gate.py`
- **Action**: In services.py, change _trip_breaker's signature to accept an optional chain: list[ResolvedModel] | None = None keyword, and pass chain=chain from its only call site inside _stream_with_failover. After computing effective_ttl, when reason is in breaker.ACCOUNT_FAULT_REASONS and chain is not None: collect sibling_refs = {ModelRef.parse(c.provider_model_ref) for c in chain if c.provider_id == ref.provider_id}, call breaker.trip_provider(ref.provider_id, sibling_refs, now=time.monotonic(), ttl_s=effective_ttl, reason=f'{reason}_propagated'), emit exactly one logger.warning('breaker_provider_propagated', provider=ref.provider_id, ref_count=len(sibling_refs), ttl_s=effective_ttl), then return early. Otherwise keep the existing single breaker.trip(ref, ...) call and breaker_quarantined logging unchanged (covers provider_404 and every non-account reason, G2). Add to test_health_breaker.py: test_trip_breaker_propagates_account_fault_to_siblings (a chain with three open_router refs, trip provider_402 on one, assert all three are down with reason provider_402_propagated) and test_trip_breaker_404_stays_single_ref (same chain shape, trip provider_404, assert only the original ref is down). Add tests/integration/test_provider_scope_and_credits_gate.py in the test_proxy_dead_hop_quarantine.py style: two fake providers registered under two open_router-prefixed chain entries plus one healthy fallback; the first open_router hop raises a 402-shaped provider error, the second open_router hop's stream_response must never be invoked (assert via a call-log fake), the request completes on the healthy fallback, and GET /health lists both open_router refs with a provider_402_propagated reason.
- **Commit**: `feat(services): propagate account-class breaker trips to sibling refs`
- **Done when**: pytest tests/unit/test_health_breaker.py tests/integration/test_provider_scope_and_credits_gate.py -q passes
- **Unit tests**: `tests/unit/test_health_breaker.py::test_trip_breaker_propagates_account_fault_to_siblings`, `tests/unit/test_health_breaker.py::test_trip_breaker_404_stays_single_ref`

## Step 3 — Add the credits-gate exclusion helper

- **Files**: `src/repoach/llm_proxy/api/services.py`, `src/repoach/llm_proxy/api/model_router.py`, `tests/unit/test_credits_gate.py`
- **Action**: In model_router.py add ModelRouter.open_router_refs_for(self, claude_model_name: str) -> frozenset[str] returning frozenset(str(ref) for ref in self._table.chain_for(claude_model_name).refs if ref.provider_id == 'open_router') -- reads the unfiltered configured chain, no breaker/skip interaction. In services.py add a module-level async function compute_credits_gate_skip_models(settings: Settings, client: httpx.AsyncClient, open_router_refs: frozenset[str]) -> frozenset[str] importing get_cached_credits from repoach.health.credits: return frozenset() immediately when settings.open_router_api_key is falsy or open_router_refs is empty; await get_cached_credits(...); return frozenset() when the snapshot is None (fail open, G4); return open_router_refs when snapshot.remaining < settings.credits_floor_usd (strict less-than, G3), logging a single logger.warning('credits_gate_closed', remaining=snapshot.remaining, floor=settings.credits_floor_usd) guarded by comparing the snapshot object identity against a module-level last-logged snapshot so repeated calls against the same cached snapshot log once; otherwise return frozenset(). Create tests/unit/test_credits_gate.py using an httpx.AsyncClient backed by httpx.MockTransport (the test_credits.py boundary-fake style, resetting the credits cache each test): test_below_floor_excludes_open_router_refs, test_at_floor_keeps_open_router (remaining == floor must NOT exclude), test_snapshot_unavailable_fails_open (500 response yields empty exclusion set), test_recovered_balance_lifts_gate_without_restart (a low-balance call excludes, then a fresh above-floor snapshot after cache expiry no longer excludes, no process restart).
- **Commit**: `feat(credits): add the open_router credits-gate exclusion helper`
- **Done when**: pytest tests/unit/test_credits_gate.py -q passes
- **Unit tests**: `tests/unit/test_credits_gate.py::test_below_floor_excludes_open_router_refs`, `tests/unit/test_credits_gate.py::test_at_floor_keeps_open_router`, `tests/unit/test_credits_gate.py::test_snapshot_unavailable_fails_open`, `tests/unit/test_credits_gate.py::test_recovered_balance_lifts_gate_without_restart`

## Step 4 — Wire the credits gate into /v1/messages dispatch

- **Files**: `src/repoach/llm_proxy/api/routes.py`, `src/repoach/llm_proxy/api/services.py`, `tests/unit/test_credits_gate.py`
- **Action**: In services.py add a thin public passthrough ClaudeProxyService.open_router_refs_for(self, claude_model_name: str) -> frozenset[str] that returns self._model_router.open_router_refs_for(claude_model_name) -- keeps _model_router private while giving routes.py a seam. In routes.py's create_message handler, add settings: Settings = Depends(get_settings) and client: httpx.AsyncClient = Depends(get_credits_client) parameters, then before calling service.create_message: open_router_refs = service.open_router_refs_for(request_data.model); gated_skip = await compute_credits_gate_skip_models(settings, client, open_router_refs) (import compute_credits_gate_skip_models from .services); call return service.create_message(request_data, skip_models=gated_skip). This reuses the existing skip_models parameter unchanged (no new public parameter on create_message or resolve_chain) and keeps every other route untouched. Add tests/unit/test_credits_gate.py::test_service_open_router_refs_for_delegates_to_router constructing a real ClaudeProxyService with a Settings carrying an open_router entry in MODEL_SONNET and asserting open_router_refs_for returns the expected frozenset.
- **Commit**: `feat(routes): gate open_router dispatch on the cached credits floor`
- **Done when**: pytest tests/unit/test_credits_gate.py -q passes and ruff check src/repoach/llm_proxy/api/routes.py src/repoach/llm_proxy/api/services.py exits 0
- **Unit tests**: `tests/unit/test_credits_gate.py::test_service_open_router_refs_for_delegates_to_router`

## Integration tests

- `tests/integration/test_provider_scope_and_credits_gate.py::test_one_402_skips_sibling_refs_end_to_end`

<!-- repoach-action-plan -->
```json
{
  "spec_id": "SP-BREAKER-PROVIDER-SCOPE",
  "title": "Provider-scoped account faults and a proactive credits gate",
  "summary": "Close the two gaps between what the proxy already knows and what dispatch consults: an account-class breaker trip (401/402/403/auth_failed) on one open_router ref now benches every open_router ref present in the resolved chains in the same call via a new BreakerState.trip_provider, while provider_404 keeps today's single-ref behavior; independently, a private async helper reads the cached OpenRouter credits snapshot and, when remaining is below the floor, excludes open_router refs from dispatch through the existing skip_models seam before the first attempt, failing open whenever the snapshot is unavailable.",
  "steps": [
    {
      "index": 1,
      "title": "Add provider-scoped bench to BreakerState",
      "files": [
        "src/repoach/llm_proxy/routing/breaker.py",
        "tests/unit/test_breaker_provider_scope.py"
      ],
      "action": "In breaker.py add ACCOUNT_FAULT_REASONS: frozenset[str] = frozenset({'auth_failed', 'provider_401', 'provider_402', 'provider_403'}) (provider_404 deliberately excluded, it stays per-ref). Add a method trip_provider(self, provider_id: str, refs: Iterable[ModelRef], *, now: float, ttl_s: float, reason: str) -> None on BreakerState that calls self.trip(ref, now=now, ttl_s=ttl_s, reason=reason) for every ref in refs whose ref.provider_id == provider_id, reusing trip's existing extend-never-shorten semantics for idempotency. Do not touch trip, is_down, down_refs, snapshot, or clear. Create tests/unit/test_breaker_provider_scope.py with real BreakerState instances (no patching of repoach code): test_account_fault_benches_all_provider_refs (trip_provider with three open_router refs benches all three with the same ttl and reason), test_404_stays_single_ref (calling plain trip for provider_404 on one ref leaves siblings untouched), test_propagation_idempotent_on_rebench (calling trip_provider twice does not shorten or duplicate), test_other_provider_refs_untouched (a claude_code ref stays up after an open_router trip_provider call).",
      "commit_message": "feat(breaker): add provider-scoped bench for account-class faults",
      "done_when": "pytest tests/unit/test_breaker_provider_scope.py -q passes",
      "unit_tests": [
        "tests/unit/test_breaker_provider_scope.py::test_account_fault_benches_all_provider_refs",
        "tests/unit/test_breaker_provider_scope.py::test_404_stays_single_ref",
        "tests/unit/test_breaker_provider_scope.py::test_propagation_idempotent_on_rebench",
        "tests/unit/test_breaker_provider_scope.py::test_other_provider_refs_untouched"
      ]
    },
    {
      "index": 2,
      "title": "Wire account-fault propagation into the failover loop",
      "files": [
        "src/repoach/llm_proxy/api/services.py",
        "tests/unit/test_health_breaker.py",
        "tests/integration/test_provider_scope_and_credits_gate.py"
      ],
      "action": "In services.py, change _trip_breaker's signature to accept an optional chain: list[ResolvedModel] | None = None keyword, and pass chain=chain from its only call site inside _stream_with_failover. After computing effective_ttl, when reason is in breaker.ACCOUNT_FAULT_REASONS and chain is not None: collect sibling_refs = {ModelRef.parse(c.provider_model_ref) for c in chain if c.provider_id == ref.provider_id}, call breaker.trip_provider(ref.provider_id, sibling_refs, now=time.monotonic(), ttl_s=effective_ttl, reason=f'{reason}_propagated'), emit exactly one logger.warning('breaker_provider_propagated', provider=ref.provider_id, ref_count=len(sibling_refs), ttl_s=effective_ttl), then return early. Otherwise keep the existing single breaker.trip(ref, ...) call and breaker_quarantined logging unchanged (covers provider_404 and every non-account reason, G2). Add to test_health_breaker.py: test_trip_breaker_propagates_account_fault_to_siblings (a chain with three open_router refs, trip provider_402 on one, assert all three are down with reason provider_402_propagated) and test_trip_breaker_404_stays_single_ref (same chain shape, trip provider_404, assert only the original ref is down). Add tests/integration/test_provider_scope_and_credits_gate.py in the test_proxy_dead_hop_quarantine.py style: two fake providers registered under two open_router-prefixed chain entries plus one healthy fallback; the first open_router hop raises a 402-shaped provider error, the second open_router hop's stream_response must never be invoked (assert via a call-log fake), the request completes on the healthy fallback, and GET /health lists both open_router refs with a provider_402_propagated reason.",
      "commit_message": "feat(services): propagate account-class breaker trips to sibling refs",
      "done_when": "pytest tests/unit/test_health_breaker.py tests/integration/test_provider_scope_and_credits_gate.py -q passes",
      "unit_tests": [
        "tests/unit/test_health_breaker.py::test_trip_breaker_propagates_account_fault_to_siblings",
        "tests/unit/test_health_breaker.py::test_trip_breaker_404_stays_single_ref"
      ]
    },
    {
      "index": 3,
      "title": "Add the credits-gate exclusion helper",
      "files": [
        "src/repoach/llm_proxy/api/services.py",
        "src/repoach/llm_proxy/api/model_router.py",
        "tests/unit/test_credits_gate.py"
      ],
      "action": "In model_router.py add ModelRouter.open_router_refs_for(self, claude_model_name: str) -> frozenset[str] returning frozenset(str(ref) for ref in self._table.chain_for(claude_model_name).refs if ref.provider_id == 'open_router') -- reads the unfiltered configured chain, no breaker/skip interaction. In services.py add a module-level async function compute_credits_gate_skip_models(settings: Settings, client: httpx.AsyncClient, open_router_refs: frozenset[str]) -> frozenset[str] importing get_cached_credits from repoach.health.credits: return frozenset() immediately when settings.open_router_api_key is falsy or open_router_refs is empty; await get_cached_credits(...); return frozenset() when the snapshot is None (fail open, G4); return open_router_refs when snapshot.remaining < settings.credits_floor_usd (strict less-than, G3), logging a single logger.warning('credits_gate_closed', remaining=snapshot.remaining, floor=settings.credits_floor_usd) guarded by comparing the snapshot object identity against a module-level last-logged snapshot so repeated calls against the same cached snapshot log once; otherwise return frozenset(). Create tests/unit/test_credits_gate.py using an httpx.AsyncClient backed by httpx.MockTransport (the test_credits.py boundary-fake style, resetting the credits cache each test): test_below_floor_excludes_open_router_refs, test_at_floor_keeps_open_router (remaining == floor must NOT exclude), test_snapshot_unavailable_fails_open (500 response yields empty exclusion set), test_recovered_balance_lifts_gate_without_restart (a low-balance call excludes, then a fresh above-floor snapshot after cache expiry no longer excludes, no process restart).",
      "commit_message": "feat(credits): add the open_router credits-gate exclusion helper",
      "done_when": "pytest tests/unit/test_credits_gate.py -q passes",
      "unit_tests": [
        "tests/unit/test_credits_gate.py::test_below_floor_excludes_open_router_refs",
        "tests/unit/test_credits_gate.py::test_at_floor_keeps_open_router",
        "tests/unit/test_credits_gate.py::test_snapshot_unavailable_fails_open",
        "tests/unit/test_credits_gate.py::test_recovered_balance_lifts_gate_without_restart"
      ]
    },
    {
      "index": 4,
      "title": "Wire the credits gate into /v1/messages dispatch",
      "files": [
        "src/repoach/llm_proxy/api/routes.py",
        "src/repoach/llm_proxy/api/services.py",
        "tests/unit/test_credits_gate.py"
      ],
      "action": "In services.py add a thin public passthrough ClaudeProxyService.open_router_refs_for(self, claude_model_name: str) -> frozenset[str] that returns self._model_router.open_router_refs_for(claude_model_name) -- keeps _model_router private while giving routes.py a seam. In routes.py's create_message handler, add settings: Settings = Depends(get_settings) and client: httpx.AsyncClient = Depends(get_credits_client) parameters, then before calling service.create_message: open_router_refs = service.open_router_refs_for(request_data.model); gated_skip = await compute_credits_gate_skip_models(settings, client, open_router_refs) (import compute_credits_gate_skip_models from .services); call return service.create_message(request_data, skip_models=gated_skip). This reuses the existing skip_models parameter unchanged (no new public parameter on create_message or resolve_chain) and keeps every other route untouched. Add tests/unit/test_credits_gate.py::test_service_open_router_refs_for_delegates_to_router constructing a real ClaudeProxyService with a Settings carrying an open_router entry in MODEL_SONNET and asserting open_router_refs_for returns the expected frozenset.",
      "commit_message": "feat(routes): gate open_router dispatch on the cached credits floor",
      "done_when": "pytest tests/unit/test_credits_gate.py -q passes and ruff check src/repoach/llm_proxy/api/routes.py src/repoach/llm_proxy/api/services.py exits 0",
      "unit_tests": [
        "tests/unit/test_credits_gate.py::test_service_open_router_refs_for_delegates_to_router"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_provider_scope_and_credits_gate.py::test_one_402_skips_sibling_refs_end_to_end"
  ]
}
```
