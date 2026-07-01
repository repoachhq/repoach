# SP-DEV-STEP-CONTEXT — Import gate + spec context in Developer step brief

Two structural fixes to the hallucinated-import class: (1) a new deterministic import-resolution gate (`check_imports`) that catches any `ferova.*` import violation with directive feedback — nearest-parent package listing, difflib close-name matches, and real symbol location — and (2) wiring it into `execute_plan_step` after the syntax gate and before the ruff gate, while also threading the spec's `raw_markdown` through `build_step_brief` so the Developer can resolve 'as specified in the spec' itself instead of improvising.

## Step 1 — Create import_gate module with unit tests

- **Files**: `src/ferova/review/import_gate.py`, `tests/unit/test_import_gate.py`
- **Action**: Create `src/ferova/review/import_gate.py` with the exact imports `import ast`, `import difflib`, `import re`, `from pathlib import Path` (no others at module level) and one public function:

```python
def check_imports(repo_root: Path, paths: list[str]) -> tuple[bool, str]:
```

For each existing `.py` file in `paths`:
1. Read and `ast.parse` the source. Iterate every `ast.Import` / `ast.ImportFrom` node. Skip any module whose dotted name does not start with `'ferova'`.
2. **Module resolution**: for a dotted ferova module path `a.b.c`, resolve it as `repo_root / 'src' / 'a' / 'b' / 'c'` — accept `<path>.py` or `<path>/__init__.py`. A miss is a violation. On miss: (a) walk up to find the nearest existing parent package directory under `repo_root / 'src'`; (b) list its `*.py` members (strip `.py`, drop `__init__`); (c) call `difflib.get_close_matches(missed_component, existing_names, n=3, cutoff=0.6)` where `missed_component` is the first non-existent part of the dotted path. Append a violation line: `"ferova.<full.path>: module not found; package '<parent>' contains: {', '.join(members)}; close matches: {matches}"`.
3. **Name resolution** (only for `ImportFrom` where the module resolved): read the target file, `ast.parse` it, collect top-level names defined as `ast.FunctionDef`, `ast.AsyncFunctionDef`, `ast.ClassDef`, or `ast.Assign` / `ast.AnnAssign` targets. For each imported name absent from that set, scan `repo_root / 'src' / 'ferova'` recursively for `.py` files whose source matches `re.compile(rf'^(def|class) {re.escape(name)}\b', re.MULTILINE)`; list the first match as the real home. Append: `"<module>.<name>: name not found in <module>; found in <real_home> (or not found anywhere)"`.

Return `(True, "")` when no violations, else `(False, "\n".join(violations))`.

Create `tests/unit/test_import_gate.py` with `from __future__ import annotations`, `from pathlib import Path`, `import pytest`, `from ferova.review.import_gate import check_imports`. Write four tests, each building a fresh `tmp_path` tree:
- `test_clean_imports_pass`: seed `src/ferova/review/findings.py` with `class Finding: ...`; candidate file contains `from ferova.review.findings import Finding`; assert `check_imports(tmp_path, [candidate]) == (True, "")`.
- `test_third_party_ignored`: candidate contains `import requests` and `from pathlib import Path`; assert `(True, "")`.
- `test_missing_module_directive_report`: seed `src/ferova/review/findings.py` with `class Finding: ...`; candidate contains `from ferova.review.models import Finding`; call `check_imports`; assert `ok is False`; assert `'ferova.review.models'` in report; assert `'findings'` in report; assert `'Finding'` in report and `'ferova.review.findings'` in report (gate locates the real home).
- `test_missing_name_located`: seed `src/ferova/review/findings.py` with `class Finding: ...`; candidate contains `from ferova.review.findings import Missing`; assert `ok is False`; assert `'Missing'` in report; assert `'ferova.review.findings'` in report or `'not found anywhere'` in report.
- **Commit**: `feat(dev): import gate — resolve ferova imports with directive feedback`
- **Done when**: pytest tests/unit/test_import_gate.py::test_clean_imports_pass tests/unit/test_import_gate.py::test_third_party_ignored tests/unit/test_import_gate.py::test_missing_module_directive_report tests/unit/test_import_gate.py::test_missing_name_located exits 0
- **Unit tests**: `tests/unit/test_import_gate.py::test_clean_imports_pass`, `tests/unit/test_import_gate.py::test_third_party_ignored`, `tests/unit/test_import_gate.py::test_missing_module_directive_report`, `tests/unit/test_import_gate.py::test_missing_name_located`

