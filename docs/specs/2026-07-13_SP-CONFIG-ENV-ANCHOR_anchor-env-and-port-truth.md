---
id: SP-CONFIG-ENV-ANCHOR
title: Anchor env-file loading to the repo root and end the :8082 fallback lie
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

# Anchor env-file loading to the repo root and end the :8082 fallback lie

## Intent

`Settings` loads its `.env`/`chains.env` against the current working
directory and, when that resolves to nothing, silently falls back to
`http://localhost:8082` — but the deployed proxy listens on `:8084`
and `:8082` is squatted by another process, so the
`FEROVA_ANTHROPIC_AUTH_TOKEN` bearer secret is POSTed to the wrong
service. Anchor config loading to the repo/package root and fail loud
instead of silently defaulting to a wrong, secret-leaking URL.

## Context

Audit 2026-07-13 finding M15 plus the port-truth lows.

- `src/ferova/core/config.py:38-44`:
  `model_config = SettingsConfigDict(env_file=("chains.env", ".env"), ...)`
  — relative names resolved against CWD by pydantic-settings. Invoked
  outside the repo root (a systemd unit, a cron timer, a foreign CWD),
  Settings loads NEITHER file.
- `src/ferova/core/config.py:87`:
  `llm_proxy_base_url: str = "http://localhost:8082"` — the silent
  default that takes over when the env files are not found.
- `src/ferova/agent_engine/agent_loop.py:332`:
  `base_url = (settings.llm_proxy_base_url or "http://localhost:8082").rstrip("/")`
  — the SAME `:8082` literal duplicated at the call site, so even a
  future config fix leaves a second hardcoded fallback.
- `agent_loop.py:320-330` already raises when the auth token is
  missing; the danger is the URL, which currently never fails — it just
  points the bearer at the wrong host.
- Ground truth: `deploy/systemd/ferova-llm-proxy.service:22` binds
  `FEROVA_PROXY_PORT=8084` (documented in `deploy/` and
  `docs/tech_debt.md`), while `CLAUDE.md` and code say `:8082`
  throughout. `:8082` is owned by the sharp-agent stack on this host.

`config.py` is owned by an existing config spec; this is an in-place
modification. `CLAUDE.md` and `deploy/` are OPERATOR-OWNED — any
doc/port-string edits there are operator-manual (see Non-Goals).

## Goals

- G1: `.env` and `chains.env` are resolved against the repository /
  package root (a stable anchor), not the process CWD, so Settings
  loads the same files regardless of where the process starts.
- G2: when neither anchored env file exists AND no
  `FEROVA_LLM_PROXY_BASE_URL` is present in the environment, Settings
  FAILS LOUD (raises at construction) rather than silently yielding the
  `:8082` default and shipping the bearer to a wrong service.
- G3: a single source of truth for the proxy base URL — the duplicated
  hardcoded `:8082` fallback at `agent_loop.py:332` is removed; the
  call site reads `settings.llm_proxy_base_url` with no literal
  fallback of its own.
- G4: the documented default reflects reality on this host (`:8084`) OR
  the value is made fully config-driven with no baked-in host default
  that can silently win.

## Non-Goals

- NG1: no change to `deploy/systemd/*.service` or `CLAUDE.md` inside
  this spec's Execution — those are operator-owned. Where the port
  string in `CLAUDE.md`/`deploy/` must change to match, mark it
  OPERATOR-MANUAL: the operator edits those by hand.
- NG2: no change to the `env=prod` auth-token boot guard
  (`config.py:129-148`) beyond adding the URL guard alongside it.
- NG3: no new settings module — the anchor logic lives in the existing
  `Settings` definition.
- NG4: no rework of the llm_proxy's own `Settings`
  (`src/ferova/llm_proxy/config/settings.py`); this spec is the
  factory-side `core/config.py`.

## Assumptions

