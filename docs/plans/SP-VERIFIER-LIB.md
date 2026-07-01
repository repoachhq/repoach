# SP-VERIFIER-LIB — Mechanical finding verifiers: symbol / docstring / lint

Two-step implementation of the SP-VERIFIER-LIB slice. Step 1 creates src/ferova/review/finding_verifiers.py with verify_finding (dispatching on ClaimType: MISSING_TEST via make_repo_symbol_searcher, MISSING_DOCSTRING via ast + make_repo_file_reader, LINT_CONVENTION via scan_file from both lint modules, and all remaining types deferred) and verify_findings_for_pr (fetches proposed findings, calls verify_finding, transitions non-proposed outcomes via update_finding_status, returns counts). Full unit-test coverage of every branch and the round-trip behaviour is provided in the same step, along with the integration smoke test in tests/integration/test_review_redesign.py. Step 2 wires verify_findings_for_pr into orchestrator.py via an anchored edit immediately after the record_findings_for_outcomes try/except block, logging review_team.findings_verified on success and review_team.findings_verify_failed on exception, with the two promised wiring unit tests appended to tests/unit/test_finding_verifiers.py.

## Step 1 — Create finding_verifiers module with full unit and integration tests

- **Files**: `src/ferova/review/finding_verifiers.py`, `tests/unit/test_finding_verifiers.py`, `tests/integration/test_review_redesign.py`
- **Action**: Create src/ferova/review/finding_verifiers.py with two public functions.

verify_finding(finding: Finding, *, repo_root: Path) -> tuple[FindingStatus, str, str]:
  Dispatch on finding.claim_type:
  - MISSING_TEST: call _extract_missing_test_symbols(finding.claim); build searcher = make_repo_symbol_searcher(repo_root). Use a single if/elif/else with `and` (never nested ifs — ruff SIM102). If any symbol found via searcher → (REFUTED, 'symbol_search', '<sym> exists in tests/src'). If symbols extracted and none found → (VERIFIED, 'symbol_search', 'no test for <syms>'). If no symbol extracted → (PROPOSED, 'symbol_search', 'no checkable symbol').
  - MISSING_DOCSTRING: reader = make_repo_file_reader(repo_root); text = reader(finding.file). If text is None → (PROPOSED, 'ast_docstring', 'file unreadable'). Parse with ast.parse; collect public module/function/class nodes lacking ast.get_docstring. If module has docstring AND zero public defs/classes are undocumented → (REFUTED, 'ast_docstring', 'all public symbols documented'). If any missing → (VERIFIED, 'ast_docstring', '<n> public symbol(s) undocumented'). Wrap ast.parse in try/except SyntaxError → (PROPOSED, 'ast_docstring', 'file unreadable').
  - LINT_CONVENTION: target = repo_root / finding.file. If not target.exists() → (PROPOSED, 'lint_scan', 'file absent'). Otherwise call scan_inline(target) + scan_silent(target); if total violations > 0 → (VERIFIED, 'lint_scan', '<n> violation(s)'); else → (REFUTED, 'lint_scan', 'file clean').
  - All other types → (PROPOSED, 'deferred', 'not mechanically verifiable — slices 5-6').

verify_findings_for_pr(db_path: Path, *, pr_number: int, repo_root: Path, head_sha: str | None) -> dict[str, int]:
  Fetch proposed findings with fetch_findings(db_path, pr_number, status=FindingStatus.PROPOSED). For each, call verify_finding; when new_status != PROPOSED call update_finding_status(db_path, finding.id, new_status, verification_method=method, verification_result=result, checked_at_sha=head_sha or ''). Accumulate and return {'verified': n, 'refuted': n, 'deferred': n}.

Imports (verbatim):
  import ast
  from pathlib import Path
  from .findings import (ClaimType, Finding, FindingStatus, fetch_findings, update_finding_status)
  from .hallucination_guard import (_extract_missing_test_symbols, make_repo_file_reader, make_repo_symbol_searcher)
  from ..lint.no_inline_comments import scan_file as scan_inline
  from ..lint.no_silent_except import scan_file as scan_silent

