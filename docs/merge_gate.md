# The merge gate: what refuses a merge

Every merge repoach makes or allows goes through one decision,
`decide_at_head` (`src/repoach/review/auto_merge.py`), computed from facts
read again at the exact head: `repoach review gate <N>` (read-only, what
`scripts/safe_merge.sh` checks), `repoach review merge <N>`, and the
`auto_merge` jobs of `auto-review.yml` and `merge-on-ci.yml`. The
decision refuses, one reason per line, when:

| Reason | Fact |
|---|---|
| `head_sha unknown` | the head could not be resolved |
| `no review-integrity record at head …` / `review incomplete …` | the four reviewers did not all run and parse at this head |
| `CI not green at head` | a required check is not green |
| `<n> open blocking finding(s) at head` | blocking findings survive re-verification at head |
| `spec coverage …` | the declared spec's acceptance selectors are missing at head |
| `spec coverage recompute failed at <sha7>: …` | a coverage row graded by a retired rule could not be recomputed |
| `<n> unverified blocking finding(s): …` | a blocking finding has no verifier, or is still proposed |
| `<n> unresolved review thread(s)` | review threads are open on the pull request |
| `review threads unreadable: <why>` | the forge could not say how many threads are open |

When any of these refuses, the lines naming the findings of older heads
still blocking after their re-check at head follow them.

## An open review thread blocks the merge

Rule (owner, SP-OPEN-THREADS-BLOCK-THE-MERGE): while a review thread is
open, nothing merges the pull request. The gate reads the open threads
from the forge at decision time:

- GitHub: GraphQL `reviewThreads` whose `isResolved` is false, 100 per
  page over every page; a listing that ends on a page announcing another
  is refused, not counted.
- GitLab: the discussions whose first note is `resolvable` and not
  `resolved` (diff threads and general ones), asked page by page with
  `per_page=100` until an empty page, at most 50 pages.

A read that fails refuses with `review threads unreadable: <why>`; it is
never taken as zero.

Where it shows:

- `repoach review pr <N>` prints `review threads: <n> unresolved` on
  stderr; its JSON and the archive comment carry `review_threads`, and
  the archive's facts table has an `Unresolved review threads` row.
- `repoach review gate <N>` prints the same line, then
  `open thread <id> at <file>:<line>` for each thread it counted; its
  JSON carries `facts.unresolved_threads` and `review_threads`.
- A refused auto-merge records `pure gate refused: <n> unresolved review
  thread(s)` in the pull request's merge-attempts comment.
- `repoach review stranded --dispatch` does not dispatch a green pull
  request whose threads are open or unreadable; it prints
  `held #<n> (<reason>)` and lists it under `held`.
- `repoach release gate --pr <N>` refuses while the release pull request
  has an open thread.

Resolving a thread starts no workflow: GitHub Actions has no
`pull_request_review_thread` trigger. A reply in the thread does: it is a
submitted pull request review, so it runs the review workflow's `rearm`
job (it reviews nothing, see below), and `merge-on-ci.yml` re-judges
when that run completes. Answer a thread, then resolve it, and a green
pull request whose last thread is closed merges without a push. A
thread resolved without a reply waits for the next CI or review
completion, or for `repoach review stranded --dispatch`. `merge-on-ci.yml` runs `main`'s code, so the rule reaches
that lane with the next release.

### The forge settings are the second line of defence

The forges can enforce the same rule server-side:

- GitHub: the ruleset rule `required_review_thread_resolution` on the
  protected branches;
- GitLab: the project setting
  `only_allow_merge_if_all_discussions_are_resolved`.

Turn them on, but do not rely on them alone: they are off by default, a
token with bypass rights merges past them, and the merge path of a forge
without them would merge. repoach's own gate is the first line and holds
on every forge; the setting catches a merge made outside repoach.

## What starts a review and what re-arms a merge

SP-REVIEW-SURVIVES-TRANSPORT-FAILURES. `auto-review.yml` reviews:

