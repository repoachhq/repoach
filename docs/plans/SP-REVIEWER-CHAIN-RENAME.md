# SP-REVIEWER-CHAIN-RENAME — Rename DEFAULT_NIM_CHAIN to DEFAULT_REVIEWER_CHAIN

Retire the misleading module-level constant DEFAULT_NIM_CHAIN in agent_engine/agent_loop.py and replace it with DEFAULT_REVIEWER_CHAIN (same value, PROXY_SONNET_CHAIN). Update the single internal importer in review/reviewer.py to use the new name as the base Reviewer.model_chain default, and remove the now-resolved tech-debt ledger item #1 from docs/tech_debt.md. No behaviour change, no chain-value change, no new architecture edge.

## Step 1 — Rename DEFAULT_NIM_CHAIN to DEFAULT_REVIEWER_CHAIN in agent_loop.py

- **Files**: `src/ferova/agent_engine/agent_loop.py`, `tests/unit/test_agent_loop_chain_rename.py`
- **Action**: In src/ferova/agent_engine/agent_loop.py: (a) rename the module-level definition `DEFAULT_NIM_CHAIN: tuple[str, ...] = PROXY_SONNET_CHAIN` to `DEFAULT_REVIEWER_CHAIN: tuple[str, ...] = PROXY_SONNET_CHAIN`; (b) update the `__all__` entry from `"DEFAULT_NIM_CHAIN"` to `"DEFAULT_REVIEWER_CHAIN"`; (c) update the module docstring — replace the two mentions of `DEFAULT_NIM_CHAIN` (the public-surface bullet at line ~13 and the constants-section bullet at line ~40) with `DEFAULT_REVIEWER_CHAIN`, keeping the surrounding prose truthful (it is the base default for the reviewers' model_chain, not a NIM-only chain). Do NOT add a back-compat alias. Create tests/unit/test_agent_loop_chain_rename.py asserting: (i) `from ferova.agent_engine.agent_loop import DEFAULT_REVIEWER_CHAIN` succeeds; (ii) `DEFAULT_REVIEWER_CHAIN is PROXY_SONNET_CHAIN` (value unchanged); (iii) `"DEFAULT_REVIEWER_CHAIN" in agent_loop.__all__`; (iv) `not hasattr(agent_loop, "DEFAULT_NIM_CHAIN")` (old name gone).
- **Commit**: `refactor(agent_engine): rename DEFAULT_NIM_CHAIN to DEFAULT_REVIEWER_CHAIN`
- **Done when**: pytest tests/unit/test_agent_loop_chain_rename.py passes and `grep -n DEFAULT_NIM_CHAIN src/ferova/agent_engine/agent_loop.py` returns no matches.
- **Unit tests**: `tests/unit/test_agent_loop_chain_rename.py::test_default_reviewer_chain_importable`, `tests/unit/test_agent_loop_chain_rename.py::test_default_reviewer_chain_value_unchanged`, `tests/unit/test_agent_loop_chain_rename.py::test_default_reviewer_chain_in_all`, `tests/unit/test_agent_loop_chain_rename.py::test_old_default_nim_chain_removed`

## Step 2 — Update reviewer.py to import and use DEFAULT_REVIEWER_CHAIN

- **Files**: `src/ferova/review/reviewer.py`, `tests/unit/test_reviewer_default_chain.py`
- **Action**: In src/ferova/review/reviewer.py: (a) replace `DEFAULT_NIM_CHAIN` with `DEFAULT_REVIEWER_CHAIN` in the `from ..agent_engine.agent_loop import (...)` block (line ~33); (b) replace the `model_chain: tuple[str, ...] = DEFAULT_NIM_CHAIN` default on the base `Reviewer` dataclass (line ~404) with `model_chain: tuple[str, ...] = DEFAULT_REVIEWER_CHAIN`. No other changes. Create tests/unit/test_reviewer_default_chain.py asserting: (i) `from ferova.review.reviewer import Reviewer` succeeds (import resolves); (ii) `Reviewer.model_chain is DEFAULT_REVIEWER_CHAIN` (the base-class default resolves to the new constant); (iii) `Reviewer.model_chain is PROXY_SONNET_CHAIN` (value unchanged from before the rename).
- **Commit**: `refactor(review): use DEFAULT_REVIEWER_CHAIN as Reviewer.model_chain default`
- **Done when**: pytest tests/unit/test_reviewer_default_chain.py passes and `grep -rn DEFAULT_NIM_CHAIN src` returns no matches.
- **Unit tests**: `tests/unit/test_reviewer_default_chain.py::test_reviewer_imports_resolve`, `tests/unit/test_reviewer_default_chain.py::test_reviewer_model_chain_default_is_new_constant`, `tests/unit/test_reviewer_default_chain.py::test_reviewer_model_chain_default_value_unchanged`

## Step 3 — Remove tech-debt ledger item #1 from docs/tech_debt.md

- **Files**: `docs/tech_debt.md`
- **Action**: In docs/tech_debt.md, delete the first table row (item #1, the `DEFAULT_NIM_CHAIN` misleading-name entry) entirely — including its leading `| 1 |` cell, all four columns, and the trailing `|` row separator. Renumber the remaining rows so the table stays contiguous starting at `| 1 |` (the former #2 becomes #1, the former #3 becomes #2). Do not touch any other prose or rows.
- **Commit**: `docs(tech_debt): remove resolved DEFAULT_NIM_CHAIN rename entry`
- **Done when**: `grep -n DEFAULT_NIM_CHAIN docs/tech_debt.md` returns no matches and the table still has a contiguous `| 1 |` row at the top.
- **Unit tests**: _(docs-only step — none promised)_

## Step 4 — Add integration coverage and verify acceptance criteria end-to-end

- **Files**: `tests/integration/test_reviewer_chain_default.py`, `tests/unit/test_reviewer_chain_no_stale_refs.py`
- **Action**: Create tests/integration/test_reviewer_chain_default.py that constructs a concrete reviewer subclass (e.g. `ArchitectReviewer` or whichever concrete reviewer is already defined in src/ferova/review/reviewer.py) without passing `model_chain=` and asserts that the resulting instance's `model_chain` is `PROXY_SONNET_CHAIN` (byte-for-byte identical to pre-rename behaviour). Also create tests/unit/test_reviewer_chain_no_stale_refs.py asserting: (i) `DEFAULT_REVIEWER_CHAIN` is importable from `ferova.agent_engine.agent_loop` and equals `PROXY_SONNET_CHAIN`; (ii) the base `Reviewer.model_chain` default is `PROXY_SONNET_CHAIN`; (iii) `DEFAULT_NIM_CHAIN` is not present as an attribute on `ferova.agent_engine.agent_loop` (no back-compat alias). Then run the full verification suite: `grep -rn DEFAULT_NIM_CHAIN src` (must be empty), `ruff check src tests` (exit 0), `ruff format --check src tests` (exit 0), the no-inline-comments lint, `arch check` if present, and `pytest tests/unit` (all green).
- **Commit**: `test(review): integration coverage for renamed reviewer default chain`
- **Done when**: `grep -rn DEFAULT_NIM_CHAIN src` returns no matches; `ruff check src tests` exits 0; `pytest tests/unit tests/integration/test_reviewer_chain_default.py` is green.
- **Unit tests**: `tests/unit/test_reviewer_chain_no_stale_refs.py::test_default_reviewer_chain_resolves_to_proxy_sonnet`, `tests/unit/test_reviewer_chain_no_stale_refs.py::test_reviewer_base_default_is_proxy_sonnet`, `tests/unit/test_reviewer_chain_no_stale_refs.py::test_no_default_nim_chain_attribute`

## Integration tests

- `tests/integration/test_reviewer_chain_default.py`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-REVIEWER-CHAIN-RENAME",
  "title": "Rename DEFAULT_NIM_CHAIN to DEFAULT_REVIEWER_CHAIN",
  "summary": "Retire the misleading module-level constant DEFAULT_NIM_CHAIN in agent_engine/agent_loop.py and replace it with DEFAULT_REVIEWER_CHAIN (same value, PROXY_SONNET_CHAIN). Update the single internal importer in review/reviewer.py to use the new name as the base Reviewer.model_chain default, and remove the now-resolved tech-debt ledger item #1 from docs/tech_debt.md. No behaviour change, no chain-value change, no new architecture edge.",
  "steps": [
    {
      "index": 1,
      "title": "Rename DEFAULT_NIM_CHAIN to DEFAULT_REVIEWER_CHAIN in agent_loop.py",
      "files": [
        "src/ferova/agent_engine/agent_loop.py",
        "tests/unit/test_agent_loop_chain_rename.py"
      ],
      "action": "In src/ferova/agent_engine/agent_loop.py: (a) rename the module-level definition `DEFAULT_NIM_CHAIN: tuple[str, ...] = PROXY_SONNET_CHAIN` to `DEFAULT_REVIEWER_CHAIN: tuple[str, ...] = PROXY_SONNET_CHAIN`; (b) update the `__all__` entry from `\"DEFAULT_NIM_CHAIN\"` to `\"DEFAULT_REVIEWER_CHAIN\"`; (c) update the module docstring — replace the two mentions of `DEFAULT_NIM_CHAIN` (the public-surface bullet at line ~13 and the constants-section bullet at line ~40) with `DEFAULT_REVIEWER_CHAIN`, keeping the surrounding prose truthful (it is the base default for the reviewers' model_chain, not a NIM-only chain). Do NOT add a back-compat alias. Create tests/unit/test_agent_loop_chain_rename.py asserting: (i) `from ferova.agent_engine.agent_loop import DEFAULT_REVIEWER_CHAIN` succeeds; (ii) `DEFAULT_REVIEWER_CHAIN is PROXY_SONNET_CHAIN` (value unchanged); (iii) `\"DEFAULT_REVIEWER_CHAIN\" in agent_loop.__all__`; (iv) `not hasattr(agent_loop, \"DEFAULT_NIM_CHAIN\")` (old name gone).",
      "commit_message": "refactor(agent_engine): rename DEFAULT_NIM_CHAIN to DEFAULT_REVIEWER_CHAIN",
      "done_when": "pytest tests/unit/test_agent_loop_chain_rename.py passes and `grep -n DEFAULT_NIM_CHAIN src/ferova/agent_engine/agent_loop.py` returns no matches.",
      "unit_tests": [
        "tests/unit/test_agent_loop_chain_rename.py::test_default_reviewer_chain_importable",
        "tests/unit/test_agent_loop_chain_rename.py::test_default_reviewer_chain_value_unchanged",
        "tests/unit/test_agent_loop_chain_rename.py::test_default_reviewer_chain_in_all",
        "tests/unit/test_agent_loop_chain_rename.py::test_old_default_nim_chain_removed"
      ]
    },
    {
      "index": 2,
      "title": "Update reviewer.py to import and use DEFAULT_REVIEWER_CHAIN",
      "files": [
        "src/ferova/review/reviewer.py",
        "tests/unit/test_reviewer_default_chain.py"
      ],
      "action": "In src/ferova/review/reviewer.py: (a) replace `DEFAULT_NIM_CHAIN` with `DEFAULT_REVIEWER_CHAIN` in the `from ..agent_engine.agent_loop import (...)` block (line ~33); (b) replace the `model_chain: tuple[str, ...] = DEFAULT_NIM_CHAIN` default on the base `Reviewer` dataclass (line ~404) with `model_chain: tuple[str, ...] = DEFAULT_REVIEWER_CHAIN`. No other changes. Create tests/unit/test_reviewer_default_chain.py asserting: (i) `from ferova.review.reviewer import Reviewer` succeeds (import resolves); (ii) `Reviewer.model_chain is DEFAULT_REVIEWER_CHAIN` (the base-class default resolves to the new constant); (iii) `Reviewer.model_chain is PROXY_SONNET_CHAIN` (value unchanged from before the rename).",
      "commit_message": "refactor(review): use DEFAULT_REVIEWER_CHAIN as Reviewer.model_chain default",
      "done_when": "pytest tests/unit/test_reviewer_default_chain.py passes and `grep -rn DEFAULT_NIM_CHAIN src` returns no matches.",
      "unit_tests": [
        "tests/unit/test_reviewer_default_chain.py::test_reviewer_imports_resolve",
        "tests/unit/test_reviewer_default_chain.py::test_reviewer_model_chain_default_is_new_constant",
        "tests/unit/test_reviewer_default_chain.py::test_reviewer_model_chain_default_value_unchanged"
      ]
    },
    {
      "index": 3,
      "title": "Remove tech-debt ledger item #1 from docs/tech_debt.md",
      "files": [
        "docs/tech_debt.md"
      ],
      "action": "In docs/tech_debt.md, delete the first table row (item #1, the `DEFAULT_NIM_CHAIN` misleading-name entry) entirely — including its leading `| 1 |` cell, all four columns, and the trailing `|` row separator. Renumber the remaining rows so the table stays contiguous starting at `| 1 |` (the former #2 becomes #1, the former #3 becomes #2). Do not touch any other prose or rows.",
      "commit_message": "docs(tech_debt): remove resolved DEFAULT_NIM_CHAIN rename entry",
      "done_when": "`grep -n DEFAULT_NIM_CHAIN docs/tech_debt.md` returns no matches and the table still has a contiguous `| 1 |` row at the top.",
      "unit_tests": []
    },
    {
      "index": 4,
      "title": "Add integration coverage and verify acceptance criteria end-to-end",
      "files": [
        "tests/integration/test_reviewer_chain_default.py",
        "tests/unit/test_reviewer_chain_no_stale_refs.py"
      ],
      "action": "Create tests/integration/test_reviewer_chain_default.py that constructs a concrete reviewer subclass (e.g. `ArchitectReviewer` or whichever concrete reviewer is already defined in src/ferova/review/reviewer.py) without passing `model_chain=` and asserts that the resulting instance's `model_chain` is `PROXY_SONNET_CHAIN` (byte-for-byte identical to pre-rename behaviour). Also create tests/unit/test_reviewer_chain_no_stale_refs.py asserting: (i) `DEFAULT_REVIEWER_CHAIN` is importable from `ferova.agent_engine.agent_loop` and equals `PROXY_SONNET_CHAIN`; (ii) the base `Reviewer.model_chain` default is `PROXY_SONNET_CHAIN`; (iii) `DEFAULT_NIM_CHAIN` is not present as an attribute on `ferova.agent_engine.agent_loop` (no back-compat alias). Then run the full verification suite: `grep -rn DEFAULT_NIM_CHAIN src` (must be empty), `ruff check src tests` (exit 0), `ruff format --check src tests` (exit 0), the no-inline-comments lint, `arch check` if present, and `pytest tests/unit` (all green).",
      "commit_message": "test(review): integration coverage for renamed reviewer default chain",
      "done_when": "`grep -rn DEFAULT_NIM_CHAIN src` returns no matches; `ruff check src tests` exits 0; `pytest tests/unit tests/integration/test_reviewer_chain_default.py` is green.",
      "unit_tests": [
        "tests/unit/test_reviewer_chain_no_stale_refs.py::test_default_reviewer_chain_resolves_to_proxy_sonnet",
        "tests/unit/test_reviewer_chain_no_stale_refs.py::test_reviewer_base_default_is_proxy_sonnet",
        "tests/unit/test_reviewer_chain_no_stale_refs.py::test_no_default_nim_chain_attribute"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_reviewer_chain_default.py"
  ]
}
```