Populate tests/unit/test_finding_verifiers.py with:
  test_missing_test_refuted_when_symbol_exists — stub searcher returning True; assert REFUTED
  test_missing_test_verified_when_absent — stub searcher returning False with extractable symbol; assert VERIFIED
  test_missing_docstring_refuted_when_documented — tmp .py file with module docstring and fully documented public symbols; assert REFUTED
  test_missing_docstring_verified_when_undocumented — tmp .py file with a public def lacking docstring; assert VERIFIED
  test_lint_verified_on_violation — tmp .py file with an inline comment; assert VERIFIED
  test_lint_refuted_when_clean — tmp .py file with no violations; assert REFUTED
  test_design_finding_deferred — DESIGN claim_type; assert PROPOSED and method=='deferred'
  test_verify_findings_for_pr_round_trip — build tmp db, seed 4 proposed findings (missing_test with findable symbol, missing_docstring on fully-documented file, lint_convention on clean file, design); patch verifiers so first three → REFUTED, design → PROPOSED; assert counts == {'verified':0,'refuted':3,'deferred':1} and db has 3 refuted + 1 proposed.

Add test_verify_findings_for_pr_round_trip as a real DB integration test in tests/integration/test_review_redesign.py using a tmp repo tree with actual files (no mocks on verifiers), seeding the same four scenarios.
- **Commit**: `feat(review): mechanical finding verifiers (symbol / docstring / lint)`
- **Done when**: pytest tests/unit/test_finding_verifiers.py::test_missing_test_refuted_when_symbol_exists tests/unit/test_finding_verifiers.py::test_missing_test_verified_when_absent tests/unit/test_finding_verifiers.py::test_missing_docstring_refuted_when_documented tests/unit/test_finding_verifiers.py::test_missing_docstring_verified_when_undocumented tests/unit/test_finding_verifiers.py::test_lint_verified_on_violation tests/unit/test_finding_verifiers.py::test_lint_refuted_when_clean tests/unit/test_finding_verifiers.py::test_design_finding_deferred tests/unit/test_finding_verifiers.py::test_verify_findings_for_pr_round_trip tests/integration/test_review_redesign.py::test_verify_findings_for_pr_round_trip passes and ruff check src/ferova/review/finding_verifiers.py exits 0 and ruff format --check src/ferova/review/finding_verifiers.py exits 0
- **Unit tests**: `tests/unit/test_finding_verifiers.py::test_missing_test_refuted_when_symbol_exists`, `tests/unit/test_finding_verifiers.py::test_missing_test_verified_when_absent`, `tests/unit/test_finding_verifiers.py::test_missing_docstring_refuted_when_documented`, `tests/unit/test_finding_verifiers.py::test_missing_docstring_verified_when_undocumented`, `tests/unit/test_finding_verifiers.py::test_lint_verified_on_violation`, `tests/unit/test_finding_verifiers.py::test_lint_refuted_when_clean`, `tests/unit/test_finding_verifiers.py::test_design_finding_deferred`, `tests/unit/test_finding_verifiers.py::test_verify_findings_for_pr_round_trip`

## Step 2 — Wire verify_findings_for_pr into orchestrator (anchored edit) and add wiring tests

- **Files**: `src/ferova/review/orchestrator.py`, `tests/unit/test_finding_verifiers.py`
- **Action**: Edit src/ferova/review/orchestrator.py using anchored edits only — never rewrite the whole file.

Anchor 1 — add import at the top of the existing imports block, immediately after 'from .findings_bridge import record_findings_for_outcomes':
  from .finding_verifiers import verify_findings_for_pr

