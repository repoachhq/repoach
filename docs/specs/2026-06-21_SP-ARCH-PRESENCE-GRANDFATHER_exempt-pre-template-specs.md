---
id: SP-ARCH-PRESENCE-GRANDFATHER
title: Grandfather pre-template specs in the presence gate
version: 0.1
status: approved
author: agent
created: 2026-06-21
updated: 2026-06-21

owns:
  code: N/A                           # refines edge_honesty.py (owned by SP-ARCH-EDGE-GATE)
  resources: N/A

depends_on: []                        # adds no import edge

constraints: {}
---

# SP-ARCH-PRESENCE-GRANDFATHER — exempt pre-template specs

## Intent
Stop the spec-presence gate (`SP-ARCH-SPEC-PRESENCE`) from flagging
grandfathered legacy specs when the diff base predates the template. The
"added-only" check correctly fires per-PR (base = `develop`), but against
a base older than the template's landing (e.g. a `develop → main` PR,
23 commits back) every pre-template freeform spec shows as "added" and is
wrongly flagged. The discriminator is time: the governance template landed
2026-06-20; specs dated before it are legitimately frontier.

## Context
Pure refinement of `edge_honesty.gather_added_specs`. Spec filenames follow
`<YYYY-MM-DD>_<SP-ID>_<slug>.md`, so an ISO date prefix sorts lexically. A
spec whose date prefix is strictly before `2026-06-20` is grandfathered —
skipped by the presence check. A filename without a valid date prefix is
NOT grandfathered (a new spec must carry the convention), so the guard
never weakens enforcement on genuinely-new specs.

## Goals
- G1: `_TEMPLATE_ERA = "2026-06-21"` — the first day a spec must be
  governed (the day AFTER the template landed; freeform specs were still
  authored on the 2026-06-20 transition day, so the whole day is
  grandfathered — governed specs dated 2026-06-20 carry frontmatter and
  are never flagged regardless).
- G2: `gather_added_specs` skips an added `docs/specs/*.md` whose filename
  has a valid `YYYY-MM-DD` prefix strictly less than `_TEMPLATE_ERA`.
- G3: A spec dated on/after the era, or with no parseable date prefix, is
  still checked (enforcement on new specs is unchanged).
- G4: `ferova arch check --base origin/main` no longer flags the
  pre-template legacy specs (exit 0 for a clean `develop → main`).

## Non-Goals
- NG1: Does NOT change the import/table edge checks.
- NG2: Does NOT grandfather a malformed-named new spec — only a clear
  pre-era date prefix exempts.

## Assumptions
- A1: Legacy specs follow the `<date>_<SP-ID>_<slug>.md` convention (date
  prefix present), confirmed across the existing corpus.

## Interface
- `gather_added_specs` gains an internal date-prefix grandfather filter;
  signature unchanged.

## Behavior

### Nominal
For each added `docs/specs/*.md`: if its name's first 10 chars parse as an
ISO date and that date `< "2026-06-20"`, skip it (grandfathered); else keep
it for the frontmatter-presence check.

### Edge cases
- name with no/invalid date prefix ⇒ NOT grandfathered (still checked).
- a date `2026-06-20` (transition day) ⇒ grandfathered; a governed spec
  that day carries frontmatter and is not flagged anyway.
- a date `2026-06-21` or later ⇒ checked (governance mandatory).

### Failure scenarios
- none new — a narrower filter on an existing check.

## Architecture Impact
- Refines `SP-ARCH-EDGE-GATE`'s `edge_honesty.py` in place; no new import,
  no new edge.
- New coupling / cycles / shared state: none.

## Diagram
N/A — a one-predicate filter inside an existing gather.

## Acceptance Criteria
- [ ] AC1: an ADDED fence-less spec dated `2026-06-18` or `2026-06-20` is
  NOT flagged (grandfathered through the transition day).
- [ ] AC2: an ADDED fence-less spec dated `2026-06-21` or later IS flagged.
- [ ] AC3: an ADDED fence-less spec with no date prefix IS flagged.
- [ ] AC4: full `pytest tests/unit` green; ruff + format + no-inline +
  no-silent clean; `ferova arch check` passes (incl. vs `origin/main`).

## Open Questions
- None. (Resolved: era is `2026-06-21` — the transition day 2026-06-20 is
  grandfathered since freeform specs were authored that day; no-date-prefix
  is not grandfathered; pure filter, no signature change.)
