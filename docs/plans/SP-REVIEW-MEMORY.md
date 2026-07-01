# SP-REVIEW-MEMORY — Implement review-scoped agentmemory recall and curated seeds

Add review-scoped agentmemory recall to the review orchestrator, mirroring the builder memory pattern. This includes a new `review_memory.py` module with curated seed lessons, a config kill-switch, CLI commands, and integration into the orchestrator's prompt rendering. The feature degrades gracefully on service failure or kill-switch off.

## Step 1 — Add review_memory.py module with curated seed lessons

- **Files**: `src/ferova/review/review_memory.py`, `tests/unit/test_review_memory.py`
- **Action**: Create `src/ferova/review/review_memory.py` with constants `REVIEW_PROJECT = "review"` and `SEED_REVIEW_LESSONS`, functions `recall_review_lessons(query: str) -> list[str]`, `review_lessons_section(lessons: list[str]) -> str`, and `seed_review_memory() -> int`. Mirror the builder_memory.py pattern, including graceful degradation on kill-switch or service failure. Write unit tests in `tests/unit/test_review_memory.py` covering recall, seeding, and kill-switch behavior.
- **Commit**: `feat(review): review-scoped agentmemory module with curated seed traps`
- **Done when**: python -c "from ferova.review.review_memory import recall_review_lessons, review_lessons_section, seed_review_memory; assert seed_review_memory() == 6" succeeds and pytest tests/unit/test_review_memory.py passes
- **Unit tests**: `tests/unit/test_review_memory.py::test_recall_disabled_is_noop`, `tests/unit/test_review_memory.py::test_recall_enabled_calls_client_with_review_project`, `tests/unit/test_review_memory.py::test_lessons_section_empty_is_blank`, `tests/unit/test_review_memory.py::test_lessons_section_renders_block`, `tests/unit/test_review_memory.py::test_seed_writes_all_lessons`

## Step 2 — Add review_memory_enabled config field

- **Files**: `src/ferova/core/config.py`, `tests/unit/test_review_memory.py`
- **Action**: Add `review_memory_enabled: bool` field to the `Settings` class in `src/ferova/core/config.py`, defaulting to `True`, with validation aliases `FEROVA_REVIEW_MEMORY_ENABLED` and `REVIEW_MEMORY_ENABLED` (using `AliasChoices`). Add a description marking it as the kill-switch for the review recall loop. Add a unit test in `tests/unit/test_review_memory.py` to verify the config field is accessible and defaults to `True`.
- **Commit**: `feat(core): add review_memory_enabled config kill-switch`
- **Done when**: python -c "from ferova.core.config import get_settings; assert get_settings().review_memory_enabled is True" succeeds and pytest tests/unit/test_review_memory.py::test_config_kill_switch_defaults_true passes
- **Unit tests**: `tests/unit/test_review_memory.py::test_config_kill_switch_defaults_true`

## Step 3 — Integrate review memory recall into orchestrator

- **Files**: `src/ferova/review/orchestrator.py`, `tests/unit/test_review_memory.py`
- **Action**: Modify `ReviewTeamOrchestrator.review_pr` in `src/ferova/review/orchestrator.py` to perform one review-scoped recall per run (query built from PR title + changed file paths). Append the rendered lessons section to every reviewer's prompt. Emit a `review_team.lessons_recalled` structlog info with `n_lessons`. Add a unit test in `tests/unit/test_review_memory.py` to verify prompt injection and logging.
- **Commit**: `feat(review): orchestrator recalls review lessons once per run, appends to prompts`
- **Done when**: python -c "from ferova.review.orchestrator import ReviewTeamOrchestrator; import tempfile; orch = ReviewTeamOrchestrator(post_to_github=False, db_path=tempfile.mktemp()); assert hasattr(orch, '_review_memory_recall')" succeeds and pytest tests/unit/test_review_memory.py::test_orchestrator_appends_lessons_to_prompt passes
- **Unit tests**: `tests/unit/test_review_memory.py::test_orchestrator_appends_lessons_to_prompt`

