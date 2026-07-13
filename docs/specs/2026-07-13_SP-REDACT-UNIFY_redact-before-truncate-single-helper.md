---
id: SP-REDACT-UNIFY
title: Redact secrets before truncating, via one shared helper
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

# Redact secrets before truncating, via one shared helper

## Intent

Three probe sites truncate an exception string BEFORE redacting the API key, so
a key cut mid-string leaks its prefix into logs; and `_redact` is copy-pasted
identically three times. Redact first, then truncate, from a single shared
helper.

## Context

Identical bug at three call sites, each `_redact(f"...{str(exc)[:120]}", api_key)`
— the `[:120]` slice runs BEFORE `_redact`'s `text.replace(secret, "***")`, so a
key straddling the 120-char boundary is truncated to a leaking prefix that
`replace` can no longer match:
- `src/ferova/review/chain_health.py:143` (helper at `chain_health.py:50-52`)
- `src/ferova/llm_proxy/providers/model_catalog.py:147` (helper at
  `model_catalog.py:81`)
- `src/ferova/llm_proxy/providers/cell_probe.py:221` (helper at
  `cell_probe.py:100`)

`_redact` is byte-for-byte identical in all three (`text.replace(secret, "***")
if secret else text`). Audit 2026-07-13 finding M23.

## Goals

- G1: redaction happens BEFORE truncation at all three sites — redact the full
  `str(exc)` against the key, THEN truncate the redacted string to the display
  cap. No key substring can survive.
- G2: a SINGLE shared `_redact` helper backs all three sites; the two duplicate
  copies are removed and import the one owner.
- G3: the display cap (120 chars) is preserved on the final redacted string.

## Non-Goals

- NG1: no change to log event names, severities, or the `ModelHealth`/probe
  return shapes.
- NG2: no general secret-scanning of exception text beyond replacing the known
  `api_key` (that is the current contract; broadening it is out of scope).

## Assumptions

- A1: the three sites all have the plaintext `api_key` in scope at the redaction
  point (they do — each passes it today).
- A2: a shared helper is importable by all three without a cycle — see the
  ownership note below.

## Interface

Ownership decision (CRITICAL): rather than create a new util module (which would
need a fresh `owns.code` declaration and an arch edge from each consumer), pick
ONE existing owner and have the other two import it. Chosen owner: the llm_proxy
provider helper — consolidate on a single `_redact` (renamed to a public
`redact_secret(text: str, secret: str, *, limit: int = 120) -> str` that redacts
THEN truncates) in an existing already-owned llm_proxy leaf module that both
`model_catalog.py` and `cell_probe.py` already sit beside. `chain_health.py`
(in `review/`) imports the SAME helper.

- If, and only if, no suitable existing leaf owner exists without creating a
  cross-owner cycle, create a new leaf `src/ferova/core/redact.py`, declare
  `owns.code: [src/ferova/core/redact.py]` on THIS spec, note it in Architecture
  Impact, and add the `depends_on` edges from the consuming specs. Prefer the
  no-new-module path.

New signature (single copy):
`redact_secret(text: str, secret: str, *, limit: int = 120) -> str` —
`(text.replace(secret, "***") if secret else text)[:limit]`.

## Behavior

### Nominal

`redact_secret(str(exc), api_key)` returns the exception text with the key
masked, then truncated to 120 chars.

### Edge cases

- Key straddling the 120-char boundary -> fully masked BEFORE truncation, so the
  truncated result contains no key fragment (the bug this spec fixes).
- Empty/absent secret -> text truncated unchanged (no-op redaction).
- Key appears multiple times -> all occurrences masked (str.replace replaces
  all).

### Failure scenarios

- Fail CLOSED: redaction precedes every truncation, so there is no ordering
  under which a prefix leaks.

## Architecture Impact

- Preferred path: no new module — one existing llm_proxy leaf owns the single
  `redact_secret`; `chain_health.py` (review) and the two provider sites import
  it. This adds ONE cross-package import edge (review -> the chosen llm_proxy
  leaf); if that edge would violate the arch graph's layering, use the
  `core/redact.py` fallback in Interface instead and declare it. The spec's
  final form MUST leave `ferova arch graph --check` green.
- New / changed coupling, cycles, or shared state: the three sites now depend on
  one helper — coupling DECREASES (dedup); no cycle.

## Diagram

N/A (in-place consolidation).

## Acceptance Criteria

- [ ] AC1: unit — `redact_secret("prefix" + full_key + "suffix", full_key)`
  where `full_key` straddles the 120-char boundary returns a string containing
  NO substring of `full_key` (assert the key and every >=8-char slice of it is
  absent); empty-secret and multi-occurrence cases covered.
- [ ] AC2 (INTEGRATION): drive a real probe path end to end — invoke the actual
  transport-failure branch of one probe site (e.g. `chain_health` head probe)
  through an `httpx.MockTransport` (truthful boundary fake) whose handler raises
  an exception whose message embeds the full API key; assert the emitted
  `ModelHealth.detail` / log record contains no key substring. No monkeypatching
  of Ferova code.
- [ ] AC3: promised test file + selectors —
  `tests/unit/test_redact_secret.py::test_redacts_before_truncating` and
  `tests/unit/test_chain_health.py::test_probe_error_detail_never_leaks_key`.
- [ ] AC4: `ruff` + `ruff format --check` + `pytest tests/unit` green; zero
  inline comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`;
  `ferova arch graph --check` exits 0.

## Open Questions

(none)
