---
id: SP-REFUTER-INJECTION-HARDEN
title: Harden the refuter against PR-content injection and make REFUTED re-openable
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

# Harden the refuter against PR-content injection and make REFUTED re-openable

## Intent

Stop PR-head content from steering the refuter into dismissing a real
blocking finding. The evidence window is substituted raw into a
fenced block the PR author controls, the verdict parser trusts the
first JSON blob, and REFUTED is terminal and self-defended — so a
single injected payload can permanently bury a blocking security
finding. Neutralise fence-breakout, stop trusting injected JSON, and
make REFUTED re-openable by a later reviewer re-raise.

## Context

`refute_finding` (`src/ferova/review/refuter.py:104-123`) builds the
judge prompt via `_render_prompt` (`refuter.py:74-82`), which
substitutes `{EVIDENCE}` — the PR-head file window from
`_evidence_excerpt` (`refuter.py:58-71`) — into a template. The
evidence is delivered inside a ``` fence; PR-authored code can emit a
closing fence plus instructions, escaping the data channel into the
instruction channel. `_parse_verdict` (`refuter.py:85-101`) accepts
the FIRST `{...}` match (`re.search(r"\{.*\}", raw, re.DOTALL)`), so
injected text that precedes an attacker-chosen `{"refuted": true}`
wins. The refuter's persona default leans "refuted", compounding the
bias.

REFUTED is TERMINAL: `findings.py` (status transitions, ~line 71)
defines no transition out of REFUTED. Worse,
`post_refuted_finding_sentinels`
(`src/ferova/review/thread_context.py:510-594`) then posts the
"Verified — challenge with evidence" sentinel that actively defends
the dismissal on the next run. So an injected REFUTED verdict on a
blocking finding is permanent and self-reinforcing.

Audit 2026-07-13 finding C3 (CRITICAL). Execution: hand-implement
with human review (audit 2026-07-13) — merge-path change.

## Goals

- G1: the evidence excerpt cannot break out of its data channel — a
  fence sequence (or any delimiter the template relies on) inside the
  PR-head window cannot terminate the block and inject instructions.
- G2: the verdict parser does not trust the first JSON blob sitting
  under attacker-controlled prose — it recovers the judge's verdict
  from a structured, injection-resistant position (e.g. the LAST
  well-formed object, or a delimited verdict section the evidence
  cannot forge).
- G3: REFUTED is RE-OPENABLE — a subsequent reviewer that re-raises
  the same finding can transition it out of REFUTED (or a new
  equivalent finding is admitted), so injection cannot permanently
  bury a blocking finding.

## Non-Goals

- NG1: no change to the refuter's LLM chain / proxy wiring.
- NG2: no attempt to "detect prompt injection" heuristically — the
  fix is structural (channel separation + parser hardening +
  re-openability), not a classifier.
- NG3: no change to how mechanical findings verify.

## Assumptions

- A1: the evidence window is untrusted input (PR-head content). Any
  hardening must treat it as adversarial, not merely malformed.
- A2: reviewers re-run each round; a genuine blocking finding will be
  re-raised by an honest reviewer if it was wrongly refuted, so a
  re-open path restores the safety property.

## Interface

N/A (in-place fix — `refute_finding` / `_parse_verdict` /
`_render_prompt` keep their signatures; the persona template file
under `prompts/review/` is out of the Coder whitelist and edited by
hand as part of this merge-path change). Adds one status transition
into the findings state machine (RE-OPEN from REFUTED), which is a
data-model change, not a public-API change.

## Behavior

### Nominal

The evidence window is escaped / neutralised (fence sequences
rendered inert, or delivered through a non-fenced structured channel
the template controls) before substitution. The judge reply is parsed
from a position the evidence cannot forge; an evidence-embedded
`{"refuted": true}` is ignored. A genuine judge REFUTED verdict on a
non-blocking or truly-absent finding still settles it.

### Edge cases

- Evidence contains a fence-break + `{"refuted": true, ...}` payload
  → the injected verdict is NOT the parsed verdict; the finding is
  NOT moved to REFUTED on that basis.
- A finding previously REFUTED is re-raised by a later reviewer → it
  re-opens (transition admitted) rather than being silently
  suppressed by the terminal state + defending sentinel.

### Failure scenarios

- Unparseable / ambiguous judge reply → PROPOSED (unchanged — leave
  for a later round), NEVER defaulted to REFUTED. Fail CLOSED toward
  keeping the finding alive.
- Injection attempt on a blocking SECURITY finding → the finding does
  NOT reach terminal REFUTED; it remains actionable/blocking. Fail
  CLOSED.

## Architecture Impact

- Adds/Removes dependency: none — in-place modification of
  `refuter.py` and the findings status-transition table
  (`findings.py`), both owned by existing specs; no new cross-owner
  import.
- New / changed coupling, cycles, or shared state: a new REFUTED →
  (re-open) transition in the findings state machine; document it in
  the findings module docstring alongside the existing transitions.

## Diagram

N/A (in-place fix)

## Acceptance Criteria

- [ ] AC1: unit — `_parse_verdict` given a reply where an
  attacker-chosen `{"refuted": true}` precedes the judge's real
  object does NOT return the injected verdict; the escaping helper
  renders a fence-break sequence inert.
- [ ] AC2 (INTEGRATION): run `refute_finding` over a real blocking
  SECURITY `Finding` whose `_evidence_excerpt` window (a tmp file on
  disk) contains a fence-break + `{"refuted": true}` payload, with a
  truthful boundary-fake judge that echoes the prompt it receives;
  assert the finding does NOT settle to terminal REFUTED (it stays
  VERIFIED/PROPOSED/blocking). Then assert a re-raise of a previously
  REFUTED finding re-opens it.
- [ ] AC3: promised test file + selectors —
  `tests/unit/test_refuter.py::test_evidence_injection_does_not_refute_blocking`,
  `::test_parse_verdict_ignores_injected_leading_object`,
  `tests/unit/test_findings.py::test_refuted_is_reopenable_on_reraise`.
- [ ] AC4: `ruff` + `ruff format --check` + `pytest tests/unit`
  green; zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no
  `# noqa`; `ferova arch graph --check` exits 0.

## Open Questions

OQ1: implement by hand + human review before re-trusting auto-merge
(audit) — the refuter gates whether reviewer findings survive to the
merge gate.
