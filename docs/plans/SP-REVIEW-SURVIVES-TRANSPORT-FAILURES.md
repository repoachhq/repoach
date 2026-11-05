# SP-REVIEW-SURVIVES-TRANSPORT-FAILURES — A short transport outage costs the review time, and a request fault is never taken for one

A claude_code reply past its output cap is named, keeps the CLI's message, trips no breaker and reaches the caller as a non-retryable GatewayOutputCapError, the CLI cap carrying 32000 tokens of thinking headroom; the CI job proxy keeps its breaker to the job and a retired configured model is named at start; each model call retries a non-4xx transport error with exponential backoff inside a 300 s window counted once per call, after which the outcome and exit 3 are unchanged; review pr names a transport-only incomplete review in its JSON, and auto-review.yml retries such a run once per head through a recorded workflow_dispatch; a thread reply runs a rearm job that merge-on-ci admits instead of a full review, and an owner command in a reply is still reviewed.

## Step 1 — Carry the HTTP status on a transport error

- **Files**: `src/repoach/providers/errors.py`, `src/repoach/agent_engine/adapters.py`, `tests/unit/test_proxy_gateway_client.py`
- **Action**: GatewayTransportError gains an optional keyword status (int or None), kept as an attribute; the gateway client passes the response status for a 5xx, 408 or 429. A timeout or a refused connection carries None.
- **Commit**: `feat(provider): carry the http status on a transport error`
- **Done when**: pytest -q tests/unit/test_proxy_gateway_client.py passes
- **Unit tests**: `tests/unit/test_proxy_gateway_client.py::test_call_raises_transport_error_on_retryable_status`, `tests/unit/test_proxy_gateway_client.py::test_call_raises_transport_error_on_connect_error`

## Step 2 — Carry the upstream status on a direct provider's transport error

- **Files**: `src/repoach/providers/direct.py`, `tests/unit/test_provider_direct_anthropic.py`
- **Action**: The direct providers pass the upstream status of an in-band error event and of a raised ProviderError to GatewayTransportError.
- **Commit**: `feat(provider): carry the upstream status on a direct transport error`
- **Done when**: pytest -q tests/unit/test_provider_direct_anthropic.py passes
- **Unit tests**: `tests/unit/test_provider_direct_anthropic.py::test_server_error_is_a_transport_error`

## Step 3 — Add the transport retry window setting

- **Files**: `src/repoach/project/config.py`, `src/repoach/project/repoach.toml.template`, `tests/unit/test_project_config.py`
- **Action**: ProviderSection gains transport_retry_window_s, an int from 0 to TRANSPORT_RETRY_WINDOW_MAX_S (1800), default TRANSPORT_RETRY_WINDOW_S (300), a boolean refused; the template documents the key under [provider] with a commented line.
- **Commit**: `feat(config): add the transport retry window under provider`
- **Done when**: pytest -q tests/unit/test_project_config.py passes
- **Unit tests**: `tests/unit/test_project_config.py::TestTemplate::test_every_key_has_a_template_line_and_round_trips`

## Step 4 — Let the environment set the window

- **Files**: `src/repoach/project/resolve.py`, `tests/unit/test_review_survives_transport_failures.py`
- **Action**: ENV_NAMES maps provider.transport_retry_window_s to REPOACH_TRANSPORT_RETRY_WINDOW_S, which outranks repoach.toml and is validated with the key's bounds, its error naming the variable.
- **Commit**: `feat(config): let the environment set the transport retry window`
- **Done when**: pytest -q tests/unit/test_review_survives_transport_failures.py -k environment passes
- **Unit tests**: `tests/unit/test_review_survives_transport_failures.py::test_the_window_comes_from_the_environment_over_the_file`

## Step 5 — Retry transport errors inside the window of one call

- **Files**: `src/repoach/agent_engine/transport_window.py`, `src/repoach/agent_engine/agent_loop.py`, `tests/unit/test_review_survives_transport_failures.py`
- **Action**: transport_window.py holds retryable_transport (a GatewayTransportError whose status is not a 4xx), backoff_s (5 s doubling, capped at 60 s), TransportWindow (window_s, an injectable clock, next_wait returning the wait cut to the time left or None) and the window_spent marker. AgentLoop reads the window from route.section and keeps _sleep and _clock seams; _provider_call records one usage row per attempt, logs agent_loop.transport_retry, sleeps and retries while next_wait gives a wait, then re-raises the last error unchanged, marked spent and logged agent_loop.transport_window_spent when the window was open; _call_turn_with_retry re-raises a spent error at once.
- **Commit**: `feat(agent): retry a transport error inside a per-call window`
- **Done when**: pytest -q tests/unit/test_review_survives_transport_failures.py -k 'window or answers' passes
- **Unit tests**: `tests/unit/test_review_survives_transport_failures.py::test_a_transport_failure_shorter_than_the_window_is_retried_until_it_answers`, `tests/unit/test_review_survives_transport_failures.py::test_a_failure_longer_than_the_window_ends_the_call_as_before`, `tests/unit/test_review_survives_transport_failures.py::test_a_closed_window_makes_one_attempt`, `tests/unit/test_review_survives_transport_failures.py::test_a_4xx_or_a_non_transport_error_is_never_retried`

## Step 6 — Spend one window per call in the outer retry loops

