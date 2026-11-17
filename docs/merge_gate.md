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
| `<n> unresolved review thread(s)` | review threads are open on the pull request (a nit opens none, see below) |
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

## A nit never blocks

Rule (owner decision 2026-11-15, SP-REVIEW-NITS-ONE-COMMENT; it amends
the 2026-10-25 rule "everything validated before merge" for the `nit`
severity alone): a lens comment of severity `nit` is recorded in the
ledger as a `note` finding with status `noted`, and is never posted as
an inline comment. It opens no review thread, no verifier, refuter or
re-check at head judges it, and the gate counts it nowhere.

Instead, every nit of the pull request goes into one comment carrying
`<!-- repoach:nit-digest -->`: a GitHub issue comment or a GitLab general
note, neither of them resolvable. A comment is the digest only when the
marker opens it; a marker quoted further down names nothing. It is posted at the first head that
has a nit and edited in place at each later head: the nits of the head
reviewed under `## At <sha7>`, those of earlier heads under
`## Earlier`, each line `path:line — lens — text`, at most 40 (the rest
counted, `… and <n> more`). A nit raised again at a later head is
listed once, at its latest head. A replay whose rendering is unchanged
writes nothing. A nit whose cited lines changed at a later head is still
listed under `## Earlier`: dropping it needs the cited-region rule of
SP-REVIEW-SETTLED-STAYS-SETTLED, which delivers it.

- `repoach review pr <N>` prints `nit digest: <n> nit(s), <url>`; its
  JSON and the archive comment carry `nits` (`count`, `digest_url`,
  `error`); the JSON also carries `body`, the rendered digest, which a
  dry run shows without posting. A reviewer whose comments are all `nit` or `minor` is
  promoted to `APPROVE` with `[auto-promoted: no blocking comment]`.
- A refused edit leaves the previous digest as it was, logs
  `nit_digest_edit_failed` and prints `nit digest not updated: <why>`;
  the verdict is unaffected.
- `minor`, `major` and `blocker` are unchanged: each opens a thread, and
  the thread blocks until it is resolved. Nothing turns one of them into
  a nit: the hallucination guard downgrades a disproved `blocker` or
  `major` to `minor`, still a thread, and a remark raised at one head as
  a `nit` and as something more severe counts at the more severe one and
  is left out of the digest.
- Lens text reaches the digest and the archive comment verbatim except
  for HTML comments: every `<!--` and `-->` in it is written `&lt;!--`
  and `--&gt;`, so neither comment can carry the other's marker. Each
  marker names one comment: a write edits the oldest trusted comment it
  opens and deletes any other, so two runs at the same head leave one
  digest.
- A nit thread opened before this rule is an ordinary open thread: the
  gate counts every unresolved thread whatever its severity, so it is
  answered and resolved like any other (`/resolve <reason>` included).

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

## A job the diff does not affect is a green no-op

Rule (SP-CI-JOBS-NO-OP-WHEN-NOT-AFFECTED, 2026-11-17). `ci.yml` always
runs, on every trigger it had, and on a `push` to `develop` too. Its
first job, `gate`, selects once with `repoach ci affected select`
against `.github/affected-jobs.toml` (named once, by
`env.REPOACH_AFFECTED_JOBS_TABLE`), then runs the arch check, the
attribution and CONTRIBUTING lints and the `[gate].pytest` suites. It
publishes `job_test`, `job_wheel_build`, `job_wheel_smoke`, `mode` and
`reason`.

- Every other job needs `gate`, keeps its name and matrix, and starts
  with an unconditional `Selection` step printing
  `::notice::<job>: <affected|not affected> by this diff (<mode>, <reason>); selection in the gate job summary`.
  Every later step runs only when the job's own output is `'true'`. An
  unaffected job therefore runs, expands its matrix and concludes
  `success` in seconds with every step but `Selection` `skipped`. A
  job-level `if` would have reported a matrix job under its unexpanded
  name, and the expanded names would then be `missing`.
- `Selection` refuses an output that is neither `true` nor `false` by
  name (`::error::<job>: the gate output for this job is '<value>',
  neither true nor false`), so a renamed output is a red, not a run that
  silently skips its tests.
- Where to read the selection: the `gate` job summary (mode, reason,
  rows hit, jobs selected) and each job's `Selection` notice.
- `mode` is `narrowed` only for a pull request into the integration
  branch. A `push` to `main` or `develop` (`full: push on origin/<branch>`),
  the release pull request (`full: pull_request on main`) and a
  `workflow_dispatch` run every step.
- A red `gate` (the selection exits 2 or 3, a lint or a gate suite
  fails) skips every other job through `needs`: the run is `failure` and
  the pull request is red with the named reason, never merged on a
  guess.
