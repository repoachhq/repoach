# SP-VERIFIER-LIB — mechanical verifiers transition proposed findings to verified | refuted

## Metadata

- **Status**: OPEN
- **Priority**: P1 — review redesign slice 4 of 11
  (docs/review_redesign_architecture.md)
- **Owner**: operator
- **Executor**: `ferova develop`
- **Opened**: 2026-06-13

## Why

Slice 3 fills `pr_findings` with `proposed` findings on every review.
This slice runs **mechanical verification** on the
mechanically-checkable claim types and transitions each finding to
`verified` (the claim is true — a real finding) or `refuted` (the
claim is false — a hallucination), reusing the hallucination guard's
on-disk primitives. The judged claim types (`design`, `security`) and
the executable ones (`broken_behavior`, `spec_gap`) are left
`proposed` for slices 5-6. Still **dual-run**: statuses are recorded,
NO merge decision changes. The verified/refuted split is the per-lens
precision signal the redesign promised, and the input slices 7/11 act
on.

"Verified" means the claim holds (test really missing, docstring
really absent, lint really violated → real finding). "Refuted" means
the repo disproves it (the symbol/docstring exists, the file is clean
→ hallucination) — exactly the hallucination guard's logic, promoted
from a comment-downgrade into a lifecycle transition.

## What

1. **New module `src/ferova/review/finding_verifiers.py`**:
   - `verify_finding(finding: Finding, *, repo_root: Path) ->
     tuple[FindingStatus, str, str]` returning
     `(new_status, method, result)`. Dispatch on
     `finding.claim_type`:
     - `ClaimType.MISSING_TEST`: extract symbols from `finding.claim`
       via `_extract_missing_test_symbols`; build a searcher with
       `make_repo_symbol_searcher(repo_root)`. If ANY symbol is found
       → `(REFUTED, "symbol_search", "<sym> exists in tests/src")`.
       If symbols were extracted and NONE found →
       `(VERIFIED, "symbol_search", "no test for <syms>")`. If no
       symbol could be extracted → `(PROPOSED, "symbol_search",
       "no checkable symbol")` (leave it).
     - `ClaimType.MISSING_DOCSTRING`: read `finding.file` via
       `make_repo_file_reader(repo_root)`; `ast.parse` it; collect
       public (`not name.startswith("_")`) module/functions/classes
       lacking a docstring (`ast.get_docstring`). If the module has a
       docstring AND no public def/class is missing one →
       `(REFUTED, "ast_docstring", "all public symbols documented")`.
       If at least one is missing → `(VERIFIED, "ast_docstring",
       "<n> public symbol(s) undocumented")`. If the file is
       absent/unparseable → `(PROPOSED, "ast_docstring",
       "file unreadable")`.
     - `ClaimType.LINT_CONVENTION`: run `scan_inline(target)` +
       `scan_silent(target)` on `repo_root / finding.file`. Violations
       → `(VERIFIED, "lint_scan", "<n> violation(s)")`; clean →
       `(REFUTED, "lint_scan", "file clean")`; absent →
       `(PROPOSED, "lint_scan", "file absent")`.
     - any other claim type (`DESIGN`, `SECURITY`, `BROKEN_BEHAVIOR`,
       `SPEC_GAP`) → `(PROPOSED, "deferred",
       "not mechanically verifiable — slices 5-6")`.
   - `verify_findings_for_pr(db_path: Path, *, pr_number: int,
     repo_root: Path, head_sha: str | None) -> dict[str, int]` — fetch
     `proposed` findings via `fetch_findings(db_path, pr_number,
     status=FindingStatus.PROPOSED)`; for each, call `verify_finding`;
     when the new status differs from `PROPOSED`, call
     `update_finding_status(db_path, finding.id, new_status,
     verification_method=method, verification_result=result,
     checked_at_sha=head_sha or "")`. Return counts
     `{"verified": n, "refuted": n, "deferred": n}`.
