# SP-FINDINGS-BRIDGE-DOCFIX — Drop stale coder_loop cross-reference from _files_in_diff docstring

Remove the stale ``coder_loop._files_in_diff`` cross-reference and 'temporary duplicate' framing from the `_files_in_diff` docstring in `src/ferova/review/findings_bridge.py`, keep the failure-soft rationale, and pin both invariants with a new unit test so the stale prose cannot silently return.

## Step 1 — Rewrite _files_in_diff docstring and pin invariants with a unit test

- **Files**: `src/ferova/review/findings_bridge.py`, `tests/unit/test_findings_bridge_docfix.py`
- **Action**: In `src/ferova/review/findings_bridge.py`, edit ONLY the docstring of `_files_in_diff` (lines 46-62): delete the sentence 'This mirrors ``coder_loop._files_in_diff`` deliberately; the duplicate is temporary until the legacy arbiter is retired with that module.' so the docstring keeps the description of what the helper walks (``diff --git``, ``+++ b/``, ``--- a/``, whitespace-tolerant, discarding ``/dev/null``) and the failure-soft rationale (malformed input yields whatever it could parse, an empty set is acceptable, the caller then keeps every comment, matching the historical no-filter behaviour). Do NOT touch any executable statement, import, or other docstring in the module. Then create `tests/unit/test_findings_bridge_docfix.py` that imports `ferova.review.findings_bridge` and reads `findings_bridge._files_in_diff.__doc__`; assert (a) the docstring is a string and does NOT contain the substring 'coder_loop' (AC1), and (b) the docstring still documents the failure-soft contract by containing the phrases 'malformed', 'empty', and 'keeps every comment' (or equivalent wording that pins the AC2 rationale). Use plain `assert` statements; no fixtures needed.
- **Commit**: `docs(review): drop stale coder_loop cross-reference from _files_in_diff`
- **Done when**: `grep -n 'coder_loop' src/ferova/review/findings_bridge.py` exits 0 with no matches; `pytest tests/unit/test_findings_bridge_docfix.py -q` passes; `pytest tests/unit/test_findings_bridge.py tests/integration/test_findings_bridge.py -q` passes (existing behaviour tests stay green untouched).
- **Unit tests**: `tests/unit/test_findings_bridge_docfix.py::test_no_coder_loop_reference`, `tests/unit/test_findings_bridge_docfix.py::test_failure_soft_contract_documented`

## Integration tests

- `tests/integration/test_findings_bridge.py`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-FINDINGS-BRIDGE-DOCFIX",
  "title": "Drop stale coder_loop cross-reference from _files_in_diff docstring",
  "summary": "Remove the stale ``coder_loop._files_in_diff`` cross-reference and 'temporary duplicate' framing from the `_files_in_diff` docstring in `src/ferova/review/findings_bridge.py`, keep the failure-soft rationale, and pin both invariants with a new unit test so the stale prose cannot silently return.",
  "steps": [
    {
      "index": 1,
      "title": "Rewrite _files_in_diff docstring and pin invariants with a unit test",
      "files": [
        "src/ferova/review/findings_bridge.py",
        "tests/unit/test_findings_bridge_docfix.py"
      ],
      "action": "In `src/ferova/review/findings_bridge.py`, edit ONLY the docstring of `_files_in_diff` (lines 46-62): delete the sentence 'This mirrors ``coder_loop._files_in_diff`` deliberately; the duplicate is temporary until the legacy arbiter is retired with that module.' so the docstring keeps the description of what the helper walks (``diff --git``, ``+++ b/``, ``--- a/``, whitespace-tolerant, discarding ``/dev/null``) and the failure-soft rationale (malformed input yields whatever it could parse, an empty set is acceptable, the caller then keeps every comment, matching the historical no-filter behaviour). Do NOT touch any executable statement, import, or other docstring in the module. Then create `tests/unit/test_findings_bridge_docfix.py` that imports `ferova.review.findings_bridge` and reads `findings_bridge._files_in_diff.__doc__`; assert (a) the docstring is a string and does NOT contain the substring 'coder_loop' (AC1), and (b) the docstring still documents the failure-soft contract by containing the phrases 'malformed', 'empty', and 'keeps every comment' (or equivalent wording that pins the AC2 rationale). Use plain `assert` statements; no fixtures needed.",
      "commit_message": "docs(review): drop stale coder_loop cross-reference from _files_in_diff",
      "done_when": "`grep -n 'coder_loop' src/ferova/review/findings_bridge.py` exits 0 with no matches; `pytest tests/unit/test_findings_bridge_docfix.py -q` passes; `pytest tests/unit/test_findings_bridge.py tests/integration/test_findings_bridge.py -q` passes (existing behaviour tests stay green untouched).",
      "unit_tests": [
        "tests/unit/test_findings_bridge_docfix.py::test_no_coder_loop_reference",
        "tests/unit/test_findings_bridge_docfix.py::test_failure_soft_contract_documented"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_findings_bridge.py"
  ]
}
```
