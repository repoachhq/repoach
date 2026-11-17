---
id: SP-AFFECTED-JOBS-MISSES-ARE-TEST-CASES
title: A failed job the table would have skipped becomes a row of a committed table, replayed by a test
version: 0.3
status: approved
author: agent
created: 2026-11-16
updated: 2026-11-17

owns:
  code: [src/repoach/affected/gh.py, src/repoach/affected/harvest.py, src/repoach/affected/misses.py, src/repoach/affected/misses_cli.py, tests/unit/test_affected_misses.py, tests/fixtures/affected_jobs/misses.tsv, tests/fixtures/affected_jobs/harvest/]
  resources: [format:affected-jobs-misses]

depends_on: [SP-AFFECTED-JOBS-TABLE, SP-CLI-STDOUT-ONE-JSON]
provides_to: [SP-RELEASE-GATE]

constraints:
  harvest_window_pull_requests: 30
---

# A failed job the table would have skipped becomes a row

## Intent

A narrowed pull request is only as safe as the table that narrows it,
and the table is only as good as the misses it has been confronted
with. This spec gives the table a harvest: `repoach ci affected harvest`
reads the merged pull requests from GitHub, pairs each `CI` head's
changed paths with the jobs that failed at it, rewrites the corpus the
replay test reads, and names every pair the table would not have
selected. A miss found later, on `develop`, on `main` or on the release pull request,
enters a committed table with a verdict before anything else is fixed,
and the replay is red until the table selects it.

## Context

Measured 2026-11-16 on `origin/develop` `f16702b`.

| # | Fact | Where |
|---|---|---|
| F1 | The corpus of SP-AFFECTED-JOBS-TABLE F7 was harvested by a session script: `gh pr list --state all --base develop --limit 30 --json number,files`, then per head branch `gh run list --event pull_request --json databaseId,conclusion,headSha` and `gh run view <id> --json jobs`; the paths were the pull request's files at harvest time, not per head | SP-AFFECTED-JOBS-TABLE F7; `tests/fixtures/affected_jobs/pr_runs.json` (SP-AFFECTED-JOBS-TABLE) |
| F2 | Per-head paths exist on the forge: `gh api repos/{owner}/{repo}/compare/{base}...{head}` lists `files[].filename` (and `previous_filename` on a rename) against the merge base of `base` and `head`, so a head's diff is read even after the base moved | GitHub REST, `compare two commits` |
| F3 | 15 failed `CI` jobs on 167 heads; `jobs[].conclusion == "failure"` with `steps[].conclusion` naming `Pytest`, `Pytest — integration` or the smoke's `pipx install`; a cancelled job (`cancelled`, 7 runs) is a cancelled run, never a judgement | `gh run view --json jobs` over the corpus |
| F4 | `ci.yml` runs on `push` to `main` and on the release pull request with the full set; `develop` runs no `CI` on push today. Owner decision 2026-11-16 (SP-PR-RUNS-AFFECTED-JOBS O2): SP-CI-JOBS-NO-OP-WHEN-NOT-AFFECTED re-enables `push: develop` with the full set, so a miss is seen at the merge | `.github/workflows/ci.yml:4-11` |
| F5 | SP-RED-STOPS-THE-LINE: a red gate stops every other worksite until a corrective spec exists; `repoach release gate` is the release pull request's gate | `docs/specs/2026-10-25_SP-RED-STOPS-THE-LINE_*.md`; `src/repoach/cli/release_cmds.py` |

## Decisions (2026-11-16)

- D1. A miss is a `(head_sha, job name)` pair where the job concluded
  `failure` on a `pull_request` run of `CI` and `select(paths of the
  head)` of SP-AFFECTED-JOBS-TABLE does not hold the job's id. A
  `cancelled` job is never a miss. `gate` is never a miss (it always
  runs).