## Step 4 — Add memory CLI commands for review scope

- **Files**: `src/ferova/cli/main.py`, `tests/unit/test_review_memory.py`
- **Action**: Add `seed-review` and `recall-review <query>` commands to the `memory_app` Typer group in `src/ferova/cli/main.py`, mirroring the builder commands. Wire them to `seed_review_memory()` and `recall_review_lessons()` respectively. Add a unit test in `tests/unit/test_review_memory.py` to verify CLI commands call the correct functions.
- **Commit**: `feat(cli): memory seed-review + recall-review commands`
- **Done when**: ferova memory seed-review && ferova memory recall-review "docstring missing" exits 0 and pytest tests/unit/test_review_memory.py::test_cli_commands_call_correct_functions passes
- **Unit tests**: `tests/unit/test_review_memory.py::test_cli_commands_call_correct_functions`

## Step 5 — Add integration test for review memory end-to-end

- **Files**: `tests/integration/test_review_memory_integration.py`, `tests/unit/test_review_memory_integration.py`
- **Action**: Create `tests/integration/test_review_memory_integration.py` to verify the end-to-end flow: seeding, recall, kill-switch off, and prompt injection during a dry-run review. Use a fake agentmemory service or mock the client to avoid live network dependencies. Add a unit test in `tests/unit/test_review_memory_integration.py` to verify the test helpers and mocks work as expected.
- **Commit**: `test(review): integration test for review memory recall and prompt injection`
- **Done when**: pytest tests/integration/test_review_memory_integration.py passes and pytest tests/unit/test_review_memory_integration.py passes
- **Unit tests**: `tests/unit/test_review_memory_integration.py::test_fake_agentmemory_client_behavior`

## Integration tests

