# SP-OWNER-SETTLES-A-FINDING — A maintainer settles, with a recorded reason, a finding the refuter cannot judge

Forge.is_maintainer asks GitHub or GitLab whether a comment's author maintains the repository; review pr reads the pull request's comments before the re-check of older heads and applies each maintainer's /resolve <reason> or /reopen once, in the finding's thread or by #<finding-id> anywhere, moving the finding to owner_resolved, which the gate and the re-check never count, with the login, comment, reason and head recorded in owner_commands; every ignored command is answered once, replies are posted only by a posting run, and the report and JSON name each owner-resolved finding.

## Step 1 — Ask the forge whether a login is a maintainer

- **Files**: `src/repoach/forge/protocol.py`, `src/repoach/forge/github.py`, `tests/unit/test_forge_conformance.py`
- **Action**: Forge gains is_maintainer(login). protocol.py adds FORGE_LOGIN_RE and maintainer_candidate(bot_username, login), which refuses the bot, an empty login and anything that is not a plain username before a URL is built from it. GitHubForge reads repos/:owner/:repo/collaborators/<login>/permission and answers role_name admin or maintain (or permission admin); a 404 is no maintainer, any other failure raises ForgeError. Add the is_maintainer and is_maintainer_developer fixtures under tests/fixtures/forge/github, synthetic answers of the documented endpoint, and the conformance tests, parametrised over both forges.
- **Commit**: `feat(forge): ask github whether a login is a maintainer`
- **Done when**: pytest -q tests/unit/test_forge_conformance.py -k 'is_maintainer and github' passes
- **Unit tests**: `tests/unit/test_forge_conformance.py::test_is_maintainer`, `tests/unit/test_forge_conformance.py::test_is_maintainer_developer`, `tests/unit/test_forge_conformance.py::test_is_maintainer_never_asks_about_the_bot`

## Step 2 — Ask gitlab, keep the permission fields recordable, answer on the bench

- **Files**: `src/repoach/forge/gitlab.py`, `src/repoach/forge/recording.py`, `src/repoach/bench/local_forge.py`
- **Action**: GitLabForge reads members/all?query=<login>, keeps the entry whose username is exactly the login and answers access_level >= 40 (MAINTAINER_ACCESS_LEVEL); the same two fixtures are added under tests/fixtures/forge/gitlab. RECORDED_FIELDS keeps access_level, permission and role_name, so a fixture recorded by scripts/record_forge_fixture.py keeps the fields is_maintainer reads. The bench LocalForge answers is_maintainer with False: it has no repository members.
- **Commit**: `feat(forge): ask gitlab whether a login is a maintainer`
- **Done when**: pytest -q tests/unit/test_forge_conformance.py passes
- **Unit tests**: `tests/unit/test_forge_conformance.py::test_fixture_sets_are_paired`, `tests/unit/test_forge_conformance.py::test_is_maintainer`, `tests/unit/test_forge_conformance.py::test_is_maintainer_developer`

## Step 3 — Add the owner_resolved finding status

- **Files**: `src/repoach/review/findings.py`, `src/repoach/review/merge_gate.py`, `tests/unit/test_review_findings.py`
- **Action**: FindingStatus gains OWNER_RESOLVED; OWNER_SETTLEABLE_STATUSES names proposed, verified, open and stuck. ALLOWED_TRANSITIONS lets each of the four reach owner_resolved and owner_resolved return to any of them (a reopen restores the status held). merge_gate._SETTLED gains owner_resolved, so neither the gate nor the review-time facts count it; head_reverify's _UNSETTLED already leaves it out.
- **Commit**: `feat(review): add the owner_resolved finding status`
- **Done when**: pytest -q tests/unit/test_review_findings.py passes
- **Unit tests**: `tests/unit/test_review_findings.py::test_allowed_transitions_law`

## Step 4 — Parse, decide and record owner commands

