# SP-DEV-STEP-CONTEXT — the Developer sees the spec, and an import gate backs it up

## Metadata

- **Status**: OPEN
- **Priority**: P0 — third occurrence of the hallucinated-import class;
  blocks redesign slice 3 (SP-FINDER-OUTPUT round 1 died on it)
- **Owner**: operator
- **Executor**: `ferova develop`
- **Opened**: 2026-06-12

## Why

SP-FINDER-OUTPUT round 1 post-mortem: the Planner compressed the
spec's exact import anchors into "Imports exactly as specified in the
spec" — but `build_step_brief` deliberately "replaces the whole spec",
so the Developer never sees it. Mistral improvised
`ferova.review.models`, and on retry tried to **create** the
hallucinated module instead of correcting the import (the contract
gate blocked it). Third occurrence of the import-hallucination class;
memory lessons and spec anchors have both proven insufficient because
they depend on transmission through the plan. Two structural fixes:

1. **Context**: the brief carries the spec text — the Developer can
   resolve "as specified in the spec" itself. The session already
   loads the spec; it just never forwards it.
2. **Backstop**: a deterministic import-resolution gate turns
   `ModuleNotFoundError` into directive feedback — what exists, and
   where the wanted names actually live — so even an improvised
   import converges on retry.

## What

1. **New module `src/ferova/review/import_gate.py`**:
   - `check_imports(repo_root: Path, paths: list[str]) ->
     tuple[bool, str]` — for each existing `.py` file in `paths`,
     parse with `ast.parse` and inspect every `import ferova…` /
     `from ferova… import names`:
     - resolve the module dotted path against
       `repo_root / "src" / <dotted path as dirs>` accepting
       `<path>.py` or `<path>/__init__.py`; a miss is a violation;
     - on a module miss, the message lists the actual `.py` modules
       of the nearest existing parent package, plus close-name
       matches via `difflib.get_close_matches`;
     - for a resolvable `from` module, verify each imported name
       appears in the target's AST as a top-level `def` / `class` /
       assignment target; a missing name is a violation and the
       message greps `src/ferova` for `(def|class) <name>` to
       say where it actually lives;
     - non-`ferova` imports are ignored (stdlib/third-party are
       pytest's job).
     Returns `(True, "")` when clean, else `(False, report)` with one
     line per violation.
2. **Wiring in `src/ferova/review/dev_runner.py`**:
   - In `execute_plan_step`, after the syntax gate and before the
     ruff gate: `imports_ok, imports_report =
     check_imports(repo_root, list(allowed_paths))`; on failure
     `revert_working_tree`, set `gate_feedback = f"import gate:
     {imports_report}"`, `continue` (same shape as the other gates).
   - In `build_step_brief`: new keyword-only parameter
     `spec_markdown: str = ""`; when non-empty, append a final
     section `## Source spec (verbatim — the plan's authority)`
     containing the spec text capped by a module constant
     `_BRIEF_SPEC_CAP_CHARS = 12_000` (cap constant lives in code;
     tests derive any threshold from measured fixtures, never
     hardcode sizes — test-arithmetic law).
   - `run_developer_session` (or wherever steps are executed — find
     the `build_step_brief` call) passes the already-loaded spec's
     `raw_markdown` through.
3. **Repo lint gates in the step chain** (post-mortem amendment: the
   factory's own dispatch of THIS spec died at the commit hook on
   inline-comment + silent-except violations — late, opaque feedback
   the full-file retry forgot): new `run_repo_lint_gates(repo_root,
   paths) -> tuple[bool, str]` in `dev_runner.py` running the repo's
   `no_inline_comments.scan_file` + `no_silent_except.scan_file` on
   the contract `.py` paths, with a house-rules reminder line in the
   report; wired after the ruff gate, before the promised-tests gate,
   same revert/feedback shape.

Required imports (verified — copy, do not improvise):
- import_gate: `import ast` · `import difflib` · `import re` ·
  `from pathlib import Path`.
- dev_runner wiring: `from .import_gate import check_imports`.

## Files in scope

- `src/ferova/review/import_gate.py` (new)
- `tests/unit/test_import_gate.py` (new)
- `src/ferova/review/dev_runner.py` (wiring + brief parameter)

## Plan-shaping constraints

- Step 1 contracts ONLY the two NEW files.
- Step 2 contracts `dev_runner.py` (the single big file of its step)
  plus `tests/unit/test_import_gate.py` for its promised wiring tests.
- Two steps maximum. Every step action must be SELF-CONTAINED — copy
  the import lines and API names verbatim into the action text; never
  write "as specified in the spec".

## Out of scope

- Third-party/stdlib import validation (pytest catches those).
- Planner-side validation of "as specified in the spec" phrasing
  (the context fix makes the phrase harmless).
- The Coder loop's apply path (its fixes target existing files where
  pytest feedback suffices; revisit if the class appears there).

## Smoke scenario

### Setup

A tmp repo with `src/ferova/review/findings.py` containing
`class Finding: ...` and a candidate file importing
`from ferova.review.models import Finding`.

### Execute

`check_imports` on the candidate; read the report; fix the import to
`ferova.review.findings` and re-run.

### Expected

First run: `(False, report)` where the report names
`ferova.review.models` as missing, lists `findings` among the
package's modules, and says `Finding` lives in
`ferova.review.findings`. Second run: `(True, "")`.

## Definition of Done

- Missing module flagged with package listing + close matches —
  `test_missing_module_directive_report`.
- Missing name in an existing module flagged with its real home —
  `test_missing_name_located`.
- Clean file passes; non-ferova imports ignored —
  `test_clean_imports_pass`, `test_third_party_ignored`.
- Wiring: a step writing a hallucinated import is reverted with
  `import gate:` feedback (fake Developer on a tmp repo) —
  `test_step_reverted_on_bad_import`.
- Brief: `build_step_brief(..., spec_markdown="…")` appends the spec
  section; empty default appends nothing —
  `test_brief_carries_spec_section`, `test_brief_without_spec_unchanged`.
- `ruff` + `ruff format --check` + `pytest tests/unit` green; zero
  inline comments; no `# noqa`.

## Commit plan

1. `feat(dev): import gate — resolve ferova imports with directive feedback`
2. `feat(dev): step brief carries the spec verbatim + import gate wired`

## Risks

- **dev_runner.py re-emission (~860 lines)**: proven scale (821 lines
  passed twice); single-big-file rule applies; stall → autopsy.
- **Brief grows by up to 12k chars**: bounded, and context
  completeness is the documented dominant lever — the trade is the
  point.