Anchor 2 — inside review_pr, immediately after the closing 'except Exception:' block that guards record_findings_for_outcomes (i.e. after the line 'logger.exception("review_team.findings_record_failed", pr_number=pr_number)'), add a second try/except block:
  try:
      counts = verify_findings_for_pr(
          self._db_path,
          pr_number=pr_number,
          repo_root=self._repo_root,
          head_sha=head_sha,
      )
      logger.info("review_team.findings_verified", pr_number=pr_number, **counts)
  except Exception:
      logger.exception("review_team.findings_verify_failed", pr_number=pr_number)

Append two tests to tests/unit/test_finding_verifiers.py:

test_orchestrator_verifies_findings — build a ReviewOrchestrator with stub collaborators; patch record_findings_for_outcomes to return None; patch verify_findings_for_pr to return {'verified':1,'refuted':0,'deferred':0}; call review_pr; assert verify_findings_for_pr was called once with the correct pr_number kwarg and review still completes (all stage mocks called).

test_verify_failure_never_breaks_review — same setup but patch verify_findings_for_pr with side_effect=RuntimeError('boom'); call review_pr; assert it does not raise and all stage mocks were called.
- **Commit**: `feat(review): orchestrator verifies findings after recording (dual-run)`
- **Done when**: pytest tests/unit/test_finding_verifiers.py::test_orchestrator_verifies_findings tests/unit/test_finding_verifiers.py::test_verify_failure_never_breaks_review tests/unit/test_orchestrator.py passes and ruff check src/ferova/review/orchestrator.py exits 0 and ruff format --check src/ferova/review/orchestrator.py exits 0
- **Unit tests**: `tests/unit/test_finding_verifiers.py::test_orchestrator_verifies_findings`, `tests/unit/test_finding_verifiers.py::test_verify_failure_never_breaks_review`

## Integration tests

