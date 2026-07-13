---
id: SP-PROXY-EDGE-HARDEN
title: Harden proxy auth truncation, /health disclosure, and upstream passthrough
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

# Harden proxy auth truncation, /health disclosure, and upstream passthrough

## Intent

Close three security-relevant LOW findings on the llm_proxy edge: a
credential-truncation flaw in the API-key check, an unauthenticated
topology-disclosure surface on `GET /health`, and an unrestricted
model/`extra_body` passthrough that lets any authenticated client
bypass configured chains and override upstream request knobs. Each is a
defence-in-depth tightening; none changes the nominal happy path for a
correctly-configured operator.

## Context

Audit 2026-07-13 findings on `src/ferova/llm_proxy/`:

- **F-AUTH** — `require_api_key`
  (`src/ferova/llm_proxy/api/dependencies.py:75-108`): after extracting
  the bearer/`x-api-key` value, lines 104-105 truncate the presented
  credential at the first `:` before `secrets.compare_digest`
  (`token.split(":", 1)[0]`). This was added to accept
  `<token>:<model-name>` suffixes, but it means `REALTOKEN:whatever`
  authenticates as `REALTOKEN`, and a configured token that itself
  contains `:` becomes un-presentable. The credential-matching surface
  is wider than the configured secret.
- **F-HEALTH** — `GET /health`
  (`src/ferova/llm_proxy/api/routes.py:150-173`) carries no
  `Depends(require_api_key)` (unlike `/`, `/v1/models`, and the probe
  routes) and returns the full breaker snapshot: per-ref `reason`,
  `ttl_remaining_s`, and `consecutive_failures`. On a public bind this
  is unauthenticated disclosure of live routing topology and failure
  state. SP-CREDITS-CHECK will additionally add a `credits` field here
  — another internal to keep behind auth.
- **F-PASS** — `RoutingTable.chain_for`
  (`src/ferova/llm_proxy/routing/table.py:73-78`) returns any
  `model="<provider>/<anything>"` as a single-ref passthrough chain
  whenever the head is in `SUPPORTED_PROVIDER_IDS`, so an authenticated
  client can name an off-chain upstream (e.g.
  `open_router/<anything>`) and skip the operator's configured
  failover chains entirely. Compounding it,
  `build_request_body` merges the client's `extra_body`
  (`src/ferova/llm_proxy/api/models/anthropic.py:97`) unfiltered into
  the NIM upstream body
  (`src/ferova/llm_proxy/providers/nvidia_nim/request.py:127-130`),
  so a client can seed `chat_template`, `request_id`, `reasoning_budget`
  and other server-owned knobs that the subsequent `_set_extra` calls
  then decline to overwrite (they no-op when the key is already
  present, lines 142-148).

These are edge-facing modifications of existing already-owned modules
(no merge-path change; `owns.code: []`).

## Goals

- G1: the API-key check compares the full presented credential against
  the configured token — no silent truncation widens the match set.
- G2: `GET /health` exposes a minimal unauthenticated liveness signal
  only; the detailed breaker (and any future `credits`) internals
  require a valid API key.
- G3: model passthrough is restricted to an operator-sanctioned set,
  and client-supplied `extra_body` cannot override server-owned NIM
  request knobs.
- G4: a correctly-configured operator (token without `:`, using
  configured chains, no smuggled `extra_body`) sees identical behavior
  to today.

## Non-Goals

- NG1: no new auth scheme (OAuth, per-client keys, scopes) — the single
  shared `anthropic_auth_token` model is unchanged.
- NG2: no removal of the legitimate passthrough feature for configured
  providers; only unconfigured/off-chain targets are refused.
- NG3: no rate limiting, no TLS/bind-address changes (deployment
  concerns out of scope).
- NG4: no change to the OpenRouter provider's own `extra_body` handling
  (`providers/open_router/request.py`) beyond what F-PASS requires for
  NIM.

## Assumptions

- A1: the configured `anthropic_auth_token`, when set, is a single
  opaque secret; operators do not intentionally embed `:` in it (the
  spec makes this explicit and enforced, per the tightened rule below).
