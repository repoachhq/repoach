---
id: SP-NIM-PROBE-UNPARSEABLE-DIAG
title: Log status code and body snippet on unparseable NIM chain-head probes
version: 0.1
status: approved
author: agent
created: 2026-07-24
updated: 2026-07-24

owns:
  code: N/A
  resources: N/A

depends_on: [SP-NIM-CHAIN-HEALTH]
provides_to: []

constraints: {}
---

# Log status code and body snippet on unparseable NIM chain-head probes

## Intent

`probe_nim_model`'s `except ValueError` branch — reached when a NIM
head returns a body `resp.json()` cannot parse — logs only
`detail = f"json_decode: {type(exc).__name__}"`: no HTTP status, no
body content. During a multi-hour incident the archived log is the
only surviving signal, and every line of it reads identically
regardless of whether NIM actually returned a 5xx HTML error page, a
rate-limit banner, or a truncated stream — there is no way to
root-cause the next occurrence after the fact. Extend that branch to
include `resp.status_code` and a redacted, 200-char body snippet in
the `nim_chain_probe_unparseable` warning, at the same diagnostic
depth the sibling transport-failure branch already gives via
`redact_secret(...)`.

## Context

- `src/repoach/review/chain_health.py:143-148` (current `probe_nim_model`):

  ```python
  try:
      content = _extract_content(resp.json())
  except ValueError as exc:
      detail = f"json_decode: {type(exc).__name__}"
      _log.warning("nim_chain_probe_unparseable", tier=tier, model=model, detail=detail)
      return ModelHealth(tier, model, STATUS_ERROR, latency_s, 0, detail)
  ```

  Confirmed present verbatim on `develop` at HEAD (`29102b0`) —
  re-grepped 2026-07-24, selectors unchanged, problem still real.
  `resp` (the already-received `httpx.Response`) is in scope but
  `resp.status_code` and `resp.text` are never read here.

- `src/repoach/review/chain_health.py:138-141`, the sibling
  transport-failure branch three lines above, for contrast:

  ```python
  except httpx.HTTPError as exc:
      detail = redact_secret(f"{type(exc).__name__}: {exc}", api_key)
      _log.warning("nim_chain_probe_transport_failed", tier=tier, model=model, detail=detail)
      return ModelHealth(tier, model, STATUS_ERROR, None, 0, detail)
  ```

  This branch already runs the exception text through
  `redact_secret` (`src/repoach/llm_proxy/providers/model_catalog.py:82`,
  redact-before-truncate, `limit` defaults to 120) before logging it.
  The unparseable branch has no equivalent call at all — it is the
  one branch in this function that drops all response detail.

- `logs/nim_health.log`: 18 `nim_chain_probe_unparseable` events
  2026-07-04 through 2026-07-22, including a sustained burst of 13
  occurrences for `model=deepseek-ai/deepseek-v4-pro tier=sonnet`
  roughly every 15 minutes from 2026-07-21T03:25 to 08:11 (~4h45m of
  continuous non-JSON responses from the sonnet head). Every line
  reads identically `detail='json_decode: JSONDecodeError'` — zero
  information distinguishing a 500, a 429, or a truncated 200 body.

