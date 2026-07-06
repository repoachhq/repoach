# SP-DEV-BRIEF-FILE-CONTENT — Embed contract file contents in the Developer step brief

Modify build_step_brief in src/ferova/review/dev_runner.py to embed the current content of every existing contract file under clear headings, with a per-file cap (12k chars) and total budget (48k chars), truncating oversized files head-first with a continuation note naming the exact read_file start_line. Missing contract paths are listed under a 'to create' heading. The retry variant re-reads from disk so the loop's own writes are visible.

## Step 1 — Add _embed_contract_files helper with caps, truncation, and to-create heading

- **Files**: `src/ferova/review/dev_runner.py`, `tests/unit/test_dev_brief_file_content.py`
- **Action**: Add a module-level helper `_embed_contract_files(contract_paths, repo_root=None)` in src/ferova/review/dev_runner.py that returns a formatted string with two sections: (1) 'Existing contract files' — for each path that exists, read its content (UTF-8), cap at 12_000 chars per file, truncate head-first with a note naming the exact `read_file(path, start_line=N)` continuation where N is the next line after the truncation point; (2) 'Files to create' — for each path that does not exist, list the path under a clear heading. Apply a total budget of 48_000 chars across all embedded content; once exhausted, remaining files are listed by name with 'read on demand' notes. Read errors are listed with the error string (mirroring read_existing_files). Use the same jail-safe path resolution as read_existing_files (resolve, relative_to check). Create tests/unit/test_dev_brief_file_content.py with helper-level tests covering: existing file embedding, missing path listing under to-create, oversized file truncation with continuation note, read error handling, and total budget enforcement.
- **Commit**: `feat(dev-runner): add _embed_contract_files helper for brief assembly`
- **Done when**: pytest tests/unit/test_dev_brief_file_content.py passes for the helper-level tests
- **Unit tests**: `tests/unit/test_dev_brief_file_content.py::test_helper_embeds_existing_file_content`, `tests/unit/test_dev_brief_file_content.py::test_helper_lists_missing_paths_under_to_create`, `tests/unit/test_dev_brief_file_content.py::test_helper_truncates_oversized_file_with_continuation_note`, `tests/unit/test_dev_brief_file_content.py::test_helper_handles_read_error_gracefully`, `tests/unit/test_dev_brief_file_content.py::test_helper_respects_total_budget`

## Step 2 — Wire helper into build_step_brief and add AC1-AC4 tests

- **Files**: `src/ferova/review/dev_runner.py`, `tests/unit/test_dev_brief_file_content.py`, `tests/integration/test_review_dev_runner.py`
- **Action**: Modify build_step_brief in src/ferova/review/dev_runner.py to call _embed_contract_files with the step's contract paths (step.files) and embed the result in the brief under a 'Contract files' heading. The retry variant (when gate feedback is present) must re-read from disk so the loop's previous writes are visible — call _embed_contract_files fresh on each invocation rather than caching. Extend tests/unit/test_dev_brief_file_content.py with AC1-AC4 tests: test_brief_embeds_existing_contract_files (AC1), test_brief_lists_missing_contract_files_to_create (AC2), test_oversized_file_truncated_with_continuation_note (AC3), test_retry_brief_reflects_disk_state (AC4). Create tests/integration/test_review_dev_runner.py with an end-to-end integration test that exercises build_step_brief against a real on-disk step contract (one existing source file, one new test file) and asserts the brief carries the source content and a to-create entry for the test. Also run the full unit suite (AC5) as part of this step's verification.
- **Commit**: `feat(dev-runner): embed contract file contents in step brief`
- **Done when**: pytest tests/unit/ exits 0, all AC1-AC4 tests in tests/unit/test_dev_brief_file_content.py pass, and pytest tests/integration/test_review_dev_runner.py passes
- **Unit tests**: `tests/unit/test_dev_brief_file_content.py::test_brief_embeds_existing_contract_files`, `tests/unit/test_dev_brief_file_content.py::test_brief_lists_missing_contract_files_to_create`, `tests/unit/test_dev_brief_file_content.py::test_oversized_file_truncated_with_continuation_note`, `tests/unit/test_dev_brief_file_content.py::test_retry_brief_reflects_disk_state`

## Integration tests

