# SP-OPEN-THREADS-BLOCK-THE-MERGE — An open review thread blocks the merge on every merge path

Both forges list every open review thread over every page (GitHub reviewThreads 100 per page, GitLab discussions page by page until an empty one); MergeFacts carries the count and whether it was read; decide_at_head reads it for every merge path, the stranded sweep holds a PR with open threads, the release gate reads the release PR's threads, review pr and review gate report them, a reply in a thread re-arms merge-on-ci, and the merge gate page names the forge settings as the second line of defence.

## Step 1 — Declare the read and read every open GitHub thread

- **Files**: `src/repoach/forge/protocol.py`, `src/repoach/forge/github.py`, `tests/unit/test_forge_unresolved_threads.py`
- **Action**: ReviewThread gains path ('') and line (None); Forge gains unresolved_threads(number) -> list[ReviewThread], every open thread over every page, ForgeError when the listing cannot be read whole. GitHubForge runs one gh api graphql --paginate --slurp query of reviewThreads(first:100,after:$endCursor) with pageInfo, isResolved, path, line, originalLine and the first comment's databaseId; unresolved_threads_from_github keeps each node not resolved (root id 0 without a databaseId, originalLine when line is null) and raises on no page, a page without data, thread connection, nodes list or boolean hasNextPage, a page carrying errors, a thread that is not an object, and a last page saying hasNextPage true. The tests build pages with the step 3 shapes.
- **Commit**: `feat(forge): read every open github review thread`
- **Done when**: pytest -q tests/unit/test_forge_unresolved_threads.py -k github passes
- **Unit tests**: `tests/unit/test_forge_unresolved_threads.py::test_github_reads_the_only_open_thread_on_page_three`, `tests/unit/test_forge_unresolved_threads.py::test_github_counts_an_open_thread_without_a_root_id_and_falls_back_to_the_original_line`, `tests/unit/test_forge_unresolved_threads.py::test_github_refuses_a_listing_it_cannot_count`, `tests/unit/test_forge_unresolved_threads.py::test_github_failed_call_raises`, `tests/unit/test_forge_unresolved_threads.py::test_github_open_thread_with_null_path_or_comments_keeps_empty_defaults`

## Step 2 — Read every open GitLab discussion, page by page

- **Files**: `src/repoach/forge/gitlab.py`, `src/repoach/bench/local_forge.py`, `tests/unit/test_forge_unresolved_threads.py`
- **Action**: GitLabForge.unresolved_threads asks merge_requests/<n>/discussions?per_page=100&page=<p> from page 1 until an empty array, without glab --paginate, at most DISCUSSION_PAGES_MAX (50) pages, else ForgeError; a page that is not an array raises. unresolved_threads_from_discussions keeps each discussion whose first note is resolvable and not resolved, diff or general, path and line from its position, and raises on a discussion without a first note object. LocalDiffForge returns no thread.
- **Commit**: `feat(forge): read every open gitlab discussion`
- **Done when**: pytest -q tests/unit/test_forge_unresolved_threads.py -k gitlab passes
- **Unit tests**: `tests/unit/test_forge_unresolved_threads.py::test_gitlab_reads_pages_until_an_empty_one_and_finds_the_open_thread_on_page_three`, `tests/unit/test_forge_unresolved_threads.py::test_gitlab_counts_a_general_resolvable_discussion`, `tests/unit/test_forge_unresolved_threads.py::test_gitlab_refuses_a_listing_past_the_page_cap`, `tests/unit/test_forge_unresolved_threads.py::test_gitlab_refuses_a_page_that_is_not_an_array_or_fails`, `tests/unit/test_forge_unresolved_threads.py::test_gitlab_refuses_a_discussion_it_cannot_read`

## Step 3 — Shape the thread answers and the recorded results

