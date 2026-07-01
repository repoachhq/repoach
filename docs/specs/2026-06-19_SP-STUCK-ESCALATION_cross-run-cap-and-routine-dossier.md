# SP-STUCK-ESCALATION — progress metric, stuck state, routine dossier

## Metadata

- **Status**: OPEN
- **Redesign slice**: 9 (the last behavioural slice of the evidence-first
  arc; slices 1–8 + 10a + 10b-1/2/3 are shipped — see
  `docs/review_redesign_architecture.md` line 187)
- **Priority**: P1 — closes the infinite-loop hole the legacy
  `MAX_ITERATIONS` cap left when SP-DELETE-LEGACY-CODER (#400) removed it
- **Owner**: operator
- **Executor**: `ferova develop` (dispatchable; see *Dispatch
  assessment*) — **no forbidden paths touched**
- **Opened**: 2026-06-19

## Why

The findings-driven Coder loop is driven by GitHub's push → CI →
re-review cycle, not by any in-process loop: each `review fix` that
pushes a commit triggers a fresh `auto-review.yml` run, which re-reviews
and re-runs the Coder. The legacy archive-verdict Coder bounded this with
`MAX_ITERATIONS` / `parse_iteration_counter` / `bump_iteration_marker`
(a PR-comment marker). SP-DELETE-LEGACY-CODER deleted that whole subtree
(`coder_loop.py` 2566 → 360) and explicitly deferred the replacement:
*"Slice 9 (stuck escalation) adds the cross-run iteration cap the legacy
`MAX_ITERATIONS` provided"* (that spec, line 66-67). SP-FINDINGS-
PLACEHOLDER-EXIT9 likewise parked *"Exit 3 / max-iteration cap / `stuck`
escalation — slice 9"* (line 104), and `review fix`'s docstring still
reads *"The cross-run iteration cap (the legacy exit 3) is deferred to
slice 9"* (`cli/review_cmds.py:191`).

Today, **nothing stops the loop**. A blocking finding the Coder cannot
resolve — one it has no power over, or one where its fix keeps failing
the ruff/pytest gate and reverts — re-triggers a Coder run on every sync
indefinitely (or, more commonly, the loop simply stalls silently with the
PR stuck at REQUEST_CHANGES and no loud signal that *the bots have given
up and a human is needed*). The pure merge gate is the only backstop: it
correctly refuses to merge, but it never escalates, so a genuinely-stuck
PR sits red forever with no operator-facing alarm distinct from the
ordinary per-round REQUEST_CHANGES ping.

The lifecycle already reserves the terminal state for exactly this:
`FindingStatus.STUCK` exists and `OPEN → STUCK` is a legal transition
(`findings.py:63,69`), and the module docstring promises the law
`open -> resolved/stuck`. **Nothing emits `STUCK` yet** — slice 9 is the
code that does, plus the cap that bounds the loop and the routine dossier
that hands the problem to the operator.

## What

Three coupled pieces: a **per-PR round ledger** (so progress is
measurable across separate CI runs), a **pure stuck assessment** (cap +
stall), and the **escalation** (mark `STUCK`, fire the routine dossier,
exit 3). Plus the **merge-gate correctness fix** that keeps a `STUCK`
finding blocking.

### 1. New module `src/ferova/review/stuck.py`

Pure logic + a small ledger table; **no network, no settings reads** (the
firing seam lives in the caller so the module stays unit-testable).

- **`pr_coder_rounds` table** (new, in this module's own `MetaData` or
  reuse `findings._metadata` — prefer a dedicated `MetaData` to avoid
  import coupling): columns `id` (pk), `pr_number` (int), `round_index`
  (int), `open_blocking_before` (int), `open_blocking_after` (int),
  `created_at` (DateTime, tz-aware). One row per Coder run that actually
  ran the Coder (not no-ops like base-wrong / nothing-to-fix).
- **`init_stuck_schema(db_path)`** — idempotent `create_all(checkfirst=True)`.
- **`record_coder_round(db_path, *, pr_number, open_blocking_before,
  open_blocking_after) -> int`** — append a row; `round_index` =
  `count(prior rows for pr) + 1`; `created_at = datetime.now(UTC)`.
- **`fetch_coder_rounds(db_path, pr_number) -> list[CoderRound]`** —
  ordered by id; `CoderRound` is a frozen dataclass / pydantic model
  with `round_index`, `open_blocking_before`, `open_blocking_after`.
- **`MAX_CODER_ROUNDS = 3`** and **`STALL_WINDOW = 2`** — module-level
  named constants (docstring: 3 = legacy `MAX_ITERATIONS` parity; 2 =
  escalate one round before the hard cap when no progress is being made).
  Tests import these, never hardcode (CLAUDE.md no-magic-numbers).
- **`assess_stuck(rounds, *, max_rounds=MAX_CODER_ROUNDS,
  stall_window=STALL_WINDOW) -> StuckAssessment`** — **pure** function
  over the prior-round history. `StuckAssessment(stuck: bool, reason:
  str, n_rounds: int)` with `reason ∈ {"", "iteration cap reached
  (<n>/<max>)", "no progress over last <w> rounds"}`. Logic:
  - `n = len(rounds)`; if `n == 0` → not stuck.
  - **cap**: `n >= max_rounds` → stuck, reason `cap`.
  - **stall**: `n >= stall_window` AND the last `stall_window`
    `open_blocking_after` values are non-decreasing AND the most recent
    is `> 0` → stuck, reason `stall`. (Strictly-decreasing = progress =
    not stuck; the cap still bounds slow progress.)
- **`mark_findings_stuck(db_path, pr_number) -> int`** — transition every
  **open** blocking finding (`status == OPEN`, `severity == BLOCKING`) to
  `STUCK` via `update_finding_status(..., STUCK, verification_result=
  "auto-resolution exhausted — escalated")`; return the count.
  `OPEN → STUCK` is already legal; `VERIFIED → STUCK` is **not**, so this
  targets `OPEN` only (verified-but-not-yet-opened findings stay blocking
  on their own merit and will be re-opened next round).
- **`build_stuck_dossier(*, pr_number, reason, rounds, stuck_findings)
  -> dict`** — the routine payload. Shape: `{"kind":
  "stuck_escalation", "pr_number": …, "reason": …, "n_rounds": …,
  "trajectory": [{"round": i, "before": …, "after": …}, …],
  "stuck_findings": [{"finder": …, "claim_type": …, "file": …, "line":
  …, "claim": …}, …]}`. Small (well under the notifier's 60 KB cap); the
  `kind` discriminator distinguishes it from the per-round TeamOutcome
  ping `orchestrator._fire_routine` already sends.

### 2. `src/ferova/review/coder_findings.py`

- `CoderFindingsResult` gains **`stuck: bool = False`** (after
  `placeholder_rejected`, line 326).
- Add **`stuck_findings_out` is not needed** — fire inside the runner.
- **Start-of-run cap check** — after the base-`develop` guard succeeds
  (the check at line ~402) and after the open blocking findings are
  fetched: only when that set is **non-empty** (there is real work left
  to escalate), load `fetch_coder_rounds(db, pr_number)` and call
  `assess_stuck(...)`. If the open set is empty, fall through to the
  ordinary nothing-to-fix no-op (exit 4) — the cap must not fire a dossier
  on a PR that has nothing left to fix (e.g. the operator just pushed the
  resolving commit). If `stuck`:
  - `mark_findings_stuck(db, pr_number)`;
  - build the dossier from the open/stuck findings + rounds and fire it
    via the settings-guarded seam in §4 (no-op without routine creds);
  - emit `_log.warning("coder_findings.stuck_escalation", pr_number=…,
    reason=…, n_rounds=…, n_stuck=…)`;
  - `return CoderFindingsResult(pr_number=pr_number, stuck=True,
    no_op_reason=f"stuck — {reason}; escalated (no fix attempted)")`
    (the Coder does **not** run, so no commit → the push/CI loop stops).
- **End-of-run record** — on the successful-push return path (line
  552+), before constructing the result, call `record_coder_round(db,
  pr_number=pr_number, open_blocking_before=len(findings),
  open_blocking_after=counts["still_open"])`. Record **only** on the
  push path (a reverted ruff/pytest no-op didn't change head, so it isn't
  a round). `len(findings)` is the before-count; `still_open` the after.
- Update the function docstring (lines 366-368) to state the cap + stuck
  escalation are now **implemented** here (not deferred).

### 3. `src/ferova/review/merge_gate.py` — STUCK is terminal-blocking (correctness)

**The pivotal correctness change.** `STUCK` must keep a finding blocking
the merge; otherwise marking a real *judged* (DESIGN/SECURITY) finding
`STUCK` would make it invisible to the gate (`gather_merge_facts` /
`summarise_ledger_facts` only count judged findings when `status is
VERIFIED`), silently unblocking the merge — the opposite of escalation.

Treat `STUCK` exactly like `VERIFIED` everywhere the gate inspects status:

- `gather_merge_facts` (line ~181): judged branch `fresh = finding.status
  is FindingStatus.VERIFIED and …` → `finding.status in
  {VERIFIED, STUCK} and …`. (The mechanical branch already re-verifies
  status-agnostically for any non-settled finding, so `STUCK` mechanical
  findings already re-verify correctly — confirm, no edit.)
- `summarise_ledger_facts` (line ~237): `if finding.status is not
  FindingStatus.VERIFIED: continue` → `if finding.status not in
  {VERIFIED, STUCK}: continue`.
- `_SETTLED` stays `{RESOLVED, REFUTED}` — `STUCK` is **not** settled, so
  the loop/gate keeps treating it as a live blocker.
- Escape hatch (do **not** special-case): a `STUCK` finding still clears
  the gate the normal way — a mechanical one re-verifies green at a new
  head once the operator fixes it; a judged one drops out once the head
  moves (its stored `checked_at_sha` goes stale) and is re-judged fresh.
  Because the gate is pure re-verification, the stored `STUCK` row is a
  hint, never a hard block — so no `ALLOWED_TRANSITIONS` change is needed
  and `STUCK` stays terminal.

`verdict_from_facts` needs no edit — it already keys on
`open_blocking_findings > 0`, which now includes STUCK.

### 4. The escalation firing seam

`run_coder_fix_from_findings` fires the dossier through a settings-guarded
helper mirroring `orchestrator._fire_routine` (lines 772-827): read
`get_settings().claude_code_routine_id` + `…_token`; if either missing →
skip (return without firing); else `notifier.fire_review_routine(
routine_id=…, token=…, payload=dossier)`. To keep it testable, pass the
notifier as an injected default param on `run_coder_fix_from_findings`
(`routine_fire=fire_review_routine`) so a unit test can assert the
dossier shape without network, and the creds-absent path (unit-test
default) simply no-ops. Place the helper in `coder_findings.py` (it needs
settings + gh-free); do **not** put settings/network in `stuck.py`.

### 5. `src/ferova/cli/review_cmds.py`

- Add `"stuck": fr.stuck` to the echoed JSON payload (after
  `placeholder_rejected`, line 207).
- Insert the exit-3 mapping **before** the generic not-pushed exit 4
  (line 218) and after the placeholder exit 9 (line 217):
  `if fr.stuck: raise typer.Exit(code=3)`. Final order: 5 (base) → 9
  (placeholder) → **3 (stuck)** → 4 (not pushed) → 0.
- Rewrite the docstring's deferral note (lines 191-192) into a live
  exit-3 entry: *"`3` — SP-STUCK-ESCALATION: the cross-run iteration cap
  (<MAX_CODER_ROUNDS> rounds) or a no-progress stall was hit; surviving
  open blocking findings were marked `stuck`, a routine dossier fired,
  and no fix was attempted (the loop stops here for human intervention)."*

### 6. `.github/workflows/auto-review.yml` — **out of scope (interim)**

No workflow edit. Exit 3 already falls through the existing dispatch
(`auto-review.yml` ~line 460: not 0/4/5/9 → `exit "$rc"`), failing the
`auto_fix` job loudly and stopping the loop (no push → no new sync). The
routine dossier carries the human-facing signal. A dedicated
`::error::Coder is stuck …` annotation is a **forbidden-path** edit
(`.github/workflows/*`) and is left as an optional hand-shipped follow-up,
keeping this slice dispatchable.

## Files in scope

- `src/ferova/review/stuck.py` (new)
- `src/ferova/review/coder_findings.py`
- `src/ferova/review/merge_gate.py`
- `src/ferova/cli/review_cmds.py`
- `tests/unit/test_stuck.py` (new)
- `tests/unit/test_coder_findings.py` (extend)
- `tests/unit/test_merge_gate.py` (extend — STUCK-blocks coverage)

## Dispatch assessment

Two new files (`stuck.py`, `test_stuck.py`) + four edited; estimated
~350–420 LOC. Under the ~500-LOC / ≥3-new-file autonomous ceiling, so
**dispatchable via `ferova develop`** — but the §3 merge-gate change
is correctness-critical and subtle. Recommendation: dispatch, then
review the `merge_gate.py` diff and the STUCK-blocks tests by hand before
the gate passes. (Operator may prefer to hand-implement §3 and dispatch
the rest.)

## Plan-shaping constraints

- `coder_findings.py` is 562 lines — use **anchored edits**
  (SP-DEV-TARGETED-PATCH): the `CoderFindingsResult` dataclass, the
  start-of-run block after the base guard, and the success-push return.
  Never re-emit the whole file.
- `merge_gate.py` edits are two one-line status-set widenings — anchored.
- No magic numbers in tests: import `MAX_CODER_ROUNDS` / `STALL_WINDOW`
  from `stuck`; assert on `assess_stuck` return fields, the `STUCK`
  finding status, the recorded round rows, and exit codes — never a bare
  `3` / `2`.
- Zero inline comments, zero `# noqa` (SP-NO-INLINE-COMMENTS-GATE);
  no silent except (SP-LINT-LOG-CATCH-ALL) — the firing helper logs a
  named warning on routine-fire failure (the notifier already does).

## Out of scope

- Any `.github/workflows/auto-review.yml` edit (§6) — interim relies on
  exit-3 fall-through.
- Settings-driven overrides of the cap / stall window — module constants
  for now; promote to `FEROVA_REVIEW_*` settings only if the operator asks.
- Re-opening / un-sticking a `STUCK` finding via a new transition — the
  pure gate's re-verification is the escape hatch; no lifecycle change.
- The persona / 10b-4 chantiers — separate.

## Smoke scenario

### Setup
A PR (base `develop`) carrying open blocking findings. A fake `Coder`,
an injected `GhCli` mock, a tmp `db_path`, and **no** routine creds in
the environment (so the fire no-ops).

### Execute
1. Seed `pr_coder_rounds` with `MAX_CODER_ROUNDS` prior rows (e.g. via
   `record_coder_round`), then call `run_coder_fix_from_findings(...)`.
2. Separately: seed two prior rounds with non-decreasing `after` (e.g.
   `after=2, after=2`) and call again on a fresh PR (stall path).
3. Run the CLI `review fix <N>` against the same stubs.

### Expected
- Cap path: the Coder is **not** invoked; every open blocking finding is
  now `STUCK`; the result has `stuck=True`,
  `no_op_reason` starts `"stuck —"`; a dossier with
  `kind == "stuck_escalation"` was handed to the injected `routine_fire`.
- Stall path: same, with `reason` naming the stall.
- Below cap with progress (strictly-decreasing afters): **not** stuck,
  the Coder runs normally, and on a successful push a new
  `pr_coder_rounds` row is recorded.
- CLI exits **3** on the stuck run; **0** on a normal pushed run; **4**
  on nothing-to-fix (unchanged).
- Merge gate: a PR whose only blocker is a `STUCK` finding has
  `open_blocking_findings > 0` and `compute_merge_decision().merge is
  False` — for both a mechanical and a judged STUCK finding.

## Definition of Done

- `assess_stuck` cap + stall + progress branches —
  `test_assess_stuck_cap`, `test_assess_stuck_stall`,
  `test_assess_stuck_progress_not_stuck`, `test_assess_stuck_empty`.
- Round ledger round-trips — `test_record_and_fetch_coder_rounds`,
  `test_round_index_increments`.
- `mark_findings_stuck` transitions only OPEN blocking findings —
  `test_mark_findings_stuck_targets_open_blocking`.
- Cap/stall short-circuits the runner, marks STUCK, fires the dossier,
  sets `stuck=True`, runs no Coder —
  `test_run_coder_fix_stuck_escalates_without_fixing`.
- A normal pushed run records exactly one round —
  `test_run_coder_fix_records_round_on_push`.
- CLI exits 3 on stuck — `test_cli_from_findings_stuck_exits_3`; normal
  paths (0/4) unaffected.
- **STUCK keeps the merge blocked** (mechanical **and** judged) —
  `test_merge_gate_stuck_finding_blocks`,
  `test_summarise_ledger_counts_stuck`.
- Existing `test_coder_findings` / `test_merge_gate` / placeholder-exit-9
  tests stay green.
- `ruff` + `ruff format --check` + `pytest tests/unit` + no-inline-comments
  + no-silent-except all green.

## Commit plan

1. `feat(review): per-PR Coder round ledger + pure stuck assessment (stuck.py)`
2. `feat(review): findings Coder caps rounds, marks stuck, fires escalation dossier`
3. `fix(review): merge gate treats STUCK findings as terminal-blocking`
4. `feat(review): review fix --from-findings exits 3 on stuck escalation`

## Risks

- **STUCK silently unblocking the gate** — the §3 change is the whole
  point; a test pins that a lone STUCK blocker (mechanical *and* judged)
  yields `merge=False`. Without it, escalation would *open* the merge.
- **Recording rounds on no-op runs** — only the successful-push path
  records; a reverted ruff/pytest run leaves head unchanged and must not
  count toward the cap (else a flapping gate burns the budget without
  ever attempting a real fix). A test pins no round is recorded on the
  revert path.
- **Cap off-by-one** — `MAX_CODER_ROUNDS = 3` must allow three *attempts*
  (runs 1-3) and refuse the fourth: the start-check reads **prior** rounds
  (`n >= max_rounds`), and rounds are recorded at run-end. A test seeds
  exactly `max_rounds` rows and asserts the next call escalates.
- **Double-fire** — escalation fires on the no-fix/no-push path, so no
  new sync is triggered and the dossier fires once per stuck PR. The
  non-empty-open-set guard means that once the operator's fix clears the
  findings, a later run finds nothing open and stays a quiet exit-4 no-op
  instead of re-firing the dossier. `test_run_coder_fix_stuck_skips_when_no_open`
  pins this.
</content>
</invoke>