- `tests/integration/test_review_dev_runner.py`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-DEV-BRIEF-FILE-CONTENT",
  "title": "Embed contract file contents in the Developer step brief",
  "summary": "Modify build_step_brief in src/ferova/review/dev_runner.py to embed the current content of every existing contract file under clear headings, with a per-file cap (12k chars) and total budget (48k chars), truncating oversized files head-first with a continuation note naming the exact read_file start_line. Missing contract paths are listed under a 'to create' heading. The retry variant re-reads from disk so the loop's own writes are visible.",
  "steps": [
    {
      "index": 1,
      "title": "Add _embed_contract_files helper with caps, truncation, and to-create heading",
      "files": [
        "src/ferova/review/dev_runner.py",
        "tests/unit/test_dev_brief_file_content.py"
      ],
      "action": "Add a module-level helper `_embed_contract_files(contract_paths, repo_root=None)` in src/ferova/review/dev_runner.py that returns a formatted string with two sections: (1) 'Existing contract files' — for each path that exists, read its content (UTF-8), cap at 12_000 chars per file, truncate head-first with a note naming the exact `read_file(path, start_line=N)` continuation where N is the next line after the truncation point; (2) 'Files to create' — for each path that does not exist, list the path under a clear heading. Apply a total budget of 48_000 chars across all embedded content; once exhausted, remaining files are listed by name with 'read on demand' notes. Read errors are listed with the error string (mirroring read_existing_files). Use the same jail-safe path resolution as read_existing_files (resolve, relative_to check). Create tests/unit/test_dev_brief_file_content.py with helper-level tests covering: existing file embedding, missing path listing under to-create, oversized file truncation with continuation note, read error handling, and total budget enforcement.",
      "commit_message": "feat(dev-runner): add _embed_contract_files helper for brief assembly",
      "done_when": "pytest tests/unit/test_dev_brief_file_content.py passes for the helper-level tests",
      "unit_tests": [
        "tests/unit/test_dev_brief_file_content.py::test_helper_embeds_existing_file_content",
        "tests/unit/test_dev_brief_file_content.py::test_helper_lists_missing_paths_under_to_create",
        "tests/unit/test_dev_brief_file_content.py::test_helper_truncates_oversized_file_with_continuation_note",
        "tests/unit/test_dev_brief_file_content.py::test_helper_handles_read_error_gracefully",
        "tests/unit/test_dev_brief_file_content.py::test_helper_respects_total_budget"
      ]
    },
    {
      "index": 2,
      "title": "Wire helper into build_step_brief and add AC1-AC4 tests",
      "files": [
        "src/ferova/review/dev_runner.py",
        "tests/unit/test_dev_brief_file_content.py",
        "tests/integration/test_review_dev_runner.py"
      ],
      "action": "Modify build_step_brief in src/ferova/review/dev_runner.py to call _embed_contract_files with the step's contract paths (step.files) and embed the result in the brief under a 'Contract files' heading. The retry variant (when gate feedback is present) must re-read from disk so the loop's previous writes are visible — call _embed_contract_files fresh on each invocation rather than caching. Extend tests/unit/test_dev_brief_file_content.py with AC1-AC4 tests: test_brief_embeds_existing_contract_files (AC1), test_brief_lists_missing_contract_files_to_create (AC2), test_oversized_file_truncated_with_continuation_note (AC3), test_retry_brief_reflects_disk_state (AC4). Create tests/integration/test_review_dev_runner.py with an end-to-end integration test that exercises build_step_brief against a real on-disk step contract (one existing source file, one new test file) and asserts the brief carries the source content and a to-create entry for the test. Also run the full unit suite (AC5) as part of this step's verification.",
      "commit_message": "feat(dev-runner): embed contract file contents in step brief",
      "done_when": "pytest tests/unit/ exits 0, all AC1-AC4 tests in tests/unit/test_dev_brief_file_content.py pass, and pytest tests/integration/test_review_dev_runner.py passes",
      "unit_tests": [
        "tests/unit/test_dev_brief_file_content.py::test_brief_embeds_existing_contract_files",
        "tests/unit/test_dev_brief_file_content.py::test_brief_lists_missing_contract_files_to_create",
        "tests/unit/test_dev_brief_file_content.py::test_oversized_file_truncated_with_continuation_note",
        "tests/unit/test_dev_brief_file_content.py::test_retry_brief_reflects_disk_state"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_review_dev_runner.py"
  ]
}
```
