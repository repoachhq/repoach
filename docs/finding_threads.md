# Finding threads: every posted finding knows its forge thread

When `repoach review pr` posts a finding as an inline comment, it records
the forge's own identifiers for that comment in the ledger table
`finding_posts`, in the same step that receives them. Every later step
that has to find the finding's thread reads that row instead of guessing
from the file and line. Specification: `SP-FINDING-THREAD-IDS`
(`docs/specs/2026-10-19_SP-FINDING-THREAD-IDS_persist-forge-thread-ids-per-finding.md`).

## What is recorded

One row per posted inline comment that states a recorded finding:

| Column | GitHub | GitLab |
|---|---|---|
| `finding_id` | the `pr_findings` row of the same head and fingerprint, the reviewer's own first | same |
| `comment_id` | review comment id | note id |
| `thread_id` | the root comment id (a reply takes it) | the discussion id (40 hex characters) |
| `review_id` | the batched review id | empty: GitLab has no review object |
| `in_reply_to` | the root id when the post is itself a reply | same |
| `head_sha` | the head the comment was anchored on | same |
| `provenance` | `posted`, or `backfill` for a legacy link | same |
| `thread_closed_at_head`, `thread_closed_by` | the head at which the thread was first seen resolved, and by whom | same |

Where the ids come from, measured 2026-11-02:

- GitHub's batched review (`POST pulls/<N>/reviews`) answers with the
  review id; the created comments and their ids are read back from
  `GET pulls/<N>/reviews/<id>/comments`. A comment is paired with its
  draft on path, line and body. A comment posted alone
  (`POST pulls/<N>/comments`) answers with its own id. The REST answers
  carry no GraphQL thread node id (`PRRT_…`); the root comment id is
  what a reply takes, so it is the thread id recorded.
- GitLab opens one diff discussion per inline comment
  (`POST merge_requests/<iid>/discussions`), which answers with the
  discussion id and its first note. A note GitLab refuses to position is
  posted as a plain note, recorded with no thread id.

A comment off the diff states no recorded finding and gets no row. A
forge answer without an id is logged (`finding_posts.id_unparseable`)
and gets no row; a failed write is logged (`finding_posts.write_failed`)
and the review goes on. A dry run posts nothing, so it writes no row.
Every run, dry runs included, creates the table when it is absent.

The `review pr` JSON carries `finding_posts`, the number of rows the run
wrote.

## Who reads it

- **Owner commands** ([owner_commands.md](owner_commands.md)). A
  `/resolve` or `/reopen` reply in an inline thread settles the findings
  whose recorded post opened that thread, even when the thread's line
  moved since. Only a thread no recorded post names falls back to the
  file and line, and only for findings that have no recorded post.
- **The refuted-finding sentinel.** The `Verified — challenge with
  evidence` reply goes to the finding's recorded thread. A finding with
  no recorded thread falls back to the file and line match; when exactly
  one root matches, a `backfill` row is written so the next run finds it
  directly.
- **Thread closures** (below).

The auto-challenge pass answers comments posted in the same run, from
the ids that run received, and reads nothing here.

## Thread closures

Each `review pr` asks the forge which threads are resolved, but only
when a recorded root post has no closure yet:

- GitHub: GraphQL `reviewThreads` (`isResolved`, `resolvedBy.login`),
  keyed by the first comment's `databaseId`;
- GitLab: the discussions, read on their first `DiffNote` (`resolved`,
  `resolved_by.username`).

A thread resolved by the bot (the login the review posts as) or by a
maintainer (the owner-command rule: GitHub role `admin`/`maintain`,
GitLab `access_level` 40 or more) is recorded with the head and the
login, and the report prints `thread closed by <login> for #<id>` on
stderr and under `## Closed threads` in the archive comment. A thread
resolved by anyone else is printed `thread resolved by <login> for
#<id>: not counted, neither the bot nor a maintainer` and is not
recorded. The JSON lists them under `threads_closed`.

**A resolved thread is not a settled finding.** A closure changes no
finding status and the gate does not count it. A finding is settled only
by a maintainer's `/resolve`, a fix at a later head (the head
re-verification), or the refuter.

## Linking findings posted before the table

```
repoach review backfill-threads <N> [--dry-run]
pr <N>: F findings, L linked, U unmatched, S already linked
```

A finding is linked when exactly one inline root authored by the bot
sits at its exact file and line (`backfill_line_tolerance = 0`) and no
other finding claims that root. A shifted line, two roots at the line,
two findings on one root, a conversation comment or a finding with no
line stay `unmatched`: no row, never an approximated link. `--dry-run`
counts and writes nothing. A forge that cannot be read ends with exit
code 2 and writes nothing.

## Prior art

The DeepSeek repository-agent prototype
[dsh-superagent-prototype](https://github.com/deepseek-ai/dsh-superagent-prototype)
(MIT, TypeScript; `docs/007-architecture.md`, read 2026-11-02) has three
ideas this table takes or names:

- **Ticket and Thread binding with closure detection** (`bindThread`,
  `threadClosed`): a Ticket follows a forge Thread, records its close and
  ends `merged` or `closed`; a closed Thread bound again is open again.
  `finding_posts` is that binding for a finding, and
  `thread_closed_at_head` its close record, without the lifecycle: a
  close settles nothing here.
- **A single writer** (`Engine`, over SQLite): the only writer of its
  domain state, one transaction per command. Here `finding_posts` is
  written only by the posting path, the sentinel's legacy match and
  `backfill-threads`.
- **An outbox with receipts** (`src/app/outbox.ts`): a message's receipt
  and its send are queued in one transaction; a message waits while its
  channel is off and is dropped when its conversation is gone, so it is
  never sent twice. Not implemented here: the sentinel and the archive
  comment are still found by scanning the pull request's comments for
  their markers. It is the planned successor of that scan
  (`docs/tech_debt.md`).