- **Files**: `src/repoach/review/retry_backoff.py`, `tests/conftest.py`
- **Action**: The suite's conftest sets REPOACH_TRANSPORT_RETRY_WINDOW_S=0 for every test, so the fake providers that raise transport errors keep their single attempt; the window tests open it themselves. retry_with_backoff stops on an exception marked window_spent, logged log_scope.window_spent, returning an exhausted, not fatal, RetryResult, so the reviewer's summary stays '[parse_failed:TRANSPORT] all 3 attempts raised — last=GatewayTransportError' and the judge's deferral 'judge call failed: GatewayTransportError'.
- **Commit**: `feat(review): spend one transport window per model call`
- **Done when**: pytest -q tests/unit/test_review_survives_transport_failures.py -k 'reviewer or judge' passes
- **Unit tests**: `tests/unit/test_review_survives_transport_failures.py::test_a_reviewer_spends_one_window_and_reports_the_same_line`, `tests/unit/test_review_survives_transport_failures.py::test_the_judge_after_the_window_defers_with_the_same_reason`

## Step 7 — Name why an incomplete review is incomplete

- **Files**: `src/repoach/review/review_retry.py`, `src/repoach/cli/review_cmds.py`
- **Action**: review_retry.incomplete_cause(final_verdict, reviews, refutations) returns None for a complete review, transport when every errored reviewer's summary names GatewayTransportError or GatewayChainExhausted (the exhausted-retries summary or the crash summary) and every deferred refutation reads 'judge call failed: <one of them>', other otherwise. review pr prints it in its JSON as incomplete_cause; the exit code is unchanged.
- **Commit**: `feat(review): name the cause of an incomplete review`
- **Done when**: pytest -q tests/unit/test_review_survives_transport_failures.py -k 'incomplete_cause or exits_3' passes
- **Unit tests**: `tests/unit/test_review_survives_transport_failures.py::test_incomplete_cause_is_transport_only_when_every_failure_is_transport`, `tests/unit/test_review_survives_transport_failures.py::test_review_pr_still_exits_3_and_names_the_transport_cause`

## Step 8 — Retry a transport-only failure once per head

- **Files**: `src/repoach/review/review_retry.py`, `src/repoach/cli/review_cmds.py`
- **Action**: dispatch_review_retry reads the PR (refused when not open or a draft, or when its head moved), reads the merge attempts comment (refused when unreadable or when a REVIEW_RETRY attempt exists at the head), records a REVIEW_RETRY attempt naming the failed run (refused when it cannot be written) and dispatches auto-review.yml with pr_number and retry_of. record_retry_ran records the retry run's id and result. repoach review retry <N> --head --run [--retry-of --result] runs one or the other and prints one stderr line, exit 0, or 2 on a forge failure or a refused dispatch.
- **Commit**: `feat(review): retry a transport-only review failure once per head`
- **Done when**: pytest -q tests/unit/test_review_survives_transport_failures.py -k retr passes
- **Unit tests**: `tests/unit/test_review_survives_transport_failures.py::test_one_retry_is_dispatched_then_the_head_is_never_retried_again`, `tests/unit/test_review_survives_transport_failures.py::test_the_attempts_comment_names_both_runs`, `tests/unit/test_review_survives_transport_failures.py::test_a_moved_head_or_a_closed_pr_is_not_retried`, `tests/unit/test_review_survives_transport_failures.py::test_review_retry_cli_dispatches_once`

## Step 9 — Re-arm the merge on a reply without reviewing

- **Files**: `.github/workflows/auto-review.yml`, `tests/github_concurrency_sim.py`, `tests/unit/test_auto_review_replies_do_not_review.py`
- **Action**: auto-review.yml listens to pull_request_review_comment created; the coalesce job takes owner comments and owner /resolve or /reopen replies only; a new rearm job (ubuntu-latest, no permission, group rearm-<pr> with cancel-in-progress, sleep 60) runs on an owner's submitted review; the review job skips every pull_request_review event and a review comment that is not a coalesced command. The simulator gains startsWith and the review_comment_event, pull_request_event and dispatch_event helpers.
- **Commit**: `ci(review): re-arm the merge on a reply instead of reviewing`
- **Done when**: pytest -q tests/unit/test_auto_review_replies_do_not_review.py -k 'reply or review_event or command' passes
- **Unit tests**: `tests/unit/test_auto_review_replies_do_not_review.py::test_a_reply_never_starts_a_bot_review`, `tests/unit/test_auto_review_replies_do_not_review.py::test_a_reply_does_not_cancel_the_review_of_a_push`, `tests/unit/test_auto_review_replies_do_not_review.py::test_an_owner_command_in_a_reply_is_reviewed_and_plain_text_is_not`, `tests/unit/test_auto_review_replies_do_not_review.py::test_a_reply_run_concludes_success_and_merge_on_ci_admits_it`, `tests/unit/test_auto_review_replies_do_not_review.py::test_no_review_event_reaches_the_review_job`

## Step 10 — Retry the review job once on transport errors

- **Files**: `.github/workflows/auto-review.yml`, `tests/unit/test_auto_review_replies_do_not_review.py`
- **Action**: The review job outputs head_sha and incomplete_cause read from its JSON; a retry job (ubuntu-latest, base ref, actions and pull-requests write, no provider key) runs when the review failed with incomplete_cause transport and no retry_of, sleeps 300 s and runs review retry, or, in a run dispatched by github-actions[bot] with retry_of, records the retry run. The review and auto_merge jobs admit that dispatch; auto_fix stays owner-only. workflow_dispatch gains the retry_of input.
- **Commit**: `ci(review): retry a review incomplete on transport errors once`
- **Done when**: pytest -q tests/unit/test_auto_review_replies_do_not_review.py passes
- **Unit tests**: `tests/unit/test_auto_review_replies_do_not_review.py::test_the_retry_job_runs_once_per_failed_head_and_never_retries_a_retry`, `tests/unit/test_auto_review_replies_do_not_review.py::test_the_retry_run_reviews_and_merges_but_never_pushes`, `tests/unit/test_auto_review_replies_do_not_review.py::test_a_bot_dispatch_without_retry_of_reviews_nothing`, `tests/unit/test_auto_review_replies_do_not_review.py::test_a_push_still_cancels_the_stale_review_of_a_push`

