# SP-VERIFIER-DOCSTRING-EXEMPT — the docstring verifier honours the project's D exemptions

## Metadata

- **Status**: OPEN
- **Priority**: P1 — a verifier false-positive that would cause false
  merge blocks after the flip
- **Owner**: operator
- **Executor**: `ferova develop`
- **Opened**: 2026-06-14

## Why

The SP-MERGE-GATE-SHADOW dry-run over recent PRs flagged ONE divergence
from the 4/4 reality: it would BLOCK #378 on a "verified" blocking
`missing_docstring` finding. Investigation: the Scribe comment was
actually about test *placement*, mis-typed `missing_docstring` by the
lens default, and the mechanical docstring verifier then ran on
`tests/unit/test_review_team.py` and counted 22 undocumented public
symbols — because **test functions conventionally carry no docstrings**.
The project's `pyproject.toml` exempts `tests/`, `scripts/` and
`src/ferova/llm_proxy/` from ruff's `D` rules entirely, so a
missing-docstring claim on those paths is a false positive by the
project's own policy. Left unfixed, this would block real merges once
the gate flips (7b). Exactly the bug shadow-first exists to catch.

## What

In `src/ferova/review/finding_verifiers.py` — `_verify_missing_docstring`
refutes a finding whose `file` is under any prefix in a new
`_DOCSTRING_EXEMPT_PREFIXES = ("tests/", "scripts/",
"src/ferova/llm_proxy/")` (mirroring the pyproject per-file
ignores), with result `"path is docstring-exempt (pyproject D)"`,
before reading/parsing the file.

## Files in scope

- `src/ferova/review/finding_verifiers.py`
- `tests/unit/test_finding_verifiers.py`

## Out of scope

- The coarse lens-default claim_type that mis-typed the comment
  (claim-type refinement is a later concern; this fixes the acute
  verifier false-positive).
- Keeping the exempt list in sync with pyproject automatically (the
  three prefixes are stable; revisit if the ignores change).

## Smoke scenario

A `missing_docstring` finding on `tests/unit/test_x.py` (undocumented
test function) → `verify_finding` returns `REFUTED` with the
exempt-path reason; a `missing_docstring` on an undocumented `src/`
public function still verifies.

## Definition of Done

- Exempt-path refutation pinned —
  `test_missing_docstring_refuted_on_exempt_path`.
- The src/ documented/undocumented cases still behave as before —
  existing `test_missing_docstring_*` stay green.
- Re-running the shadow dry-run no longer blocks #378.
- `ruff` + `ruff format --check` + `pytest tests/unit` green; zero
  inline comments; no `# noqa`.

## Commit plan

1. `fix(review): docstring verifier refutes claims on D-exempt paths`

## Risks

- **A genuinely undocumented src/ symbol mis-cited under tests/**:
  impossible — the verifier keys on the finding's file path, which is
  the cited location.
