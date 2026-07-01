# SP-FINDER-OUTPUT — dual-run: every reviewer comment becomes a persisted Finding

## Metadata

- **Status**: OPEN
- **Priority**: P1 — review redesign slice 3 of 11
  (docs/review_redesign_architecture.md)
- **Owner**: operator
- **Executor**: `ferova develop`
- **Opened**: 2026-06-12

## Why

The `pr_findings` ledger (slice 1) is empty. This slice starts filling
it on every real review run — **dual-run**: findings are recorded
alongside the verdict flow and change NOTHING in any decision. The
ledger data is what slices 4-7 verify and gate on, and what slice 11
distills into lessons; weeks of real-PR findings before the flip is
the whole point of dual-running.

No prompt change and no new model output contract: the reviewers'
parsed comments already carry the finding's substance (file, line,
severity, body, lens). The bridge derives findings from them — the
claim taxonomy refinement is verifier work (slices 4-5), so each lens
maps to a provisional default claim_type here.

## What

1. **New module `src/ferova/review/findings_bridge.py`** — pure
   derivation + one persistence helper:
   - `LENS_DEFAULT_CLAIM_TYPE: dict[BotRole, ClaimType]` —
     `ARCHITECT → ClaimType.DESIGN`, `SENTINEL → ClaimType.SECURITY`,
     `TESTER → ClaimType.MISSING_TEST`,
     `SCRIBE → ClaimType.MISSING_DOCSTRING` (provisional, documented
     as such; refined by verifiers in slices 4-5). Use the enum
     members as they exist in `findings.py` — read that file first.
   - `SEVERITY_MAP: dict[str, Severity]` — `"blocker"` and `"major"`
     → `Severity.BLOCKING`; `"minor"` and `"nit"` →
     `Severity.ADVISORY`; unknown strings → `Severity.ADVISORY`.
   - `comment_to_finding(comment: ReviewComment, *, role: BotRole,
     pr_number: int, head_sha: str, round_n: int) -> Finding` —
     `claim` = the comment body (first 500 chars), `evidence_pointer`
     = `f"{comment.file}:{comment.line} — {comment.body[:200]}"`,
     `line_start = line_end = comment.line`, status left at its
     default (`proposed`).
   - `_is_unparsed(outcome: ReviewerOutcome) -> bool` — module-private
     predicate (post-mortem amendment: the public
     `is_unparsed_outcome` lives only on a parked branch — the import
     gate killed round 2 on that phantom anchor): returns `True` when
     `(outcome.summary or "").startswith("[parse_failed:")` or
     `(outcome.summary or "").startswith("_(bot crashed:")`.
   - `record_findings_for_outcomes(db_path: Path, *, pr_number: int,
     head_sha: str | None, outcomes: list[ReviewerOutcome],
     round_n: int) -> int` — calls `init_findings_schema` once, skips
     any outcome where `_is_unparsed(outcome)` is true
     (transport/crash garbage must never seed the ledger), records
     every comment of the surviving outcomes via `record_finding`,
     returns the recorded count. `head_sha=None` becomes `""`.
2. **Wiring in `src/ferova/review/orchestrator.py`** — in
   `review_pr`, right before the `TeamOutcome` construction (the
   `n_blockers`/`n_majors` aggregation area around line 380), call
   `record_findings_for_outcomes(self._db_path, pr_number=pr_number,
   head_sha=head_sha, outcomes=outcomes, round_n=2 if a round-2 ran
   else 1)` — simplest correct round value: `2` when the round-2
   path replaced any outcome, else `1`; if that bookkeeping is not
   already at hand, pass `1` and note it (the ledger's `round` field
   is informational until slice 9). Wrap in `try/except Exception`
   with a `_log.warning("review_team.findings_record_failed", ...)`
   — dual-run must NEVER break the verdict flow. On success emit
   `_log.info("review_team.findings_recorded", pr_number=pr_number,
   n_findings=n)`.

Required imports (each one grep-verified against develop this time —
copy, do not improvise; the bridge needs NOTHING from consensus.py):
- bridge: `from .findings import (ClaimType, Finding, Severity,
  init_findings_schema, record_finding)` ·
  `from .reviewer import BotRole, ReviewComment, ReviewerOutcome` ·
  `from pathlib import Path`.
- orchestrator: `from .findings_bridge import
  record_findings_for_outcomes`.

## Files in scope

- `src/ferova/review/findings_bridge.py` (new)
- `tests/unit/test_findings_bridge.py` (new)
- `src/ferova/review/orchestrator.py` (wiring only)

## Plan-shaping constraints

- Step 1 contracts ONLY the two NEW files.
- Step 2 contracts `orchestrator.py` (1 235 lines — the single big
  file of its step, nothing else big) plus
  `tests/unit/test_findings_bridge.py` for its promised wiring tests.
- Two steps maximum. No size thresholds anywhere — no test may
  hardcode a magic size (test-arithmetic law).

## Out of scope

- Reading findings anywhere (gate, report, Coder) — later slices.
- Any prompt or persona change; any verdict/consensus change.
- claim_type refinement beyond the lens defaults.
- The `pr_coder_responses.fixes_json` 32k clip (separate hygiene).

## Smoke scenario

### Setup

A tmp db path; two synthetic outcomes — Architect with one blocker
comment and one nit comment, Scribe with
`summary="[parse_failed:TRANSPORT] …"` and zero comments.

### Execute

`record_findings_for_outcomes` on both, then `fetch_findings` from
`findings.py`.

### Expected

Exactly 2 findings, both `finder="architect"`, claim_type `design`,
severities `blocking` and `advisory`, status `proposed`; nothing from
the Scribe garbage; the function returns 2.

## Definition of Done

- Lens defaults + severity map pinned —
  `test_lens_default_claim_types`, `test_severity_mapping`.
- Comment round-trip via the real ledger on a tmp db —
  `test_record_findings_round_trip`.
- Unparsed outcomes are skipped — `test_unparsed_outcomes_skipped`.
- `head_sha=None` stored as empty string — `test_none_head_sha_empty`.
- Wiring: a stubbed orchestrator run records findings and emits
  `review_team.findings_recorded`; a bridge that raises does NOT
  break `review_pr` (warning emitted, TeamOutcome still returned) —
  `test_orchestrator_records_findings`,
  `test_findings_failure_never_breaks_review`.
- `ruff` + `ruff format --check` + `pytest tests/unit` green; zero
  inline comments; no `# noqa`.

## Commit plan

1. `feat(review): findings bridge — comments become proposed findings (dual-run)`
2. `feat(review): orchestrator records findings on every review run`

## Risks

- **orchestrator.py full-file re-emission (1 235 lines)** in step 2:
  single-big-file-per-step rule applies; on an output-truncation
  stall, root-cause protocol — autopsy before anything else.
- **Coarse claim_types pollute the ledger**: acceptable and explicit —
  `proposed` status means unverified; the verifier slices own the
  refinement, and slice 11 only learns from verified findings.