## Step 11 — Name a claude_code reply past its cap and keep the CLI's message

- **Files**: `src/repoach/llm_proxy/providers/exceptions.py`, `src/repoach/llm_proxy/providers/claude_code/client.py`, `tests/unit/test_claude_code_output_cap.py`
- **Action**: exceptions.py gains OutputCapExceededError (a ProviderError, status 422, error_type output_cap_exceeded). The claude_code client builds every CLI error through cli_exit_error and cli_result_error: a non-zero exit whose stdout is the CLI's JSON with is_error reads it, the message keeps the CLI's result text up to CLI_ERROR_TEXT_MAX_CHARS (2000) instead of the first 200 characters of stdout, and api_error max_output_tokens raises OutputCapExceededError; other exits keep stderr, else the head of stdout. _spawn_env sets CLAUDE_CODE_MAX_OUTPUT_TOKENS to max_tokens plus CLI_THINKING_HEADROOM_TOKENS (32000).
- **Commit**: `fix(proxy): name a claude reply past its output cap`
- **Done when**: pytest -q tests/unit/test_claude_code_output_cap.py passes
- **Unit tests**: `tests/unit/test_claude_code_output_cap.py::test_max_tokens_reaches_the_cli_as_its_output_cap`, `tests/unit/test_claude_code_output_cap.py::test_cap_rides_on_the_filtered_environment`, `tests/unit/test_claude_code_output_cap.py::test_disabled_thinking_reaches_the_cli_as_a_zero_budget`

## Step 12 — Trip nothing on a request fault and tell the caller not to repeat it

- **Files**: `src/repoach/llm_proxy/api/services.py`, `src/repoach/llm_proxy/api/models/agent_v1.py`, `tests/unit/test_proxy_chain_exhaustion.py`
- **Action**: services classifies OutputCapExceededError as output_cap_exceeded, a member of NO_TRIP_REASONS whose failure trips no breaker, and ends an exhausted chain whose last error is one with an output_cap_exceeded error event; a preflight-refused chain answers 502 with Retry-After, the seconds until its first candidate re-opens. The AgentError code literal gains output_cap_exceeded.
- **Commit**: `fix(proxy): never trip or repeat a reply past its cap`
- **Done when**: pytest -q tests/unit/test_proxy_chain_exhaustion.py passes
- **Unit tests**: `tests/unit/test_proxy_chain_exhaustion.py::test_preflight_exhaustion_returns_502`, `tests/unit/test_proxy_chain_exhaustion.py::test_exhaustion_emits_terminal_sse_error`

## Step 13 — Raise a reply past its cap as not retryable on the agent side

- **Files**: `src/repoach/llm_proxy/api/agent_dispatcher.py`, `src/repoach/agent_engine/adapters.py`, `tests/unit/test_proxy_output_cap_is_not_transport.py`
- **Action**: aggregate_sse_stream records the error event's type; dispatch_agent_request returns an output_cap_exceeded error as stop_reason error with that AgentError code. providers.errors gains GatewayOutputCapError (retryable False) and GatewayTransportError.retry_after_s. ProxyGatewayClient raises GatewayOutputCapError for an output_cap_exceeded error response and reads Retry-After into retry_after_s. TransportWindow waits a Retry-After that ends inside the window and gives up on one that does not.
- **Commit**: `fix(agent): never retry a reply past its output cap`
- **Done when**: pytest -q tests/unit/test_proxy_output_cap_is_not_transport.py -k 'cap or reopens' passes
- **Unit tests**: `tests/unit/test_proxy_output_cap_is_not_transport.py::test_the_cli_error_keeps_its_result_text_and_names_the_cap`, `tests/unit/test_proxy_output_cap_is_not_transport.py::test_an_output_cap_fault_trips_nothing_and_reaches_the_caller_as_not_retryable`, `tests/unit/test_proxy_output_cap_is_not_transport.py::test_the_reviewer_asks_once_and_the_review_is_not_retried_as_transport`, `tests/unit/test_proxy_output_cap_is_not_transport.py::test_a_tripped_chain_names_when_it_reopens_and_the_window_waits_only_that_long`

## Step 14 — Map a reply past its cap on the direct claude_code route

- **Files**: `src/repoach/providers/direct.py`, `tests/fixtures/claude_code/max_output_tokens.json`, `tests/unit/test_proxy_output_cap_is_not_transport.py`
- **Action**: The in-process providers map OutputCapExceededError to GatewayOutputCapError before any other CLI error, so the direct claude_code route never retries it as a transport error. The fixture is the JSON a real claude 2.1.317 printed with CLAUDE_CODE_MAX_OUTPUT_TOKENS=300.
- **Commit**: `fix(provider): map a direct reply past its cap to a refusal`
- **Done when**: pytest -q tests/unit/test_proxy_output_cap_is_not_transport.py -k direct passes
- **Unit tests**: `tests/unit/test_proxy_output_cap_is_not_transport.py::test_the_direct_claude_code_route_raises_the_same_error_once`

## Step 15 — Scope the CI breaker to the job and name a retired model at start