- A1: the package root is discoverable from `config.py` (walk up to the
  directory containing `pyproject.toml`, or use the installed package
  location) — the same anchor strategy the lint CLIs adopt.
- A2: an explicit `FEROVA_LLM_PROXY_BASE_URL` in the real environment
  is authoritative and must always win over any file/default (the
  systemd unit sets exactly this).

## Interface

`src/ferova/core/config.py`:
- Compute an anchored, absolute env-file tuple (e.g. a module-level
  `_repo_root()` helper returning the dir containing `pyproject.toml`)
  and pass absolute paths to `SettingsConfigDict(env_file=...)`.
- `llm_proxy_base_url` loses its baked-in `:8082` string default; make
  it a required-with-guard field: if unset after the anchored load and
  the environment, the `model_validator` raises
  `ValueError("llm_proxy_base_url unresolved — no anchored env file and
  no FEROVA_LLM_PROXY_BASE_URL; refusing to default to a wrong proxy")`.

`src/ferova/agent_engine/agent_loop.py:332`:
- `base_url = settings.llm_proxy_base_url.rstrip("/")` — no `or
  "http://localhost:8082"` fallback.

## Behavior

### Nominal

- Process started from any CWD with the repo's `.env`/`chains.env`
  present at the anchored root → Settings loads them; base URL resolves
  to the configured value (`:8084` on this host).

### Edge cases

- `FEROVA_LLM_PROXY_BASE_URL` set in the environment, no files → that
  value wins, no raise.
- Anchored files present but no base-url key anywhere → the field's
  documented, reality-aligned default applies (G4) OR raises per G2 if
  no default is kept; the design MUST NOT resolve to `:8082`.

### Failure scenarios

- No anchored env file found AND no `FEROVA_LLM_PROXY_BASE_URL` in the
  environment → fail CLOSED: `Settings()` raises at construction. The
  process never boots pointing the bearer secret at `:8082`.

## Architecture Impact

- Adds/Removes dependency: none — in-place modification of
  `core/config.py` (owned by an existing config spec) and one literal
  removed from `agent_loop.py` (owned by an existing agent-engine
  spec); no new cross-owner import.
- New / changed coupling, cycles, or shared state: removes a duplicated
  constant (the `:8082` literal) — reduces coupling to a single source
  of truth in `Settings`.

## Diagram

N/A (in-place fix).

## Acceptance Criteria

- [ ] AC1: unit — with the anchored env files present, `Settings()`
  constructed from a tmp foreign CWD (via `monkeypatch.chdir(tmp_path)`)
  loads the anchored files and resolves the configured base URL; with
  no anchored files and no `FEROVA_LLM_PROXY_BASE_URL`, `Settings()`
  raises `ValueError`.
- [ ] AC2 (INTEGRATION): drive the real resolution flow — from a
  foreign CWD, construct `Settings` (or call `get_settings()` after
  clearing the cached singleton) and assert it either finds the
  anchored env files or raises, and NEVER silently yields
  `http://localhost:8082`; then assert `AgentLoop`'s base-url
  computation (`agent_loop.py:332`) reads that resolved value with no
  `:8082` fallback of its own (patch `settings.llm_proxy_base_url` to a
  sentinel and assert the client targets the sentinel host).
- [ ] AC3: promised tests —
  `tests/unit/test_config.py::test_env_files_anchored_from_foreign_cwd`,
  `::test_unresolved_base_url_raises`, and
  `tests/unit/test_agent_loop.py::test_base_url_has_no_hardcoded_fallback`.
- [ ] AC4: OPERATOR-MANUAL follow-up recorded in the PR body — the
  operator aligns the `:8082` strings in `CLAUDE.md` and `deploy/` (and
  `docs/tech_debt.md`) with `:8084`; the bots do not touch those files.
- [ ] AC5: `ruff` + `ruff format --check` + `pytest tests/unit` green;
  zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`;
  `ferova arch graph --check` exits 0.

## Open Questions

(none)