- `tests/integration/test_review_redesign.py::test_verify_findings_for_pr_round_trip`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-VERIFIER-LIB",
  "title": "Mechanical finding verifiers: symbol / docstring / lint",
  "summary": "Two-step implementation of the SP-VERIFIER-LIB slice. Step 1 creates src/ferova/review/finding_verifiers.py with verify_finding (dispatching on ClaimType: MISSING_TEST via make_repo_symbol_searcher, MISSING_DOCSTRING via ast + make_repo_file_reader, LINT_CONVENTION via scan_file from both lint modules, and all remaining types deferred) and verify_findings_for_pr (fetches proposed findings, calls verify_finding, transitions non-proposed outcomes via update_finding_status, returns counts). Full unit-test coverage of every branch and the round-trip behaviour is provided in the same step, along with the integration smoke test in tests/integration/test_review_redesign.py. Step 2 wires verify_findings_for_pr into orchestrator.py via an anchored edit immediately after the record_findings_for_outcomes try/except block, logging review_team.findings_verified on success and review_team.findings_verify_failed on exception, with the two promised wiring unit tests appended to tests/unit/test_finding_verifiers.py.",
  "steps": [
    {
      "index": 1,
      "title": "Create finding_verifiers module with full unit and integration tests",
      "files": [
        "src/ferova/review/finding_verifiers.py",
        "tests/unit/test_finding_verifiers.py",
        "tests/integration/test_review_redesign.py"
      ],
      "action": "Create src/ferova/review/finding_verifiers.py with two public functions.\n\nverify_finding(finding: Finding, *, repo_root: Path) -> tuple[FindingStatus, str, str]:\n  Dispatch on finding.claim_type:\n  - MISSING_TEST: call _extract_missing_test_symbols(finding.claim); build searcher = make_repo_symbol_searcher(repo_root). Use a single if/elif/else with `and` (never nested ifs — ruff SIM102). If any symbol found via searcher → (REFUTED, 'symbol_search', '<sym> exists in tests/src'). If symbols extracted and none found → (VERIFIED, 'symbol_search', 'no test for <syms>'). If no symbol extracted → (PROPOSED, 'symbol_search', 'no checkable symbol').\n  - MISSING_DOCSTRING: reader = make_repo_file_reader(repo_root); text = reader(finding.file). If text is None → (PROPOSED, 'ast_docstring', 'file unreadable'). Parse with ast.parse; collect public module/function/class nodes lacking ast.get_docstring. If module has docstring AND zero public defs/classes are undocumented → (REFUTED, 'ast_docstring', 'all public symbols documented'). If any missing → (VERIFIED, 'ast_docstring', '<n> public symbol(s) undocumented'). Wrap ast.parse in try/except SyntaxError → (PROPOSED, 'ast_docstring', 'file unreadable').\n  - LINT_CONVENTION: target = repo_root / finding.file. If not target.exists() → (PROPOSED, 'lint_scan', 'file absent'). Otherwise call scan_inline(target) + scan_silent(target); if total violations > 0 → (VERIFIED, 'lint_scan', '<n> violation(s)'); else → (REFUTED, 'lint_scan', 'file clean').\n  - All other types → (PROPOSED, 'deferred', 'not mechanically verifiable — slices 5-6').\n\nverify_findings_for_pr(db_path: Path, *, pr_number: int, repo_root: Path, head_sha: str | None) -> dict[str, int]:\n  Fetch proposed findings with fetch_findings(db_path, pr_number, status=FindingStatus.PROPOSED). For each, call verify_finding; when new_status != PROPOSED call update_finding_status(db_path, finding.id, new_status, verification_method=method, verification_result=result, checked_at_sha=head_sha or ''). Accumulate and return {'verified': n, 'refuted': n, 'deferred': n}.\n\nImports (verbatim):\n  import ast\n  from pathlib import Path\n  from .findings import (ClaimType, Finding, FindingStatus, fetch_findings, update_finding_status)\n  from .hallucination_guard import (_extract_missing_test_symbols, make_repo_file_reader, make_repo_symbol_searcher)\n  from ..lint.no_inline_comments import scan_file as scan_inline\n  from ..lint.no_silent_except import scan_file as scan_silent\n\nPopulate tests/unit/test_finding_verifiers.py with:\n  test_missing_test_refuted_when_symbol_exists — stub searcher returning True; assert REFUTED\n  test_missing_test_verified_when_absent — stub searcher returning False with extractable symbol; assert VERIFIED\n  test_missing_docstring_refuted_when_documented — tmp .py file with module docstring and fully documented public symbols; assert REFUTED\n  test_missing_docstring_verified_when_undocumented — tmp .py file with a public def lacking docstring; assert VERIFIED\n  test_lint_verified_on_violation — tmp .py file with an inline comment; assert VERIFIED\n  test_lint_refuted_when_clean — tmp .py file with no violations; assert REFUTED\n  test_design_finding_deferred — DESIGN claim_type; assert PROPOSED and method=='deferred'\n  test_verify_findings_for_pr_round_trip — build tmp db, seed 4 proposed findings (missing_test with findable symbol, missing_docstring on fully-documented file, lint_convention on clean file, design); patch verifiers so first three → REFUTED, design → PROPOSED; assert counts == {'verified':0,'refuted':3,'deferred':1} and db has 3 refuted + 1 proposed.\n\nAdd test_verify_findings_for_pr_round_trip as a real DB integration test in tests/integration/test_review_redesign.py using a tmp repo tree with actual files (no mocks on verifiers), seeding the same four scenarios.",
      "commit_message": "feat(review): mechanical finding verifiers (symbol / docstring / lint)",
      "done_when": "pytest tests/unit/test_finding_verifiers.py::test_missing_test_refuted_when_symbol_exists tests/unit/test_finding_verifiers.py::test_missing_test_verified_when_absent tests/unit/test_finding_verifiers.py::test_missing_docstring_refuted_when_documented tests/unit/test_finding_verifiers.py::test_missing_docstring_verified_when_undocumented tests/unit/test_finding_verifiers.py::test_lint_verified_on_violation tests/unit/test_finding_verifiers.py::test_lint_refuted_when_clean tests/unit/test_finding_verifiers.py::test_design_finding_deferred tests/unit/test_finding_verifiers.py::test_verify_findings_for_pr_round_trip tests/integration/test_review_redesign.py::test_verify_findings_for_pr_round_trip passes and ruff check src/ferova/review/finding_verifiers.py exits 0 and ruff format --check src/ferova/review/finding_verifiers.py exits 0",
      "unit_tests": [
        "tests/unit/test_finding_verifiers.py::test_missing_test_refuted_when_symbol_exists",
        "tests/unit/test_finding_verifiers.py::test_missing_test_verified_when_absent",
        "tests/unit/test_finding_verifiers.py::test_missing_docstring_refuted_when_documented",
        "tests/unit/test_finding_verifiers.py::test_missing_docstring_verified_when_undocumented",
        "tests/unit/test_finding_verifiers.py::test_lint_verified_on_violation",
        "tests/unit/test_finding_verifiers.py::test_lint_refuted_when_clean",
        "tests/unit/test_finding_verifiers.py::test_design_finding_deferred",
        "tests/unit/test_finding_verifiers.py::test_verify_findings_for_pr_round_trip"
      ]
    },
    {
      "index": 2,
      "title": "Wire verify_findings_for_pr into orchestrator (anchored edit) and add wiring tests",
      "files": [
        "src/ferova/review/orchestrator.py",
        "tests/unit/test_finding_verifiers.py"
      ],
      "action": "Edit src/ferova/review/orchestrator.py using anchored edits only — never rewrite the whole file.\n\nAnchor 1 — add import at the top of the existing imports block, immediately after 'from .findings_bridge import record_findings_for_outcomes':\n  from .finding_verifiers import verify_findings_for_pr\n\nAnchor 2 — inside review_pr, immediately after the closing 'except Exception:' block that guards record_findings_for_outcomes (i.e. after the line 'logger.exception(\"review_team.findings_record_failed\", pr_number=pr_number)'), add a second try/except block:\n  try:\n      counts = verify_findings_for_pr(\n          self._db_path,\n          pr_number=pr_number,\n          repo_root=self._repo_root,\n          head_sha=head_sha,\n      )\n      logger.info(\"review_team.findings_verified\", pr_number=pr_number, **counts)\n  except Exception:\n      logger.exception(\"review_team.findings_verify_failed\", pr_number=pr_number)\n\nAppend two tests to tests/unit/test_finding_verifiers.py:\n\ntest_orchestrator_verifies_findings — build a ReviewOrchestrator with stub collaborators; patch record_findings_for_outcomes to return None; patch verify_findings_for_pr to return {'verified':1,'refuted':0,'deferred':0}; call review_pr; assert verify_findings_for_pr was called once with the correct pr_number kwarg and review still completes (all stage mocks called).\n\ntest_verify_failure_never_breaks_review — same setup but patch verify_findings_for_pr with side_effect=RuntimeError('boom'); call review_pr; assert it does not raise and all stage mocks were called.",
      "commit_message": "feat(review): orchestrator verifies findings after recording (dual-run)",
      "done_when": "pytest tests/unit/test_finding_verifiers.py::test_orchestrator_verifies_findings tests/unit/test_finding_verifiers.py::test_verify_failure_never_breaks_review tests/unit/test_orchestrator.py passes and ruff check src/ferova/review/orchestrator.py exits 0 and ruff format --check src/ferova/review/orchestrator.py exits 0",
      "unit_tests": [
        "tests/unit/test_finding_verifiers.py::test_orchestrator_verifies_findings",
        "tests/unit/test_finding_verifiers.py::test_verify_failure_never_breaks_review"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_review_redesign.py::test_verify_findings_for_pr_round_trip"
  ]
}
```
