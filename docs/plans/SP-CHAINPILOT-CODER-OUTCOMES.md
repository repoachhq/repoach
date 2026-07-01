# SP-CHAINPILOT-CODER-OUTCOMES — Harvest Coder outcomes per model

Create a pure read-only module `src/ferova/review/coder_outcomes.py` that aggregates Coder results per model from existing review tables (`pr_coder_responses`, `pr_merges`, `pr_coder_rounds`, `pr_findings`). Exposes a frozen `CoderModelOutcome` dataclass and a single `harvest_coder_outcomes(db_path) -> list[CoderModelOutcome]` function. No writes, no new tables, no Settings, no network.

## Step 1 — Create coder_outcomes module and unit tests

- **Files**: `src/ferova/review/coder_outcomes.py`, `tests/unit/test_coder_outcomes.py`
- **Action**: Create `src/ferova/review/coder_outcomes.py` with:

- `from __future__ import annotations`
- `from dataclasses import dataclass`
- `from pathlib import Path`
- `from sqlalchemy import select, func`
- `from ferova.review.persistence import _pr_coder_responses as pr_coder_responses, _pr_merges as pr_merges, init_schema, _engine_for`
- `from ferova.review.stuck import pr_coder_rounds, init_stuck_schema`
- `from ferova.review.findings import pr_findings, init_findings_schema, FindingStatus, Severity`

Define:
```python
@dataclass(frozen=True)
class CoderModelOutcome:
    model: str
    n_prs: int
    n_ci_green: int
    ci_green_rate: float | None
    avg_rounds_to_green: float | None
    n_stuck: int
    stuck_rate: float | None
```

Implement `harvest_coder_outcomes(db_path: Path) -> list[CoderModelOutcome]`:
1. Call `init_schema(db_path)`, `init_stuck_schema(db_path)`, `init_findings_schema(db_path)` (idempotent, handles missing tables).
2. Query `pr_coder_responses` for distinct `(model_used, pr_number)` pairs, grouped by model.
3. For each model, count distinct PRs (`n_prs`).
4. Query `pr_merges` for PRs with outcome in `('APPROVE', 'ALREADY_MERGED')` that belong to the model's PRs → `n_ci_green`.
5. For each model's merged PRs, query `pr_coder_rounds` to count rounds per PR → compute `avg_rounds_to_green` (mean round count across merged PRs; `None` when no merged PR).
6. Query `pr_findings` for PRs with `status == 'stuck'` and `severity == 'blocking'` that belong to the model's PRs → `n_stuck`.
7. Compute `ci_green_rate = n_ci_green / n_prs` (or `None` when `n_prs == 0`), `stuck_rate = n_stuck / n_prs` (or `None`).
8. Return list sorted by `model`.

Create `tests/unit/test_coder_outcomes.py` with tests using `tmp_path` SQLite DB:
- `test_nominal_case` — 4 PRs for model A, 3 merged, rounds 1/2/2, 1 stuck → verify all fields.
- `test_no_merged_prs` — model with PRs but none merged → `ci_green_rate=0.0`, `avg_rounds_to_green=None`.
- `test_empty_db` — fresh DB → `[]`.
- `test_multi_model_pr` — one PR served by two models → both models receive the PR's outcome.
- `test_merged_pr_without_rounds` — merged PR with no `pr_coder_rounds` row → counts toward `n_ci_green`, contributes no round sample.
- `test_zero_prs_model` — model with zero attributed PRs → `ci_green_rate=None`, `stuck_rate=None`.
- `test_missing_table_handling` — call on a DB path that has never been initialized → returns `[]` without raising.
- `test_read_only` — verify no INSERT/UPDATE statements are executed (use `conn.execute` spy or verify DB state unchanged after harvest).
- **Commit**: `feat(review): add coder_outcomes harvest module`
- **Done when**: pytest tests/unit/test_coder_outcomes.py -v --tb=short 2>&1 | tail -20 && python -c 'from ferova.review.coder_outcomes import CoderModelOutcome, harvest_coder_outcomes; print("imports ok")'
- **Unit tests**: `tests/unit/test_coder_outcomes.py::test_nominal_case`, `tests/unit/test_coder_outcomes.py::test_no_merged_prs`, `tests/unit/test_coder_outcomes.py::test_empty_db`, `tests/unit/test_coder_outcomes.py::test_multi_model_pr`, `tests/unit/test_coder_outcomes.py::test_merged_pr_without_rounds`, `tests/unit/test_coder_outcomes.py::test_zero_prs_model`, `tests/unit/test_coder_outcomes.py::test_missing_table_handling`, `tests/unit/test_coder_outcomes.py::test_read_only`

