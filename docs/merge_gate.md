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
submitted pull request review, so it re-runs the review workflow, and
`merge-on-ci.yml` re-judges when that run completes. Answer a thread,
then resolve it, and a green pull request whose last thread is closed
merges without a push. A thread resolved without a reply waits for the
next CI or review completion, or for `repoach review stranded
--dispatch`. `merge-on-ci.yml` runs `main`'s code, so the rule reaches
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