- D2. `harvest --merged N` rewrites `tests/fixtures/affected_jobs/pr_runs.json`
  from the forge: for the last `N` pull requests merged into the
  integration branch, every completed `pull_request` run of `CI` with
  its head, the head's paths read through `compare` (F2), and each
  job's name, conclusion and failed steps; scrubbed of every URL and
  token (only `repoachhq/repoach`, numbers, shas, paths and job names
  remain). It prints `misses: <new>/<failed jobs>` and exits 1 on a
  new miss, naming each `MISS #<pr> run <id> <job> paths <first three>…`;
  exit 2 when `gh` fails, the file untouched.
- D3. `tests/fixtures/affected_jobs/misses.tsv`, `format:affected-jobs-misses`,
  tab-separated, header `pr	run	head_sha	job	verdict	paths	note`,
  one row per `(run, job)`, sorted by `pr`, `run`, `job`; `verdict` in
  `{miss, conflict, external}`; `paths` is `;`-joined. `record --pr
  --run --job --verdict --note` appends a row (exit 1 on a duplicate
  key); `replay` prints every `miss` row whose job `select(paths)` does
  not hold and every row of `pr_runs.json` whose failed job is not
  selected, exit 1 if any, else `misses: 0/<failed jobs>`. `conflict`
  and `external` rows are printed with their verdict and never fail
  the replay. The table is append-only.
- D4. The procedure, written in `docs/merge_gate.md` under "A red
  after a narrowed green": (1) the line stops (F5); (2) `repoach ci
  affected record` with the verdict — `miss` when the job the table
  skipped would have failed on the pull request's own diff, `conflict`
  when two green pull requests fail together on `main`, `external`
  when the input is outside the diff (a feed, a registry, a flaky
  suite); (3) a `miss` widens a row of `.github/affected-jobs.toml` in
  its own pull request, with `replay` red before and green after,
  merged before any code fix; (4) the code fix on its own branch.
- D5. The harvest runs by hand and from `repoach release gate` as a
  report line (`affected-jobs misses: <new>/<failed jobs>` over the last 30
  merged pull requests), never as a refusal of the release.
- D6 (0.3, how the harvest reads the forge). These are implementation
  choices, each made so that nothing is read as narrower than it was.
  - The window is `gh pr list --state merged --base <integration
    branch> --limit N`, which lists newest first by creation.
  - A pull request's runs are `gh run list --branch <head> --event
    pull_request --workflow <ci.yml name>`, completed and created between
    its opening and its merge, so a reused branch name lends no runs. A
    listing that reaches its limit (200) is refused, never cut.
  - Every attempt is read: a job that failed on any attempt counts as
    failed.
  - A head's paths come from `compare/<the pull request's .base.sha>...<head>`,
    not `compare/develop...<head>`: a head merged by a merge commit is an
    ancestor of `develop`, and its compare would list no file. A
    `compare` status outside `ahead`, `diverged` and `identical` is
    refused by name.
  - In `replay`, a corpus failure that a `conflict` or `external` row
    covers (same `run` and `job`) is not a miss. A `miss` row is judged
    on its own paths. A job name that `ci.yml` does not produce is
    refused by name (exit 2), never skipped.
  - The release gate writes nothing. It prints the line on stderr and
    puts it under `affected_jobs_misses` in its JSON. When the forge or
    an input refuses, the line is `affected-jobs misses: cannot say:
    <why>`.

## End state

`repoach ci affected harvest --merged 30` regenerates the corpus from
the forge in one command; `repoach ci affected replay` is what
`tests/unit/test_affected_misses.py` calls; the procedure that fills
`misses.tsv` is written where the merge gate's other rules are.

Measured 2026-11-17 (0.3), first harvest: #265-#302, 173 runs, 47
failed jobs, `misses: 2/47`. The 2 were #293 run `43314128941`, a
docs-only head: both `Test suite` legs failed on attempt 1, where
`tests/integration/test_slow_settings_isolation_end_to_end.py` timed out
after 60 s, and attempt 2 was cancelled. The TABLE corpus kept the
latest attempt only, so it had shown them as `cancelled`. Through the
procedure of D4 they became two `external` rows of `misses.tsv`. Since
then, harvest and replay print `misses: 0/47`.

