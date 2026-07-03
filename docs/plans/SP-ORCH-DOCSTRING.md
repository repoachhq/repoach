# SP-ORCH-DOCSTRING — Truthful orchestrator module docstring

Rewrite the module docstring of src/ferova/review/orchestrator.py so it describes the evidence-first pipeline that actually runs today (diff fetch → four reviewers in parallel → hallucination guard → optional round-2 dialogue → findings ledger + review-integrity row → mechanical verification + adversarial refuter → ledger-derived team verdict → inline comments, per-bot reviews, and a report-only sticky archive comment), and pin the truthfulness with a unit test so the retired verdict-first narrative cannot silently return. No executable statement, import, or other docstring in the module is touched.

## Step 1 — Rewrite the orchestrator module docstring to describe the evidence-first pipeline

- **Files**: `src/ferova/review/orchestrator.py`, `tests/unit/test_orchestrator_docstring.py`
- **Action**: Replace the existing module docstring (currently lines 1-19) of src/ferova/review/orchestrator.py with a new docstring that narrates the pipeline ReviewTeamOrchestrator.review_pr actually drives today, in order: (1) fetch the PR diff via gh pr diff; (2) run the four reviewer bots concurrently; (3) apply the hallucination guard; (4) optionally run a round-2 confirm-or-retract dialogue; (5) record findings and the review-integrity row; (6) run mechanical verification (finding_verifiers) and the adversarial refuter for judged claims; (7) derive the team verdict from the findings ledger via merge_gate.verdict_from_facts; (8) post inline comments, per-bot reviews, and a sticky archive comment that is report-only (not the retrievable source of truth); (9) persist outcomes. The new docstring must explicitly state that auto-merge and the findings-driven Coder fix loop are separate workflow-driven follow-ups hosted in auto_merge.py and coder_findings.py, and that the orchestrator reviews but never merges. Do NOT modify any executable statement, import, blank line, or other docstring in the module — this is a module-docstring-only slice. The new docstring must contain the words 'findings' and 'derived' (so AC2's ledger-pipeline check passes) and the phrase 'report-only' (so AC2's archive-comment check passes). It must NOT contain the substrings 'Aggregates their verdicts', 'does **not** auto-merge', or 'run_coder_response' (so AC1's grep returns no matches). Also create tests/unit/test_orchestrator_docstring.py with two tests that pin AC1 and AC2: test_orchestrator_docstring_drops_the_retired_narrative reads src/ferova/review/orchestrator.py as text and asserts that none of the substrings 'Aggregates their verdicts', 'does **not** auto-merge', and 'run_coder_response' appear; test_orchestrator_docstring_describes_the_ledger_pipeline imports ferova.review.orchestrator and asserts that its __doc__ contains 'findings', 'derived', and 'report-only'. The test file must not touch any executable line of orchestrator.py — it only inspects the module docstring and the source file's text.
- **Commit**: `docs(review): rewrite orchestrator module docstring to match the evidence-first pipeline`
- **Done when**: grep -nE "Aggregates their verdicts|does \*\*not\*\* auto-merge|run_coder_response" src/ferova/review/orchestrator.py returns no matches, python -c "import ferova.review.orchestrator as m; d=m.__doc__; assert 'findings' in d and 'derived' in d and 'report-only' in d" exits 0, and pytest tests/unit/test_orchestrator_docstring.py -q exits 0 with both tests passing.
- **Unit tests**: `tests/unit/test_orchestrator_docstring.py::test_orchestrator_docstring_drops_the_retired_narrative`, `tests/unit/test_orchestrator_docstring.py::test_orchestrator_docstring_describes_the_ledger_pipeline`

## Step 2 — Pin the docstring's truthfulness with a unit test

- **Files**: `tests/unit/test_orchestrator_docstring.py`
- **Action**: Create tests/unit/test_orchestrator_docstring.py with two tests that pin AC1 and AC2 from the spec. test_orchestrator_docstring_drops_the_retired_narrative reads src/ferova/review/orchestrator.py as text and asserts that none of the substrings 'Aggregates their verdicts', 'does **not** auto-merge', and 'run_coder_response' appear (AC1). test_orchestrator_docstring_describes_the_ledger_pipeline imports ferova.review.orchestrator and asserts that its __doc__ contains 'findings', 'derived', and 'report-only' (AC2). The test file must not touch any executable line of orchestrator.py — it only inspects the module docstring and the source file's text. Run pytest tests/unit/test_orchestrator_docstring.py and confirm both tests pass.
- **Commit**: `test(review): pin orchestrator module docstring truthfulness`
- **Done when**: pytest tests/unit/test_orchestrator_docstring.py -q exits 0 with both tests passing.
- **Unit tests**: `tests/unit/test_orchestrator_docstring.py::test_orchestrator_docstring_drops_the_retired_narrative`, `tests/unit/test_orchestrator_docstring.py::test_orchestrator_docstring_describes_the_ledger_pipeline`

## Integration tests

- `tests/integration/test_orchestrator_docstring_integration.py::test_orchestrator_docstring_truthful_via_imported_module`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-ORCH-DOCSTRING",
  "title": "Truthful orchestrator module docstring",
  "summary": "Rewrite the module docstring of src/ferova/review/orchestrator.py so it describes the evidence-first pipeline that actually runs today (diff fetch → four reviewers in parallel → hallucination guard → optional round-2 dialogue → findings ledger + review-integrity row → mechanical verification + adversarial refuter → ledger-derived team verdict → inline comments, per-bot reviews, and a report-only sticky archive comment), and pin the truthfulness with a unit test so the retired verdict-first narrative cannot silently return. No executable statement, import, or other docstring in the module is touched.",
  "steps": [
    {
      "index": 1,
      "title": "Rewrite the orchestrator module docstring to describe the evidence-first pipeline",
      "files": [
        "src/ferova/review/orchestrator.py",
        "tests/unit/test_orchestrator_docstring.py"
      ],
      "action": "Replace the existing module docstring (currently lines 1-19) of src/ferova/review/orchestrator.py with a new docstring that narrates the pipeline ReviewTeamOrchestrator.review_pr actually drives today, in order: (1) fetch the PR diff via gh pr diff; (2) run the four reviewer bots concurrently; (3) apply the hallucination guard; (4) optionally run a round-2 confirm-or-retract dialogue; (5) record findings and the review-integrity row; (6) run mechanical verification (finding_verifiers) and the adversarial refuter for judged claims; (7) derive the team verdict from the findings ledger via merge_gate.verdict_from_facts; (8) post inline comments, per-bot reviews, and a sticky archive comment that is report-only (not the retrievable source of truth); (9) persist outcomes. The new docstring must explicitly state that auto-merge and the findings-driven Coder fix loop are separate workflow-driven follow-ups hosted in auto_merge.py and coder_findings.py, and that the orchestrator reviews but never merges. Do NOT modify any executable statement, import, blank line, or other docstring in the module — this is a module-docstring-only slice. The new docstring must contain the words 'findings' and 'derived' (so AC2's ledger-pipeline check passes) and the phrase 'report-only' (so AC2's archive-comment check passes). It must NOT contain the substrings 'Aggregates their verdicts', 'does **not** auto-merge', or 'run_coder_response' (so AC1's grep returns no matches). Also create tests/unit/test_orchestrator_docstring.py with two tests that pin AC1 and AC2: test_orchestrator_docstring_drops_the_retired_narrative reads src/ferova/review/orchestrator.py as text and asserts that none of the substrings 'Aggregates their verdicts', 'does **not** auto-merge', and 'run_coder_response' appear; test_orchestrator_docstring_describes_the_ledger_pipeline imports ferova.review.orchestrator and asserts that its __doc__ contains 'findings', 'derived', and 'report-only'. The test file must not touch any executable line of orchestrator.py — it only inspects the module docstring and the source file's text.",
      "commit_message": "docs(review): rewrite orchestrator module docstring to match the evidence-first pipeline",
      "done_when": "grep -nE \"Aggregates their verdicts|does \\*\\*not\\*\\* auto-merge|run_coder_response\" src/ferova/review/orchestrator.py returns no matches, python -c \"import ferova.review.orchestrator as m; d=m.__doc__; assert 'findings' in d and 'derived' in d and 'report-only' in d\" exits 0, and pytest tests/unit/test_orchestrator_docstring.py -q exits 0 with both tests passing.",
      "unit_tests": [
        "tests/unit/test_orchestrator_docstring.py::test_orchestrator_docstring_drops_the_retired_narrative",
        "tests/unit/test_orchestrator_docstring.py::test_orchestrator_docstring_describes_the_ledger_pipeline"
      ]
    },
    {
      "index": 2,
      "title": "Pin the docstring's truthfulness with a unit test",
      "files": [
        "tests/unit/test_orchestrator_docstring.py"
      ],
      "action": "Create tests/unit/test_orchestrator_docstring.py with two tests that pin AC1 and AC2 from the spec. test_orchestrator_docstring_drops_the_retired_narrative reads src/ferova/review/orchestrator.py as text and asserts that none of the substrings 'Aggregates their verdicts', 'does **not** auto-merge', and 'run_coder_response' appear (AC1). test_orchestrator_docstring_describes_the_ledger_pipeline imports ferova.review.orchestrator and asserts that its __doc__ contains 'findings', 'derived', and 'report-only' (AC2). The test file must not touch any executable line of orchestrator.py — it only inspects the module docstring and the source file's text. Run pytest tests/unit/test_orchestrator_docstring.py and confirm both tests pass.",
      "commit_message": "test(review): pin orchestrator module docstring truthfulness",
      "done_when": "pytest tests/unit/test_orchestrator_docstring.py -q exits 0 with both tests passing.",
      "unit_tests": [
        "tests/unit/test_orchestrator_docstring.py::test_orchestrator_docstring_drops_the_retired_narrative",
        "tests/unit/test_orchestrator_docstring.py::test_orchestrator_docstring_describes_the_ledger_pipeline"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_orchestrator_docstring_integration.py::test_orchestrator_docstring_truthful_via_imported_module"
  ]
}
```