## Step 2 — Wire import gate + spec context into dev_runner

- **Files**: `src/ferova/review/dev_runner.py`, `tests/unit/test_import_gate.py`, `tests/integration/test_import_gate.py`
- **Action**: Edit `src/ferova/review/dev_runner.py` — emit the COMPLETE file (all 825+ lines) with these precise changes:

**1. New import** — add to the existing `from .coder_loop import ...` block:
```python
from .import_gate import check_imports
```

**2. New module constant** — add after the existing `DEFAULT_BRANCH_TEMPLATE` constant (line 61):
```python
_BRIEF_SPEC_CAP_CHARS: int = 12_000
```

**3. Extend `build_step_brief` signature** (currently `def build_step_brief(plan: ActionPlan, step: PlanStep, *, gate_feedback: str = "") -> str:`) — add new keyword-only parameter:
```python
def build_step_brief(
    plan: ActionPlan,
    step: PlanStep,
    *,
    gate_feedback: str = "",
    spec_markdown: str = "",
) -> str:
```
At the end of the function body, before `return "\n".join(lines)`, insert:
```python
    if spec_markdown:
        lines += [
            "",
            "## Source spec (verbatim — the plan's authority)",
            "",
            spec_markdown[:_BRIEF_SPEC_CAP_CHARS],
        ]
```

**4. Extend `execute_plan_step` signature** — add `spec_markdown: str = ""` as the last keyword-only parameter (after `db: Path`):
```python
def execute_plan_step(
    step: PlanStep,
    *,
    plan: ActionPlan,
    repo_root: Path,
    developer: Developer,
    repo_tree: str,
    db: Path,
    spec_markdown: str = "",
) -> StepOutcome:
```
In the loop body, update the `build_step_brief` call (currently line 499) to:
```python
        brief = build_step_brief(plan, step, gate_feedback=gate_feedback, spec_markdown=spec_markdown)
```
Insert the import gate check AFTER the syntax gate block (after `if not syntax_ok: ... continue`) and BEFORE `ruff_ok, ruff_tail = run_ruff_gate(repo_root):`:
```python
        imports_ok, imports_report = check_imports(repo_root, list(allowed_paths))
        if not imports_ok:
            revert_working_tree(repo_root)
            gate_feedback = f"import gate: {imports_report}"
            continue
```

**5. Thread `spec.raw_markdown` in `run_developer_session`** — the `execute_plan_step` call (currently lines 737-744) becomes:
```python
        outcome = execute_plan_step(
            step,
            plan=action_plan,
            repo_root=repo,
            developer=dev,
            repo_tree=tree,
            db=db,
            spec_markdown=spec.raw_markdown,
        )
```

Append to `tests/unit/test_import_gate.py` three wiring tests (keep all existing tests; add these at the bottom). Each uses `from __future__ import annotations`, `from pathlib import Path`, `from unittest.mock import MagicMock`, `import subprocess`, `import pytest`, imports from `ferova.review.dev_runner`, `ferova.review.plan`, and `ferova.review.import_gate`:

- `test_brief_carries_spec_section`: call `build_step_brief(plan, step, spec_markdown="# My Spec\nContent")` (build a minimal `ActionPlan`/`PlanStep` inline); assert `"## Source spec (verbatim" in result` and `"# My Spec" in result`.
- `test_brief_without_spec_unchanged`: call `build_step_brief(plan, step)` with no `spec_markdown`; assert `"## Source spec" not in result`.
- `test_step_reverted_on_bad_import`: build a tmp git repo (`git init`, `git config user.email/user.name`, `git add -A`, `git commit -m 'init'`) with `src/ferova/review/findings.py` containing `class Finding: ...`; build a one-step `ActionPlan` whose step `files` is `["candidate.py"]`; build a `MagicMock` Developer whose `respond` returns a fix for `candidate.py` containing `from ferova.review.models import Finding\n` (non-existent module); monkeypatch `ferova.review.dev_runner.run_ruff_gate` to return `(True, "")` and `ferova.review.dev_runner.run_promised_tests` to return `(True, "", False)`; call `execute_plan_step(step, plan=plan, repo_root=tmp_path, developer=dev, repo_tree="", db=tmp_path / "t.db", spec_markdown="")` using a real db (call `init_schema` first); assert `outcome.ok is False`; assert `"import gate:" in outcome.reason`.

