# SP-RETIRE-VERDICT-ARCHIVE — retire the legacy verdict-persistence layer (10b-4)

**Status:** OPEN
**Redesign slice:** 10b-4 (the optional schema cleanup deferred by
SP-DELETE-LEGACY-CODER; the last loose end of the evidence-first arc).
**Touches forbidden paths:** no — hand-shipped as part of finishing the arc.

## Why

Since the pure merge gate (SP-PURE-MERGE-GATE / SP-VERDICT-FLIP 10a) the
merge decision is a pure function over the findings ledger re-verified at
head; the self-reported archive verdict no longer gates anything. A thin
legacy verdict-persistence layer survived only as an audit echo:

- `pr_merges.verdict` (`NOT NULL`) is **written but never read** — no
  `select` touches it; the merge decision ignores it.
- `auto_merge.parse_archive_verdict` parses `final_verdict` out of the
  sticky archive solely to feed that column (informational).
- `OUTCOME_SKIP_NOT_APPROVED` is **never emitted** post-flip (the pure
  gate refuses with `OUTCOME_SKIP_GATE`); it lingers as an import + an
  unreachable CLI exit-5 branch.
- `report.LEGACY_VERDICT_HEADER` + the `**Verdict:**` echo line frame the
  archive appendix as a "legacy verdict — informational only" section.

The machine-readable `TeamOutcome` JSON in the archive comment is **kept**
— `ferova review report` consumes it, and the routine fire uses the
in-memory `TeamOutcome`. Only the dead *verdict* framing/persistence goes.

## Change

`persistence.py`:
- Drop `Column("verdict", ...)` from `_pr_merges`.
- Remove the `verdict` param + insert value from `record_merge`.
- Add `_drop_retired_columns(engine)` (called from `init_schema` after the
  add-migration): `ALTER TABLE pr_merges DROP COLUMN verdict` when present
  (SQLite 3.35+, idempotent via `has_column`). **Required for correctness**
  — a pre-existing DB keeps the `NOT NULL` column, so an insert that omits
  it would fail; the drop heals the operator's persistent local DB.
- Update the module docstring's `pr_merges.outcome` list (drop the dead
  `SKIP_NOT_APPROVED`, add the real `SKIP_GATE` / `SKIP_CI_*`).

`auto_merge.py`:
- Delete `parse_archive_verdict` and the `OUTCOME_SKIP_NOT_APPROVED`
  constant.
- In `run_auto_merge`: drop the `fetch_archive_comment` + parse, the
  `_persist(verdict=...)` param, and the `verdict` log field.
- Rewrite the module docstring's gate list (step 3 is the CI gate, step 4
  the pure merge gate — not the archive verdict).

`report.py`:
- Delete `LEGACY_VERDICT_HEADER`; rename `render_ledger_report`'s
  `legacy_verdict_block` param to `archive_appendix` and append it under a
  plain `---` separator (no legacy header). Update the module docstring.

`orchestrator.py`:
- Drop the `**Verdict:** …` echo line from the archive appendix; rename
  `legacy_body` → `archive_appendix`; keep the guard section + JSON
  `<details>` + transcript. Update `_render_report_body` accordingly.

`cli/review_cmds.py`:
- Remove the `OUTCOME_SKIP_NOT_APPROVED` import + its unreachable exit-5
  branch; correct the `review merge` docstring (pure gate, not verdict).

## Tests

- `test_review_merge_persistence.py` (new): a legacy `pr_merges` with a
  `NOT NULL` verdict column is healed by `init_schema` (column dropped),
  `record_merge` then inserts cleanly, and a fresh DB is idempotent.
- `test_review_auto_merge.py`: delete the two `parse_archive_verdict`
  tests + helper/`verdict` scaffolding; the outcome-persistence + gate
  tests stay green (they never asserted the verdict column).
- `test_report_render.py`: rename the two legacy-block tests to
  `archive_appendix` semantics (appended when present, absent when empty).

## Acceptance

- `grep -rn 'parse_archive_verdict|OUTCOME_SKIP_NOT_APPROVED|
  LEGACY_VERDICT_HEADER|legacy_verdict_block' src` → empty.
- A pre-existing DB with the `NOT NULL` verdict column survives
  `init_schema` + `record_merge` (the migration test).
- `ferova review report` still finds the archive JSON (the appendix
  keeps the `<details>` JSON block).
- Full `tests/unit` green; ruff + format + no-inline-comments +
  no-silent-except clean.

## Out of scope

- Removing the per-reviewer `verdict` enum from `ReviewerOutcome` /
  `TeamOutcome.final_verdict` — still live schema (the JSON archive +
  routine + ledger-sourced team verdict use it).
</content>