Against the TABLE corpus (#260-#296), the paths of all 162 runs both
corpora hold are identical, since TABLE 0.4 already read each head
through `compare`. The differences are:
- the window: #260-#264 and #292 (not merged) are out, #297-#302 are in;
- earlier-attempt failures now counted: #281 run `41501648389` and #288
  run `41910270088`, both selected;
- the two keys of D6.

## Easy solution rejected

Treat a red on `main` after a narrowed green as an ordinary
regression: fix the code, re-run, move on. Rejected: the code was green
on the pull request for the jobs that ran; the defect that let it merge
is in the table, and fixing only the code leaves the next pull request
the same hole. The miss has to change the table, and the only way to
know the table changed is a replayed row.

Also rejected: a harvest that reads the pull request's final file list
(what F1 did). It was good enough to measure, since every failed job of
the corpus sat on a pull request whose every head touched `src/` or
`tests/`, but a head that failed before a later push added the tree the
table selects on would be hidden by it; the harvest reads per head (F2).

## Hard part

Attribution. On `main` and on the release pull request the full set
judges the merge of many pull requests at once, so a red there may be a
miss of one table row, a semantic conflict between two green pull
requests, or an input outside every diff; no table selects for the
last two. The row's author writes which it was, the replay judges `miss`
rows only, and the rate of each verdict stays readable. The harvest
also has to tell a judgement from a death: `failure` on a `Pytest` step
is a judgement, `cancelled` is a cancelled run, and a job skipped by the
actor allowlist (a fork's pull request) is neither — it is excluded with
its reason in the report.

## Goals

- G1: the corpus is regenerable from the forge in one command, per head, scrubbed; a harvest of today's window prints `misses: 0/<failed jobs>` once each red has its row (0/47 on 2026-11-17).
- G2: the replay is a test and a command, and a `miss` row is red until the table selects it.
- G3: a red after a narrowed green has one written procedure, ending in a row.

## Non-Goals

- NG1: no automatic edit of the table; the harvest names, a human or an agent widens, the replay proves.
- NG2: no scheduled harvest; the release gate reports, the procedure records.
- NG3: no change to `autofix`, `notify` or the merge gate's refusals.

## Assumptions

- A1: `gh` is authenticated on the machine that harvests (the owner's, or the release gate's runner).
- A2: GitHub keeps `pull_request` runs of `CI` for the harvest window (90 days of logs; the run and job metadata longer).

## Interface

Since 0.3 the interface is the one the code has. Three modules beyond
0.2: `gh.py` holds the one `gh` runner, so the test suite can close it
without importing the review package, and so `misses.py` imports no `gh`
caller. `misses.py` holds the corpus models and `load_corpus`, since the
replay reads them. `misses_cli.py` holds the three commands, registered
on `repoach ci affected` from `cli/main.py`, because the TABLE module
`affected/cli.py` importing them would be an edge TABLE -> MISSES and a
cycle.

```python
# src/repoach/affected/gh.py
class HarvestError(RuntimeError): ...                      # names the gh command and the first line of its stderr
def run_gh(*args: str) -> str: ...                         # the one forge reader; tests/conftest.py replaces it with a refusal

# src/repoach/affected/misses.py
class JobOutcome(BaseModel): name: str; conclusion: str; failed_steps: tuple[str, ...]
class HeadRun(BaseModel):
    pr: int; run: int; head_sha: str; paths: tuple[str, ...]; paths_truncated: bool; conclusion: str; jobs: tuple[JobOutcome, ...]
class MissRow(BaseModel):
    pr: int; run: int; head_sha: str; job: str; verdict: Literal["miss", "conflict", "external"]; paths: tuple[str, ...]; note: str
def load_corpus(path: Path) -> list[HeadRun]: ...          # CorpusError on any key the format does not define, or a missing one
def load_misses(path: Path) -> list[MissRow]: ...          # MissesError: header, cell count, number, verdict, a miss without paths, duplicate key, unsorted
def record_row(path: Path, row: MissRow) -> list[MissRow]: ...   # DuplicateRowError on (run, job); atomic, sorted, no other row changed
def replay(corpus, misses, table, needs, check_names: Mapping[str, str]) -> ReplayReport: ...   # UnknownJobError on a name ci.yml does not produce
class ReplayReport(BaseModel): failed_jobs: int; misses; new_misses; recorded; truncated

# src/repoach/affected/harvest.py
class Harvest(BaseModel): repo: str; runs: tuple[HeadRun, ...]; not_admitted: int
def harvest(repo: str, *, merged: int, base: str, workflow: str, gh: GhRunner) -> Harvest: ...   # workflow = ci.yml's name
def write_corpus(runs, path, *, repo: str, harvested: date, source: str) -> None: ...         # atomic: temp file then os.replace
def harvest_and_replay(root, *, repo, merged, base, table, workflow, misses, gh) -> Harvested: ...   # inputs read before any forge call

# repoach ci affected harvest [--merged 30] [--repo owner/name] [--out tests/fixtures/affected_jobs/pr_runs.json]
#     --repo defaults to what `gh repo view` resolves for the working tree
# repoach ci affected record --pr N --run ID --job NAME --verdict miss|conflict|external --note TEXT [--paths a;b] [--head-sha SHA]
#     --paths and --head-sha default to the run's head in the corpus; a miss with no paths is refused (exit 2)
# repoach ci affected replay [--corpus …] [--misses …] [--table …] [--workflow …]
```

The corpus format is SP-AFFECTED-JOBS-TABLE's, extended: each head
carries `paths_truncated`, each job `failed_steps`. The TABLE replay
test reads the new key.

Stdout: one JSON object per command (`SP-CLI-STDOUT-ONE-JSON`); the
`MISS` lines and `misses: <new>/<failed jobs>` on stderr. Exit codes: 0; 1 a miss
(harvest, replay) or a duplicate (record); 2 the forge or a file refused,
nothing written.

## Behavior

### Nominal
`harvest --merged 30` walks the merged pull requests newest first,
reads each head's paths and jobs, writes the corpus, replays it against
the table and prints `misses: 0/<failed jobs>`. `replay` on the committed corpus
and the committed misses table exits 0.

### Edge cases
- a job failed then passed on a re-run at the same head → the failure counts: a job the table would have skipped is still a job it would have skipped.
- a run skipped by the actor allowlist (every job `skipped`) → excluded, counted as `not admitted` in the report.
- the same head on two pull requests (a stacked branch) → one `HeadRun` per `(pr, run)`.
- a `conflict` or `external` row whose job the table now selects → kept, printed with its verdict; nothing to prove.

### Failure scenarios
- `gh` unauthenticated or rate-limited → exit 2 with the first line of its stderr, corpus untouched.
- `misses.tsv` names a job `ci.yml` no longer defines → `test_the_misses_table_is_well_formed` red with the name; the row is kept and its `note` says what the job became.
- a `compare` answer truncated (`files` at the 300-file cap) → that head is recorded with `paths_truncated: true` and the replay treats it as full class (every job selected), printed as such, so a truncated head can never be a false miss nor a false pass.

## Tests

| # | File | Test | Mutation that turns it red |
|---|---|---|---|
| T1 | `tests/unit/test_affected_misses.py` | `replay` over the committed corpus and table → `misses: 0/<failed jobs>` | set the python row's `jobs = []` (`test_t1_replay_goes_red_when_the_python_row_selects_nothing`). Removing `tests/` from the row does not turn it red: those paths fall to "unknown → every job" (SP-AFFECTED-JOBS-TABLE T6) |
| T2 | same | `harvest` on the `gh` answers of `tests/fixtures/affected_jobs/harvest/gh_answers.json` (two pull requests, three heads: one miss, one cancelled job, one not-admitted run; written in gh 2.62's shapes, not recorded live) writes exactly the heads admitted, exit 1, one `MISS` line | count the cancelled job |
| T3 | same | `write_corpus` on a failing `gh` leaves the previous file byte-identical | write before the fetch completes |
| T4 | same | the misses table is well formed: header, sort, unique keys, verdict vocabulary, jobs defined in `ci.yml` | shuffle two rows; invent a verdict |
| T5 | same | a `conflict` or `external` row never fails the replay; a `miss` row whose job is unselected does | treat `external` as `miss` |
| T6 | same | `record` refuses a duplicate `(run, job)` with exit 1; appends otherwise, sorted | append anyway |
| T7 | same | a head with `paths_truncated` is replayed as the full class and printed | replay it as its listed paths |

## Success conditions

- C1 (external oracle: the forge): `repoach ci affected harvest --merged 30` on the branch exits 0, prints `misses: 0/<n>`, and `git diff --stat tests/fixtures/affected_jobs/pr_runs.json` after it shows only per-head path differences against the session corpus (the verifier reads the diff and names two heads whose paths changed and why). Since 0.3: the TABLE corpus was already per head, so no common run's paths change; the verifier names instead two runs whose outcome changed (#281 run `41501648389`, #293 run `43314128941`: earlier-attempt failures) and the window shift (End state).
- C2: `repoach ci affected replay` exits 0; after `repoach ci affected record --pr 0 --run 0 --job "Test suite (Python 3.11)" --verdict miss --paths docs/x.md --note probe` it exits 1 naming the row, and `git checkout tests/fixtures/affected_jobs/misses.tsv` restores it.
- C3: `docs/merge_gate.md` has the four-step section of D4; `pytest -q tests/unit/test_affected_misses.py` green with T1–T7 in `-v`; `repoach arch check --base origin/develop` → `edge-honesty: ok`.

## Architecture Impact
- Adds dependency: SP-AFFECTED-JOBS-MISSES-ARE-TEST-CASES -> SP-AFFECTED-JOBS-TABLE (`select`, `load_table`, `workflow_jobs`, the corpus format)
- Adds dependency: SP-RELEASE-GATE -> SP-AFFECTED-JOBS-MISSES-ARE-TEST-CASES (D5: `repoach release gate` calls `harvest` and `replay` for its report line; `replay` itself imports no `gh` caller)
- Adds dependency: SP-AFFECTED-JOBS-MISSES-ARE-TEST-CASES -> SP-CLI-STDOUT-ONE-JSON
- Adds dependency (0.3): SP-STRUCTLOG-CACHE-TEST-FIXTURE -> SP-AFFECTED-JOBS-MISSES-ARE-TEST-CASES (`tests/conftest.py` replaces `repoach.affected.gh.run_gh` with a refusal, so no test reads the forge; declared in that spec's `depends_on`)
- New resource: `format:affected-jobs-misses`. `gh` is called through `subprocess`, as the lint scripts do; no import from `forge`. No cycle.

## Diagram
```mermaid
flowchart LR
    F[gh: pulls, runs, compare] --> H[harvest]
    H --> C[pr_runs.json]
    C --> R[replay]
    M[misses.tsv] --> R
    T[affected-jobs.toml + select] --> R
```

## Acceptance Criteria
- [ ] AC1: `pytest -q tests/unit/test_affected_misses.py` → green. Today (`f16702b`): no such file.
- [ ] AC2: `repoach ci affected replay` → exit 0, `misses: 0/<failed jobs>` on stderr (0/47 on 2026-11-17).
- [ ] AC3: `docs/merge_gate.md` carries the section of D4, dated.
- [ ] AC4: `repoach arch check --base origin/develop` → `edge-honesty: ok`.

## Open Questions
- None. The harvest window is 30 pull requests and the release gate reports, never refuses (D5), from the Yoob precedent (SP-MATRIX-MISSES-ARE-TEST-CASES Q1); the owner approved the family on 2026-11-16 with the umbrella's four decisions and raised nothing against it.
