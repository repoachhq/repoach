# Owner commands: `/resolve` and `/reopen`

Some blocking findings state facts that live outside the repository: a
forge setting, a runner's kernel parameter, how a protected environment is
configured. The refuter judges from the cited file's window, so it can
never see such a fact; the finding is deferred `needs judgement` at every
head and the gate keeps refusing. A maintainer of the repository settles
such a finding with a comment. The reason is mandatory and recorded with
the settlement. Specification: `SP-OWNER-SETTLES-A-FINDING`
(`docs/specs/2026-11-01_SP-OWNER-SETTLES-A-FINDING_a-maintainer-settles-what-the-refuter-cannot-judge.md`).

## The commands

| Where | Command | Effect |
|---|---|---|
| A reply in the finding's inline thread | `/resolve <reason>` | Settles the finding the thread anchors |
| A reply in the finding's inline thread | `/reopen` | Counts it again |
| Any comment, the pull request's conversation included | `/resolve #<finding-id> <reason>` | Settles finding `<finding-id>` |
| Any comment | `/reopen #<finding-id>` | Counts it again |

The comment body, stripped, must start with the command. The reason is
the rest of the comment, whitespace collapsed; it must be at least 20
characters and is cut at 1000. `<finding-id>` is the ledger id the review
report prints (`finding 12: security ci/review-gate.sh:204 …`, `#12` in
the verdict basis). Most findings are never posted as inline threads
(posting is capped), so the `#<finding-id>` form on the pull request is
the usual one.

A thread is joined to its finding by the thread's anchor: the finding's
file, and its cited line range holding the thread's line. A thread that
anchors several open findings settles none and asks for `#<finding-id>`.

## Who may use them

Only a maintainer, as the forge reports it for each command:

- GitHub: collaborator role `admin` or `maintain`
  (`GET repos/:owner/:repo/collaborators/<login>/permission`);
- GitLab: project member, inherited membership included, with
  `access_level` 40 (Maintainer) or more
  (`GET projects/:id/members/all?query=<login>`, exact username).

The comment text never decides who is an owner, and the bot's own login
never is. A command from anyone else is ignored and answered once: `not a
maintainer of this repository`. When the forge cannot say (the token may
not read members, the API fails), nothing is applied, nothing is
recorded, and the next run asks again.

## What is recorded

An applied `/resolve` moves the finding to `owner_resolved` and writes
`verification_method = owner_resolve`, `verification_result =
owner-resolved by <login>: <reason>` and the reviewed head on the finding.
The `owner_commands` table of the ledger keeps one row per command
comment: the comment id, the command, the login, the finding, the reason,
the head, `resolution = owner_resolve` (or `owner_reopen`), and the status,
method, result and checked head the finding held before. `/reopen`
restores those four fields, and the gate counts the finding again.

Neither the merge gate, the review-time verdict nor the re-check of older
heads counts an `owner_resolved` finding. A finding is the finding of one
head: if the reviewers raise the same claim again at a new head, that is a
new row, settled again by its own command.

## When they are applied

`repoach review pr <N>` applies the commands at the reviewed head, before
the re-check of older heads and before the verdict, on a fresh run and on
a replay of a completed run at the same head alike. So once a maintainer
has commented, the next `review pr` (Yoob's `review:gate` job runs
`ci/review-gate.sh`, which passes `--pr` only) settles the finding at the
same head.

Each command is applied once, keyed by its comment id: a re-run at the
same head or at a later head neither re-applies nor re-answers it.
Commands are applied in creation order, so `/resolve` then `/reopen` ends
reopened.

## Answers

Every command gets one answer, in the thread for a thread command, on the
pull request otherwise:

- applied: `Finding #<id> owner-resolved by @<login> at <sha7>: <reason>.`
  or `Finding #<id> reopened by @<login>: the gate counts it again.`;
- a reason under 20 characters: the rule, and the command is ignored;
- an unknown `#<id>`, a conversation comment naming no finding, a thread
  anchoring none or several: why, and the command is ignored;
- `/resolve` on an advisory or already settled finding, `/reopen` on a
  finding that is not owner-resolved: `No change: …`.

The answers are posted only by a posting run. A dry run applies the
commands to the ledger, posts nothing and prints each answer as `owner
reply not posted: <reply>`; the next posting run posts it once.

## Report

- stderr of `review pr`: one `owner-resolved #<id> by <login>: <reason>`
  line per owner-resolved finding, and `owner commands: <n> applied, <m>
  ignored` when the run handled any;
- the `review pr` JSON: `owner_resolved: [{finding_id, login, reason,
  comment_id, head_sha}]` and `owner_commands: [{comment_id, command,
  author, finding_id, outcome}]` (the commands this run handled first);
- the Markdown report: an `## Owner-resolved findings` section, fenced
  because the reason is a person's text.

## What they never do

There is no `/wontfix`, no debt command and no expiry. A command never
fixes code, never resolves a forge thread and never changes the refuter;
`review gate` applies none, it reads the ledger `review pr` wrote.