- A2: the `<token>:<model>` suffix convention still needs to be
  accepted for clients that append a model name — the fix must keep a
  bounded form of it, not break those clients (see Behavior).
- A3: `SUPPORTED_PROVIDER_IDS` and the configured tier chains
  (`RoutingTable.chains`) together define the sanctioned model surface;
  a passthrough target is legitimate only when it names a provider the
  operator actually configured.

## Interface

Signatures are largely unchanged; the new surface is internal.

- `require_api_key(request, settings) -> None`
  (`api/dependencies.py`) — unchanged signature. New internal rule: the
  presented credential is matched against the configured token by
  stripping ONLY a trailing `:<model-suffix>` that is not itself part
  of the token, rather than blindly cutting at the first `:`. Concrete
  rule (choose the stricter, documented form): if the configured
  `anthropic_auth_token` contains `:`, raise a startup/config
  `ValueError` (a `:`-bearing token is unsupported and un-presentable);
  otherwise accept the credential when it equals the token OR equals
  `f"{token}:{suffix}"` for some non-empty `suffix` (i.e. strip a
  suffix only when the prefix is an exact match), still via
  `secrets.compare_digest` over the full candidate.
- `GET /health` (`api/routes.py`) — gains
  `_auth=Depends(require_api_key)` for the DETAILED body, but a new
  minimal liveness path stays unauthenticated. Preferred shape: keep
  `GET /health` returning only `{"status": "healthy"}` unauthenticated,
  and move the breaker/`credits` internals to an authenticated field
  that is populated only when the request presents a valid key
  (an authenticated caller gets `breaker`/`credits`; an anonymous
  caller gets liveness only). Equivalent alternative: a dedicated
  authenticated `GET /health/detail` with `/health` reduced to
  liveness. The chosen shape is an Open Question to settle in review;
  either satisfies G2.
- `RoutingTable.chain_for(model_name) -> Chain`
  (`routing/table.py`) — unchanged signature. The `"/" in model_name`
  passthrough branch (lines 73-76) additionally requires the parsed
  provider ref to be operator-sanctioned; an unsanctioned passthrough
  target raises a typed error surfaced by the route as HTTP 400/404
  (not silently routed). The sanctioning predicate lives in the routing
  layer.
- `build_request_body(request_data, nim, *, thinking_enabled) -> dict`
  (`providers/nvidia_nim/request.py`) — unchanged signature. The client
  `extra_body` merge at lines 127-130 is replaced by a whitelist
  filter: only client keys on an explicit allowed set are copied into
  the upstream `extra_body`; server-owned keys (`chat_template`,
  `chat_template_kwargs`, `request_id`, `reasoning_budget`, and the
  `_set_extra` knobs) are never client-overridable. Filtered keys are
  logged at debug (`nim_extra_body_filtered`).

Errors:
- F-AUTH: `HTTPException(401)` unchanged for a genuine mismatch;
  `ValueError` at settings validation for a `:`-bearing configured
  token.
- F-PASS routing: a typed `UnsanctionedModelError` (or reuse of an
  existing routing error) surfaced as HTTP 400.

## Behavior

### Nominal

- F-AUTH: operator token `REALTOKEN` (no `:`). Client presents
  `REALTOKEN` or `Bearer REALTOKEN` → 200. Client presents
  `REALTOKEN:claude-sonnet` → 200 (exact-prefix suffix strip). No
  configured token → check remains a no-op (unchanged).
- F-HEALTH: anonymous `GET /health` → 200 `{"status": "healthy"}`.
  Authenticated `GET /health` (valid key) → 200 with `breaker` (and,
  once SP-CREDITS-CHECK lands, `credits`).
- F-PASS: `model` naming a configured tier or a sanctioned provider
  passthrough routes as today; `extra_body` keys on the whitelist pass
  through unchanged.

### Edge cases

