# SP-OPEN-THREADS-BLOCK-THE-MERGE — An open review thread blocks the merge on every merge path

Both forges list every open review thread over every page (GitHub reviewThreads 100 per page, GitLab discussions page by page until an empty one); MergeFacts carries the count and whether it was read; decide_at_head reads it for every merge path, the stranded sweep holds a PR with open threads, the release gate reads the release PR's threads, review pr and review gate report them, merge-on-ci re-judges on a resolved thread, and the merge gate page names the forge settings as the second line of defence.

## Step 1 — Read every open review thread on both forges

- **Files**: `src/repoach/forge/protocol.py`, `src/repoach/forge/github.py`, `src/repoach/forge/gitlab.py`, `src/repoach/bench/local_forge.py`, `tests/forge_fakes.py`, `tests/fixtures/forge/github/review_threads.json`, `tests/fixtures/forge/gitlab/review_threads.json`, `tests/unit/test_forge_unresolved_threads.py`
- **Action**: ReviewThread gains path and line. Forge gains unresolved_threads(number) -> list[ReviewThread]. GitHub: one gh api graphql --paginate --slurp query of reviewThreads(first:100) with isResolved path line originalLine and the first comment's databaseId; unresolved_threads_from_github keeps every node not resolved (root id 0 when it has none) and raises ForgeError on a page without data, without a thread connection, on no page, and when the last page says hasNextPage true. GitLab: merge_requests/<n>/discussions?per_page=100&page=<p> from page 1 until an empty array, at most DISCUSSION_PAGES_MAX (50) pages, else ForgeError; unresolved_threads_from_discussions keeps each discussion whose first note is resolvable and not resolved, path and line from its position. LocalDiffForge returns none. tests/forge_fakes.py gains threads_page and thread_node; the recorded review_threads results gain path and line at their defaults.
- **Commit**: `feat(forge): read every open review thread on both forges`
- **Done when**: pytest -q tests/unit/test_forge_unresolved_threads.py passes
- **Unit tests**: `tests/unit/test_forge_unresolved_threads.py::test_github_reads_the_only_open_thread_on_page_three`, `tests/unit/test_forge_unresolved_threads.py::test_github_counts_an_open_thread_without_a_root_id_and_falls_back_to_the_original_line`, `tests/unit/test_forge_unresolved_threads.py::test_github_refuses_a_listing_it_cannot_count`, `tests/unit/test_forge_unresolved_threads.py::test_github_failed_call_raises`, `tests/unit/test_forge_unresolved_threads.py::test_gitlab_reads_pages_until_an_empty_one_and_finds_the_open_thread_on_page_three`, `tests/unit/test_forge_unresolved_threads.py::test_gitlab_counts_a_general_resolvable_discussion`, `tests/unit/test_forge_unresolved_threads.py::test_gitlab_refuses_a_listing_past_the_page_cap`, `tests/unit/test_forge_unresolved_threads.py::test_gitlab_refuses_a_page_that_is_not_an_array_or_fails`

## Step 2 — Refuse the merge while a review thread is open

