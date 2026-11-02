# SP-FINDING-THREAD-IDS — Persist the forge comment and thread ids of every posted finding

Every inline comment the review posts for a recorded finding writes one finding_posts row with the forge's own ids (GitHub review comment id, root comment id and batched review id; GitLab note id and discussion id), read from the answer of the post that created it. The refuted-finding sentinel and the owner commands find a finding's thread through that row first and fall back to (file, line) only for findings with no row. repoach review backfill-threads links legacy findings strictly. Each review run records the recorded threads the forge reports resolved by the bot or a maintainer and prints thread closed by <login> for #<id>, without settling any finding.

## Step 1 — Let a GraphQL query through the read path

- **Files**: `src/repoach/forge/protocol.py`, `tests/unit/test_forge_write_barrier.py`
- **Action**: In protocol.py add _graphql_read(args): an api graphql argv sent as POST (or with no method) is a read only when it carries a single query= field whose document starts with query or { and names no mutation or subscription anywhere, and no operationName field; a document read from a file (query=@...), two documents or none are writes. is_write_argv returns False for such a read before its api rule. Extend the is_write_argv parametrised cases with each of those shapes.
- **Commit**: `feat(forge): let a graphql query through the read path`
- **Done when**: pytest -q tests/unit/test_forge_write_barrier.py passes
- **Unit tests**: `tests/unit/test_forge_write_barrier.py::test_is_write_argv`

## Step 2 — Read review threads and who resolved them on both forges

- **Files**: `src/repoach/forge/github.py`, `src/repoach/forge/gitlab.py`, `tests/unit/test_forge_conformance.py`
- **Action**: protocol.py gains ReviewThread (root_id, thread_id, resolved, resolved_by) and Forge.review_threads. GitHubForge.review_threads runs one paginated, slurped gh api graphql reviewThreads query (isResolved, resolvedBy.login, the first comment's databaseId), normalised by review_threads_from_github; GitLabForge.review_threads reads the discussions on their first DiffNote (resolved, resolved_by.username) through review_threads_from_discussions; the bench LocalDiffForge returns none. recording.py keeps the new GraphQL and GitLab keys. Record review_threads fixtures for GitHub (#278) and GitLab (!339, two discussions kept) and add the conformance case plus an open-thread and no-root parse case.
- **Commit**: `feat(forge): read review threads and who resolved them`
- **Done when**: pytest -q tests/unit/test_forge_conformance.py tests/unit/test_forge_surface.py passes
- **Unit tests**: `tests/unit/test_forge_conformance.py::test_review_threads`, `tests/unit/test_forge_conformance.py::test_review_threads_reads_an_open_thread_and_skips_one_without_a_root`, `tests/unit/test_forge_conformance.py::test_review_threads_raises_forge_error_on_bad_json_or_a_failed_call`

## Step 3 — Store finding posts, thread closures and backfill links

- **Files**: `src/repoach/review/finding_posts.py`, `tests/unit/test_finding_posts.py`, `tests/unit/test_finding_posts_backfill.py`
- **Action**: finding_posts.py owns the finding_posts table (unique finding_id and comment_id; thread_closed_at_head and thread_closed_by nullable) created by init_finding_posts_schema, which adds any column an older copy lacks. record_finding_post refuses a finding id the PR does not have (FindingPostIntegrityError) and returns the stored id for a comment already stored; fetch_finding_posts, resolve_thread (the latest root post), thread_id_of, ThreadIndex and thread_index match an inline root to its findings by root id or discussion id. backfill_finding_posts links a finding to the one bot root at its exact file and line that no other finding claims, and reports the rest unmatched. detect_closed_threads reads Forge.review_threads only when a root post has no closure, records a thread resolved by the bot or a maintainer and reports anyone else's resolution as not counted.
- **Commit**: `feat(review): store finding posts, closures and backfill links`
- **Done when**: pytest -q tests/unit/test_finding_posts.py tests/unit/test_finding_posts_backfill.py passes
- **Unit tests**: `tests/unit/test_finding_posts.py::test_a_post_for_an_unknown_finding_is_refused`, `tests/unit/test_finding_posts.py::test_an_older_table_gains_the_missing_columns`, `tests/unit/test_finding_posts.py::test_a_thread_resolved_by_a_maintainer_is_recorded_and_settles_nothing`, `tests/unit/test_finding_posts.py::test_a_thread_resolved_by_someone_else_is_reported_not_recorded`, `tests/unit/test_finding_posts_backfill.py::test_backfill_is_strict_and_leaves_unmatched_unknown`

## Step 4 — Record the forge ids of every posted finding

- **Files**: `src/repoach/review/orchestrator.py`, `src/repoach/review/finding_post_record.py`, `tests/unit/test_finding_posts.py`
- **Action**: finding_post_record.py (apart from finding_posts.py, which findings_bridge's import of thread_context would otherwise close a cycle on) holds posted_finding_id (the finding of that head with the comment's fingerprint, the reviewer's own first), pair_posted (a batched review's created comments paired with their drafts on path, line and body, else on a unique path and line, never guessed) and record_posted_comments (one posted row per comment with the comment id, the thread id - the root id on GitHub, the discussion id on GitLab - and the GitHub review id; a comment with no finding or no id writes nothing and a failed write is logged). The orchestrator calls it after a batched review or a per-comment post, creates the table on every run and, after the owner commands, calls detect_closed_threads on both the fresh and the replay path; TeamOutcome carries finding_posts, threads_closed and thread_lines, and the archive report renders thread_lines as a Closed threads section.
- **Commit**: `feat(review): record the forge ids of every posted finding`
- **Done when**: pytest -q tests/unit/test_finding_posts.py -k 'posted or gitlab or reviewers or without_an_id' passes
- **Unit tests**: `tests/unit/test_finding_posts.py::test_each_posted_comment_records_one_row_with_the_forge_ids`, `tests/unit/test_finding_posts.py::test_gitlab_post_records_the_discussion_and_note_ids`, `tests/unit/test_finding_posts.py::test_two_reviewers_on_one_line_get_two_rows`, `tests/unit/test_finding_posts.py::test_a_forge_answer_without_an_id_writes_no_row`, `tests/unit/test_finding_posts.py::test_pair_posted_falls_back_to_a_unique_line_and_skips_an_ambiguous_one`

## Step 5 — Report finding posts and closed threads in review pr

- **Files**: `src/repoach/cli/review_cmds.py`, `tests/unit/test_review_ci_mode.py`, `tests/unit/test_finding_posts.py`
- **Action**: The review pr JSON gains finding_posts and threads_closed, and each thread line is printed on stderr after the owner lines; the documented-schema test requires both keys. A dry run through the review harness writes no row, leaves the table in the ledger and reports finding_posts 0.
- **Commit**: `feat(cli): report finding posts and closed threads in review pr`
- **Done when**: pytest -q tests/unit/test_review_ci_mode.py tests/unit/test_finding_posts.py passes
- **Unit tests**: `tests/unit/test_review_ci_mode.py::test_review_pr_json_payload_matches_documented_schema`, `tests/unit/test_finding_posts.py::test_dry_run_writes_no_post_and_reports_zero`

## Step 6 — Link legacy findings with review backfill-threads

- **Files**: `src/repoach/cli/review_cmds.py`, `tests/unit/test_finding_posts_backfill.py`
- **Action**: Add repoach review backfill-threads <N> [--dry-run] [--forge]: select the forge, run backfill_finding_posts with the forge's bot login and print its one line; a forge that cannot be selected or read exits 2 and writes nothing.
- **Commit**: `feat(cli): link legacy findings with review backfill-threads`
- **Done when**: pytest -q tests/unit/test_finding_posts_backfill.py passes
- **Unit tests**: `tests/unit/test_finding_posts_backfill.py::test_backfill_threads_cli_dry_run_writes_nothing`, `tests/unit/test_finding_posts_backfill.py::test_backfill_threads_cli_unreachable_forge_exits_2`

## Step 7 — Post the refuted-finding sentinel on the recorded thread

- **Files**: `src/repoach/review/thread_context.py`, `tests/unit/test_findings_sentinel_rehome.py`
- **Action**: post_refuted_finding_sentinels answers on resolve_thread's recorded root first; a finding with no recorded thread falls back to _match_finding_thread over _line_candidates and writes a backfill row only when exactly one root sits at its file and line and the bot wrote it.
- **Commit**: `feat(review): post the sentinel on the finding's recorded thread`
- **Done when**: pytest -q tests/unit/test_findings_sentinel_rehome.py passes
- **Unit tests**: `tests/unit/test_findings_sentinel_rehome.py::test_recorded_thread_receives_the_sentinel_when_its_root_line_shifted`, `tests/unit/test_findings_sentinel_rehome.py::test_legacy_line_match_is_recorded_as_a_backfill_row`, `tests/unit/test_findings_sentinel_rehome.py::test_a_root_someone_else_opened_is_never_recorded`

## Step 8 — Settle an owner command on the recorded thread

- **Files**: `src/repoach/review/owner_commands.py`, `tests/unit/test_owner_commands.py`, `tests/integration/test_finding_thread_ids_end_to_end.py`
- **Action**: apply_owner_commands reads thread_index once; _target addresses the findings whose recorded post opened the reply's thread and falls back to the anchor only over findings with no post. The end-to-end test posts a Sentinel finding through the harness GitHub, checks its row, moves the root to another line, and settles it with a maintainer's /resolve reply at the next run, which ends APPROVE.
- **Commit**: `feat(review): settle an owner command on the recorded thread`
- **Done when**: pytest -q tests/unit/test_owner_commands.py tests/integration/test_finding_thread_ids_end_to_end.py passes
- **Unit tests**: `tests/unit/test_owner_commands.py::test_resolve_reply_settles_the_recorded_finding_after_its_line_moved`

## Step 9 — Document finding threads and the outbox successor

- **Files**: `docs/finding_threads.md`, `docs/owner_commands.md`, `docs/tech_debt.md`
- **Action**: finding_threads.md describes the columns per forge and where each id comes from, who reads the table, thread closures and why a closed thread settles nothing, review backfill-threads, and the dsh-superagent prior art; owner_commands.md says a thread is joined by its recorded post first; tech-debt row 105 records the outbox with receipts as the successor of the marker scan.
- **Commit**: `docs(review): document finding threads and their closures`
- **Done when**: docs/finding_threads.md names every column, the closure rule and the backfill command
- **Unit tests**: _(docs-only step — none promised)_

## Integration tests

- `tests/integration/test_finding_thread_ids_end_to_end.py::test_a_resolve_reply_settles_the_posted_finding_by_its_recorded_thread`

<!-- repoach-action-plan -->
```json
{
  "spec_id": "SP-FINDING-THREAD-IDS",
  "title": "Persist the forge comment and thread ids of every posted finding",
  "summary": "Every inline comment the review posts for a recorded finding writes one finding_posts row with the forge's own ids (GitHub review comment id, root comment id and batched review id; GitLab note id and discussion id), read from the answer of the post that created it. The refuted-finding sentinel and the owner commands find a finding's thread through that row first and fall back to (file, line) only for findings with no row. repoach review backfill-threads links legacy findings strictly. Each review run records the recorded threads the forge reports resolved by the bot or a maintainer and prints thread closed by <login> for #<id>, without settling any finding.",
  "steps": [
    {
      "index": 1,
      "title": "Let a GraphQL query through the read path",
      "files": [
        "src/repoach/forge/protocol.py",
        "tests/unit/test_forge_write_barrier.py"
      ],
      "action": "In protocol.py add _graphql_read(args): an api graphql argv sent as POST (or with no method) is a read only when it carries a single query= field whose document starts with query or { and names no mutation or subscription anywhere, and no operationName field; a document read from a file (query=@...), two documents or none are writes. is_write_argv returns False for such a read before its api rule. Extend the is_write_argv parametrised cases with each of those shapes.",
      "commit_message": "feat(forge): let a graphql query through the read path",
      "done_when": "pytest -q tests/unit/test_forge_write_barrier.py passes",
      "unit_tests": [
        "tests/unit/test_forge_write_barrier.py::test_is_write_argv"
      ]
    },
    {
      "index": 2,
      "title": "Read review threads and who resolved them on both forges",
      "files": [
        "src/repoach/forge/github.py",
        "src/repoach/forge/gitlab.py",
        "tests/unit/test_forge_conformance.py"
      ],
      "action": "protocol.py gains ReviewThread (root_id, thread_id, resolved, resolved_by) and Forge.review_threads. GitHubForge.review_threads runs one paginated, slurped gh api graphql reviewThreads query (isResolved, resolvedBy.login, the first comment's databaseId), normalised by review_threads_from_github; GitLabForge.review_threads reads the discussions on their first DiffNote (resolved, resolved_by.username) through review_threads_from_discussions; the bench LocalDiffForge returns none. recording.py keeps the new GraphQL and GitLab keys. Record review_threads fixtures for GitHub (#278) and GitLab (!339, two discussions kept) and add the conformance case plus an open-thread and no-root parse case.",
      "commit_message": "feat(forge): read review threads and who resolved them",
      "done_when": "pytest -q tests/unit/test_forge_conformance.py tests/unit/test_forge_surface.py passes",
      "unit_tests": [
        "tests/unit/test_forge_conformance.py::test_review_threads",
        "tests/unit/test_forge_conformance.py::test_review_threads_reads_an_open_thread_and_skips_one_without_a_root",
        "tests/unit/test_forge_conformance.py::test_review_threads_raises_forge_error_on_bad_json_or_a_failed_call"
      ]
    },
    {
      "index": 3,
      "title": "Store finding posts, thread closures and backfill links",
      "files": [
        "src/repoach/review/finding_posts.py",
        "tests/unit/test_finding_posts.py",
        "tests/unit/test_finding_posts_backfill.py"
      ],
      "action": "finding_posts.py owns the finding_posts table (unique finding_id and comment_id; thread_closed_at_head and thread_closed_by nullable) created by init_finding_posts_schema, which adds any column an older copy lacks. record_finding_post refuses a finding id the PR does not have (FindingPostIntegrityError) and returns the stored id for a comment already stored; fetch_finding_posts, resolve_thread (the latest root post), thread_id_of, ThreadIndex and thread_index match an inline root to its findings by root id or discussion id. backfill_finding_posts links a finding to the one bot root at its exact file and line that no other finding claims, and reports the rest unmatched. detect_closed_threads reads Forge.review_threads only when a root post has no closure, records a thread resolved by the bot or a maintainer and reports anyone else's resolution as not counted.",
      "commit_message": "feat(review): store finding posts, closures and backfill links",
      "done_when": "pytest -q tests/unit/test_finding_posts.py tests/unit/test_finding_posts_backfill.py passes",
      "unit_tests": [
        "tests/unit/test_finding_posts.py::test_a_post_for_an_unknown_finding_is_refused",
        "tests/unit/test_finding_posts.py::test_an_older_table_gains_the_missing_columns",
        "tests/unit/test_finding_posts.py::test_a_thread_resolved_by_a_maintainer_is_recorded_and_settles_nothing",
        "tests/unit/test_finding_posts.py::test_a_thread_resolved_by_someone_else_is_reported_not_recorded",
        "tests/unit/test_finding_posts_backfill.py::test_backfill_is_strict_and_leaves_unmatched_unknown"
      ]
    },
    {
      "index": 4,
      "title": "Record the forge ids of every posted finding",
      "files": [
        "src/repoach/review/orchestrator.py",
        "src/repoach/review/finding_post_record.py",
        "tests/unit/test_finding_posts.py"
      ],
      "action": "finding_post_record.py (apart from finding_posts.py, which findings_bridge's import of thread_context would otherwise close a cycle on) holds posted_finding_id (the finding of that head with the comment's fingerprint, the reviewer's own first), pair_posted (a batched review's created comments paired with their drafts on path, line and body, else on a unique path and line, never guessed) and record_posted_comments (one posted row per comment with the comment id, the thread id - the root id on GitHub, the discussion id on GitLab - and the GitHub review id; a comment with no finding or no id writes nothing and a failed write is logged). The orchestrator calls it after a batched review or a per-comment post, creates the table on every run and, after the owner commands, calls detect_closed_threads on both the fresh and the replay path; TeamOutcome carries finding_posts, threads_closed and thread_lines, and the archive report renders thread_lines as a Closed threads section.",
      "commit_message": "feat(review): record the forge ids of every posted finding",
      "done_when": "pytest -q tests/unit/test_finding_posts.py -k 'posted or gitlab or reviewers or without_an_id' passes",
      "unit_tests": [
        "tests/unit/test_finding_posts.py::test_each_posted_comment_records_one_row_with_the_forge_ids",
        "tests/unit/test_finding_posts.py::test_gitlab_post_records_the_discussion_and_note_ids",
        "tests/unit/test_finding_posts.py::test_two_reviewers_on_one_line_get_two_rows",
        "tests/unit/test_finding_posts.py::test_a_forge_answer_without_an_id_writes_no_row",
        "tests/unit/test_finding_posts.py::test_pair_posted_falls_back_to_a_unique_line_and_skips_an_ambiguous_one"
      ]
    },
    {
      "index": 5,
      "title": "Report finding posts and closed threads in review pr",
      "files": [
        "src/repoach/cli/review_cmds.py",
        "tests/unit/test_review_ci_mode.py",
        "tests/unit/test_finding_posts.py"
      ],
      "action": "The review pr JSON gains finding_posts and threads_closed, and each thread line is printed on stderr after the owner lines; the documented-schema test requires both keys. A dry run through the review harness writes no row, leaves the table in the ledger and reports finding_posts 0.",
      "commit_message": "feat(cli): report finding posts and closed threads in review pr",
      "done_when": "pytest -q tests/unit/test_review_ci_mode.py tests/unit/test_finding_posts.py passes",
      "unit_tests": [
        "tests/unit/test_review_ci_mode.py::test_review_pr_json_payload_matches_documented_schema",
        "tests/unit/test_finding_posts.py::test_dry_run_writes_no_post_and_reports_zero"
      ]
    },
    {
      "index": 6,
      "title": "Link legacy findings with review backfill-threads",
      "files": [
        "src/repoach/cli/review_cmds.py",
        "tests/unit/test_finding_posts_backfill.py"
      ],
      "action": "Add repoach review backfill-threads <N> [--dry-run] [--forge]: select the forge, run backfill_finding_posts with the forge's bot login and print its one line; a forge that cannot be selected or read exits 2 and writes nothing.",
      "commit_message": "feat(cli): link legacy findings with review backfill-threads",
      "done_when": "pytest -q tests/unit/test_finding_posts_backfill.py passes",
      "unit_tests": [
        "tests/unit/test_finding_posts_backfill.py::test_backfill_threads_cli_dry_run_writes_nothing",
        "tests/unit/test_finding_posts_backfill.py::test_backfill_threads_cli_unreachable_forge_exits_2"
      ]
    },
    {
      "index": 7,
      "title": "Post the refuted-finding sentinel on the recorded thread",
      "files": [
        "src/repoach/review/thread_context.py",
        "tests/unit/test_findings_sentinel_rehome.py"
      ],
      "action": "post_refuted_finding_sentinels answers on resolve_thread's recorded root first; a finding with no recorded thread falls back to _match_finding_thread over _line_candidates and writes a backfill row only when exactly one root sits at its file and line and the bot wrote it.",
      "commit_message": "feat(review): post the sentinel on the finding's recorded thread",
      "done_when": "pytest -q tests/unit/test_findings_sentinel_rehome.py passes",
      "unit_tests": [
        "tests/unit/test_findings_sentinel_rehome.py::test_recorded_thread_receives_the_sentinel_when_its_root_line_shifted",
        "tests/unit/test_findings_sentinel_rehome.py::test_legacy_line_match_is_recorded_as_a_backfill_row",
        "tests/unit/test_findings_sentinel_rehome.py::test_a_root_someone_else_opened_is_never_recorded"
      ]
    },
    {
      "index": 8,
      "title": "Settle an owner command on the recorded thread",
      "files": [
        "src/repoach/review/owner_commands.py",
        "tests/unit/test_owner_commands.py",
        "tests/integration/test_finding_thread_ids_end_to_end.py"
      ],
      "action": "apply_owner_commands reads thread_index once; _target addresses the findings whose recorded post opened the reply's thread and falls back to the anchor only over findings with no post. The end-to-end test posts a Sentinel finding through the harness GitHub, checks its row, moves the root to another line, and settles it with a maintainer's /resolve reply at the next run, which ends APPROVE.",
      "commit_message": "feat(review): settle an owner command on the recorded thread",
      "done_when": "pytest -q tests/unit/test_owner_commands.py tests/integration/test_finding_thread_ids_end_to_end.py passes",
      "unit_tests": [
        "tests/unit/test_owner_commands.py::test_resolve_reply_settles_the_recorded_finding_after_its_line_moved"
      ]
    },
    {
      "index": 9,
      "title": "Document finding threads and the outbox successor",
      "files": [
        "docs/finding_threads.md",
        "docs/owner_commands.md",
        "docs/tech_debt.md"
      ],
      "action": "finding_threads.md describes the columns per forge and where each id comes from, who reads the table, thread closures and why a closed thread settles nothing, review backfill-threads, and the dsh-superagent prior art; owner_commands.md says a thread is joined by its recorded post first; tech-debt row 105 records the outbox with receipts as the successor of the marker scan.",
      "commit_message": "docs(review): document finding threads and their closures",
      "done_when": "docs/finding_threads.md names every column, the closure rule and the backfill command",
      "unit_tests": []
    }
  ],
  "integration_tests": [
    "tests/integration/test_finding_thread_ids_end_to_end.py::test_a_resolve_reply_settles_the_posted_finding_by_its_recorded_thread"
  ]
}
```
