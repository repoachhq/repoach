# SP-ROUTING-SWITCHOVER — route through the RoutingTable

**Status:** implemented (hand-shipped)
**Redesign slice:** C — pillar 2 closeout. Umbrella:
`docs/proxy_routing_redesign_architecture.md`. Follows A/B/C0/C1
(#405/#406/#407/#408).
**Touches forbidden paths:** no.

## Why

Slices A–C1 built and proved the `llm_proxy/routing/` domain layer
(`ModelRef`, `Tier`, `Chain`, `RoutingTable`) and re-registered the
direct providers, all behind the unchanged `ResolvedModel` boundary —
but nothing was wired. This slice flips the dispatch path onto the
table and deletes the string-based routing it replaces. After it, the
chain logic lives in exactly one place, which is the precondition for
the health breaker (slice D) to filter a chain in one method.

Because the real providers are now registered (C1), the strict
`RoutingTable.from_settings` accepts every chain the failover tests
declare — so the switchover lands with no test-fixture relabeling.

## Change

`ResolvedModel` and the failover loop are untouched; the rewrite is
entirely behind the boundary.

### `api/model_router.py`

`ModelRouter.__init__` builds `self._table =
RoutingTable.from_settings(settings)` once. `resolve` returns
`self._table.chain_for(name).refs[0].to_resolved(name)`. `resolve_chain`
becomes the two-line adapter:

```python
chain = self._table.chain_for(claude_model_name)
if skip_models:
    chain = chain.without(frozenset(ModelRef.parse(ref) for ref in skip_models))
return [ref.to_resolved(claude_model_name) for ref in chain.refs]
```

`_build_resolved` is deleted (its job is `ModelRef.to_resolved`).
`resolve_messages_request` / `resolve_token_count_request` are unchanged.

### `config/settings.py`

Delete `resolve_model`, `resolve_models`, `_split_chain`, and the now
dead `parse_provider_type` / `parse_model_name` (their only callers were
the deleted `ModelRouter` paths). The `provider_type` property,
`validate_model_format`, and everything else stay.

### Doc references

`llm/capability.py` re-points its stale `ModelRouter.resolve_models`
mention at `routing.classify_tier`.

## Acceptance

- The existing failover suites are the characterization tests and stay
  green unchanged: `test_proxy_chain_failover`, `test_proxy_semantic_failover`
  (skip_models filtering + all-skipped→head fallback), `test_proxy_failover_toolless`,
  `test_proxy_budget_retry`, `test_proxy_failover_events`.
- `test_routing_refs` drops its `_build_resolved` comparison; `test_routing_table`
  converts its `resolve_models` parity test to explicit expected chains.
- Full `pytest tests/unit` green (1058 passed); ruff + format clean; no
  inline comments; no silent except.

## Follow-on

Pillar 2 is complete. Next is pillar 1 — `SP-PROXY-HEALTH-BREAKER`:
`routing/breaker.py` + `Chain.without_down(breaker)`, fed by the live
failover classifier (`services.py`) and the `nim_health_probe` rows, so a
dead/cold model stops being re-tried at the chain head every request.
