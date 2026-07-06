---
id: SP-CLAIM-TYPE-ROUTING
title: Content-based claim typing and fail-closed routing
version: 0.1
status: approved
author: jfaye + Claude (review-side evidence sweep, 2026-07-06)
created: 2026-07-06
updated: 2026-07-06

owns:
  code: []
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Content-based claim typing and fail-closed routing

## Intent

Route findings by what the claim SAYS, not by which bot spoke, and
make the routing fail closed. Today `claim_type` is a per-lens
default (`findings_bridge.py:17-21`): a Tester security claim becomes
`missing_test` and dies in symbol search as "no checkable symbol"; a
Sentinel style nit goes to the OPUS refuter as `security`;
`lint_convention` has a verifier but no producer (unreachable);
`spec_gap` is a dead enum value; and a blocking finding of any type
outside both verifier sets falls through BOTH branches of
`gather_merge_facts` (`merge_gate.py:200-207`) — it can never block a
merge.

## Context

`comment_to_finding` (`src/ferova/review/findings_bridge.py:76-108`)
stamps `claim_type` from `LENS_DEFAULT_CLAIM_TYPE`. The verifier
dispatch is `_MECHANICAL` (`finding_verifiers.py:97-101`:
missing_test / missing_docstring / lint_convention) plus
`JUDGED_CLAIM_TYPES` (`refuter.py:32`: design / security); unmapped
types stay PROPOSED forever, and PROPOSED never gates. Reviewer
persona templates live under `prompts/review/` and are
operator-owned — the classifier must therefore work from the comment
CONTENT in the bridge, not from new bot output fields. Live ledger
evidence: 31 of the last 33 findings sit in `proposed` with an empty
`verification_method`.

## Goals

- G1: A pure classifier `classify_claim_type(body: str, role: str) ->
  ClaimType` in the bridge: mechanical keyword/phrase cues assign
  missing_test (the existing trigger-phrase regexes), missing_docstring,
  lint_convention, broken_behavior and spec_gap from the comment text;
  when no cue fires, the lens default remains the fallback.
  `comment_to_finding` uses it.
- G2: `spec_gap` joins `JUDGED_CLAIM_TYPES` so every claim type has
  either a mechanical verifier or a judge — no enum value without a
  route.
- G3: `gather_merge_facts` fails closed: a BLOCKING finding whose
  claim type is in neither verifier set, or which sits PROPOSED at
  head, is counted in a new `blocking_unverified` fact that refuses
  the merge with a directive reason (today it silently never gates).

## Non-Goals

- NG1: No changes under `prompts/review/` (operator-owned; the
  classifier works from content).
- NG2: No change to the refuter's evidence window or cap.
- NG3: No re-classification of historical ledger rows.

## Assumptions

- A1: All touched files (`findings_bridge.py`, `refuter.py`,
  `merge_gate.py`, `finding_verifiers.py`) are frontier or their
  owners' declared edges already cover the imports used (verified for
  the anchor set 2026-07-06); this spec owns nothing.
- A2: Keyword cues may misclassify edge cases; the lens fallback plus
  the fail-closed gate fact bound the damage (a misrouted blocking
  claim surfaces at the gate instead of vanishing).

## Interface

Inputs:
- `classify_claim_type(body, role)` — pure, total (always returns a
  ClaimType).

Outputs:
- New merge-gate fact `blocking_unverified: list[str]` (finding ids
  with reasons), included in the gate's refusal reasons when
  non-empty.

Errors: none raised.

## Behavior

### Nominal

A Tester comment saying "this branch can drop the lock — race under
concurrent writes" classifies as broken_behavior (cue), not
missing_test (lens), and routes to the CI/pytest channel; a Scribe
comment citing a missing module docstring keeps missing_docstring.

### Edge cases

- No cue fires → lens default (today's behaviour).
- Multiple cues fire → a documented priority order (mechanical types
  before judged types) decides.
- Blocking finding PROPOSED at head → gate refuses, reason names the
  finding and its state.

### Failure scenarios

- Classifier exceptions are impossible by construction (pure string
  matching); a regression here is caught by the AC tests.

## Architecture Impact

- No edge added or removed; no ownership change.

## Diagram

N/A (one classifier + one gate fact).

## Acceptance Criteria

- [ ] AC1: `tests/unit/test_findings_bridge.py::test_content_cues_override_lens_default`
  — a security-worded Tester comment classifies as security; a
  test-worded Sentinel comment classifies as missing_test.
- [ ] AC2: `tests/unit/test_findings_bridge.py::test_no_cue_keeps_lens_default`
  — cue-free comments keep today's lens mapping.
- [ ] AC3: `tests/unit/test_review_refuter.py::test_spec_gap_is_judged`
  — a spec_gap finding reaches the refuter path.
- [ ] AC4: `tests/unit/test_merge_gate.py::test_blocking_unverified_refuses_merge`
  — a blocking PROPOSED finding at head yields merge=False with a
  reason naming it.
- [ ] AC5: The full unit suite passes.

## Open Questions

(none)
