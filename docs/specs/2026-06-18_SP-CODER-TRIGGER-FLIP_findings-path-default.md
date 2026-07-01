# SP-CODER-TRIGGER-FLIP — flip the CI Coder onto the findings path

**Status:** implemented (hand-shipped)
**Redesign slice:** 10b — the trigger-flip (after prereqs P1 #394, P3
#395, P2 #396 all merged)
**Touches forbidden paths:** yes (`.github/workflows/auto-review.yml`) —
hand-shipped via PR + factory, never bot-edited

## Why

Slices 8a/8b built the evidence-first Coder (`run_coder_fix_from_findings`):
it resolves the PR's open blocking findings from the ledger, materialises
CI failures as `broken_behavior` findings, and re-verifies resolution at
head by the same check that confirmed each finding. But CI never invoked
it — `auto-review.yml` still ran the legacy `run_coder_fix` (archive
verdict / reviewer-comment driven), and the CLI `--from-findings` flag
defaulted off. This flip retires the legacy trigger so everything flows
through the ledger.

The three capability gaps that blocked the flip are now closed:

- **P1 #394** — off-diff reviewer comments never become findings (the
  Coder can't edit a file the PR never touched).
- **P3 #395** — the findings path preserves the placeholder exit-9
  diagnostic.
- **P2 #396** — the evidence sentinel is re-homed onto the findings path,
  so cross-run round-2 anti-anchoring context survives (`refuted` reviewer
  findings post `"Verified — challenge with evidence"`).

## Change

Two edits, exit-code-compatible with the unchanged workflow handler:

1. `.github/workflows/auto-review.yml` — the `Run Coder auto-fix` step
   invokes `review fix <N> --from-findings`.
2. `src/ferova/cli/review_cmds.py` — `review fix` defaults
   `--from-findings` on; `--no-from-findings` selects the legacy path
   (retired in 10b).

### Exit-code compatibility

The findings path emits **0** (pushed → in-run re-review takes over),
**4** (no-op), **5** (base ≠ develop), **9** (placeholder rejected). The
workflow's handler maps 0 → `pushed=true`; 3/4/5 → benign exit 0; 8 →
loud error; 9 → loud error; else → propagate. The findings path never
returns 3 (max-iter, deferred to slice 9) or 8 (accept-inconsistent,
unreachable — `respond_to_findings` has no ACCEPT semantics), so every
code it does emit is handled. The pure merge gate (10a) is the interim
backstop for the absent cross-run iteration cap.

## Acceptance

- `tests/unit/test_coder_findings.py` (the `review fix --from-findings`
  command path) green; `tests/unit/test_review_coder_loop.py` (legacy
  `run_coder_fix`, still present until 10b) green.
- `ruff` + format clean; no inline comments; ShellCheck green.

## Follow-on (slice 10b)

Delete `run_coder_fix` + the arbiter / challenge / ACCEPT-consistency
sub-trees + the dead exit-3/8 workflow branches; re-source
`final_verdict` off the ledger then remove the consensus machinery.
`coder_loop._format_evidence_reply` / `pre_verify_review_comments` become
dead with `run_coder_fix` and are removed there.
