# SP-REFUTER — adversarial judging of design / security findings

## Metadata

- **Status**: OPEN
- **Priority**: P1 — review redesign slice 5 of 11
  (docs/review_redesign_architecture.md)
- **Owner**: operator
- **Executor**: hand-implemented (touches `prompts/review/*` — bot
  whitelist forbids it; force-majeure)
- **Opened**: 2026-06-14

## Why

The mechanical verifiers (slice 4) cannot check `design` and
`security` claims on disk, so they leave them `proposed`. These two
lenses (Architect, Sentinel) are also where the bench hallucinates
most. The refuter judges them: an independent agent, on a heavier
chain than the SONNET finders (OPUS), prompted to **refute** the claim
over the cited code evidence — refute-by-default. Refutation succeeds
→ `refuted` (false positive); refutation fails → `verified` (real
finding). Still dual-run: statuses recorded, no merge decision
changes. This fills the verified/refuted split for the two highest-
hallucination lenses — the per-lens precision signal slice 7 gates on
and slice 11 learns from.

## What

1. **New module `src/ferova/review/refuter.py`**:
   - `JUDGED_CLAIM_TYPES = {DESIGN, SECURITY}`; `_MAX_JUDGED = 10`
     cap; `_EVIDENCE_CONTEXT = 25` lines each side.
   - `make_refuter_judge() -> Judge` — builds an OPUS one-shot
     (`AgentLoop(capability=OPUS)`, `run_oneshot(json_response=True)`);
     built lazily so a review with no judged findings never spins it
     up.
   - `_evidence_excerpt`, `_render_prompt` (persona substitution),
     `_parse_verdict` (tolerant `{"refuted": bool, "reasoning": str}`).
   - `refute_finding(finding, *, repo_root, judge) ->
     (FindingStatus, reasoning)` — REFUTED / VERIFIED / PROPOSED
     (unreadable evidence, unparseable verdict, or a judge exception
     all leave it proposed).
   - `judge_findings_for_pr(db_path, *, pr_number, repo_root,
     head_sha, judge_factory=make_refuter_judge) -> dict[str,int]` —
     fetch proposed, filter to judged types, judge (lazy judge),
     persist transitions, return `{verified, refuted, deferred}`.
2. **`prompts/review/refuter_0.1.0.md`** (hand-ship) — adversarial
   persona, refute-by-default, strict JSON verdict.
3. **Orchestrator wiring** — after `verify_findings_for_pr`, a
   try/except-guarded `judge_findings_for_pr` call emitting
   `review_team.findings_judged`; a refuter failure never breaks the
   review.
4. **`tests/unit/conftest.py`** — autouse stub of the orchestrator's
   `judge_findings_for_pr` so no unit test reaches the live OPUS loop;
   refuter-internal tests inject a fake judge.

## Files in scope

- `src/ferova/review/refuter.py` (new)
- `prompts/review/refuter_0.1.0.md` (new)
- `src/ferova/review/orchestrator.py` (wiring)
- `tests/unit/conftest.py`
- `tests/unit/test_refuter.py` (new)
- `tests/unit/test_review_team.py` (resilience test)

## Out of scope

- Multi-judge panels / per-lens diversity (one refuter per finding for
  now; expand if precision data warrants).
- Any verdict/consensus/merge change (dual-run; the gate flip is
  slice 7).
- Judging the mechanical claim types (slice 4 owns those).

## Smoke scenario

`judge_findings_for_pr` on a tmp ledger with a design + a security +
a missing_test finding, an injected judge returning
`{"refuted": true}`: the design and security flip to `refuted`, the
missing_test stays `proposed`, counts `{verified:0, refuted:2,
deferred:0}`, the judge built exactly once.

## Definition of Done

- Verdict parser: extracts the bool, rejects bad shapes —
  `test_parse_verdict_*`.
- refute_finding: refuted / verified / proposed (missing evidence,
  unparseable, judge raises) — `test_refute_finding_*`.
- judge_findings_for_pr: transitions + counts + lazy single judge;
  no targets → no judge built — `test_judge_findings_*`.
- Orchestrator resilience: a refuter crash never breaks the review —
  `test_judge_failure_never_breaks_review`.
- `ruff` + `ruff format --check` + `pytest tests/unit` green; zero
  inline comments; no `# noqa`.

## Commit plan

1. `feat(review): adversarial refuter for design/security findings`
2. `feat(prompts): refuter 0.1.0 persona`
3. `feat(review): orchestrator judges findings after mechanical verification`
4. `test(review): refuter logic + orchestrator resilience + hermetic stub`

## Risks

- **OPUS chain volatility**: a flaky judge call leaves the finding
  `proposed` (graceful) for a later round — dual-run, nothing breaks.
- **Cost**: up to `_MAX_JUDGED` OPUS calls per PR; bounded and only on
  design/security findings, which are few.
