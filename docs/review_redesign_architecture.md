# Review Redesign — Evidence-First Architecture

- **Status**: direction agreed by the operator on 2026-06-11. This
  document is the target architecture and the slice plan; each slice
  ships as its own spec through the factory.
- **Scope**: the REVIEW phase only (reviewers, consensus, verdict loop,
  auto-merge). The BUILD phase (Planner / Developer / spec→PR) is kept
  as-is, judged solid. Diagram: `review_redesign_architecture.svg`.

## Why

Almost every review-phase defect found by the 2026-06-11 audit — and
every patch shipped since the factory went live — is a variant of one
structural flaw: **the system asks LLMs for a self-reported judgment
(a verdict) and then trusts it.** parse_failed promoted to APPROVE,
the forgeable archive gate, TRANSPORT hidden inside a 4/4, Scribe
hallucinating missing docstrings, convergent false blockers on
truncated diffs, the Architect COMMENT loop, nit-only promotion — each
got its own guard, filter or sentinel *around* the verdict. The
verdict is the wrong primitive.

Internal proof: the system itself never really uses the verdict — the
effective decision is always re-derived from `n_blockers`/`n_majors`,
and the only signal never doubted in two months of operation is the
green CI. That is the thread to pull.

## Principles (agreed)

1. **The finding replaces the verdict as the atomic unit.** Reviewers
   become *finders*: they emit findings, never verdicts. A finding
   without an evidence pointer is stillborn.
2. **The merge is a pure function owned by the harness.** No LLM in
   the decision. Inputs are machine-checkable facts at the exact head
   SHA.
3. **Spec acceptance criteria are executed, not judged.** The action
   plan's `done_when` commands and the spec's smoke scenario run as a
   coverage gate.
4. **Stored state is a hint — verification at head is the truth.**
   The findings ledger seeds *what to check*; the gate re-runs the
   checks on the exact head it is about to merge. Forgeable or stale
   state cannot influence the decision by construction.
5. **Never review a truncated diff.** The scoper slices the diff into
   complete units; a finder either sees a whole slice or does not
   review it (which is recorded as a coverage hole, not an approval).

## The Finding

Pydantic model + `pr_findings` SQLite table.

```
Finding
├─ id, pr_number, head_sha, round, finder (lens)
├─ claim_type        (taxonomy below)
├─ severity          blocking | advisory
├─ file, line_range
├─ claim             (one falsifiable sentence)
├─ evidence_pointer  (what to check: path, symbol, command, excerpt)
├─ status            proposed → verified | refuted
│                    verified+blocking → open → resolved | stuck
└─ verification      (method, result, checked_at_sha)
```

### Claim taxonomy → verification method

| claim_type        | verifier                                            |
|-------------------|-----------------------------------------------------|
| missing_test      | mechanical — pytest collection / symbol grep        |
| missing_docstring | mechanical — AST read of the cited symbol           |
| lint_convention   | mechanical — run the relevant gate                  |
| broken_behavior   | mechanical — execute the cited repro / test         |
| spec_gap          | mechanical first (done_when run), judge on residue  |
| design, security  | judged — adversarial refuter over fetched evidence  |

Mechanical verifiers are promoted from today's hallucination guard
(file reader, symbol searcher). Judged claims go to a **refuter**: an
independent agent on a *different* chain than the finder, prompted to
refute over evidence excerpts it cannot fabricate, refute-by-default.

## Pipeline

```
PR event
  → SCOPE    diff → complete slices (no truncation, ever)
  → FIND     lenses (design / security / tests / docs) per slice
             → findings with evidence pointers (schema-forced)
  → VERIFY   mechanical verifiers + adversarial refuter
             → findings ledger (lifecycle persisted)
  → GATE     pure function at head SHA:
               CI green @ head
               ∧ zero open blocking findings (re-verified at head)
               ∧ spec coverage (done_when + smoke executed green)
               ∧ slice coverage ≥ threshold
  → SHIP     auto-merge → develop
  ∥ FIX      Coder consumes open findings (with evidence), fixes,
             pushes; the SAME verifier re-runs on the new head →
             resolved or still open
  ∥ STUCK    open blocking findings not strictly decreasing between
             rounds → loud stop: routine notification with the full
             dossier; the PR waits for the operator
```

Coverage is a first-class fact: finder × slice cells are recorded. An
absent finder (transport failure, crash) is a coverage hole that can
block the gate — it can never count as approval.

## Retired / Promoted

| Retired                                   | Promoted                                       |
|-------------------------------------------|------------------------------------------------|
| verdict enum + 4/4 consensus              | hallucination guard → mechanical verifier lib  |
| nit-only auto-promote                     | evidence-challenge → refuter prompt            |
| parse_failed / TRANSPORT promote paths    | round-2 dialogue → contested-finding judging   |
| archive comment as merge gate             | truncation-announce → scoper                   |
| Coder chasing raw reviewer comments       | prior-dialogue ratchet → finding dedup/rounds  |

