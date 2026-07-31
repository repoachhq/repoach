---
id: SP-CHAIN-STATUS-PROXY-DEFAULT
title: chain-status --proxy-url defaults from settings host/port, not a stale :8082 literal
version: 0.1
status: approved
author: agent
created: 2026-07-24
updated: 2026-07-24

owns:
  code: []
  resources: []

depends_on: [SP-CHAIN-STATUS-DIGEST]
provides_to: []

constraints: {}
---

# chain-status --proxy-url defaults from settings host/port, not a stale :8082 literal

## Intent

`repoach review chain-status` run with no `--proxy-url` flag against the
real deployed proxy silently prints `proxy: unreachable (breaker state
unknown)` instead of the true breaker snapshot, because the CLI's
`--proxy-url` `typer.Option` default is a hardcoded
`"http://127.0.0.1:8082"` literal that never matches an operator's
deployed port (`:8084` per `CLAUDE.md`). Resolve the default from the
already-constructed `Settings` (`settings.host` / `settings.port`)
inside the function body, the same pattern the two lines below it
already use for `db_path` (`target_db = db_path if db_path else
get_settings().db_path`), so the digest degrades to the configured
port instead of a baked-in wrong one.

## Context

- `src/repoach/cli/chain_status.py:185-187` —
  ```python
  proxy_url: str = typer.Option(
      "http://127.0.0.1:8082", "--proxy-url", help="Base URL of the running llm_proxy."
  ),
  ```
  the default is evaluated once at Typer option-declaration time and is
  a bare string literal, never resolved from configuration.
- `src/repoach/cli/chain_status.py:200-207` — `settings = LSettings()`
  is constructed two lines above `target_db = db_path if db_path else
  get_settings().db_path`, and `proxy_url` (still the hardcoded
  default when the flag is omitted) is passed straight into
  `build_chain_status(..., proxy_url=proxy_url, ...)` without ever
  consulting `settings`.
- `src/repoach/llm_proxy/config/settings.py:390-391` — `Settings`
  already models `host: str = Field(default="127.0.0.1", ...)` and
  `port: int = Field(default=8082, ...)`, the exact fields
  `src/repoach/llm_proxy/__main__.py:36-37` uses to bind the real
  server (`host=settings.host, port=settings.port`) — so `settings`
  already carries the operator's configured port; `chain_status.py`
  simply never reads it.
- `src/repoach/cli/chain_status.py:141-144` —
  `_build_breaker_lines` catches any `client.get(...)` failure and
  renders exactly `"  proxy: unreachable (breaker state unknown)"`,
  which is the observable symptom: a wrong default port degrades
  silently into this line with no indication the URL itself was
  wrong.
- Per `CLAUDE.md`, `llm_proxy` code-default is `:8082` but the deployed
  instance runs `:8084` — so on this host, the current hardcoded
  default is provably wrong every time the flag is omitted.
- `chain_status.py` is owned in full by `SP-CHAIN-STATUS-DIGEST`
  (verified: `grep -rl "cli/chain_status.py" docs/specs/ | xargs grep
  -l "owns:"` finds only `SP-CHAIN-STATUS-DIGEST`'s frontmatter
  listing it under `owns.code`); this spec is an in-place modification
  of that owned file, hence `owns.code: []` and `SP-CHAIN-STATUS-DIGEST`
  in `depends_on` (precedent: `SP-PROXY-EARLY-ABORT-ERROR-FRAME`,
  `SP-NIM-PROBE-UNPARSEABLE-DIAG`). `tests/unit/test_chain_status.py`
  is not listed under any spec's `owns.code` (test files are not owned
  artifacts per the `SP-TEST-PARALLEL` precedent), so adding a test
  there introduces no conflict either.

## Goals

- G1: `--proxy-url` typer.Option default becomes `None`; when the
  flag is omitted, `chain_status()` resolves the effective proxy URL
  from the already-constructed `settings` as
  `f"http://{settings.host}:{settings.port}"`.
- G2: when `--proxy-url` is passed explicitly, the passed value wins
  verbatim (unchanged CLI contract, no regression for existing
  callers/tests that already pass `--proxy-url` explicitly).
- G3: `build_chain_status()`'s own signature and behavior are
  untouched — the resolution happens only in the `chain_status()`
  Typer command wrapper, mirroring the existing `target_db` pattern.

## Non-Goals

- NG1: no behavior change beyond the `--proxy-url` default
  resolution — `build_chain_status`, `_build_breaker_lines`, the
  digest's line formats, and every other CLI option
  (`--window-hours`, `--db-path`) are untouched.