| Event | Review | Merge re-armed |
|---|---|---|
| push (`synchronize`), `opened`, `reopened`, `ready_for_review` | yes; a newer push cancels the stale review | by the run's own `auto_merge`, then by CI's completion |
| an owner comment on the pull request | yes, after a 60 s coalesce | by the run's own `auto_merge` |
| an owner `/resolve` or `/reopen` in a thread reply (`pull_request_review_comment`) | yes, after a 60 s coalesce | by the run's own `auto_merge` |
| a submitted review, a thread reply among them (`pull_request_review`) | **no** | yes: the `rearm` job (60 s coalesce, only the last of a burst) succeeds, and `merge-on-ci.yml` admits the completed run |
| `workflow_dispatch -f pr_number=<n>` by the owner | yes | by the run's own `auto_merge` |
| the automatic retry (`workflow_dispatch` by `github-actions[bot]` with `retry_of`) | yes, once per head | by the run's own `auto_merge`; `auto_fix` never runs on it |

A reply used to start a full review that cancelled the one in progress
(eight cancellations a minute on #288); a review moves no head, so it now
only re-arms. A run whose every job is skipped concludes `skipped`,
which `merge-on-ci.yml` does not admit; the `rearm` job is what makes a
reply's run conclude `success`.

## A transport outage costs time, not the review

Every model call of a review (the four reviewers, the refuter's judge,
the re-check of older heads, the planner and the developer) goes through
`AgentLoop._provider_call`, which retries a transport error (a timeout,
a refused connection, a 5xx; never a 4xx, a 408 or a 429) with waits of
5, 10, 20, 40, 60, 60… seconds inside a window of
`[provider].transport_retry_window_s` seconds, 300 by default
(`REPOACH_TRANSPORT_RETRY_WINDOW_S` overrides it, 0 turns it off, 1800
at most). Each attempt is one usage row. The window is counted once per
call: the reviewers' and the tool loop's own retries do not open a
second one.

A chain the proxy refuses before dispatch, because every candidate is
tripped, answers 502 with `Retry-After`: the seconds until its first
candidate re-opens. The window waits exactly that when it fits, and
gives up at once when it does not (a model gone with a 410 stays tripped
for 7 days). Past the window nothing changes: the
reviewer reports `[parse_failed:TRANSPORT] all 3 attempts raised —
last=GatewayTransportError`, the judge `judge call failed:
GatewayTransportError`, the verdict is `ERROR` and `review pr` exits 3.

A reply that outgrew its output cap is not a transport error. The
`claude` CLI counts its thinking in `CLAUDE_CODE_MAX_OUTPUT_TOKENS` and
does not bound it (`MAX_THINKING_TOKENS` is ignored by
`claude-opus-5-5`), so the proxy gives it the caller's `max_tokens` plus
32 000 tokens. When a reply still outgrows the cap, the CLI's own
message is kept (`API Error: Claude's response exceeded the 4096 output
token maximum…`), the candidate is not tripped, and the caller receives
`output_cap_exceeded`, which no loop retries: on 2026-11-04 the
Sentinel's answer on #288 was four replies of thinking and nothing else,
$2.16 a try, and its failure tripped `claude_code/opus`, so the judge
calls that followed found no opus candidate left.

In CI each job proxy keeps its breaker in its own `runner.temp`
(`REPOACH_BREAKER_PROBE_SEED_DB`), so one job's trips never lock the
next. Locally the breaker is the project's ledger; at start the proxy
logs `proxy_configured_model_gone: <ref> (provider_410) in <chain>…`
for each configured model the provider has retired, to remove from
`chains.env`.

`review pr` then names the cause in its JSON, `incomplete_cause`:
`transport` when every reviewer that errored and every deferred finding
failed on a transport error, `other` otherwise. On `transport`, the
`retry` job waits 300 s, records the retry in the merge attempts comment
(outcome `REVIEW_RETRY`, naming the failed run) and dispatches the
workflow once at the same head (`repoach review retry <N> --head <sha>
--run <id>`). The retry run reviews the head afresh, records its own id
and result in the same comment, and never retries again; a head already
retried, a moved head or a closed pull request is not retried. Like
`merge-on-ci.yml`, the dispatched workflow is `main`'s, so the retry
works from the release that carries it.
