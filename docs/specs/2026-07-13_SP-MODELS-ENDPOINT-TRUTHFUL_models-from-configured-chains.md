---
id: SP-MODELS-ENDPOINT-TRUTHFUL
title: Derive /v1/models from the configured chains, drop fictional ids
version: 0.1
status: draft
author: jfaye (Ferova audit 2026-07-13)
created: 2026-07-13
updated: 2026-07-13

owns:
  code: []
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Derive /v1/models from the configured chains, drop fictional ids

## Intent

`GET /v1/models` serves a hand-written static list of Claude ids — including
`claude-haiku-4-20250514`, a model that never existed — unrelated to the chains
this proxy is actually configured to route. Derive the endpoint from the
configured routing table and drop the fictional ids.

## Context

`src/ferova/llm_proxy/api/routes.py:22-58` defines the module-level literal
`SUPPORTED_CLAUDE_MODELS` (seven `ModelResponse` entries), and `list_models`
(`routes.py:182-190`) returns it verbatim via `ModelsListResponse`. The list
includes `claude-haiku-4-20250514` (`routes.py:33-37`), which is fictional, and
bears no relation to the capability chains the proxy resolves
(`ModelRouter` / `resolve_chain`, the `MODEL_OPUS` / `MODEL_SONNET` /
`MODEL_HAIKU` config). Audit 2026-07-13 finding M24.

## Goals

- G1: `/v1/models` is derived from the ACTUAL configured chains / routing table
  (the capability model ids the proxy will actually route to), not a static
  literal.
- G2: the fictional `claude-haiku-4-20250514` id (and any other id not backed by
  a configured chain) is absent from the response.
- G3: the response still validates as `ModelsListResponse` (same wire shape:
  `data`, `first_id`, `has_more`, `last_id`) so compatibility clients are
  unaffected in structure.

## Non-Goals

- NG1: no change to the `ModelResponse` / `ModelsListResponse` schemas.
- NG2: no attempt to advertise per-hop fallback models individually if the
  routing table exposes only capability heads — advertise what the router
  resolves (decide heads-vs-full-chain in the plan; heads is acceptable and
  truthful).
- NG3: no auth change on the endpoint (`require_api_key` stays).

## Assumptions

- A1: the configured chains / routing table are reachable at request time via
  the settings/`ModelRouter` already injected into route handlers
  (`get_settings`, `get_proxy_service`), so the endpoint can enumerate the
  configured capability model ids without new I/O.
- A2: the routing table exposes a stable, enumerable set of model ids (the
  capability heads or the resolved chain refs) suitable for advertisement.

## Interface

Changed (in-place): `list_models` (`routes.py:182-190`) builds its `data` from
the configured chains (via `Settings` / `ModelRouter`, injected through the
existing dependency pattern) instead of the static `SUPPORTED_CLAUDE_MODELS`.
The static literal (`routes.py:22-58`) is removed or reduced to a fallback used
only when no chains are configured (decide in the plan; the fictional id is
removed regardless).

## Behavior

### Nominal

`GET /v1/models` returns the model ids the proxy is configured to route to, in a
stable order, with `first_id`/`last_id` reflecting that list.

### Edge cases

- No chains configured -> empty `data` (with `first_id`/`last_id` null) or a
  documented minimal fallback — NOT the fictional list.
- Duplicate ids across capability tiers -> de-duplicated in the advertised set.

### Failure scenarios

- If the routing table cannot be read, the endpoint returns an empty/minimal
  truthful list rather than fictional ids — fail CLOSED to honesty (never
  advertise a model that does not exist).

## Architecture Impact

- Adds dependency: none new at the spec-graph level — in-place modification of
  `routes.py` (owned by an existing spec), reading the already-injected settings
  / router. No new cross-owner import.
- New / changed coupling, cycles, or shared state: `list_models` now reads the
  routing config it already has access to via dependencies; no cycle.

## Diagram

N/A (in-place fix).

## Acceptance Criteria

- [ ] AC1: unit — given a settings/router configured with a known set of chain
  model ids, the `list_models` handler's returned `data` equals exactly that
  set (order stable), and `claude-haiku-4-20250514` is absent.
- [ ] AC2 (INTEGRATION): drive the real endpoint via FastAPI `TestClient` with
  the settings dependency overridden (`app.dependency_overrides`, the sanctioned
  seam) to a known chain configuration; assert the JSON `data` reflects the
  configured chain models and that the fictional id is NOT present. No
  monkeypatching of Ferova code.
- [ ] AC3: promised test file + selectors —
  `tests/unit/test_models_endpoint.py::test_models_reflect_configured_chains`
  and `::test_fictional_haiku_4_absent`.
- [ ] AC4: `ruff` + `ruff format --check` + `pytest tests/unit` green; zero
  inline comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`;
  `ferova arch graph --check` exits 0.

## Open Questions

(none)