Create `tests/integration/test_import_gate.py` with a single test `test_check_imports_smoke_scenario` that exercises the Smoke Scenario end-to-end: build a `tmp_path` tree with `src/ferova/review/findings.py` containing `class Finding: ...`; write a candidate file `candidate.py` with `from ferova.review.models import Finding`; first call returns `(False, report)` with `'ferova.review.models'` in report and `'findings'` in report; fix the import to `from ferova.review.findings import Finding`; second call returns `(True, "")`. Import only `from pathlib import Path` and `from ferova.review.import_gate import check_imports`.
- **Commit**: `feat(dev): step brief carries the spec verbatim + import gate wired`
- **Done when**: pytest tests/unit/test_import_gate.py::test_brief_carries_spec_section tests/unit/test_import_gate.py::test_brief_without_spec_unchanged tests/unit/test_import_gate.py::test_step_reverted_on_bad_import tests/integration/test_import_gate.py::test_check_imports_smoke_scenario exits 0 and ruff check src exits 0
- **Unit tests**: `tests/unit/test_import_gate.py::test_brief_carries_spec_section`, `tests/unit/test_import_gate.py::test_brief_without_spec_unchanged`, `tests/unit/test_import_gate.py::test_step_reverted_on_bad_import`

## Integration tests

- `tests/integration/test_import_gate.py::test_check_imports_smoke_scenario`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-DEV-STEP-CONTEXT",
  "title": "Import gate + spec context in Developer step brief",
  "summary": "Two structural fixes to the hallucinated-import class: (1) a new deterministic import-resolution gate (`check_imports`) that catches any `ferova.*` import violation with directive feedback — nearest-parent package listing, difflib close-name matches, and real symbol location — and (2) wiring it into `execute_plan_step` after the syntax gate and before the ruff gate, while also threading the spec's `raw_markdown` through `build_step_brief` so the Developer can resolve 'as specified in the spec' itself instead of improvising.",
  "steps": [
    {
      "index": 1,
      "title": "Create import_gate module with unit tests",
      "files": [
        "src/ferova/review/import_gate.py",
        "tests/unit/test_import_gate.py"
      ],
      "action": "Create `src/ferova/review/import_gate.py` with the exact imports `import ast`, `import difflib`, `import re`, `from pathlib import Path` (no others at module level) and one public function:\n\n```python\ndef check_imports(repo_root: Path, paths: list[str]) -> tuple[bool, str]:\n```\n\nFor each existing `.py` file in `paths`:\n1. Read and `ast.parse` the source. Iterate every `ast.Import` / `ast.ImportFrom` node. Skip any module whose dotted name does not start with `'ferova'`.\n2. **Module resolution**: for a dotted ferova module path `a.b.c`, resolve it as `repo_root / 'src' / 'a' / 'b' / 'c'` — accept `<path>.py` or `<path>/__init__.py`. A miss is a violation. On miss: (a) walk up to find the nearest existing parent package directory under `repo_root / 'src'`; (b) list its `*.py` members (strip `.py`, drop `__init__`); (c) call `difflib.get_close_matches(missed_component, existing_names, n=3, cutoff=0.6)` where `missed_component` is the first non-existent part of the dotted path. Append a violation line: `\"ferova.<full.path>: module not found; package '<parent>' contains: {', '.join(members)}; close matches: {matches}\"`.\n3. **Name resolution** (only for `ImportFrom` where the module resolved): read the target file, `ast.parse` it, collect top-level names defined as `ast.FunctionDef`, `ast.AsyncFunctionDef`, `ast.ClassDef`, or `ast.Assign` / `ast.AnnAssign` targets. For each imported name absent from that set, scan `repo_root / 'src' / 'ferova'` recursively for `.py` files whose source matches `re.compile(rf'^(def|class) {re.escape(name)}\\b', re.MULTILINE)`; list the first match as the real home. Append: `\"<module>.<name>: name not found in <module>; found in <real_home> (or not found anywhere)\"`.\n\nReturn `(True, \"\")` when no violations, else `(False, \"\\n\".join(violations))`.\n\nCreate `tests/unit/test_import_gate.py` with `from __future__ import annotations`, `from pathlib import Path`, `import pytest`, `from ferova.review.import_gate import check_imports`. Write four tests, each building a fresh `tmp_path` tree:\n- `test_clean_imports_pass`: seed `src/ferova/review/findings.py` with `class Finding: ...`; candidate file contains `from ferova.review.findings import Finding`; assert `check_imports(tmp_path, [candidate]) == (True, \"\")`.\n- `test_third_party_ignored`: candidate contains `import requests` and `from pathlib import Path`; assert `(True, \"\")`.\n- `test_missing_module_directive_report`: seed `src/ferova/review/findings.py` with `class Finding: ...`; candidate contains `from ferova.review.models import Finding`; call `check_imports`; assert `ok is False`; assert `'ferova.review.models'` in report; assert `'findings'` in report; assert `'Finding'` in report and `'ferova.review.findings'` in report (gate locates the real home).\n- `test_missing_name_located`: seed `src/ferova/review/findings.py` with `class Finding: ...`; candidate contains `from ferova.review.findings import Missing`; assert `ok is False`; assert `'Missing'` in report; assert `'ferova.review.findings'` in report or `'not found anywhere'` in report.",
      "commit_message": "feat(dev): import gate — resolve ferova imports with directive feedback",
      "done_when": "pytest tests/unit/test_import_gate.py::test_clean_imports_pass tests/unit/test_import_gate.py::test_third_party_ignored tests/unit/test_import_gate.py::test_missing_module_directive_report tests/unit/test_import_gate.py::test_missing_name_located exits 0",
      "unit_tests": [
        "tests/unit/test_import_gate.py::test_clean_imports_pass",
        "tests/unit/test_import_gate.py::test_third_party_ignored",
        "tests/unit/test_import_gate.py::test_missing_module_directive_report",
        "tests/unit/test_import_gate.py::test_missing_name_located"
      ]
    },
    {
      "index": 2,
      "title": "Wire import gate + spec context into dev_runner",
      "files": [
        "src/ferova/review/dev_runner.py",
        "tests/unit/test_import_gate.py",
        "tests/integration/test_import_gate.py"
      ],
      "action": "Edit `src/ferova/review/dev_runner.py` — emit the COMPLETE file (all 825+ lines) with these precise changes:\n\n**1. New import** — add to the existing `from .coder_loop import ...` block:\n```python\nfrom .import_gate import check_imports\n```\n\n**2. New module constant** — add after the existing `DEFAULT_BRANCH_TEMPLATE` constant (line 61):\n```python\n_BRIEF_SPEC_CAP_CHARS: int = 12_000\n```\n\n**3. Extend `build_step_brief` signature** (currently `def build_step_brief(plan: ActionPlan, step: PlanStep, *, gate_feedback: str = \"\") -> str:`) — add new keyword-only parameter:\n```python\ndef build_step_brief(\n    plan: ActionPlan,\n    step: PlanStep,\n    *,\n    gate_feedback: str = \"\",\n    spec_markdown: str = \"\",\n) -> str:\n```\nAt the end of the function body, before `return \"\\n\".join(lines)`, insert:\n```python\n    if spec_markdown:\n        lines += [\n            \"\",\n            \"## Source spec (verbatim — the plan's authority)\",\n            \"\",\n            spec_markdown[:_BRIEF_SPEC_CAP_CHARS],\n        ]\n```\n\n**4. Extend `execute_plan_step` signature** — add `spec_markdown: str = \"\"` as the last keyword-only parameter (after `db: Path`):\n```python\ndef execute_plan_step(\n    step: PlanStep,\n    *,\n    plan: ActionPlan,\n    repo_root: Path,\n    developer: Developer,\n    repo_tree: str,\n    db: Path,\n    spec_markdown: str = \"\",\n) -> StepOutcome:\n```\nIn the loop body, update the `build_step_brief` call (currently line 499) to:\n```python\n        brief = build_step_brief(plan, step, gate_feedback=gate_feedback, spec_markdown=spec_markdown)\n```\nInsert the import gate check AFTER the syntax gate block (after `if not syntax_ok: ... continue`) and BEFORE `ruff_ok, ruff_tail = run_ruff_gate(repo_root):`:\n```python\n        imports_ok, imports_report = check_imports(repo_root, list(allowed_paths))\n        if not imports_ok:\n            revert_working_tree(repo_root)\n            gate_feedback = f\"import gate: {imports_report}\"\n            continue\n```\n\n**5. Thread `spec.raw_markdown` in `run_developer_session`** — the `execute_plan_step` call (currently lines 737-744) becomes:\n```python\n        outcome = execute_plan_step(\n            step,\n            plan=action_plan,\n            repo_root=repo,\n            developer=dev,\n            repo_tree=tree,\n            db=db,\n            spec_markdown=spec.raw_markdown,\n        )\n```\n\nAppend to `tests/unit/test_import_gate.py` three wiring tests (keep all existing tests; add these at the bottom). Each uses `from __future__ import annotations`, `from pathlib import Path`, `from unittest.mock import MagicMock`, `import subprocess`, `import pytest`, imports from `ferova.review.dev_runner`, `ferova.review.plan`, and `ferova.review.import_gate`:\n\n- `test_brief_carries_spec_section`: call `build_step_brief(plan, step, spec_markdown=\"# My Spec\\nContent\")` (build a minimal `ActionPlan`/`PlanStep` inline); assert `\"## Source spec (verbatim\" in result` and `\"# My Spec\" in result`.\n- `test_brief_without_spec_unchanged`: call `build_step_brief(plan, step)` with no `spec_markdown`; assert `\"## Source spec\" not in result`.\n- `test_step_reverted_on_bad_import`: build a tmp git repo (`git init`, `git config user.email/user.name`, `git add -A`, `git commit -m 'init'`) with `src/ferova/review/findings.py` containing `class Finding: ...`; build a one-step `ActionPlan` whose step `files` is `[\"candidate.py\"]`; build a `MagicMock` Developer whose `respond` returns a fix for `candidate.py` containing `from ferova.review.models import Finding\\n` (non-existent module); monkeypatch `ferova.review.dev_runner.run_ruff_gate` to return `(True, \"\")` and `ferova.review.dev_runner.run_promised_tests` to return `(True, \"\", False)`; call `execute_plan_step(step, plan=plan, repo_root=tmp_path, developer=dev, repo_tree=\"\", db=tmp_path / \"t.db\", spec_markdown=\"\")` using a real db (call `init_schema` first); assert `outcome.ok is False`; assert `\"import gate:\" in outcome.reason`.\n\nCreate `tests/integration/test_import_gate.py` with a single test `test_check_imports_smoke_scenario` that exercises the Smoke Scenario end-to-end: build a `tmp_path` tree with `src/ferova/review/findings.py` containing `class Finding: ...`; write a candidate file `candidate.py` with `from ferova.review.models import Finding`; first call returns `(False, report)` with `'ferova.review.models'` in report and `'findings'` in report; fix the import to `from ferova.review.findings import Finding`; second call returns `(True, \"\")`. Import only `from pathlib import Path` and `from ferova.review.import_gate import check_imports`.",
      "commit_message": "feat(dev): step brief carries the spec verbatim + import gate wired",
      "done_when": "pytest tests/unit/test_import_gate.py::test_brief_carries_spec_section tests/unit/test_import_gate.py::test_brief_without_spec_unchanged tests/unit/test_import_gate.py::test_step_reverted_on_bad_import tests/integration/test_import_gate.py::test_check_imports_smoke_scenario exits 0 and ruff check src exits 0",
      "unit_tests": [
        "tests/unit/test_import_gate.py::test_brief_carries_spec_section",
        "tests/unit/test_import_gate.py::test_brief_without_spec_unchanged",
        "tests/unit/test_import_gate.py::test_step_reverted_on_bad_import"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_import_gate.py::test_check_imports_smoke_scenario"
  ]
}
```
