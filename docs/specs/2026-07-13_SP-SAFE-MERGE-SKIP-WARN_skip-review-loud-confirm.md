---
id: SP-SAFE-MERGE-SKIP-WARN
title: Make safe_merge --skip-review warn loudly and require explicit confirmation
version: 0.1
status: draft
author: jfaye (Ferova audit 2026-07-13)
created: 2026-07-13
updated: 2026-07-13

owns:
  code: []
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Make safe_merge --skip-review warn loudly and require explicit confirmation

## Intent

`safe_merge.sh --skip-review` silently skips BOTH the review-bot run
and the pure evidence-first merge gate, then merges — and with
`--skip-tests` this merges into develop with no review and no gate,
showing NO override prompt, because the gate block is skipped rather
than refused. Turn the silent bypass into a loud, explicitly-confirmed
one.

## Context

Audit 2026-07-13 finding M12.

- `scripts/safe_merge.sh:140-145` (step 4): `--skip-review` prints one
  quiet parenthetical (`(skipped via --skip-review — verdict check also
  skipped)`) and skips `ferova review pr`.
- `scripts/safe_merge.sh:147-202` (step 5, the pure merge gate): the
  whole `ferova review gate` block is wrapped in `if [[ "$skip_review"
  == "yes" ]]` and short-circuited with `(skipped — --skip-review
  implies the user accepts no automated gate)`. The refusal path that
  normally prompts for an emergency override (`safe_merge.sh:170-186`)
  is inside the skipped block, so with `--skip-review` NO prompt is
  shown at all.
- Combined with `--skip-tests` (lint-only CI, `safe_merge.sh:134-138`),
  a `--skip-review` run reaches the merge (step 7) with no review, no
  evidence gate, and no interactive gate — a silent, unattended bypass
  of every merge safety.

This is an operator SAFETY shell script. `scripts/` is bot-writable,
but a change to the merge safety tool warrants human review:
Execution is OPERATOR-MANUAL (hand-implement + human review, audit
2026-07-13 — merge-path change).

## Goals

- G1: `--skip-review` prints a LOUD, in-band warning that names exactly
  what is bypassed: the review-bot run (step 4) AND the pure evidence
  gate (step 5), and — when combined with `--skip-tests` — that CI is
  lint-only too.
- G2: after the warning, the script REQUIRES an explicit confirmation
  before proceeding — either an interactive `read` confirmation
  (matching the existing emergency-override prompt style at
  `safe_merge.sh:178-183`) or an explicit opt-in flag (e.g.
  `--i-understand-skip-review`). Absent the confirmation, the script
  HALTS with a non-zero exit and does not merge.
- G3: no silent bypass path remains — the quiet parentheticals at
  `safe_merge.sh:142,149` are replaced by the warning + confirmation.

## Non-Goals

- NG1: no change to `--skip-tests` semantics beyond surfacing it in the
  combined warning.
- NG2: no change to the pure-gate logic itself (`ferova review gate`)
  or the fresh-head guard (step 6).
- NG3: no removal of `--skip-review` — it stays available for the
  operator who genuinely needs it, just no longer silent.

## Assumptions

- A1: the script is run interactively by the operator in the normal
  case; a non-interactive/CI invocation is exactly the case that must
  NOT proceed on a silent `--skip-review`, so requiring an explicit
  `--i-understand`-style flag (or a TTY confirmation) is the correct
  gate.
- A2: `set -e` is active; a non-zero exit on unconfirmed bypass halts
  the script before any `gh pr merge`.

## Interface

`scripts/safe_merge.sh`:
- At the point `--skip-review` is detected (before step 4), emit a
  multi-line `fail`/`bold`-styled warning enumerating the bypassed
  gates, then require confirmation:
  - if a `--i-understand-skip-review` flag was passed, proceed;
  - else `read -r confirm` and require an exact sentinel (e.g. typing
    `skip-review`) — anything else prints `Aborted.` and `exit 1`.
- No change to the non-skip path.

## Behavior

### Nominal

- No `--skip-review` → unchanged: full review + gate run.

### Edge cases

- `--skip-review` alone, operator types the sentinel → warning shown,
  confirmation accepted, proceeds (review + gate bypassed, as the
  operator explicitly chose).
- `--skip-review --skip-tests` → the warning additionally states CI is
  lint-only; same confirmation requirement.
- `--skip-review --i-understand-skip-review` → warning shown, no
  interactive prompt, proceeds.

### Failure scenarios

- `--skip-review` with no confirmation (empty input, wrong sentinel,
  or non-interactive with no opt-in flag) → fail CLOSED: warning
  printed, `Aborted.`, `exit 1`, no merge.

## Architecture Impact

- Adds/Removes dependency: none — in-place modification of an operator
  shell script; no Python module ownership touched, no cross-owner
  import.
- New / changed coupling, cycles, or shared state: none.

## Diagram

N/A (in-place shell-script fix).

## Acceptance Criteria

- [ ] AC1: a shell-level test harness invokes `scripts/safe_merge.sh`
  with `--skip-review` and asserts the bypass warning text (naming
  step 4 and step 5) appears on stdout/stderr.
- [ ] AC2 (INTEGRATION): drive the real script — invoke
  `safe_merge.sh --skip-review` with the confirmation prompt fed empty
  / a wrong sentinel (e.g. `printf '' | ...` or `echo no | ...`) in a
  harness that stubs only the true external boundaries (`gh`, `ferova`,
  `git` on PATH as no-op fakes) and assert the process EXITS NON-ZERO
  and never reaches the `gh pr merge` fake — observing the halt, not a
  unit of a helper. A second case feeds the correct sentinel and
  asserts it proceeds past the warning.
- [ ] AC3: promised test —
  `tests/unit/test_safe_merge_skip_review_warns.py` (or a `bats`/shell
  harness at `tests/shell/test_safe_merge_skip_review_warns.sh`)
  covering `::test_skip_review_warns_and_halts_without_confirm` and
  `::test_skip_review_proceeds_with_sentinel`.
- [ ] AC4: `shellcheck scripts/safe_merge.sh` clean; the script's own
  gates (`.githooks/pre-commit` shellcheck) pass; no inline comments
  added that trip SP-NO-INLINE-COMMENTS-GATE (shell comments are
  outside that gate but keep the change tidy).

## Open Questions

- OQ1: implement by hand + human review before re-trusting the merge
  tool (audit 2026-07-13 — merge-path safety change).