- **Files**: `src/repoach/review/open_threads.py`, `src/repoach/review/merge_gate.py`, `src/repoach/review/auto_merge.py`, `src/repoach/review/release_gate.py`, `src/repoach/review/report.py`, `src/repoach/review/orchestrator.py`, `src/repoach/cli/review_cmds.py`, `src/repoach/cli/release_cmds.py`, `tests/forge_fakes.py`, `tests/merge_wait_fakes.py`, `tests/fixtures/generated_artefacts/harness.py`, `tests/unit/test_open_threads_block_the_merge.py`, `tests/integration/test_open_threads_block_the_merge_end_to_end.py`
- **Action**: open_threads.py holds OpenThreads (read, threads, error; refusal, facts_update, summary_line, thread_lines, as_json), read_open_threads (a ForgeError becomes an unread result with the redacted reason), unresolved_reason, unreadable_reason and hold_open_threads. MergeFacts gains unresolved_threads, threads_read (default False) and threads_error; compute_merge_decision adds '<n> unresolved review thread(s)' or 'review threads unreadable: <why>'. decide_at_head reads the threads unless given open_threads; evaluate_merge_gate reads them once and keeps them on GateEvaluation. review stranded holds a stranded PR its threads refuse ('held #<n> (<reason>)', JSON held). ReleaseFacts gains unresolved_threads and threads_error, read for --pr. review pr prints 'review threads: <n> unresolved' and carries review_threads in its JSON and archive; the facts table gains 'Unresolved review threads'; review gate prints the line and one 'open thread <id> at <file>:<line>' per thread. The test forges answer a thread read with no open thread (scripted_github threads, NO_OPEN_THREADS, no_open_threads for facts built without a forge), the fixture forges serve per-PR threads.
- **Commit**: `feat(review): refuse the merge while a review thread is open`
- **Done when**: pytest -q tests/unit/test_open_threads_block_the_merge.py tests/integration/test_open_threads_block_the_merge_end_to_end.py passes
- **Unit tests**: `tests/unit/test_open_threads_block_the_merge.py::test_one_open_thread_refuses_and_zero_permits`, `tests/unit/test_open_threads_block_the_merge.py::test_unread_threads_fail_closed`, `tests/unit/test_open_threads_block_the_merge.py::test_read_open_threads_names_each_thread_or_the_failure`, `tests/unit/test_open_threads_block_the_merge.py::test_auto_merge_refuses_a_green_pr_with_an_open_thread_then_merges_once_it_is_resolved`, `tests/unit/test_open_threads_block_the_merge.py::test_auto_merge_refuses_when_the_threads_cannot_be_read`, `tests/unit/test_open_threads_block_the_merge.py::test_hold_open_threads_keeps_back_every_pr_its_threads_refuse`, `tests/unit/test_open_threads_block_the_merge.py::test_stranded_dispatch_holds_a_green_pr_with_an_open_thread`, `tests/unit/test_open_threads_block_the_merge.py::test_release_gate_refuses_open_or_unreadable_threads_on_the_release_pr`, `tests/unit/test_open_threads_block_the_merge.py::test_report_shows_the_thread_count_and_its_refusal`

## Step 3 — Re-judge the merge when a review thread is resolved

- **Files**: `.github/workflows/merge-on-ci.yml`, `tests/unit/test_open_threads_block_the_merge.py`
- **Action**: merge-on-ci.yml gains pull_request_review_thread (resolved, unresolved), admitted for github-actions[bot] or an allowlisted actor on a same-repo, non-draft PR into develop; the concurrency group falls back to github.event.pull_request.head.ref and the resolve step reads the PR number from the event.
- **Commit**: `ci(merge): re-judge the merge when a review thread is resolved`
- **Done when**: pytest -q tests/unit/test_open_threads_block_the_merge.py -k merge_on_ci passes
- **Unit tests**: `tests/unit/test_open_threads_block_the_merge.py::test_merge_on_ci_rearms_on_a_resolved_thread`

## Step 4 — Document the rule and the forge settings

- **Files**: `docs/merge_gate.md`, `README.md`
- **Action**: docs/merge_gate.md lists every refusal reason of the gate, the open-threads rule, where it shows, the re-arm, and names required_review_thread_resolution and only_allow_merge_if_all_discussions_are_resolved as the second line of defence; the README's gate example carries unresolved_threads and links the page.
- **Commit**: `docs(review): document that an open thread blocks the merge`
- **Done when**: docs/merge_gate.md names the rule and both forge settings

## Integration tests

- `tests/integration/test_open_threads_block_the_merge_end_to_end.py::test_an_open_thread_refuses_a_green_pr_until_it_is_resolved`