## Step 2 — Create integration test for coder_outcomes

- **Files**: `tests/integration/test_coder_outcomes.py`
- **Action**: Create `tests/integration/test_coder_outcomes.py` with a single integration test that:
1. Creates a temp SQLite DB at `tmp_path / "review.db"`.
2. Calls `init_schema`, `init_stuck_schema`, `init_findings_schema` to create all tables.
3. Inserts rows into `pr_coder_responses` (two models, 3 PRs total, one PR shared by both models).
4. Inserts rows into `pr_merges` (2 merged outcomes for model A, 1 for model B).
5. Inserts rows into `pr_coder_rounds` (rounds for the merged PRs).
6. Inserts rows into `pr_findings` (1 stuck blocking finding for model A).
7. Calls `harvest_coder_outcomes(db_path)` and asserts:
   - 2 `CoderModelOutcome` entries, ordered by model.
   - Model A: `n_prs=2`, `n_ci_green=2`, `ci_green_rate=1.0`, `avg_rounds_to_green` computed correctly, `n_stuck=1`, `stuck_rate=0.5`.
   - Model B: `n_prs=2`, `n_ci_green=1`, `ci_green_rate=0.5`, `avg_rounds_to_green` computed correctly, `n_stuck=0`, `stuck_rate=0.0`.
8. Verifies no side-effects: re-querying tables shows same row count.

Use `from ferova.review.persistence import init_schema, _engine_for, _pr_coder_responses as pr_coder_responses, _pr_merges as pr_merges` and `from sqlalchemy import insert` to seed data.
- **Commit**: `test(review): add integration test for coder_outcomes harvest`
- **Done when**: pytest tests/integration/test_coder_outcomes.py -v --tb=short 2>&1 | tail -20
- **Unit tests**: `tests/unit/test_coder_outcomes.py::test_nominal_case`

## Integration tests