- `CI not green at head` reads the same names as before plus `gate`:
  `DEFAULT_REQUIRED_CHECK_NAMES` and the names derived from `ci.yml` are
  `gate`, both test suites, `wheel-build` and both wheel smokes. Until
  the next release `merge-on-ci.yml` runs `main`'s code, which requires
  the other five; they keep their names and conclude `success`, so both
  lists are satisfied.
- A red on a full run after a narrowed green follows the procedure
  below.

## A skipped required check is a failed check

Rule (SP-SKIPPED-REQUIRED-CHECK-IS-NOT-GREEN, 2026-11-17). A required
check whose job never ran is not a verdict, so it is never green.

- GitHub concludes a job `skipped` when its `if` is false (an actor
  outside the allowlist, a fork) or one of its `needs` failed. Both
  GitHub readers of the required checks (`judge_required_checks`, used
  by `review gate` and `review merge`, and the forge's
  `classify_required_checks`) read it as a failure:
  `required_check_failed: wheel-build=SKIPPED (the job never ran: a false job condition or a failed need)`.
  The outcome is `SKIP_CI_FAILED`, exit 5, recorded in the merge
  attempts comment; a new run at the head re-arms it like any failed
  check. The GitLab forge already read `skipped` this way.
- `NEUTRAL` stays green: it is a check's verdict that it has nothing to
  say about the head. `CANCELLED` is unchanged: ignored when another run
  of the same name exists, `cancelled with no other run` otherwise.
- A matrix job that never expanded reports one check under its template
  name, so its expanded names are `missing`. The refusal names the
  template it saw:
  `wheel-smoke (3.11): missing; the job reported unexpanded as "wheel-smoke (${{ matrix.python-version }})"=SKIPPED`.
  When a check also failed, these lines follow the failed ones in the
  `required_check_failed` line.
- A later run at the same head that did expand wins on each expanded
  name (the latest `startedAt` per name), so a head re-run after a
  skipped run is judged on the run that ran.
- A job whose every step is skipped concludes `success` (the no-op legs
  above), not `skipped`, and stays green.
- Replayed on release PR #307's head `79b9321`: the skipped run
  44071742332 reads `required_check_failed` with `wheel-build=SKIPPED`
  and four annotated `missing` names; with the later run 44081266335 the
  head reads green (`tests/unit/test_skipped_required_check.py`, over
  `tests/fixtures/check_runs/head_79b9321_*.json`).

## A red after a narrowed green

Rule (SP-AFFECTED-JOBS-MISSES-ARE-TEST-CASES D4, 2026-11-17). A pull
request into `develop` runs only the `CI` jobs its diff selects in
`.github/affected-jobs.toml`; `main`, the release pull request and
`develop` run every job (SP-CI-JOBS-NO-OP-WHEN-NOT-AFFECTED, above). When
one of those full runs goes red on something a narrowed pull request let
through:

1. The line stops (SP-RED-STOPS-THE-LINE): a red gate stops every
   other worksite until a corrective spec exists.
2. Record the red with its verdict:
   `repoach ci affected record --pr <n> --run <id> --job "<check name>" --verdict <verdict> --note "<why>" [--paths "a;b"]`.
   - `miss`: the job the table skipped would have failed on the pull
     request's own diff;
   - `conflict`: two green pull requests fail together on `main`;
   - `external`: the input is outside the diff (a feed, a registry, a
     flaky suite).

   `--paths` defaults to the run's head in the corpus, and a `miss`
   without paths is refused. The row lands in
   `tests/fixtures/affected_jobs/misses.tsv`, sorted, never edited or
   removed afterwards; a second row for the same `(run, job)` is refused
   with exit 1.
3. A `miss` widens a row of `.github/affected-jobs.toml` in its own pull
   request: `repoach ci affected replay` is red with the row before the
   change (`MISS #<n> run <id> <job> paths …`, exit 1) and green after
   it. That pull request merges before any code fix.
4. The code fix, on its own branch.

`repoach ci affected harvest --merged 30` rewrites the corpus
`tests/fixtures/affected_jobs/pr_runs.json` from the forge: for the last
30 merged pull requests (as `gh pr list --state merged` lists them,
newest first by creation), every completed `pull_request` run of `CI`,
each job over every attempt (a failure on an earlier attempt counts),
and the paths of the head it judged, read with
`compare/<pull request base>...<head>`. A run whose every job was
skipped by the actor allowlist is counted as `not admitted`, never
recorded; a `cancelled` job is never a failure; a head whose listing
reaches the forge's 300-file cap is replayed as the full class and
printed `TRUNCATED`. It prints `misses: <new>/<failed jobs>` and exits 1
when a failed job is unselected and no row covers it; when `gh` fails it
exits 2 and leaves the corpus as it was. `repoach release gate` prints
the same count as `affected-jobs misses: <new>/<failed jobs> over the
last 30 merged pull requests`, or `cannot say: <why>`, and never refuses
on it.

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