<!-- repoach-action-plan -->
```json
{
  "spec_id": "SP-OPEN-THREADS-BLOCK-THE-MERGE",
  "title": "An open review thread blocks the merge on every merge path",
  "summary": "Both forges list every open review thread over every page (GitHub reviewThreads 100 per page, GitLab discussions page by page until an empty one); MergeFacts carries the count and whether it was read; decide_at_head reads it for every merge path, the stranded sweep holds a PR with open threads, the release gate reads the release PR's threads, review pr and review gate report them, merge-on-ci re-judges on a resolved thread, and the merge gate page names the forge settings as the second line of defence.",
  "steps": [
    {
      "index": 1,
      "title": "Read every open review thread on both forges",
      "files": [
        "src/repoach/forge/protocol.py",
        "src/repoach/forge/github.py",
        "src/repoach/forge/gitlab.py",
        "src/repoach/bench/local_forge.py",
        "tests/forge_fakes.py",
        "tests/fixtures/forge/github/review_threads.json",
        "tests/fixtures/forge/gitlab/review_threads.json",
        "tests/unit/test_forge_unresolved_threads.py"
      ],
      "action": "ReviewThread gains path and line. Forge gains unresolved_threads(number) -> list[ReviewThread]. GitHub: one gh api graphql --paginate --slurp query of reviewThreads(first:100) with isResolved path line originalLine and the first comment's databaseId; unresolved_threads_from_github keeps every node not resolved (root id 0 when it has none) and raises ForgeError on a page without data, without a thread connection, on no page, and when the last page says hasNextPage true. GitLab: merge_requests/<n>/discussions?per_page=100&page=<p> from page 1 until an empty array, at most DISCUSSION_PAGES_MAX (50) pages, else ForgeError; unresolved_threads_from_discussions keeps each discussion whose first note is resolvable and not resolved, path and line from its position. LocalDiffForge returns none. tests/forge_fakes.py gains threads_page and thread_node; the recorded review_threads results gain path and line at their defaults.",
      "commit_message": "feat(forge): read every open review thread on both forges",
      "done_when": "pytest -q tests/unit/test_forge_unresolved_threads.py passes",
      "unit_tests": [
        "tests/unit/test_forge_unresolved_threads.py::test_github_reads_the_only_open_thread_on_page_three",
        "tests/unit/test_forge_unresolved_threads.py::test_github_counts_an_open_thread_without_a_root_id_and_falls_back_to_the_original_line",
        "tests/unit/test_forge_unresolved_threads.py::test_github_refuses_a_listing_it_cannot_count",
        "tests/unit/test_forge_unresolved_threads.py::test_github_failed_call_raises",
        "tests/unit/test_forge_unresolved_threads.py::test_gitlab_reads_pages_until_an_empty_one_and_finds_the_open_thread_on_page_three",
        "tests/unit/test_forge_unresolved_threads.py::test_gitlab_counts_a_general_resolvable_discussion",
        "tests/unit/test_forge_unresolved_threads.py::test_gitlab_refuses_a_listing_past_the_page_cap",
        "tests/unit/test_forge_unresolved_threads.py::test_gitlab_refuses_a_page_that_is_not_an_array_or_fails"
      ]
    },
    {
      "index": 2,
      "title": "Refuse the merge while a review thread is open",
      "files": [
        "src/repoach/review/open_threads.py",
        "src/repoach/review/merge_gate.py",
        "src/repoach/review/auto_merge.py",
        "src/repoach/review/release_gate.py",
        "src/repoach/review/report.py",
        "src/repoach/review/orchestrator.py",
        "src/repoach/cli/review_cmds.py",
        "src/repoach/cli/release_cmds.py",
        "tests/forge_fakes.py",
        "tests/merge_wait_fakes.py",
        "tests/fixtures/generated_artefacts/harness.py",
        "tests/unit/test_open_threads_block_the_merge.py",
        "tests/integration/test_open_threads_block_the_merge_end_to_end.py"
      ],
      "action": "open_threads.py holds OpenThreads (read, threads, error; refusal, facts_update, summary_line, thread_lines, as_json), read_open_threads (a ForgeError becomes an unread result with the redacted reason), unresolved_reason, unreadable_reason and hold_open_threads. MergeFacts gains unresolved_threads, threads_read (default False) and threads_error; compute_merge_decision adds '<n> unresolved review thread(s)' or 'review threads unreadable: <why>'. decide_at_head reads the threads unless given open_threads; evaluate_merge_gate reads them once and keeps them on GateEvaluation. review stranded holds a stranded PR its threads refuse ('held #<n> (<reason>)', JSON held). ReleaseFacts gains unresolved_threads and threads_error, read for --pr. review pr prints 'review threads: <n> unresolved' and carries review_threads in its JSON and archive; the facts table gains 'Unresolved review threads'; review gate prints the line and one 'open thread <id> at <file>:<line>' per thread. The test forges answer a thread read with no open thread (scripted_github threads, NO_OPEN_THREADS, no_open_threads for facts built without a forge), the fixture forges serve per-PR threads.",
      "commit_message": "feat(review): refuse the merge while a review thread is open",
      "done_when": "pytest -q tests/unit/test_open_threads_block_the_merge.py tests/integration/test_open_threads_block_the_merge_end_to_end.py passes",
      "unit_tests": [
        "tests/unit/test_open_threads_block_the_merge.py::test_one_open_thread_refuses_and_zero_permits",
        "tests/unit/test_open_threads_block_the_merge.py::test_unread_threads_fail_closed",
        "tests/unit/test_open_threads_block_the_merge.py::test_read_open_threads_names_each_thread_or_the_failure",
        "tests/unit/test_open_threads_block_the_merge.py::test_auto_merge_refuses_a_green_pr_with_an_open_thread_then_merges_once_it_is_resolved",
        "tests/unit/test_open_threads_block_the_merge.py::test_auto_merge_refuses_when_the_threads_cannot_be_read",
        "tests/unit/test_open_threads_block_the_merge.py::test_hold_open_threads_keeps_back_every_pr_its_threads_refuse",
        "tests/unit/test_open_threads_block_the_merge.py::test_stranded_dispatch_holds_a_green_pr_with_an_open_thread",
        "tests/unit/test_open_threads_block_the_merge.py::test_release_gate_refuses_open_or_unreadable_threads_on_the_release_pr",
        "tests/unit/test_open_threads_block_the_merge.py::test_report_shows_the_thread_count_and_its_refusal"
      ]
    },
    {
      "index": 3,
      "title": "Re-judge the merge when a review thread is resolved",
      "files": [
        ".github/workflows/merge-on-ci.yml",
        "tests/unit/test_open_threads_block_the_merge.py"
      ],
      "action": "merge-on-ci.yml gains pull_request_review_thread (resolved, unresolved), admitted for github-actions[bot] or an allowlisted actor on a same-repo, non-draft PR into develop; the concurrency group falls back to github.event.pull_request.head.ref and the resolve step reads the PR number from the event.",
      "commit_message": "ci(merge): re-judge the merge when a review thread is resolved",
      "done_when": "pytest -q tests/unit/test_open_threads_block_the_merge.py -k merge_on_ci passes",
      "unit_tests": [
        "tests/unit/test_open_threads_block_the_merge.py::test_merge_on_ci_rearms_on_a_resolved_thread"
      ]
    },
    {
      "index": 4,
      "title": "Document the rule and the forge settings",
      "files": [
        "docs/merge_gate.md",
        "README.md"
      ],
      "action": "docs/merge_gate.md lists every refusal reason of the gate, the open-threads rule, where it shows, the re-arm, and names required_review_thread_resolution and only_allow_merge_if_all_discussions_are_resolved as the second line of defence; the README's gate example carries unresolved_threads and links the page.",
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
