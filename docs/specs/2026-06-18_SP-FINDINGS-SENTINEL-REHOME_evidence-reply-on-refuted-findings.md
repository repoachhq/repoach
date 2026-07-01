# SP-FINDINGS-SENTINEL-REHOME — re-home the evidence sentinel onto the findings path

**Status:** implemented (hand-shipped)
**Redesign slice:** 10b prerequisite **P2** (after P1 #394 off-diff filter,
P3 #395 placeholder exit-9)
**Touches forbidden paths:** no (`prompts/review/*` and `.github/workflows/*`
untouched — reader side is already wired)

## Why

The evidence-first review loop carries an anti-convergent-hallucination
guard (the #229 fix): when the system disproves a reviewer comment, the
Coder bot posts a `"Verified — challenge with evidence"` reply on that
inline thread, and the **next** review run reads it back via
`thread_context.fetch_resolved_disagreements` and injects it into the
reviewer prompt so the bot must bring fresh evidence to re-raise the same
critique instead of re-asserting it.

Today that sentinel is written **only** by the legacy path
(`coder_loop._format_evidence_reply` → `pre_verify_review_comments` →
`run_coder_fix`). The 10b trigger-flip retires `run_coder_fix`. Once CI
runs `review fix --from-findings` (`run_coder_fix_from_findings`), nothing
posts the sentinel — `coder_findings.py` makes only read-only `gh` calls
and drives resolution through the SQLite ledger. The in-run re-review
(`auto-review.yml` runs `review pr` on the pushed head in the same run)
means a missing sentinel degrades the very next round immediately, so the
writer must be re-homed onto the findings path **before** the flip.

## Decision (operator-confirmed)

The Coder no longer gets an author-privileged challenge: under the
findings path the **refuter** (adversarial OPUS, refute-by-default,
independent chain) is the sole arbiter of whether a reviewer-origin
design/security finding is real. The legacy `coder_challenge_pass`
defense dies with the flip (10b). This matches the agreed evidence-first
thesis (no LLM author-privilege in the decision) and #394 already kills
the dominant false-block class (findings on untouched files). If live
data later shows refuter false-positives, a lightweight challenge can be
added — additively.

## Mechanism

The orchestrator's **initial** verify/refute pass
(`verify_findings_for_pr` + `judge_findings_for_pr`) transitions a
disproved finding `proposed → refuted`. `refuted` is **terminal** in the
finding lifecycle (`ALLOWED_TRANSITIONS[REFUTED] == {}`), and a
fix-resolved finding settles at `resolved` (via `open → resolved`), never
`refuted`. So **a finding stored `refuted` is exactly the initial-refute
"not real / already-satisfied" case** — the faithful analogue of the
legacy pre-verify short-circuit, and never a "reviewer was right, now
fixed" case (which would make the "challenge with evidence" message
wrong).

`thread_context.post_refuted_finding_sentinels(gh, db_path, pr_number)`:

1. Fetch findings with status `refuted`; keep reviewer-origin ones
   (`finder != "ci"`, with a real `file` and `line_start > 0`).
   CI-materialised findings (`finder == "ci"`, `file == "(ci):<name>"`)
   have no reviewer thread and are skipped — and when there are no
   reviewer-origin refuted findings the function returns before any `gh`
   call.
2. List the PR's inline comments once; partition into roots / replies.
3. For each finding, re-locate its root thread by `(path, line)` equality
   with a `similarity(root_body, finding.claim)` tiebreak (a `Finding`
   carries no GitHub comment id — the source `ReviewComment` never had
   one).
4. Skip threads that already carry the sentinel (idempotent across
   re-runs and the accumulating refuted ledger).
5. Post a `"Verified — challenge with evidence"` reply built from the
   finding's claim type + the verifier/refuter reasoning. A failed `gh`
   reply is logged and skipped, never raised.

Wired into `ReviewTeamOrchestrator.review_pr` right after the
verify/judge passes, guarded by `try/except` like its siblings so a `gh`
outage never breaks a review.

## Acceptance

- `tests/unit/test_findings_sentinel_rehome.py`:
  - reply body embeds the sentinel verbatim + reason + method;
  - thread re-located by `(path, line)`; similarity breaks ties; `None`
    on no match;
  - sentinel posted on a refuted reviewer finding's thread;
  - idempotent (existing-sentinel thread untouched);
  - CI-origin findings and "no refuted findings" skip the `gh` call;
  - unmatched thread skipped; `gh` failure swallowed;
  - posted sentinel round-trips back through
    `fetch_resolved_disagreements`.
- Full `tests/unit` green; `ruff` + format clean; no inline comments.

## Follow-on (not in this PR)

The **flip** (hand-ship — `auto-review.yml:438` add `--from-findings`,
`review_cmds.py` default `True`) then the **delete** (retire
`run_coder_fix` + the arbiter / challenge / ACCEPT-consistency sub-trees,
10b). After the flip, `coder_loop._format_evidence_reply` /
`pre_verify_review_comments` become the dead legacy writer and are removed
with `run_coder_fix`.