- **Files**: `src/repoach/llm_proxy/api/runtime.py`, `.github/workflows/auto-review.yml`, `tests/unit/test_proxy_output_cap_is_not_transport.py`
- **Action**: Both job proxy starts of auto-review.yml set REPOACH_BREAKER_PROBE_SEED_DB to runner.temp/job_proxy_breaker.db, so a job never rehydrates another job's trips. runtime.report_configured_models_gone, called at startup, logs one proxy_configured_model_gone error per configured MODEL_* entry the breaker holds with a terminal reason, naming its chains and the hours left, and returns the lines.
- **Commit**: `fix(ci): keep the job proxy breaker to its job`
- **Done when**: pytest -q tests/unit/test_proxy_output_cap_is_not_transport.py -k 'retired or job_proxy' passes
- **Unit tests**: `tests/unit/test_proxy_output_cap_is_not_transport.py::test_a_configured_model_the_provider_retired_is_named_at_start`, `tests/unit/test_proxy_output_cap_is_not_transport.py::test_the_job_proxy_keeps_its_breaker_to_the_job`

## Step 16 — Name which model served each role

- **Files**: `src/repoach/providers/usage.py`, `src/repoach/cli/review_cmds.py`, `tests/unit/test_review_survives_transport_failures.py`
- **Action**: usage.served_by_line(run) groups the run's usage rows by role, in the order each first called, naming the models that answered and, when calls failed, how many and their error classes, or none served. review pr prints it on stderr after its usage line.
- **Commit**: `feat(review): name which model served each review role`
- **Done when**: pytest -q tests/unit/test_review_survives_transport_failures.py -k served passes
- **Unit tests**: `tests/unit/test_review_survives_transport_failures.py::test_the_report_names_which_model_served_each_role_and_what_failed`

## Step 17 — Pin how the judge's chain resolved on #288

- **Files**: `tests/unit/test_proxy_output_cap_is_not_transport.py`
- **Action**: The judge asks the opus tier through the alias claude-opus-4-7; with nvidia_nim and open_router tripped the opus chain serves claude_code/opus, and with that tail tripped too it falls back to its head nvidia_nim/z-ai/glm-5.2, the chain the job log names; an unclassified name resolves the DEFAULT chain, which has no tail.
- **Commit**: `test(proxy): pin the opus chain the judge resolved on 288`
- **Done when**: pytest -q tests/unit/test_proxy_output_cap_is_not_transport.py -k judge_asks passes
- **Unit tests**: `tests/unit/test_proxy_output_cap_is_not_transport.py::test_the_judge_asks_the_opus_tier_and_288s_chain_was_its_tripped_tail`

## Step 18 — Replay #288's outage through the whole review

- **Files**: `tests/integration/test_review_survives_transport_failures_end_to_end.py`
- **Action**: Each reviewer's real AgentLoop talks to a provider answering the breaker's 502 until its own fake clock passes the outage, then approving; review pr runs through the CLI over the GitHub forge whose gh transport is the review-team fake. A 220 s outage completes with four approvals and incomplete_cause null; a longer one exits 3 with incomplete_cause transport and today's reviewer summary.
- **Commit**: `test(review): replay the 288 outage end to end`
- **Done when**: pytest -q tests/integration/test_review_survives_transport_failures_end_to_end.py passes
- **Unit tests**: `tests/integration/test_review_survives_transport_failures_end_to_end.py::test_288s_outage_inside_the_window_costs_time_and_the_review_completes`, `tests/integration/test_review_survives_transport_failures_end_to_end.py::test_an_outage_past_the_window_still_exits_3_and_names_transport`

## Step 19 — Hold the merge-wait workflow tests to the new triggers

- **Files**: `tests/unit/test_merge_wait_workflows.py`
- **Action**: The fourteen-event and #246 bursts now complete one rearm per burst and no review; the shape test reads the bot filter on the coalesce job's comment author and checks the rearm job's runner, group and review-author filter.
- **Commit**: `test(ci): pin the merge-wait bursts to the rearm job`
- **Done when**: pytest -q tests/unit/test_merge_wait_workflows.py passes
- **Unit tests**: `tests/unit/test_merge_wait_workflows.py::test_fourteen_review_events_coalesce`, `tests/unit/test_merge_wait_workflows.py::test_246_bursts_each_review_once`, `tests/unit/test_merge_wait_workflows.py::test_workflow_shape`

## Step 20 — Document the window, the retry and the triggers

- **Files**: `docs/merge_gate.md`, `docs/tech_debt.md`
- **Action**: merge_gate.md gains a table of what starts a review and what re-arms a merge, and a section on the transport window, Retry-After, the output-cap fault, the job-scoped breaker, the incomplete_cause field and the one automatic retry; the thread section says a reply runs the rearm job. tech_debt.md row 107, opened with the #288 measurement, is removed; rows 108 to 110 stay open with their measurements (the DEFAULT and literal chains' tail, the CI probe seed, the provider health line).
- **Commit**: `docs(review): document the transport window and the review triggers`
- **Done when**: docs/merge_gate.md names the window, the retry, the review triggers and the re-arm
- **Unit tests**: _(docs-only step — none promised)_

## Integration tests

- `tests/integration/test_review_survives_transport_failures_end_to_end.py::test_288s_outage_inside_the_window_costs_time_and_the_review_completes`
- `tests/integration/test_review_survives_transport_failures_end_to_end.py::test_an_outage_past_the_window_still_exits_3_and_names_transport`