- NG2: no change to `Settings.host` / `Settings.port` themselves, nor
  to any other reader of those fields (e.g.
  `src/repoach/llm_proxy/__main__.py`).
- NG3: no change to how `LSettings()` is constructed or cached; the
  fix only reads fields already present on the existing `settings`
  instance at `chain_status.py:200`.
- NG4: no attempt to detect or repair a deployed-but-unreachable
  proxy beyond what the existing degradation path already does — a
  correctly-resolved-but-actually-down proxy still renders
  `proxy: unreachable`, which is correct behavior for that case.

## Interface

`src/repoach/cli/chain_status.py`, `chain_status()`:

```python
def chain_status(
    window_hours: float | None = typer.Option(...),
    db_path: str | None = typer.Option(...),
    proxy_url: str | None = typer.Option(
        None,
        "--proxy-url",
        help="Base URL of the running llm_proxy (default: settings host:port).",
    ),
) -> None:
    ...
    settings = LSettings()
    window = ...
    target_db = ...
    target_proxy_url = (
        proxy_url if proxy_url else f"http://{settings.host}:{settings.port}"
    )

    async def _run() -> str:
        async with httpx.AsyncClient() as client:
            return await build_chain_status(
                target_db, window, proxy_url=target_proxy_url, client=client, settings=settings
            )
    ...
```

`build_chain_status()` keeps its existing required `proxy_url: str`
keyword-only parameter — unchanged.

## Behavior

### Nominal

- Operator runs `repoach review chain-status` with no `--proxy-url`
  and `settings.port` resolving to the real deployed port (e.g.
  `8084`, via `REPOACH_PROXY_PORT`) → the digest's `/health` probe
  targets `http://127.0.0.1:8084`, matching the actually running
  proxy.
- Operator runs `repoach review chain-status --proxy-url
  http://example:9999` → that exact value is used, exactly as today.

### Edge cases

- `settings.host` / `settings.port` still at their field defaults
  (`127.0.0.1`, `8082`) and no override configured → the resolved
  default is `http://127.0.0.1:8082`, identical to today's literal;
  no regression for an operator who has never repointed the port.
- `--proxy-url ""` (empty string) passed explicitly → falls through
  to the resolved settings default (falsy string), matching the
  existing `target_db = db_path if db_path else ...` truthiness
  convention already used two lines below.

### Failure scenarios

- Resolved proxy URL (whether default-derived or explicit) points at
  an unreachable/non-listening host → `_build_breaker_lines` still
  catches the connection failure and renders `"  proxy: unreachable
  (breaker state unknown)"`; `chain_status()` still exits 0. This
  path is unchanged — only the input URL construction changes.

## Architecture Impact

Adds/Removes dependency: none. In-place modification inside
`src/repoach/cli/chain_status.py`, a file already owned by
`SP-CHAIN-STATUS-DIGEST` (added to `depends_on`); no new file, no new
cross-module import (`settings.host` / `settings.port` are read off
the `LSettings()` instance already constructed in this function).
`arch graph` topology is unchanged.

## Acceptance Criteria

- [ ] AC1: unit — with `--proxy-url` omitted and `settings.port`
  resolving to a non-default value (via
  `monkeypatch.setenv("REPOACH_PROXY_PORT", "9321")`, the
  `Settings.port` field's actual validation alias per
  `src/repoach/llm_proxy/config/settings.py:141,391`), the digest's
  breaker-fetch targets `http://<settings.host>:9321/health` rather
  than `:8082` — asserted via the mock transport's captured request
  URL (extend `_make_handler`/`_make_client` in
  `tests/unit/test_chain_status.py` to record the requested URL).
- [ ] AC2: unit — with `--proxy-url` explicitly supplied on the CLI,
  the supplied value is used verbatim regardless of
  `settings.host`/`settings.port` (no override of an explicit flag).
- [ ] AC3: promised test —
  `tests/unit/test_chain_status.py::test_cli_proxy_url_default_resolves_from_settings_port`.
  This test MUST FAIL on pre-change code: today's hardcoded
  `"http://127.0.0.1:8082"` Typer default ignores `settings.port`
  entirely, so a settings port of `9321` with `--proxy-url` omitted
  still targets `:8082` on the current tree, contradicting the
  assertion this test makes.
- [ ] AC4: `ruff check`, `ruff format --check`, `pytest tests/unit`
  green; zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no
  `# noqa`; existing `tests/unit/test_chain_status.py` tests that
  already pass `--proxy-url` explicitly
  (`test_cli_argv_parsing_and_exit_zero`,
  `test_cli_degradation_matrix_unreachable_proxy_and_empty_db`)
  continue to pass unmodified.

## Open Questions

(none)