- **Files**: `src/repoach/review/owner_store.py`, `src/repoach/review/owner_commands.py`, `tests/unit/test_owner_commands.py`
- **Action**: owner_store.py holds the owner_commands table (unique pr_number, comment_id), OwnerCommandRow, fetch_owner_commands, record_owner_command and mark_reply_posted. owner_commands.py holds parse_command (/resolve or /reopen, optional #<id>, the rest as the reason), apply_owner_commands (comments of both lists in creation order, the author asked once per run through Forge.is_maintainer, the reason of at least 20 characters, the target by #<id> or by the thread root's file and line, one answer per command, the finding moved with the previous status, method, result and checked sha recorded, replies posted only when post is true) and owner_settlements. A forge read failure applies nothing and leaves the command for the next run.
- **Commit**: `feat(review): apply owner resolve and reopen commands`
- **Done when**: pytest -q tests/unit/test_owner_commands.py passes
- **Unit tests**: `tests/unit/test_owner_commands.py::test_thread_reply_resolves_the_finding_it_anchors`, `tests/unit/test_owner_commands.py::test_pr_level_command_names_the_finding`, `tests/unit/test_owner_commands.py::test_resolve_without_reason_is_answered_and_ignored`, `tests/unit/test_owner_commands.py::test_non_owner_is_answered_once_and_ignored`, `tests/unit/test_owner_commands.py::test_a_command_is_applied_once_across_runs`

## Step 5 — Pin the ledger effect and the refusals

- **Files**: `tests/unit/test_owner_commands.py`
- **Action**: Unit tests over a real SQLite ledger and a forge seam serving comments: the gate facts stop counting an owner-resolved finding and the verdict flips, the re-check at head builds no judge for it, a reopen restores what the finding held, unknown ids, ambiguous threads, settled findings and unreadable permissions are answered and change nothing.
- **Commit**: `test(review): pin the owner command ledger effect and refusals`
- **Done when**: pytest -q tests/unit/test_owner_commands.py passes
- **Unit tests**: `tests/unit/test_owner_commands.py::test_owner_resolved_is_settled_and_not_counted`, `tests/unit/test_owner_commands.py::test_reverify_skips_an_owner_resolved_finding`, `tests/unit/test_owner_commands.py::test_reopen_counts_the_finding_again`, `tests/unit/test_owner_commands.py::test_unknown_finding_is_answered_and_ignored`, `tests/unit/test_owner_commands.py::test_ambiguous_thread_asks_for_the_id`

## Step 6 — Apply owner commands before the verdict

- **Files**: `src/repoach/review/orchestrator.py`, `src/repoach/review/report.py`, `tests/integration/test_owner_resolves_a_finding_end_to_end.py`
- **Action**: ReviewTeamOrchestrator._apply_owner_commands runs apply_owner_commands at the reviewed head with post set to the run's posting flag, before _reverify_older_heads, on the fresh path and on the replay of a completed run; a failure is logged and applies nothing. TeamOutcome carries owner_resolved, owner_commands and owner_lines. render_ledger_report gains owner_lines, rendered as a fenced Owner-resolved findings section.
- **Commit**: `feat(review): apply owner commands before the verdict`
- **Done when**: pytest -q tests/integration/test_owner_resolves_a_finding_end_to_end.py passes
- **Unit tests**: `tests/unit/test_owner_commands.py::test_report_line_and_json_name_the_owner`, `tests/unit/test_owner_commands.py::test_dry_run_prints_replies_and_a_posting_run_posts_them_once`, `tests/unit/test_owner_commands.py::test_unreadable_comments_apply_nothing`

## Step 7 — Report owner-resolved findings in review pr

- **Files**: `src/repoach/cli/review_cmds.py`, `tests/unit/test_review_ci_mode.py`
- **Action**: The review pr JSON gains owner_resolved and owner_commands, and stderr prints each owner line (owner-resolved #<id> by <login>: <reason>, owner commands: <n> applied, <m> ignored, owner reply not posted: <reply>).
- **Commit**: `feat(cli): report owner-resolved findings in review pr`
- **Done when**: pytest -q tests/unit/test_review_ci_mode.py passes
- **Unit tests**: `tests/unit/test_review_ci_mode.py::test_review_pr_json_payload_matches_documented_schema`, `tests/unit/test_owner_commands.py::test_github_and_gitlab_maintainers_are_owners_and_the_bot_never_is`

## Step 8 — Document owner commands

- **Files**: `docs/owner_commands.md`
- **Action**: Describe the two commands and both forms, who may use them and how the forge is asked, the mandatory reason and what is recorded, the answers, when they are applied and posted, the report lines, and what the commands never do.
- **Commit**: `docs(review): document owner resolve and reopen commands`
- **Done when**: docs/owner_commands.md names both commands, the owner rule and the record
- **Unit tests**: _(docs-only step — none promised)_

## Integration tests

- `tests/integration/test_owner_resolves_a_finding_end_to_end.py::test_owner_resolve_flips_the_verdict_at_the_same_head`

<!-- repoach-action-plan -->
```json
{
  "spec_id": "SP-OWNER-SETTLES-A-FINDING",
  "title": "A maintainer settles, with a recorded reason, a finding the refuter cannot judge",
  "summary": "Forge.is_maintainer asks GitHub or GitLab whether a comment's author maintains the repository; review pr reads the pull request's comments before the re-check of older heads and applies each maintainer's /resolve <reason> or /reopen once, in the finding's thread or by #<finding-id> anywhere, moving the finding to owner_resolved, which the gate and the re-check never count, with the login, comment, reason and head recorded in owner_commands; every ignored command is answered once, replies are posted only by a posting run, and the report and JSON name each owner-resolved finding.",
  "steps": [
    {
      "index": 1,
      "title": "Ask the forge whether a login is a maintainer",
      "files": [
        "src/repoach/forge/protocol.py",
        "src/repoach/forge/github.py",
        "tests/unit/test_forge_conformance.py"
      ],
      "action": "Forge gains is_maintainer(login). protocol.py adds FORGE_LOGIN_RE and maintainer_candidate(bot_username, login), which refuses the bot, an empty login and anything that is not a plain username before a URL is built from it. GitHubForge reads repos/:owner/:repo/collaborators/<login>/permission and answers role_name admin or maintain (or permission admin); a 404 is no maintainer, any other failure raises ForgeError. Add the is_maintainer and is_maintainer_developer fixtures under tests/fixtures/forge/github, synthetic answers of the documented endpoint, and the conformance tests, parametrised over both forges.",
      "commit_message": "feat(forge): ask github whether a login is a maintainer",
      "done_when": "pytest -q tests/unit/test_forge_conformance.py -k 'is_maintainer and github' passes",
      "unit_tests": [
        "tests/unit/test_forge_conformance.py::test_is_maintainer",
        "tests/unit/test_forge_conformance.py::test_is_maintainer_developer",
        "tests/unit/test_forge_conformance.py::test_is_maintainer_never_asks_about_the_bot"
      ]
    },
    {
      "index": 2,
      "title": "Ask gitlab, keep the permission fields recordable, answer on the bench",
      "files": [
        "src/repoach/forge/gitlab.py",
        "src/repoach/forge/recording.py",
        "src/repoach/bench/local_forge.py"
      ],
      "action": "GitLabForge reads members/all?query=<login>, keeps the entry whose username is exactly the login and answers access_level >= 40 (MAINTAINER_ACCESS_LEVEL); the same two fixtures are added under tests/fixtures/forge/gitlab. RECORDED_FIELDS keeps access_level, permission and role_name, so a fixture recorded by scripts/record_forge_fixture.py keeps the fields is_maintainer reads. The bench LocalForge answers is_maintainer with False: it has no repository members.",
      "commit_message": "feat(forge): ask gitlab whether a login is a maintainer",
      "done_when": "pytest -q tests/unit/test_forge_conformance.py passes",
      "unit_tests": [
        "tests/unit/test_forge_conformance.py::test_fixture_sets_are_paired",
        "tests/unit/test_forge_conformance.py::test_is_maintainer",
        "tests/unit/test_forge_conformance.py::test_is_maintainer_developer"
      ]
    },
    {
      "index": 3,
      "title": "Add the owner_resolved finding status",
      "files": [
        "src/repoach/review/findings.py",
        "src/repoach/review/merge_gate.py",
        "tests/unit/test_review_findings.py"
      ],
      "action": "FindingStatus gains OWNER_RESOLVED; OWNER_SETTLEABLE_STATUSES names proposed, verified, open and stuck. ALLOWED_TRANSITIONS lets each of the four reach owner_resolved and owner_resolved return to any of them (a reopen restores the status held). merge_gate._SETTLED gains owner_resolved, so neither the gate nor the review-time facts count it; head_reverify's _UNSETTLED already leaves it out.",
      "commit_message": "feat(review): add the owner_resolved finding status",
      "done_when": "pytest -q tests/unit/test_review_findings.py passes",
      "unit_tests": [
        "tests/unit/test_review_findings.py::test_allowed_transitions_law"
      ]
    },
    {
      "index": 4,
      "title": "Parse, decide and record owner commands",
      "files": [
        "src/repoach/review/owner_store.py",
        "src/repoach/review/owner_commands.py",
        "tests/unit/test_owner_commands.py"
      ],
      "action": "owner_store.py holds the owner_commands table (unique pr_number, comment_id), OwnerCommandRow, fetch_owner_commands, record_owner_command and mark_reply_posted. owner_commands.py holds parse_command (/resolve or /reopen, optional #<id>, the rest as the reason), apply_owner_commands (comments of both lists in creation order, the author asked once per run through Forge.is_maintainer, the reason of at least 20 characters, the target by #<id> or by the thread root's file and line, one answer per command, the finding moved with the previous status, method, result and checked sha recorded, replies posted only when post is true) and owner_settlements. A forge read failure applies nothing and leaves the command for the next run.",
      "commit_message": "feat(review): apply owner resolve and reopen commands",
      "done_when": "pytest -q tests/unit/test_owner_commands.py passes",
      "unit_tests": [
        "tests/unit/test_owner_commands.py::test_thread_reply_resolves_the_finding_it_anchors",
        "tests/unit/test_owner_commands.py::test_pr_level_command_names_the_finding",
        "tests/unit/test_owner_commands.py::test_resolve_without_reason_is_answered_and_ignored",
        "tests/unit/test_owner_commands.py::test_non_owner_is_answered_once_and_ignored",
        "tests/unit/test_owner_commands.py::test_a_command_is_applied_once_across_runs"
      ]
    },
    {
      "index": 5,
      "title": "Pin the ledger effect and the refusals",
      "files": [
        "tests/unit/test_owner_commands.py"
      ],
      "action": "Unit tests over a real SQLite ledger and a forge seam serving comments: the gate facts stop counting an owner-resolved finding and the verdict flips, the re-check at head builds no judge for it, a reopen restores what the finding held, unknown ids, ambiguous threads, settled findings and unreadable permissions are answered and change nothing.",
      "commit_message": "test(review): pin the owner command ledger effect and refusals",
      "done_when": "pytest -q tests/unit/test_owner_commands.py passes",
      "unit_tests": [
        "tests/unit/test_owner_commands.py::test_owner_resolved_is_settled_and_not_counted",
        "tests/unit/test_owner_commands.py::test_reverify_skips_an_owner_resolved_finding",
        "tests/unit/test_owner_commands.py::test_reopen_counts_the_finding_again",
        "tests/unit/test_owner_commands.py::test_unknown_finding_is_answered_and_ignored",
        "tests/unit/test_owner_commands.py::test_ambiguous_thread_asks_for_the_id"
      ]
    },
    {
      "index": 6,
      "title": "Apply owner commands before the verdict",
      "files": [
        "src/repoach/review/orchestrator.py",
        "src/repoach/review/report.py",
        "tests/integration/test_owner_resolves_a_finding_end_to_end.py"
      ],
      "action": "ReviewTeamOrchestrator._apply_owner_commands runs apply_owner_commands at the reviewed head with post set to the run's posting flag, before _reverify_older_heads, on the fresh path and on the replay of a completed run; a failure is logged and applies nothing. TeamOutcome carries owner_resolved, owner_commands and owner_lines. render_ledger_report gains owner_lines, rendered as a fenced Owner-resolved findings section.",
      "commit_message": "feat(review): apply owner commands before the verdict",
      "done_when": "pytest -q tests/integration/test_owner_resolves_a_finding_end_to_end.py passes",
      "unit_tests": [
        "tests/unit/test_owner_commands.py::test_report_line_and_json_name_the_owner",
        "tests/unit/test_owner_commands.py::test_dry_run_prints_replies_and_a_posting_run_posts_them_once",
        "tests/unit/test_owner_commands.py::test_unreadable_comments_apply_nothing"
      ]
    },
    {
      "index": 7,
      "title": "Report owner-resolved findings in review pr",
      "files": [
        "src/repoach/cli/review_cmds.py",
        "tests/unit/test_review_ci_mode.py"
      ],
      "action": "The review pr JSON gains owner_resolved and owner_commands, and stderr prints each owner line (owner-resolved #<id> by <login>: <reason>, owner commands: <n> applied, <m> ignored, owner reply not posted: <reply>).",
      "commit_message": "feat(cli): report owner-resolved findings in review pr",
      "done_when": "pytest -q tests/unit/test_review_ci_mode.py passes",
      "unit_tests": [
        "tests/unit/test_review_ci_mode.py::test_review_pr_json_payload_matches_documented_schema",
        "tests/unit/test_owner_commands.py::test_github_and_gitlab_maintainers_are_owners_and_the_bot_never_is"
      ]
    },
    {
      "index": 8,
      "title": "Document owner commands",
      "files": [
        "docs/owner_commands.md"
      ],
      "action": "Describe the two commands and both forms, who may use them and how the forge is asked, the mandatory reason and what is recorded, the answers, when they are applied and posted, the report lines, and what the commands never do.",
      "commit_message": "docs(review): document owner resolve and reopen commands",
      "done_when": "docs/owner_commands.md names both commands, the owner rule and the record",
      "unit_tests": []
    }
  ],
  "integration_tests": [
    "tests/integration/test_owner_resolves_a_finding_end_to_end.py::test_owner_resolve_flips_the_verdict_at_the_same_head"
  ]
}
```
