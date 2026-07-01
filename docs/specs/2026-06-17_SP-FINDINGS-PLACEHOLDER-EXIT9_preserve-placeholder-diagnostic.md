# SP-FINDINGS-PLACEHOLDER-EXIT9 — the findings Coder preserves the placeholder diagnostic (exit 9)

## Metadata

- **Status**: OPEN
- **Priority**: P1 — trigger-flip prerequisite **P3 of 3** (the
  findings-driven Coder must keep the loud placeholder-garbage signal the
  legacy path emits; see `project_review_redesign.md` 2026-06-17
  trigger-flip parity)
- **Owner**: operator
- **Executor**: `ferova develop`
- **Opened**: 2026-06-17

## Why

When the legacy Coder (`run_coder_fix`) proposes only LLM placeholder
content ("# ... rest of file ...", massive shrinkage, a test file with no
`def test_`), `apply_fixes` rejects all of it, the runner **persists the
full plan** (`persist_placeholder_rejected` → `logs/coder_placeholder_
rejected_<pr>_<utc>.txt`) and exits **9** — a loud DIAGNOSED FAILURE the
operator must investigate, distinct from a benign "nothing to fix" (exit
4). The workflow keys on it: `auto-review.yml` maps exit 9 to an
`::error::` annotation and dumps the persisted log.

The findings-driven Coder (`run_coder_fix_from_findings`) already collects
the placeholder rejection list (`placeholder_rejections_out=rejections`)
but, when `applied == 0`, collapses **both** whitelist-only and
placeholder-only rejections into one generic no-op
(`no_op_reason="all proposed fixes rejected (whitelist or placeholder)"`),
which the CLI maps to the benign exit 4. It never calls
`persist_placeholder_rejected`. So after the trigger flip a Coder that
emits pure placeholder garbage becomes **invisible**: no `::error::`, no
persisted plan (the workflow's `logs/coder_placeholder_rejected_*.txt`
dump finds nothing), indistinguishable from a normal whitelist rejection.

Detection already survives the flip (`is_placeholder_content` runs inside
the shared `apply_fixes`), so the working tree is never corrupted. This
slice restores only the **escalation** — the operator-visible signal —
mirroring the legacy contract on the findings path.

## What

1. **`src/ferova/review/coder_findings.py`**:
   - `CoderFindingsResult` gains `placeholder_rejected: bool = False`
     (mirrors `coder_loop.CoderLoopResult.placeholder_rejected`).
   - Add `persist_placeholder_rejected` to the existing lazy
     `from .coder_loop import (...)` block inside
     `run_coder_fix_from_findings`.
   - In the `applied == 0` branch, mirror the legacy split at
     `coder_loop.py:2312-2338`: when the placeholder-rejection list
     (the one passed as `placeholder_rejections_out`) is **non-empty**,
     call `persist_placeholder_rejected(pr_number=pr_number, plan=plan,
     rejected=<that list>)`, emit
     `_log.warning("coder_findings.placeholder_rejected_no_fixes",
     pr_number=pr_number, n_rejections=..., rejected_paths=[...],
     persisted_path=...)`, and return `CoderFindingsResult(...,
     fixes_applied=0, fixes_rejected=len(rejected), rejected_paths=
     rejected, placeholder_rejected=True, no_op_reason=f"all
     {n} fix(es) rejected as placeholder content — see <file> for the
     full plan")`. When the placeholder list is empty (whitelist-only
     rejection) keep the current generic no-op + `placeholder_rejected`
     left False.
   - The existing local rejection-list variable may be renamed to
     `placeholder_rejections` for parity/readability; keep the second
     `apply_fixes` return (`rejected`, all-rejected paths) as-is.
