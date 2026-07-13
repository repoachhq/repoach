---
id: SP-CI-SUPPLY-CHAIN-HARDEN
title: Pin CI actions, gate untrusted CI, document the branch-protection gap
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

# Pin CI actions, gate untrusted CI, document the branch-protection gap

## Intent

The workflows that run untrusted PR code on the self-hosted runner
alongside six provider keys and a write token use mutable action tags
(`@v4`/`@v5`) rather than pinned SHAs, and `ci.yml` runs PR code on the
self-hosted runner with no actor gate. On this private free-plan repo
server-side branch protection is impossible, so the whole protection
model is client-side and bypassable. Harden the supply chain and
document the compensating controls.

## Context

Audit 2026-07-13 findings H1, H2, plus CI lows. THIS SPEC TOUCHES
`.github/workflows/*`, which is WHITELIST-FORBIDDEN for the bots — so
Execution is OPERATOR-MANUAL throughout: hand-implement with human
review, NEVER `ferova develop`. It has no src integration AC (this is a
workflow/policy change); its ACs are checklist-style verifiable
conditions.

- H1 — `.github/workflows/auto-review.yml`: the `auto_fix` job
  (`auto-review.yml:282-466`) and the review job both run untrusted PR
  code (`pip install -e ".[dev]"`, the pytest matrix) on
  `runs-on: self-hosted` (`auto-review.yml:73,297`) bound to
  `environment: bots` (`auto-review.yml:80,307`, six provider keys) with
  `permissions: contents: write` (`auto-review.yml:308-309`), gated only
  by `contains(fromJSON('["jwfaye"]'), github.actor)`
  (`auto-review.yml:72,296,618`). Third-party actions are pinned to
  MUTABLE major tags — `actions/checkout@v4`
  (`auto-review.yml:98,136,328`), `actions/setup-python@v5`
  (`:104,339,345`), `actions/upload-artifact@v4` (`:241,254,532`),
  `actions/download-artifact@v4` (`:677`) — not immutable SHAs.
- Low — `.github/workflows/ci.yml:14-45`: the `test` job runs PR code
  on `runs-on: self-hosted` (`ci.yml:17`) with NO actor gate at all,
  and uses the same mutable `@v4`/`@v5` tags (`ci.yml:25,30`).
- H2 — no server-side branch protection is possible on this private
  free-plan repo (`gh api .../protection` returns 403). develop/main
  protection is ENTIRELY client-side: the workflow `if:` actor gates
  plus `.githooks/pre-push`'s textual branch check, which is bypassable
  with `git push --no-verify`.

## Goals

- G1: every third-party action in `auto-review.yml` and `ci.yml` is
  pinned to a full-length commit SHA (with the human-readable version
  in a trailing comment), not a mutable major tag.
- G2: `ci.yml`'s PR-triggered `test` job carries an actor gate
  equivalent to the review workflow's
  `contains(fromJSON('["jwfaye"]'), github.actor)`, so an untrusted
  actor cannot run PR code on the self-hosted runner via CI.
- G3: the missing server-side branch protection (H2) and its
  client-side compensating controls (workflow actor gates + pre-push
  textual check, and the `--no-verify` bypass they cannot stop) are
  DOCUMENTED, together with the upgrade/public-repo path that would
  restore real server-side protection.

## Non-Goals

- NG1: no move OFF the self-hosted runner and no change to the
  `environment: bots` secret placement — the residual-trust design
  (actor gate + Environment) is unchanged; this spec hardens pinning
  and closes the ungated `ci.yml` hole.
- NG2: no change to the review-bot logic, the gate, or any Python
  module — workflow/policy only.
- NG3: no attempt to enable server-side branch protection on the
  free plan (it 403s); this spec documents the gap, it does not close
  it in code.
- NG4: the bots MUST NOT implement this — `.github/workflows/*` is
  path-whitelist-forbidden for every fix the bots emit (CLAUDE.md path
  whitelist). Operator hand-implements.

## Assumptions

- A1: the current SHA for each action tag is resolvable at
  implementation time (`gh api /repos/actions/checkout/git/refs/tags/v4`
  or the release commit) and will be captured in the pin.
- A2: `jwfaye` remains the sole trusted actor; the actor allowlist
  stays a single-entry `fromJSON` array for parity across workflows.

## Interface

N/A — no code signatures. Changes are YAML in `.github/workflows/` and
prose in the repo's security/ops documentation (operator-owned).

## Behavior

### Nominal

- A trusted-actor PR: both workflows run as today, now against
  SHA-pinned actions.

### Edge cases

- An untrusted actor opens a PR: `ci.yml`'s `test` job is now gated and
  does not execute PR code on the self-hosted runner (matching
  `auto-review.yml`).

### Failure scenarios

- A mutable action tag is repointed upstream (tag-move supply-chain
  attack) → the SHA pin ignores the moved tag; the pinned commit is
  what runs. Fail CLOSED against tag mutation.
- A direct `git push --no-verify` to develop/main → NOT preventable
  client-side; this residual risk is explicitly documented (G3) rather
  than silently assumed away.

## Architecture Impact

- Adds/Removes dependency: none — workflow and documentation changes
  only; no Python module ownership, no cross-owner import. `owns.code`
  is `[]`.
- New / changed coupling, cycles, or shared state: none.

## Diagram

N/A (workflow/policy change).

## Acceptance Criteria

- [ ] AC1: every `uses:` in `.github/workflows/auto-review.yml` and
  `.github/workflows/ci.yml` references a full 40-char commit SHA (a
  grep for `@v[0-9]` across both files returns nothing); each pin
  carries a trailing version comment.
- [ ] AC2: `.github/workflows/ci.yml`'s `test` job carries an actor
  gate (`contains(fromJSON('["jwfaye"]'), github.actor)`) equivalent to
  the `auto-review.yml` jobs; an untrusted actor cannot run PR code on
  the self-hosted runner via CI.
- [ ] AC3: the server-side branch-protection limitation (H2), the
  client-side compensating controls, the `--no-verify` residual risk,
  and the upgrade/public-repo path that would restore server-side
  protection are documented in the repo's security/ops notes
  (operator-owned location, e.g. `docs/tech_debt.md` or a
  `docs/security_posture.md`).
- [ ] AC4: OPERATOR-MANUAL confirmation — the PR implementing this is
  hand-authored with human review; the bots did not (and cannot) emit
  the workflow edits (path whitelist forbids `.github/workflows/*`).

## Open Questions

- OQ1: implement by hand + human review (audit 2026-07-13 — workflow
  files are bot-forbidden; this is a supply-chain / policy change).