- `tests/integration/test_review_memory_integration.py`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-REVIEW-MEMORY",
  "title": "Implement review-scoped agentmemory recall and curated seeds",
  "summary": "Add review-scoped agentmemory recall to the review orchestrator, mirroring the builder memory pattern. This includes a new `review_memory.py` module with curated seed lessons, a config kill-switch, CLI commands, and integration into the orchestrator's prompt rendering. The feature degrades gracefully on service failure or kill-switch off.",
  "steps": [
    {
      "index": 1,
      "title": "Add review_memory.py module with curated seed lessons",
      "files": [
        "src/ferova/review/review_memory.py",
        "tests/unit/test_review_memory.py"
      ],
      "action": "Create `src/ferova/review/review_memory.py` with constants `REVIEW_PROJECT = \"review\"` and `SEED_REVIEW_LESSONS`, functions `recall_review_lessons(query: str) -> list[str]`, `review_lessons_section(lessons: list[str]) -> str`, and `seed_review_memory() -> int`. Mirror the builder_memory.py pattern, including graceful degradation on kill-switch or service failure. Write unit tests in `tests/unit/test_review_memory.py` covering recall, seeding, and kill-switch behavior.",
      "commit_message": "feat(review): review-scoped agentmemory module with curated seed traps",
      "done_when": "python -c \"from ferova.review.review_memory import recall_review_lessons, review_lessons_section, seed_review_memory; assert seed_review_memory() == 6\" succeeds and pytest tests/unit/test_review_memory.py passes",
      "unit_tests": [
        "tests/unit/test_review_memory.py::test_recall_disabled_is_noop",
        "tests/unit/test_review_memory.py::test_recall_enabled_calls_client_with_review_project",
        "tests/unit/test_review_memory.py::test_lessons_section_empty_is_blank",
        "tests/unit/test_review_memory.py::test_lessons_section_renders_block",
        "tests/unit/test_review_memory.py::test_seed_writes_all_lessons"
      ]
    },
    {
      "index": 2,
      "title": "Add review_memory_enabled config field",
      "files": [
        "src/ferova/core/config.py",
        "tests/unit/test_review_memory.py"
      ],
      "action": "Add `review_memory_enabled: bool` field to the `Settings` class in `src/ferova/core/config.py`, defaulting to `True`, with validation aliases `FEROVA_REVIEW_MEMORY_ENABLED` and `REVIEW_MEMORY_ENABLED` (using `AliasChoices`). Add a description marking it as the kill-switch for the review recall loop. Add a unit test in `tests/unit/test_review_memory.py` to verify the config field is accessible and defaults to `True`.",
      "commit_message": "feat(core): add review_memory_enabled config kill-switch",
      "done_when": "python -c \"from ferova.core.config import get_settings; assert get_settings().review_memory_enabled is True\" succeeds and pytest tests/unit/test_review_memory.py::test_config_kill_switch_defaults_true passes",
      "unit_tests": [
        "tests/unit/test_review_memory.py::test_config_kill_switch_defaults_true"
      ]
    },
    {
      "index": 3,
      "title": "Integrate review memory recall into orchestrator",
      "files": [
        "src/ferova/review/orchestrator.py",
        "tests/unit/test_review_memory.py"
      ],
      "action": "Modify `ReviewTeamOrchestrator.review_pr` in `src/ferova/review/orchestrator.py` to perform one review-scoped recall per run (query built from PR title + changed file paths). Append the rendered lessons section to every reviewer's prompt. Emit a `review_team.lessons_recalled` structlog info with `n_lessons`. Add a unit test in `tests/unit/test_review_memory.py` to verify prompt injection and logging.",
      "commit_message": "feat(review): orchestrator recalls review lessons once per run, appends to prompts",
      "done_when": "python -c \"from ferova.review.orchestrator import ReviewTeamOrchestrator; import tempfile; orch = ReviewTeamOrchestrator(post_to_github=False, db_path=tempfile.mktemp()); assert hasattr(orch, '_review_memory_recall')\" succeeds and pytest tests/unit/test_review_memory.py::test_orchestrator_appends_lessons_to_prompt passes",
      "unit_tests": [
        "tests/unit/test_review_memory.py::test_orchestrator_appends_lessons_to_prompt"
      ]
    },
    {
      "index": 4,
      "title": "Add memory CLI commands for review scope",
      "files": [
        "src/ferova/cli/main.py",
        "tests/unit/test_review_memory.py"
      ],
      "action": "Add `seed-review` and `recall-review <query>` commands to the `memory_app` Typer group in `src/ferova/cli/main.py`, mirroring the builder commands. Wire them to `seed_review_memory()` and `recall_review_lessons()` respectively. Add a unit test in `tests/unit/test_review_memory.py` to verify CLI commands call the correct functions.",
      "commit_message": "feat(cli): memory seed-review + recall-review commands",
      "done_when": "ferova memory seed-review && ferova memory recall-review \"docstring missing\" exits 0 and pytest tests/unit/test_review_memory.py::test_cli_commands_call_correct_functions passes",
      "unit_tests": [
        "tests/unit/test_review_memory.py::test_cli_commands_call_correct_functions"
      ]
    },
    {
      "index": 5,
      "title": "Add integration test for review memory end-to-end",
      "files": [
        "tests/integration/test_review_memory_integration.py",
        "tests/unit/test_review_memory_integration.py"
      ],
      "action": "Create `tests/integration/test_review_memory_integration.py` to verify the end-to-end flow: seeding, recall, kill-switch off, and prompt injection during a dry-run review. Use a fake agentmemory service or mock the client to avoid live network dependencies. Add a unit test in `tests/unit/test_review_memory_integration.py` to verify the test helpers and mocks work as expected.",
      "commit_message": "test(review): integration test for review memory recall and prompt injection",
      "done_when": "pytest tests/integration/test_review_memory_integration.py passes and pytest tests/unit/test_review_memory_integration.py passes",
      "unit_tests": [
        "tests/unit/test_review_memory_integration.py::test_fake_agentmemory_client_behavior"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_review_memory_integration.py"
  ]
}
```