2. **Wiring in `src/ferova/review/orchestrator.py`** — in
   `review_pr`, immediately AFTER the
   `record_findings_for_outcomes(...)` block, add a try/except-guarded
   call to `verify_findings_for_pr(self._db_path, pr_number=pr_number,
   repo_root=self._repo_root, head_sha=head_sha)`, emitting
   `review_team.findings_verified` with the counts on success and
   `review_team.findings_verify_failed` on exception. Dual-run: a
   verifier failure must NEVER break the review. **Edit this file with
   anchored `edits` (Developer 0.2.0), not a full rewrite.**

Required imports (each grep-verified against develop — copy verbatim):
- verifiers: `import ast` · `from pathlib import Path` ·
  `from .findings import (ClaimType, Finding, FindingStatus,
  fetch_findings, update_finding_status)` ·
  `from .hallucination_guard import (_extract_missing_test_symbols,
  make_repo_file_reader, make_repo_symbol_searcher)` ·
  `from ..lint.no_inline_comments import scan_file as scan_inline` ·
  `from ..lint.no_silent_except import scan_file as scan_silent`.
- orchestrator: `from .finding_verifiers import verify_findings_for_pr`.

## Files in scope

- `src/ferova/review/finding_verifiers.py` (new)
- `tests/unit/test_finding_verifiers.py` (new)
- `src/ferova/review/orchestrator.py` (anchored-edit wiring only)

## Plan-shaping constraints

- Step 1 contracts ONLY the two NEW files.
- Step 2 contracts `orchestrator.py` (edited via anchored `edits`,
  never re-emitted whole) plus `tests/unit/test_finding_verifiers.py`
  for its promised wiring test.
- Two steps maximum. No hardcoded magic sizes (test-arithmetic law).

## Out of scope

- The refuter for design/security (slice 5) and the spec gate
  (slice 6).
- `broken_behavior` execution (needs a sandbox — later).
- Any change to the verdict/consensus/merge flow (dual-run).
- Re-classifying provisional claim types (the lens default stands).

## Smoke scenario

### Setup

A tmp repo + tmp db. Seed four `proposed` findings: a `missing_test`
naming a symbol that EXISTS under `tests/`, a `missing_docstring`
on a fully-documented file, a `lint_convention` on a clean file, and
a `design` finding.

### Execute

`verify_findings_for_pr`, then `fetch_findings`.

### Expected

The first three flip to `refuted` (repo disproves each), the `design`
one stays `proposed`; returned counts `{verified:0, refuted:3,
deferred:1}`.

## Definition of Done

- Each mechanical verifier's verified/refuted/proposed branches pinned
  — `test_missing_test_refuted_when_symbol_exists`,
  `test_missing_test_verified_when_absent`,
  `test_missing_docstring_refuted_when_documented`,
  `test_missing_docstring_verified_when_undocumented`,
  `test_lint_verified_on_violation`, `test_lint_refuted_when_clean`,
  `test_design_finding_deferred`.
- `verify_findings_for_pr` transitions only non-proposed outcomes and
  returns correct counts — `test_verify_findings_for_pr_round_trip`.
- Wiring: a stubbed orchestrator run verifies findings and emits
  `review_team.findings_verified`; a verifier that raises does NOT
  break `review_pr` — `test_orchestrator_verifies_findings`,
  `test_verify_failure_never_breaks_review`.
- `ruff` + `ruff format --check` + `pytest tests/unit` green; zero
  inline comments; no `# noqa`.

## Commit plan

1. `feat(review): mechanical finding verifiers (symbol / docstring / lint)`
2. `feat(review): orchestrator verifies findings after recording (dual-run)`

## Risks

- **Coarse docstring/lint verification** mislabels some findings: it
  is dual-run observation, not a gate; slices 5-7 refine and only
  verified findings ever gate.
- **Cross-module reuse of `_extract_missing_test_symbols`** (a private
  guard helper): intentional promotion of the guard's logic into the
  verifier; if a reviewer flags it, the Coder challenges with this
  rationale.
- **orchestrator.py anchored edit** must use unique snippets — the
  import-gate and the size guard both stand behind a bad attempt.