<!-- repoach-action-plan -->
```json
{
  "spec_id": "SP-REVIEW-SURVIVES-TRANSPORT-FAILURES",
  "title": "A short transport outage costs the review time, and a request fault is never taken for one",
  "summary": "A claude_code reply past its output cap is named, keeps the CLI's message, trips no breaker and reaches the caller as a non-retryable GatewayOutputCapError, the CLI cap carrying 32000 tokens of thinking headroom; the CI job proxy keeps its breaker to the job and a retired configured model is named at start; each model call retries a non-4xx transport error with exponential backoff inside a 300 s window counted once per call, after which the outcome and exit 3 are unchanged; review pr names a transport-only incomplete review in its JSON, and auto-review.yml retries such a run once per head through a recorded workflow_dispatch; a thread reply runs a rearm job that merge-on-ci admits instead of a full review, and an owner command in a reply is still reviewed.",
  "steps": [
    {
      "index": 1,
      "title": "Carry the HTTP status on a transport error",
      "files": [
        "src/repoach/providers/errors.py",
        "src/repoach/agent_engine/adapters.py",
        "tests/unit/test_proxy_gateway_client.py"
      ],
      "action": "GatewayTransportError gains an optional keyword status (int or None), kept as an attribute; the gateway client passes the response status for a 5xx, 408 or 429. A timeout or a refused connection carries None.",
      "commit_message": "feat(provider): carry the http status on a transport error",
      "done_when": "pytest -q tests/unit/test_proxy_gateway_client.py passes",
      "unit_tests": [
        "tests/unit/test_proxy_gateway_client.py::test_call_raises_transport_error_on_retryable_status",
        "tests/unit/test_proxy_gateway_client.py::test_call_raises_transport_error_on_connect_error"
      ]
    },
    {
      "index": 2,
      "title": "Carry the upstream status on a direct provider's transport error",
      "files": [
        "src/repoach/providers/direct.py",
        "tests/unit/test_provider_direct_anthropic.py"
      ],
      "action": "The direct providers pass the upstream status of an in-band error event and of a raised ProviderError to GatewayTransportError.",
      "commit_message": "feat(provider): carry the upstream status on a direct transport error",
      "done_when": "pytest -q tests/unit/test_provider_direct_anthropic.py passes",
      "unit_tests": [
        "tests/unit/test_provider_direct_anthropic.py::test_server_error_is_a_transport_error"
      ]
    },
    {
      "index": 3,
      "title": "Add the transport retry window setting",
      "files": [
        "src/repoach/project/config.py",
        "src/repoach/project/repoach.toml.template",
        "tests/unit/test_project_config.py"
      ],
      "action": "ProviderSection gains transport_retry_window_s, an int from 0 to TRANSPORT_RETRY_WINDOW_MAX_S (1800), default TRANSPORT_RETRY_WINDOW_S (300), a boolean refused; the template documents the key under [provider] with a commented line.",
      "commit_message": "feat(config): add the transport retry window under provider",
      "done_when": "pytest -q tests/unit/test_project_config.py passes",
      "unit_tests": [
        "tests/unit/test_project_config.py::TestTemplate::test_every_key_has_a_template_line_and_round_trips"
      ]
    },
    {
      "index": 4,
      "title": "Let the environment set the window",
      "files": [
        "src/repoach/project/resolve.py",
        "tests/unit/test_review_survives_transport_failures.py"
      ],
      "action": "ENV_NAMES maps provider.transport_retry_window_s to REPOACH_TRANSPORT_RETRY_WINDOW_S, which outranks repoach.toml and is validated with the key's bounds, its error naming the variable.",
      "commit_message": "feat(config): let the environment set the transport retry window",
      "done_when": "pytest -q tests/unit/test_review_survives_transport_failures.py -k environment passes",
      "unit_tests": [
        "tests/unit/test_review_survives_transport_failures.py::test_the_window_comes_from_the_environment_over_the_file"
      ]
    },
    {
      "index": 5,
      "title": "Retry transport errors inside the window of one call",
      "files": [
        "src/repoach/agent_engine/transport_window.py",
        "src/repoach/agent_engine/agent_loop.py",
        "tests/unit/test_review_survives_transport_failures.py"
      ],
      "action": "transport_window.py holds retryable_transport (a GatewayTransportError whose status is not a 4xx), backoff_s (5 s doubling, capped at 60 s), TransportWindow (window_s, an injectable clock, next_wait returning the wait cut to the time left or None) and the window_spent marker. AgentLoop reads the window from route.section and keeps _sleep and _clock seams; _provider_call records one usage row per attempt, logs agent_loop.transport_retry, sleeps and retries while next_wait gives a wait, then re-raises the last error unchanged, marked spent and logged agent_loop.transport_window_spent when the window was open; _call_turn_with_retry re-raises a spent error at once.",
      "commit_message": "feat(agent): retry a transport error inside a per-call window",
      "done_when": "pytest -q tests/unit/test_review_survives_transport_failures.py -k 'window or answers' passes",
      "unit_tests": [
        "tests/unit/test_review_survives_transport_failures.py::test_a_transport_failure_shorter_than_the_window_is_retried_until_it_answers",
        "tests/unit/test_review_survives_transport_failures.py::test_a_failure_longer_than_the_window_ends_the_call_as_before",
        "tests/unit/test_review_survives_transport_failures.py::test_a_closed_window_makes_one_attempt",
        "tests/unit/test_review_survives_transport_failures.py::test_a_4xx_or_a_non_transport_error_is_never_retried"
      ]
    },
    {
      "index": 6,
      "title": "Spend one window per call in the outer retry loops",
      "files": [
        "src/repoach/review/retry_backoff.py",
        "tests/conftest.py"
      ],
      "action": "The suite's conftest sets REPOACH_TRANSPORT_RETRY_WINDOW_S=0 for every test, so the fake providers that raise transport errors keep their single attempt; the window tests open it themselves. retry_with_backoff stops on an exception marked window_spent, logged log_scope.window_spent, returning an exhausted, not fatal, RetryResult, so the reviewer's summary stays '[parse_failed:TRANSPORT] all 3 attempts raised — last=GatewayTransportError' and the judge's deferral 'judge call failed: GatewayTransportError'.",
      "commit_message": "feat(review): spend one transport window per model call",
      "done_when": "pytest -q tests/unit/test_review_survives_transport_failures.py -k 'reviewer or judge' passes",
      "unit_tests": [
        "tests/unit/test_review_survives_transport_failures.py::test_a_reviewer_spends_one_window_and_reports_the_same_line",
        "tests/unit/test_review_survives_transport_failures.py::test_the_judge_after_the_window_defers_with_the_same_reason"
      ]
    },
    {
      "index": 7,
      "title": "Name why an incomplete review is incomplete",
      "files": [
        "src/repoach/review/review_retry.py",
        "src/repoach/cli/review_cmds.py"
      ],
      "action": "review_retry.incomplete_cause(final_verdict, reviews, refutations) returns None for a complete review, transport when every errored reviewer's summary names GatewayTransportError or GatewayChainExhausted (the exhausted-retries summary or the crash summary) and every deferred refutation reads 'judge call failed: <one of them>', other otherwise. review pr prints it in its JSON as incomplete_cause; the exit code is unchanged.",
      "commit_message": "feat(review): name the cause of an incomplete review",
      "done_when": "pytest -q tests/unit/test_review_survives_transport_failures.py -k 'incomplete_cause or exits_3' passes",
      "unit_tests": [
        "tests/unit/test_review_survives_transport_failures.py::test_incomplete_cause_is_transport_only_when_every_failure_is_transport",
        "tests/unit/test_review_survives_transport_failures.py::test_review_pr_still_exits_3_and_names_the_transport_cause"
      ]
    },
    {
      "index": 8,
      "title": "Retry a transport-only failure once per head",
      "files": [
        "src/repoach/review/review_retry.py",
        "src/repoach/cli/review_cmds.py"
      ],
      "action": "dispatch_review_retry reads the PR (refused when not open or a draft, or when its head moved), reads the merge attempts comment (refused when unreadable or when a REVIEW_RETRY attempt exists at the head), records a REVIEW_RETRY attempt naming the failed run (refused when it cannot be written) and dispatches auto-review.yml with pr_number and retry_of. record_retry_ran records the retry run's id and result. repoach review retry <N> --head --run [--retry-of --result] runs one or the other and prints one stderr line, exit 0, or 2 on a forge failure or a refused dispatch.",
      "commit_message": "feat(review): retry a transport-only review failure once per head",
      "done_when": "pytest -q tests/unit/test_review_survives_transport_failures.py -k retr passes",
      "unit_tests": [
        "tests/unit/test_review_survives_transport_failures.py::test_one_retry_is_dispatched_then_the_head_is_never_retried_again",
        "tests/unit/test_review_survives_transport_failures.py::test_the_attempts_comment_names_both_runs",
        "tests/unit/test_review_survives_transport_failures.py::test_a_moved_head_or_a_closed_pr_is_not_retried",
        "tests/unit/test_review_survives_transport_failures.py::test_review_retry_cli_dispatches_once"
      ]
    },
    {
      "index": 9,
      "title": "Re-arm the merge on a reply without reviewing",
      "files": [
        ".github/workflows/auto-review.yml",
        "tests/github_concurrency_sim.py",
        "tests/unit/test_auto_review_replies_do_not_review.py"
      ],
      "action": "auto-review.yml listens to pull_request_review_comment created; the coalesce job takes owner comments and owner /resolve or /reopen replies only; a new rearm job (ubuntu-latest, no permission, group rearm-<pr> with cancel-in-progress, sleep 60) runs on an owner's submitted review; the review job skips every pull_request_review event and a review comment that is not a coalesced command. The simulator gains startsWith and the review_comment_event, pull_request_event and dispatch_event helpers.",
      "commit_message": "ci(review): re-arm the merge on a reply instead of reviewing",
      "done_when": "pytest -q tests/unit/test_auto_review_replies_do_not_review.py -k 'reply or review_event or command' passes",
      "unit_tests": [
        "tests/unit/test_auto_review_replies_do_not_review.py::test_a_reply_never_starts_a_bot_review",
        "tests/unit/test_auto_review_replies_do_not_review.py::test_a_reply_does_not_cancel_the_review_of_a_push",
        "tests/unit/test_auto_review_replies_do_not_review.py::test_an_owner_command_in_a_reply_is_reviewed_and_plain_text_is_not",
        "tests/unit/test_auto_review_replies_do_not_review.py::test_a_reply_run_concludes_success_and_merge_on_ci_admits_it",
        "tests/unit/test_auto_review_replies_do_not_review.py::test_no_review_event_reaches_the_review_job"
      ]
    },
    {
      "index": 10,
      "title": "Retry the review job once on transport errors",
      "files": [
        ".github/workflows/auto-review.yml",
        "tests/unit/test_auto_review_replies_do_not_review.py"
      ],
      "action": "The review job outputs head_sha and incomplete_cause read from its JSON; a retry job (ubuntu-latest, base ref, actions and pull-requests write, no provider key) runs when the review failed with incomplete_cause transport and no retry_of, sleeps 300 s and runs review retry, or, in a run dispatched by github-actions[bot] with retry_of, records the retry run. The review and auto_merge jobs admit that dispatch; auto_fix stays owner-only. workflow_dispatch gains the retry_of input.",
      "commit_message": "ci(review): retry a review incomplete on transport errors once",
      "done_when": "pytest -q tests/unit/test_auto_review_replies_do_not_review.py passes",
      "unit_tests": [
        "tests/unit/test_auto_review_replies_do_not_review.py::test_the_retry_job_runs_once_per_failed_head_and_never_retries_a_retry",
        "tests/unit/test_auto_review_replies_do_not_review.py::test_the_retry_run_reviews_and_merges_but_never_pushes",
        "tests/unit/test_auto_review_replies_do_not_review.py::test_a_bot_dispatch_without_retry_of_reviews_nothing",
        "tests/unit/test_auto_review_replies_do_not_review.py::test_a_push_still_cancels_the_stale_review_of_a_push"
      ]
    },
    {
      "index": 11,
      "title": "Name a claude_code reply past its cap and keep the CLI's message",
      "files": [
        "src/repoach/llm_proxy/providers/exceptions.py",
        "src/repoach/llm_proxy/providers/claude_code/client.py",
        "tests/unit/test_claude_code_output_cap.py"
      ],
      "action": "exceptions.py gains OutputCapExceededError (a ProviderError, status 422, error_type output_cap_exceeded). The claude_code client builds every CLI error through cli_exit_error and cli_result_error: a non-zero exit whose stdout is the CLI's JSON with is_error reads it, the message keeps the CLI's result text up to CLI_ERROR_TEXT_MAX_CHARS (2000) instead of the first 200 characters of stdout, and api_error max_output_tokens raises OutputCapExceededError; other exits keep stderr, else the head of stdout. _spawn_env sets CLAUDE_CODE_MAX_OUTPUT_TOKENS to max_tokens plus CLI_THINKING_HEADROOM_TOKENS (32000).",
      "commit_message": "fix(proxy): name a claude reply past its output cap",
      "done_when": "pytest -q tests/unit/test_claude_code_output_cap.py passes",
      "unit_tests": [
        "tests/unit/test_claude_code_output_cap.py::test_max_tokens_reaches_the_cli_as_its_output_cap",
        "tests/unit/test_claude_code_output_cap.py::test_cap_rides_on_the_filtered_environment",
        "tests/unit/test_claude_code_output_cap.py::test_disabled_thinking_reaches_the_cli_as_a_zero_budget"
      ]
    },
    {
      "index": 12,
      "title": "Trip nothing on a request fault and tell the caller not to repeat it",
      "files": [
        "src/repoach/llm_proxy/api/services.py",
        "src/repoach/llm_proxy/api/models/agent_v1.py",
        "tests/unit/test_proxy_chain_exhaustion.py"
      ],
      "action": "services classifies OutputCapExceededError as output_cap_exceeded, a member of NO_TRIP_REASONS whose failure trips no breaker, and ends an exhausted chain whose last error is one with an output_cap_exceeded error event; a preflight-refused chain answers 502 with Retry-After, the seconds until its first candidate re-opens. The AgentError code literal gains output_cap_exceeded.",
      "commit_message": "fix(proxy): never trip or repeat a reply past its cap",
      "done_when": "pytest -q tests/unit/test_proxy_chain_exhaustion.py passes",
      "unit_tests": [
        "tests/unit/test_proxy_chain_exhaustion.py::test_preflight_exhaustion_returns_502",
        "tests/unit/test_proxy_chain_exhaustion.py::test_exhaustion_emits_terminal_sse_error"
      ]
    },
    {
      "index": 13,
      "title": "Raise a reply past its cap as not retryable on the agent side",
      "files": [
        "src/repoach/llm_proxy/api/agent_dispatcher.py",
        "src/repoach/agent_engine/adapters.py",
        "tests/unit/test_proxy_output_cap_is_not_transport.py"
      ],
      "action": "aggregate_sse_stream records the error event's type; dispatch_agent_request returns an output_cap_exceeded error as stop_reason error with that AgentError code. providers.errors gains GatewayOutputCapError (retryable False) and GatewayTransportError.retry_after_s. ProxyGatewayClient raises GatewayOutputCapError for an output_cap_exceeded error response and reads Retry-After into retry_after_s. TransportWindow waits a Retry-After that ends inside the window and gives up on one that does not.",
      "commit_message": "fix(agent): never retry a reply past its output cap",
      "done_when": "pytest -q tests/unit/test_proxy_output_cap_is_not_transport.py -k 'cap or reopens' passes",
      "unit_tests": [
        "tests/unit/test_proxy_output_cap_is_not_transport.py::test_the_cli_error_keeps_its_result_text_and_names_the_cap",
        "tests/unit/test_proxy_output_cap_is_not_transport.py::test_an_output_cap_fault_trips_nothing_and_reaches_the_caller_as_not_retryable",
        "tests/unit/test_proxy_output_cap_is_not_transport.py::test_the_reviewer_asks_once_and_the_review_is_not_retried_as_transport",
        "tests/unit/test_proxy_output_cap_is_not_transport.py::test_a_tripped_chain_names_when_it_reopens_and_the_window_waits_only_that_long"
      ]
    },
    {
      "index": 14,
      "title": "Map a reply past its cap on the direct claude_code route",
      "files": [
        "src/repoach/providers/direct.py",
        "tests/fixtures/claude_code/max_output_tokens.json",
        "tests/unit/test_proxy_output_cap_is_not_transport.py"
      ],
      "action": "The in-process providers map OutputCapExceededError to GatewayOutputCapError before any other CLI error, so the direct claude_code route never retries it as a transport error. The fixture is the JSON a real claude 2.1.317 printed with CLAUDE_CODE_MAX_OUTPUT_TOKENS=300.",
      "commit_message": "fix(provider): map a direct reply past its cap to a refusal",
      "done_when": "pytest -q tests/unit/test_proxy_output_cap_is_not_transport.py -k direct passes",
      "unit_tests": [
        "tests/unit/test_proxy_output_cap_is_not_transport.py::test_the_direct_claude_code_route_raises_the_same_error_once"
      ]
    },
    {
      "index": 15,
      "title": "Scope the CI breaker to the job and name a retired model at start",
      "files": [
        "src/repoach/llm_proxy/api/runtime.py",
        ".github/workflows/auto-review.yml",
        "tests/unit/test_proxy_output_cap_is_not_transport.py"
      ],
      "action": "Both job proxy starts of auto-review.yml set REPOACH_BREAKER_PROBE_SEED_DB to runner.temp/job_proxy_breaker.db, so a job never rehydrates another job's trips. runtime.report_configured_models_gone, called at startup, logs one proxy_configured_model_gone error per configured MODEL_* entry the breaker holds with a terminal reason, naming its chains and the hours left, and returns the lines.",
      "commit_message": "fix(ci): keep the job proxy breaker to its job",
      "done_when": "pytest -q tests/unit/test_proxy_output_cap_is_not_transport.py -k 'retired or job_proxy' passes",
      "unit_tests": [
        "tests/unit/test_proxy_output_cap_is_not_transport.py::test_a_configured_model_the_provider_retired_is_named_at_start",
        "tests/unit/test_proxy_output_cap_is_not_transport.py::test_the_job_proxy_keeps_its_breaker_to_the_job"
      ]
    },
    {
      "index": 16,
      "title": "Name which model served each role",
      "files": [
        "src/repoach/providers/usage.py",
        "src/repoach/cli/review_cmds.py",
        "tests/unit/test_review_survives_transport_failures.py"
      ],
      "action": "usage.served_by_line(run) groups the run's usage rows by role, in the order each first called, naming the models that answered and, when calls failed, how many and their error classes, or none served. review pr prints it on stderr after its usage line.",
      "commit_message": "feat(review): name which model served each review role",
      "done_when": "pytest -q tests/unit/test_review_survives_transport_failures.py -k served passes",
      "unit_tests": [
        "tests/unit/test_review_survives_transport_failures.py::test_the_report_names_which_model_served_each_role_and_what_failed"
      ]
    },
    {
      "index": 17,
      "title": "Pin how the judge's chain resolved on #288",
      "files": [
        "tests/unit/test_proxy_output_cap_is_not_transport.py"
      ],
      "action": "The judge asks the opus tier through the alias claude-opus-4-7; with nvidia_nim and open_router tripped the opus chain serves claude_code/opus, and with that tail tripped too it falls back to its head nvidia_nim/z-ai/glm-5.2, the chain the job log names; an unclassified name resolves the DEFAULT chain, which has no tail.",
      "commit_message": "test(proxy): pin the opus chain the judge resolved on 288",
      "done_when": "pytest -q tests/unit/test_proxy_output_cap_is_not_transport.py -k judge_asks passes",
      "unit_tests": [
        "tests/unit/test_proxy_output_cap_is_not_transport.py::test_the_judge_asks_the_opus_tier_and_288s_chain_was_its_tripped_tail"
      ]
    },
    {
      "index": 18,
      "title": "Replay #288's outage through the whole review",
      "files": [
        "tests/integration/test_review_survives_transport_failures_end_to_end.py"
      ],
      "action": "Each reviewer's real AgentLoop talks to a provider answering the breaker's 502 until its own fake clock passes the outage, then approving; review pr runs through the CLI over the GitHub forge whose gh transport is the review-team fake. A 220 s outage completes with four approvals and incomplete_cause null; a longer one exits 3 with incomplete_cause transport and today's reviewer summary.",
      "commit_message": "test(review): replay the 288 outage end to end",
      "done_when": "pytest -q tests/integration/test_review_survives_transport_failures_end_to_end.py passes",
      "unit_tests": [
        "tests/integration/test_review_survives_transport_failures_end_to_end.py::test_288s_outage_inside_the_window_costs_time_and_the_review_completes",
        "tests/integration/test_review_survives_transport_failures_end_to_end.py::test_an_outage_past_the_window_still_exits_3_and_names_transport"
      ]
    },
    {
      "index": 19,
      "title": "Hold the merge-wait workflow tests to the new triggers",
      "files": [
        "tests/unit/test_merge_wait_workflows.py"
      ],
      "action": "The fourteen-event and #246 bursts now complete one rearm per burst and no review; the shape test reads the bot filter on the coalesce job's comment author and checks the rearm job's runner, group and review-author filter.",
      "commit_message": "test(ci): pin the merge-wait bursts to the rearm job",
      "done_when": "pytest -q tests/unit/test_merge_wait_workflows.py passes",
      "unit_tests": [
        "tests/unit/test_merge_wait_workflows.py::test_fourteen_review_events_coalesce",
        "tests/unit/test_merge_wait_workflows.py::test_246_bursts_each_review_once",
        "tests/unit/test_merge_wait_workflows.py::test_workflow_shape"
      ]
    },
    {
      "index": 20,
      "title": "Document the window, the retry and the triggers",
      "files": [
        "docs/merge_gate.md",
        "docs/tech_debt.md"
      ],
      "action": "merge_gate.md gains a table of what starts a review and what re-arms a merge, and a section on the transport window, Retry-After, the output-cap fault, the job-scoped breaker, the incomplete_cause field and the one automatic retry; the thread section says a reply runs the rearm job. tech_debt.md row 107, opened with the #288 measurement, is removed; rows 108 to 110 stay open with their measurements (the DEFAULT and literal chains' tail, the CI probe seed, the provider health line).",
      "commit_message": "docs(review): document the transport window and the review triggers",
      "done_when": "docs/merge_gate.md names the window, the retry, the review triggers and the re-arm",
      "unit_tests": []
    }
  ],
  "integration_tests": [
    "tests/integration/test_review_survives_transport_failures_end_to_end.py::test_288s_outage_inside_the_window_costs_time_and_the_review_completes",
    "tests/integration/test_review_survives_transport_failures_end_to_end.py::test_an_outage_past_the_window_still_exits_3_and_names_transport"
  ]
}
```
