# SP-PROXY-SECURE-DEFAULTS — loopback bind, token boot guard, no hardcoded fallback secret

## Metadata

- **Status**: OPEN
- **Priority**: P2
- **Owner**: operator
- **Executor**: `ferova develop`
- **Opened**: 2026-06-11

## Why

Three audit findings converge on "the proxy is insecure by default":

- `llm_proxy/config/settings.py` defaults `host` to `"0.0.0.0"` and
  `anthropic_auth_token` to `""`; `require_api_key`
  (`api/dependencies.py`) is a documented **no-op when the token is
  empty**. A fresh deployment that forgets `.env` serves an
  unauthenticated LLM proxy on all interfaces (quota theft + driving
  the claude_code backend on the operator's Max subscription).
  `.env.example` documents `FEROVA_PROXY_HOST=127.0.0.1`, contradicting
  the code default.
- `agent_engine/agent_loop.py` substitutes the hardcoded literal
  `"freecc"` when `settings.llm_proxy_auth_token` is `None` — a shared
  secret in public source, and it makes the adjacent
  `ValueError("ANTHROPIC_AUTH_TOKEN is missing…")` unreachable for the
  None case.
- `core/config.py` *documents* a prod boot guard ("When ``env=prod`` we
  refuse to boot if this is unset") that is implemented nowhere.
- Token comparison in `dependencies.py` uses `!=`, not constant-time.

## What

1. **`src/ferova/llm_proxy/config/settings.py`**:
   - `host` default becomes `"127.0.0.1"`.
   - New `model_validator(mode="after")`: when `host` is not loopback
     (`127.0.0.1`, `localhost`, `::1`) and `anthropic_auth_token` is
     empty, raise a `ValueError` explaining that a non-loopback bind
     requires `FEROVA_ANTHROPIC_AUTH_TOKEN`.
2. **`src/ferova/llm_proxy/api/dependencies.py`** — compare tokens
   with `secrets.compare_digest` (both operands encoded), preserving
   the existing bearer/`x-api-key`/suffix-stripping behaviour.
3. **`src/ferova/agent_engine/agent_loop.py`** — remove the
   `"freecc"` fallback: when `llm_proxy_auth_token` is `None` or
   empty, raise `ValueError` naming the real variable
   (`FEROVA_ANTHROPIC_AUTH_TOKEN`).
4. **`src/ferova/core/config.py`** — implement the documented
   guard as a `model_validator(mode="after")`: `env == "prod"` and
   `llm_proxy_auth_token` unset → `ValueError`. The field description
   stops being fiction.
5. **`.env.example`** — keep `FEROVA_PROXY_HOST=127.0.0.1` (now matching
   the code default) and add a comment line documenting that the token
   is mandatory for any agent run and for non-loopback binds.

## Files in scope

- `src/ferova/llm_proxy/config/settings.py`
- `src/ferova/llm_proxy/api/dependencies.py`
- `src/ferova/agent_engine/agent_loop.py`
- `src/ferova/core/config.py`
- `.env.example`
- `tests/unit/test_proxy_secure_defaults.py` (new)
- `tests/unit/test_agent_loop_proxy.py`

## Out of scope

- Converting provider keys to `SecretStr` in proxy settings (separate
  hygiene slice).
- The `~/.config/free-claude-code/.env` upstream-residue load path.
- CI workflow token wiring (`auto-review.yml` already exports
  `FEROVA_ANTHROPIC_AUTH_TOKEN`; the ci.yml smoke step is repaired by
  SP-CI-SMOKE-REPAIR and must export one too).

## Smoke scenario

### Setup

Instantiate the proxy `Settings` in-memory (no env files) three ways:
defaults only; `host="0.0.0.0"` with empty token; `host="0.0.0.0"`
with a token.

### Execute

Construct each, then call `require_api_key` (via the FastAPI
dependency with a stub request) using the correct token, a wrong
token, and a `bearer <token>:suffix` form.

### Expected

Defaults bind loopback and validate; non-loopback + empty token raises
at construction; non-loopback + token constructs. Correct and
bearer-with-suffix tokens pass, wrong token gets 401.

## Definition of Done

- `host` default is loopback — `test_default_host_is_loopback`.
- Non-loopback + empty token refuses to construct —
  `test_public_bind_requires_token`.
- `compare_digest` path preserves bearer/x-api-key/suffix behaviours —
  parametrised auth tests.
- `AgentLoop` (and `ProxyGatewayClient` construction) raises with the
  `FEROVA_ANTHROPIC_AUTH_TOKEN` name when the secret is unset; no
  `freecc` literal remains anywhere in `src/` — grep-style test or
  direct unit test.
- `env=prod` without `llm_proxy_auth_token` refuses to construct core
  `Settings`; `env=dev` without it still constructs —
  `test_prod_boot_guard`.
- `ruff` + `ruff format --check` + `pytest tests/unit` green; zero
  inline comments; no `# noqa`.

## Commit plan

1. `fix(proxy): loopback default bind + non-loopback token boot guard`
2. `fix(proxy): constant-time API-key comparison`
3. `fix(engine): drop freecc fallback, fail loud on missing proxy token`
4. `feat(config): implement the documented env=prod token boot guard`
5. `test: secure-default guards, auth paths, prod boot guard`

## Risks

- **Existing local/systemd deployments**: the operator's `.env` already
  pins `127.0.0.1` + a token, so behaviour is unchanged live; any
  environment relying on the implicit `freecc` handshake will now fail
  loudly at startup — that is the point, but expect one config touch
  on first redeploy.
- **Unit tests constructing AgentLoop with mocked settings** must set
  `llm_proxy_auth_token`; update fixtures rather than weakening the
  guard.
