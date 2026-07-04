# SP-CHAINS-THINKING-CLASS — Machine-readable thinking class for chain models

Add a ThinkingClass type and model-family classification table to the provider catalog, a pure audit_chain_thinking function that reports reasoner/unknown chain heads, and a read-only ferova chains-audit CLI command that prints findings and exits 0.

## Step 1 — Add ThinkingClass type, classification table, and classify_thinking to catalog

- **Files**: `src/ferova/llm_proxy/providers/catalog.py`, `tests/unit/test_provider_catalog.py`
- **Action**: In catalog.py, define ThinkingClass as Literal['reasoner','hybrid','non_thinking','unknown'], add a THINKING_CLASSIFICATION dict mapping model-family substring patterns to ThinkingClass (covering glm-5.2→hybrid, minimax-m3→reasoner, qwen3.7-max→hybrid, deepseek-v4-pro→hybrid, kimi-k2.6→reasoner, mistral-medium-3.5→non_thinking, claude_code opus/sonnet/haiku→hybrid), and add a pure function classify_thinking(model_id: str) -> ThinkingClass that matches the model_id against patterns (first match wins, default unknown). In test_provider_catalog.py, add test_every_live_chain_model_has_a_thinking_class (AC1) enumerating the seven spec-listed models and asserting non-unknown, plus tests for classify_thinking edge cases.
- **Commit**: `feat(catalog): add ThinkingClass, classification table, and classify_thinking function`
- **Done when**: pytest tests/unit/test_provider_catalog.py::test_every_live_chain_model_has_a_thinking_class passes and ruff check src/ferova/llm_proxy/providers/catalog.py exits 0
- **Unit tests**: `tests/unit/test_provider_catalog.py::test_every_live_chain_model_has_a_thinking_class`, `tests/unit/test_provider_catalog.py::test_classify_thinking_known_hybrid`, `tests/unit/test_provider_catalog.py::test_classify_thinking_known_reasoner`, `tests/unit/test_provider_catalog.py::test_classify_thinking_unknown_model`

## Step 2 — Create audit_chain_thinking pure function

- **Files**: `src/ferova/llm_proxy/providers/thinking_audit.py`, `tests/unit/test_chains_thinking_audit.py`
- **Action**: Create thinking_audit.py with a pure function audit_chain_thinking(chains: Mapping[str, list[str]]) -> list[str] that for each chain extracts the head model id (first element), strips the provider prefix to get the model family, calls classify_thinking, and if the class is reasoner or unknown emits a finding line naming the chain, model, and class. Skip chains with empty model lists (emit a malformed-chain finding). Write test_chains_thinking_audit.py with AC2 (reasoner head reported), AC3 (non_thinking head clean), AC4 (unknown model reported), plus edge cases for empty chains and malformed chains.
- **Commit**: `feat(providers): add audit_chain_thinking pure function`
- **Done when**: pytest tests/unit/test_chains_thinking_audit.py passes and ruff check src/ferova/llm_proxy/providers/thinking_audit.py exits 0
- **Unit tests**: `tests/unit/test_chains_thinking_audit.py::test_reasoner_head_is_reported`, `tests/unit/test_chains_thinking_audit.py::test_non_thinking_head_is_clean`, `tests/unit/test_chains_thinking_audit.py::test_unknown_model_is_reported_not_guessed`, `tests/unit/test_chains_thinking_audit.py::test_empty_chain_list_no_findings`, `tests/unit/test_chains_thinking_audit.py::test_malformed_chain_reported`

## Step 3 — Add chains-audit CLI command

- **Files**: `src/ferova/cli/main.py`, `tests/unit/test_chains_audit_cli.py`, `tests/integration/test_chains_audit_cli.py`
- **Action**: Add a chains-audit Typer command to the main app in main.py. It reads Settings() to get model_opus/model_sonnet/model_haiku, parses each into a list of model ids using Chain.parse, builds a dict of chain name → model id list, calls audit_chain_thinking, prints each finding line with typer.echo, and always exits 0. Write a unit test in tests/unit/test_chains_audit_cli.py asserting the chains-audit command is registered on the Typer app and that its callback returns None (no exception path). Write an integration test in tests/integration/test_chains_audit_cli.py that invokes the CLI with a temporary chains.env containing a reasoner head and asserts the finding is printed and exit code is 0.
- **Commit**: `feat(cli): add chains-audit command for thinking-class visibility`
- **Done when**: pytest tests/unit/test_chains_audit_cli.py passes, pytest tests/integration/test_chains_audit_cli.py passes, and python -c "from ferova.cli.main import app; assert 'chains-audit' in [c.name for c in app.registered_commands]" succeeds
- **Unit tests**: `tests/unit/test_chains_audit_cli.py::test_chains_audit_command_is_registered`

## Integration tests