- `tests/integration/test_coder_outcomes.py`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-CHAINPILOT-CODER-OUTCOMES",
  "title": "Harvest Coder outcomes per model",
  "summary": "Create a pure read-only module `src/ferova/review/coder_outcomes.py` that aggregates Coder results per model from existing review tables (`pr_coder_responses`, `pr_merges`, `pr_coder_rounds`, `pr_findings`). Exposes a frozen `CoderModelOutcome` dataclass and a single `harvest_coder_outcomes(db_path) -> list[CoderModelOutcome]` function. No writes, no new tables, no Settings, no network.",
  "steps": [
    {
      "index": 1,
      "title": "Create coder_outcomes module and unit tests",
      "files": [
        "src/ferova/review/coder_outcomes.py",
        "tests/unit/test_coder_outcomes.py"
      ],
      "action": "Create `src/ferova/review/coder_outcomes.py` with:\n\n- `from __future__ import annotations`\n- `from dataclasses import dataclass`\n- `from pathlib import Path`\n- `from sqlalchemy import select, func`\n- `from ferova.review.persistence import _pr_coder_responses as pr_coder_responses, _pr_merges as pr_merges, init_schema, _engine_for`\n- `from ferova.review.stuck import pr_coder_rounds, init_stuck_schema`\n- `from ferova.review.findings import pr_findings, init_findings_schema, FindingStatus, Severity`\n\nDefine:\n```python\n@dataclass(frozen=True)\nclass CoderModelOutcome:\n    model: str\n    n_prs: int\n    n_ci_green: int\n    ci_green_rate: float | None\n    avg_rounds_to_green: float | None\n    n_stuck: int\n    stuck_rate: float | None\n```\n\nImplement `harvest_coder_outcomes(db_path: Path) -> list[CoderModelOutcome]`:\n1. Call `init_schema(db_path)`, `init_stuck_schema(db_path)`, `init_findings_schema(db_path)` (idempotent, handles missing tables).\n2. Query `pr_coder_responses` for distinct `(model_used, pr_number)` pairs, grouped by model.\n3. For each model, count distinct PRs (`n_prs`).\n4. Query `pr_merges` for PRs with outcome in `('APPROVE', 'ALREADY_MERGED')` that belong to the model's PRs → `n_ci_green`.\n5. For each model's merged PRs, query `pr_coder_rounds` to count rounds per PR → compute `avg_rounds_to_green` (mean round count across merged PRs; `None` when no merged PR).\n6. Query `pr_findings` for PRs with `status == 'stuck'` and `severity == 'blocking'` that belong to the model's PRs → `n_stuck`.\n7. Compute `ci_green_rate = n_ci_green / n_prs` (or `None` when `n_prs == 0`), `stuck_rate = n_stuck / n_prs` (or `None`).\n8. Return list sorted by `model`.\n\nCreate `tests/unit/test_coder_outcomes.py` with tests using `tmp_path` SQLite DB:\n- `test_nominal_case` — 4 PRs for model A, 3 merged, rounds 1/2/2, 1 stuck → verify all fields.\n- `test_no_merged_prs` — model with PRs but none merged → `ci_green_rate=0.0`, `avg_rounds_to_green=None`.\n- `test_empty_db` — fresh DB → `[]`.\n- `test_multi_model_pr` — one PR served by two models → both models receive the PR's outcome.\n- `test_merged_pr_without_rounds` — merged PR with no `pr_coder_rounds` row → counts toward `n_ci_green`, contributes no round sample.\n- `test_zero_prs_model` — model with zero attributed PRs → `ci_green_rate=None`, `stuck_rate=None`.\n- `test_missing_table_handling` — call on a DB path that has never been initialized → returns `[]` without raising.\n- `test_read_only` — verify no INSERT/UPDATE statements are executed (use `conn.execute` spy or verify DB state unchanged after harvest).",
      "commit_message": "feat(review): add coder_outcomes harvest module",
      "done_when": "pytest tests/unit/test_coder_outcomes.py -v --tb=short 2>&1 | tail -20 && python -c 'from ferova.review.coder_outcomes import CoderModelOutcome, harvest_coder_outcomes; print(\"imports ok\")'",
      "unit_tests": [
        "tests/unit/test_coder_outcomes.py::test_nominal_case",
        "tests/unit/test_coder_outcomes.py::test_no_merged_prs",
        "tests/unit/test_coder_outcomes.py::test_empty_db",
        "tests/unit/test_coder_outcomes.py::test_multi_model_pr",
        "tests/unit/test_coder_outcomes.py::test_merged_pr_without_rounds",
        "tests/unit/test_coder_outcomes.py::test_zero_prs_model",
        "tests/unit/test_coder_outcomes.py::test_missing_table_handling",
        "tests/unit/test_coder_outcomes.py::test_read_only"
      ]
    },
    {
      "index": 2,
      "title": "Create integration test for coder_outcomes",
      "files": [
        "tests/integration/test_coder_outcomes.py"
      ],
      "action": "Create `tests/integration/test_coder_outcomes.py` with a single integration test that:\n1. Creates a temp SQLite DB at `tmp_path / \"review.db\"`.\n2. Calls `init_schema`, `init_stuck_schema`, `init_findings_schema` to create all tables.\n3. Inserts rows into `pr_coder_responses` (two models, 3 PRs total, one PR shared by both models).\n4. Inserts rows into `pr_merges` (2 merged outcomes for model A, 1 for model B).\n5. Inserts rows into `pr_coder_rounds` (rounds for the merged PRs).\n6. Inserts rows into `pr_findings` (1 stuck blocking finding for model A).\n7. Calls `harvest_coder_outcomes(db_path)` and asserts:\n   - 2 `CoderModelOutcome` entries, ordered by model.\n   - Model A: `n_prs=2`, `n_ci_green=2`, `ci_green_rate=1.0`, `avg_rounds_to_green` computed correctly, `n_stuck=1`, `stuck_rate=0.5`.\n   - Model B: `n_prs=2`, `n_ci_green=1`, `ci_green_rate=0.5`, `avg_rounds_to_green` computed correctly, `n_stuck=0`, `stuck_rate=0.0`.\n8. Verifies no side-effects: re-querying tables shows same row count.\n\nUse `from ferova.review.persistence import init_schema, _engine_for, _pr_coder_responses as pr_coder_responses, _pr_merges as pr_merges` and `from sqlalchemy import insert` to seed data.",
      "commit_message": "test(review): add integration test for coder_outcomes harvest",
      "done_when": "pytest tests/integration/test_coder_outcomes.py -v --tb=short 2>&1 | tail -20",
      "unit_tests": [
        "tests/unit/test_coder_outcomes.py::test_nominal_case"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_coder_outcomes.py"
  ]
}
```
