# SP-ROUTING-TABLE — the Chain and the RoutingTable

**Status:** specified
**Redesign slice:** B — pillar 2 (routing domain), second slice.
Umbrella: `docs/proxy_routing_redesign_architecture.md`. Builds on slice
A (`SP-ROUTING-DOMAIN-TYPES`, #405).
**Touches forbidden paths:** no.

## Why

Slice A landed the leaf value objects (`ModelRef`, `ProviderId`, `Tier`,
`classify_tier`). This slice adds the two aggregates that turn a tier
into an ordered list of candidates: `Chain` (an ordered, de-duplicated,
non-empty list of `ModelRef`) and `RoutingTable` (`Tier -> Chain`, built
once from `Settings`). Together they are the single replacement for the
string-based chain logic spread across `Settings.resolve_models` /
`resolve_model` / `_split_chain` (`settings.py:366-420`) and the
duplicate resolution in `ModelRouter` (`model_router.py:54-95`).

Still **additive and unwired**: nothing imports these yet. The
switchover that repoints `ModelRouter`/`Settings` at `RoutingTable` and
deletes the string logic is slice C (`SP-ROUTING-SWITCHOVER`). Landing
the table first, with a parity test against the live `resolve_models`,
de-risks that switchover before it touches the dispatch path.

## Change

Two new modules in `src/ferova/llm_proxy/routing/`: `chain.py`,
`table.py`. Export both from `routing/__init__.py`. No edits to existing
modules.

### `chain.py`

**`Chain`** — a frozen Pydantic v2 model wrapping `refs: tuple[ModelRef,
...]`, validated non-empty.

- `classmethod parse(spec: str) -> Chain` — splits the comma-separated
  `MODEL_*` slot value (reproducing `_split_chain`: strip each part,
  drop empties), parses each part with `ModelRef.parse`, and
  **de-duplicates preserving order** (first occurrence wins). An all-empty
  spec raises `ValueError` (a malformed slot fails loudly, not silently
  to `[]` as the legacy coder path does).
- `without(self, skip: frozenset[ModelRef]) -> Chain` — returns a new
  `Chain` with `skip` removed; if every ref is skipped, falls back to a
  single-entry `Chain` of the original head (`self.refs[0]`), reproducing
  `model_router.py:85` (`[...] or [chain[0]]`) so the caller surfaces the
  final failure instead of looping on an empty chain.

De-duplication is a deliberate hardening over the legacy path (which
never dedup'd); the current `chains.env` has no duplicate refs, so slice
C parity is unaffected.

### `table.py`

**`RoutingTable`** — a frozen dataclass holding an immutable
`Mapping[Tier, Chain]` that always contains `Tier.DEFAULT`.

- `classmethod from_settings(settings: Settings) -> RoutingTable` —
  builds `{Tier.DEFAULT: Chain.parse(settings.model)}` then adds an entry
  for each tier slot (`model_opus`/`model_sonnet`/`model_haiku`/
  `model_coder`) that is configured (non-`None`, non-blank). Parsing
  happens once at build time, so a malformed ref raises here rather than
  mid-failover.
- `chain_for(self, model_name: str) -> Chain` — reproduces
  `Settings.resolve_models` exactly:
  1. **H10 passthrough** — if `model_name` contains `/` and its head is a
     supported provider, return a single-entry `Chain` of
     `ModelRef.parse(model_name)` (the literal ref, bypassing tier
     classification).
  2. otherwise `tier = classify_tier(model_name)`; return
     `self._chains.get(tier, self._chains[Tier.DEFAULT])` — an
     unconfigured tier falls back to the global `MODEL` chain, matching
     the legacy `model_<tier> is not None` guard.

## Acceptance

- New `tests/unit/test_routing_chain.py`:
  - `Chain.parse` round-trips each `MODEL_*` slot of `chains.env`
    (`[str(r) for r in parse(slot).refs]` equals the de-dup'd split).
  - empty / all-blank spec raises `ValueError`; duplicates collapse,
    order preserved.
  - `without` removes the skipped refs; all-skipped falls back to the
    original head.
- New `tests/unit/test_routing_table.py`:
  - `from_settings` on a `Settings(...)` built with explicit slots
    (opus+sonnet+coder set, haiku `None`): `chain_for` returns the tier
    chain when configured, the `DEFAULT` chain when not.
  - H10 explicit `provider/...` → single literal ref.
  - **Parity**: for a representative name set (`claude-opus-4-7`,
    `claude-sonnet-4`, `claude-3-5-haiku`, a coder name, a default name,
    an explicit `nvidia_nim/...` ref), `[str(r) for r in
    table.chain_for(n).refs]` equals `settings.resolve_models(n)`.
- `ruff` + format clean; no inline comments; no silent except; full
  `pytest tests/unit` green. No existing module behaviour changes.

## Follow-on

Slice C (`SP-ROUTING-SWITCHOVER`): `ModelRouter.resolve_chain` becomes
`table.chain_for(name).without(skip)` → `[ref.to_resolved(name) for
ref in chain.refs]`; delete `resolve_models`/`resolve_model`/
`_split_chain`/`_build_resolved`; characterization parity tests pin the
live chains. Then slice D adds `Chain.without_down(breaker)` to close the
health loop.