2. **`src/ferova/cli/review_cmds.py`** — in the `--from-findings`
   branch (the `if from_findings:` block, ~209-233):
   - add `"placeholder_rejected": fr.placeholder_rejected` to the echoed
     JSON payload;
   - **before** the existing `if not fr.pushed: raise typer.Exit(code=4)`
     (and after the `base=` → exit-5 check), insert
     `if fr.placeholder_rejected: raise typer.Exit(code=9)`. Ordering
     matters: placeholder (9) must win over the generic not-pushed (4).
   - Extend the `review_fix` docstring's exit-code list to note that on
     the `--from-findings` path exit 8 (accept-inconsistent) is
     intentionally unreachable (`respond_to_findings` has no ACCEPT
     semantics) and exit 3 / the cross-run iteration cap are deferred to
     slice 9 (SP-STUCK-ESCALATION) — the pure merge gate is the interim
     backstop.
3. **No workflow change** — `auto-review.yml` already maps exit 9 to the
   loud `::error::` + dumps `logs/coder_placeholder_rejected_*.txt`
   (which `persist_placeholder_rejected` writes). Confirm, do not edit.

## Files in scope

- `src/ferova/review/coder_findings.py`
- `src/ferova/cli/review_cmds.py`
- `tests/unit/test_coder_findings.py`

## Plan-shaping constraints

- Step 1 contracts `coder_findings.py` (534 lines) + its tests in
  `tests/unit/test_coder_findings.py`. Use an anchored edit on the
  `applied == 0` branch and the `CoderFindingsResult` dataclass
  (SP-DEV-TARGETED-PATCH — do not re-emit the whole file).
- Step 2 contracts `src/ferova/cli/review_cmds.py` via an anchored
  edit on the `if from_findings:` block.
- Two steps maximum. No magic numbers in tests — assert on the
  `placeholder_rejected` flag / exit code / persisted-path existence,
  never on a hardcoded count.

## Out of scope

- Exit 3 / max-iteration cap / `stuck` escalation — slice 9.
- Re-adding any ACCEPT/challenge semantics to the findings path —
  intentionally subsumed by the refuter (exit 8 stays unreachable).
- Touching the legacy `run_coder_fix` placeholder path or
  `persist_placeholder_rejected` itself (a KEEPER — reused here).
- The off-diff filter (P1) and sentinel re-homing (P2) — separate specs.

## Smoke scenario

### Setup

A PR with one open blocking finding; a fake `Coder` whose
`respond_to_findings` returns a single fix whose content is a placeholder
("# ... rest of file ..."); a tmp `logs_dir`.

### Execute

`run_coder_fix_from_findings(pr_number, coder=<fake>, ...)`, then run the
CLI `review fix <N> --from-findings` against the same stubs.

### Expected

`apply_fixes` rejects the placeholder (applied == 0); the result has
`placeholder_rejected=True`; a `coder_placeholder_rejected_<pr>_*.txt`
file is written; the CLI exits **9**; a whitelist-only rejection (e.g. a
fix targeting `.github/`) instead leaves `placeholder_rejected=False` and
exits **4**.

## Definition of Done

- Placeholder-only rejection flags + persists + exits 9 —
  `test_placeholder_rejection_flags_and_persists`,
  `test_cli_from_findings_placeholder_exits_9`.
- Whitelist-only rejection stays benign (flag False, exit 4) —
  `test_whitelist_rejection_stays_exit_4`.
- The new field defaults False and a normal pushed run is unaffected
  (still exit 0) — existing `run_coder_fix_from_findings` tests stay
  green.
- `persist_placeholder_rejected` is invoked with the placeholder list,
  not the whitelist `rejected` list — `test_persist_gets_placeholder_list`.
- `ruff` + `ruff format --check` + `pytest tests/unit` green; zero
  inline comments; no `# noqa`.

## Commit plan

1. `feat(review): findings Coder persists + flags placeholder rejections`
2. `feat(review): review fix --from-findings exits 9 on placeholder garbage`

## Risks

- **Double-counting the rejection lists** — `placeholder_rejections_out`
  collects ONLY placeholder rejections; the second `apply_fixes` return
  is the union of all rejected paths. Persist + flag on the former;
  report counts from the latter, exactly as legacy does
  (`coder_loop.py:2312-2338`). A test pins that the persisted list is the
  placeholder one.
- **Exit-code ordering regression** — placeholder (9) must be checked
  before the generic not-pushed (4); a smoke test on a placeholder run
  asserts 9, not 4.