- **Files**: `tests/forge_fakes.py`, `tests/fixtures/forge/github/review_threads.json`, `tests/fixtures/forge/gitlab/review_threads.json`
- **Action**: tests/forge_fakes.py gains threads_page(nodes, more=False) and thread_node(root_id, resolved, path, line), the GraphQL reviewThreads shapes. The two recorded review_threads fixtures keep their exchanges; their expected results gain path and line at the defaults.
- **Commit**: `test(forge): shape the review thread answers`
- **Done when**: pytest -q tests/unit/test_forge_unresolved_threads.py passes
- **Unit tests**: `tests/unit/test_forge_unresolved_threads.py::test_github_reads_the_only_open_thread_on_page_three`, `tests/unit/test_forge_unresolved_threads.py::test_gitlab_mr_without_discussions_reads_one_empty_page`

## Step 4 — Refuse on open or unreadable threads in the pure gate

- **Files**: `src/repoach/review/open_threads.py`, `src/repoach/review/merge_gate.py`, `tests/unit/test_open_threads_block_the_merge.py`
- **Action**: open_threads.py holds OpenThreads (read, threads, error; count, refusal, summary_line, thread_lines, as_json), read_open_threads (a ForgeError becomes an unread result with the redacted reason), unresolved_reason and unreadable_reason. MergeFacts gains open_threads (unread by default), set by with_threads, and the read-only unresolved_threads, threads_read and threads_error derived from it; compute_merge_decision adds '<n> unresolved review thread(s)' while n > 0 and 'review threads unreadable: <why>' when not read.
- **Commit**: `feat(review): refuse the merge on open review threads`
- **Done when**: pytest -q tests/unit/test_open_threads_block_the_merge.py -k 'refuses_and_zero or fail_closed or names_each' passes
- **Unit tests**: `tests/unit/test_open_threads_block_the_merge.py::test_one_open_thread_refuses_and_zero_permits`, `tests/unit/test_open_threads_block_the_merge.py::test_unread_threads_fail_closed`, `tests/unit/test_open_threads_block_the_merge.py::test_read_open_threads_names_each_thread_or_the_failure`

## Step 5 — Read the threads on every merge path

- **Files**: `src/repoach/review/auto_merge.py`, `tests/merge_wait_fakes.py`, `tests/unit/test_open_threads_block_the_merge.py`
- **Action**: decide_at_head reads the threads last, after the re-check and the ledger read and right before the decision, and folds them in with MergeFacts.with_threads; evaluate_merge_gate keeps facts.open_threads on GateEvaluation.open_threads. run_auto_merge records SKIP_GATE with 'pure gate refused: <n> unresolved review thread(s)' in its attempts comment. The fixture forge serves per-PR threads to gh api graphql.
- **Commit**: `feat(review): read the open threads on every merge path`
- **Done when**: pytest -q tests/unit/test_open_threads_block_the_merge.py -k auto_merge passes
- **Unit tests**: `tests/unit/test_open_threads_block_the_merge.py::test_auto_merge_refuses_a_green_pr_with_an_open_thread_then_merges_once_it_is_resolved`, `tests/unit/test_open_threads_block_the_merge.py::test_auto_merge_refuses_when_the_threads_cannot_be_read`, `tests/unit/test_open_threads_block_the_merge.py::test_the_gate_reads_the_threads_last_and_hands_them_to_review_gate`

## Step 6 — Hold stranded PRs whose threads refuse

- **Files**: `src/repoach/review/open_threads.py`, `src/repoach/cli/review_cmds.py`, `tests/unit/test_open_threads_block_the_merge.py`
- **Action**: hold_open_threads(rows, read) splits any rows with pr_number and head_sha into those to dispatch and HeldPR(pr_number, head_sha, reason) for each one its threads refuse. review stranded prints 'held #<n> (<reason>)' on stderr under --dispatch, lists held in its JSON and its table, and dispatches only the others.
- **Commit**: `feat(review): hold stranded prs with open review threads`
- **Done when**: pytest -q tests/unit/test_open_threads_block_the_merge.py -k 'hold or stranded' passes
- **Unit tests**: `tests/unit/test_open_threads_block_the_merge.py::test_hold_open_threads_keeps_back_every_pr_its_threads_refuse`, `tests/unit/test_open_threads_block_the_merge.py::test_stranded_dispatch_holds_a_green_pr_with_an_open_thread`, `tests/unit/test_open_threads_block_the_merge.py::test_an_unexpected_error_propagates_instead_of_holding_or_counting`

