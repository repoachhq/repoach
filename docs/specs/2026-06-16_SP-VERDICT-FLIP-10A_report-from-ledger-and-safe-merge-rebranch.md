# SP-VERDICT-FLIP (10a) — render the report from the ledger, rebranch safe_merge onto the pure gate

## Metadata

- **Status**: OPEN
- **Priority**: P1 — review redesign slice 10a of the replace-now
  sequence (8a→8b→**10a**→10b); `docs/review_redesign_architecture.md`
  slice 10 (SP-VERDICT-FLIP) split: 10a wires the pure gate everywhere
  and makes the archive report-only; 10b deletes the now-dead
  verdict/consensus/challenge/arbiter/pre-verify machinery.
- **Owner**: operator
- **Executor**: hand-implemented (touches review-core + `scripts/safe_merge.sh`
  — both hand-ship; no `prompts/review/` change).
- **Opened**: 2026-06-16

## Why

Slice 7 (#390) flipped **auto-merge** (CI `auto_merge` job →
`run_auto_merge` → `compute_merge_decision`) onto the pure
evidence-first gate, and the architecture declared the archive
"report-only". But two consumers still read the **forgeable archive
verdict**:

1. **`scripts/safe_merge.sh [5/6]`** — the operator's local merge tool
   still gates on "unanimous 4/4 APPROVE" parsed out of the sticky
   archive comment. That is exactly the self-reported judgment the
   redesign replaces (audit CRITICAL #1, forgeable merge gate). A PR
   the pure gate would refuse (open blocking finding at head) can still
   be hand-merged if 4/4 reviewers said APPROVE, and vice-versa.
2. **The archive comment itself** is still rendered from the team
   verdict (`final_verdict` + per-reviewer APPROVE/REQUEST_CHANGES),
   not from the ledger. It shows opinions, not the re-verified facts the
   gate actually decided on — so the human reads a different artefact
   than the one that governs the merge.

This slice closes both: it exposes the pure gate as a **read-only CLI
decision**, rebranches `safe_merge.sh` onto it, and renders the archive
as a **findings-and-facts report** built from the ledger. No machinery
is deleted yet (10b) — the verdict path stays computed but no longer
governs any merge.

## What

### 1. Read-only pure-gate CLI — `ferova review gate <N>`

New CLI command `ferova review gate <N>` (new `gate_pr` entry in
`review_cmds.py`) that:

- resolves head + required-CI-green the same way `run_auto_merge`
  does (reuse the existing fact-gathering, do **not** duplicate it);
- hydrates the ledger db (same `FEROVA_DB_PATH` contract as the CI
  `auto_merge` job);
- calls `gather_merge_facts` + `compute_merge_decision`;
- prints the decision as JSON (`{"pr_number", "head_sha", "merge",
  "reasons", "facts": {...}}`);
- exits `0` when `merge` is True, `5` when False, `1` on a transport
  error gathering facts.

It **never merges and never posts** — pure read. `safe_merge.sh` keeps
ownership of the actual `gh pr merge`. Factor the fact-gathering shared
with `run_auto_merge` into one helper so the gate decision is identical
whether reached by CI auto-merge or the local tool.

### 2. Report rendered from the ledger

New `render_ledger_report(db_path, *, pr_number, decision, facts) -> str`
(in a `review/report.py` module, or alongside the renderer that builds
the archive body today) that produces the sticky-comment markdown from
the **ledger**, not the verdict:

- a header line with the pure-gate decision (`MERGE-READY` /
  `BLOCKED — <reasons>`) at `head_sha`;
- the merge facts table (CI green, open blocking findings, spec
  coverage known/covered, review complete);
- findings grouped by `status` then `severity` / `claim_type`, each
  with file:line, claim, and evidence pointer;
- the per-lens / per-reviewer verdict block is retained **verbatim
  under a clearly-marked "(legacy verdict — informational only, not the
  merge authority)" section** until 10b removes it, so this slice does
  not regress observability while the verdict path is still computed.

The archive upsert (`upsert_archive_comment`) now writes this report
body. The verdict is still computed and recorded; it is simply no
longer the headline nor the gate.

### 3. Rebranch `safe_merge.sh [5/6]`

Replace the archive-verdict parse (the `n_approve == REQUIRED_APPROVALS`
check) with a call to `ferova review gate "$pr_number"`:

- exit `0` → gate satisfied, proceed to `[6/6]` merge;
- exit `5` → print the `reasons` and refuse the merge (loud, like the
  current non-APPROVE path);
- exit `1` / unreadable → retry with the existing backoff loop, then
  fail loudly (preserve the SP-SAFE-MERGE-ARCHIVE-RETRY robustness).

`--skip-review` keeps skipping the gate (unchanged operator escape).
The `REQUIRED_APPROVALS` knob and the archive-verdict parser become
dead in `safe_merge.sh` and are removed here (the shell tool is in
scope); their Python equivalents are 10b.

## Files in scope

- `src/ferova/cli/review_cmds.py` (`gate` command)
- `src/ferova/review/auto_merge.py` (extract shared fact-gathering
  helper consumed by both `run_auto_merge` and the new gate path)
- `src/ferova/review/report.py` (new) — `render_ledger_report`
- the archive-rendering call site (wherever `upsert_archive_comment`
  body is built today — `orchestrator.py` / `consensus.py`)
- `scripts/safe_merge.sh` ([5/6] rebranch)
- `tests/unit/test_merge_gate_cli.py` (new) + `test_report_render.py` (new)

## Out of scope (10b)

- Deleting `consensus.py`, `_aggregate_verdict`, the challenge / arbiter
  / pre-verify machinery, and the verdict-derivation in the finders.
- Removing the legacy verdict block from the report body.
- Deleting the parked shadow branch.

## Smoke scenario

- `review gate <N>` on a PR with one open mechanical blocking finding at
  head → JSON `merge=false`, reasons include "1 open blocking finding",
  exit 5.
- `review gate <N>` on a clean PR (CI green, no blocking findings, spec
  covered, review complete) → `merge=true`, exit 0.
- `render_ledger_report` on a ledger with mixed findings → markdown
  shows the decision header, the facts table, findings grouped by
  status, and the legacy verdict under the informational section.
- `safe_merge.sh` [5/6] refuses when `review gate` exits 5; proceeds
  when it exits 0; `--skip-review` bypasses it.

## Definition of Done

- The gate decision reached via `review gate` is byte-identical to the
  one `run_auto_merge` computes (shared helper, covered by a test that
  asserts both call the same fact-gatherer).
- `render_ledger_report` covered by `test_report_render.py`.
- `safe_merge.sh` rebranch covered by a shell-level test or an
  invocation test asserting exit-code routing.
- `ruff` + `ruff format --check` + `pytest tests/unit` green; zero
  inline comments; no `# noqa`.
- The legacy verdict path is still computed (10b deletes it); its tests
  stay green.

## Commit plan

1. `feat(review): extract shared merge-fact gathering + review gate CLI`
2. `feat(review): render the sticky archive report from the ledger`
3. `feat(review): rebranch safe_merge [5/6] onto the pure gate`

## Risks

- **Two fact-gatherers drifting**: the local gate and CI auto-merge
  must decide identically — enforced by extracting ONE helper and
  testing both reach it (no copy).
- **safe_merge FEROVA_DB_PATH**: the local gate must read the same ledger
  the local review run wrote; `safe_merge.sh` already runs
  `ferova review pr` locally first ([4/6]), so the ledger is fresh
  at head before `[5/6]` calls the gate.
- **Report regression**: keep the verdict block (informational) so the
  human does not lose per-reviewer context before 10b.
