---
id: SP-EVIDENCE-SENTINEL-AUTHOR
title: Trust marker-bearing comments only when authored by the bot identity
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

# Trust marker-bearing comments only when authored by the bot identity

## Intent

Two trust-bearing markers — the "Verified — challenge with evidence"
sentinel and the review-archive HTML marker — are honoured on ANY
comment regardless of who wrote it, so a PR author can forge either.
Require the comment author to be the bot identity before the marker
is trusted.

## Context

Finding H4: `fetch_resolved_disagreements`
(`src/ferova/review/thread_context.py:165-234`) scans reply bodies
for `EVIDENCE_REPLY_SENTINEL` ("Verified — challenge with evidence",
defined `thread_context.py:74`) and treats any reply containing it as
a bot Coder challenge (`thread_context.py:208-215`) — with NO
`user.login` check. The inline-comment payload already carries
`user.login` (`gh_client.py:409`), so the author is available but
unused. A PR author posts the sentinel string themselves and feeds a
forged "challenge" into the next reviewer prompt.

Finding M6: `find_archive_comment`
(`src/ferova/review/gh_client.py:489-525`) and the
`fetch_archive_comment` / `fetch_archive_comment_with_status`
accessors (`gh_client.py:567-625`) return the FIRST comment bearing
`ARCHIVE_MARKER` (`<!-- ferova-review-archive -->`,
`gh_client.py:487`) with no author check — any commenter can post a
comment carrying the marker and forge the review archive the
downstream surfaces read.

Both markers must be gated on the authenticated app/bot login.

Audit 2026-07-13 findings H4 + M6. Execution: hand-implement with
human review (audit 2026-07-13) — merge-path change.

## Goals

- G1: `fetch_resolved_disagreements` accepts a sentinel-bearing reply
  ONLY when its `user.login` is the bot identity.
- G2: `find_archive_comment` and the `fetch_archive_comment*`
  accessors return a marker-bearing comment ONLY when its author is
  the bot identity.
- G3: the bot identity is resolved from the authenticated app/bot
  login (not a hard-coded literal), threaded to both call sites.

## Non-Goals

- NG1: no change to the marker strings themselves.
- NG2: no change to how comments are posted (the writers already post
  AS the bot); this only hardens the READ side.
- NG3: no broader ACL system — just author == bot identity.

## Assumptions

- A1: the authenticated bot login is discoverable (e.g. the app/bot
  login used to authenticate `gh`, or an injected identity) and can
  be passed to `thread_context` and `gh_client` read paths without a
  new secret.
- A2: the comment payloads expose `user.login`
  (`gh_client.py:409`), so filtering by author needs no extra API
  call.

## Interface

The two read paths gain the bot identity as an input (constructor
field on `GhCli` and/or a parameter threaded into
`fetch_resolved_disagreements`), so behaviour is deterministic under
test. Concretely, the reader functions take the expected bot login
(or read it from an already-available identity) and drop any
marker-bearing comment whose `user.login` differs. Signatures gain
one identity parameter each; no other public contract changes.

## Behavior

### Nominal

A sentinel reply authored by the bot login → fed into the reviewer
prompt as today. An archive comment authored by the bot login →
returned as today.

### Edge cases

- Marker present but `user.login` is a non-bot login → ignored
  (sentinel: not a challenge; archive: not the archive).
- Missing / null `user.login` on a marker-bearing comment → treated
  as untrusted, ignored.
- Multiple archive comments, only the bot-authored one carries the
  marker legitimately → the forged (non-bot) one is skipped; the
  bot-authored one is returned.

### Failure scenarios

- Bot identity unavailable at the call site → fail CLOSED: treat no
  comment as trusted rather than trusting all (log the missing
  identity). A missing identity must never widen trust.

## Architecture Impact

- Adds/Removes dependency: none — in-place modification of
  `thread_context.py` and `gh_client.py` (owned by existing specs);
  no new cross-owner import. Threads an existing identity value to two
  read paths.
- New / changed coupling, cycles, or shared state: the two readers now
  depend on the bot-identity value; supply it from the already-known
  authentication context, not a new global.

## Diagram

N/A (in-place fix)

## Acceptance Criteria

- [ ] AC1: unit — `fetch_resolved_disagreements` over a reply list
  containing a bot-authored sentinel reply and a non-bot-authored
  sentinel reply returns only the bot-authored disagreement.
- [ ] AC2 (INTEGRATION): drive `find_archive_comment` /
  `fetch_archive_comment_with_status` through `GhCli` with a truthful
  boundary fake for the `gh api` call (a fake `_run` returning a JSON
  comment list) containing a non-bot-authored comment bearing
  `ARCHIVE_MARKER` FIRST and a bot-authored marker comment second;
  assert the forged comment is ignored and the bot-authored archive
  is returned. Add the sentinel counterpart driving
  `fetch_resolved_disagreements` over a fake thread payload.
- [ ] AC3: promised test file + selectors —
  `tests/unit/test_thread_context.py::test_forged_sentinel_reply_ignored`
  and `tests/unit/test_gh_client.py::test_forged_archive_comment_ignored`.
- [ ] AC4: `ruff` + `ruff format --check` + `pytest tests/unit`
  green; zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no
  `# noqa`; `ferova arch graph --check` exits 0.

## Open Questions

OQ1: implement by hand + human review before re-trusting auto-merge
(audit) — forgeable trust markers feed the reviewer prompt and the
archive surfaces.