- `src/repoach/review/chain_health.py` is owned by `SP-NIM-CHAIN-HEALTH`
  (`docs/specs/2026-06-09_SP-NIM-CHAIN-HEALTH_head-health-monitor.md`);
  this spec's `depends_on` names that spec and edits the file under
  that existing coupling, exactly as
  `SP-PROXY-EARLY-ABORT-ERROR-FRAME`'s `depends_on:
  [SP-PROXY-FIRST-BYTE-DEADLINE]` edited a file it did not own. No new
  module is introduced, so `owns.code: N/A`.

- The identical `detail = f"json_decode: {type(exc).__name__}"`
  pattern also exists at
  `src/repoach/llm_proxy/providers/cell_probe.py:230` and
  `src/repoach/llm_proxy/providers/model_catalog.py:180`. Both of
  those already check `resp.status_code` in a preceding branch
  (`cell_probe.py:222-225`, `model_catalog.py:172-175`) before
  attempting `resp.json()`, so their blind spot is narrower than
  `chain_health.py`'s (which calls `resp.json()` unconditionally on
  any status). They are out of scope here (see Non-Goals) — the
  proposed direction names only `probe_nim_model`.

## Goals

- G1: on `resp.json()` raising `ValueError` inside `probe_nim_model`,
  the resulting `detail` string and the `nim_chain_probe_unparseable`
  warning log both include the numeric `resp.status_code`.
- G2: the same `detail`/log also include a body snippet — the raw
  response text run through `redact_secret(resp.text, api_key,
  limit=200)` (redact-then-truncate, matching the transport branch's
  use of the same shared helper, at a 200-char cap instead of the
  120-char default) — so a leaked API key in the echoed body is
  masked exactly like every other diagnostic string in this module.
- G3: the returned `ModelHealth.detail` for this branch carries the
  same enriched string (status + redacted body snippet), not just the
  exception class name, so `repoach monitor-chains` CLI output and any
  downstream consumer of `ModelHealth.detail` (e.g. the
  `nim_health_probe` persistence written per
  `SP-NIM-HEALTH-HISTORY`) sees the same diagnostic depth the
  structured log gets.
- G4: the fix is confined to the `except ValueError` block of
  `probe_nim_model` — no other branch, function, or module changes
  behavior.

## Non-Goals

- NG1: no behavior change beyond the `except ValueError` block's
  logged/returned `detail` string — `classify()`, the transport-failure
  branch, the success/info-log branch (`chain_health.py:149-159`), and
  every other function in the module are untouched.
- NG2: no change to `ModelHealth`'s field set
  (`src/repoach/health/model_health.py`) — `detail` stays a plain
  `str`; no new field is added to carry status/body separately.
- NG3: no fix to the sibling `json_decode` blind spots in
  `src/repoach/llm_proxy/providers/cell_probe.py:230` or
  `src/repoach/llm_proxy/providers/model_catalog.py:180` — same
  pattern, different owning specs, left for a future targeted spec.
- NG4: no change to `redact_secret` itself
  (`src/repoach/llm_proxy/providers/model_catalog.py:82`) — this spec
  is a caller passing an explicit `limit=200`, not a change to the
  helper's default or contract.
- NG5: no retry, backoff, or alerting behavior added around repeated
  `nim_chain_probe_unparseable` events — this spec only makes each
  individual occurrence legible after the fact; sustained-burst
  detection is separate future work.

## Interface

`src/repoach/review/chain_health.py`, `probe_nim_model` (signature
unchanged):

```python
async def probe_nim_model(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model: str,
    *,
    tier: str,
    prompt: str = _PROBE_PROMPT,
    max_tokens: int = 16,
    timeout_s: float = 30.0,
    slow_threshold_s: float = 8.0,
) -> ModelHealth:
```

No new public names. The `except ValueError` block's body changes
from:

```python
except ValueError as exc:
    detail = f"json_decode: {type(exc).__name__}"
    _log.warning("nim_chain_probe_unparseable", tier=tier, model=model, detail=detail)
    return ModelHealth(tier, model, STATUS_ERROR, latency_s, 0, detail)
```

to (illustrative shape; no inline comments in the real diff):

```python
except ValueError as exc:
    body_snippet = redact_secret(resp.text, api_key, limit=200)
    detail = (
        f"json_decode: {type(exc).__name__} status={resp.status_code} "
        f"body={body_snippet!r}"
    )
    _log.warning(
        "nim_chain_probe_unparseable",
        tier=tier,
        model=model,
        status_code=resp.status_code,
        body_snippet=body_snippet,
        detail=detail,
    )
    return ModelHealth(tier, model, STATUS_ERROR, latency_s, 0, detail)
```

`status_code`/`body_snippet` are added as explicit `structlog`
keyword fields (grep/JSON-query friendly) in addition to being folded
into `detail` (so `ModelHealth.detail`/CLI output stays a single
self-describing string per G3).

## Behavior

### Nominal

- NIM returns a 200 with a well-formed JSON body →
  `_extract_content` succeeds, the `except ValueError` block never
  runs, behavior is byte-for-byte unchanged from today.

### Edge cases

- NIM returns a non-2xx status (e.g. 503) with a non-JSON HTML error
  page → `resp.json()` raises `ValueError`; the new branch logs
  `status_code=503` and a redacted, truncated snippet of the HTML
  body, and `ModelHealth.detail` reads e.g. `"json_decode:
  JSONDecodeError status=503 body='<html>...'"`.