## Step 7 — Refuse a release whose PR has open threads

- **Files**: `src/repoach/review/release_gate.py`, `src/repoach/cli/release_cmds.py`, `tests/unit/test_open_threads_block_the_merge.py`
- **Action**: ReleaseFacts gains open_threads (None without --pr), read for the release PR; compute_release_decision appends OpenThreads.refusal(). The release gate JSON carries review_threads.
- **Commit**: `feat(release): refuse a release pr with open review threads`
- **Done when**: pytest -q tests/unit/test_open_threads_block_the_merge.py -k release passes
- **Unit tests**: `tests/unit/test_open_threads_block_the_merge.py::test_release_gate_refuses_open_or_unreadable_threads_on_the_release_pr`, `tests/unit/test_open_threads_block_the_merge.py::test_release_gate_reads_the_release_pr_threads_only_when_a_pr_is_given`

## Step 8 — Report the open threads in the review and its archive

- **Files**: `src/repoach/review/report.py`, `src/repoach/review/orchestrator.py`, `tests/unit/test_open_threads_block_the_merge.py`
- **Action**: TeamOutcome gains open_threads; _refresh_open_threads reads them unless already read, once per run: after posting and before the archive, else at the end of the run. The archive facts table gains 'Unresolved review threads' (unreadable when not read), its headline refuses as the gate does, and team_outcome_to_dict carries review_threads.
- **Commit**: `feat(review): report the open review threads`
- **Done when**: pytest -q tests/unit/test_open_threads_block_the_merge.py -k report passes
- **Unit tests**: `tests/unit/test_open_threads_block_the_merge.py::test_report_shows_the_thread_count_and_its_refusal`, `tests/unit/test_open_threads_block_the_merge.py::test_review_pr_reads_the_threads_once_and_rereads_once_after_a_failed_read`

## Step 9 — Print the threads from review pr and review gate

- **Files**: `src/repoach/cli/review_cmds.py`, `tests/fixtures/generated_artefacts/harness.py`, `tests/integration/test_open_threads_block_the_merge_end_to_end.py`
- **Action**: review pr prints 'review threads: <n> unresolved' on stderr and carries review_threads in its JSON. review gate prints the same line, then one 'open thread <id> at <file>:<line>' per thread, and carries facts.unresolved_threads, facts.threads_read and review_threads. The harness forge serves a threads list to gh api graphql.
- **Commit**: `feat(review): print the open review threads`
- **Done when**: pytest -q tests/integration/test_open_threads_block_the_merge_end_to_end.py passes
- **Unit tests**: `tests/unit/test_open_threads_block_the_merge.py::test_review_gate_names_every_thread_it_counted`, `tests/unit/test_open_threads_block_the_merge.py::test_thread_lines_cover_missing_root_path_and_line`, `tests/unit/test_open_threads_block_the_merge.py::test_a_file_name_or_error_from_the_forge_cannot_forge_a_line`

## Step 10 — Answer thread reads in the test forges

- **Files**: `tests/forge_fakes.py`, `tests/unit/test_merge_gate.py`, `tests/unit/test_review_team.py`
- **Action**: scripted_github(threads=()) answers a reviewThreads read before its run side; NO_OPEN_THREADS is the answer of a PR with no thread; no_open_threads(facts) is facts read from such a forge. The strict GitHub fakes of the gate, review and replay suites answer gh api graphql with NO_OPEN_THREADS, their duck forges gain unresolved_threads -> [], and facts built without a forge go through no_open_threads.
- **Commit**: `test(review): answer review thread reads in the test forges`
- **Done when**: pytest -q tests/unit/test_merge_gate.py tests/unit/test_review_team.py passes
- **Unit tests**: `tests/unit/test_merge_gate.py::test_decision_merges_when_all_green`, `tests/unit/test_review_team.py::test_review_pr_runs_team_and_publishes`

## Step 11 — Re-arm the merge when a thread is answered