- `tests/integration/test_chains_audit_cli.py::test_chains_audit_reports_reasoner_head`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-CHAINS-THINKING-CLASS",
  "title": "Machine-readable thinking class for chain models",
  "summary": "Add a ThinkingClass type and model-family classification table to the provider catalog, a pure audit_chain_thinking function that reports reasoner/unknown chain heads, and a read-only ferova chains-audit CLI command that prints findings and exits 0.",
  "steps": [
    {
      "index": 1,
      "title": "Add ThinkingClass type, classification table, and classify_thinking to catalog",
      "files": [
        "src/ferova/llm_proxy/providers/catalog.py",
        "tests/unit/test_provider_catalog.py"
      ],
      "action": "In catalog.py, define ThinkingClass as Literal['reasoner','hybrid','non_thinking','unknown'], add a THINKING_CLASSIFICATION dict mapping model-family substring patterns to ThinkingClass (covering glm-5.2→hybrid, minimax-m3→reasoner, qwen3.7-max→hybrid, deepseek-v4-pro→hybrid, kimi-k2.6→reasoner, mistral-medium-3.5→non_thinking, claude_code opus/sonnet/haiku→hybrid), and add a pure function classify_thinking(model_id: str) -> ThinkingClass that matches the model_id against patterns (first match wins, default unknown). In test_provider_catalog.py, add test_every_live_chain_model_has_a_thinking_class (AC1) enumerating the seven spec-listed models and asserting non-unknown, plus tests for classify_thinking edge cases.",
      "commit_message": "feat(catalog): add ThinkingClass, classification table, and classify_thinking function",
      "done_when": "pytest tests/unit/test_provider_catalog.py::test_every_live_chain_model_has_a_thinking_class passes and ruff check src/ferova/llm_proxy/providers/catalog.py exits 0",
      "unit_tests": [
        "tests/unit/test_provider_catalog.py::test_every_live_chain_model_has_a_thinking_class",
        "tests/unit/test_provider_catalog.py::test_classify_thinking_known_hybrid",
        "tests/unit/test_provider_catalog.py::test_classify_thinking_known_reasoner",
        "tests/unit/test_provider_catalog.py::test_classify_thinking_unknown_model"
      ]
    },
    {
      "index": 2,
      "title": "Create audit_chain_thinking pure function",
      "files": [
        "src/ferova/llm_proxy/providers/thinking_audit.py",
        "tests/unit/test_chains_thinking_audit.py"
      ],
      "action": "Create thinking_audit.py with a pure function audit_chain_thinking(chains: Mapping[str, list[str]]) -> list[str] that for each chain extracts the head model id (first element), strips the provider prefix to get the model family, calls classify_thinking, and if the class is reasoner or unknown emits a finding line naming the chain, model, and class. Skip chains with empty model lists (emit a malformed-chain finding). Write test_chains_thinking_audit.py with AC2 (reasoner head reported), AC3 (non_thinking head clean), AC4 (unknown model reported), plus edge cases for empty chains and malformed chains.",
      "commit_message": "feat(providers): add audit_chain_thinking pure function",
      "done_when": "pytest tests/unit/test_chains_thinking_audit.py passes and ruff check src/ferova/llm_proxy/providers/thinking_audit.py exits 0",
      "unit_tests": [
        "tests/unit/test_chains_thinking_audit.py::test_reasoner_head_is_reported",
        "tests/unit/test_chains_thinking_audit.py::test_non_thinking_head_is_clean",
        "tests/unit/test_chains_thinking_audit.py::test_unknown_model_is_reported_not_guessed",
        "tests/unit/test_chains_thinking_audit.py::test_empty_chain_list_no_findings",
        "tests/unit/test_chains_thinking_audit.py::test_malformed_chain_reported"
      ]
    },
    {
      "index": 3,
      "title": "Add chains-audit CLI command",
      "files": [
        "src/ferova/cli/main.py",
        "tests/unit/test_chains_audit_cli.py",
        "tests/integration/test_chains_audit_cli.py"
      ],
      "action": "Add a chains-audit Typer command to the main app in main.py. It reads Settings() to get model_opus/model_sonnet/model_haiku, parses each into a list of model ids using Chain.parse, builds a dict of chain name → model id list, calls audit_chain_thinking, prints each finding line with typer.echo, and always exits 0. Write a unit test in tests/unit/test_chains_audit_cli.py asserting the chains-audit command is registered on the Typer app and that its callback returns None (no exception path). Write an integration test in tests/integration/test_chains_audit_cli.py that invokes the CLI with a temporary chains.env containing a reasoner head and asserts the finding is printed and exit code is 0.",
      "commit_message": "feat(cli): add chains-audit command for thinking-class visibility",
      "done_when": "pytest tests/unit/test_chains_audit_cli.py passes, pytest tests/integration/test_chains_audit_cli.py passes, and python -c \"from ferova.cli.main import app; assert 'chains-audit' in [c.name for c in app.registered_commands]\" succeeds",
      "unit_tests": [
        "tests/unit/test_chains_audit_cli.py::test_chains_audit_command_is_registered"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_chains_audit_cli.py::test_chains_audit_reports_reasoner_head"
  ]
}
```