- NIM returns a 200 with a truncated/malformed JSON stream (the
  incident's actual shape — `resp.status_code` still 2xx) →
  `status_code=200` appears in the log, distinguishing "head is up
  but streaming garbage" from "head is down", which the old log could
  never show.
- The echoed body happens to contain the literal `api_key` value
  (a provider error page reflecting the sent `Authorization` header,
  or a proxy misconfiguration) → `redact_secret` masks every
  occurrence with `"***"` before truncation, so the key can never
  leak even if it straddles the 200-char cutoff (same
  redact-before-truncate guarantee `SP-REDACT-UNIFY` established for
  the transport branch).
- The body is longer than 200 characters → `redact_secret`'s
  `limit=200` truncates the already-redacted string; the log and
  `detail` carry only the first 200 characters, keeping log lines
  bounded during a repeating-burst incident.

### Failure scenarios

- `resp.text` itself raises (e.g. an encoding it cannot decode) →
  out of scope for this spec; `httpx.Response.text` already handles
  the standard encoding-fallback cases internally, and `probe_nim_model`
  overall keeps its "never raises" contract only for its own explicit
  `try`/`except` blocks (unchanged number of them — no new bare
  `except` is added, `# noqa`/inline comments remain zero).

## Architecture Impact

- `src/repoach/review/chain_health.py` is owned by
  `SP-NIM-CHAIN-HEALTH` (`owns.code`); this spec's `depends_on:
  [SP-NIM-CHAIN-HEALTH]` is the edge that authorizes editing it — no
  additional edge is introduced. `owns.code: N/A` — no new module.
  `redact_secret`'s import (`repoach.llm_proxy.providers.model_catalog`)
  is pre-existing in this file (already imported for the
  transport-failure branch) — this spec adds a second call site, not
  a new import.
- New / changed coupling, cycles, or shared state: none. No new
  dependency edge, no schema change, no new persisted field.

## Diagram

N/A (in-place fix confined to one `except` block).

## Acceptance Criteria

- [ ] AC1: unit —
  `tests/unit/test_chain_health.py::test_probe_includes_status_and_body_snippet_on_unparseable_response`.
  Extend the existing `_FakeResponse` test double with an optional
  `text: str = ""` constructor field (set as `self.text`), then drive
  `probe_nim_model` through `_FakeClient` with `_FakeResponse(503,
  ValueError("Expecting value: line 1 column 1 (char 0)"),
  text="<html>Service Unavailable</html>")`. Assert `result.status ==
  "error"` and both `"503" in result.detail` and `"Service
  Unavailable" in result.detail`. FAILS on today's code (`detail ==
  "json_decode: ValueError"` contains neither substring); PASSES
  after the fix.
- [ ] AC2: unit — same file,
  `test_unparseable_response_body_snippet_is_redacted`. Build a
  `_FakeResponse(500, ValueError("boom"), text=f"error token {key} in
  upstream, tail-marker-present")` with a distinctive `key =
  "sk-secret-999"` passed as `probe_nim_model`'s `api_key`. Assert
  `"tail-marker-present" in result.detail` (proves the body snippet is
  actually included — FAILS on today's code, which never touches
  `resp.text`) AND `key not in result.detail` AND `"***" in
  result.detail` (proves `redact_secret` ran on the body, not just the
  exception text).
- [ ] AC3: unit — same file,
  `test_unparseable_response_body_snippet_truncated_to_200_chars`.
  Build a body `"a" * 250 + "TAIL_BEYOND_200"` (the marker starts at
  character 251). Assert `"a" * 50 in result.detail` (proves a body
  prefix is present — FAILS on today's code, which includes no body at
  all) AND `"TAIL_BEYOND_200" not in result.detail` (proves the
  200-char cap is honored, distinguishing this from an unbounded body
  dump).
- [ ] AC4: unit — same file, `test_unparseable_response_status_code_is_a_log_field`.
  Add an autouse fixture that rebinds the module logger before the
  capture (`monkeypatch.setattr(chain_health, "_log",
  structlog.get_logger("chain_health.test"))`) — the same fix already
  applied to the refutation suites in this repo (commit
  `4c7f651`, "refutation suites rebind the module logger for
  capture_logs") to defeat `cache_logger_on_first_use=True` swallowing
  the `capture_logs` swap. Inside `structlog.testing.capture_logs()`,
  drive the AC1 scenario and assert the captured
  `nim_chain_probe_unparseable` event dict has `status_code == 503` as
  an explicit key (not only embedded in the `detail` string) — FAILS
  on today's code (the key is absent from the event dict entirely).
- [ ] AC5: existing tests in `tests/unit/test_chain_health.py` —
  `test_probe_returns_error_on_transport_failure`,
  `test_probe_classifies_real_content_as_ok`,
  `test_api_key_redacted_in_error_detail`,
  `test_probe_error_detail_never_leaks_key`,
  `test_non_nim_head_is_skipped`,
  `test_empty_content_head_classified_empty` — all still pass
  unmodified, proving the transport-failure branch, the success path,
  and the skip path are byte-for-byte unaffected (NG1).
- [ ] AC6: `ruff check` + `ruff format --check` green; zero inline
  comments (SP-NO-INLINE-COMMENTS-GATE) and no `# noqa` anywhere in the
  diff; full `pytest tests/unit` green; `repoach arch graph --check`
  exits 0 (no new ownership conflict — `owns.code: N/A`, edit made
  under the `depends_on: [SP-NIM-CHAIN-HEALTH]` edge).

## Open Questions

None.