- F-AUTH: `WRONGTOKEN:REALTOKEN` → 401 (prefix is not an exact match,
  so no suffix strip rescues it). `REALTOKEN:` (empty suffix) → 401
  (suffix must be non-empty; a bare trailing `:` is not the sanctioned
  suffix form). Configured token containing `:` → settings validation
  raises at load, refusing to start with an un-presentable secret.
- F-HEALTH: `HEAD`/`OPTIONS` probes (`api/routes.py:176-179`) keep
  their current unauthenticated liveness behavior (they carry no
  internals).
- F-PASS: `model="open_router/<anything>"` when OpenRouter is NOT a
  configured/sanctioned target → refused (400), not silently routed
  off-chain. Client `extra_body={"request_id": "x"}` → key stripped;
  the server's own `request_id` (if any) is used.

### Failure scenarios

- All three findings are fail-open today; each fix fails CLOSED: an
  ambiguous credential is rejected, undisclosed internals stay
  undisclosed to anonymous callers, and an unsanctioned model or
  smuggled server-owned `extra_body` key is refused/stripped rather
  than honored.

## Architecture Impact

- Adds/Removes dependency: none — in-place modification of
  `api/dependencies.py`, `api/routes.py`, `routing/table.py`, and
  `providers/nvidia_nim/request.py` (each owned by an existing spec);
  introduces no new cross-owner import. A small shared sanctioning
  predicate may live beside the routing table in its existing module.
- New / changed coupling, cycles, or shared state: none. The
  `extra_body` whitelist is a module-local constant in
  `providers/nvidia_nim/request.py`.

## Diagram

N/A (in-place hardening of three existing edge paths).

## Acceptance Criteria

- [ ] AC1 (F-AUTH unit): `require_api_key` behavior table — `REALTOKEN`
  and `Bearer REALTOKEN` → allowed; `REALTOKEN:model` → allowed;
  `WRONGTOKEN:REALTOKEN`, `REALTOKEN:` (empty suffix), and a plain
  wrong token → 401; a configured token containing `:` raises at
  settings validation. Constant-time comparison preserved
  (`secrets.compare_digest` over the full candidate).
- [ ] AC2 (INTEGRATION — FastAPI `TestClient`, real app, settings via
  `app.dependency_overrides[get_settings]`): drive all three findings
  end-to-end.
  (a) With token configured: anonymous `GET /health` → 200 and the body
  carries liveness only (no `breaker`/per-ref internals); the same
  request with a valid key → 200 and the body carries `breaker`.
  (b) A `:`-suffixed presented credential authenticates per the
  tightened rule and a `WRONGTOKEN:REALTOKEN` credential is rejected.
  (c) A chat request with `model="open_router/<anything>"` that is NOT
  sanctioned → 400 (not routed off-chain), and a chat request carrying
  a smuggled server-owned `extra_body` key reaches the NIM upstream
  with that key stripped — asserted by inspecting the built body via a
  truthful boundary fake (`httpx.MockTransport` capturing the upstream
  POST), never by monkeypatching ferova code.
- [ ] AC3: promised test files + selectors —
  `tests/unit/test_proxy_require_api_key.py::test_suffix_strip_requires_exact_prefix`,
  `::test_colon_bearing_token_rejected_at_config`;
  `tests/unit/test_proxy_health_auth.py::test_health_internals_require_auth`;
  `tests/unit/test_proxy_passthrough_guard.py::test_unsanctioned_model_refused`,
  `::test_client_extra_body_server_keys_stripped`.
- [ ] AC4: `ruff` + `ruff format --check` + `pytest tests/unit` green;
  zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`;
  `ferova arch graph --check` exits 0.

## Open Questions

- OQ1: `/health` shape — authenticated field on `GET /health` vs a
  dedicated authenticated `GET /health/detail`. Both satisfy G2; settle
  in review, favoring whichever keeps existing operator dashboards
  working and composes cleanly with SP-CREDITS-CHECK's `credits` field.
- OQ2: the sanctioned-passthrough predicate — "provider present in any
  configured chain" vs "provider in an explicit allow-list setting".
  Default to the former (zero new config) unless review prefers an
  explicit knob.
