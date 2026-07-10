---
id: SP-RELEASE-VERIFY-MERGE-COMMIT
title: release verify accepts the sanctioned merge-commit shape and exits loudly
version: 0.1
status: draft
author: jfaye (first live run of ferova release verify, release #68, 2026-07-09)
created: 2026-07-09
updated: 2026-07-09

owns:
  code: []
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# release verify accepts the sanctioned merge-commit shape and exits loudly

## Intent

`ferova release verify` must PASS the exact merge shape the gate
prescribes and FAIL LOUDLY otherwise. Today it does neither.

## Context

First live run (release #68, 2026-07-09): the operator merged with
"Create a merge commit" exactly as `release gate` instructed — main
tip `bc77de9` is a merge commit whose second parent is the approved
SHA `483b961`, `main..develop == 0`, a textbook release. `verify`
reported `verified: false` ("squash or stale merge?") because
`verify_release` compares the MAIN TIP to the approved SHA — a test
only a fast-forward can satisfy; the sanctioned method can never
pass it. Twin defect: the CLI exited 0 despite `verified: false`
(the spec'd contract was exit 5). Neither reviewers, judge nor the
suite caught it: no test modelled a real merge commit — the
integration fixture only exercised the divergence case.

## Goals

- G1: `verify_release` verifies the RELEASE SHAPE, not tip equality:
  verified is true when the approved SHA is the main tip itself
  (fast-forward) OR the second parent of a merge-commit main tip
  whose `main..develop` distance is zero; a squash (approved SHA
  absent from main's parents and history) or a stale merge (distance
  nonzero) stays refused with the existing detail message.
- G2: `ferova release verify` exits 5 when not verified, 0 when
  verified, 1 on evaluation errors — the same contract as
  `release gate` (and as spec'd originally in SP-RELEASE-GATE AC).
- G3: The truthful-fixture rule applies: tests build REAL throwaway
  git repos producing a real merge commit, a real squash and a real
  stale merge — no scripted git output.

## Non-Goals

- NG1: No change to `release gate` — its facts were correct on #68.
- NG2: No history rewriting or auto-repair on refusal — verify
  reports; the operator acts.

## Acceptance Criteria

- AC1: `tests/unit/test_release_gate.py::test_verify_accepts_merge_commit_release`
  — throwaway repo, gate-style receipt for the develop head, real
  `git merge --no-ff` onto main → verified true.
- AC2: `::test_verify_still_refuses_squash` — real squash merge →
  verified false, detail names squash/stale.
- AC3: `::test_verify_refuses_stale_merge` — merge commit taken, then
  develop advances (distance nonzero) → verified false.
- AC4: `tests/unit/test_release_cli.py::test_cli_release_verify_exit_codes`
  — verified false → `typer.Exit` code 5; verified true → clean
  return; receipt missing → exit 1.

## Open Questions

- OQ1: Should verified also require `main..develop == 0` in the
  fast-forward case (it is zero by construction)? Default: compute it
  once and require it in both branches — one rule, no cases.
