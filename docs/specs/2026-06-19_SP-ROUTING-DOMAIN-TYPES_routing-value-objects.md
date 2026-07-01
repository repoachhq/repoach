# SP-ROUTING-DOMAIN-TYPES — the routing value objects

**Status:** specified
**Redesign slice:** A — pillar 2 (routing domain), first slice.
Umbrella: `docs/proxy_routing_redesign_architecture.md`.
**Touches forbidden paths:** no.

## Why

Routing today has no domain model. A `provider/model` ref is a bare
string parsed ad hoc (`Settings.parse_provider_type` /
`parse_model_name`, `settings.py:422-430`); a tier is a substring match
on the model name (`settings.py:407-420`); neither is a type. The
umbrella arc replaces the string-based internals with a
`llm_proxy/routing/` domain layer behind the unchanged `ResolvedModel`
boundary.

This slice lands the two leaf value objects — `ModelRef` (+ `ProviderId`)
and `Tier` (+ `classify_tier`) — as **pure, additive types with zero
wiring**. Nothing imports them yet; the switchover that deletes the
string logic is slice C (`SP-ROUTING-SWITCHOVER`). Landing the leaves
first keeps each PR small and lets the registry-sync assertion grow to
cover the new enum before anything depends on it.

## Change

New package `src/ferova/llm_proxy/routing/` with `__init__.py`,
`refs.py`, `tier.py`. No edits to existing modules except the registry
sync assertion (below).

### `refs.py`

**`ProviderId(StrEnum)`** — members are exactly `SUPPORTED_PROVIDER_IDS`
(`config/provider_ids.py`), values equal to the strings
(`"nvidia_nim"`, `"open_router"`, `"claude_code"`). Built from the tuple
so the two never drift.

**`ModelRef`** — a frozen Pydantic v2 model (`model_config =
ConfigDict(frozen=True)`), the validated value object for a
`provider/model` ref:

- Fields: `provider_id: ProviderId`, `model: str` (non-empty).
- `classmethod parse(ref: str) -> ModelRef` — splits on the **first**
  `/`; `provider_id` validated against `ProviderId` (unknown provider →
  `ValueError`); everything after the first `/` is `model` (so
  `"nvidia_nim/mistralai/mistral-medium-3.5-128b"` →
  `model="mistralai/mistral-medium-3.5-128b"`). A ref with no `/` or an
  empty `model` raises `ValueError`.
- `__str__ -> "{provider_id}/{model}"` — round-trips a `chains.env`
  entry verbatim (`parse(s)` then `str(...)` is identity for valid `s`).
- `to_resolved(self, original_model: str) -> ResolvedModel` — the
  boundary adapter, returns the existing
  `api.model_router.ResolvedModel` with
  `provider_id=str(self.provider_id)`, `provider_model=self.model`,
  `provider_model_ref=str(self)`, `original_model=original_model`. This
  reproduces `ModelRouter._build_resolved` (`model_router.py:88-95`)
  exactly.

To avoid an import cycle (`model_router` imports `settings`, not the
reverse today), `to_resolved` imports `ResolvedModel` lazily inside the
method, or `ResolvedModel` is left where it is and imported under
`TYPE_CHECKING` with a runtime local import. Keep `routing/` free of
any import from `api/`.

### `tier.py`

**`Tier(StrEnum)`** — `OPUS`, `SONNET`, `HAIKU`, `CODER`, `DEFAULT`.

**`classify_tier(model_name: str) -> Tier`** — the classification logic
extracted verbatim from `Settings.resolve_models`
(`settings.py:407-420`), minus the chain lookup:

- If `model_name` contains `/` and the head is in `SUPPORTED_PROVIDER_IDS`
  → `Tier.DEFAULT` (the H10 explicit-`provider/...` passthrough; the
  table resolves DEFAULT to the literal ref in slice B).
- Else, lowercase and match in this **first-match-wins** order: `"coder"`
  → `CODER`, `"opus"` → `OPUS`, `"haiku"` → `HAIKU`, `"sonnet"` →
  `SONNET`, else `DEFAULT`.

`classify_tier` only names the tier; it does **not** consult whether a
slot is configured (that `model_<tier> is not None` fallback lives in
the table, slice B). This keeps the function pure and total.

### Registry sync

Extend the existing assertion in `providers/registry.py:121-128` (or add
a sibling) so `set(ProviderId) == set(SUPPORTED_PROVIDER_IDS)`, keeping
the id set single-sourced across `provider_ids.py`,
`PROVIDER_DESCRIPTORS`, `PROVIDER_FACTORIES`, and now `ProviderId`.

## Acceptance

- New `tests/unit/test_routing_refs.py`:
  - `ProviderId` members equal `SUPPORTED_PROVIDER_IDS`.
  - `ModelRef.parse` round-trips every entry of every `MODEL_*` chain in
    `chains.env` (`str(parse(s)) == s`), including multi-`/` model names.
  - unknown provider, no-`/`, and empty-model refs raise `ValueError`.
  - `to_resolved(name)` equals the current `ModelRouter._build_resolved`
    output for the same ref and name (pin both).
- New `tests/unit/test_routing_tier.py`:
  - first-match-wins order (`coder` before `opus`/`haiku`/`sonnet`);
    explicit `provider/...` → `DEFAULT`; unmatched → `DEFAULT`.
  - a table-driven case per branch of `settings.py:407-420`.
- `ruff` + format clean; no inline comments; full `pytest tests/unit`
  green. No existing module behaviour changes (additive only, save the
  registry assertion).

## Follow-on

Slice B (`SP-ROUTING-TABLE`): `Chain` + `RoutingTable.from_settings`
consuming these types. Slice C (`SP-ROUTING-SWITCHOVER`): repoint
`ModelRouter` / `Settings` at the table and delete `resolve_models` /
`resolve_model` / `_split_chain` / `_build_resolved`.
