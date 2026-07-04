# SP-USAGE-REASONING-SPLIT — Surface reasoning tokens as a first-class usage field

Thread a real (or estimated) reasoning-token count from the OpenAI-compatible transport, through the Anthropic-format SSE builder as a non-usage sibling field, into the agent_v1 Usage schema consumed by /v1/agent, while leaving the /v1/messages usage object byte-compatible with today.

## Step 1 — Expose reasoning-token split in the Anthropic SSE builder

- **Files**: `src/ferova/llm_proxy/core/anthropic/sse.py`, `tests/unit/test_anthropic_sse_reasoning_split.py`
- **Action**: In SSEBuilder, extract the reasoning-token computation already embedded in estimate_output_tokens (the tiktoken-encoded and character-fallback branches both compute a local `reasoning_tokens` value from `self.accumulated_reasoning` before summing it away) into a new public method `estimate_reasoning_tokens() -> int` that returns just that value, reusing the same ENCODER-available/unavailable branching so the two methods never drift. Extend `message_delta(stop_reason, output_tokens, reasoning_tokens: int = 0)` to add a top-level sibling key `reasoning_tokens` to the emitted event dict, placed alongside (not inside) the existing nested `usage` dict, so the Anthropic-shaped `usage` object itself never gains a new key.
- **Commit**: `feat(llm_proxy): expose reasoning-token estimate and wire field on SSE message_delta`
- **Done when**: pytest tests/unit/test_anthropic_sse_reasoning_split.py passes
- **Unit tests**: `tests/unit/test_anthropic_sse_reasoning_split.py::test_estimate_reasoning_tokens_matches_accumulated_reasoning_only`, `tests/unit/test_anthropic_sse_reasoning_split.py::test_estimate_reasoning_tokens_zero_when_no_thinking_text`, `tests/unit/test_anthropic_sse_reasoning_split.py::test_message_delta_emits_reasoning_tokens_as_sibling_not_in_usage`

## Step 2 — Read upstream completion_tokens_details.reasoning_tokens in the OpenAI-compat transport

- **Files**: `src/ferova/llm_proxy/providers/openai_compat.py`, `tests/unit/test_openai_compat_reasoning_extraction.py`
- **Action**: In `_stream_response_impl`, after the streaming loop where `output_tokens` is resolved from `usage_info.completion_tokens` or `sse.estimate_output_tokens()`, resolve a `reasoning_tokens_value`: when `usage_info` is present, read `usage_info.completion_tokens_details.reasoning_tokens`, tolerating a missing `completion_tokens_details` attribute, a `None` detail object, a `None` value, or a non-integer value (all fall back to 0, with exactly one `logger.debug` call on the non-integer/malformed case, never raising); when `usage_info` is absent entirely, fall back to `sse.estimate_reasoning_tokens()` from Step 1. Pass the resolved value into the existing `sse.message_delta(map_stop_reason(finish_reason), output_tokens, reasoning_tokens=reasoning_tokens_value)` call, leaving `output_tokens` semantics (still including reasoning) untouched.
- **Commit**: `feat(llm_proxy): surface upstream reasoning_tokens detail in openai_compat transport`
- **Done when**: pytest tests/unit/test_openai_compat_reasoning_extraction.py passes
- **Unit tests**: `tests/unit/test_openai_compat_reasoning_extraction.py::test_completion_tokens_details_reasoning_is_read`, `tests/unit/test_openai_compat_reasoning_extraction.py::test_missing_completion_tokens_details_defaults_to_zero`, `tests/unit/test_openai_compat_reasoning_extraction.py::test_non_integer_detail_value_defaults_to_zero_and_logs`, `tests/unit/test_openai_compat_reasoning_extraction.py::test_absent_usage_falls_back_to_sse_estimate`

## Step 3 — Add reasoning_tokens to the agent_v1 Usage schema

- **Files**: `src/ferova/llm_proxy/api/models/agent_v1.py`, `tests/unit/test_agent_v1_usage_reasoning_field.py`
- **Action**: Add `reasoning_tokens: int = 0` to the `Usage` pydantic model in agent_v1.py, alongside `input_tokens`/`output_tokens`/`total_tokens`, with no change to `total_tokens` semantics (reasoning stays included in output_tokens, per NG3). Keep the field optional-with-default so existing callers building `Usage(...)` without it keep working.
- **Commit**: `feat(llm_proxy): add reasoning_tokens field to agent_v1 Usage schema`
- **Done when**: pytest tests/unit/test_agent_v1_usage_reasoning_field.py passes
- **Unit tests**: `tests/unit/test_agent_v1_usage_reasoning_field.py::test_usage_reasoning_tokens_defaults_to_zero`, `tests/unit/test_agent_v1_usage_reasoning_field.py::test_usage_accepts_explicit_reasoning_tokens`

## Step 4 — Populate reasoning_tokens end-to-end in the agent dispatcher and lock in acceptance tests