- **Files**: `.github/workflows/merge-on-ci.yml`, `tests/unit/test_open_threads_block_the_merge.py`
- **Action**: GitHub Actions has no pull_request_review_thread trigger, so no workflow listens to it. A reply in a thread is a submitted pull_request_review, which re-runs auto-review.yml, and merge-on-ci.yml admits that run's completion; its header names this path, and that a thread resolved without a reply waits for the next CI or review completion or for review stranded --dispatch. A test holds every workflow to the events Actions offers.
- **Commit**: `ci(merge): re-arm the merge through a reply's review`
- **Done when**: pytest -q tests/unit/test_open_threads_block_the_merge.py -k 'workflow or rearms' passes
- **Unit tests**: `tests/unit/test_open_threads_block_the_merge.py::test_every_workflow_listens_only_to_events_actions_offers`, `tests/unit/test_open_threads_block_the_merge.py::test_a_reply_in_a_thread_rearms_the_merge_through_the_review_workflow`

## Step 12 — Document the rule and the forge settings

- **Files**: `docs/merge_gate.md`
- **Action**: docs/merge_gate.md lists every refusal reason of the gate, the open-threads rule, where it shows and the re-arm, and names required_review_thread_resolution and only_allow_merge_if_all_discussions_are_resolved as the second line of defence; the README's gate example links it.
- **Commit**: `docs(review): document that an open thread blocks the merge`
- **Done when**: docs/merge_gate.md names the rule and both forge settings
- **Unit tests**: _(docs-only step — none promised)_

## Integration tests

- `tests/integration/test_open_threads_block_the_merge_end_to_end.py::test_an_open_thread_refuses_a_green_pr_until_it_is_resolved`