The two HELD specs (SP-REVIEW-PARSE-FAILED-GATE,
SP-MERGE-ARCHIVE-INTEGRITY) are absorbed: their intent is closed by
construction in slices 7 and 10. The parked branch
`feat/sp-review-parse-failed-gate-impl` dies with the flip.

## Learning loop — the builder sees the review

The ledger is the first review artefact safe to learn from: raw
reviewer comments are polluted by hallucinations, but a *verified*
finding is ground truth. Three feedback altitudes, all reading the
same table:

1. **Per build (short loop)** — at cycle end (merged or stuck), the
   PR's verified findings are distilled into builder-scoped
   agentmemory lessons keyed by claim_type ("this build produced 3
   missing_test findings on new modules"). The Planner already recalls
   lessons before planning — the next plan includes the tests upfront.
   The Developer stops repeating the same class of mistake.
2. **Aggregate (long loop)** — SQL over `pr_findings` is the
   improvement map: recurring claim_types (Developer weaknesses),
   finding-dense modules (debt hot spots), rounds-to-ship distribution
   (process health). Surfaced via a CLI report.
3. **Meta (bench quality)** — verified/refuted ratio *per lens*
   measures each reviewer's precision: which prompt hallucinates,
   which chain judges well. The system observes its own reviewers.

Only verified findings feed learning — refuted ones feed the meta
view, never the lessons.

The memory is two-scoped (`project=builder` and `project=review`,
same agentmemory service):

- **builder scope** — what the Planner/Developer should do better
  (verified findings distilled into build lessons).
- **review scope** — what the bench itself should know: curated trap
  lessons (verify-before-flag, no truncation extrapolation, …) plus,
  post-flip, refuted-finding patterns so the bench stops repeating
  its own false positives. Recalled once per review run and appended
  to every lens prompt; the refuter recalls it for calibration.

The review-scope *recall* + curated seeds ship early on the current
bench (SP-REVIEW-MEMORY, 2026-06-11) — the plumbing carries into the
redesigned bench unchanged. The automatic *remember* stays gated on
the ledger (slice 11): learning from unverified reviewer comments
would teach the bench its own hallucinations.

## Slice plan

Each slice is one spec → one factory run → one PR, sized under the
~500 LOC autonomous-Developer ceiling. The old pipeline keeps running
until slice 7 flips the gate; slices 3–6 dual-run (findings recorded
alongside verdicts, decision unchanged) so every stage is observable
on real PRs before it owns anything.

1. **SP-FINDING-MODEL** — Finding model, `pr_findings` table,
   lifecycle, CRUD. Pure addition.
2. **SP-DIFF-SCOPER** — slice the diff into complete units; reviewers
   consume slices. Kills truncation for the *current* bench too.
3. **SP-FINDER-OUTPUT** — finders emit schema-forced findings;
   persisted; verdict still derived for compatibility (dual-run).
4. **SP-VERIFIER-LIB** — mechanical verifiers per claim_type
   (hallucination guard promoted); statuses update on real PRs.
5. **SP-REFUTER** — adversarial judged verification for design /
   security claims on an independent chain.
6. **SP-SPEC-GATE** — execute plan `done_when` + spec smoke as a
   recorded coverage fact.
7. **SP-PURE-MERGE-GATE** — the flip: auto-merge decides on re-verified
   facts at head; archive becomes report-only. Closes audit CRITICALs
   1 and 2 by construction.
8. **SP-CODER-FINDINGS** — Coder consumes open findings; resolution
   re-verified by the same check that confirmed the finding.
9. **SP-STUCK-ESCALATION** — progress metric, stuck state, routine
   notification dossier.
10. **SP-VERDICT-FLIP** — remove verdict / consensus / promote paths
    and the archive-gate machinery; render the report from the ledger;
    delete the parked branch.
11. **SP-REVIEW-LESSONS** — the learning loop: distill verified
    findings into builder-scoped agentmemory lessons at cycle end +
    a CLI insights report (aggregate + per-lens precision). Can land
    as soon as dual-run data exists (after slice 4), independent of
    the flip.

## Open questions (to settle slice by slice)

- **Cross-run continuity**: rounds are separate CI runs; the ledger
  must survive between them (principle 4 makes this safe — storage is
  a seed, the gate re-verifies at head). Candidate: structured block
  in a PR comment as transport, SQLite as local cache.
- **Coverage threshold**: which finder × slice coverage blocks the
  gate vs. only warns.
- **Refuter diversity**: how many lenses per judged claim, and which
  chains, given NIM volatility.