- **Files**: `src/ferova/llm_proxy/api/agent_dispatcher.py`, `tests/unit/test_proxy_usage_reasoning_split.py`, `tests/integration/test_usage_reasoning_split_flow.py`
- **Action**: In `_aggregate_sse_stream`, add a `reasoning_tokens = 0` accumulator alongside `input_tokens`/`output_tokens`, and in the `message_delta` branch read the new top-level `reasoning_tokens` sibling key off the event dict (`data.get("reasoning_tokens")`, defaulting to 0, summed the same way `output_tokens` is), then include it when constructing the returned `Usage(input_tokens=..., output_tokens=..., total_tokens=..., reasoning_tokens=reasoning_tokens)`. Write `tests/unit/test_proxy_usage_reasoning_split.py` with the four spec acceptance tests: AC1 builds a fake message_delta event carrying a non-zero `reasoning_tokens` sibling field (as Step 2's transport would emit from an upstream `completion_tokens_details.reasoning_tokens`) and asserts the resulting agent_v1 `Usage.reasoning_tokens` matches while `output_tokens` is unchanged; AC2 omits the detail field and asserts `reasoning_tokens == 0`; AC3 omits upstream usage entirely and asserts the SSE estimator's reasoning share lands in `reasoning_tokens`; AC4 asserts a `/v1/messages` response payload contains no `reasoning_tokens` key in its usage object. Write `tests/integration/test_usage_reasoning_split_flow.py` with one end-to-end test that drives the agent dispatcher with a fake openai_chat stream whose final usage carries `completion_tokens_details.reasoning_tokens=1200` and asserts the dispatched `/v1/agent` response carries `usage.reasoning_tokens == 1200` while `output_tokens` is unchanged.
- **Commit**: `feat(llm_proxy): populate reasoning_tokens in agent dispatcher and add acceptance tests`
- **Done when**: pytest tests/unit/test_proxy_usage_reasoning_split.py tests/integration/test_usage_reasoning_split_flow.py passes
- **Unit tests**: `tests/unit/test_proxy_usage_reasoning_split.py::test_upstream_reasoning_detail_is_surfaced`, `tests/unit/test_proxy_usage_reasoning_split.py::test_missing_detail_defaults_to_zero`, `tests/unit/test_proxy_usage_reasoning_split.py::test_estimator_split_survives_when_usage_absent`, `tests/unit/test_proxy_usage_reasoning_split.py::test_messages_usage_shape_unchanged`

## Integration tests

- `tests/integration/test_usage_reasoning_split_flow.py::test_agent_dispatch_reports_reasoning_tokens_end_to_end`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-USAGE-REASONING-SPLIT",
  "title": "Surface reasoning tokens as a first-class usage field",
  "summary": "Thread a real (or estimated) reasoning-token count from the OpenAI-compatible transport, through the Anthropic-format SSE builder as a non-usage sibling field, into the agent_v1 Usage schema consumed by /v1/agent, while leaving the /v1/messages usage object byte-compatible with today.",
  "steps": [
    {
      "index": 1,
      "title": "Expose reasoning-token split in the Anthropic SSE builder",
      "files": [
        "src/ferova/llm_proxy/core/anthropic/sse.py",
        "tests/unit/test_anthropic_sse_reasoning_split.py"
      ],
      "action": "In SSEBuilder, extract the reasoning-token computation already embedded in estimate_output_tokens (the tiktoken-encoded and character-fallback branches both compute a local `reasoning_tokens` value from `self.accumulated_reasoning` before summing it away) into a new public method `estimate_reasoning_tokens() -> int` that returns just that value, reusing the same ENCODER-available/unavailable branching so the two methods never drift. Extend `message_delta(stop_reason, output_tokens, reasoning_tokens: int = 0)` to add a top-level sibling key `reasoning_tokens` to the emitted event dict, placed alongside (not inside) the existing nested `usage` dict, so the Anthropic-shaped `usage` object itself never gains a new key.",
      "commit_message": "feat(llm_proxy): expose reasoning-token estimate and wire field on SSE message_delta",
      "done_when": "pytest tests/unit/test_anthropic_sse_reasoning_split.py passes",
      "unit_tests": [
        "tests/unit/test_anthropic_sse_reasoning_split.py::test_estimate_reasoning_tokens_matches_accumulated_reasoning_only",
        "tests/unit/test_anthropic_sse_reasoning_split.py::test_estimate_reasoning_tokens_zero_when_no_thinking_text",
        "tests/unit/test_anthropic_sse_reasoning_split.py::test_message_delta_emits_reasoning_tokens_as_sibling_not_in_usage"
      ]
    },
    {
      "index": 2,
      "title": "Read upstream completion_tokens_details.reasoning_tokens in the OpenAI-compat transport",
      "files": [
        "src/ferova/llm_proxy/providers/openai_compat.py",
        "tests/unit/test_openai_compat_reasoning_extraction.py"
      ],
      "action": "In `_stream_response_impl`, after the streaming loop where `output_tokens` is resolved from `usage_info.completion_tokens` or `sse.estimate_output_tokens()`, resolve a `reasoning_tokens_value`: when `usage_info` is present, read `usage_info.completion_tokens_details.reasoning_tokens`, tolerating a missing `completion_tokens_details` attribute, a `None` detail object, a `None` value, or a non-integer value (all fall back to 0, with exactly one `logger.debug` call on the non-integer/malformed case, never raising); when `usage_info` is absent entirely, fall back to `sse.estimate_reasoning_tokens()` from Step 1. Pass the resolved value into the existing `sse.message_delta(map_stop_reason(finish_reason), output_tokens, reasoning_tokens=reasoning_tokens_value)` call, leaving `output_tokens` semantics (still including reasoning) untouched.",
      "commit_message": "feat(llm_proxy): surface upstream reasoning_tokens detail in openai_compat transport",
      "done_when": "pytest tests/unit/test_openai_compat_reasoning_extraction.py passes",
      "unit_tests": [
        "tests/unit/test_openai_compat_reasoning_extraction.py::test_completion_tokens_details_reasoning_is_read",
        "tests/unit/test_openai_compat_reasoning_extraction.py::test_missing_completion_tokens_details_defaults_to_zero",
        "tests/unit/test_openai_compat_reasoning_extraction.py::test_non_integer_detail_value_defaults_to_zero_and_logs",
        "tests/unit/test_openai_compat_reasoning_extraction.py::test_absent_usage_falls_back_to_sse_estimate"
      ]
    },
    {
      "index": 3,
      "title": "Add reasoning_tokens to the agent_v1 Usage schema",
      "files": [
        "src/ferova/llm_proxy/api/models/agent_v1.py",
        "tests/unit/test_agent_v1_usage_reasoning_field.py"
      ],
      "action": "Add `reasoning_tokens: int = 0` to the `Usage` pydantic model in agent_v1.py, alongside `input_tokens`/`output_tokens`/`total_tokens`, with no change to `total_tokens` semantics (reasoning stays included in output_tokens, per NG3). Keep the field optional-with-default so existing callers building `Usage(...)` without it keep working.",
      "commit_message": "feat(llm_proxy): add reasoning_tokens field to agent_v1 Usage schema",
      "done_when": "pytest tests/unit/test_agent_v1_usage_reasoning_field.py passes",
      "unit_tests": [
        "tests/unit/test_agent_v1_usage_reasoning_field.py::test_usage_reasoning_tokens_defaults_to_zero",
        "tests/unit/test_agent_v1_usage_reasoning_field.py::test_usage_accepts_explicit_reasoning_tokens"
      ]
    },
    {
      "index": 4,
      "title": "Populate reasoning_tokens end-to-end in the agent dispatcher and lock in acceptance tests",
      "files": [
        "src/ferova/llm_proxy/api/agent_dispatcher.py",
        "tests/unit/test_proxy_usage_reasoning_split.py",
        "tests/integration/test_usage_reasoning_split_flow.py"
      ],
      "action": "In `_aggregate_sse_stream`, add a `reasoning_tokens = 0` accumulator alongside `input_tokens`/`output_tokens`, and in the `message_delta` branch read the new top-level `reasoning_tokens` sibling key off the event dict (`data.get(\"reasoning_tokens\")`, defaulting to 0, summed the same way `output_tokens` is), then include it when constructing the returned `Usage(input_tokens=..., output_tokens=..., total_tokens=..., reasoning_tokens=reasoning_tokens)`. Write `tests/unit/test_proxy_usage_reasoning_split.py` with the four spec acceptance tests: AC1 builds a fake message_delta event carrying a non-zero `reasoning_tokens` sibling field (as Step 2's transport would emit from an upstream `completion_tokens_details.reasoning_tokens`) and asserts the resulting agent_v1 `Usage.reasoning_tokens` matches while `output_tokens` is unchanged; AC2 omits the detail field and asserts `reasoning_tokens == 0`; AC3 omits upstream usage entirely and asserts the SSE estimator's reasoning share lands in `reasoning_tokens`; AC4 asserts a `/v1/messages` response payload contains no `reasoning_tokens` key in its usage object. Write `tests/integration/test_usage_reasoning_split_flow.py` with one end-to-end test that drives the agent dispatcher with a fake openai_chat stream whose final usage carries `completion_tokens_details.reasoning_tokens=1200` and asserts the dispatched `/v1/agent` response carries `usage.reasoning_tokens == 1200` while `output_tokens` is unchanged.",
      "commit_message": "feat(llm_proxy): populate reasoning_tokens in agent dispatcher and add acceptance tests",
      "done_when": "pytest tests/unit/test_proxy_usage_reasoning_split.py tests/integration/test_usage_reasoning_split_flow.py passes",
      "unit_tests": [
        "tests/unit/test_proxy_usage_reasoning_split.py::test_upstream_reasoning_detail_is_surfaced",
        "tests/unit/test_proxy_usage_reasoning_split.py::test_missing_detail_defaults_to_zero",
        "tests/unit/test_proxy_usage_reasoning_split.py::test_estimator_split_survives_when_usage_absent",
        "tests/unit/test_proxy_usage_reasoning_split.py::test_messages_usage_shape_unchanged"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_usage_reasoning_split_flow.py::test_agent_dispatch_reports_reasoning_tokens_end_to_end"
  ]
}
```