<!-- repoach-action-plan -->
```json
{
  "spec_id": "SP-OPEN-THREADS-BLOCK-THE-MERGE",
  "title": "An open review thread blocks the merge on every merge path",
  "summary": "Both forges list every open review thread over every page (GitHub reviewThreads 100 per page, GitLab discussions page by page until an empty one); MergeFacts carries the count and whether it was read; decide_at_head reads it for every merge path, the stranded sweep holds a PR with open threads, the release gate reads the release PR's threads, review pr and review gate report them, a reply in a thread re-arms merge-on-ci, and the merge gate page names the forge settings as the second line of defence.",
  "steps": [
    {
      "index": 1,
      "title": "Declare the read and read every open GitHub thread",
      "files": [
        "src/repoach/forge/protocol.py",
        "src/repoach/forge/github.py",
        "tests/unit/test_forge_unresolved_threads.py"
      ],
      "action": "ReviewThread gains path ('') and line (None); Forge gains unresolved_threads(number) -> list[ReviewThread], every open thread over every page, ForgeError when the listing cannot be read whole. GitHubForge runs one gh api graphql --paginate --slurp query of reviewThreads(first:100,after:$endCursor) with pageInfo, isResolved, path, line, originalLine and the first comment's databaseId; unresolved_threads_from_github keeps each node not resolved (root id 0 without a databaseId, originalLine when line is null) and raises on no page, a page without data, thread connection, nodes list or boolean hasNextPage, a page carrying errors, a thread that is not an object, and a last page saying hasNextPage true. The tests build pages with the step 3 shapes.",
      "commit_message": "feat(forge): read every open github review thread",
      "done_when": "pytest -q tests/unit/test_forge_unresolved_threads.py -k github passes",
      "unit_tests": [
        "tests/unit/test_forge_unresolved_threads.py::test_github_reads_the_only_open_thread_on_page_three",
        "tests/unit/test_forge_unresolved_threads.py::test_github_counts_an_open_thread_without_a_root_id_and_falls_back_to_the_original_line",
        "tests/unit/test_forge_unresolved_threads.py::test_github_refuses_a_listing_it_cannot_count",
        "tests/unit/test_forge_unresolved_threads.py::test_github_failed_call_raises",
        "tests/unit/test_forge_unresolved_threads.py::test_github_open_thread_with_null_path_or_comments_keeps_empty_defaults"
      ]
    },
    {
      "index": 2,
      "title": "Read every open GitLab discussion, page by page",
      "files": [
        "src/repoach/forge/gitlab.py",
        "src/repoach/bench/local_forge.py",
        "tests/unit/test_forge_unresolved_threads.py"
      ],
      "action": "GitLabForge.unresolved_threads asks merge_requests/<n>/discussions?per_page=100&page=<p> from page 1 until an empty array, without glab --paginate, at most DISCUSSION_PAGES_MAX (50) pages, else ForgeError; a page that is not an array raises. unresolved_threads_from_discussions keeps each discussion whose first note is resolvable and not resolved, diff or general, path and line from its position, and raises on a discussion without a first note object. LocalDiffForge returns no thread.",
      "commit_message": "feat(forge): read every open gitlab discussion",
      "done_when": "pytest -q tests/unit/test_forge_unresolved_threads.py -k gitlab passes",
      "unit_tests": [
        "tests/unit/test_forge_unresolved_threads.py::test_gitlab_reads_pages_until_an_empty_one_and_finds_the_open_thread_on_page_three",
        "tests/unit/test_forge_unresolved_threads.py::test_gitlab_counts_a_general_resolvable_discussion",
        "tests/unit/test_forge_unresolved_threads.py::test_gitlab_refuses_a_listing_past_the_page_cap",
        "tests/unit/test_forge_unresolved_threads.py::test_gitlab_refuses_a_page_that_is_not_an_array_or_fails",
        "tests/unit/test_forge_unresolved_threads.py::test_gitlab_refuses_a_discussion_it_cannot_read"
      ]
    },
    {
      "index": 3,
      "title": "Shape the thread answers and the recorded results",
      "files": [
        "tests/forge_fakes.py",
        "tests/fixtures/forge/github/review_threads.json",
        "tests/fixtures/forge/gitlab/review_threads.json"
      ],
      "action": "tests/forge_fakes.py gains threads_page(nodes, more=False) and thread_node(root_id, resolved, path, line), the GraphQL reviewThreads shapes. The two recorded review_threads fixtures keep their exchanges; their expected results gain path and line at the defaults.",
      "commit_message": "test(forge): shape the review thread answers",
      "done_when": "pytest -q tests/unit/test_forge_unresolved_threads.py passes",
      "unit_tests": [
        "tests/unit/test_forge_unresolved_threads.py::test_github_reads_the_only_open_thread_on_page_three",
        "tests/unit/test_forge_unresolved_threads.py::test_gitlab_mr_without_discussions_reads_one_empty_page"
      ]
    },
    {
      "index": 4,
      "title": "Refuse on open or unreadable threads in the pure gate",
      "files": [
        "src/repoach/review/open_threads.py",
        "src/repoach/review/merge_gate.py",
        "tests/unit/test_open_threads_block_the_merge.py"
      ],
      "action": "open_threads.py holds OpenThreads (read, threads, error; count, refusal, summary_line, thread_lines, as_json), read_open_threads (a ForgeError becomes an unread result with the redacted reason), unresolved_reason and unreadable_reason. MergeFacts gains open_threads (unread by default), set by with_threads, and the read-only unresolved_threads, threads_read and threads_error derived from it; compute_merge_decision adds '<n> unresolved review thread(s)' while n > 0 and 'review threads unreadable: <why>' when not read.",
      "commit_message": "feat(review): refuse the merge on open review threads",
      "done_when": "pytest -q tests/unit/test_open_threads_block_the_merge.py -k 'refuses_and_zero or fail_closed or names_each' passes",
      "unit_tests": [
        "tests/unit/test_open_threads_block_the_merge.py::test_one_open_thread_refuses_and_zero_permits",
        "tests/unit/test_open_threads_block_the_merge.py::test_unread_threads_fail_closed",
        "tests/unit/test_open_threads_block_the_merge.py::test_read_open_threads_names_each_thread_or_the_failure"
      ]
    },
    {
      "index": 5,
      "title": "Read the threads on every merge path",
      "files": [
        "src/repoach/review/auto_merge.py",
        "tests/merge_wait_fakes.py",
        "tests/unit/test_open_threads_block_the_merge.py"
      ],
      "action": "decide_at_head reads the threads last, after the re-check and the ledger read and right before the decision, and folds them in with MergeFacts.with_threads; evaluate_merge_gate keeps facts.open_threads on GateEvaluation.open_threads. run_auto_merge records SKIP_GATE with 'pure gate refused: <n> unresolved review thread(s)' in its attempts comment. The fixture forge serves per-PR threads to gh api graphql.",
      "commit_message": "feat(review): read the open threads on every merge path",
      "done_when": "pytest -q tests/unit/test_open_threads_block_the_merge.py -k auto_merge passes",
      "unit_tests": [
        "tests/unit/test_open_threads_block_the_merge.py::test_auto_merge_refuses_a_green_pr_with_an_open_thread_then_merges_once_it_is_resolved",
        "tests/unit/test_open_threads_block_the_merge.py::test_auto_merge_refuses_when_the_threads_cannot_be_read",
        "tests/unit/test_open_threads_block_the_merge.py::test_the_gate_reads_the_threads_last_and_hands_them_to_review_gate"
      ]
    },
    {
      "index": 6,
      "title": "Hold stranded PRs whose threads refuse",
      "files": [
        "src/repoach/review/open_threads.py",
        "src/repoach/cli/review_cmds.py",
        "tests/unit/test_open_threads_block_the_merge.py"
      ],
      "action": "hold_open_threads(rows, read) splits any rows with pr_number and head_sha into those to dispatch and HeldPR(pr_number, head_sha, reason) for each one its threads refuse. review stranded prints 'held #<n> (<reason>)' on stderr under --dispatch, lists held in its JSON and its table, and dispatches only the others.",
      "commit_message": "feat(review): hold stranded prs with open review threads",
      "done_when": "pytest -q tests/unit/test_open_threads_block_the_merge.py -k 'hold or stranded' passes",
      "unit_tests": [
        "tests/unit/test_open_threads_block_the_merge.py::test_hold_open_threads_keeps_back_every_pr_its_threads_refuse",
        "tests/unit/test_open_threads_block_the_merge.py::test_stranded_dispatch_holds_a_green_pr_with_an_open_thread",
        "tests/unit/test_open_threads_block_the_merge.py::test_an_unexpected_error_propagates_instead_of_holding_or_counting"
      ]
    },
    {
      "index": 7,
      "title": "Refuse a release whose PR has open threads",
      "files": [
        "src/repoach/review/release_gate.py",
        "src/repoach/cli/release_cmds.py",
        "tests/unit/test_open_threads_block_the_merge.py"
      ],
      "action": "ReleaseFacts gains open_threads (None without --pr), read for the release PR; compute_release_decision appends OpenThreads.refusal(). The release gate JSON carries review_threads.",
      "commit_message": "feat(release): refuse a release pr with open review threads",
      "done_when": "pytest -q tests/unit/test_open_threads_block_the_merge.py -k release passes",
      "unit_tests": [
        "tests/unit/test_open_threads_block_the_merge.py::test_release_gate_refuses_open_or_unreadable_threads_on_the_release_pr",
        "tests/unit/test_open_threads_block_the_merge.py::test_release_gate_reads_the_release_pr_threads_only_when_a_pr_is_given"
      ]
    },
    {
      "index": 8,
      "title": "Report the open threads in the review and its archive",
      "files": [
        "src/repoach/review/report.py",
        "src/repoach/review/orchestrator.py",
        "tests/unit/test_open_threads_block_the_merge.py"
      ],
      "action": "TeamOutcome gains open_threads; _refresh_open_threads reads them unless already read, once per run: after posting and before the archive, else at the end of the run. The archive facts table gains 'Unresolved review threads' (unreadable when not read), its headline refuses as the gate does, and team_outcome_to_dict carries review_threads.",
      "commit_message": "feat(review): report the open review threads",
      "done_when": "pytest -q tests/unit/test_open_threads_block_the_merge.py -k report passes",
      "unit_tests": [
        "tests/unit/test_open_threads_block_the_merge.py::test_report_shows_the_thread_count_and_its_refusal",
        "tests/unit/test_open_threads_block_the_merge.py::test_review_pr_reads_the_threads_once_and_rereads_once_after_a_failed_read"
      ]
    },
    {
      "index": 9,
      "title": "Print the threads from review pr and review gate",
      "files": [
        "src/repoach/cli/review_cmds.py",
        "tests/fixtures/generated_artefacts/harness.py",
        "tests/integration/test_open_threads_block_the_merge_end_to_end.py"
      ],
      "action": "review pr prints 'review threads: <n> unresolved' on stderr and carries review_threads in its JSON. review gate prints the same line, then one 'open thread <id> at <file>:<line>' per thread, and carries facts.unresolved_threads, facts.threads_read and review_threads. The harness forge serves a threads list to gh api graphql.",
      "commit_message": "feat(review): print the open review threads",
      "done_when": "pytest -q tests/integration/test_open_threads_block_the_merge_end_to_end.py passes",
      "unit_tests": [
        "tests/unit/test_open_threads_block_the_merge.py::test_review_gate_names_every_thread_it_counted",
        "tests/unit/test_open_threads_block_the_merge.py::test_thread_lines_cover_missing_root_path_and_line",
        "tests/unit/test_open_threads_block_the_merge.py::test_a_file_name_or_error_from_the_forge_cannot_forge_a_line"
      ]
    },
    {
      "index": 10,
      "title": "Answer thread reads in the test forges",
      "files": [
        "tests/forge_fakes.py",
        "tests/unit/test_merge_gate.py",
        "tests/unit/test_review_team.py"
      ],
      "action": "scripted_github(threads=()) answers a reviewThreads read before its run side; NO_OPEN_THREADS is the answer of a PR with no thread; no_open_threads(facts) is facts read from such a forge. The strict GitHub fakes of the gate, review and replay suites answer gh api graphql with NO_OPEN_THREADS, their duck forges gain unresolved_threads -> [], and facts built without a forge go through no_open_threads.",
      "commit_message": "test(review): answer review thread reads in the test forges",
      "done_when": "pytest -q tests/unit/test_merge_gate.py tests/unit/test_review_team.py passes",
      "unit_tests": [
        "tests/unit/test_merge_gate.py::test_decision_merges_when_all_green",
        "tests/unit/test_review_team.py::test_review_pr_runs_team_and_publishes"
      ]
    },
    {
      "index": 11,
      "title": "Re-arm the merge when a thread is answered",
      "files": [
        ".github/workflows/merge-on-ci.yml",
        "tests/unit/test_open_threads_block_the_merge.py"
      ],
      "action": "GitHub Actions has no pull_request_review_thread trigger, so no workflow listens to it. A reply in a thread is a submitted pull_request_review, which re-runs auto-review.yml, and merge-on-ci.yml admits that run's completion; its header names this path, and that a thread resolved without a reply waits for the next CI or review completion or for review stranded --dispatch. A test holds every workflow to the events Actions offers.",
      "commit_message": "ci(merge): re-arm the merge through a reply's review",
      "done_when": "pytest -q tests/unit/test_open_threads_block_the_merge.py -k 'workflow or rearms' passes",
      "unit_tests": [
        "tests/unit/test_open_threads_block_the_merge.py::test_every_workflow_listens_only_to_events_actions_offers",
        "tests/unit/test_open_threads_block_the_merge.py::test_a_reply_in_a_thread_rearms_the_merge_through_the_review_workflow"
      ]
    },
    {
      "index": 12,
      "title": "Document the rule and the forge settings",
      "files": [
        "docs/merge_gate.md"
      ],
      "action": "docs/merge_gate.md lists every refusal reason of the gate, the open-threads rule, where it shows and the re-arm, and names required_review_thread_resolution and only_allow_merge_if_all_discussions_are_resolved as the second line of defence; the README's gate example links it.",
      "commit_message": "docs(review): document that an open thread blocks the merge",
      "done_when": "docs/merge_gate.md names the rule and both forge settings",
      "unit_tests": []
    }
  ],
  "integration_tests": [
    "tests/integration/test_open_threads_block_the_merge_end_to_end.py::test_an_open_thread_refuses_a_green_pr_until_it_is_resolved"
  ]
}
```
