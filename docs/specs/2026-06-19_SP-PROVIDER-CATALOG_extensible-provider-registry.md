# SP-PROVIDER-CATALOG — make the provider registry extensible

**Status:** specified
**Redesign slice:** inserted prerequisite (between B and the switchover).
Umbrella: `docs/proxy_routing_redesign_architecture.md`.
**Touches forbidden paths:** no.

## Why

Slice A baked the provider set into a hand-maintained `ProviderId`
StrEnum (3 members) guarded against `SUPPORTED_PROVIDER_IDS`. That froze
the registry at the **3 providers that survived the #311 deep-cut**
(`nvidia_nim`, `open_router`, `claude_code`) — even though the operator
holds live API keys for **kimi, groq, cerebras, and deepseek** (in
`.env`), which were direct providers stripped by that cut. A strict
`ModelRef` would reject those legitimate providers, and adding one back
means editing an enum, a tuple, descriptors, and factories in lockstep.

The provider set must become **extensible from a single source** before
those providers are re-wired and before the routing switchover enforces
validation. After this slice, adding a provider is: one descriptor + one
factory + its client class + its credential field — no enum, no id-tuple,
no `ModelRef` edits.

## Change

Collapse the two hand-maintained id lists (`SUPPORTED_PROVIDER_IDS` tuple
+ `ProviderId` enum) into a single derived value sourced from the
descriptor table, and move that table to a leaf module so `config` and
`routing` can derive from it without the registry's import cycle
(`registry` imports `settings`, so `settings` cannot import `registry`).

### New leaf `providers/catalog.py`

Holds the provider-identity data, moved verbatim from `registry.py`:
`TransportType`, the `ProviderDescriptor` dataclass, and
`PROVIDER_DESCRIPTORS`. Adds the **derived** id set:

```python
SUPPORTED_PROVIDER_IDS: tuple[str, ...] = tuple(PROVIDER_DESCRIPTORS)
```

Imports only `providers.defaults` (a leaf); `providers/__init__` pulls
`base` + `exceptions`, both verified free of any `config`/`settings`
import, so `config → providers.catalog` is acyclic.

### `registry.py`

Imports `TransportType`, `ProviderDescriptor`, `PROVIDER_DESCRIPTORS`,
`SUPPORTED_PROVIDER_IDS` from `catalog` and **re-exports** them (so
`from ...registry import PROVIDER_DESCRIPTORS` in `api/dependencies.py`
keeps working). Keeps factories, `build_provider_config`,
`create_provider`, `ProviderRegistry`. The old descriptor↔ids assertion
is dropped (the ids now derive). The **factory↔descriptor** sync
assertion stays and becomes the single extensibility guard: every
descriptor must have a factory.

### `config/provider_ids.py`

Becomes a thin re-export — `from ferova.llm_proxy.providers.catalog
import SUPPORTED_PROVIDER_IDS` — keeping the stable import path used by
`settings.py`, `routing/*`, and tests. Docstring updated to name the
catalog as the source.

### `routing/refs.py`

Drop the `ProviderId` StrEnum and its sync guard. `ModelRef.provider_id`
becomes a `str` validated against `SUPPORTED_PROVIDER_IDS` (a
`field_validator` rejecting an unregistered provider). `parse` and
`to_resolved` carry the plain string; `__str__` is unchanged. This is
the unfreeze: once a descriptor is added to the catalog, `ModelRef`
accepts that provider with no edit here. `routing/__init__` stops
exporting `ProviderId`.

`ResolvedModel.provider_id` was already a `str`, so the dispatch boundary
is unaffected and slice A's `to_resolved` parity still holds.

## Acceptance

- New `tests/unit/test_provider_catalog.py`:
  - `SUPPORTED_PROVIDER_IDS == tuple(PROVIDER_DESCRIPTORS)` and equals the
    current 3 ids (behaviour preserved for today's registry).
  - every descriptor id has a `PROVIDER_FACTORIES` entry (the guard).
  - the `config.provider_ids` re-export equals the catalog value.
- `tests/unit/test_routing_refs.py` updated: drop the `ProviderId`
  membership test; parse/round-trip/`to_resolved` assert on `str`
  provider ids; an unregistered provider (`"kimi/..."` today) still
  raises `ValueError` — proving validation now derives from the catalog.
- Full `pytest tests/unit` green; ruff + format clean; no inline
  comments; no silent except. No runtime behaviour change for the three
  registered providers.

## Follow-on

- `SP-PROVIDER-REWIRE`: add `kimi` / `groq` / `cerebras` / `deepseek`
  descriptors + factories + clients (most are OpenAI-compatible →
  reuse `OpenAIChatTransport`) + `Settings` credential fields. This
  re-validates the failover test fixtures that reference `kimi/`/`groq/`.
- Then `SP-ROUTING-SWITCHOVER` (was slice C): repoint `ModelRouter`/
  `Settings` at `RoutingTable`; with the real providers registered, the
  strict switchover needs no fixture relabeling.
