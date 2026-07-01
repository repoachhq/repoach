# SP-CODER-FINDINGS (8a) — Coder resolves open findings, resolution re-verified at head

## Metadata

- **Status**: OPEN
- **Priority**: P1 — review redesign slice 8a of the replace-now
  sequence (8a→8b→10a→10b); `docs/review_redesign_architecture.md`
- **Owner**: operator
- **Executor**: hand-implemented (touches review-core + a new
  `prompts/review/` persona — both hand-ship)
- **Opened**: 2026-06-15

## Why

After the flip (slice 7b), the merge gate decides on **open blocking
findings re-verified at head** — but the Coder loop is still entirely
verdict/comment-driven (`run_coder_fix` triggers on
`verdict==REQUEST_CHANGES || CI red`, consumes reviewer comments
rebuilt from the archive, and never touches the findings ledger). Two
consequences:

1. **A gap the flip opened**: a PR can be archive-`APPROVE` yet carry a
   verified blocking finding the gate refuses — and the Coder no-ops
   because the verdict is APPROVE. Nothing fixes it.
2. **No resolution loop**: even when the Coder fixes something, the
   finding's lifecycle never advances to `resolved`, so the gate keeps
   re-verifying it from scratch every run.

This slice adds the **findings-driven fix path** (additive — the old
comment path stays inert until 8b debranches it): the Coder consumes
open blocking findings, fixes them, and the **same check that confirmed
each finding** re-verifies its resolution at the new head.

## What

New module `src/ferova/review/coder_findings.py`:

1. `fetch_open_blocking_findings(db_path, pr_number) -> list[Finding]`
   — blocking findings in status `verified` or `open` (not settled,
   not advisory).
2. `open_verified_blocking(db_path, pr_number, *, head_sha) -> int`
   — transition each `verified` blocking finding to `open` (the
   to-fix queue); returns how many moved.
3. `reverify_resolution_for_pr(db_path, *, pr_number, repo_root,
   head_sha) -> dict[str,int]` — for each `open` finding, re-run the
   SAME check (`verify_finding` for mechanical types, `refute_finding`
   for judged) at head. When the check **no longer confirms** the
   problem (mechanical: status≠verified; judged: refuted) →
   `open → resolved`. When it still confirms → stays `open`. Returns
   `{"resolved": n, "still_open": n}`.
4. `run_coder_fix_from_findings(pr_number, *, gh, repo_root, coder,
   db_path) -> CoderFindingsResult` — the additive entry:
   - base-branch guard (`develop` only);
   - fetch open blocking findings at head; no-op when none;
   - `open_verified_blocking` to mark the queue;
   - build the Coder fix-plan from the structured findings
     (`Coder.respond_to_findings`), apply via the existing
     `apply_fixes` (whitelist + placeholder guards reused), run the
     local ruff+pytest gate (reuse the existing gate helper), commit +
     push;
   - `reverify_resolution_for_pr` at the new head;
   - persist a row to L4 and return the result.

New persona `prompts/review/coder_findings_0.1.0.md` + a
`Coder.respond_to_findings(*, findings, diff, spec_plan)` method that
renders the findings (file, line, claim_type, severity, claim,
evidence_pointer) into the prompt and returns the same
`{"fixes", "commit_message", "summary"}` shape `apply_fixes` consumes.

CLI: `ferova review fix <N> --from-findings` routes to the new
entry (the default stays the legacy path until 8b).

## Files in scope

- `src/ferova/review/coder_findings.py` (new)
- `src/ferova/review/reviewer.py` (`respond_to_findings`)
- `prompts/review/coder_findings_0.1.0.md` (new, hand-ship)
- `src/ferova/cli/review_cmds.py` (`--from-findings` flag)
- `tests/unit/test_coder_findings.py` (new)

## Out of scope (later in the sequence)

- Debranching the legacy verdict trigger (8b).
- `stuck` escalation after N unresolved rounds (slice 9).
- Rendering the report from the ledger + rebranching `safe_merge`
  (10a); deleting the challenge/arbiter/pre-verify/consensus/verdict
  machinery (10b).

## Smoke scenario

- `fetch_open_blocking_findings`: returns verified+blocking and
  open+blocking; skips advisory, refuted, resolved, proposed.
- `open_verified_blocking`: a verified blocking finding → open; an
  advisory verified one untouched.
- `reverify_resolution_for_pr`: a mechanical `missing_test` finding
  whose test now exists re-checks to not-verified → `resolved`; one
  still missing stays `open`.
- `run_coder_fix_from_findings`: no open blocking findings → no-op; one
  open finding → Coder fix applied, gate green, pushed, finding
  resolved on re-verify.

## Definition of Done

- The new module's functions + the `respond_to_findings` path covered
  by `test_coder_findings.py`.
- The legacy `run_coder_fix` path is unchanged (additive); its tests
  stay green.
- `ruff` + `ruff format --check` + `pytest tests/unit` green; zero
  inline comments; no `# noqa`.

## Commit plan

1. `feat(review): coder_findings module — fetch/open/re-verify open findings`
2. `feat(review): Coder.respond_to_findings + coder_findings persona`
3. `feat(review): run_coder_fix_from_findings + CLI --from-findings`

## Risks

- **Re-verify semantics inversion**: the check confirms a *problem*;
  resolution = the check no longer confirms it. Easy to get backwards
  — pinned explicitly in tests for both mechanical and judged types.
- **Judged-resolution cost**: re-running the OPUS refuter at head per
  open judged finding burns proxy tokens; capped the same way
  `judge_findings_for_pr` caps (`_MAX_JUDGED`).
